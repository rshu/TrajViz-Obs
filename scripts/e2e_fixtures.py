"""Deterministic native sessions and comparison manifests shared by both end-to-end harnesses."""

from __future__ import annotations

import json
from pathlib import Path


def native_run(path: Path, session_id: str, *, regression: bool = False, shell_read: bool = False,
               agent: str | None = None) -> Path:
    """Small native transcripts with independently known failed calls, edits and normal completion."""
    records: list[dict] = []

    def event(kind: str, message: dict) -> None:
        serial = len(records)
        records.append({'type': kind, 'sessionId': session_id, 'uuid': f'{agent or "main"}-{serial}',
                        'parentUuid': records[-1]['uuid'] if records else None, 'cwd': '/p',
                        'timestamp': f'2026-10-22T10:00:{serial:02d}.000Z', 'isSidechain': bool(agent),
                        **({'agentId': agent} if agent else {}), 'message': message})

    def assistant(content: list[dict], stop: str) -> None:
        event('assistant', {'role': 'assistant', 'id': f'msg-{len(records)}', 'model': 'claude-test',
                            'content': content, 'stop_reason': stop,
                            'usage': {'input_tokens': 5, 'output_tokens': 10}})

    def tool(name: str, inputs: dict, *, failed: bool = False) -> None:
        tid = f'tool-{len(records)}'
        assistant([{'type': 'tool_use', 'id': tid, 'name': name, 'input': inputs}], 'tool_use')
        event('user', {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': tid,
                                                 'is_error': failed, 'content': '1 failed' if failed else 'ok'}]})

    event('user', {'role': 'user', 'content': 'Fix calculator addition and verify the test.'})
    if regression:
        tool('Bash', {'command': 'pytest -q tests/test_calculator.py'}, failed=True)
    tool('Bash' if shell_read else 'Read', {'command': 'cat /p/src/calculator.py | cat /p/src/calculator.py'} if shell_read
         else {'file_path': '/p/src/calculator.py'})
    tool('Edit', {'file_path': '/p/src/calculator.py', 'old_string': 'return a - b', 'new_string': 'return a + b'})
    tool('Bash', {'command': 'pytest -q tests/test_calculator.py'})
    assistant([{'type': 'text', 'text': 'Addition is fixed and the test passes.'}], 'end_turn')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(record) + '\n' for record in records), encoding='utf-8')
    return path


def prepare_comparison_inputs(output: Path) -> dict[str, Path]:
    """Reusable native fixtures and relative-path manifests for bridge and Claude Code command tests.

    Before has one failed validation then recovery (6 steps, 4 tools, 75% tool success). After uses a
    composite shell read instead of Read (5 steps, 3 tools, 100% success); fuzzy alignment must recognize it.
    The reference uses Read and otherwise matches After. No model, collector or external checkout is needed.
    """
    output.mkdir(parents=True, exist_ok=True)
    before = native_run(output / 'before.jsonl', 'aaa11111-0000-4000-8000-000000000001', regression=True)
    after = native_run(output / 'after.jsonl', 'bbb22222-0000-4000-8000-000000000002', shell_read=True)
    reference = native_run(output / 'reference.jsonl', 'ccc33333-0000-4000-8000-000000000003')
    anchor = output / 'addition.diff'
    anchor.write_text('--- a/src/calculator.py\n+++ b/src/calculator.py\n@@ -1 +1 @@\n-return a - b\n+return a + b\n')
    manifests = {}
    for name, compared in (('before', before), ('after', after)):
        manifest = output / f'{name}-manifest.json'
        manifest.write_text(json.dumps([{'task_id': tid, 'reference': reference.name, 'compared': compared.name,
                                         'anchor': anchor.name} for tid in ('addition', 'addition-repeat')]))
        manifests[f'{name}_manifest'] = manifest
    return {'before': before, 'after': after, 'reference': reference, 'anchor': anchor, **manifests}
