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
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

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
        for view in ('context_pressure', 'context_usage'):
            call('view', *common, view, '--option', 'agent=main', '--option', 'window_limit=100000')
        bundle = call('bundle', '--transcript', str(source), '--out', str(output / 'session.zip'))
        with zipfile.ZipFile(bundle['path']) as archive:
            require(archive.testzip() is None and len(archive.namelist()) == bundle['members_total'], 'Invalid ZIP')
            require(any(archive.read(name) == source.read_bytes() for name in archive.namelist()),
                    'Bundle does not preserve the source transcript')
        for dark in ([], ['--dark']):
            report = call('report', *common, '--offline', '--out', str(output / ('dark.html' if dark else 'light.html')),
                          *dark)
            html = Path(report['path']).read_text(encoding='utf-8')
            require('<!DOCTYPE html>' in html and 'Plotly' in html and report['bytes'] > 1_000_000, 'Invalid report')
        for reverse in ([], ['--reverse']):
            compared = call('compare', *common, '--other', str(exported / 'trajectory.json'),
                            '--fuzzy-commands', *reverse)
            for view in document(Path(compared['path']))['views'].values():
                validate(view)
        scorecard = call('scorecard', *common, '--others', str(exported / 'trajectory.json'), '--labels', 'raw,reloaded')
        for view in document(Path(scorecard['path']))['views'].values():
            validate(view)
        call('scorecard', *common, '--no-self', '--others', str(source), str(exported / 'trajectory.json'))
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
                labeled = call('labels', '--sidecar', applied['path'], '--other', applied['path'])
                require(labeled['summary']['classified'] == 1 and 'comparison' in labeled,
                        'Labels/phase comparison did not apply the single reply')
        require(call('brief', *common)['brief'], 'Empty statistics brief')
        attribution = call('attribution', *common, '--reference-root', str(output / 'no-reference'))
        require(attribution.get('available') is False and attribution.get('reason'), 'Untruthful attribution')
        converged = call('converge-cli', '--artifacts', str(artifacts), '--', str(source), str(source))
        require(converged['exit'] == 0 and document(Path(converged['path'])), 'Converge CLI did not export JSON')
        require(call('collect', '--', '--version')['exit'] == 0, 'Collector version failed')
        for collector in ('cursor', 'opencode', 'codearts'):
            require(call('collect', '--', collector, '--help')['exit'] == 0, f'{collector} help failed')
        # Real subprocess usage, bad input and structured failure protocol.
        for argv, status, code in [
            (['view', *common, 'missing_view'], 1, 'unknown_view'),
            (['view', *common, 'health', '--option', 'nonsense=x'], 1, 'bad_option'),
            (['view', *common, 'context_pressure', '--option', 'window_limit=nan'], 1, 'bad_option'),
            (['view', *common, 'health', '--limit', '0'], 2, 'usage'),
            (['view', *common, 'health', '--offset', '-1'], 2, 'usage'),
            (['snapshot', '--transcript', str(output / 'missing.jsonl')], 1, 'load_error'),
            (['resolve', '--session', str(output / 'missing.jsonl')], 1, 'not_found'),
            (['sessions', '--limit', '501'], 2, 'usage'),
            (['json', *common, '--out', str(output / 'bad'), '--view', 'missing_view'], 1, 'unknown_view'),
            (['report', *common, '--out', str(source)], 1, 'bad_output'),
        ]:
            call(argv[0], *argv[1:], status=status, error=code)
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
        output = args.output.resolve() if args.output else Path(temporary)
        output.mkdir(parents=True, exist_ok=True)
        if any(output.iterdir()):
            parser.error('--output must be empty')
        # Resolving a virtualenv's Python symlink discards its site-packages.
        result = run_matrix(os.path.abspath(shutil.which(args.python) or args.python), output)
        print(f"Passed {len(result['commands'])} subprocess commands, {result['views']} views, "
              f"{result['schemas']} schemas, {result['schema_validations']} view validations.")


if __name__ == '__main__':
    main()
