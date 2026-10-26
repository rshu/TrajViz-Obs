#!/usr/bin/env python3
"""Exercise the real bridge process and independently validate every exported view.

Requires the bridge's dev extra. No model calls, account access, or network requests.
Artifacts go to a temporary directory, or an explicitly supplied empty --output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from e2e_fixtures import native_run, prepare_comparison_inputs
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'bridge/tests/fixtures/native_claude_code/django__django-11477.jsonl'


def require(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def document(path: Path) -> dict:
    value = json.loads(path.read_text(encoding='utf-8'))
    require(isinstance(value, dict), f'Expected an object in {path}')
    return value


def run_matrix(python: str, output: Path) -> dict:
    ledger: dict = {'status': 'running', 'commands': [], 'schema_validations': 0}
    env = {**os.environ, 'XDG_CACHE_HOME': str(output / 'cache'), 'CLAUDE_CONFIG_DIR': str(output / 'config')}
    artifacts = output / 'artifacts'
    source = output / 'native session with spaces.jsonl'
    source.write_bytes(FIXTURE.read_bytes())
    common = ['--transcript', str(source), '--artifacts', str(artifacts), '--window', '200000']
    comparison_inputs = prepare_comparison_inputs(output / 'comparisons')
    run_before, run_after = comparison_inputs['before'], comparison_inputs['after']
    run_reference, patch = comparison_inputs['reference'], comparison_inputs['anchor']
    ledger['fixtures'] = {key: str(path) for key, path in comparison_inputs.items()}

    def call(command: str, *args: str, status: int = 0, error: str | None = None) -> dict:
        argv = [python, '-I', str(ROOT / 'bridge/trajviz_obs_cli.py'), command, *args]
        start = time.monotonic()
        result = subprocess.run(argv, cwd=output, env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=120, check=False)
        row = {'command': command, 'args': list(args), 'exit': result.returncode, 'status': 'failed',
               'seconds': round(time.monotonic() - start, 3)}
        ledger['commands'].append(row)
        require(result.returncode == status, f'{argv}: exit {result.returncode}: {result.stdout}\n{result.stderr}')
        # A whole stdout JSON parse also detects accidental upstream prints before/after the response.
        value = json.loads(result.stdout, parse_constant=lambda v: (_ for _ in ()).throw(ValueError(v)))
        require(isinstance(value, dict), f'{command} did not answer with an object')
        require(value.get('error', {}).get('code') == error, f'{argv}: unexpected error: {value}')
        row['status'] = 'passed'
        return value

    def validate(view: dict) -> None:
        vid = view['id']
        Draft202012Validator(document(output / 'schemas' / f'{vid}.schema.json')).validate(view)
        ledger['schema_validations'] += 1

    def saved_views(path: str) -> dict:
        views = document(Path(path))['views']
        for view in views.values():
            validate(view)
        return views

    def converge(*args: str) -> dict:
        result = call('converge-cli', '--artifacts', str(artifacts), '--', *args)
        require(result['exit'] == 0 and result['path'], f'Converge failed: {result}')
        return document(Path(result['path']))

    try:
        before = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
        call('doctor')
        catalog = call('view', *common)['views']
        ids = {entry['id'] for entry in catalog}
        require(len(ids) == len(catalog) and len(ids) >= 35, 'Incomplete/duplicate view catalog')
        schemas = call('schemas', '--out', str(output / 'schemas'))
        for filename in schemas['files']:
            Draft202012Validator.check_schema(document(output / 'schemas' / filename))
        exported = output / 'export'
        call('json', *common, '--out', str(exported), '--with-trajectory')
        index = document(exported / 'index.json')
        require({row['id'] for row in index['views']} == ids, 'Export and catalog differ')
        for entry in catalog:
            vid = entry['id']
            full = document(exported / f'{vid}.json')
            validate(full)
            for extra in ([], ['--json']):
                inline = call('view', *common, vid, '--limit', '2', '--offset', '1', *extra)
                require(inline['view']['id'] == vid and inline['bytes'] == Path(inline['path']).stat().st_size,
                        f'Invalid view envelope: {vid}')
                validate(document(Path(inline['path'])))
                require(len(json.dumps(inline).encode()) <= 256 * 1024, f'Unbounded inline output: {vid}')
        # Independent invariants over the normalized trajectory, rather than more calls to view().
        trajectory = document(exported / 'trajectory.json')
        steps = trajectory['steps']
        summary = document(exported / 'summary.json')['data']
        calls = [c for step in steps for c in step.get('tool_calls', [])]
        require(summary['steps'] == len(steps) and summary['tool_calls'] == len(calls), 'Counts disagree')
        require(summary['assistant_steps'] == sum(s.get('role') == 'assistant' for s in steps),
                'Assistant count disagrees')
        outcomes = document(exported / 'tool_outcomes.json')['data']['calls']
        require(len(outcomes) == len(calls) == 24 and sum(c.get('status') == 'error' for c in calls) == 1,
                'The public native fixture lost tool calls or its known failure')
        call('snapshot', *common, '--max-points', '2')
        call('snapshot', '--transcript', str(exported / 'trajectory.json'))
        selected = output / 'selected'
        selected_doc = call('json', *common, '--out', str(selected), '--view', 'health', '--view', 'issues')
        require(set(selected_doc['files']) == {'index.json', 'health.json', 'issues.json'}, 'View filtering failed')
        require({row['id'] for row in document(selected / 'index.json')['views']} == {'health', 'issues'},
                'Filtered index advertises unexported views')
        for vid in ('health', 'issues'):
            require(document(selected / f'{vid}.json') == document(exported / f'{vid}.json'),
                    f'Filtering changed complete {vid} data')
        # Replacing existing exports must produce complete documents, without staging residue.
        (selected / 'health.json').write_text('previous export')
        call('json', *common, '--out', str(selected), '--view', 'health')
        require(document(selected / 'health.json') == document(exported / 'health.json')
                and not list(selected.glob('.trajviz-obs-*')), 'JSON overwrite was incomplete')
        for view in ('context_pressure', 'context_usage'):
            optioned = call('view', *common, view, '--option', 'agent=main', '--option', 'window_limit=100000')
            option_view = document(Path(optioned['path']))
            validate(option_view)
            data = option_view['data']
            window = data['stats']['window_limit'] if view == 'context_pressure' else data['window_limit']
            agent = data['agent'] if view == 'context_pressure' else data['agent_key']
            require((window, agent) == (100000, '__main__'),
                    f'{view} ignored the agent or window override')
        usage = call('view', *common, 'context_usage', '--option', 'snapshot_step=3', '--json')
        require(document(Path(usage['path']))['data']['step'] == 3,
                'Context snapshot option was not applied')
        bundle = call('bundle', '--transcript', str(source), '--out', str(output / 'session.zip'))
        with zipfile.ZipFile(bundle['path']) as archive:
            require(archive.testzip() is None and len(archive.namelist()) == bundle['members_total'], 'Invalid ZIP')
            require(any(archive.read(name) == source.read_bytes() for name in archive.namelist()),
                    'Bundle does not preserve the source transcript')
        # A real native directory tree, including nested workflow agents and their metadata.
        bundle_id = 'ddd44444-0000-4000-8000-000000000004'
        bundle_parent = native_run(output / 'bundle-input' / f'{bundle_id}.jsonl', bundle_id)
        child_root = bundle_parent.with_suffix('') / 'subagents'
        native_run(child_root / 'agent-one.jsonl', bundle_id, agent='one')
        nested = native_run(child_root / 'workflows' / 'wf-1' / 'agent-two.jsonl', bundle_id, agent='two')
        nested.with_suffix('.meta.json').write_text(json.dumps({'agentType': 'Explore', 'description': 'Nested worker'}))
        inputs = [bundle_parent, *sorted(child_root.rglob('*'))]
        originals = {p.relative_to(bundle_parent.parent).as_posix(): p.read_bytes() for p in inputs if p.is_file()}
        bundled = call('bundle', '--transcript', str(bundle_parent), '--out', str(output / 'nested-session.zip'))
        require(set(bundled['members']) == set(originals) and bundled['members_total'] == 4,
                'Bundle omitted a parent, child or nested metadata file')
        with zipfile.ZipFile(bundled['path']) as archive:
            require(archive.testzip() is None and set(archive.namelist()) == set(originals), 'Nested ZIP is corrupt')
            require(all(archive.read(name) == content for name, content in originals.items()),
                    'ZIP contents changed native session data')
        merged = call('snapshot', '--transcript', bundled['path'])
        require(merged['counts']['assistant_steps'] == 12, 'Bundled subagents did not survive a real TrajViz reload')

        # Resolve the same run by path, id, unique prefix and cross-project exact id.
        cwd = output / 'project with spaces'
        cwd.mkdir()
        config = output / 'config'
        project = config / 'projects' / re.sub(r'[^A-Za-z0-9]', '-', str(cwd))
        project.mkdir(parents=True)
        session_ids = ('aaa11111-0000-4000-8000-000000000001', 'aaa22222-0000-4000-8000-000000000002',
                       'bbb33333-0000-4000-8000-000000000003')
        for number, sid in enumerate(session_ids):
            target = project / f'{sid}.jsonl'
            records = [json.loads(line) for line in (run_before if number < 2 else run_after).read_text().splitlines()]
            target.write_text(''.join(json.dumps({**row, 'sessionId': sid}) + '\n' for row in records))
            os.utime(target, (1_700_000_100 + number * 100, 1_700_000_100 + number * 100))
        location = ['--cwd', str(cwd), '--config-dir', str(config)]
        listed = call('sessions', *location, '--limit', '2')['sessions']
        require([row['id'] for row in listed] == list(reversed(session_ids[1:])), 'Session listing order/limit failed')
        for spec in (session_ids[-1], session_ids[-1][:8], str(project / f'{session_ids[-1]}.jsonl'),
                     os.path.relpath(project / f'{session_ids[-1]}.jsonl', cwd)):
            resolved = call('resolve', *location, '--session', spec)
            require(resolved['session_id'] == session_ids[-1]
                    and Path(resolved['path']).resolve() == (project / f'{session_ids[-1]}.jsonl').resolve(),
                    f'Session selector resolved the wrong file: {spec}')
        session_directory = project / session_ids[-1]
        session_directory.mkdir()
        require(call('resolve', *location, '--session', str(session_directory))['kind'] == 'dir',
                'Native session directory selector failed')
        ambiguous = call('resolve', *location, '--session', 'aaa', status=1, error='ambiguous')
        require({row['id'] for row in ambiguous['error']['candidates']} == set(session_ids[:2]),
                'Ambiguous selector did not identify both candidates')
        require(call('resolve', '--cwd', str(output), '--config-dir', str(config),
                     '--session-id', session_ids[-1])['session_id'] == session_ids[-1],
                'Cross-project exact session lookup failed')
        for reverse in ([], ['--reverse']):
            compared = call('compare', *common, '--other', str(exported / 'trajectory.json'),
                            '--fuzzy-commands', *reverse)
            for view in document(Path(compared['path']))['views'].values():
                validate(view)
        scorecard = call('scorecard', *common, '--others', str(exported / 'trajectory.json'), '--labels', 'raw,reloaded')
        for view in document(Path(scorecard['path']))['views'].values():
            validate(view)
        call('scorecard', *common, '--no-self', '--others', str(source), str(exported / 'trajectory.json'))
        # Different before/after runs prove orientation, grounding and the fuzzy composite-command flag.
        paired = ['--transcript', str(run_after), '--other', str(run_before), '--artifacts', str(artifacts),
                  '--anchor-patch', str(patch)]
        for reverse in ([], ['--reverse']):
            compared = call('compare', *paired, '--fuzzy-commands', *reverse)
            views = saved_views(compared['path'])
            outcome = views['comparison_summary']['data']['outcome']
            require(compared['orientation'] == {'reference': str(run_after if reverse else run_before),
                                                'compared': str(run_before if reverse else run_after)},
                    'Comparison orientation was not reversed')
            require((outcome['reference_steps'], outcome['compared_steps']) == ((5, 6) if reverse else (6, 5))
                    and views['comparison_summary']['data']['anchor_mode'] == 'external',
                    'Comparison lost known before/after steps or external patch grounding')
        fuzzy_options = ['--transcript', str(run_after), '--other', str(run_reference),
                         '--artifacts', str(artifacts), '--anchor-patch', str(patch)]
        exact = call('compare', *fuzzy_options)['summary']['alignment']['alignment_f1']
        fuzzy = call('compare', *fuzzy_options, '--fuzzy-commands')['summary']['alignment']['alignment_f1']
        require(exact < fuzzy == 1.0, 'Fuzzy matching did not recognize the composite shell read')
        group = call('scorecard', '--transcript', str(run_after), '--artifacts', str(artifacts),
                     '--others', str(run_before), str(run_reference), '--labels', 'after,before,reference',
                     '--fuzzy-commands')
        group_views = saved_views(group['path'])
        require(group['rows'] == group_views['run_scorecard']['data'], 'Scorecard envelope and saved rows disagree')
        require([(row['label'], row['steps'], row['tool_calls'], row['tool_success_pct']) for row in group['rows']]
                == [('after', 5, 3, 100.0), ('before', 6, 4, 75.0), ('reference', 5, 3, 100.0)],
                'Scorecard lost native failure recovery, labels or run order')
        no_self = call('scorecard', '--transcript', str(output / 'intentionally-missing.jsonl'),
                       '--artifacts', str(artifacts), '--no-self', '--others', str(run_before), str(run_after),
                       '--labels', 'before,after')
        require([row['path'] for row in no_self['rows']] == [str(run_before), str(run_after)],
                '--no-self read the absent current run or reordered supplied runs')
        saved_views(no_self['path'])
        for feature, reply in [
            ('analyze', 'The failed tool call is preserved.'),
            ('fixes', '{"where":"CLAUDE.md","fix":"Verify assumptions first","also":"","confidence":"high"}'),
            ('label', '{"phase":"understand","action":"code_reading"}'),
        ]:
            options = ['--feature', feature, '--max-steps', '1', '--limit', '1', '--assistant-only']
            plan = call('llm-prompt', *common, *options)
            require(plan['plan']['calls'] == 1, f'{feature}: incorrect planned call count')
            replies = output / f'{feature}-replies.json'
            replies.write_text(json.dumps({'hash': plan['hash'], 'replies': [{'index': 0, 'text': reply}]}))
            applied = call('llm-apply', *common, *options, '--replies', str(replies))
            if feature == 'analyze':
                require(applied['reply'] == reply, 'Analysis did not apply the recorded reply')
            elif feature == 'fixes':
                require(not applied['errors'] and len(applied['issues']) == 1
                        and applied['issues'][0].get('judgment') == json.loads(reply),
                        'Fix judgment was not applied successfully')
            elif feature == 'label':
                labeled = call('labels', '--sidecar', applied['path'])
                label_views = saved_views(labeled['path'])
                label_ids = {'labels_summary', 'label_phase_counts', 'label_action_counts',
                             'label_phase_durations', 'label_action_durations', 'label_timeline'}
                require(set(label_views) == label_ids and labeled['summary']['classified'] == 1,
                        'The label export omitted a complete view or the replayed classification')
                require(label_views['label_phase_counts']['data']['counts'] == {'understand': 1}
                        and label_views['label_action_counts']['data']['counts'] == {'code_reading': 1},
                        'Label charts do not contain the recorded phase/action')
                sidecar = document(Path(applied['path']))
                classified = [row for row in sidecar['steps'] if row.get('phase') == 'understand']
                require(len(classified) == 1, 'The bounded label replay classified more than one step')
                classified[0].update(phase='implement', action='implement_runtime_logic')
                other_labels = output / 'other-labels.json'
                other_labels.write_text(json.dumps(sidecar))
                compared_labels = call('labels', '--sidecar', applied['path'], '--other', str(other_labels))
                compared_views = saved_views(compared_labels['path'])
                require(set(compared_views) == label_ids | {'label_phase_count_comparison',
                                                            'label_phase_duration_comparison'},
                        'Label comparison did not export all eight schema-valid views')
                phase_pair = compared_views['label_phase_count_comparison']['data']
                require(phase_pair['reference']['counts'] == {'implement': 1}
                        and phase_pair['compared']['counts'] == {'understand': 1},
                        'Label comparison swapped its reference/compared sidecars')
        require(call('brief', *common)['brief'], 'Empty statistics brief')
        attribution = call('attribution', *common, '--reference-root', str(output / 'no-reference'))
        require(attribution.get('available') is False and attribution.get('reason'), 'Untruthful attribution')
        pair_report = converge(str(run_reference), str(run_after), '--anchor-patch', str(patch),
                               '--fuzzy-commands', '--task-id', 'addition')
        require(pair_report['task_id'] == 'addition' and pair_report['anchor_mode'] == 'external'
                and pair_report['alignment']['alignment_f1'] == 1.0
                and pair_report['anchor_analysis']['compared']['anchor_files_written'] == 1,
                'Converge pairwise options lost the task, grounding or fuzzy shell read')
        manifest_before, manifest_after = comparison_inputs['before_manifest'], comparison_inputs['after_manifest']
        batch_report = converge('--batch', str(manifest_before), '--fuzzy-commands')
        require(batch_report['summary']['task_count'] == batch_report['summary']['compared_count'] == 2
                and batch_report['summary']['error_count'] == 0 and batch_report['summary']['anchored_count'] == 2,
                'Converge batch did not load both relative-path manifest entries and anchors')
        require({row['task_id'] for row in batch_report['per_task']} == {'addition', 'addition-repeat'}
                and all(row['alignment_summary']['harmful_ratio'] == 0.25 for row in batch_report['per_task']),
                'Converge batch did not preserve the known failed validation')
        intervention = converge('--before', str(manifest_before), '--after', str(manifest_after), '--fuzzy-commands')
        effect = intervention['intervention_effect']
        require(intervention['paired_tasks'] == 2 and intervention['unpaired_before'] == intervention['unpaired_after'] == 0
                and effect['harmful_ratio']['before_mean'] == 0.25 and effect['harmful_ratio']['after_mean'] == 0.0
                and effect['harmful_ratio']['direction'] == 'improved'
                and effect['alignment_f1']['after_mean'] == 1.0,
                'Converge before/after pairing lost the independently known recovery improvement')
        for options, heading in [
            ([str(run_reference), str(run_after)], 'Converge Comparison Summary'),
            (['--batch', str(manifest_before)], 'Converge Batch Comparison Summary'),
            (['--before', str(manifest_before), '--after', str(manifest_after)], 'Converge Intervention Report'),
        ]:
            text_result = call('converge-cli', '--artifacts', str(artifacts), '--', *options, '--output', 'summary')
            require(text_result['exit'] == 0 and text_result['path'] is None and heading in text_result['text'],
                    'Converge text output did not pass through without creating a JSON artifact')
        require(call('collect', '--', '--version')['exit'] == 0, 'Collector version failed')
        for collector in ('cursor', 'opencode', 'codearts'):
            require(call('collect', '--', collector, '--help')['exit'] == 0, f'{collector} help failed')
        # Real subprocess usage, bad input and structured failure protocol.
        for argv, status, code in [
            (['view', *common, 'missing_view'], 1, 'unknown_view'),
            (['view', *common, 'health', '--option', 'nonsense=x'], 1, 'bad_option'),
            (['view', *common, 'context_pressure', '--option', 'window_limit=nan'], 1, 'bad_option'),
            (['view', *common, 'context_usage', '--option', 'snapshot_step=invalid'], 1, 'bad_option'),
            (['view', *common, 'context_pressure', '--option', 'agent=missing-agent'], 1, 'bad_option'),
            (['view', *common, 'health', '--limit', '0'], 2, 'usage'),
            (['view', *common, 'health', '--offset', '-1'], 2, 'usage'),
            (['snapshot', '--transcript', str(output / 'missing.jsonl')], 1, 'load_error'),
            (['resolve', '--session', str(output / 'missing.jsonl')], 1, 'not_found'),
            (['sessions', '--limit', '501'], 2, 'usage'),
            (['json', *common, '--out', str(output / 'bad'), '--view', 'missing_view'], 1, 'unknown_view'),
            (['bundle', '--transcript', str(source), '--out', str(source)], 1, 'bad_output'),
            (['compare', *common, '--other', str(run_before), '--anchor-patch', str(output / 'missing.diff')],
             1, 'not_found'),
            (['scorecard', '--transcript', str(source), '--no-self', '--others', str(run_after)], 1, 'compare_failed'),
            (['labels', '--sidecar', str(source)], 1, 'load_error'),
        ]:
            call(argv[0], *argv[1:], status=status, error=code)
        for options, expected in [(['--before', str(manifest_before)], 2),
                                  (['--batch', str(output / 'missing-manifest.json')], 1)]:
            failure = call('converge-cli', '--artifacts', str(artifacts), '--', *options)
            require(failure['exit'] == expected and failure['path'] is None and failure['text'],
                    'Converge did not preserve its own input/usage failure status')
        # A full JSON export must protect both the parent and nested child inputs on disk.
        protected_parent = artifacts / 'protected' / 'trajectory.json'
        protected_parent.parent.mkdir()
        protected_parent.write_bytes(source.read_bytes())
        original_parent = protected_parent.read_bytes()
        call('json', '--transcript', str(protected_parent), '--out', str(protected_parent.parent),
             '--with-trajectory', status=1, error='bad_output')
        require(protected_parent.read_bytes() == original_parent, 'JSON export overwrote its transcript')
        call('bundle', '--transcript', str(bundle_parent), '--out', str(nested), status=1, error='bad_output')
        require(nested.read_bytes() == originals[nested.relative_to(bundle_parent.parent).as_posix()],
                'Bundle replaced a nested subagent input')
        require(hashlib.sha256(source.read_bytes()).hexdigest() == before, 'The source fixture was changed')
        ledger.update(status='passed', views=len(ids), schemas=len(schemas['files']))
    except BaseException as exc:
        ledger.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        (output / 'ledger.json').write_text(json.dumps(ledger, indent=2) + '\n', encoding='utf-8')
    return ledger


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', default=sys.executable, help='Interpreter containing the bridge dependencies')
    parser.add_argument('--output', type=Path, help='An empty directory; omitted uses a disposable temporary directory')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='trajviz-obs-matrix-') as temporary:
        output = (args.output or Path(temporary)).resolve()
        if output.is_relative_to(ROOT):
            parser.error('--output must be outside the repository')
        output.mkdir(parents=True, exist_ok=True)
        if any(output.iterdir()):
            parser.error('--output must be empty')
        # Resolving a virtualenv's Python symlink discards its site-packages.
        result = run_matrix(os.path.abspath(shutil.which(args.python) or args.python), output)
        print(f"Passed {len(result['commands'])} subprocess commands, {result['views']} views, "
              f"{result['schemas']} schemas, {result['schema_validations']} view validations.")


if __name__ == '__main__':
    main()
