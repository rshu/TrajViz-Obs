"""``dashboard`` and ``converge``: the TrajViz dashboards on localhost, the Insight one preloaded with the session."""

from __future__ import annotations

import errno
import io
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import agent_trajviz as tv
import gradio
import pytest
from agent_trajviz import MissingExtraError

from trajviz_obs.dashboard_cmd import _cached_label_notice, build_preloaded_insight
from trajviz_obs.errors import BridgeError


def test_preloaded_app_has_load_event(fixture_session):
    app = build_preloaded_insight(str(fixture_session))
    assert any("tv-load-btn" in (getattr(d, "js", "") or "") for d in app.fns.values()) or any("tv-load-btn" in str(c) for c in app.config["dependencies"])


def test_preloaded_attribution_heading_and_instructions_use_public_terminology(fixture_session):
    app = build_preloaded_insight(str(fixture_session))
    components = json.dumps(app.get_config_file()["components"], default=str)
    assert "Reference-based attribution (optional)" in components
    assert any(isinstance(block, gradio.Button) and block.value == "Inspect trajectory" for block in app.blocks.values())
    assert "decaf" not in components.lower()
    assert {"Attribution workspace", "Trajectory", "Saved runs & evaluation"} <= {
        block.label for block in app.blocks.values() if isinstance(block, gradio.Tab)}


def test_live_project_attribution_explains_inferred_identity_without_changing_explicit_validation(
        fixture_session, tmp_path):
    source = tmp_path / "-native-project" / "session.jsonl"
    source.parent.mkdir()
    source.write_bytes(fixture_session.read_bytes())
    app = build_preloaded_insight(str(source))
    run = _loaded_run(_loader(app), source)
    manual = next(entry.fn for entry in app.fns.values() if entry.api_name == "on_diagnose")
    automatic = next(entry.fn for entry in app.fns.values() if entry.api_name == "on_diagnose_autoload")
    markup, status = manual(run.raw, "", "", "")
    assert "No gold-grounded attribution" in status
    assert "no gold reference" in markup and "reference data" in markup and "Diagnose failure" in markup
    html, status, *_ = automatic(run.raw, "")
    assert html == "" and "Optional:" in status and "Diagnose failure" in status
    inspect = next(entry.fn for entry in app.fns.values() if entry.api_name == "on_inspect_trajectory")
    _html, _status, report = inspect(run)
    assert report["mode"] == "trajectory_only" and report["available"]
    assert report["outcome"]["status"] == "unknown"
    invalid = manual(run.raw, "../invalid", "case", "")[0]
    assert "invalid agent id" in invalid and "Supply --reference-root" not in invalid
    assert source.read_bytes() == fixture_session.read_bytes()


def test_preload_sets_the_trajectory_file_to_the_transcript(fixture_session):
    app = build_preloaded_insight(str(fixture_session))
    files = [b for b in app.blocks.values() if isinstance(b, gradio.File) and str(b.label).startswith("Trajectory")]
    assert len(files) == 1
    preload = [d for d in app.fns.values() if d.js and "tv-load-btn" in d.js and "setTimeout" in d.js]
    assert len(preload) == 1
    assert preload[0].outputs == [files[0]]
    assert preload[0].fn() == str(fixture_session)


def test_preloaded_file_keeps_its_real_path(fixture_session):
    """Gradio would hand TrajViz a cache copy, losing ``<id>/subagents/``; the transcript is served in place."""
    build_preloaded_insight(str(fixture_session))
    assert gradio.utils.is_static_file(str(fixture_session))


def _loader(app):
    return next(block for block in app.fns.values() if getattr(block.fn, "__name__", "") == "_do_load")


def _loaded_run(block, path, session_id=None):
    values = block.fn(str(path), False, "", session_id) if session_id is not None else block.fn(str(path), False, "")
    return next(value for value in values.values() if isinstance(value, tv.Trajectory))


def test_token_explanations_distinguish_missing_cache_counters_from_zero(tmp_path):
    source = tmp_path / "opencode.json"
    base = 1_790_000_000_000
    usage = {"total": 50, "input": 45, "output": 5}
    source.write_text(json.dumps({
        "source_format": "opencode_v2", "schema_version": 1,
        "session": {"id": "tokens", "time": {"created": base, "updated": base + 3000}},
        "messages": [{
            "id": f"message-{index}", "type": "assistant", "agent": "build",
            "content": [{"type": "text", "text": "Recorded reply"}],
            "time": {"created": base + index * 1000, "completed": base + (index + 1) * 1000},
            "tokens": tokens,
        } for index, tokens in enumerate([usage, {**usage, "reasoning": 0, "cache": {"read": 0, "write": 0}}])],
    }))
    app = build_preloaded_insight(str(source))
    outputs = _loader(app).fn(str(source), False, "")

    def markup(class_name):
        component = next(block for block in app.blocks.values() if class_name in (block.elem_classes or []))
        value = outputs[component]
        return value["value"] if isinstance(value, dict) else value

    summary = markup("token-summary-wrap")
    assert "0% cache hit" not in summary and "Largest step" in summary
    details = markup("token-step-explain")
    partial, complete = details.split("data-step='1'", 1)
    assert "Available token counts" in partial and "Some token counts are unavailable" in partial
    assert "Total 50" in partial and "No prefix cache hits" not in partial
    assert "No prefix cache hits" in complete


