#!/usr/bin/env python3
"""Load every upstream format and an installed extension through real bridge subprocesses.

Fixtures are deterministic synthetic exports, not real app sessions or private host logs. This verifies
adapter/bridge compatibility, rather than claiming live app collection coverage. Requires the bridge dev
extra. Extension metadata is installed only in a disposable directory added by an isolated test launcher;
-I intentionally ignores PYTHONPATH, and the managed plugin environment is never changed.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
FORMATS = {'claude_code', 'codex', 'cursor', 'codearts', 'icode', 'opencode', 'pi', 'dsh', 'otlp'}
BASE_MS = 1_792_660_800_000


def require(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def write(path: Path, value: object, *, jsonl: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(row) + '\n' for row in value) if jsonl else json.dumps(value), encoding='utf-8')
    return path


def stamp(index: int) -> str:
    return f'2026-10-22T10:00:{index:02d}.000Z'


def fixtures(root: Path) -> dict[str, Path]:
    """Every format contains one failed Bash call, a final answer and exactly 25 logged tokens."""
    result = {}
    usage = {'input_tokens': 12, 'output_tokens': 8}
    result['claude_code'] = write(root / 'claude.jsonl', [
        {'type': 'user', 'sessionId': 'claude', 'uuid': 'u', 'parentUuid': None, 'timestamp': stamp(0),
         'message': {'role': 'user', 'content': 'Run the synthetic check'}},
        {'type': 'assistant', 'sessionId': 'claude', 'uuid': 'a', 'parentUuid': 'u', 'timestamp': stamp(1),
         'message': {'role': 'assistant', 'id': 'a', 'content': [{'type': 'tool_use', 'id': 'call', 'name': 'Bash',
                     'input': {'command': 'pytest -q'}}], 'usage': usage, 'stop_reason': 'tool_use'}},
        {'type': 'user', 'sessionId': 'claude', 'uuid': 't', 'parentUuid': 'a', 'timestamp': stamp(2),
         'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call',
                                               'content': 'synthetic failure', 'is_error': True}]}},
        {'type': 'assistant', 'sessionId': 'claude', 'uuid': 'f', 'parentUuid': 't', 'timestamp': stamp(3),
         'message': {'role': 'assistant', 'id': 'f', 'content': [{'type': 'text', 'text': 'Failure preserved'}],
                     'usage': {'input_tokens': 3, 'output_tokens': 2}, 'stop_reason': 'end_turn'}},
    ], jsonl=True)
    object_messages = [
        {'info': {'role': 'user', 'id': 'u', 'time': {'created': BASE_MS}},
         'parts': [{'type': 'text', 'text': 'Run the synthetic check'}]},
        {'info': {'role': 'assistant', 'id': 'a', 'time': {'created': BASE_MS + 1000, 'completed': BASE_MS + 2000},
                  'tokens': {'total': 20, 'input': 12, 'output': 8}, 'modelID': 'synthetic'},
         'parts': [{'type': 'tool', 'tool': 'Bash', 'callID': 'call', 'state': {'status': 'error',
                     'input': {'command': 'pytest -q'}, 'output': 'synthetic failure', 'error': 'exit 1'}}]},
        {'info': {'role': 'assistant', 'id': 'f', 'finish': 'stop',
                  'time': {'created': BASE_MS + 3000, 'completed': BASE_MS + 4000},
                  'tokens': {'total': 5, 'input': 3, 'output': 2}},
         'parts': [{'type': 'text', 'text': 'Failure preserved'}]},
    ]
    for name in ('opencode', 'cursor', 'codearts'):
        raw = {'info': {'id': name, 'directory': '/synthetic', 'time': {'created': BASE_MS, 'updated': BASE_MS + 4000}},
               'messages': object_messages}
        if name == 'cursor':
            raw['export_metadata'] = {'source_format': 'cursor_composer'}
        elif name == 'codearts':
            raw['export_metadata'] = {'schema_version': 2, 'source_format': 'codearts_opencode_sqlite'}
        result[name] = write(root / f'{name}.json', raw)
    codex_payloads = [
        ('session_meta', {'id': 'codex', 'cwd': '/synthetic', 'model': 'synthetic'}),
        ('response_item', {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Run check'}]}),
        ('response_item', {'type': 'function_call', 'name': 'exec_command', 'call_id': 'call',
                           'arguments': json.dumps({'cmd': 'pytest -q'})}),
        ('response_item', {'type': 'function_call_output', 'call_id': 'call',
                           'output': json.dumps({'output': 'synthetic failure', 'metadata': {'exit_code': 1}})}),
        ('event_msg', {'type': 'token_count', 'info': {'last_token_usage': {'input_tokens': 12, 'output_tokens': 8,
                                                                          'total_tokens': 20}}}),
        ('response_item', {'type': 'reasoning', 'summary': [{'text': 'The failed check is preserved'}]}),
        ('response_item', {'type': 'message', 'role': 'assistant',
                           'content': [{'type': 'output_text', 'text': 'Failure preserved'}]}),
        ('event_msg', {'type': 'token_count', 'info': {'last_token_usage': {'input_tokens': 3, 'output_tokens': 2,
                                                                          'total_tokens': 5}}}),
        ('event_msg', {'type': 'task_complete'}),
    ]
    result['codex'] = write(root / 'codex.jsonl', [{'type': kind, 'payload': payload, 'timestamp': stamp(i)}
                                                for i, (kind, payload) in enumerate(codex_payloads)], jsonl=True)
    result['pi'] = write(root / 'pi.jsonl', [
        {'type': 'session', 'id': 'pi', 'cwd': '/synthetic', 'timestamp': stamp(0)},
        {'type': 'message', 'id': 'u', 'timestamp': stamp(1), 'message': {'role': 'user', 'content': 'Run check'}},
        {'type': 'message', 'id': 'a', 'timestamp': stamp(2), 'message': {'role': 'assistant', 'model': 'synthetic',
         'content': [{'type': 'toolCall', 'id': 'call', 'name': 'bash', 'arguments': {'command': 'pytest -q'}}],
         'usage': {'input': 12, 'output': 8}, 'stopReason': 'toolUse'}},
        {'type': 'message', 'timestamp': stamp(3), 'message': {'role': 'toolResult', 'toolCallId': 'call',
          'isError': True, 'content': [{'type': 'text', 'text': 'synthetic failure'}]}},
        {'type': 'message', 'id': 'f', 'timestamp': stamp(4), 'message': {'role': 'assistant',
          'content': [{'type': 'text', 'text': 'Failure preserved'}], 'usage': {'input': 3, 'output': 2},
          'stopReason': 'stop'}},
    ], jsonl=True)
    result['dsh'] = write(root / 'dsh.jsonl', [
        {'type': 'session', 'id': 'dsh', 'cwd': '/synthetic', 'createdAt': BASE_MS},
        {'type': 'user/message', 'time': BASE_MS + 1000, 'data': {'content': 'Run check', 'source': {'kind': 'user'}}},
        {'type': 'assistant/message', 'time': BASE_MS + 2000, 'data': {'message': {'id': 'a', 'content': [
            {'type': 'tool-call', 'id': 'call', 'name': 'bash', 'arguments': json.dumps({'command': 'pytest -q'})}]},
            'usage': {'inputTokens': 12, 'outputTokens': 8}}},
        {'type': 'tool/result', 'time': BASE_MS + 3000, 'data': {'message': {'source': {'callId': 'call'},
            'content': [{'type': 'tool-result', 'isError': True, 'content': 'synthetic failure'}]}}},
        {'type': 'assistant/message', 'time': BASE_MS + 4000, 'data': {'message': {'id': 'f', 'content': [
            {'type': 'text', 'text': 'Failure preserved'}]}, 'usage': {'inputTokens': 3, 'outputTokens': 2}}},
    ], jsonl=True)
    def icode_message(role: str, index: int, contents: list, tokens: int = 0) -> dict:
        return {'type': 'message', 'role': role, 'message_id': str(index), 'contents': contents,
                'additional_properties': {'_chrys_created_at': stamp(index), '_group': {'token_count': tokens}}}
    result['icode'] = write(root / 'icode.json', {
        'meta': {'session_id': 'icode', 'primary_cwd': '/synthetic', 'created_at': stamp(0), 'updated_at': stamp(4)},
        '_chrys_export': {'format': 'chrys-expanded-session-v1'}, 'state': {'messages': [
            icode_message('user', 0, [{'type': 'text', 'text': 'Run check'}]),
            icode_message('assistant', 1, [{'type': 'function_call', 'call_id': 'call', 'name': 'sh',
                                           'arguments': json.dumps({'command': 'pytest -q'})}], 20),
            icode_message('tool', 2, [{'type': 'function_result', 'call_id': 'call', 'result': 'synthetic failure',
                                     'additional_properties': {'failed': True}}]),
            icode_message('assistant', 3, [{'type': 'text', 'text': 'Failure preserved'}], 5),
        ]}})
    def span(index: int, name: str, parent: int | None = None, **values) -> dict:
        attributes = {'session.id': 'controlled', 'agent_id': 'main', **values}
        return {'name': name, 'traceId': '1' * 32, 'spanId': f'{index:016x}',
                **({'parentSpanId': f'{parent:016x}'} if parent else {}),
                'startTimeUnixNano': str(BASE_MS * 1_000_000 + index * 100_000_000),
                'endTimeUnixNano': str(BASE_MS * 1_000_000 + index * 100_000_000 + 50_000_000),
                'attributes': [{'key': key, 'value': {
                    'boolValue' if isinstance(value, bool) else 'intValue' if isinstance(value, int) else 'stringValue': value
                }} for key, value in attributes.items()]}
    result['otlp'] = write(root / 'otlp.json', {'resourceSpans': [{'resource': {'attributes': []},
        'scopeSpans': [{'scope': {'name': 'claude-code'}, 'spans': [
            span(1, 'claude_code.interaction', user_prompt='Run check'),
            span(2, 'claude_code.llm_request', 1, request_id='request-a', input_tokens=12, output_tokens=8,
                 cache_read_tokens=0, cache_creation_tokens=0, reasoning_tokens=0,
                 **{'response.model_output': 'Checking'}),
            span(3, 'claude_code.tool', 2, request_id='request-a', tool_name='Bash', tool_use_id='call',
                 tool_input=json.dumps({'command': 'pytest -q'}), tool_output='synthetic failure', success=False),
            span(4, 'claude_code.llm_request', 1, request_id='request-b', input_tokens=3, output_tokens=2,
                 cache_read_tokens=0, cache_creation_tokens=0, reasoning_tokens=0,
                 **{'response.model_output': 'Failure preserved'}),
            span(5, 'claude_code.tool.execution', 3, tool_use_id='call', success=False),
        ]}]}]})
    return result


def run(python: str, output: Path) -> dict:
    sources = fixtures(output / 'fixtures')
    original = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in sources.items()}
    ledger: dict = {'status': 'running', 'commands': [], 'formats': {}, 'schema_validations': 0}
    env = {**os.environ, 'XDG_CACHE_HOME': str(output / 'cache'), 'CLAUDE_CONFIG_DIR': str(output / 'config')}
    def call(command: str, *args: str, launcher: Path = ROOT / 'bridge/trajviz_obs_cli.py',
             status: int = 0, error: str | None = None) -> dict:
        started = time.monotonic()
        result = subprocess.run([python, '-I', str(launcher), command, *args], cwd=output, env=env,
                                capture_output=True, text=True, timeout=120, check=False)
        row = {'command': command, 'args': list(args), 'exit': result.returncode,
               'seconds': round(time.monotonic() - started, 3)}
        ledger['commands'].append(row)
        require(result.returncode == status, f'{command}: {result.stdout}\n{result.stderr}')
        value = json.loads(result.stdout)
        require(value.get('error', {}).get('code') == error, f'{command}: unexpected error {value}')
        return value
    def doc(path: Path) -> dict:
        return json.loads(path.read_text(encoding='utf-8'))
    def validate(views: Path, schemas: Path) -> None:
        for entry in doc(views / 'index.json')['views']:
            view = doc(views / entry['file'])
            Draft202012Validator(doc(schemas / f'{view["id"]}.schema.json')).validate(view)
            ledger['schema_validations'] += 1
    try:
        declared = json.loads(subprocess.check_output([python, '-I', '-c',
            'import json,agent_trajviz as tv; print(json.dumps(dict(tv.FORMATS)))'], text=True))
        require(set(declared) == FORMATS == set(sources), 'Upstream format inventory changed; extend fixtures')
        schemas = output / 'schemas'
        call('schemas', '--out', str(schemas))
        for name, source in sources.items():
            artifacts = output / 'artifacts' / name
            args = ['--transcript', str(source), '--artifacts', str(artifacts)]
            snapshot = call('snapshot', *args)
            require(snapshot['trajviz']['format'] == name and snapshot['counts']['assistant_steps'] == 2
                    and snapshot['counts']['tool_calls'] == 1 and snapshot['tools']['failed'] == 1,
                    f'{name}: native format identity, assistant steps or failed tool was lost')
            require(snapshot['tokens']['total'] == 25, f'{name}: logged 25-token total changed')
            inline = call('view', *args, 'tool_outcomes', '--json')
            exported = Path(call('json', *args, '--with-trajectory')['path'])
            validate(exported, schemas)
            trajectory = doc(exported / 'trajectory.json')
            calls = [tool for step in trajectory['steps'] for tool in step.get('tool_calls', [])]
            require(trajectory['format'] == name and len(calls) == 1 and calls[0]['tool_name'] == 'Bash'
                    and calls[0]['status'] == 'error' and calls[0]['input']['command'] == 'pytest -q',
                    f'{name}: normalized tool identity, command or result did not survive export')
            require(doc(Path(inline['path'])) == doc(exported / 'tool_outcomes.json'),
                    f'{name}: inline view and full exported view disagree')
            reloaded = call('snapshot', '--transcript', str(exported / 'trajectory.json'))
            require(reloaded['trajviz']['format'] == name and reloaded['tokens'] == snapshot['tokens']
                    and reloaded['counts']['tool_calls'] == 1 and reloaded['tools']['failed'] == 1,
                    f'{name}: reloadable trajectory lost identity, tokens or failed tool')
            bundled = call('bundle', '--transcript', str(source), '--out', str(artifacts / 'portable.zip'))
            portable = call('snapshot', '--transcript', bundled['path'])
            require(portable['trajviz']['format'] == name and portable['tokens'] == snapshot['tokens']
                    and portable['counts']['tool_calls'] == 1 and portable['tools']['failed'] == 1,
                    f'{name}: bundled trajectory lost identity, tokens or failed tool')
            require(hashlib.sha256(source.read_bytes()).hexdigest() == original[name], f'{name}: source was changed')
            ledger['formats'][name] = {'label': declared[name], 'assistant_steps': 2, 'failed_tools': 1, 'tokens': 25,
                                       'views': len(doc(exported / 'index.json')['views'])}
        # Real importlib.metadata distribution discovery, without adding anything to the managed environment.
        site = output / 'extension-site'
        site.mkdir()
        write(site / 'custom.json', {'agent': 'matrix-custom', 'turns': [{'role': 'user', 'text': 'Inspect'},
                                                                    {'role': 'assistant', 'text': 'Complete'}]})
        module = site / 'matrix_extension.py'
        module.write_text('''import agent_trajviz as tv

def register():
    tv.register_format(tv.Format(key="matrix-custom", label="Matrix Custom",
        detect=lambda doc: isinstance(doc, dict) and doc.get("agent") == "matrix-custom",
        parse=lambda doc: [{"role": t["role"], "text_preview": t["text"]} for t in doc["turns"]] ))
    tv.register_view("matrix_custom_view", lambda run, marker="default": {
        "steps": len(run.steps), "assistant_steps": sum(s.get("role") == "assistant" for s in run.steps),
        "marker": marker}, title="Matrix custom view", kind="summary", options={"marker": "Test marker"},
        schema={"type":"object","required":["steps","assistant_steps","marker"],"additionalProperties":False,
                "properties":{"steps":{"type":"integer"},"assistant_steps":{"type":"integer"},
                              "marker":{"type":"string"}}})
''')
        metadata = site / 'matrix_extension-0.0.0.dist-info'
        metadata.mkdir()
        (metadata / 'METADATA').write_text('Metadata-Version: 2.1\nName: matrix-extension\nVersion: 0.0.0\n')
        (metadata / 'entry_points.txt').write_text('[agent_trajviz.plugins]\nmatrix = matrix_extension:register\n')
        launcher = output / 'extension_launcher.py'
        launcher.write_text('import runpy,sys\n' + f'sys.path.insert(0, {str(site)!r})\n'
                            + f'runpy.run_path({str(ROOT / "bridge/trajviz_obs_cli.py")!r}, run_name="__main__")\n')
        custom = site / 'custom.json'
        custom_args = ['--transcript', str(custom), '--artifacts', str(output / 'artifacts' / 'custom')]
        unavailable = call('snapshot', *custom_args, status=1, error='load_error')
        require(unavailable['error'].get('trajviz_code') == 'unknown_format', 'The custom format leaked outside its site')
        catalog = call('view', *custom_args, launcher=launcher)
        require(any(view['id'] == 'matrix_custom_view' for view in catalog['views']), 'Installed view was not discovered')
        custom_view = call('view', *custom_args, 'matrix_custom_view', '--option', 'marker=installed', launcher=launcher)
        require(doc(Path(custom_view['path']))['data'] == {'steps': 2, 'assistant_steps': 1, 'marker': 'installed'},
                'Entry-point format parser or custom view option did not run')
        custom_export = Path(call('json', *custom_args, '--with-trajectory', launcher=launcher)['path'])
        custom_schemas = output / 'extension-schemas'
        call('schemas', '--out', str(custom_schemas), launcher=launcher)
        validate(custom_export, custom_schemas)
        require(doc(custom_export / 'trajectory.json')['format'] == 'matrix-custom'
                and doc(custom_export / 'matrix_custom_view.json')['data']['marker'] == 'default',
                'Full JSON export omitted the installed custom format/view')
        ledger.update(status='passed', extension={'discovery': 'temporary dist-info with explicit isolated launcher',
                      'format': 'matrix-custom', 'view': 'matrix_custom_view', 'managed_environment_modified': False},
                      unsupported_builtins=['OpenHands', 'SWE-agent'])
    except BaseException as exc:
        ledger.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        (output / 'ledger.json').write_text(json.dumps(ledger, indent=2) + '\n')
    return ledger


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='Empty directory outside the repo; otherwise use temporary files.')
    parser.add_argument('--python', default=sys.executable)
    args = parser.parse_args()
    with (contextlib.nullcontext(args.output) if args.output else
          tempfile.TemporaryDirectory(prefix='trajviz-obs-formats-')) as folder:
        output = Path(folder).resolve()
        if output.is_relative_to(ROOT):
            parser.error('--output must be outside the repository')
        output.mkdir(parents=True, exist_ok=True)
        if any(output.iterdir()):
            parser.error('--output must be empty and outside the repository')
        ledger = run(os.path.abspath(shutil.which(args.python) or args.python), output)
    print(f'Passed {len(ledger["formats"])} formats, {len(ledger["commands"])} bridge subprocesses, '
          f'{ledger["schema_validations"]} schema validations and isolated installed extension discovery.')


if __name__ == '__main__':
    main()
