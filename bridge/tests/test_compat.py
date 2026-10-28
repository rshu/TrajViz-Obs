"""The compat adapter: the real context window on load, the empty-session signal, labeler chat injection."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest
from agent_trajviz import LoadError

from trajviz_obs import compat
from trajviz_obs.compat import EmptySession, labeler_chat, load_run, window_info
from trajviz_obs.errors import BridgeError


@pytest.fixture
def native_ui_session(tmp_path):
    """Real native shapes: local commands, model context, tool results, and a child log."""
    sid = "native-ui-session"

    def event(kind, index, content, *, prefix="parent", **fields):
        row = {"type": kind, "uuid": f"{prefix}-{index}", "sessionId": sid,
               "parentUuid": f"{prefix}-{index - 1}" if index else None,
               "timestamp": f"2026-10-28T01:00:{index:02d}Z", "cwd": str(tmp_path), **fields}
        if kind in ("user", "assistant"):
            row["message"] = {"role": kind, "content": content}
            if kind == "assistant":
                row["message"].update(id=f"message-{prefix}-{index}", model="claude-test",
                                      usage={"input_tokens": 11, "output_tokens": 3})
        else:
            row["content"] = content
        return row

    def records(prefix):
        return [
            event("user", 0, "<request>Keep literal XML input</request>", prefix=prefix),
            event("assistant", 1, [{"type": "tool_use", "id": f"call-{prefix}", "name": "Bash",
                                    "input": {"command": "pytest -q"}}], prefix=prefix),
            event("user", 2, [{"type": "text", "text": "The whole TrajViz view health is in /tmp/health.json"},
                               {"type": "tool_result", "tool_use_id": f"call-{prefix}", "content": "1 failed",
                                "is_error": True}], prefix=prefix, isMeta=True),
            event("user", 3, "<local-command-caveat>Display only</local-command-caveat>", prefix=prefix, isMeta=True),
            event("user", 4, "<command-name>/obs</command-name><command-message>obs</command-message>"
                             "<command-args>health</command-args>", prefix=prefix),
            event("system", 5, "<local-command-stdout>health</local-command-stdout>", prefix=prefix,
                  subtype="local_command", commandRun={"command": "obs", "plugin": "trajviz-obs"}),
            event("user", 6, "TrajViz artifacts for this session go under /tmp/session", prefix=prefix, isMeta=True),
            event("user", 7, "<system-reminder>Useful model context</system-reminder>", prefix=prefix, isMeta=True),
            event("user", 8, "<task-notification>Child completed</task-notification>", prefix=prefix),
            event("user", 9, "<command-name>literal XML only</command-name>", prefix=prefix),
            event("user", 10, "The whole TrajViz view is a phrase I want explained", prefix=prefix),
            event("system", 11, "Checkpoint", prefix=prefix, subtype="compact_boundary",
                  compactMetadata={"trigger": "manual"}),
            event("assistant", 12, [{"type": "text", "text": "Checked the failure."}], prefix=prefix),
        ]

    parent, child = records("parent"), records("child")
    path = tmp_path / f"{sid}.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in parent))
    folder = path.with_suffix("") / "subagents"
    folder.mkdir(parents=True)
    child_path = folder / "agent-child.jsonl"
    child_path.write_text("".join(json.dumps(row) + "\n" for row in child))
    return path, parent, child, child_path


@pytest.mark.parametrize("selection", ["file", "directory", "zip"])
def test_native_local_command_filter_preserves_source_identity_and_tools(native_ui_session, tmp_path, selection):
    import agent_trajviz as tv
    from agent_trajviz.insight.formats.claude_code import convert_claude_code_events
    from agent_trajviz.insight.parser import parse_steps

    path, parent, child, child_path = native_ui_session
    hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (path, child_path)}
    original = parse_steps(convert_claude_code_events(parent, source_path=str(path), child_event_lists={
        "child": {"events": child, "source_path": str(child_path)}}))
    source = path
    if selection == "directory":
        source = path.with_suffix("")
    elif selection == "zip":
        source = tmp_path / "session.zip"
        with zipfile.ZipFile(source, "w") as archive:
            for item in (path, child_path):
                archive.write(item, item.relative_to(path.parent))
    run = load_run(str(source))
    assert run.raw["_trajviz_obs_ignored_ui_records"] == 8
    assert not any("Display only" in s["text_preview"] or "<command-message>" in s["text_preview"]
                   or "TrajViz artifacts" in s["text_preview"] for s in run.steps)
    for text in ("<request>", "<system-reminder>", "<task-notification>",
                 "<command-name>literal XML", "The whole TrajViz view is a phrase"):
        assert sum(s["text_preview"].startswith(text) for s in run.steps) == 2
    stable = lambda steps: [{k: v for k, v in s.items() if k not in ("index", "source_path")}
                            for s in steps if s["role"] == "assistant"]
    assert stable(run.steps) == stable(original)
    assert sum(s["role"] == "compaction" for s in run.steps) == 2
    assert {s["agent"] for s in run.steps} == {"", "child"}
    assert all(s["raw_index"] in (0, 1, 7, 8, 9, 10, 11, 12) for s in run.steps)
    calls = [call for step in run.steps for call in step["tool_calls"]]
    assert len(calls) == 2 and all(call["status"] == "error" and call["output"] == "1 failed" for call in calls)
    assert hashes == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in hashes}
    exported = tmp_path / f"{selection}-trajectory.json"
    exported.write_text(json.dumps(run.to_dict(include_raw=True)))
    assert load_run(str(exported)).to_dict(include_raw=True) == run.to_dict(include_raw=True)
    # Native dashboard and Converge loads use the same process-wide converter.
    assert tv.load(str(source)).steps == run.steps


def test_native_filter_handles_inline_progress_without_erasing_other_meta():
    from trajviz_obs.native_records import normalize_native_records

    events = [{"type": "progress", "isMeta": True, "data": {"agentId": "child", "message": {
        "role": "user", "content": "TrajViz artifacts for this session go under /tmp/session"}}},
        {"type": "user", "isMeta": True, "message": {"role": "user", "content": "Keep model context"}},
        {"type": "user", "message": {"role": "user", "content": "<request>Keep XML</request>"}}]
    before = json.dumps(events)
    filtered, ignored = normalize_native_records(events)
    assert ignored == 1 and filtered[0]["data"]["message"]["content"] == []
    assert filtered[1:] == events[1:] and json.dumps(events) == before


def test_native_filter_preserves_malformed_uuid_and_nested_progress_metadata():
    from trajviz_obs.native_records import normalize_native_records

    note = "TrajViz artifacts for this session go under /tmp/session"
    command = "<command-name>/obs</command-name><command-message>obs</command-message>"
    events = [{"type": "user", "uuid": value, "message": {"role": "user", "content": command}}
              for value in ([], {})]
    events += [{"type": "progress", "isMeta": True, "data": {"agentId": "child", "message": {
        "type": "user", "message": {"role": "user", "content": note}}}},
        {"type": "progress", "isMeta": True, "data": {"agentId": "child", "message": {
            "type": "user", "isMeta": False, "message": {"role": "user", "content": note}}}}]
    before = json.dumps(events)
    filtered, ignored = normalize_native_records(events)
    assert ignored == 1 and filtered[:2] == events[:2] and filtered[3] == events[3]
    assert filtered[2]["data"]["message"]["message"]["content"] == []
    assert "isMeta" not in filtered[2]["data"]["message"]
    assert json.dumps(events) == before


def test_native_filter_reports_a_changed_upstream_converter(monkeypatch):
    from agent_trajviz.insight import loaders

    monkeypatch.delattr(loaders, "convert_claude_code_events")
    with pytest.raises(BridgeError, match="native Claude Code converter changed"):
        compat.load_plugins()


def test_native_filter_handles_current_command_before_its_stdout_is_written():
    from trajviz_obs.native_records import normalize_native_records

    command = "<command-name>/obs</command-name><command-message>obs</command-message>"
    events = [{"type": "user", "isMeta": True, "uuid": "caveat", "message": {
        "role": "user", "content": "<local-command-caveat>Local command</local-command-caveat>"}},
        {"type": "user", "uuid": "current-command", "parentUuid": "caveat", "message": {
            "role": "user", "content": command}},
        {"type": "user", "uuid": "literal-input", "message": {"role": "user", "content": command}}]
    filtered, ignored = normalize_native_records(events)
    assert ignored == 2 and len(filtered) == len(events)
    assert filtered[0]["message"]["content"] == filtered[1]["message"]["content"] == []
    assert filtered[2] == events[2] and events[1]["message"]["content"] == command


def test_native_filter_and_labeler_share_the_same_normalized_indices(native_ui_session):
    from agent_trajviz.insight import loaders
    from agent_trajviz.labeling import labeler

    path, _, _, _ = native_ui_session
    run = load_run(str(path))
    converter = loaders.convert_claude_code_events
    compat.load_plugins()
    assert loaders.convert_claude_code_events is converter
    with compat.labeler_run(run):
        assert labeler.load_all_steps(str(path)) == run.steps


def test_window_override_reaches_pressure_stats(fixture_session):
    run = load_run(str(fixture_session), window_limit=1_000_000)
    stats = run.view("context_pressure").data["stats"]
    assert stats["window_limit"] == 1_000_000 and window_info(run)["window_source"] == "override"


def test_window_override_reaches_brief(fixture_session):
    # TrajViz 0.2.0's brief prints no window for this fixture (it names one only in a premature-compaction
    # issue), so assert on what the brief's analysis reads: the run-level pressure with no override argument.
    run = load_run(str(fixture_session), window_limit=1_000_000)
    assert run.context_pressure()["stats"]["window_limit"] == 1_000_000


def test_window_info_without_override_reports_trajviz_s(fixture_session):
    info = window_info(load_run(str(fixture_session)))
    assert info == {"window": 200_000, "window_source": "model", "window_note": ""}


def test_window_override_must_be_positive(fixture_session):
    with pytest.raises(ValueError):
        load_run(str(fixture_session), window_limit=0)


def test_empty_session_is_signalled(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text('{"type":"ai-title","title":"x"}\n')
    with pytest.raises(EmptySession):
        load_run(str(p), current=True)
    with pytest.raises(LoadError):
        load_run(str(p))


@pytest.mark.parametrize("version", ["0.1.9", "0.3.0", "1.0.0", "unknown"])
def test_unsupported_trajviz_is_refused(monkeypatch, version):
    monkeypatch.setattr(compat, "TRAJVIZ_VERSION", version)
    with pytest.raises(BridgeError) as caught:
        compat.check_supported()
    assert caught.value.code == "unsupported_trajviz"


def test_labeler_chat_injection(fixture_session, tmp_path):
    import time

    from agent_trajviz.labeling import labeler, llm
    from agent_trajviz.labeling.labeler import label_trajectory

    original_call, original_sleep = llm.call_llm, time.sleep
    seen = []
    with labeler_chat(lambda system, user: seen.append(user) or '{"phase":"understand","action":"code_reading"}'):
        out = label_trajectory(str(fixture_session), str(tmp_path / "l.json"), base_url="x", api_key="x", model="m")
        labeler.time.sleep(60)  # the labeler's retry sleep is a no-op...
        assert time.sleep is original_sleep  # ...without touching the process-wide time.sleep
    assert seen and out["counts"]["assistant"] == len(seen)
    assert llm.call_llm is original_call and labeler.time.sleep is original_sleep


def test_labeler_chat_redacts_like_call_llm():
    from agent_trajviz.labeling import llm

    seen = []
    with labeler_chat(lambda system, user: seen.append((system, user)) or "reply"):
        reply = llm.call_llm(base_url="x", api_key="x", model="m", system_prompt="sys",
                             user_message="key sk-ant-abcdefghijklmnopqrstuvwxyz0123")
    assert reply == "reply" and seen == [("sys", "key [REDACTED]")]


# TrajViz plugins: loaded once per process before any load or view lookup.


def _obs_fake_plugin() -> None:
    """A plugin hook registering a format (``{"agent": "obsfake", "turns": [...]}``) and a view."""
    import agent_trajviz as tv

    tv.register_format(tv.Format(key="obsfake", label="Obs fake",
                                 detect=lambda doc: isinstance(doc, dict) and doc.get("agent") == "obsfake",
                                 parse=lambda doc: [{"role": t["role"], "text_preview": t["text"]} for t in doc["turns"]]))
    tv.register_view("obs_fake_view", lambda run: {"answer": 42}, title="Obs fake view")


def _obsfake_run(folder: Path, name: str = "run.json") -> str:
    path = folder / name
    path.write_text(json.dumps({"agent": "obsfake", "turns": [{"role": "user", "text": "hi"},
                                                            {"role": "assistant", "text": "hello"}]}))
    return str(path)


def test_plugins_load_once_per_process(fixture_session, fake_plugins, monkeypatch):
    import agent_trajviz

    calls, real = [], agent_trajviz.load_plugins
    monkeypatch.setattr(agent_trajviz, "load_plugins", lambda: calls.append(1) or real())
    fake_plugins("obs-fake", lambda: None)
    load_run(str(fixture_session))
    load_run(str(fixture_session))
    assert compat.load_plugins() == ["obs-fake"] and calls == [1]


def test_failing_plugin_is_plugin_error(fixture_session, fake_plugins, run_cli):
    runs = []

    def broken() -> None:
        runs.append(1)
        raise RuntimeError("boom")

    fake_plugins("broken", broken)
    for _ in range(2):  # the outcome is kept: the hook is not run again
        with pytest.raises(BridgeError) as e:
            load_run(str(fixture_session))
        assert e.value.code == "plugin_error" and "'broken'" in e.value.message and "boom" in e.value.message
    code, out = run_cli(["snapshot", "--transcript", str(fixture_session)])
    assert code == 1 and out["error"]["code"] == "plugin_error" and runs == [1]


def test_plugin_format_loads(fake_plugins, tmp_path):
    fake_plugins("obs-fake", _obs_fake_plugin)
    assert load_run(_obsfake_run(tmp_path)).format == "obsfake"


@pytest.mark.parametrize("command", ["catalog", "view", "schemas", "scorecard"])
def test_plugins_load_before_any_view_lookup_or_load(command, fake_plugins, run_cli, fixture_session, tmp_path):
    """Each command, run first in the process, sees the plugin's view (or reads the plugin's format)."""
    fake_plugins("obs-fake", _obs_fake_plugin)
    custom, artifacts = _obsfake_run(tmp_path), str(tmp_path / "artifacts")
    if command == "catalog":
        code, out = run_cli(["view", "--transcript", str(fixture_session)])
        assert code == 0 and "obs_fake_view" in [view["id"] for view in out["views"]]
    elif command == "view":
        code, out = run_cli(["view", "--transcript", custom, "--artifacts", artifacts, "obs_fake_view"])
        assert code == 0 and "| answer | 42 |" in out["inline"]["table"]
    elif command == "schemas":
        code, out = run_cli(["schemas", "--out", str(tmp_path / "schemas")])
        assert code == 0 and "obs_fake_view.schema.json" in out["files"]
    else:
        other = _obsfake_run(tmp_path, "other.json")
        code, out = run_cli(["scorecard", "--no-self", "--transcript", custom, "--artifacts", artifacts,
                             "--others", custom, other])
        assert code == 0 and [row.get("error") for row in out["rows"]] == [None, None]
