"""Exercise /obs in one real Claude Code process (uses the configured account).

Writes a machine-readable ledger and private raw events under --output.
The test deliberately produces a failed tool call, fixes a tiny program, then
checks the live session through /obs, its views, exports, report and analysis.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

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


def validate_report(path: Path) -> dict:
    require(path.is_file(), f'Report was not written: {path}')
    text = path.read_text()
    require(text.count('<!DOCTYPE html>') == 1 and 'Plotly' in text, 'Not a complete Plotly HTML report')
    require(path.stat().st_size > 1_000_000, 'Offline report has no embedded Plotly bundle')
    require(re.search(r'<script\b[^>]*\bsrc=[\"\'][^\"\']*plotly', text, re.IGNORECASE) is None,
            'Offline report still loads a Plotly script externally')
    return {'path': str(path), 'bytes': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def validate_analysis(artifacts: Path, text: str) -> dict:
    require(re.search(r'(?mi)^usage: .* input .* output tokens', text), 'Analysis usage summary is missing')
    replies = json_object(artifacts / 'replies-analyze.json')
    require(isinstance(replies.get('hash'), str) and len(replies['hash']) == 64, 'Analysis has no prompt hash')
    rows = replies.get('replies', [])
    require(len(rows) == 1 and isinstance(rows[0].get('text'), str) and rows[0]['text'].strip()
            and 'error' not in rows[0], 'The session-model call did not answer successfully')
    history = json_object(artifacts / 'analyze.json').get('history', [])
    require(history and history[-1].get('role') == 'assistant'
            and history[-1].get('content') == rows[0]['text'], 'Analysis history does not contain the reply')
    require('stale_prompts' not in text and 'Cancelled:' not in text, 'Analysis did not finish')
    return {'successful_calls': len(rows), 'reply_characters': len(rows[0]['text']), 'prompt_hash': replies['hash']}


def validate_feature_reply(artifacts: Path, feature: str, text: str) -> dict:
    require(re.search(r'(?mi)^usage: .* input .* output tokens', text), f'{feature} usage summary missing')
    require(re.search(rf'(?m)^(?:trajviz-obs: )?{feature}: 1 call\b', text), f'{feature} planned more than one call')
    require(not re.search(r'(?mi)^Errors:|stale_prompts|Cancelled:', text), f'{feature} reported a failure: {text}')
    replies = json_object(artifacts / f'replies-{feature}.json')
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cache', type=Path)
    parser.add_argument('--model', default='haiku')
    parser.add_argument('--skip-llm', action='store_true')
    parser.add_argument('--skip-report', action='store_true')
    parser.add_argument('--skip-dashboard', action='store_true')
    parser.add_argument('--test-obs-tool', action='store_true', help='Additional paid turn using the plugin tool')
    parser.add_argument('--test-llm-all', action='store_true', help='Also exercise one fixes and one label model call')
    parser.add_argument('--test-subagent', action='store_true', help='Spawn one real native subagent and verify merging')
    parser.add_argument('--budget', default='5')
    args = parser.parse_args()
    if args.test_llm_all and args.skip_llm:
        parser.error('--test-llm-all cannot be combined with --skip-llm')
    output = args.output.resolve()
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
                    'coverage': {'report': not args.skip_report, 'analysis': not args.skip_llm,
                                 'dashboards': not args.skip_dashboard, 'model_obs_tool': args.test_obs_tool,
                                 'fixes': args.test_llm_all, 'label': args.test_llm_all, 'subagents': args.test_subagent}}
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
                        require(expected is not None and re.search(expected, row['result'], re.IGNORECASE | re.MULTILINE),
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
                subagent = turn('Use the Agent tool to launch exactly one general-purpose subagent. '
                                'Ask it to read add.py, verify add(-1, 1) == 0 with Bash, and reply '
                                'SUBAGENT_CHECK_OK. It must not modify files. Wait for its result and '
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
            extras = ['charts'] if not args.skip_report else []
            if not args.skip_dashboard:
                extras.append('app')
            if extras:
                turn('/obs setup --extras ' + ','.join(extras), 'optional extras setup', timeout=600,
                     expected=r'TrajViz \S+ is set up at .*extras ')
                require(set(extras) <= set(json_object(cache / 'trajviz-obs' / 'state.json').get('extras', [])),
                        'Setup did not record the requested extras')
            if not args.skip_report:
                turn(f'/obs report --offline -o "{output / "report.html"}"', 'offline HTML report', timeout=360,
                     expected=r'Report written to ')
                check('offline HTML report content', lambda: validate_report(output / 'report.html'))
            if not args.skip_llm:
                analysis = turn('/obs analyze Identify the failed tool call and the recovery. Be concise.',
                                'session-model analysis', timeout=360, expected=r'analyze: 1 call')
                check('successful analysis reply and replay artifacts', lambda: validate_analysis(
                    cache / 'trajviz-obs' / 'sessions' / ledger['session_id'], analysis['result']))
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
            if args.test_obs_tool:
                tool = turn('Use ToolSearch to discover mcp__trajviz-obs__obs if needed, '
                            'then call it with {"what":"health","limit":4}. '
                            'Use the tool, then report the Tool Success metric. Do not use Bash or read files.',
                            'model invokes obs tool')
                uses = [row for row in blocks(tool['events'], 'tool_use') if row.get('name') == 'mcp__trajviz-obs__obs']
                require(uses, 'The model did not call the obs tool')
                ids = {row['id'] for row in uses}
                returned = [row for row in blocks(tool['events'], 'tool_result') if row.get('tool_use_id') in ids]
                require(returned and all(not row.get('is_error') for row in returned)
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