@pytest.fixture
def native_timed_parent_and_child(tmp_path):
    """A human prompt and child dispatch with equal text, missing parent usage, and a measured child zero."""
    base = 1_790_000_000_000

    def prompt(mid, offset):
        return {"type": "user", "id": mid, "text": "Please verify the final results",
                "time": {"created": base + offset}}

    def reply(mid, start, finish, content):
        return {"type": "assistant", "id": mid, "agent": "build", "model": {"id": "controlled"},
                "time": {"created": base + start, "completed": base + finish}, "content": content}

    spawn = {"type": "tool", "id": "spawn", "name": "subagent",
             "state": {"status": "completed", "metadata": {"sessionID": "child"}, "input": {}}}
    child = reply("child-reply", 340, 420, [{"type": "text", "text": "Done."}])
    child["tokens"] = {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}}
    path = tmp_path / "parent-and-child.json"
    path.write_text(json.dumps({"source_format": "opencode_v2", "schema_version": 1,
        "session": {"id": "root", "time": {"created": base, "updated": base + 420}},
        "messages": [prompt("root-prompt", 100), reply("root-reply", 200, 260, [spawn])],
        "children": [{"session": {"id": "child"}, "messages": [prompt("child-task", 300), child]}]}))
    return path


def test_dashboard_issues_stay_consistent_after_download_ai_and_settings_reset(native_timed_parent_and_child,
                                                                                tmp_path, monkeypatch):
    from agent_trajviz.insight.ui import overview_tab
    from agent_trajviz.insight.ui.shared import drop_latest_run

    monkeypatch.setenv("GRADIO_TEMP_DIR", str(tmp_path / "gradio"))
    settings = SimpleNamespace(config=SimpleNamespace(ready=False, missing=("ANALYZE_API_KEY",)))
    monkeypatch.setattr(overview_tab, "resolve_session_settings", lambda _settings: settings)
    app = build_preloaded_insight(str(native_timed_parent_and_child))
    request = SimpleNamespace(session_hash="controlled-issue-audit")
    export = next(entry for entry in app.fns.values() if entry.api_name == "prepare_issues_agent_download")
    suggest = next(entry.fn for entry in app.fns.values() if entry.api_name == "on_suggest_fixes")
    reset = next(entry for entry in app.fns.values() if entry.api_name == "reset_settings")
    loaded = _loader(app).fn(str(native_timed_parent_and_child), False, "", request=request)
    run, issues = (loaded[field] for field in export.inputs)
    try:
        assert issues == []
        result = export.fn(run, issues, request=request)
        assert json.loads(Path(result["value"]).read_text())["issues"] == []
        assert [payload for _, payload in suggest(run, request=request)] == [[]]
        assert reset.fn(run.steps, run)[reset.outputs.index(export.inputs[1])] == []
    finally:
        drop_latest_run(request.session_hash)


def test_dashboard_gantt_hover_distinguishes_missing_tokens_from_measured_zero(native_timed_parent_and_child):
    app = build_preloaded_insight(str(native_timed_parent_and_child))
    axis = next(entry for entry in app.fns.values() if entry.api_name == "on_gantt_axis_change")
    loaded = _loader(app).fn(str(native_timed_parent_and_child), False, "")
    steps = loaded[axis.inputs[1]]
    for fig in (loaded[axis.outputs[0]], axis.fn("wallclock", steps, False), axis.fn("skip_idle", steps, False)):
        hover = fig.data[0].hovertext
        assert hover[0].startswith("<b>Step 1</b>") and hover[0].endswith("Tokens unavailable")
        assert hover[1].startswith("<b>Step 3</b>") and hover[1].endswith("0 tokens")


def test_dashboard_workflow_view_control_is_serialized_with_its_native_change_event(fixture_session):
    config = build_preloaded_insight(str(fixture_session)).get_config_file()
    components = {component["id"]: component for component in config["components"]}

    def descendants(node):
        for child in node.get("children", []):
            yield child
            yield from descendants(child)

    workflow, = [node for node in descendants(config["layout"])
                 if components[node["id"]]["props"].get("label") == "Workflow"]
    toolbar, = [node for node in workflow["children"]
                if "obs-workflow-filters" in (components[node["id"]]["props"].get("elem_classes") or [])]
    view, = [components[node["id"]] for node in descendants(toolbar)
             if components[node["id"]]["type"] == "radio"]
    props = view["props"]
    assert props["label"] == "View" and props["visible"] is True
    assert props["choices"] == [("Cards", "Cards"), ("Expanded", "Expanded")]
    assert props["value"] == "Cards" and "obs-workflow-view" in props["elem_classes"]

    change, = [dependency for dependency in config["dependencies"]
               if any(target == view["id"] and event == "change" for target, event in dependency["targets"])]
    assert change["api_name"] == "do_filter_workflow" and view["id"] in change["inputs"]


def test_dashboard_workflow_keeps_token_evidence_when_switching_modes(native_timed_parent_and_child):
    app = build_preloaded_insight(str(native_timed_parent_and_child))
    callback = next(entry for entry in app.fns.values() if entry.api_name == "do_filter_workflow")
    loaded = _loader(app).fn(str(native_timed_parent_and_child), False, "")
    steps = loaded[callback.inputs[0]]
    original = loaded[callback.outputs[0]]
    cards = callback.fn(steps, "Assistant,User,All", "", "", "Cards")[0]
    expanded = callback.fn(steps, "Assistant,User,All", "", "", "Expanded")[0]
    for markup in (original, cards, expanded):
        steps_by_id = re.split(r"id=['\"]wf-card-(\d+)['\"]", markup)[1:]
        by_step = dict(zip(steps_by_id[::2], steps_by_id[1::2], strict=True))
        assert "<span>n/a tok</span>" in by_step["1"]
        assert "<span>0 tok</span>" in by_step["3"]


