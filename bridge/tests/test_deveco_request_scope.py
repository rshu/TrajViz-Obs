"""Private request views are a pinned host capability, never an import capability."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import agent_trajviz as tv
import jsonschema
import pytest
from agent_trajviz.insight import prompt_construction as engine
from agent_trajviz.insight.session import session_from_run
from test_deveco import message, native, write
from test_deveco_requests import captured, journal, pending_capture, process_rows, run_at

from trajviz_obs.compat import load_run
from trajviz_obs.dashboard_cmd import build_preloaded_insight
from trajviz_obs.deveco_request_scope import bind_request_scope, captured_view, inherit_request_scope
from trajviz_obs.errors import BridgeError
from trajviz_obs.native_quality import project_dashboard_session
from trajviz_obs.views_cmd import export

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Private request evidence requires POSIX")
SECRET = "PRIVATE_REQUEST_ONLY"


def projected(run):
    return project_dashboard_session(session_from_run(run, str(run.source))).shown


def loaded(app, source):
    loader = next(block for block in app.fns.values() if getattr(block.fn, "__name__", "") == "_do_load")
    values = loader.fn(str(source), False, "")
    return next(value for value in values.values() if isinstance(value, tv.Trajectory))


def test_actual_captured_view_conforms_to_schema_and_engine_stays_native(tmp_path):
    run = run_at(tmp_path)
    root, *_ = captured(tmp_path)
    before = copy.deepcopy(run.to_dict(include_raw=True))
    original_engine = engine.inspect_prompt_construction
    bind_request_scope(run, root, "root")
    view = captured_view(run, "prompt_construction")
    assert isinstance(view, tv.View)
    jsonschema.validate(view.to_dict(), tv.view_schemas()["prompt_construction"])
    assert view.data["selected_request"]["evidence"] == "captured"
    assert view.data["selected_request"]["step_index"] == 1
    assert engine.inspect_prompt_construction is original_engine
    assert run.view("prompt_construction").data["selected_request"]["evidence"] == "estimated"
    assert captured_view(run, "health") is None
    assert run.to_dict(include_raw=True) == before


@pytest.mark.parametrize("root,sid", [(None, "root"), (Path("relative"), "root"), (Path("/private/tmp"), None),
                                     (Path("/private/tmp"), "foreign"), (Path("/private/tmp/../capture"), "root"),
                                     (Path("/private/tmp/capture\0invalid"), "root")])
def test_scope_requires_explicit_absolute_root_and_same_native_session(tmp_path, root, sid):
    run = run_at(tmp_path)
    with pytest.raises(BridgeError) as exc:
        bind_request_scope(run, root, sid)
    assert exc.value.code == "request_capture_scope"
    assert captured_view(run, "prompt_construction") is None


def test_projection_inherits_host_scope_and_latest_title_stays_unlinked(tmp_path):
    run = run_at(tmp_path)
    root, assistant, *_ = captured(tmp_path, observed_at="2026-11-26T10:00:01Z")
    _, title, *_ = captured(tmp_path, purpose="title", aid=None, observed_at="2026-11-26T10:00:02Z")
    bind_request_scope(run, root, "root")
    shown = projected(run)
    data = shown.view("prompt_construction").data
    assert data["selected_request"]["id"] == title["request_id"]
    assert data["selected_request"]["step_index"] is None
    assert data["selected_request"]["purpose"] == "title"
    selected = shown.view("prompt_construction", request_id=assistant["request_id"]).data["selected_request"]
    assert selected["step_index"] == 1
    assert shown.raw == run.raw and shown.steps == run.steps
    assert SECRET not in json.dumps(shown.to_dict(include_raw=True))


def test_each_view_refresh_resolves_late_native_trace_evidence(tmp_path):
    run = run_at(tmp_path)
    root, *_ = pending_capture(tmp_path)
    bind_request_scope(run, root, "root")
    shown = projected(run)
    first = shown.view("prompt_construction").data["selected_request"]
    assert first["step_index"] is None
    journal(root, list(reversed(process_rows())))
    second = shown.view("prompt_construction").data["selected_request"]
    assert second["step_index"] == 1
    assert second["native_mapping"]["basis"] == "native_processor_span"
    assert first["serialized_body"] == second["serialized_body"]
    # Already-returned evidence remains a snapshot rather than changing in place.
    assert first["step_index"] is None


def test_dashboard_loader_scopes_only_pinned_file_even_for_forged_native_identity(tmp_path):
    run = run_at(tmp_path)
    source = tmp_path / "deveco.json"
    root, *_ = captured(tmp_path)
    forged = tmp_path / "uploaded-copy.json"
    forged.write_bytes(source.read_bytes())
    before = copy.deepcopy(run.to_dict(include_raw=True))
    original_engine = engine.inspect_prompt_construction
    app = build_preloaded_insight(str(source), request_capture_root=root, request_session_id="root")
    shown = loaded(app, source)
    assert shown.view("prompt_construction").data["selected_request"]["evidence"] == "captured"
    uploaded = loaded(app, forged)
    uploaded.raw["_source_path"] = str(source)
    assert uploaded.view("prompt_construction").data["selected_request"]["evidence"] == "estimated"
    assert SECRET not in json.dumps(uploaded.to_dict(include_raw=True))
    assert captured_view(uploaded, "prompt_construction") is None
    assert engine.inspect_prompt_construction is original_engine
    assert run.to_dict(include_raw=True) == before
    # Another app does not inherit the first app's private capability.
    other = build_preloaded_insight(str(source))
    assert loaded(other, source).view("prompt_construction").data["selected_request"]["evidence"] == "estimated"
    config = json.dumps(app.get_config_file(), default=str)
    assert str(root) not in config and SECRET not in config


def test_foreign_native_upload_does_not_reuse_scope(tmp_path):
    run = run_at(tmp_path)
    source = tmp_path / "deveco.json"
    root, *_ = captured(tmp_path)
    captured(tmp_path, sid="foreign", aid="a", body={"model": "x", "messages": [{"role": "user", "content": "FOREIGN_PRIVATE"}]})
    foreign = write(tmp_path, native("foreign", messages=[message(mid="u", sid="foreign", role="user"),
                                                         message(mid="a", sid="foreign")]), "foreign.json")
    app = build_preloaded_insight(str(source), request_capture_root=root, request_session_id="root")
    uploaded = loaded(app, foreign)
    assert captured_view(uploaded, "prompt_construction") is None
    assert "FOREIGN_PRIVATE" not in json.dumps(uploaded.view("prompt_construction").to_dict())
    assert run.format == "deveco"


def test_ordinary_export_of_scoped_dashboard_keeps_private_request_bodies_out(tmp_path):
    run = run_at(tmp_path)
    root, *_ = captured(tmp_path)
    bind_request_scope(run, root, "root")
    shown = projected(run)
    assert SECRET in json.dumps(shown.view("prompt_construction").to_dict())
    out = tmp_path / "ordinary-export"
    export(shown, out, only=["prompt_construction"], with_trajectory=True)
    for name in ("trajectory.json", "prompt_construction.json", "index.json"):
        assert SECRET not in (out / name).read_text()
        assert str(root) not in (out / name).read_text()
    assert load_run(str(out / "trajectory.json")).view("prompt_construction").data["selected_request"]["evidence"] == "estimated"


def test_cli_explicit_view_capture_and_ordinary_json_export_are_separate(tmp_path, run_cli):
    run_at(tmp_path)
    source = tmp_path / "deveco.json"
    root, *_ = captured(tmp_path)
    artifacts = tmp_path / "view-artifacts"
    code, result = run_cli(["view", "prompt_construction", "--transcript", str(source), "--artifacts", str(artifacts),
                           "--request-capture-root", str(root), "--request-session-id", "root", "--json"])
    assert code == 0
    written = json.loads((artifacts / "views/prompt_construction.json").read_text())
    assert written["data"]["selected_request"]["evidence"] == "captured"
    assert SECRET in json.dumps(written)
    assert result["inline"]["data"]["selected_request"]["step_index"] == 1
    out = tmp_path / "json-export"
    code, _ = run_cli(["json", "--transcript", str(source), "--view", "prompt_construction", "--out", str(out)])
    assert code == 0 and SECRET not in (out / "prompt_construction.json").read_text()
    assert SECRET not in (out / "trajectory.json").read_text()
    code, error = run_cli(["view", "prompt_construction", "--transcript", str(source), "--artifacts", str(root),
                          "--request-capture-root", str(root), "--request-session-id", "root"])
    assert code != 0 and error["error"]["code"] == "request_capture_scope"


def test_scope_cannot_be_inherited_by_an_unrelated_source(tmp_path):
    run = run_at(tmp_path)
    root, *_ = captured(tmp_path)
    bind_request_scope(run, root, "root")
    foreign = load_run(str(write(tmp_path, native("foreign", messages=[message(mid="u", sid="foreign", role="user"),
        message(mid="a", sid="foreign")]), "foreign.json")))
    with pytest.raises(BridgeError):
        inherit_request_scope(run, foreign)
    assert captured_view(foreign, "prompt_construction") is None
