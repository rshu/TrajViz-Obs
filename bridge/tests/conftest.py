"""Shared fixtures for the bridge tests."""

from __future__ import annotations

import contextlib
import io
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

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
def fixture_snapshot(fixture_session: Path) -> dict:
    """The snapshot document of :func:`fixture_session` (TrajViz's own window estimate, no artifact folder)."""
    from trajviz_obs.compat import load_run
    from trajviz_obs.sidecars import agent_meta, scan_transcript
    from trajviz_obs.snapshot import build_snapshot

    path = str(fixture_session)
    return build_snapshot(load_run(path), transcript_path=path, facts=scan_transcript(path),
                          agents_meta=agent_meta(path), artifacts=None)


@pytest.fixture
def fixture_run(fixture_session: Path):
    """:func:`fixture_session` loaded through ``compat.load_run`` (TrajViz's own window estimate)."""
    from trajviz_obs.compat import load_run

    return load_run(str(fixture_session))


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
    reply; ``<id>/subagents/workflows/wf_1/`` holds sub-agent ``abc``'s log (``agent-abc.jsonl``, one reply,
    no spawning call, so TrajViz labels it by its id) and ``agent-abc.meta.json`` naming it.
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
    child = [{**user(7, "check the build"), "agentId": "abc", "isSidechain": True},
             {**assistant(8, [{"type": "text", "text": "The build is fine."}]), "agentId": "abc", "isSidechain": True}]
    (workflow / "agent-abc.jsonl").write_text("".join(json.dumps(r) + "\n" for r in child), encoding="utf-8")
    (workflow / "agent-abc.meta.json").write_text(json.dumps(
        {"agentType": "general-purpose", "description": "Builder check", "spawnDepth": 1}), encoding="utf-8")
    return SessionTree(id=sid, transcript=str(transcript), dir=tmp_path)


@pytest.fixture
def tmp_session_with_child(tmp_session: SessionTree) -> SessionTree:
    """:func:`tmp_session`, named for tests that rely on its sub-agent log under ``<id>/subagents/``."""
    return tmp_session


@pytest.fixture
def tmp_session_with_bad_child(tmp_session: SessionTree) -> SessionTree:
    """:func:`tmp_session` plus two sub-agent logs TrajViz cannot read (``agent-bad1``, ``agent-bad2``)."""
    sub = tmp_session.dir / tmp_session.id / "subagents"
    (sub / "agent-bad1.jsonl").write_text('{"type":broken}\n', encoding="utf-8")
    (sub / "workflows" / "agent-bad2.jsonl").write_text("not json at all\n", encoding="utf-8")
    return tmp_session


_CLOCK = datetime(2026, 10, 22, 10, 0, tzinfo=UTC)


