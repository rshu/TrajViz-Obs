"""Captured-body UI stays truthful without changing native TrajViz globals."""
from __future__ import annotations

import copy
import inspect
from types import SimpleNamespace

import pytest
from agent_trajviz import dashboard
from agent_trajviz.insight.ui import prompt_tab

from trajviz_obs.deveco_request_ui import _presenters, _render_binding, install
from trajviz_obs.errors import BridgeError


def detail(**extra):
    return {"id": "request-2", "request_id": "request-2", "scope": "recorded_request", "evidence": "captured",
            "session_id": "ses_probe", "model": "test-model", "step_index": 2, "purpose": "assistant",
            "assistant_message_id": "msg_native_owner", "body_bytes": 12345, "body_sha256": "a" * 64,
            "coverage": {"complete": True}, "dispatch_status": "complete", "components": [], "messages": [], "tools": [],
            "evidence_quality": {"content_complete": True, "outbound_body_observed": True, "body_sha256_verified": True},
            "native_mapping": {"status": "verified", "basis": "native_processor_span"},
            "changes": {"available": True, "previous_request_id": "request-1", "comparison_request_id": "request-1",
                        "basis": "previous_verified_native_assistant_request", "lineage_verified": False,
                        "added": ["message-1"], "added_components": [{"kind": "message", "label": "user 1", "role": "user"}],
                        "removed": [], "retained": [], "rewritten_components": []}, **extra}


class Run:
    format = "deveco"
    def __init__(self, selected=None):
        self.steps = [{"index": 1}, {"index": 2}]
        self.detail = selected or detail()

    def view(self, name, **kwargs):
        assert name == "prompt_construction"
        return SimpleNamespace(data={"format": self.format, "requests": [self.detail], "selected_request": self.detail,
                                     "evolution": []})


def test_captured_summary_shows_exact_body_and_native_ownership_with_workflow_link():
    summary, _ = _presenters(prompt_tab)
    source = detail()
    original = copy.deepcopy(source)
    rendered = summary(source, Run())
    assert "Complete serialized HTTP body captured" in rendered
    assert "已捕获完整序列化 HTTP 请求体" in rendered
    assert "12345" in rendered and "a" * 64 in rendered
    assert "request-2" in rendered and "msg_native_owner" in rendered
    assert "Verified native assistant step" in rendered
    assert "href='#tv-step-2'" in rendered
    assert "Recorded components recovered." not in rendered
    assert source == original


@pytest.mark.parametrize("state,reason,expected", [("unlinked", "processor_trace_pending_or_conflicting", "Pending native ownership"),
                                                  ("unlinked", "processor_session_mismatch", "Unlinked request")])
def test_pending_or_unlinked_body_remains_visible_without_a_step_link(state, reason, expected):
    summary, _ = _presenters(prompt_tab)
    selected = detail(native_mapping={"status": state, "reason": reason}, step_index=None, assistant_message_id=None)
    rendered = summary(selected, Run(selected))
    assert expected in rendered and "12345" in rendered
    assert "#tv-step-" not in rendered and "later refresh" in rendered


def test_native_order_comparison_preserves_core_text_diff_and_does_not_claim_lineage(monkeypatch):
    monkeypatch.setattr(prompt_tab.prompt_text, "render_text_evolution", lambda selected: "<mark>CORE-TEXT-DIFF</mark>")
    _, changes = _presenters(prompt_tab)
    selected = detail()
    original = copy.deepcopy(selected)
    rendered = changes(selected)
    assert "previous native assistant step" in rendered
    assert "Native step order supplies the comparison baseline" in rendered
    assert "原生步骤顺序" in rendered
    assert "same recorded lineage" not in rendered
    assert "No prompt assembly events were recorded" not in rendered
    assert "CORE-TEXT-DIFF" in rendered and "Added components" in rendered and "request-1" in rendered
    assert selected == original


def test_unavailable_comparison_and_auxiliary_title_use_body_provenance():
    summary, changes = _presenters(prompt_tab)
    selected = detail(purpose="title", step_index=None, assistant_message_id=None,
                      native_mapping={"status": "unlinked"}, changes={"available": False, "reason": "No previous request on a recorded matching lineage is available."})
    assert "Auxiliary title request" in summary(selected, Run(selected))
    rendered = changes(selected)
    assert "No comparable captured request" in rendered
    assert "No previous request with verified lineage" not in rendered
    assert "recorded matching lineage" not in rendered
    assert "No prompt assembly events were recorded" not in rendered


def test_non_deveco_and_uncaptured_details_keep_native_rendering_unchanged():
    render = _render_binding(prompt_tab)
    other = Run()
    other.format = "opencode"
    assert render(other) == prompt_tab.outputs(other)
    estimated = Run(detail(scope="trajectory_estimate", evidence_quality={"content_complete": True}))
    assert render(estimated) == prompt_tab.outputs(estimated)


def test_install_scopes_cloned_callbacks_to_one_app_preserving_core_globals_and_other_callbacks():
    app, untouched = dashboard.insight_app(), dashboard.insight_app()
    before = {key: entry.fn for key, entry in app.fns.items()}
    other = {key: entry.fn for key, entry in untouched.fns.items()}
    outputs = prompt_tab.outputs
    install(app)
    changed = []
    for key, entry in app.fns.items():
        if entry.fn is not before[key]:
            changed.append(entry)
            assert inspect.unwrap(entry.fn) is before[key]
        else:
            assert entry.fn is before[key]
    assert len(changed) == 6
    assert all(entry.fn is other[key] for key, entry in untouched.fns.items())
    assert prompt_tab.outputs is outputs
    callback = next(entry.fn for entry in changed if entry.fn.__name__ == "on_prompt_load")
    show = inspect.getclosurevars(callback).nonlocals["show"]
    render = show.__globals__["outputs"]
    assert "Complete serialized HTTP body captured" in render(Run())[2]
    install(app)
    assert show.__globals__["outputs"] is render


def test_install_fails_closed_when_core_version_or_callback_shape_changes(monkeypatch):
    import agent_trajviz
    app = dashboard.insight_app()
    before = [entry.fn for entry in app.fns.values()]
    monkeypatch.setattr(agent_trajviz, "__version__", "0.7.2")
    with pytest.raises(BridgeError, match="render seam changed"):
        install(app)
    assert [entry.fn for entry in app.fns.values()] == before
