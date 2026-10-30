"""The compat adapter: the real context window on load, the empty-session signal, labeler chat injection."""

from __future__ import annotations

import hashlib
import json
import types
import zipfile
from pathlib import Path

import pytest
from agent_trajviz import LoadError

from trajviz_obs import compat
from trajviz_obs.compat import (
    EmptySession,
    input_files,
    input_path,
    labeler_chat,
    load_run,
    protect_run_outputs,
    window_info,
)
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
    # TrajViz's brief prints no window for this fixture (it names one only in a premature-compaction
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


@pytest.mark.parametrize("version", ["0.1.9", "0.2.0", "0.3.0", "0.4.0", "1.0.0", "unknown"])
def test_unsupported_trajviz_is_refused(monkeypatch, version):
    monkeypatch.setattr(compat, "TRAJVIZ_VERSION", version)
    with pytest.raises(BridgeError) as caught:
        compat.check_supported()
    assert caught.value.code == "unsupported_trajviz"


@pytest.mark.parametrize("version", ["0.3.1", "0.3.2", "0.3.99"])
def test_supported_published_minor_is_accepted(monkeypatch, version):
    monkeypatch.setattr(compat, "TRAJVIZ_VERSION", version)
    compat.check_supported()


@pytest.fixture
def otlp_capture(tmp_path):
    """Two explicitly identified requests in a local saved-telemetry bundle."""
    folder = tmp_path / "capture"
    folder.mkdir()
    members = []
    for index, sid in enumerate(("session-a", "session-b"), 1):
        attrs = [{"key": "session.id", "value": {"stringValue": sid}},
                 *[{"key": key, "value": {"intValue": str(value)}} for key, value in (
                     ("input_tokens", 6 + index), ("output_tokens", 2), ("reasoning_tokens", 0),
                     ("cache_read_tokens", 0), ("cache_creation_tokens", 0))]]
        request = {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name",
            "value": {"stringValue": "claude-code"}}]}, "scopeSpans": [{"spans": [{
            "traceId": str(index) * 32, "spanId": str(index) * 16, "name": "claude_code.llm_request",
            "startTimeUnixNano": "1790000000000000000", "endTimeUnixNano": "1790000001000000000",
            "attributes": attrs}]}]}]}
        member = folder / "traces" / f"{sid}.jsonl"
        member.parent.mkdir(exist_ok=True)
        member.write_text(json.dumps(request) + "\n")
        members.append(member)
    logs = folder / "events" / "logs.jsonl"
    logs.parent.mkdir()
    logs.write_text(json.dumps({"resourceLogs": []}) + "\n")
    manifest = folder / "manifest.json"
    manifest.write_text(json.dumps({"type": "agent-trajviz-otlp-bundle", "schema_version": 1,
        "profile": "claude_code", "traces": [str(p.relative_to(folder)) for p in members],
        "logs": [str(logs.relative_to(folder))]}))
    # An unrelated root JSONL must not redirect input-path protection away
    # from the explicit OTLP manifest selected by the public loader.
    (folder / "native.jsonl").write_text('{"unrelated":true}\n')
    archive = tmp_path / "capture.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        for member in [manifest, *members, logs]:
            handle.write(member, member.relative_to(folder))
    stream = tmp_path / "capture.jsonl"
    stream.write_text("".join(member.read_text() for member in members))
    return {"directory": folder, "manifest": manifest, "zip": archive, "stream": stream,
            "members": [manifest, *members, logs]}


@pytest.mark.parametrize("layout", ["directory", "manifest", "zip", "stream"])
def test_saved_otlp_session_selection_preserves_capture_and_selected_identity(otlp_capture, layout):
    source = otlp_capture[layout]
    with pytest.raises(LoadError) as caught:
        load_run(str(source))
    assert caught.value.code == "ambiguous_session"
    first = load_run(str(source), session_id="session-a")
    second = load_run(str(source), session_id="session-b")
    assert first.format == second.format == "otlp" and first.sha256 != second.sha256
    assert first.raw["_otel"]["session_id"] == "session-a"
    assert second.raw["_otel"]["session_id"] == "session-b"
    assert {step["session_id"] for step in first.steps} == {"session-a"}
    assert {span["trace_id"] for span in first.raw["_otel"]["spans"]} == {"1" * 32}
    assert input_path(first) == source.absolute()
    with pytest.raises(LoadError) as caught:
        load_run(str(source), session_id="missing-session")
    assert caught.value.code == "session_not_found"
    original = first.view("metrics")
    assert compat.project_view(first, original) is original
    assert compat.project_analysis(first) is first._analysis