def make_session(tmp_path: Path, *, steps: int = 40, agents: int = 0,
                 session_id: str = "5e551011-0000-4000-8000-000000000002") -> str:
    """Write a native Claude Code session of *steps* main-agent turns and *agents* sub-agents; return its path.

    Each turn is one assistant record (usage, a text block and one tool call: ``Bash`` with a ``pytest``
    command every third turn, else ``Read`` of a file) and its tool result; every tenth call fails. Each
    sub-agent is spawned by an ``Agent`` call in place of a main turn and logs three turns of its own in
    ``<id>/subagents/agent-<agent id>.jsonl``. Records are 1.5 s apart.
    """
    sid, clock, serial = session_id, 0.0, 0

    def event(kind: str, message: dict, agent: str = "", **fields) -> dict:
        nonlocal clock, serial
        clock, serial = clock + 1.5, serial + 1
        stamp = (_CLOCK + timedelta(seconds=clock)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        record = {"type": kind, "sessionId": sid, "uuid": f"{agent or 'main'}-{serial}", "parentUuid": None,
                  "cwd": "/p", "isSidechain": bool(agent), "timestamp": stamp, "message": message, **fields}
        return {**record, "agentId": agent} if agent else record

    def assistant(i: int, content: list, agent: str = "") -> dict:
        usage = {"input_tokens": 3, "output_tokens": 50 + i % 7, "cache_read_input_tokens": 1000 + 20 * i,
                 "cache_creation_input_tokens": 200}
        return event("assistant", {"role": "assistant", "id": f"msg-{agent}-{i}", "model": "claude-test",
                                   "content": content, "usage": usage, "stop_reason": "tool_use"}, agent)

    def user(content: str | list, agent: str = "", **fields) -> dict:
        return event("user", {"role": "user", "content": content}, agent, **fields)

    def turn(i: int, agent: str = "") -> list[dict]:
        tid = f"toolu_{agent}{i}"
        call = ({"command": f"pytest -q tests/test_{i % 11}.py"} if i % 3 == 0 else {"file_path": f"/p/src/m{i % 13}.py"})
        result = {"type": "tool_result", "tool_use_id": tid, "content": "ok"}
        if i % 10 == 0:
            result.update(content="boom: exit 1", is_error=True)
        return [assistant(i, [{"type": "text", "text": f"step {i}"},
                              {"type": "tool_use", "id": tid, "name": "Bash" if i % 3 == 0 else "Read", "input": call}],
                          agent),
                user([result], agent)]

    records = [{"type": "ai-title", "aiTitle": "Synthetic session", "sessionId": sid}, user("build it")]
    children: dict[str, list[dict]] = {}
    spawn_every = steps // (agents + 1) if agents else 0
    for i in range(steps):
        if len(children) < agents and i and i % spawn_every == 0:
            aid, tid = f"a{len(children):016x}", f"toolu_spawn{len(children)}"
            description = f"Worker {len(children)}"
            records.append(assistant(i, [{"type": "tool_use", "id": tid, "name": "Agent", "input": {
                "description": description, "prompt": "go", "subagent_type": "Explore"}}]))
            children[aid] = [user("go", aid), *(r for j in range(3) for r in turn(j, aid))]
            records.append(user([{"type": "tool_result", "tool_use_id": tid, "content": "done"}],
                                toolUseResult={"agentId": aid, "description": description}))
        else:
            records += turn(i)
    transcript = tmp_path / f"{sid}.jsonl"
    transcript.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    for aid, child in children.items():
        log = tmp_path / sid / "subagents" / f"agent-{aid}.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("".join(json.dumps(r) + "\n" for r in child), encoding="utf-8")
    return str(transcript)


@pytest.fixture
def big_session(tmp_path: Path) -> str:
    """A 5,000-turn synthetic session with three sub-agents (see :func:`make_session`)."""
    return make_session(tmp_path, steps=5000, agents=3)


#: Session ids :func:`fake_projects` writes.
AAA1 = "aaa11111-0000-4000-8000-000000000001"
AAA2 = "aaa22222-0000-4000-8000-000000000002"
BBB3 = "bbb33333-0000-4000-8000-000000000003"


def _write_session(folder: Path, session_id: str, started: str, mtime: int, extra_lines: int = 0) -> Path:
    """A stub transcript ``<folder>/<session_id>.jsonl`` (a start time, *extra_lines* more records) with *mtime*."""
    path = folder / f"{session_id}.jsonl"
    lines = [{"type": "last-prompt", "sessionId": session_id}]
    lines += [{"type": "user", "timestamp": started, "sessionId": session_id}]
    lines += [{"type": "assistant", "timestamp": started, "n": i} for i in range(extra_lines)]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def fake_projects(tmp_path: Path) -> SimpleNamespace:
    """``<config>/projects/<slug>/`` holding sessions aaa111…, aaa222…, bbb333… (bbb newest)."""
    from trajviz_obs.paths import project_slug

    cwd = str(tmp_path / "work" / "My Proj.x_y")
    config = tmp_path / "config"
    project = config / "projects" / project_slug(cwd)
    project.mkdir(parents=True)
    _write_session(project, AAA1, "2026-10-01T10:00:00.000Z", 1_700_000_100)
    _write_session(project, AAA2, "2026-10-02T10:00:00.000Z", 1_700_000_200, extra_lines=2)
    _write_session(project, BBB3, "2026-10-03T10:00:00.000Z", 1_700_000_300, extra_lines=4)
    (project / BBB3 / "subagents").mkdir(parents=True)
    return SimpleNamespace(cwd=cwd, config=config, project=project)


@pytest.fixture
def synthetic_session(tmp_path) -> str:
    """A 30-turn native session (no sub-agents) to compare the fixture against."""
    (tmp_path / "other").mkdir()
    return make_session(tmp_path / "other", steps=30)