def test_host_context_window_reaches_native_dashboard_without_affecting_other_uploads(fixture_session,
                                                                                     synthetic_session):
    """The actual native load callback drives every chart/issue; no process-global loader is replaced."""
    baseline = tv.load(fixture_session).context_pressure()["stats"]["window_limit"]
    other_window = tv.load(synthetic_session).context_pressure()["stats"]["window_limit"]
    app = build_preloaded_insight(str(fixture_session), window=1_000_000)
    loader = _loader(app)
    run = _loaded_run(loader, fixture_session)
    assert run.context_pressure()["stats"]["window_limit"] == 1_000_000
    assert run.raw["metadata"]["context_window_limit"] == 1_000_000
    assert _loaded_run(loader, synthetic_session).context_pressure()["stats"]["window_limit"] == other_window
    assert tv.load(fixture_session).context_pressure()["stats"]["window_limit"] == baseline
    second_app = build_preloaded_insight(str(fixture_session), window=500_000)
    assert _loaded_run(_loader(second_app), fixture_session).context_pressure()["stats"]["window_limit"] == 500_000
    assert _loaded_run(loader, fixture_session).context_pressure()["stats"]["window_limit"] == 1_000_000


def test_dashboard_window_loader_preserves_explicit_format_mismatch(fixture_session):
    app = build_preloaded_insight(str(fixture_session), window=1_000_000)
    loaded = _loader(app).fn(str(fixture_session), False, "codex")
    assert any("Format mismatch" in str(value) for value in loaded.values())
    assert not any(isinstance(value, tv.Trajectory) for value in loaded.values())


def test_changed_dashboard_loader_cannot_silently_drop_context_window(fixture_session, monkeypatch):
    from trajviz_obs import dashboard_cmd

    app = build_preloaded_insight(str(fixture_session))
    _loader(app).fn = lambda *args: None
    monkeypatch.setattr(dashboard_cmd._dashboard(), "insight_app", lambda: app)
    with pytest.raises(BridgeError, match="context window") as caught:
        build_preloaded_insight(str(fixture_session), window=1_000_000)
    assert caught.value.code == "dashboard_incompatible"


@pytest.fixture
def dashboard_labels(tmp_path, fixture_session):
    from trajviz_obs.compat import load_run

    index, step = next((i, step) for i, step in enumerate(load_run(str(fixture_session)).steps)
                       if step["role"] == "assistant")
    path = tmp_path / "labels.json"
    path.write_text(json.dumps({"taxonomy_version": "2", "model": "test", "steps": [
        {"index": index, "raw_index": step["raw_index"], "role": "assistant", "agent": step.get("agent") or "",
         "session_id": step.get("session_id"), "time_created_ms": step.get("time_created_ms"),
         "phase": "understand", "action": "code_reading",
         "duration_s": 2.0, "tokens_total": 12, "text_preview": "Read the fixture"},
    ]}), encoding="utf-8")
    return path


def test_labels_preload_uses_all_native_charts_and_resets_for_other_trajectories(fixture_session,
                                                                              synthetic_session,
                                                                              dashboard_labels):
    app = build_preloaded_insight(str(fixture_session), labels_path=str(dashboard_labels))
    label_file = next(block for block in app.blocks.values()
                      if isinstance(block, gradio.File) and block.label == "Labels (optional)")
    reset = next(block for block in app.fns.values() if getattr(block.fn, "__name__", "") == "reset_labels")
    assert len(reset.inputs) == 3 and reset.inputs[0] is next(block for block in app.blocks.values()
        if isinstance(block, gradio.File) and str(block.label).startswith("Trajectory"))
    assert isinstance(reset.inputs[1], gradio.Dropdown) and isinstance(reset.inputs[2], gradio.State)
    loader = _loader(app)
    assert reset.trigger_after == loader._id and reset.targets == [(None, "then")]
    assert reset.fn(str(fixture_session), None, _loaded_run(loader, fixture_session))[-1]["value"] == str(dashboard_labels)
    assert reset.fn(str(synthetic_session), None, _loaded_run(loader, synthetic_session))[-1]["value"] is None
    assert reset.fn(str(fixture_session), None, None)[-1]["value"] is None
    assert reset.outputs[-1] is label_file and gradio.utils.is_static_file(str(dashboard_labels))
    native = next(block for block in app.fns.values() if getattr(block.fn, "__name__", "") == "do_load_labels")
    values = native.fn(str(dashboard_labels), False)
    assert all(values[index]["visible"] for index in (2, 5, 8))
    assert all(values[index].data for index in (3, 4, 6, 7, 9))
    assert any(target == (label_file._id, "change") for target in native.targets) or any(
        (label_file._id, "change") in block.targets for block in app.fns.values()
        if getattr(block.fn, "__name__", "") == "do_load_labels")


@pytest.fixture
def filtered_label_run(monkeypatch):
    from trajviz_obs import dashboard_cmd

    steps = [
        {"raw_index": 10, "role": "user", "agent": "", "session_id": "main", "time_created_ms": 100},
        {"raw_index": 12, "role": "assistant", "agent": "", "session_id": "main", "time_created_ms": 110},
        {"raw_index": 3, "role": "assistant", "agent": "child", "session_id": "main", "time_created_ms": 120},
        {"raw_index": 30, "role": "user", "agent": "", "session_id": "main", "time_created_ms": 130},
    ]
    run = SimpleNamespace(format="claude_code", raw={"_trajviz_obs_ignored_ui_records": 4}, steps=steps)
    monkeypatch.setattr(dashboard_cmd, "load_run", lambda _path: run)
    return run


def test_cached_labels_accept_assistant_only_prefix_without_rewriting_it(filtered_label_run, tmp_path):
    path = tmp_path / "labels.json"
    # Labels need not cover the current tail, or include the user steps between assistants.
    path.write_text(json.dumps({"steps": [dict(filtered_label_run.steps[1], index=1),
                                         dict(filtered_label_run.steps[2], index=2)]}))
    before = path.read_bytes()
    assert _cached_label_notice("current.jsonl", str(path)) is None
    assert path.read_bytes() == before


