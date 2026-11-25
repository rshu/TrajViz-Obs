"""The dashboard never authorizes private prompt data from a browser-supplied ID."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import gradio as gr
import pytest
from agent_trajviz import Trajectory, dashboard

from prompt_capture import write_record
from trajviz_obs.compat import load_run

PRIVATE_A = "PRIVATE_CAPTURE_SESSION_A_8d3092"
PRIVATE_B = "PRIVATE_CAPTURE_SESSION_B_4a128c"
PRIVATE_PI = "PRIVATE_PI_SAME_SESSION_ID_2db89f"


def _source(directory: Path, name: str, session: str) -> Path:
    source = directory / name
    source.write_text(json.dumps({
        "source_format": "opencode_v2", "schema_version": 1, "session": {"id": session},
        "messages": [
            {"type": "user", "id": "user", "text": "Review the small fixture."},
            {"type": "assistant", "id": "assistant", "time": {"created": 1_790_000_000_000, "completed": 1_790_000_001_000},
             "content": [{"type": "text", "text": "Reviewed."}], "tokens": {"input": 5, "output": 2}},
        ],
    }))
    return source


def _capture(root: Path, session: str, text: str, *, adapter: str = "opencode") -> Path:
    return Path(write_record(str(root), {
        "schema_version": 1, "adapter": adapter, "session_id": session, "instance_id": "instance",
        "record_id": f"record-{session}", "sequence": 1, "observed_at": "2026-11-25T00:00:01.000Z",
        "stage": "context", "kind": "primary", "correlation": {"agent_id": "build"},
        "data": {"messages": [{"role": "user", "content": text}], "tools": []},
    }))


@pytest.fixture
def scoped_capture(tmp_path, monkeypatch):
    root = tmp_path / "captures"
    source = _source(tmp_path, "trusted.json", "session-a")
    forged = _source(tmp_path, "browser-upload.json", "session-a")
    other = _source(tmp_path, "other-session.json", "session-b")
    file = _capture(root, "session-a", PRIVATE_A)
    _capture(root, "session-b", PRIVATE_B)
    _capture(root, "session-a", PRIVATE_PI, adapter="pi")
    monkeypatch.setenv("TRAJVIZ_OBS_PROMPT_CAPTURE", "1")
    monkeypatch.setenv("TRAJVIZ_OBS_PROMPT_CAPTURE_DIR", str(root))
    monkeypatch.setenv("TRAJVIZ_OBS_TELEMETRY", "0")
    return {"root": root, "source": source, "forged": forged, "other": other, "file": file}


def _scoped(run, fixture, **options):
    from trajviz_obs.prompt_explorer import scoped_dataset

    return scoped_dataset(run, pinned_source=fixture["source"], capture_root=fixture["root"], **options)


def _mounted(fixture, *, enabled=True):
    from trajviz_obs.prompt_explorer import mount_prompt_explorer

    app = dashboard.insight_app()
    assert mount_prompt_explorer(app, transcript_path=str(fixture["source"]), session_id="session-a",
                                capture_root=fixture["root"] if enabled else None)
    return app


def _callback(app, name):
    return next(entry for entry in app.fns.values() if entry.api_name == name)


def test_only_the_host_pinned_source_can_open_its_matching_private_capture(scoped_capture):
    fixture = scoped_capture
    dataset = _scoped(load_run(str(fixture["source"])), fixture)
    serialized = json.dumps(dataset)
    assert PRIVATE_A in serialized
    assert PRIVATE_B not in serialized
    assert PRIVATE_PI not in serialized


def test_browser_upload_cannot_authorize_capture_by_forging_the_native_session_id(scoped_capture):
    fixture = scoped_capture
    run = load_run(str(fixture["forged"]))
    # Embedded provenance is not the actual input file opened by the bridge.
    run.raw["source"] = str(fixture["source"])
    dataset = _scoped(run, fixture, session_id="session-a")
    assert not any(value in json.dumps(dataset) for value in (PRIVATE_A, PRIVATE_B, PRIVATE_PI))


def test_other_imported_session_cannot_open_private_capture_from_the_launch_directory(scoped_capture):
    fixture = scoped_capture
    dataset = _scoped(load_run(str(fixture["other"])), fixture, session_id="session-b")
    assert not any(value in json.dumps(dataset) for value in (PRIVATE_A, PRIVATE_B, PRIVATE_PI))


def test_cleared_or_failed_load_replaces_old_capture_with_empty_data(scoped_capture):
    fixture = scoped_capture
    populated = _scoped(load_run(str(fixture["source"])), fixture)
    assert PRIVATE_A in json.dumps(populated)
    cleared = _scoped(None, fixture)
    assert not any(value in json.dumps(cleared) for value in (PRIVATE_A, PRIVATE_B, PRIVATE_PI))


def test_mismatched_capture_selector_does_not_override_the_host_pinned_session(scoped_capture):
    fixture = scoped_capture
    dataset = _scoped(load_run(str(fixture["source"])), fixture, session_id="session-b")
    assert not any(value in json.dumps(dataset) for value in (PRIVATE_A, PRIVATE_B, PRIVATE_PI))


def test_mount_keeps_capture_files_out_of_config_static_paths_and_upload_controls(scoped_capture):
    from trajviz_obs.prompt_explorer import mount_prompt_explorer

    fixture = scoped_capture
    app = dashboard.insight_app()
    upload_ids = {block._id for block in app.blocks.values() if isinstance(block, gr.File)}
    assert mount_prompt_explorer(app, transcript_path=str(fixture["source"]), session_id="session-a", capture_root=fixture["root"])
    assert any(isinstance(block, gr.Tab) and block.label == "Prompt Explorer" for block in app.blocks.values())
    config = json.dumps(app.get_config_file(), default=str)
    assert not any(value in config for value in (PRIVATE_A, PRIVATE_B, PRIVATE_PI))
    assert str(fixture["root"]) not in config
    assert not gr.utils.is_static_file(str(fixture["file"]))
    assert {block._id for block in app.blocks.values() if isinstance(block, gr.File)} == upload_ids


def test_mount_is_idempotent(scoped_capture):
    from trajviz_obs.prompt_explorer import mount_prompt_explorer

    fixture = scoped_capture
    app = dashboard.insight_app()
    assert mount_prompt_explorer(app, transcript_path=str(fixture["source"]), session_id="session-a", capture_root=fixture["root"])
    mount_prompt_explorer(app, transcript_path=str(fixture["source"]), session_id="session-a", capture_root=fixture["root"])
    assert sum(isinstance(block, gr.Tab) and block.label == "Prompt Explorer" for block in app.blocks.values()) == 1


def test_launch_environment_alone_cannot_enable_dashboard_prompt_access(scoped_capture, monkeypatch):
    from trajviz_obs import prompt_explorer

    fixture = scoped_capture
    calls = []
    original = prompt_explorer.scoped_dataset

    def observe(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(prompt_explorer, "scoped_dataset", observe)
    app = dashboard.insight_app()
    # Capture environment is deliberately on. Only vetted host arguments authorize access.
    assert prompt_explorer.mount_prompt_explorer(app, transcript_path=str(fixture["source"]), session_id="session-a")
    refreshed = _callback(app, "obs_prompt_refresh").fn(load_run(str(fixture["source"])), {}, "physical")
    assert not any(value in json.dumps(refreshed, default=str) for value in (PRIVATE_A, PRIVATE_B, PRIVATE_PI))
    config = json.dumps(app.get_config_file(), default=str)
    assert not any(value in config for value in (PRIVATE_A, PRIVATE_B, PRIVATE_PI))
    assert str(fixture["root"]) not in config
    assert all(arguments.get("capture_root", values[2] if len(values) > 2 else None) is None
               for values, arguments in calls)


def test_refresh_renders_only_current_authorized_capture_and_clear_removes_it(scoped_capture):
    fixture = scoped_capture
    app = _mounted(fixture)
    run = load_run(str(fixture["source"]))
    refresh = _callback(app, "obs_prompt_refresh").fn
    populated = refresh(run, {}, "physical")
    assert PRIVATE_A in populated[5]
    assert PRIVATE_B not in json.dumps(populated, default=str)
    assert PRIVATE_PI not in json.dumps(populated, default=str)
    cleared = refresh(None, populated[4], "physical")
    assert cleared[0]["records"] == [] and cleared[4] == {"request": None, "component": None}
    assert not any(value in cleared[5] for value in (PRIVATE_A, PRIVATE_B, PRIVATE_PI))


@pytest.mark.parametrize("alias", [False, True])
def test_preloaded_dashboard_loader_preserves_host_scope_including_display_alias(scoped_capture, alias):
    from trajviz_obs.dashboard_cmd import build_preloaded_insight

    fixture = scoped_capture
    display = fixture["source"].with_name("host-display-alias.json") if alias else fixture["source"]
    if alias:
        display.write_bytes(fixture["source"].read_bytes())
    app = build_preloaded_insight(str(fixture["source"]), prompt_session_id="session-a",
                                  prompt_capture_root=fixture["root"],
                                  display_path=str(display) if alias else None)
    load = next(entry for entry in app.fns.values() if getattr(entry.fn, "__name__", "") == "_do_load")
    values = load.fn(str(display), False, "")
    run = next(value for value in values.values() if isinstance(value, Trajectory))
    result = _callback(app, "obs_prompt_refresh").fn(run, {}, "physical")
    assert PRIVATE_A in result[5]
    assert not any(value in json.dumps(result, default=str) for value in (PRIVATE_B, PRIVATE_PI))


def test_picker_values_cannot_read_another_capture_or_interpret_paths(scoped_capture, monkeypatch):
    from trajviz_obs import prompt_explorer

    fixture = scoped_capture
    app = _mounted(fixture)
    run = load_run(str(fixture["source"]))
    state, _lane, _request, _component, selection, _html, _mode = _callback(app, "obs_prompt_refresh").fn(run, {}, "physical")

    def unexpected_read(*_args, **_kwargs):
        pytest.fail("A browser selection attempted to load capture files")

    monkeypatch.setattr(prompt_explorer, "load_dataset", unexpected_read)
    for value in ("record-session-b", "../captures/session-b.json", "/private/not-authorized.json"):
        output = _callback(app, "obs_prompt_request").fn(run, state, selection, "physical", value)
        assert PRIVATE_A in output[5]
        assert not any(secret in json.dumps(output, default=str) for secret in (PRIVATE_B, PRIVATE_PI))
    forged = json.dumps({"kind": "request", "value": "../../private-session"})
    output = _callback(app, "obs_prompt_action").fn(run, state, selection, "physical", forged)
    assert PRIVATE_A in output[5] and PRIVATE_B not in json.dumps(output, default=str)


def test_pending_picker_after_source_clear_cannot_restore_previous_private_content(scoped_capture):
    from agent_trajviz.insight.ui.shared import drop_latest_run, record_latest_run

    fixture = scoped_capture
    app = _mounted(fixture)
    run = load_run(str(fixture["source"]))
    request = SimpleNamespace(session_hash="prompt-explorer-clear-security")
    record_latest_run(request.session_hash, run)
    try:
        populated = _callback(app, "obs_prompt_refresh").fn(run, {}, "physical", request=request)
        state, selection = populated[0], populated[4]
        clear = next(entry.fn for entry in app.fns.values() if getattr(entry.fn, "__name__", "") == "clear_source")
        cleared = clear(request=request)
        assert cleared[0]["records"] == []
        late = _callback(app, "obs_prompt_request").fn(run, state, selection, "physical", selection["request"], request=request)
        assert late[0]["records"] == []
        assert not any(secret in late[5] for secret in (PRIVATE_A, PRIVATE_B, PRIVATE_PI))
    finally:
        drop_latest_run(request.session_hash)


def test_forged_source_after_load_cannot_use_an_old_authorized_dataset(scoped_capture):
    fixture = scoped_capture
    app = _mounted(fixture)
    trusted = load_run(str(fixture["source"]))
    populated = _callback(app, "obs_prompt_refresh").fn(trusted, {}, "physical")
    forged = load_run(str(fixture["forged"]))
    result = _callback(app, "obs_prompt_request").fn(forged, populated[0], populated[4], "physical", populated[4]["request"])
    assert result[0]["records"] == []
    assert not any(secret in result[5] for secret in (PRIVATE_A, PRIVATE_B, PRIVATE_PI))


def test_sensitive_callback_context_is_server_state_not_editable_browser_json(scoped_capture):
    app = _mounted(scoped_capture)
    assert isinstance(_callback(app, "obs_prompt_refresh").inputs[0], gr.State)
    for name in ("obs_prompt_lane", "obs_prompt_request", "obs_prompt_component", "obs_prompt_mode", "obs_prompt_action"):
        entry = _callback(app, name)
        assert all(isinstance(block, gr.State) for block in entry.inputs[:3])


def test_pi_effective_controls_are_hidden_and_reset_for_other_capture_boundaries(scoped_capture):
    from trajviz_obs.prompt_explorer import _render_state

    dataset = _scoped(load_run(str(scoped_capture["source"])), scoped_capture)
    output = _render_state(dataset, {}, "effective")
    assert output[6]["visible"] is False and output[6]["value"] == "physical"
    record = dataset["records"][0]
    record["adapter"] = "pi"
    record["effective_state"] = record["components"]
    output = _render_state(dataset, {}, "effective")
    assert output[6]["visible"] is True and output[6]["value"] == "effective"
    assert "Active Pi system sections" in output[5]


def test_selecting_a_new_snapshot_opens_a_changed_component_first(scoped_capture):
    from trajviz_obs.prompt_explorer import _render_state
    from trajviz_obs.prompt_explorer_render import component_token

    dataset = _scoped(load_run(str(scoped_capture["source"])), scoped_capture)
    before = dataset["records"][0]
    after = {**before, "selection_id": "next-request", "baseline_selection_id": before["selection_id"],
             "comparison_status": "compared", "components": [*before["components"],
                 {"id": "new-result", "content": "new", "kind": "tool_result", "position": 1, "utf8_bytes": 5}],
             "changes": {"added": ["new-result"], "modified": [], "removed": [], "moved": [],
                         "unchanged": [before["components"][0]["id"]]}}
    dataset["records"].append(after)
    output = _render_state(dataset, {"request": "next-request"}, "physical")
    assert output[4]["component"] == component_token("new-result")
    assert "Changes from baseline" in output[5]


@pytest.mark.parametrize("action", ["refresh", "request"])
def test_source_clear_during_component_render_cannot_publish_stale_private_content(scoped_capture, monkeypatch, action):
    from agent_trajviz.insight.ui.shared import drop_latest_run, record_latest_run

    from trajviz_obs import prompt_explorer

    fixture = scoped_capture
    app = _mounted(fixture)
    run = load_run(str(fixture["source"]))
    request = SimpleNamespace(session_hash="prompt-explorer-render-clear-race")
    record_latest_run(request.session_hash, run)
    populated = _callback(app, "obs_prompt_refresh").fn(run, {}, "physical", request=request)
    render = prompt_explorer.render_explorer

    def clear_during_render(*args, **kwargs):
        drop_latest_run(request.session_hash)
        return render(*args, **kwargs)

    monkeypatch.setattr(prompt_explorer, "render_explorer", clear_during_render)
    try:
        if action == "refresh":
            result = _callback(app, "obs_prompt_refresh").fn(run, populated[4], "physical", request=request)
        else:
            result = _callback(app, "obs_prompt_request").fn(run, populated[0], populated[4], "physical",
                                                           populated[4]["request"], request=request)
        assert PRIVATE_A not in json.dumps(result, default=str)
    finally:
        drop_latest_run(request.session_hash)