def test_saved_session_selector_is_not_silently_ignored_for_native_inputs(fixture_session):
    with pytest.raises(LoadError) as caught:
        load_run(str(fixture_session), session_id="arbitrary-native-id")
    assert caught.value.code == "unsupported_selection"


@pytest.mark.parametrize("layout", ["directory", "manifest"])
@pytest.mark.parametrize("alias", ["direct", "hardlink", "symlink"])
def test_saved_otlp_protects_every_actual_member_including_other_selected_sessions(otlp_capture, tmp_path,
                                                                                layout, alias):
    source = otlp_capture[layout]
    run = load_run(str(source), session_id="session-a")
    assert set(otlp_capture["members"]) <= set(input_files(run))
    assert len(input_files(run)) == len(set(input_files(run)))
    for index, member in enumerate(otlp_capture["members"]):
        destination = member
        if alias != "direct":
            destination = tmp_path / f"{layout}-{alias}-{index}.json"
            if alias == "hardlink":
                destination.hardlink_to(member)
            else:
                destination.symlink_to(member)
        with pytest.raises(BridgeError) as caught:
            protect_run_outputs(run, [destination])
        assert caught.value.code == "bad_output"
    with pytest.raises(BridgeError):
        protect_run_outputs(run, [source])


def test_otlp_zip_protection_uses_the_actual_archive_and_export_uses_its_own_input(otlp_capture, tmp_path):
    source = otlp_capture["zip"]
    run = load_run(str(source), session_id="session-a")
    alias = tmp_path / "archive-alias.zip"
    alias.hardlink_to(source)
    with pytest.raises(BridgeError):
        protect_run_outputs(run, [alias])
    # ZIP provenance labels do not describe local files consumed on reload.
    protect_run_outputs(run, otlp_capture["members"])
    exported = tmp_path / "reloaded.json"
    exported.write_text(json.dumps(run.to_dict(include_raw=True)))
    reloaded = load_run(str(exported), session_id="session-a")
    assert input_path(reloaded) == exported.absolute()
    with pytest.raises(BridgeError):
        protect_run_outputs(reloaded, [exported])
    protect_run_outputs(reloaded, [source, *otlp_capture["members"]])


def test_otlp_directory_fallback_never_follows_forged_external_or_symlinked_provenance(otlp_capture, tmp_path):
    run = load_run(str(otlp_capture["directory"]), session_id="session-a")
    compat._INPUT_FILES.pop(run)  # exercise directory-only fallback used by callers with an existing run
    otlp_capture["manifest"].unlink()
    external = tmp_path / "unrelated.json"
    external.write_text("keep this file")
    alias = otlp_capture["directory"] / "symlink.json"
    alias.symlink_to(external)
    run.raw["_otel"]["source"]["source_files"].extend([str(external), str(alias)])
    files = input_files(run)
    assert external not in files and alias not in files
    assert set(otlp_capture["members"][1:]) <= set(files)
    protect_run_outputs(run, [external])


def _dashboard_callback_template(upload_obj, dark=False, selected_format="", session_id=None):
    return do_load(upload_obj, dark, selected_format, session_id=session_id)  # noqa: F821


def _dashboard_load_seam(monkeypatch):
    from agent_trajviz.insight.ui import load as ui_load

    calls = []

    def native(path, format_hint="", *, session_id=None):
        calls.append({"path": path, "format_hint": format_hint, "session_id": session_id})
        return calls[-1]

    def do_load(upload_obj, dark=False, selected_format="", *, session_id=None):
        return load_session(upload_obj, format_hint=selected_format, session_id=session_id)  # noqa: F821

    loading = types.FunctionType(do_load.__code__, {"load_session": native}, "do_load", do_load.__defaults__)
    loading.__kwdefaults__ = do_load.__kwdefaults__
    monkeypatch.setattr(ui_load, "load_session", native)
    monkeypatch.setattr(ui_load, "do_load", loading)
    callback = types.FunctionType(_dashboard_callback_template.__code__, {"do_load": loading}, "_do_load",
                                  _dashboard_callback_template.__defaults__)
    callback.__module__ = ui_load.__name__
    return callback, calls