def test_native_export_labels_are_checked_even_without_the_runtime_filter_marker(filtered_label_run, tmp_path):
    filtered_label_run.raw = {}
    path = tmp_path / "labels.json"
    path.write_text(json.dumps({"steps": [dict(filtered_label_run.steps[1], index=1)]}))
    assert _cached_label_notice("trajectory.json", str(path)) is None
    path.write_text(json.dumps({"steps": [dict(filtered_label_run.steps[1], index=2)]}))
    assert "Run /obs label again" in _cached_label_notice("trajectory.json", str(path))


@pytest.mark.parametrize("change", [
    {"index": 20}, {"raw_index": 11}, {"role": "user"}, {"agent": "other"},
    {"session_id": "other"}, {"time_created_ms": 109}, {"raw_index": None},
])
def test_cached_labels_refuse_changed_step_identity(filtered_label_run, tmp_path, change):
    path = tmp_path / "labels.json"
    row = dict(filtered_label_run.steps[1], index=1)
    row.update(change)
    path.write_text(json.dumps({"steps": [row]}))
    assert "Run /obs label again" in _cached_label_notice("current.jsonl", str(path))


def test_incompatible_cached_labels_show_notice_and_clear_on_another_trajectory(fixture_session,
                                                                             fixture_run, tmp_path):
    path = tmp_path / "labels.json"
    step = next(step for step in fixture_run.steps if step["role"] == "assistant")
    path.write_text(json.dumps({"steps": [dict(step, index=len(fixture_run.steps) + 1)]}))
    before = path.read_bytes()
    app = build_preloaded_insight(str(fixture_session), labels_path=str(path))
    reset = next(block for block in app.fns.values() if getattr(block.fn, "__name__", "") == "reset_labels")
    loaded = _loaded_run(_loader(app), fixture_session)
    outputs = reset.fn(str(fixture_session), None, loaded)
    assert outputs[-1]["value"] is None and "Run /obs label again" in outputs[1]
    assert "Run /obs label again" not in reset.fn(str(tmp_path / "another.jsonl"), None, loaded)[1]
    assert path.read_bytes() == before


def test_dashboard_rejects_invalid_label_sidecar_before_launch(fixture_session, tmp_path, run_cli):
    labels = tmp_path / "invalid.json"
    labels.write_text("{}", encoding="utf-8")
    code, out = run_cli(["dashboard", "--transcript", str(fixture_session), "--labels", str(labels)])
    assert code == 1 and out["error"]["trajviz_code"] == "invalid_labels"


def test_dashboard_rejects_missing_label_sidecar_before_launch(fixture_session, tmp_path, run_cli):
    code, out = run_cli(["dashboard", "--transcript", str(fixture_session),
                         "--labels", str(tmp_path / "gone.json")])
    assert code == 1 and out["error"]["code"] == "not_found"


def test_dashboard_rejects_nonpositive_window(fixture_session, run_cli):
    code, out = run_cli(["dashboard", "--transcript", str(fixture_session), "--window", "0"])
    assert code == 2 and out["error"]["code"] == "usage"


@pytest.mark.parametrize("dark", [False, True])
def test_dashboard_writes_offline_html_without_starting_a_server(fixture_session, tmp_path, monkeypatch, run_cli, dark):
    from trajviz_obs import dashboard_cmd

    monkeypatch.setattr(dashboard_cmd, "_dashboard", lambda: pytest.fail("No app is needed for a static export"))
    output = tmp_path / "new folder" / "session.html"
    labels = tmp_path / "old-labels.json"
    labels.write_text("{}", encoding="utf-8")
    original = fixture_session.read_bytes()
    flags = ["--dark"] if dark else []
    code, result = run_cli(["dashboard", "--transcript", str(fixture_session), "--window", "1000000",
                            "--labels", str(labels), "--html", str(output), *flags])
    markup = output.read_text(encoding="utf-8")
    assert code == 0 and result == {"path": str(output), "bytes": output.stat().st_size}
    assert markup.startswith("<!DOCTYPE html>") and "plotly.js v" in markup
    assert not re.search(r"<script\b[^>]*\bsrc=", markup) and "Deep dive" in markup
    assert f"class='tv-theme-{'dark' if dark else 'light'}'" in markup
    if os.name != "nt":
        assert output.stat().st_mode & 0o077 == 0
    assert fixture_session.read_bytes() == original


@pytest.mark.parametrize("alias", ["input", "hardlink", "symlink", "labels"])
def test_dashboard_html_rejects_source_and_sidecar_aliases(fixture_session, dashboard_labels, tmp_path, run_cli, alias):
    source = tmp_path / "session.jsonl"
    source.write_bytes(fixture_session.read_bytes())
    dest = tmp_path / "report.html"
    if alias == "input":
        dest = source
    elif alias == "hardlink":
        dest.hardlink_to(source)
    elif alias == "symlink":
        dest.symlink_to(source)
    else:
        dest.symlink_to(dashboard_labels)
    before = dest.read_bytes()
    code, result = run_cli(["dashboard", "--transcript", str(source), "--labels", str(dashboard_labels),
                            "--html", str(dest)])
    assert code == 1 and result["error"]["code"] == "bad_output"
    assert dest.read_bytes() == before


def test_dashboard_html_rejects_listen_options(fixture_session, tmp_path, run_cli):
    output = tmp_path / "session.html"
    code, result = run_cli(["dashboard", "--transcript", str(fixture_session), "--html", str(output), "--port", "1234"])
    assert code == 2 and result["error"]["code"] == "usage" and not output.exists()


