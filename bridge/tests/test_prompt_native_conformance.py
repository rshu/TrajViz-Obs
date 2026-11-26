"""Native 0.7 prompt evidence preserves estimates, branch scope and sidecar isolation."""
from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from agent_trajviz import LoadError
from test_deveco import BASE, message, native, task, write
from test_opencode import assistant as oc_assistant
from test_opencode import native as oc_native
from test_pi import capture, entries
from test_pi import write as write_pi

from trajviz_obs.compat import load_run


def at(mid, role, start, end=None, text="", parts=None):
    value = message(mid=mid, role=role, completed=end, parts=parts or [{"type": "text", "text": text}])
    value["info"]["time"]["created"] = BASE + start
    return value


def selected_for(run, mid):
    view = run.view("prompt_construction").data
    index = next(step["index"] for step in run.steps if step["id"] == mid)
    row = next(row for row in view["requests"] if row["step_index"] == index)
    return run.view("prompt_construction", request_id=row["id"]).data["selected_request"]


@pytest.mark.skipif(os.name != "posix", reason="Private prompt capture requires POSIX")
@pytest.mark.parametrize("format_name", ["deveco", "opencode"])
def test_native_fallback_compares_only_estimated_candidates_and_ignores_capture_sidecars(tmp_path, monkeypatch, format_name):
    import prompt_capture

    if format_name == "deveco":
        payload = native(messages=[at("u", "user", 10, text="Original instruction"),
            at("a1", "assistant", 20, 30, "First result"),
            at("feedback", "user", 40, text="Explicit corrective feedback"),
            at("a2", "assistant", 50, 60, "Revised result")])
        latest = "a2"
    else:
        payload = oc_native(messages=[{"id": "u", "type": "user", "text": "Original instruction", "time": {"created": BASE + 10}},
            oc_assistant("a1", created=20, completed=30),
            {"id": "feedback", "type": "user", "text": "Explicit corrective feedback", "time": {"created": BASE + 40}},
            oc_assistant("a2", created=50, completed=60)])
        latest = "ses_root:a2"
    source = write(tmp_path, payload)
    before = source.read_bytes()
    capture_root = tmp_path / "private-capture"
    capture_path = prompt_capture.write_record(str(capture_root), {
        "schema_version": 1, "adapter": format_name, "session_id": payload["session"]["id"],
        "instance_id": "writer", "record_id": str(uuid4()), "sequence": 1,
        "observed_at": "2026-11-25T00:00:01Z", "stage": "component.system", "kind": "independent",
        "correlation": {"pairing": "independent"}, "data": {"system": ["PRIVATE_SIDECAR_ONLY"]}})
    capture_bytes = Path(capture_path).read_bytes()
    monkeypatch.setenv("TRAJVIZ_OBS_PROMPT_CAPTURE", "1")
    monkeypatch.setenv("TRAJVIZ_OBS_PROMPT_CAPTURE_DIR", str(capture_root))
    run = load_run(str(source))
    detail = selected_for(run, latest)
    assert detail["evidence"] == "estimated" and detail["scope"] == "trajectory_estimate"
    changes = detail["changes"]
    assert changes["available"] is True and changes["lineage_verified"] is False
    assert changes["evidence"] == "estimated" and changes["basis"] == "adjacent_transcript_candidates"
    assert changes["previous_candidate_id"] and changes["rewritten_components"] == []
    assert "Explicit corrective feedback" in json.dumps(detail["messages"])
    assert detail["tools"] == [] and detail["parameters"] == {}
    assert detail["composition"]["evidence"] == "estimated"
    assert detail["text_evolution"]["comparison_basis"] == "adjacent_transcript_candidates"
    exported = run.to_dict(include_raw=True)
    assert "PRIVATE_SIDECAR_ONLY" not in json.dumps([detail, run.raw, exported])
    assert str(capture_root) not in json.dumps(exported)
    restored = load_run(str(write(tmp_path, exported, "portable.json")))
    assert restored.view("prompt_construction").to_dict() == run.view("prompt_construction").to_dict()
    assert source.read_bytes() == before
    assert Path(capture_path).read_bytes() == capture_bytes


def test_estimates_exclude_child_context_and_unfinished_response_or_tool_output(tmp_path):
    late = {"type": "tool", "id": "late", "callID": "late-call", "tool": "read",
        "state": {"status": "completed", "input": {"path": "controlled.txt"}, "output": "TOO_LATE_RESULT",
                  "time": {"start": BASE + 210, "end": BASE + 700}}}
    child = native("child", parent="root", messages=[at("child-user", "user", 240, text="CHILD_PRIVATE_CONTEXT"),
        message(mid="child-answer", sid="child", parts=[{"type": "text", "text": "CHILD_PRIVATE_RESPONSE"}])])
    child["messages"][0]["info"]["sessionID"] = "child"
    payload = native(messages=[at("u", "user", 100, text="Root instruction"),
        at("a1", "assistant", 200, 750, parts=[{"type": "text", "text": "UNFINISHED_RESPONSE"}, late, task("child")]),
        at("a2", "assistant", 500, 600, "Parallel boundary"), at("a3", "assistant", 800, 900, "Later boundary")], children=[child])
    run = load_run(str(write(tmp_path, payload)))
    early = selected_for(run, "a2")
    early_text = json.dumps(early["messages"])
    assert "TOO_LATE_RESULT" not in early_text and "UNFINISHED_RESPONSE" not in early_text
    assert "CHILD_PRIVATE_CONTEXT" not in early_text and "CHILD_PRIVATE_RESPONSE" not in early_text
    assert any("proven completion" in note for note in early["limitations"])
    later = selected_for(run, "a3")
    assert "TOO_LATE_RESULT" in json.dumps(later["messages"])
    assert "UNFINISHED_RESPONSE" in json.dumps(later["messages"])