@pytest.mark.parametrize("window", [None, 200000])
def test_dashboard_defaults_apply_only_to_mapped_source_and_accept_new_session_selection(otlp_capture, tmp_path,
                                                                                      monkeypatch, window):
    callback, native_calls = _dashboard_load_seam(monkeypatch)
    original = load_run
    loaded = []

    def load(path, **options):
        loaded.append((path, options))
        return original(path, **options)

    monkeypatch.setattr(compat, "load_run", load)
    directory = str(otlp_capture["directory"])
    display = str(otlp_capture["manifest"])
    wrapped = compat.dashboard_window_callback(callback, directory, window, session_id="session-a", display_path=display)
    first = wrapped(display)
    second = wrapped(display, session_id="session-b")
    assert first.run.raw["_otel"]["session_id"] == "session-a"
    assert second.run.raw["_otel"]["session_id"] == "session-b"
    assert loaded == [(directory, {"window_limit": window, "session_id": "session-a"}),
                      (directory, {"window_limit": window, "session_id": "session-b"})]
    unrelated = str(tmp_path / "other-capture.json")
    assert wrapped(unrelated, session_id="other-session") == {
        "path": unrelated, "format_hint": "", "session_id": "other-session"}
    assert wrapped(unrelated)["session_id"] is None
    assert len(loaded) == 2 and len(native_calls) == 2


def test_dashboard_keeps_upstream_selection_and_format_refusal_classification(otlp_capture, monkeypatch):
    callback, native_calls = _dashboard_load_seam(monkeypatch)
    directory = str(otlp_capture["directory"])
    display = str(otlp_capture["manifest"])
    wrapped = compat.dashboard_window_callback(callback, directory, None, session_id="session-a", display_path=display)
    assert wrapped(display, session_id="missing")["session_id"] == "missing"
    assert wrapped(display, selected_format="codex")["format_hint"] == "codex"
    assert native_calls == [{"path": directory, "format_hint": "", "session_id": "missing"},
                            {"path": directory, "format_hint": "codex", "session_id": "session-a"}]


@pytest.fixture
def dsh_family(tmp_path):
    def events(sid, version, tokens, **fields):
        return [{"type": "session", "id": sid, "version": version, "createdAt": 1790000000000, **fields},
                {"type": "assistant/message", "time": 1790000000300, "seq": 1, "data": {
                    "message": {"id": f"{sid}-a", "content": [{"type": "text", "text": "Controlled reply"}]},
                    "usage": {"inputTokens": tokens, "outputTokens": 0, "cacheReadTokens": 0,
                              "cacheWriteTokens": 0, "reasoningTokens": 0, "totalTokens": tokens}}}]

    parent, child, unrelated = (tmp_path / name for name in ("parent", "child", "unrelated"))
    for folder in (parent, child, unrelated):
        folder.mkdir()
    def write(path, rows):
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    stale = parent / "session.v3.jsonl"
    write(stale, events("parent", 3, 9999))
    selected = parent / "session.v4.jsonl"
    write(selected, [*events("parent", 4, 15), {"type": "subagent/catalog", "time": 1790000000400,
                                             "seq": 2, "data": {"childId": "child"}}])
    child_file = child / "session.v4.jsonl"
    write(child_file, events("child", 4, 5, parentSession="parent", origin="subagent"))
    unrelated_file = unrelated / "session.v4.jsonl"
    write(unrelated_file, events("unrelated", 4, 777, parentSession="parent", origin="subagent"))
    return parent, selected, child_file, stale, unrelated_file


@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("directory", [False, True])
def test_native_dsh_input_identity_and_protection_follow_only_the_consumed_generation(dsh_family, tmp_path,
                                                                                    compressed, directory):
    parent, selected, child, stale, unrelated = dsh_family
    if compressed:
        zstd = pytest.importorskip("zstandard")
        target = selected.with_name(selected.name + ".zstd")
        target.write_bytes(zstd.ZstdCompressor().compress(selected.read_bytes()))
        selected.unlink()
        selected = target
    source = parent if directory else selected
    before = {p: p.read_bytes() for p in (selected, child, stale, unrelated)}
    run = load_run(str(source))
    assert run.format == "dsh" and run.metrics["tokens"]["total"] == 20 and len(run.steps) == 2
    assert input_path(run) == source.absolute() and compat.resolve_input_path(str(source)) == source.absolute()
    files = input_files(run)
    assert selected in files and child in files and stale not in files and unrelated not in files
    alias = tmp_path / "child-alias.jsonl"
    alias.hardlink_to(child)
    with pytest.raises(BridgeError):
        protect_run_outputs(run, [alias])
    protect_run_outputs(run, [stale, unrelated])
    exported = tmp_path / "export.json"
    exported.write_text(json.dumps(run.to_dict(include_raw=True)))
    restored = load_run(str(exported))
    assert input_files(restored) == (exported,)
    protect_run_outputs(restored, [selected, child])
    assert {p: p.read_bytes() for p in before} == before


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