def test_dashboard_html_requires_a_session_for_ambiguous_capture(captured_sessions, tmp_path, run_cli):
    output = tmp_path / "selected.html"
    argv = ["dashboard", "--transcript", str(captured_sessions.directory), "--html", str(output)]
    code, result = run_cli(argv)
    assert code == 1 and result["error"]["trajviz_code"] == "ambiguous_session" and not output.exists()
    code, result = run_cli([*argv, "--session-id", "capture-a"])
    assert code == 0 and result["path"] == str(output)
    assert "<script" in output.read_text(encoding="utf-8")


def test_live_theme_and_inline_download_apply_only_to_the_selected_dashboard(fixture_session, tmp_path, monkeypatch):
    from agent_trajviz.insight.ui import upload

    monkeypatch.setenv("GRADIO_TEMP_DIR", str(tmp_path / "gradio"))
    normal = build_preloaded_insight(str(fixture_session))
    customized = build_preloaded_insight(str(fixture_session), dark=True, inline_plotly=True)
    for app, dark in ((normal, False), (customized, True)):
        initializer = next(entry for entry in app.fns.values() if "window.tvSetGradioText = " in (entry.js or ""))
        assert initializer.fn() is dark and initializer.outputs[0].value is dark
        assert f"colorScheme = '{'dark' if dark else 'light'}'" in initializer.js
        outputs = _loader(app).fn(str(fixture_session), initializer.fn())
        run = next(value for value in outputs.values() if isinstance(value, tv.Trajectory))
        export = next(entry.fn for entry in app.fns.values() if entry.api_name == "prepare_html_export")
        assert export is not upload.prepare_html_export
        exported = export(run, None, initializer.fn(), request=SimpleNamespace(session_hash=f"theme-{dark}"))
        html = Path(exported["value"]).read_text(encoding="utf-8")
        assert ("plotly.js v" in html) is dark
        assert bool(re.search(r"<script\b[^>]*\bsrc=", html)) is not dark
        assert f"class='tv-theme-{'dark' if dark else 'light'}'" in html


def test_expanded_workflow_caps_tool_io_and_keeps_full_source(fixture_session, fixture_run):
    app = build_preloaded_insight(str(fixture_session))
    render = next(entry.fn for entry in app.fns.values() if entry.api_name == "do_filter_workflow")
    step, part = next((step, part) for step in fixture_run.steps for part in step["parts"]
                      if part.get("type") == "tool_call")
    recorded = "<img src=x onerror=alert(1)>" + "a" * 200_000 + "beyond-the-limit"
    part = {**part, "input": recorded, "output": recorded}
    step = {**step, "parts": [part]}
    cards, _, _ = render([step], "Assistant,User,All", "", "", "Cards")
    expanded, _, _ = render([step], "Assistant,User,All", "", "", "Expanded")
    assert "class='wf-expand-body'" in expanded and "class='wf-expand-body'" not in cards
    assert expanded.count("truncated at 200,000 chars") == 2
    assert "beyond-the-limit" not in expanded
    assert "&lt;img src=x onerror=alert(1)&gt;" in expanded and "<img src=x" not in expanded
    assert part["input"] == recorded == part["output"]


def test_dashboard_launch_preserves_native_auth_and_upload_limit(monkeypatch):
    from trajviz_obs.compat import dashboard_server_options

    monkeypatch.setenv("TRAJVIZ_AUTH", "reader:secret:with:colons")
    assert dashboard_server_options(8765) == {
        "server_name": "127.0.0.1", "server_port": 8765, "share": False,
        "auth": ("reader", "secret:with:colons"), "max_file_size": "500mb",
    }
    monkeypatch.setenv("TRAJVIZ_AUTH", "invalid")
    with pytest.raises(ValueError, match="TRAJVIZ_AUTH"):
        dashboard_server_options(None)


class _EofAfterProbe(io.StringIO):
    """A stdin that fetches the URL the command printed (proving the server is up), then reports EOF."""

    def __init__(self, stdout: io.StringIO) -> None:
        super().__init__("")
        self.stdout = stdout
        self.status: int | None = None

    def _probe(self) -> None:
        if self.status is None:
            url = json.loads(self.stdout.getvalue().splitlines()[0])["url"]
            with urllib.request.urlopen(url, timeout=30) as response:
                self.status = response.status

    def read(self, *args):
        self._probe()
        return super().read(*args)

    def readline(self, *args):
        self._probe()
        return super().readline(*args)

    def __iter__(self):
        self._probe()
        return super().__iter__()


def _serve(monkeypatch, argv: list[str]) -> tuple[int, list[str], _EofAfterProbe]:
    from trajviz_obs import cli

    out = io.StringIO()
    stdin = _EofAfterProbe(out)
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", out)
    code = cli.main(argv)
    return code, out.getvalue().splitlines(), stdin


def _argv(kind: str, transcript, data_root) -> list[str]:
    return (["dashboard", "--transcript", str(transcript)] if kind == "dashboard"
            else ["converge", "--data-root", str(data_root)])


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.mark.parametrize("explicit_port", [False, True], ids=["any-port", "explicit-port"])
@pytest.mark.parametrize("kind", ["dashboard", "converge"])
def test_serves_until_stdin_closes(kind, explicit_port, fixture_session, tmp_path, monkeypatch):
    port = _free_port() if explicit_port else None
    argv = [*_argv(kind, fixture_session, tmp_path), "--exit-on-stdin", *(["--port", str(port)] if port else [])]
    code, lines, stdin = _serve(monkeypatch, argv)
    assert code == 0
    assert len(lines) == 1
    url = json.loads(lines[0])["url"]
    assert url.startswith(f"http://127.0.0.1:{port}/" if port else "http://127.0.0.1:")
    assert stdin.status == 200
    with pytest.raises(OSError):
        urllib.request.urlopen(url, timeout=5)


