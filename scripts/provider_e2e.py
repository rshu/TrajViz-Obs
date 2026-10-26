#!/usr/bin/env python3
"""Exercise native /obs env-mode commands against controlled localhost providers.

No external model requests are made: every turn must be a native plugin command with zero Claude cost.
Requires Claude Code function hooks and an existing plugin cache (or first-use package network access).
Only synthetic credentials and a public transcript fixture are sent to the local HTTP server.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import subprocess
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'bridge/tests/fixtures/native_claude_code/django__django-11477.jsonl'
LABEL = '{"phase":"understand","action":"code_reading"}'
JUDGMENT = '{"where":"CLAUDE.md","fix":"Check assumptions before editing","also":"","confidence":"high"}'
SECRET = 'sk-ant-abcdefghijklmnopqrstuvwxyz0123'
PRIVATE_BODY = 'synthetic-provider-body-must-stay-private'


def require(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class Provider:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.responses: deque[tuple[int, object]] = deque()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                pass

            def do_POST(self) -> None:
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.calls.append({'path': self.path, 'headers': dict(self.headers), 'body': request})
                if not owner.responses:
                    status, response = 500, {'error': 'unexpected additional provider request'}
                else:
                    status, response = owner.responses.popleft()
                if isinstance(response, str):
                    response = ({'content': [{'type': 'text', 'text': response}]} if self.path.endswith('/messages')
                                else {'choices': [{'message': {'content': response}}]})
                payload = json.dumps(response).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}/v1'

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        require(not self.thread.is_alive(), 'Local provider server did not stop')


class NativeSession:
    def __init__(self, work: Path, cache: Path, env: dict[str, str], ledger: dict) -> None:
        self.events: queue.Queue[dict | None] = queue.Queue()
        self.ledger = ledger
        self.stderr = (work / 'stderr.log').open('w')
        self.raw = (work / 'events.jsonl').open('w')
        argv = ['claude', '--plugin-dir', str(ROOT), '--input-format', 'stream-json', '--output-format',
                'stream-json', '--verbose', '--replay-user-messages', '--permission-mode', 'dontAsk',
                '--tools', '', '--strict-mcp-config', '--setting-sources', '', '--settings', json.dumps({
                    'pluginConfigs': {'trajviz-obs': {'options': {'llm': 'env', 'labelMaxSteps': 2,
                        'watch': False, 'statusline': False, 'openBrowser': False}}}}), '--max-budget-usd', '0.01']
        self.process = subprocess.Popen(argv, cwd=work, env={**env, 'XDG_CACHE_HOME': str(cache)},
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr,
                                        text=True, bufsize=1)

        def read() -> None:
            assert self.process.stdout is not None
            for line in self.process.stdout:
                self.raw.write(line)
                self.raw.flush()
                try:
                    self.events.put(json.loads(line))
                except json.JSONDecodeError:
                    pass
            self.events.put(None)

        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()

    def turn(self, command: str, expected: str, *, timeout: float = 120) -> str:
        require(command.startswith('/obs '), 'Only plugin commands are permitted in provider E2E')
        request_id = str(uuid.uuid4())
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps({'type': 'user', 'uuid': request_id,
                                             'message': {'role': 'user', 'content': command}}) + '\n')
        self.process.stdin.flush()
        deadline, received, acknowledged = time.monotonic() + timeout, [], False
        while time.monotonic() < deadline:
            try:
                event = self.events.get(timeout=min(5, max(0.1, deadline - time.monotonic())))
            except queue.Empty:
                continue
            require(event is not None, f'Claude exited during {command}; see stderr.log')
            received.append(event)
            if event.get('type') == 'user' and event.get('uuid') == request_id:
                acknowledged = True
            if event.get('type') != 'result' or not acknowledged or event.get('local_command') != 'custom':
                continue
            text = re.sub(r'(?m)^trajviz-obs: ', '', event.get('result', ''))
            row = {'command': command, 'text': text, 'cost_usd': event.get('total_cost_usd', 0)}
            self.ledger['commands'].append(row)
            require(not event.get('is_error') and event.get('subtype') == 'success', f'Native command failed: {text}')
            require(not event.get('permission_denials'), f'Native command permission denied: {text}')
            require(any(item.get('local_command_run', {}).get('command') == 'obs' for item in received),
                    'The command did not execute through the native /obs hook')
            require(row['cost_usd'] == 0, 'A provider test unexpectedly called the Claude session model')
            require(re.search(expected, text, re.MULTILINE), f'Unexpected native output: {text}')
            require(PRIVATE_BODY not in text, 'Provider response body leaked into native command output')
            return text
        raise TimeoutError(f'Native command exceeded {timeout}s: {command}')

    def close(self) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        self.reader.join(timeout=5)
        self.stderr.close()
        self.raw.close()
        require(not self.reader.is_alive(), 'Claude event reader did not stop')


def exercise(provider_name: str, output: Path, cache: Path) -> dict:
    work = output / provider_name
    work.mkdir()
    source = work / f'{uuid.uuid4()}.jsonl'
    source.write_bytes(FIXTURE.read_bytes())
    artifacts = cache / 'trajviz-obs' / 'sessions' / source.stem
    ledger: dict = {'provider': provider_name, 'commands': [], 'status': 'running', 'artifacts': str(artifacts)}
    provider = Provider()
    session = None
    try:
        env = {key: value for key, value in os.environ.items() if not key.startswith(('ANALYZE_', 'LABEL_'))}
        env.update(NO_PROXY='127.0.0.1,localhost', no_proxy='127.0.0.1,localhost', SSH_TTY='trajviz-provider-e2e')
        for prefix in ('ANALYZE', 'LABEL'):
            env.update({f'{prefix}_BASE_URL': provider.base, f'{prefix}_API_KEY': f'synthetic-{prefix.lower()}-key',
                        f'{prefix}_MODEL': f'local-{prefix.lower()}', f'{prefix}_PROVIDER': provider_name})
        env.update(LABEL_MAX_TOKENS='321', LABEL_TEMPERATURE='0.17')
        session = NativeSession(work, cache, env, ledger)
        selector = f'--session "{source}"'

        def turn(feature: str, replies: list[tuple[int, object]], expected: str) -> tuple[str, list[dict]]:
            require(not provider.responses, 'A previous provider response was never consumed')
            start = len(provider.calls)
            provider.responses.extend(replies)
            text = session.turn('/obs ' + feature, expected)
            require(not provider.responses, 'The native command made fewer provider calls than expected')
            return text, provider.calls[start:]

        _, first = turn(f'analyze {selector} Explain this failure with {SECRET}', [(200, 'first local answer')],
                        r'analyze: 1 call.*model ANALYZE_MODEL \(env\)[\s\S]*first local answer')
        require(len(first) == 1 and SECRET not in json.dumps(first) and '[REDACTED]' in json.dumps(first),
                'Analysis question credentials were not redacted before HTTP')
        _, second = turn(f'analyze {selector} What changed?', [(200, 'second local answer')], r'second local answer')
        require(len(second) == 1 and second[0]['body']['messages'][-2:] == [
            {'role': 'assistant', 'content': 'first local answer'}, {'role': 'user', 'content': 'What changed?'}],
            'Native env analysis did not restore its two-question history')
        history = json.loads((artifacts / 'analyze.json').read_text())['history']
        require(len(history) == 4 and history[-1]['content'] == 'second local answer', 'Analysis history was not saved')
        fix_text, fixes = turn(f'fixes {selector} --limit 1', [(200, JUDGMENT)],
                               r'fixes: 1 call[\s\S]*Check assumptions before editing')
        require(len(fixes) == 1 and 'confidence high' in fix_text, 'Native fix judgment was not parsed/applied')

        labels = work / 'labels.json'
        label_text, calls = turn(f'label {selector} --max-steps 9 --assistant-only --model local-override -o "{labels}"',
                                 [(200, LABEL), (200, LABEL)], r'label: 2 calls.*model local-override \(env\)')
        sidecar = json.loads(labels.read_text())
        require(len(calls) == 2 and all(call['body']['model'] == 'local-override' for call in calls),
                'Headless labelMaxSteps/model override was not applied')
        require(sidecar['model'] == 'local-override' and all(row['role'] == 'assistant' for row in sidecar['steps'])
                and sum(row.get('label_source') == 'llm' for row in sidecar['steps']) == 2,
                'Native assistant-only labeling sidecar or step cap is incorrect')
        require('Errors:' not in label_text, 'Successful labeling reported an error')
        require(all(call['body'].get('max_tokens') == 321 and call['body'].get('temperature') == 0.17
                    for call in calls), 'LABEL_MAX_TOKENS/LABEL_TEMPERATURE settings were not honored')

        taxonomy = work / 'custom taxonomy.md'
        taxonomy.write_text('# Synthetic taxonomy (v9)\n### inspect\n- `inspect_code`: Inspect code.\n', encoding='utf-8')
        custom_path = work / 'custom-labels.json'
        _, custom = turn(f'label {selector} --max-steps 1 --taxonomy "{taxonomy}" '
                         '--max-content-chars 120 --delay 0 --user-phase inquiry --user-action question '
                         f'--max-tokens 222 --temperature 0.37 -o "{custom_path}"',
                         [(200, '{"phase":"inspect","action":"inspect_code"}')], r'label: 1 call')
        custom_labels = json.loads(custom_path.read_text())
        request = custom[0]['body']
        require(len(custom) == 1 and request.get('max_tokens') == 222 and request.get('temperature') == 0.37,
                'Explicit labeling model options did not override environment settings')
        require('Synthetic taxonomy (v9)' in str(request) and len(request['messages'][-1]['content']) <= 120,
                'Custom taxonomy or per-step content bound was not sent to the provider')
        require(custom_labels['taxonomy_version'] == 'v9'
                and sum(row.get('action') == 'inspect_code' for row in custom_labels['steps']) == 1
                and all(row['phase'] == 'inquiry' and row['action'] == 'question'
                        for row in custom_labels['steps'] if row['role'] == 'user'),
                'Custom taxonomy validation or deterministic user tags were not preserved')
        original_taxonomy = taxonomy.read_bytes()
        turn(f'label {selector} --max-steps 1 --taxonomy "{taxonomy}" -o "{taxonomy}"', [],
             r'overwrite.*(?:input|taxonomy)|(?:input|taxonomy).*overwrite')
        require(taxonomy.read_bytes() == original_taxonomy, 'Label output overwrote the taxonomy input')

        retry_path = work / 'retry-labels.json'
        _, retried = turn(f'label {selector} --max-steps 2 --assistant-only -o "{retry_path}"',
                            [(500, PRIVATE_BODY), (500, PRIVATE_BODY), (200, LABEL)], r'prompt 0: HTTPError')
        retry_labels = json.loads(retry_path.read_text())
        require(len(retried) == 3 and retried[0]['body'] == retried[1]['body']
                and retried[1]['body'] != retried[2]['body'], 'Label retry consumed or skipped the next capped step')
        require(sum(row.get('label_source') == 'llm' for row in retry_labels['steps']) == 1,
                'Failed label step was not unknown while the next capped step succeeded')
        require(PRIVATE_BODY not in json.dumps(retry_labels), 'Provider error body leaked into label sidecar')

        unauthorized, failed = turn(f'analyze {selector} Test unauthorized response', [(401, PRIVATE_BODY)], r'HTTP 401')
        require(len(failed) == 1 and PRIVATE_BODY not in unauthorized, 'Unauthorized request leaked its body or retried')
        malformed_path = work / 'malformed-labels.json'
        _, malformed = turn(f'label {selector} --max-steps 1 --assistant-only -o "{malformed_path}"',
                               [(200, {'unexpected': PRIVATE_BODY}), (200, {'unexpected': PRIVATE_BODY})],
                               r'prompt 0: (?:KeyError|ValueError)')
        malformed_labels = json.loads(malformed_path.read_text())
        require(len(malformed) == 2 and not any(row.get('label_source') == 'llm' for row in malformed_labels['steps'])
                and PRIVATE_BODY not in json.dumps(malformed_labels), 'Malformed response retry was not safe and bounded')

        expected_path = '/v1/messages' if provider_name == 'anthropic' else '/v1/chat/completions'
        for request in provider.calls:
            headers = {name.lower(): value for name, value in request['headers'].items()}
            labeling = request['body']['model'] in ('local-label', 'local-override')
            key = f'synthetic-{"label" if labeling else "analyze"}-key'
            require(request['path'] == expected_path, 'Provider routing selected the wrong API protocol')
            require(headers.get('x-api-key') == key if provider_name == 'anthropic' else
                    headers.get('authorization') == f'Bearer {key}', 'Provider authentication header is incorrect')
            require(SECRET not in json.dumps(request['body']), 'A later request restored an unredacted credential')
            if provider_name == 'anthropic':
                require(headers.get('anthropic-version') == '2023-06-01' and request['body'].get('system'),
                        'Anthropic protocol omitted its version or system prompt')
        ledger.update(status='passed', http_requests=len(provider.calls), checks=[
            'native env routing', 'two-question history', 'question redaction', 'fix judgment',
            'label model override', 'assistant-only labels', 'headless step cap', 'bounded per-step retries',
            'environment and explicit sampling settings', 'custom taxonomy and user tags', 'bounded label content',
            'taxonomy overwrite refusal',
            'safe HTTP 401', 'safe malformed response', 'protocol authentication', 'zero external model cost'])
    except BaseException as exc:
        ledger.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        if session is not None:
            session.close()
        provider.close()
        (work / 'provider-requests.json').write_text(json.dumps(provider.calls, indent=2) + '\n')
        (work / 'ledger.json').write_text(json.dumps(ledger, indent=2) + '\n')
    return ledger


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='An empty directory outside the repository')
    parser.add_argument('--cache', type=Path, required=True, help='An existing plugin runtime cache root')
    args = parser.parse_args()
    output, cache = args.output.resolve(), args.cache.resolve()
    if output.is_relative_to(ROOT):
        parser.error('--output must be outside the repository')
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        parser.error('--output must be an empty directory outside the repository')
    ledger: dict = {'status': 'running', 'providers': [], 'claude_version': subprocess.check_output(
        ['claude', '--version'], text=True).strip()}
    try:
        for provider_name in ('openai', 'anthropic'):
            result = exercise(provider_name, output, cache)
            ledger['providers'].append(result)
            print(f'Passed {provider_name}: {len(result["commands"])} native commands, '
                  f'{result["http_requests"]} localhost provider requests, $0 external model cost', flush=True)
        ledger['status'] = 'passed'
    except BaseException as exc:
        ledger.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        (output / 'ledger.json').write_text(json.dumps(ledger, indent=2) + '\n')


if __name__ == '__main__':
    main()
