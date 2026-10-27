"""Exercise /obs in one real Claude Code process (uses the configured account).

Writes a machine-readable ledger and private raw events under --output.
The test deliberately produces a failed tool call, fixes a tiny program, then
checks the live session through /obs, its views, exports, dashboards and analysis.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from e2e_fixtures import prepare_comparison_inputs
from optional_matrix import prepare_attribution_reference, source_hashes
from optional_matrix import prepare_inputs as prepare_collectors

ROOT = Path(__file__).resolve().parents[1]
FAILURE = 'intentional observability smoke-test failure'
OPENER = build_opener(ProxyHandler({}))


def require(condition, message: str) -> None:
    """Keep validation active under python -O too."""
    if not condition:
        raise AssertionError(message)


def json_object(path: Path) -> dict:
    require(path.is_file(), f'Missing JSON artifact: {path}')
    value = json.loads(path.read_text())
    require(isinstance(value, dict), f'Expected a JSON object in {path}')
    return value


def blocks(events: list[dict], kind: str) -> list[dict]:
    found = []
    for event in events:
        message = event.get('message')
        content = message.get('content', []) if isinstance(message, dict) else []
        if isinstance(content, list):
            found.extend(block for block in content if isinstance(block, dict) and block.get('type') == kind)
    return found


def validate_coding(events: list[dict], work: Path) -> dict:
    uses = {block['id']: block for block in blocks(events, 'tool_use')}
    results = blocks(events, 'tool_result')
    failed = [row for row in results if row.get('is_error') and FAILURE in str(row.get('content'))]
    require(failed, 'No real failed tool_result contains the intentional assertion failure')
    require(any(uses.get(row.get('tool_use_id'), {}).get('name') == 'Bash' for row in failed),
            'The intentional failure did not come from Bash')
    require(any(not row.get('is_error') and uses.get(row.get('tool_use_id'), {}).get('name') == 'Bash'
                and 'add(2, 2)' in str(uses.get(row.get('tool_use_id'), {}).get('input', {})) for row in results),
            'No successful Bash result verifies add(2, 2)')
    require((work / 'add.py').is_file(), 'The model did not create add.py')
    check = subprocess.run([sys.executable, '-I', '-c',
                            ('import runpy; add = runpy.run_path("add.py")["add"]; '
                             'assert add(2, 2) == 4; assert add(-1, 1) == 0')],
                           check=False, cwd=work, capture_output=True, text=True, timeout=10)
    require(check.returncode == 0, f'Independent add.py verification failed: {check.stderr}')
    return {'tool_calls': len(uses), 'intentional_errors': len(failed), 'independent_add_check': True}


def validate_export(directory: Path, session_id: str) -> dict:
    index = json_object(directory / 'index.json')
    require(index.get('schema_version') == 1 and isinstance(index.get('views'), list), 'Invalid view index')
    require(str(index.get('generator', '')).startswith('agent-trajviz '), 'Export is not from TrajViz')
    require({'health', 'issues', 'tool_usage', 'summary'} <= {row.get('id') for row in index['views']},
            'Export is missing diagnostic views')
    for row in index['views']:
        filename = row.get('file', '')
        require(isinstance(filename, str) and Path(filename).name == filename, 'Invalid view filename')
        require(json_object(directory / filename).get('id') == row['id'], f'Wrong view in {filename}')
    trajectory = json_object(directory / 'trajectory.json')
    require(trajectory.get('type') == 'agent-trajviz-trajectory', 'Export is not a trajectory document')
    require(Path(trajectory.get('source', '')).stem == session_id, 'Export does not belong to the live session')
    steps = trajectory.get('steps', [])
    require(any(step.get('role') == 'assistant' for step in steps), 'No assistant steps in trajectory')
    calls = [call for step in steps for call in step.get('tool_calls', [])]
    require(any(call.get('status') == 'error' and FAILURE in str(call) for call in calls),
            'Export did not preserve the failed tool call')
    require(any(row.get('metric') == 'Tool Success'
                for row in json_object(directory / 'health.json').get('data', [])), 'No Tool Success metric')
    require(any(row.get('kind') == 'error' for row in json_object(directory / 'issues.json').get('data', [])),
            'No error diagnostic in export')
    return {'views': len(index['views']), 'steps': len(steps), 'source': trajectory['source']}


def feature_replies(artifacts: Path, feature: str, expected: int = 1) -> dict:
    paths = sorted(artifacts.glob(f'replies-{feature}-*.json'), key=lambda p: (p.stat().st_mtime_ns, p.name))
    require(len(paths) == expected, f'Expected {expected} {feature} reply artifacts, found {len(paths)}')
    return json_object(paths[-1])


def validate_analysis(artifacts: Path, text: str, expected: int = 1) -> dict:
    require(re.search(r'(?mi)^usage: .* input .* output tokens', text), 'Analysis usage summary is missing')
    replies = feature_replies(artifacts, 'analyze', expected)
    require(isinstance(replies.get('hash'), str) and len(replies['hash']) == 64, 'Analysis has no prompt hash')
    rows = replies.get('replies', [])
    require(len(rows) == 1 and isinstance(rows[0].get('text'), str) and rows[0]['text'].strip()
            and 'error' not in rows[0], 'The session-model call did not answer successfully')
    history = json_object(artifacts / 'analyze.json').get('history', [])
    require(len(history) == expected * 2, 'Analysis did not preserve every question and reply')
    require(history and history[-1].get('role') == 'assistant'
            and history[-1].get('content') == rows[0]['text'], 'Analysis history does not contain the reply')
    require('stale_prompts' not in text and 'Cancelled:' not in text, 'Analysis did not finish')
    return {'successful_calls': len(rows), 'reply_characters': len(rows[0]['text']), 'prompt_hash': replies['hash']}


def validate_feature_reply(artifacts: Path, feature: str, text: str, expected: int = 1) -> dict:
    require(re.search(r'(?mi)^usage: .* input .* output tokens', text), f'{feature} usage summary missing')
    require(re.search(rf'(?m)^(?:trajviz-obs: )?{feature}: 1 call\b', text), f'{feature} planned more than one call')
    require(not re.search(r'(?mi)^Errors:|stale_prompts|Cancelled:', text), f'{feature} reported a failure: {text}')
    replies = feature_replies(artifacts, feature, expected)
    require(isinstance(replies.get('hash'), str) and len(replies['hash']) == 64, f'{feature} prompt hash missing')
    rows = replies.get('replies', [])
    require(len(rows) == 1 and isinstance(rows[0].get('text'), str) and rows[0]['text'].strip()
            and 'error' not in rows[0], f'{feature} model call did not answer successfully')
    if feature == 'fixes':
        require('Fixes for the top 1 issue:' in text and 'confidence ' in text and 'no judgment' not in text,
                'The model reply was not applied as a valid fix judgment')
    return {'successful_calls': 1, 'reply_characters': len(rows[0]['text']), 'prompt_hash': replies['hash']}


def validate_labels(path: Path, session_id: str) -> dict:
    sidecar = json_object(path)
    require(sidecar.get('schema_version') == 'trajectory_labels.v2', 'Not a TrajViz v2 label sidecar')
    require(Path(sidecar.get('trajectory_file', '')).stem == session_id, 'Labels describe another session')
    steps = sidecar.get('steps', [])
    require(steps and all(row.get('role') == 'assistant' for row in steps), 'Assistant-only label output is invalid')
    labeled = [row for row in steps if row.get('label_source') == 'llm'
               and row.get('phase') not in (None, '', 'unknown') and row.get('action') not in (None, '', 'unknown')]
    require(len(labeled) == 1, 'Exactly one assistant step should have a valid model label')
    return {'schema_version': sidecar['schema_version'], 'assistant_steps': len(steps), 'model_labeled': len(labeled)}


def written_document(result: dict, prefix: str) -> dict:
    match = re.search(rf'(?m)^(?:trajviz-obs: )?{re.escape(prefix)} written to (.+)\.$', result['result'])
    require(match, f'No artifact path for {prefix}: {result["result"]}')
    return json_object(Path(match[1]))


def validate_bundle(path: Path, session_id: str, *, subagents: bool) -> dict:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        require(archive.testzip() is None and f'{session_id}.jsonl' in names, 'Bundle lost the native parent')
        children = [name for name in names if '/subagents/' in name and name.endswith('.jsonl')]
        require(not subagents or children, 'Bundle lost the native subagent logs')
        for name in names:
            require(not name.startswith('/') and '..' not in Path(name).parts, 'Unsafe ZIP member')
        return {'members': len(names), 'subagent_logs': len(children)}


def exercise_comparisons(turn, output: Path) -> dict:
    fixtures = prepare_comparison_inputs(output / 'comparison inputs')
    before, after, reference, patch = [fixtures[name] for name in ('before', 'after', 'reference', 'anchor')]
    for reverse in (False, True):
        result = turn(f'/obs compare "{before}" --session "{after}" --anchor-patch "{patch}" '
                      f'--fuzzy-commands{" --reverse" if reverse else ""}', 'anchored native comparison',
                      expected=r'Comparison views written to ')
        views = written_document(result, 'Comparison views')['views']
        summary = views['comparison_summary']['data']
        outcome = summary['outcome']
        require((outcome['reference_steps'], outcome['compared_steps']) == ((5, 6) if reverse else (6, 5))
                and summary['anchor_mode'] == 'external', 'Native comparison ignored orientation or patch flags')
    group = turn(f'/obs scorecard "{before}" "{reference}" --session "{after}" '
                 '--labels after,before,reference --fuzzy-commands', 'native three-run scorecard',
                 expected=r'Scorecard views written to ')
    rows = written_document(group, 'Scorecard views')['views']['run_scorecard']['data']
    require([(r['label'], r['steps'], r['tool_calls']) for r in rows]
            == [('after', 5, 3), ('before', 6, 4), ('reference', 5, 3)], 'Native scorecard lost labels or input order')
    group = turn(f'/obs scorecard "{before}" "{after}" --no-self --labels before,after',
                 'native scorecard excluding current run', expected=r'Scorecard views written to ')
    rows = written_document(group, 'Scorecard views')['views']['run_scorecard']['data']
    require([r['label'] for r in rows] == ['before', 'after'], 'Native --no-self retained the selected run')
    cases = [
        (f'"{reference}" "{after}" --anchor-patch "{patch}" --fuzzy-commands --task-id addition', 'pairwise'),
        (f'--batch "{fixtures["before_manifest"]}" --fuzzy-commands', 'batch'),
        (f'--before "{fixtures["before_manifest"]}" --after "{fixtures["after_manifest"]}" --fuzzy-commands', 'before-after'),
    ]
    for arguments, mode in cases:
        result = turn('/obs converge-cli ' + arguments, f'native Converge {mode}',
                      expected=r'Converge report written to ')
        report = written_document(result, 'Converge report')
        if mode == 'pairwise':
            require(report['task_id'] == 'addition' and report['alignment']['alignment_f1'] == 1.0,
                    'Native pairwise did not apply fuzzy/task flags')
        elif mode == 'batch':
            require(report['summary']['compared_count'] == 2 and report['summary']['error_count'] == 0,
                    'Native batch did not compare both manifest entries')
        else:
            require(report['paired_tasks'] == 2 and report['intervention_effect']['harmful_ratio']['direction'] == 'improved',
                    'Native intervention missed the known before/after improvement')
        turn('/obs converge-cli ' + arguments + ' --output summary', f'native Converge {mode} text',
             expected=r'Converge (?:Comparison Summary|Batch Comparison Summary|Intervention Report)')
    turn('/obs', 'return to current session after comparisons', expected=r'obs snapshot \d+')
    return {'anchored_comparisons': 2, 'scorecards': 2, 'converge_modes': 3}


def exercise_collectors(turn, output: Path, fixtures: dict) -> dict:
    before = source_hashes(output / 'collectors' / 'inputs')
    for command in fixtures['commands']:
        result = turn(command, 'native collector export', expected=r'\S')
        require('exited with' not in result['result'], f'Collector failed: {result["result"]}')
    for name in ('cursor', 'opencode', 'codearts'):
        source = output / 'collectors' / 'exports' / f'{name}.json'
        require(source.is_file(), f'Native collector did not write {name}')
        dest = output / 'collected views' / name
        turn(f'/obs json --session "{source}" --with-trajectory -o "{dest}"',
             f'native reload of {name} collector export', expected=r'files .* written to ')
        trajectory = json_object(dest / 'trajectory.json')
        require(any(step.get('role') == 'assistant' for step in trajectory['steps']), f'{name} lost assistant messages')
        if name in ('opencode', 'codearts'):
            summary = json_object(dest / 'summary.json')['data']
            require(summary['tokens']['total'] == 240, f'{name} lost exact provider token accounting')
    legacy = output / 'collectors' / 'exports' / 'codearts-legacy.json'
    require(legacy.is_file(), 'CodeArts legacy archival export was not written')
    turn(f'/obs --session "{legacy}"', 'native legacy CodeArts archive unsupported notice',
         expected=r'legacy message schema is not yet supported.*Re-export.*SQLite')
    require(source_hashes(output / 'collectors' / 'inputs') == before, 'A collector modified its source store')
    turn('/obs', 'return to current session after collectors', expected=r'obs snapshot \d+')
    return {'collectors': 3, 'formats_reloaded': 3, 'legacy_archive_refused_as_trajectory': True,
            'source_stores_unchanged': True}


def validate_subagents(directory: Path, events: list[dict], overview: str) -> dict:
    spawns = [row for row in blocks(events, 'tool_use') if row.get('name') in ('Agent', 'Task')]
    require(spawns, 'No real native subagent tool call occurred')
    ids = {row['id'] for row in spawns}
    results = [row for row in blocks(events, 'tool_result') if row.get('tool_use_id') in ids]
    require(results and all(not row.get('is_error') for row in results), 'The native subagent call did not complete')
    agents = json_object(directory / 'agents.json').get('data', [])
    require(len(agents) >= 2, 'The export did not merge the main agent and native subagent')
    match = re.search(r'\b(\d+) agents\b', overview)
    require(match is not None and int(match.group(1)) >= 2, 'The overview did not count the native subagent')
    trajectory = json_object(directory / 'trajectory.json')
    require(any(row.get('is_sub_agent') for row in trajectory.get('steps', [])), 'No subagent steps in export')
    folder = Path(trajectory['source']).with_suffix('') / 'subagents'
    siblings = [path for path in folder.rglob('agent-*.jsonl') if path.is_file() and not path.is_symlink()]
    require(siblings, 'Claude did not persist a native sibling subagent transcript')
    return {'agent_count': len(agents), 'native_transcripts': len(siblings), 'spawn_calls': len(spawns)}


def subagent_check_command(work: Path) -> str:
    source = str((work / 'add.py').resolve())
    code = (f'import runpy; add = runpy.run_path({source!r})["add"]; '
            'assert add(-1, 1) == 0; print("SUBAGENT_CHECK_OK")')
    return shlex.join([sys.executable, '-I', '-c', code])


def wait_native_subagent(work: Path, session_id: str, env: dict[str, str], timeout: float = 180) -> dict:
    """New hosts launch Agent asynchronously; wait for actual persisted completion.

    Only inspect this test session's known child directory. Waiting for a parent
    turn result alone merely proves the spawn was accepted on these hosts.
    """
    config = Path(env.get('CLAUDE_CONFIG_DIR', str(Path.home() / '.claude')))
    slug = re.sub(r'[^A-Za-z0-9]', '-', str(work))
    folder = config / 'projects' / slug / session_id / 'subagents'
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for path in folder.rglob('agent-*.jsonl'):
            if path.is_symlink() or not path.is_file():
                continue
            rows = []
            for line in path.read_text(encoding='utf-8').splitlines():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass  # The child may be writing its last record.
            assistants = [r for r in rows if r.get('type') == 'assistant']
            if not assistants:
                continue
            content = assistants[-1].get('message', {}).get('content', [])
            text = content if isinstance(content, str) else '\n'.join(
                b.get('text', '') for b in content if isinstance(b, dict) and b.get('type') == 'text')
            uses = {b['id'] for b in blocks(rows, 'tool_use') if b.get('name') == 'Bash'}
            success = any(b.get('tool_use_id') in uses and not b.get('is_error')
                          for b in blocks(rows, 'tool_result'))
            if 'SUBAGENT_CHECK_OK' in text and success:
                return {'transcript': str(path), 'assistant_records': len(assistants), 'verified_bash': True}
        time.sleep(0.2)
    raise TimeoutError(f'Native subagent did not persist its successful completion within {timeout}s')


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def wait_closed(port: int, timeout: float = 15) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            probe.settimeout(0.5)
            if probe.connect_ex(('127.0.0.1', port)) != 0:
                return {'port': port, 'closed': True}
        time.sleep(0.2)
    raise AssertionError(f'Dashboard port {port} is still open after {timeout}s')


def validate_http(url: str, timeout: float = 20) -> dict:
    require(re.fullmatch(r'http://127\.0\.0\.1:\d+/?', url), f'Refusing non-local dashboard URL: {url}')
    deadline = time.monotonic() + timeout
    last_error = ''
    while time.monotonic() < deadline:
        try:
            with OPENER.open(url, timeout=3) as response:
                page = response.read().decode('utf-8')
                require(response.status == 200 and '<html' in page.lower() and 'gradio' in page.lower(),
                        'Dashboard did not serve a Gradio HTML page')
            with OPENER.open(url.rstrip('/') + '/config', timeout=3) as response:
                config = json.load(response)
                require(isinstance(config.get('components'), list) and config['components'],
                        'Dashboard has no Gradio components')
            return {'url': url, 'http_status': 200, 'components': len(config['components'])}
        except (URLError, OSError, ValueError) as exc:
            last_error = str(exc)
            time.sleep(0.25)
    raise AssertionError(f'Dashboard did not become healthy: {url}: {last_error}')


def exercise_command_matrix(turn, output: Path, session_id: str) -> dict:
    """Exercise native routing and validation without additional model calls."""
    index = json_object(output / 'export' / 'index.json')
    saved = output / 'saved-native.jsonl'
    saved.write_bytes((ROOT / 'bridge/tests/fixtures/native_claude_code/django__django-11477.jsonl').read_bytes())
    for row in index['views']:
        vid = row['id']
        turn(f'/obs view {vid} --limit 2 --offset 1 --json', f'native view {vid}',
             expected=rf'^obs view {re.escape(vid)} · ')
    commands = [
        ('/obs help', r'^/obs \[subcommand\]'),
        ('/obs overview', r'obs snapshot \d+'),
        ('/obs view', r'\d+ TrajViz views'),
        ('/obs issues --limit 1', r'^obs view issues'),
        ('/obs patterns', r'^obs view '), ('/obs agents', r'^obs view agents'),
        ('/obs context', r'^obs view context_pressure'), ('/obs timeline', r'^obs view '),
        ('/obs view context_usage agent=main window_limit=100000', r'^obs view context_usage'),
        ('/obs sessions 5', re.escape(session_id)),
        (f'/obs --session {session_id[:8]}', r'obs snapshot \d+'),
        (f'/obs --session "{output / "export/trajectory.json"}"', r'obs snapshot \d+'),
        (f'/obs --session "{saved}"', r'\b23 assistant steps\b'),
        ('/obs', r'obs snapshot \d+'),
        ('/obs pane', r'obs pane (?:opened|is open but not drawn)'),
        ('/obs watch off', r'obs: watch off'), ('/obs watch on', r'obs: watch on'),
        ('/obs statusline on', r'obs: statusline on'), ('/obs statusline off', r'obs: statusline off'),
        ('/obs pane close', r'obs pane closed'),
        (f'/obs schemas "{output / "schemas"}"', r'JSON Schemas of \d+ views'),
        (f'/obs bundle -o "{output / "session.zip"}"', r'Bundle of \d+ files?'),
        (f'/obs compare "{output / "export/trajectory.json"}" --reverse --fuzzy-commands', r'Comparison: reference'),
        (f'/obs scorecard "{output / "export/trajectory.json"}" --labels live,export', r'Scorecard views written'),
        ('/obs collect --version', r'agent-trajviz-collect \d'),
        ('/obs collect cursor --help', r'usage:'),
        ('/obs attribution', r'Attribution|not available|unavailable|agent override|no gold reference'),
        ('/obs health --limit 0', r'limit.*(?:1|500)'),
        ('/obs view missing_view', r'No view'),
        ('/obs view health nonsense=x', r'No option'),
        ('/obs bundle --out', r'needs an output path'),
        ('/obs doctor unexpected', r'Unexpected arguments|Usage:'),
        ('/obs health --unexpected', r'Unknown option'),
        ('/obs --session', r'--session needs'),
        ('/obs report', r'removed.*?/obs dashboard'),
    ]
    for command, expected in commands:
        turn(command, 'native command ' + command, expected=expected)
    require((output / 'session.zip').is_file(), 'Native bundle export missing')
    require((output / 'schemas/health.schema.json').is_file(), 'Native schemas export missing')
    selected = output / 'selected views'
    turn(f'/obs json --view health --view issues -o "{selected}"', 'native filtered JSON export',
         expected=r'3 files .* written to ')
    require({path.name for path in selected.iterdir()} == {'index.json', 'health.json', 'issues.json'},
            'Native JSON --view selection wrote unintended files')
    comparisons = exercise_comparisons(turn, output)
    return {'views': len(index['views']), 'additional_commands': len(commands), **comparisons}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cache', type=Path)
    parser.add_argument('--model', default='haiku')
    parser.add_argument('--skip-llm', action='store_true')
    parser.add_argument('--skip-dashboard', action='store_true')
    parser.add_argument('--test-obs-tool', action='store_true', help='Additional paid turn using the plugin tool')
    parser.add_argument('--test-llm-all', action='store_true', help='Also exercise one fixes and one label model call')
    parser.add_argument('--test-subagent', action='store_true', help='Spawn one real native subagent and verify merging')
    parser.add_argument('--test-command-matrix', action='store_true', help='Exercise all views, commands and invalid inputs')
    parser.add_argument('--budget', default='5')
    parser.add_argument('--attribution-root', type=Path, help='An existing attribution engine checkout for offline native attribution checks.')
    parser.add_argument('--decaf-root', dest='attribution_root', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.test_llm_all and args.skip_llm:
        parser.error('--test-llm-all cannot be combined with --skip-llm')
    output = args.output.resolve()
    if output == ROOT or ROOT in output.parents:
        parser.error('--output must be outside the repository; real session logs are private test artifacts')
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        parser.error('--output must be empty so stale artifacts cannot pass validation')
    work = output / 'workspace'
    work.mkdir(exist_ok=True)
    env = os.environ.copy()
    cache = args.cache.resolve() if args.cache else output / 'cache'
    env['XDG_CACHE_HOME'] = str(cache)
    # No browser windows during an automated test; paths remain in command output.
    env['SSH_TTY'] = 'trajviz-obs-e2e'
    collectors = prepare_collectors(output / 'collectors') if args.test_command_matrix else None
    if collectors:
        env.update(collectors['environment'])
    if args.attribution_root:
        require((args.attribution_root / 'awe/__init__.py').is_file(), '--attribution-root must contain the attribution engine package')
        env.update({'TRAJVIZ_ATTRIBUTION_PATH': str(args.attribution_root.resolve()), 'AWE_PROJECT_DATA': str(output / 'awe-cache'),
                    'AWE_OUTPUT_DATA': str(output / 'awe-output'), 'OPENROUTER_API_KEY': ''})
    tools = 'Bash,Read,Write,Edit' + (',Agent' if args.test_subagent else '')
    if args.test_obs_tool:
        tools += ',ToolSearch'
    argv = [
        'claude', '--plugin-dir', str(ROOT), '--input-format', 'stream-json',
        '--output-format', 'stream-json', '--verbose', '--replay-user-messages', '--model', args.model,
        '--permission-mode', 'dontAsk', '--allowedTools', tools + ',mcp__trajviz-obs__obs',
        '--tools', tools, '--strict-mcp-config',
        '--setting-sources', '', '--settings', json.dumps({'pluginConfigs': {'trajviz-obs': {'options': {
            'analysisModel': 'session', 'labelModel': 'session', 'llm': 'session', 'openBrowser': False}}}}),
        '--max-budget-usd', args.budget,
    ]
    events: queue.Queue[dict | None] = queue.Queue()
    ledger: dict = {'claude_version': subprocess.check_output(['claude', '--version'], text=True).strip(),
                    'commands': [], 'checks': [], 'output': str(output), 'cache': str(cache), 'status': 'running',
                    'coverage': {'analysis': not args.skip_llm,
                                 'dashboards': not args.skip_dashboard, 'model_obs_tool': args.test_obs_tool,
                                 'fixes': args.test_llm_all, 'label': args.test_llm_all, 'subagents': args.test_subagent,
                                 'command_matrix': args.test_command_matrix}}
    ports: set[int] = set()
    with (output / 'stderr.log').open('w') as stderr, (output / 'events.jsonl').open('w') as raw:
        process = subprocess.Popen(argv, cwd=work, env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=stderr, text=True, bufsize=1)

        def read() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                raw.write(line)
                raw.flush()
                try:
                    events.put(json.loads(line))
                except json.JSONDecodeError:
                    pass
            events.put(None)

        reader = threading.Thread(target=read, daemon=True)
        reader.start()

        def check(label: str, operation) -> dict:
            row = {'label': label, 'status': 'running'}
            ledger['checks'].append(row)
            try:
                detail = operation()
                row.update(status='passed', detail=detail)
                print(f'Validated {label}', flush=True)
                return detail
            except BaseException as exc:
                row.update(status='failed', error=f'{type(exc).__name__}: {exc}')
                raise

        def turn(prompt: str, label: str, timeout: float = 240, expected: str | None = None) -> dict:
            print(f'Running {label}', flush=True)
            assert process.stdin is not None
            request_id = str(uuid.uuid4())
            process.stdin.write(json.dumps({'type': 'user', 'message': {'role': 'user', 'content': prompt},
                                            'uuid': request_id}) + '\n')
            process.stdin.flush()
            deadline = time.monotonic() + timeout
            received: list[dict] = []
            acknowledged = False
            while time.monotonic() < deadline:
                try:
                    event = events.get(timeout=min(10, max(0.1, deadline - time.monotonic())))
                except queue.Empty:
                    continue
                if event is None:
                    raise RuntimeError(f'Claude exited during {label}, code {process.poll()}; see stderr.log')
                received.append(event)
                if event.get('type') == 'user' and event.get('uuid') == request_id:
                    acknowledged = True
                if event.get('session_id'):
                    prior = ledger.setdefault('session_id', event['session_id'])
                    require(prior == event['session_id'], f'Session identity changed during {label}')
                if event.get('type') == 'result':
                    if not acknowledged:
                        continue  # A background agent's earlier parent result belongs to another turn.
                    if prompt.startswith('/obs') and event.get('local_command') != 'custom':
                        continue
                    row = {'label': label, 'prompt': prompt, 'result': event.get('result', ''),
                           'is_error': event.get('is_error', False), 'cost_usd': event.get('total_cost_usd', 0),
                           'event_count': len(received), 'status': 'received'}
                    ledger['commands'].append(row)
                    if row['is_error']:
                        raise AssertionError(f'{label}: {row["result"]}')
                    require(event.get('subtype') == 'success', f'{label}: {event}')
                    require(not event.get('permission_denials'), f'{label}: permission denied')
                    if prompt.startswith('/obs'):
                        require(any(item.get('local_command_run', {}).get('command') == 'obs' for item in received),
                                f'{label} did not execute the plugin local command')
                        # The host prefixes local-command text with the plugin name on some surfaces.
                        command_text = re.sub(r'(?m)^trajviz-obs: ', '', row['result'])
                        require(expected is not None and re.search(expected, command_text, re.IGNORECASE | re.MULTILINE),
                                f'{label}: unexpected output (is_error alone is insufficient): {row["result"]}')
                    row['status'] = 'output-validated'
                    print(f'Validated {label}: {str(row["result"])[:180]}', flush=True)
                    return {**event, 'events': received}
            raise TimeoutError(f'{label} exceeded {timeout}s')

        try:
            task = '''Work only in this scratch directory. Use Bash to run exactly:
python3 -c "assert 2 + 2 == 5, 'intentional observability smoke-test failure'"
This failure is intentional. Then create add.py containing a pure add(a, b) function,
run a Python assertion that add(2, 2) == 4 using Bash, and briefly report success.
Use the tools to do this work; do not merely describe commands.'''
            coding = turn(task, 'real coding turn')
            check('real failed tool call and working recovery', lambda: validate_coding(coding['events'], work))
            subagent = None
            if args.test_subagent:
                command = subagent_check_command(work)
                subagent = turn('Use the Agent tool to launch exactly one general-purpose subagent. '
                                f'Ask it to read {str(work / "add.py")!r} with Read, then run exactly this '
                                f'command with Bash:\n{command}\n'
                                'The command verifies add(-1, 1) == 0 and prints SUBAGENT_CHECK_OK. '
                                'Ask it to reply SUBAGENT_CHECK_OK only after the command succeeds. '
                                'It must not modify files. Wait for its result and '
                                'report it. You must actually spawn the subagent; do not do the check yourself.',
                                'real native subagent turn', timeout=240)
                check('native subagent completion',
                      lambda: wait_native_subagent(work, ledger['session_id'], env))
            overview = turn('/obs', 'current-session overview', expected=r'obs snapshot \d+ · .*assistant steps')
            require(FAILURE in overview['result'], 'Overview did not identify the intentional failure')
            turn('/obs health', 'health view', expected=r'obs view health.*\n[\s\S]*Tool Success')
            turn('/obs view tool_usage', 'tool usage view', expected=r'obs view tool_usage.*\n[\s\S]*python')
            turn(f'/obs json --with-trajectory -o "{output / "export"}"', 'JSON export',
                 expected=r'\d+ files .* written to ')
            check('JSON export content and live-session identity',
                  lambda: validate_export(output / 'export', ledger['session_id']))
            if subagent is not None:
                check('native subagent snapshot, merge, and sibling transcript',
                      lambda: validate_subagents(output / 'export', subagent['events'], overview['result']))
            if args.test_command_matrix:
                check('native command and view matrix',
                      lambda: exercise_command_matrix(turn, output, ledger['session_id']))
                check('native collector exports, reloads and source preservation',
                      lambda: exercise_collectors(turn, output, collectors))
                if args.attribution_root:
                    reference = prepare_attribution_reference(output / 'collectors')
                    attribution = turn(f'/obs attribution --session "{reference["transcript"]}" '
                                       f'--reference-root "{reference["reference_root"]}" '
                                       f'--agent {reference["agent"]} --instance {reference["instance"]}',
                                       'native real objective attribution', expected=r'Attribution \(reference\)')
                    require('code_localization' in attribution['result'] and 'missed_relevant_file' in attribution['result'],
                            'Native attribution lost the known objective diagnosis')
                    ledger['checks'].append({'label': 'real attribution native diagnosis', 'status': 'passed',
                                             'mode': 'offline objective detectors, no model judging'})
                    turn('/obs', 'current session after attribution', expected=r'obs snapshot \d+')
                check('native bundle contains the parent and real subagent',
                      lambda: validate_bundle(output / 'session.zip', ledger['session_id'], subagents=bool(subagent)))
            extras = ['app'] if not args.skip_dashboard else []
            if extras:
                turn('/obs setup --extras ' + ','.join(extras), 'optional extras setup', timeout=600,
                     expected=r'TrajViz \S+ is set up at .*extras ')
                require(set(extras) <= set(json_object(cache / 'trajviz-obs' / 'state.json').get('extras', [])),
                        'Setup did not record the requested extras')
            if not args.skip_llm:
                analysis = turn('/obs analyze Identify the failed tool call and the recovery. Be concise.',
                                'session-model analysis', timeout=360, expected=r'analyze: 1 call')
                check('successful analysis reply and replay artifacts', lambda: validate_analysis(
                    cache / 'trajviz-obs' / 'sessions' / ledger['session_id'], analysis['result']))
                followup = turn('/obs analyze', 'default analysis question with saved history',
                                timeout=360, expected=r'analyze: 1 call')
                check('analysis follow-up history and replay', lambda: validate_analysis(
                    cache / 'trajviz-obs' / 'sessions' / ledger['session_id'], followup['result'], expected=2))
            if args.test_llm_all:
                artifacts = cache / 'trajviz-obs' / 'sessions' / ledger['session_id']
                fixes = turn('/obs fixes --limit 1', 'session-model fix judgment', timeout=360,
                             expected=r'fixes: 1 call')
                check('successful fix judgment and replay artifact',
                      lambda: validate_feature_reply(artifacts, 'fixes', fixes['result']))
                label_path = output / 'labels.json'
                labeled = turn(f'/obs label --assistant-only --max-steps 1 -o "{label_path}"',
                               'session-model step labeling', timeout=360, expected=r'label: 1 call')
                check('successful labeling model reply',
                      lambda: validate_feature_reply(artifacts, 'label', labeled['result']))
                check('valid assistant-only label sidecar', lambda: validate_labels(label_path, ledger['session_id']))
                turn(f'/obs labels "{label_path}"', 'label sidecar views',
                     expected=r'Labels: \d+ steps · 1 classified')
                taxonomy = output / 'custom taxonomy.md'
                taxonomy.write_text('# Test taxonomy (v9)\n### inspect\n'
                                    '- `inspect_code`: Inspect code, tool behavior, or its test results.\n',
                                    encoding='utf-8')
                default_labels = turn(f'/obs label --max-steps 1 --model haiku --taxonomy "{taxonomy}" '
                                      '--max-content-chars 400 --delay 0 --max-tokens 256 '
                                      '--user-phase human --user-action request',
                                      'default label output with custom taxonomy, all roles and explicit model',
                                      timeout=360, expected=r'label: 1 call')
                check('second labeling reply and model override', lambda: validate_feature_reply(
                    artifacts, 'label', default_labels['result'], expected=2))
                sidecar = json_object(artifacts / 'labels.json')
                require({row.get('role') for row in sidecar['steps']} >= {'assistant', 'user'},
                        'Default labeling unexpectedly removed user steps')
                require(sum(row.get('label_source') == 'llm' for row in sidecar['steps']) == 1
                        and sidecar.get('model') == 'haiku', 'Default label output lost its cap or model override')
                require(sidecar['taxonomy_version'] == 'v9'
                        and any(row.get('phase') == 'inspect' and row.get('action') == 'inspect_code'
                                for row in sidecar['steps'])
                        and all(row.get('phase') == 'human' and row.get('action') == 'request'
                                for row in sidecar['steps'] if row.get('role') == 'user'),
                        'Native labeling ignored the custom taxonomy or deterministic user tags')
                turn(f'/obs labels "{artifacts / "labels.json"}" "{label_path}"', 'native label comparison',
                     expected=r'Phases compared \(reference: the other sidecar; compared: this one\)')
            if args.test_obs_tool:
                tool = turn('Use ToolSearch to discover mcp__trajviz-obs__obs if needed. '
                            'Call it exactly once for each of these five inputs: '
                            '{"what":"health","limit":4}, {"what":"issues","limit":2}, '
                            '{"what":"brief"}, {"what":"view"}, '
                            '{"what":"view","view":"context_usage","options":{"agent":"main","window_limit":100000},"limit":2}. '
                            'Then briefly report Tool Success. Do not use Bash or read files.',
                            'model invokes every obs tool mode')
                uses = [row for row in blocks(tool['events'], 'tool_use') if row.get('name') == 'mcp__trajviz-obs__obs']
                require(uses, 'The model did not call the obs tool')
                require({'health', 'issues', 'brief', 'view'} <= {use.get('input', {}).get('what') for use in uses}
                        and any(use.get('input', {}).get('view') == 'context_usage' for use in uses)
                        and any(use.get('input', {}).get('what') == 'view' and 'view' not in use.get('input', {})
                                for use in uses), 'The model did not exercise every registered obs tool mode')
                ids = {row['id'] for row in uses}
                returned = [row for row in blocks(tool['events'], 'tool_result') if row.get('tool_use_id') in ids]
                require(len(returned) >= len(uses) and all(not row.get('is_error') for row in returned)
                        and 'Tool Success' in str(returned), 'Obs tool did not return health data')
                ledger['checks'].append({'label': 'real model obs tool call', 'status': 'passed', 'calls': len(uses)})
            doctor = turn('/obs doctor', 'doctor', expected=r'obs doctor\n[\s\S]*\bpython\s+[\s\S]*\btrajviz\s+')
            require(re.search(r'^\s+error\s+', doctor['result'], re.MULTILINE) is None, 'Doctor reported a plugin error')
            if not args.skip_dashboard:
                for command, name in [('dashboard', 'Insight'), ('converge', 'Converge')]:
                    port = free_port()
                    ports.add(port)
                    start = turn(f'/obs {command} --port {port}', f'{name} dashboard startup', timeout=180,
                                 expected=rf'{name} dashboard at http://127\.0\.0\.1:{port}')
                    url = f'http://127.0.0.1:{port}/'
                    if command == 'dashboard':
                        require(f'with session {ledger["session_id"]} loaded' in start['result'],
                                'Insight preload was not wired for the current session')
                    check(f'{name} live HTTP', lambda url=url: validate_http(url))
                    turn(f'/obs {command} --port {port}', f'{name} reuses unchanged server',
                         expected=rf'{name} dashboard already running at http://127\.0\.0\.1:{port}')
                    if command == 'dashboard':
                        saved = output / 'saved-native.jsonl'
                        if not saved.is_file():
                            saved.write_bytes((ROOT / 'bridge/tests/fixtures/native_claude_code/django__django-11477.jsonl')
                                              .read_bytes())
                        selected = turn(f'/obs dashboard --session "{saved}" --port {port}',
                                        'Insight restarts for another session', timeout=180,
                                        expected=rf'Insight dashboard at http://127\.0\.0\.1:{port}')
                        require('with session saved-native loaded' in selected['result'],
                                'Changing Insight source kept the previous preload')
                        turn(f'/obs dashboard --port {port}', 'Insight returns to current session', timeout=180,
                             expected=rf'Insight dashboard at http://127\.0\.0\.1:{port}/? '
                                      rf'with session {ledger["session_id"]} loaded')
                    else:
                        turn(f'/obs converge --data-root "{output}" --port {port}',
                             'Converge restarts for another data root', timeout=180,
                             expected=rf'Converge dashboard at http://127\.0\.0\.1:{port}/? over {re.escape(str(output))}')
                        turn(f'/obs converge --port {port}', 'Converge returns to default data root', timeout=180,
                             expected=rf'Converge dashboard at http://127\.0\.0\.1:{port}/? over ')
                    check(f'{name} source replacement HTTP', lambda url=url: validate_http(url))
                    turn('/obs doctor', f'doctor while {name} is live', expected=r'obs doctor\n[\s\S]*\bpython\s+')
                    check(f'{name} survives another command', lambda url=url: validate_http(url))
                    turn(f'/obs {command} stop', f'{name} stop', expected=rf'{name} dashboard stopped\.')
                    check(f'{name} stop releases port', lambda port=port: wait_closed(port))
                    turn(f'/obs {command} --port {port}', f'{name} restart', timeout=180,
                         expected=rf'{name} dashboard at http://127\.0\.0\.1:{port}')
                    check(f'{name} restarted HTTP', lambda url=url: validate_http(url))
            ledger['status'] = 'passed'
        except BaseException as exc:
            ledger['status'] = 'failed'
            ledger['error'] = f'{type(exc).__name__}: {exc}'
            raise
        finally:
            if process.stdin:
                process.stdin.close()
            shutdown = 'stdin-eof'
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                shutdown = 'terminate'
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    shutdown = 'kill'
                    process.kill()
                    process.wait()
            reader.join(timeout=3)
            ledger['process_exit'] = {'code': process.returncode, 'shutdown': shutdown}
            for port in sorted(ports):
                try:
                    check(f'session-end cleanup of port {port}', lambda port=port: wait_closed(port))
                except Exception as exc:  # noqa: BLE001 - write the cleanup failure to the ledger before failing
                    ledger.update(status='failed', error=ledger.get('error', f'{type(exc).__name__}: {exc}'))
            if ledger['status'] == 'passed' and (process.returncode != 0 or shutdown != 'stdin-eof'):
                ledger.update(status='failed', error=f'Claude did not exit cleanly: {ledger["process_exit"]}')
            (output / 'ledger.json').write_text(json.dumps(ledger, indent=2) + '\n')
            print(f'Ledger: {output / "ledger.json"}', flush=True)
            if ledger['status'] == 'failed' and sys.exc_info()[0] is None:
                raise AssertionError(ledger['error'])


if __name__ == '__main__':
    main()