@pytest.mark.parametrize("kind", ["dashboard", "converge"])
def test_managed_process_survives_closed_stdin_and_stops_on_sigterm(kind, fixture_session, tmp_path):
    """Claude Code's $.process.spawn does not supply stdin; the URL must remain usable after startup."""
    launcher = Path(__file__).resolve().parents[1] / "trajviz_obs_cli.py"
    argv = [sys.executable, "-I", str(launcher), *_argv(kind, fixture_session, tmp_path)]
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    first: queue.Queue[str] = queue.Queue()
    threading.Thread(target=lambda: first.put(proc.stdout.readline()), daemon=True).start()
    try:
        line = first.get(timeout=60)
        assert line, proc.stderr.read() if proc.poll() is not None else "No URL announced"
        url = json.loads(line)["url"]
        with urllib.request.urlopen(url, timeout=30) as response:
            assert response.status == 200
        assert proc.poll() is None
        proc.terminate()
        stdout, stderr = proc.communicate(timeout=30)
        assert proc.returncode == 0, stderr
        assert stdout == ""
        with pytest.raises(OSError):
            urllib.request.urlopen(url, timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=10)


def test_missing_app_extra_prints_only_the_error(fixture_session, run_cli, monkeypatch):
    from trajviz_obs import dashboard_cmd

    def missing() -> None:
        raise MissingExtraError("app", ["gradio"], "agent_trajviz.dashboard")

    monkeypatch.setattr(dashboard_cmd, "_dashboard", missing)
    code, out = run_cli(["dashboard", "--transcript", str(fixture_session)])
    assert code == 1
    assert out["error"]["code"] == "missing_extra" and out["error"]["extra"] == "app"


def test_missing_transcript_is_not_found(tmp_path, run_cli):
    code, out = run_cli(["dashboard", "--transcript", str(tmp_path / "gone.jsonl")])
    assert code == 1 and out["error"]["code"] == "not_found"


@pytest.mark.parametrize("layout", ["native", "session-jsonl", "single-parent"])
def test_dashboard_directory_selector_preloads_the_actual_parent(layout, tmp_session, tmp_path, run_cli,
                                                                monkeypatch):
    from trajviz_obs import dashboard_cmd

    if layout == "native":
        directory = tmp_session.dir / tmp_session.id
        selected = Path(tmp_session.transcript)
    else:
        directory = tmp_path / "saved-run"
        directory.mkdir()
        selected = directory / ("session.jsonl" if layout == "session-jsonl" else "parent.jsonl")
        selected.write_bytes(Path(tmp_session.transcript).read_bytes())
    launched = []

    def launch(path, *_args, **_kwargs):
        launched.append(path)
        dashboard_cmd._announce("http://127.0.0.1:1234/")

    monkeypatch.setattr(dashboard_cmd, "serve_insight", launch)
    code, result = run_cli(["dashboard", "--transcript", str(directory), "--window", "1000000"])
    assert code == 0 and result["url"] == "http://127.0.0.1:1234/" and launched == [str(selected.resolve())]
    app = build_preloaded_insight(launched[0], window=1_000_000)
    run = _loaded_run(_loader(app), selected)
    assert run.context_pressure()["stats"]["window_limit"] == 1_000_000
    if layout == "native":
        assert any(step.get("agent") == "abc" for step in run.steps)


def test_missing_data_root_is_not_found(tmp_path, run_cli):
    code, out = run_cli(["converge", "--data-root", str(tmp_path / "gone")])
    assert code == 1 and out["error"]["code"] == "not_found"


@pytest.mark.parametrize("kind", ["dashboard", "converge"])
def test_failing_plugin_is_reported_before_any_url(kind, fixture_session, run_cli, fake_plugins):
    def broken() -> None:
        raise RuntimeError("boom")

    fake_plugins("broken", broken)
    code, out = run_cli(_argv(kind, fixture_session, fixture_session.parent))
    assert code == 1 and out["error"]["code"] == "plugin_error" and "boom" in out["error"]["message"]


@pytest.fixture
def builds(monkeypatch) -> list[str]:
    """The dashboard apps the test asked TrajViz to build; each build is a stand-in that builds nothing."""
    from agent_trajviz import dashboard

    built: list[str] = []
    monkeypatch.setattr(dashboard, "insight_app", lambda **kwargs: built.append("insight"))
    monkeypatch.setattr(dashboard, "converge_app", lambda **kwargs: built.append("converge"))
    return built


@pytest.mark.parametrize("kind", ["dashboard", "converge"])
def test_busy_port_is_port_busy(kind, fixture_session, tmp_path, run_cli, builds):
    """A --port another socket holds is ``port_busy``, found before the app is built."""
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        code, out = run_cli([*_argv(kind, fixture_session, tmp_path), "--port", str(port)])  # one JSON document
    assert code == 1 and out["error"]["code"] == "port_busy" and builds == []
    assert str(port) in out["error"]["message"] and out["error"]["port"] == port


@pytest.mark.parametrize("port", ["70000", "0"])
@pytest.mark.parametrize("kind", ["dashboard", "converge"])
def test_port_outside_1_to_65535_is_a_usage_error(kind, port, fixture_session, tmp_path, run_cli, builds):
    code, out = run_cli([*_argv(kind, fixture_session, tmp_path), "--port", port])
    assert code == 2 and out["error"]["code"] == "usage" and builds == []
    assert "--port" in out["error"]["message"] and "1 to 65535" in out["error"]["message"]
    assert repr(port) in out["error"]["message"]