def test_missing_native_usage_is_not_observed_prompt_occupancy(tmp_path):
    payload = native(messages=[at("u", "user", 10, text="Readable recorded candidate"),
                               at("a", "assistant", 20, 30, "Answer")])
    run = load_run(str(write(tmp_path, payload)))
    data = run.view("prompt_construction").data
    assert data["evolution"][-1]["observed_prompt_tokens"] is None
    assert data["evolution"][-1]["observed_prompt_basis"] is None
    assert data["selected_request"]["token_estimate"]["evidence"] == "estimated"
    assert "Total Tokens" in run.steps[-1]["_metrics_unavailable_fields"]


def test_native_pi_prompt_state_follows_live_leaf_with_original_source_indices(tmp_path):
    rows = capture(entries(), leaf_id="abandoned")
    source = write_pi(tmp_path, rows)
    run = load_run(str(source))
    view = run.view("prompt_construction").data
    assert len(view["requests"]) == 1
    assert ":abandoned:2" in view["requests"][0]["id"]
    assert view["selected_request"]["scope"] == "native_prompt_state"
    assert view["selected_request"]["evidence"] == "reconstructed"
    assert view["selected_request"]["message_refs"][0]["raw_index"] == 1
    assert run.raw["_pi_entries"] == rows
    assert [step["raw_index"] for step in run.steps] == [1, 2]
    assert run.raw["metadata"]["pi_active_leaf_id"] == "abandoned"
    assert run.raw["metadata"]["pi_excluded_entry_count"] == 1
    with pytest.raises(ValueError):
        run.view("prompt_construction", request_id="unknown")
    assert run.view("prompt_construction").to_dict() == run.view("prompt_construction").to_dict()
    exported = write(tmp_path, run.to_dict(include_raw=True), "portable.json")
    assert load_run(str(exported)).view("prompt_construction").to_dict() == run.view("prompt_construction").to_dict()


def test_pi_progress_stream_is_not_mistaken_for_a_native_parent_tree(tmp_path):
    rows = [{"type": "session", "version": 3, "id": "controlled-stream",
             "timestamp": "2026-11-25T00:00:00Z"},
            {"type": "message_start"}, {"type": "message_update"},
            {"type": "message_end", "message": {"role": "assistant",
                "content": [{"type": "text", "text": "Final recorded answer"}], "model": "controlled",
                "timestamp": 1795564801000, "stopReason": "stop",
                "usage": {"input": 1, "output": 1, "reasoning": 0, "cacheRead": 0,
                          "cacheWrite": 0, "totalTokens": 2}}}]
    run = load_run(str(write_pi(tmp_path, rows)))
    assert len(run.steps) == 1 and run.metrics["tokens"]["total"] == 2
    assert run.raw["_pi_entries"] == rows
    assert run.raw["metadata"]["history_scope"] == "event_stream"
    assert not any(str(step.get("id", "")).startswith("_trajviz_obs_pi_source_") for step in run.steps)
    data = run.view("prompt_construction").data
    assert data["available"] is True and len(data["requests"]) == 1
    assert data["selected_request"]["scope"] == "native_prompt_state"
    assert data["selected_request"]["evidence"] == "reconstructed"
    assert run.steps[0]["raw_index"] == 3
    assert data["selected_request"]["id"].endswith(":3:3")
    assert data["selected_request"]["step_index"] == run.steps[0]["index"]
    assert data["selected_request"]["step_index_basis"] == "native_source_index"
    assert data["evolution"][0]["step_index"] == run.steps[0]["index"]
    exported = write(tmp_path, run.to_dict(include_raw=True), "stream-export.json")
    restored = load_run(str(exported))
    assert restored.steps[0]["raw_index"] == 3
    assert restored.view("prompt_construction").to_dict() == run.view("prompt_construction").to_dict()


@pytest.mark.parametrize("mixed", ["native-message", "appended-parent", "captured-leaf"])
def test_progress_markers_do_not_override_native_branch_ambiguity(tmp_path, mixed):
    rows = [{"type": "session", "version": 3, "id": "controlled-stream"},
            {"type": "message_start"}, {"type": "message_end", "message": {
                "role": "assistant", "content": [{"type": "text", "text": "Answer"}],
                "timestamp": 1795564801000, "stopReason": "stop"}}]
    if mixed == "native-message":
        rows.append({"type": "message", "id": "native", "parentId": "missing",
                     "message": {"role": "user", "content": "Native branch input"}})
    elif mixed == "appended-parent":
        rows.append({"type": "entry_appended", "entry": {"type": "message", "id": "native",
                     "parentId": "missing", "message": {"role": "user", "content": "Native branch input"}}})
    else:
        rows[0]["_trajviz_obs_pi"] = {"schema_version": 1, "leaf_id": "native"}
    path = write_pi(tmp_path, rows)
    before = path.read_bytes()
    with pytest.raises(LoadError) as caught:
        load_run(str(path))
    assert getattr(caught.value, "code", None) == "invalid_pi_branch"
    assert path.read_bytes() == before
