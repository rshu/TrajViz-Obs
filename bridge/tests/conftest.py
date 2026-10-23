"""Shared fixtures for the bridge tests."""

from __future__ import annotations

import contextlib
import io
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

FIXTURES = Path(__file__).parent / "fixtures"

#: Settings TrajViz's ``config_from_env`` reads (``ANALYZE_*``, with ``LABEL_*`` filling gaps).
LLM_ENV = tuple(f"{prefix}_{key}" for prefix in ("ANALYZE", "LABEL") for key in ("BASE_URL", "API_KEY", "MODEL"))


@pytest.fixture(autouse=True)
def _restore_environ():
    """Restore os.environ after each test.

    TrajViz's ``load_dotenv`` writes ``os.environ`` directly, and monkeypatch cannot undo a write to a key
    it never recorded (``delenv(..., raising=False)`` of an absent key records nothing).
    """
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
def no_llm_env(monkeypatch) -> None:
    """Unset every LLM setting for one test (restored afterwards)."""
    for name in LLM_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def fixture_session() -> Path:
    """The vendored native Claude Code transcript (see fixtures/native_claude_code/README.md)."""
    return FIXTURES / "native_claude_code" / "django__django-11477.jsonl"


@pytest.fixture
def tmp_artifacts(tmp_path: Path) -> Path:
    """An empty artifact folder for one test."""
    path = tmp_path / "artifacts"
    path.mkdir()
    return path


@pytest.fixture(autouse=True)
def _restore_subcommands():
    """Keep handlers a test registers from leaking into later tests."""
    from trajviz_obs import cli

    cli.load_modules()
    saved = dict(cli.SUBCOMMANDS)
    yield
    cli.SUBCOMMANDS.clear()
    cli.SUBCOMMANDS.update(saved)


@pytest.fixture
def run_cli() -> Callable[[list[str]], tuple[int, dict]]:
    """Call ``cli.main(argv)`` in-process; return its exit code and the parsed stdout JSON."""
    from trajviz_obs import cli

    def run(argv: list[str]) -> tuple[int, dict]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(argv)
        return code, json.loads(out.getvalue())

    return run


#: Session id of the synthetic session :func:`tmp_session` writes.
TMP_SESSION_ID = "5e551011-0000-4000-8000-000000000001"


@dataclass(frozen=True)
class SessionTree:
    """A synthetic session on disk: ``<dir>/<id>.jsonl`` plus ``<dir>/<id>/subagents/...``."""

    id: str
    transcript: str
    dir: Path


def _event(kind: str, n: int, session_id: str, **fields) -> dict:
    return {"type": kind, "sessionId": session_id, "uuid": f"event-{n}",
            "parentUuid": f"event-{n - 1}" if n else None, "cwd": "/p", "isSidechain": False,
            "timestamp": f"2026-10-22T10:00:{n:02d}.000Z", **fields}


@pytest.fixture
def tmp_session(tmp_path: Path) -> SessionTree:
    """A small native Claude Code session in Claude Code's current layout.

    The transcript holds two ``ai-title`` records (the second wins), one real prompt ("fix the tests"),
    an ``isMeta`` caveat row, a ``<command-name>`` slash-command row, a tool call with its result and a final
    reply; ``<id>/subagents/workflows/wf_1/agent-abc.meta.json`` names sub-agent ``abc``.
    """
    sid = TMP_SESSION_ID
    usage = {"input_tokens": 1200, "output_tokens": 40, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 0}

    def assistant(n: int, content: list) -> dict:
        return _event("assistant", n, sid, message={"role": "assistant", "id": f"msg-{n}", "model": "claude-test",
                                                    "content": content, "usage": usage, "stop_reason": "end_turn"})

    def user(n: int, content: str | list, **fields) -> dict:
        return _event("user", n, sid, message={"role": "user", "content": content}, **fields)

    records = [
        {"type": "ai-title", "aiTitle": "Draft title", "sessionId": sid},
        user(1, "<local-command-caveat>Caveat: the command below was run directly.</local-command-caveat>",
             isMeta=True),
        user(2, "<command-name>/obs</command-name>\n<command-message>obs</command-message>"),
        user(3, [{"type": "text", "text": "fix the tests"}]),
        assistant(4, [{"type": "tool_use", "id": "toolu_1", "name": "Bash",
                       "input": {"command": "pytest -q", "description": "Run the tests"}}]),
        user(5, [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "1 failed"}]),
        assistant(6, [{"type": "text", "text": "Fixed the failing test."}]),
        {"type": "ai-title", "aiTitle": "Build the obs plugin", "sessionId": sid},
    ]
    transcript = tmp_path / f"{sid}.jsonl"
    transcript.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    workflow = tmp_path / sid / "subagents" / "workflows" / "wf_1"
    workflow.mkdir(parents=True)
    (workflow / "agent-abc.meta.json").write_text(json.dumps(
        {"agentType": "general-purpose", "description": "Builder check", "spawnDepth": 1}), encoding="utf-8")
    return SessionTree(id=sid, transcript=str(transcript), dir=tmp_path)