@pytest.mark.parametrize("stage", ["build", "launch"])
@pytest.mark.parametrize("kind", ["dashboard", "converge"])
def test_other_os_error_with_a_port_stays_internal(kind, stage, fixture_session, tmp_path, run_cli, monkeypatch):
    """Only a port that cannot be bound is ``port_busy``: an OSError from building or launching the app is not."""
    from agent_trajviz import dashboard

    def unwritable(*args, **kwargs):
        raise PermissionError(errno.EACCES, "Permission denied", str(tmp_path / "gradio-cache"))

    if stage == "build":  # converge builds its app inside launch_converge
        monkeypatch.setattr(dashboard, "insight_app" if kind == "dashboard" else "converge_app", unwritable)
    else:
        monkeypatch.setattr(gradio.Blocks, "launch", unwritable)
    code, out = run_cli([*_argv(kind, fixture_session, tmp_path), "--port", str(_free_port())])
    assert code == 3 and out["error"]["code"] == "internal" and "Permission denied" in out["error"]["message"]


@pytest.fixture
def captured_sessions(tmp_path):
    from trajviz_obs.compat import load_run

    folder = tmp_path / "capture"
    folder.mkdir()
    spans = [{"traceId": str(index) * 32, "spanId": str(index) * 16, "name": "claude_code.llm_request",
        "startTimeUnixNano": "1790000000000000000", "endTimeUnixNano": "1790000001000000000",
        "attributes": [{"key": "session.id", "value": {"stringValue": sid}},
                       {"key": "input_tokens", "value": {"intValue": "7"}}]}
        for index, sid in enumerate(("capture-a", "capture-b"), 1)]
    source = folder / "traces.jsonl"
    source.write_text(json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}) + "\n")
    manifest = folder / "manifest.json"
    manifest.write_text(json.dumps({"type": "agent-trajviz-otlp-bundle", "schema_version": 1,
                                   "profile": "claude_code", "traces": [source.name], "logs": []}))
    run = load_run(str(folder), session_id="capture-a")
    step = run.steps[0]
    labels = tmp_path / "labels-a.json"
    labels.write_text(json.dumps({"schema_version": "trajectory_labels.v2", "taxonomy_version": "2", "steps": [{
        "index": 0, "raw_index": step["raw_index"], "role": step["role"], "agent": step["agent"],
        "session_id": step["session_id"], "time_created_ms": step["time_created_ms"],
        "phase": "understand", "action": "code_reading", "text_preview": "Controlled capture"}]}))
    return SimpleNamespace(directory=folder, manifest=manifest, source=source, labels=labels)


def _session_inspector(app):
    return next(block for block in app.fns.values() if block.api_name == "_inspect_sessions")


def test_dashboard_capture_default_is_target_only_and_user_selection_wins(captured_sessions, tmp_path):
    source = str(captured_sessions.source)
    app = build_preloaded_insight(source, session_id="capture-a")
    loader = _loader(app)
    assert _loaded_run(loader, source).raw["_otel"]["session_id"] == "capture-a"
    assert _loaded_run(loader, source, "capture-b").raw["_otel"]["session_id"] == "capture-b"
    inspector = _session_inspector(app)
    assert inspector.fn(source)["value"] == "capture-a"
    other = tmp_path / "other-capture.jsonl"
    other.write_bytes(captured_sessions.source.read_bytes())
    assert inspector.fn(str(other))["value"] is None


def test_dashboard_does_not_choose_the_first_session_from_an_ambiguous_capture(captured_sessions):
    app = build_preloaded_insight(str(captured_sessions.source))
    assert _session_inspector(app).fn(str(captured_sessions.source))["value"] is None
    result = _loader(app).fn(str(captured_sessions.source), False, "", None)
    assert not any(isinstance(value, tv.Trajectory) for value in result.values())


def test_dashboard_directory_display_manifest_maps_back_to_original_capture(captured_sessions):
    from trajviz_obs.compat import input_path

    app = build_preloaded_insight(str(captured_sessions.directory), session_id="capture-a")
    assert app._obs_display_path == str(captured_sessions.manifest)
    first = _loaded_run(_loader(app), app._obs_display_path)
    second = _loaded_run(_loader(app), app._obs_display_path, "capture-b")
    assert input_path(first) == captured_sessions.directory
    assert first.raw["_otel"]["session_id"] == "capture-a" and second.raw["_otel"]["session_id"] == "capture-b"
    assert _session_inspector(app).fn(app._obs_display_path)["value"] == "capture-a"


def test_labels_follow_loaded_session_state_and_clear_after_selection_change_or_failure(captured_sessions, tmp_path):
    app = build_preloaded_insight(str(captured_sessions.directory), session_id="capture-a",
                                  labels_path=str(captured_sessions.labels))
    loader = _loader(app)
    reset = next(block for block in app.fns.values() if getattr(block.fn, "__name__", "") == "reset_labels")
    display = app._obs_display_path
    first = _loaded_run(loader, display)
    second = _loaded_run(loader, display, "capture-b")
    assert reset.fn(display, "capture-a", first)[-1]["value"] == str(captured_sessions.labels)
    assert reset.fn(display, "capture-b", second)[-1]["value"] is None
    assert reset.fn(display, "capture-b", first)[-1]["value"] is None
    assert reset.fn(display, None, second)[-1]["value"] is None
    assert reset.fn(display, "capture-a", None)[-1]["value"] is None
    assert reset.fn(str(tmp_path / "another-capture.json"), "capture-a", first)[-1]["value"] is None
    assert reset.trigger_after == loader._id and reset.inputs[2] in loader.outputs


def test_labels_fail_closed_when_the_native_loaded_state_seam_is_absent(fixture_session, dashboard_labels):
    from trajviz_obs import dashboard_cmd

    app = dashboard_cmd._dashboard().insight_app()
    loader = _loader(app)
    original = loader.fn

    def _do_load(*args):
        return original(*args)

    loader.fn = _do_load
    assert dashboard_cmd._add_labels_preload(app, str(fixture_session), str(dashboard_labels)) is False
    reset = next(block for block in app.fns.values() if getattr(block.fn, "__name__", "") == "_reset_labels")
    assert not reset.inputs and reset.fn()[-1]["value"] is None


def test_cached_label_validation_applies_to_every_format_and_selected_session(filtered_label_run, tmp_path):
    filtered_label_run.format = "pi"
    filtered_label_run.raw = {}
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps({"steps": [dict(filtered_label_run.steps[1], index=2)]}))
    assert "Run /obs label again" in _cached_label_notice("pi.jsonl", str(labels))
    labels.write_text(json.dumps({"steps": [dict(filtered_label_run.steps[1], index=1)]}))
    assert _cached_label_notice("pi.jsonl", str(labels), session_id="other", run=filtered_label_run)


def test_dashboard_capture_selection_refusal_happens_before_any_server_announcement(captured_sessions, run_cli,
                                                                                  monkeypatch):
    from trajviz_obs import dashboard_cmd

    monkeypatch.setattr(dashboard_cmd, "serve_insight", lambda *_args, **_kwargs: pytest.fail("No server may start"))
    argv = ["dashboard", "--transcript", str(captured_sessions.directory), "--session-id", "missing-session"]
    code, result = run_cli(argv)
    assert code == 1 and result["error"]["trajviz_code"] == "session_not_found"


def test_dashboard_multi_session_directory_without_default_opens_its_selector(captured_sessions):
    app = build_preloaded_insight(str(captured_sessions.directory), labels_path=str(captured_sessions.labels))
    assert app._obs_display_path == str(captured_sessions.manifest)
    assert _session_inspector(app).fn(app._obs_display_path)["value"] is None
    assert app._obs_labels_preloaded is False
    result = _loader(app).fn(app._obs_display_path, False, "", None)
    assert not any(isinstance(value, tv.Trajectory) for value in result.values())
    assert _loaded_run(_loader(app), app._obs_display_path, "capture-b").raw["_otel"]["session_id"] == "capture-b"


def test_dashboard_cli_can_serve_a_capture_without_selecting_a_session(captured_sessions, run_cli, monkeypatch):
    from trajviz_obs import dashboard_cmd

    launched = []

    def launch(path, _port, **options):
        launched.append((path, options["session_id"]))
        dashboard_cmd._announce("http://127.0.0.1:1234/")

    monkeypatch.setattr(dashboard_cmd, "serve_insight", launch)
    code, result = run_cli(["dashboard", "--transcript", str(captured_sessions.directory)])
    assert code == 0 and result["url"] == "http://127.0.0.1:1234/"
    assert launched == [(str(captured_sessions.directory), None)]


def _native_directory(tmp_path):
    folder = tmp_path / "native"
    folder.mkdir()
    rows = [{"type": "session", "version": 4, "id": "controlled", "createdAt": 1790000000000},
            {"type": "assistant/message", "time": 1790000000300, "data": {"message": {
                "id": "a", "content": [{"type": "text", "text": "Controlled reply"}]},
                "usage": {"inputTokens": 3, "outputTokens": 2, "totalTokens": 5}}}]
    (folder / "session.v4.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    return folder


def test_native_directory_uses_a_private_display_export_and_keeps_original_analysis_source(tmp_path):
    from trajviz_obs.compat import input_path

    folder = _native_directory(tmp_path)
    app = build_preloaded_insight(str(folder))
    try:
        display = Path(app._obs_display_path)
        assert display.is_file() and display.parent != folder
        run = _loaded_run(_loader(app), str(display))
        assert run.format == "dsh" and run.metrics["tokens"]["total"] == 5
        assert input_path(run) == folder
    finally:
        app.close()
        app._obs_display_owner.cleanup()


def test_dashboard_server_exposes_only_display_file_and_cleans_temporary_export(tmp_path, monkeypatch, capsys):
    from trajviz_obs import dashboard_cmd

    folder = _native_directory(tmp_path)
    allowed = []

    def launch(_app, **options):
        allowed.extend(options["allowed_paths"])
        assert len(allowed) == 1 and Path(allowed[0]).is_file() and allowed[0] != str(folder)
        return None, "http://127.0.0.1:1234/", None

    monkeypatch.setattr(gradio.Blocks, "launch", launch)
    monkeypatch.setattr(dashboard_cmd, "_wait_for_shutdown", lambda **_: None)
    dashboard_cmd.serve_insight(str(folder), None)
    assert json.loads(capsys.readouterr().out)["url"] == "http://127.0.0.1:1234/"
    assert not Path(allowed[0]).exists()


@pytest.mark.parametrize("kind", ["dashboard", "converge"])
def test_dashboard_still_launches_when_usage_attachment_fails(kind, fixture_session, tmp_path, monkeypatch, capsys):
    from trajviz_obs import dashboard_cmd
    from trajviz_obs.dashboard_usage import DashboardUsage

    def broken_attach(_usage, _app, received_kind):
        assert received_kind == kind
        raise RuntimeError("instrumentation unavailable")

    monkeypatch.setattr(DashboardUsage, "attach", broken_attach)
    monkeypatch.setattr(gradio.Blocks, "launch", lambda _app, **_: (None, "http://127.0.0.1:1234/", None))
    monkeypatch.setattr(dashboard_cmd, "_wait_for_shutdown", lambda **_: None)
    usage = DashboardUsage("http://127.0.0.1:8765/v1/events", tmp_path, "claude-code", "0.3.8")
    if kind == "dashboard":
        dashboard_cmd.serve_insight(str(fixture_session), None, usage=usage)
    else:
        dashboard_cmd.serve_converge(str(tmp_path), None, usage=usage)
    captured = capsys.readouterr()
    assert json.loads(captured.out)["url"] == "http://127.0.0.1:1234/"
    assert "Could not attach usage telemetry (RuntimeError)." in captured.err
