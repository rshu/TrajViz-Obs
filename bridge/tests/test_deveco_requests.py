"""Exact DevEco requests remain private, source-owned, and attempt-scoped."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from test_deveco import message, native, write

from trajviz_obs.compat import load_run
from trajviz_obs.deveco_requests import RequestCaptureError, inspect_prompt_construction, load_request_evidence

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Private request evidence requires POSIX")


def run_at(tmp_path, sid="root"):
    return load_run(str(write(tmp_path, native(sid, messages=[message(mid="u", sid=sid, role="user"),
        message(mid="a", sid=sid, tokens={"input": 200, "output": 3, "total": 203})]))))


def captured(tmp_path, *, sid="root", aid="a", purpose="assistant", invocation=None, body=None, **updates):
    root = tmp_path / "private-requests"
    root.mkdir(mode=0o700, exist_ok=True)
    directory = root / hashlib.sha256(sid.encode()).hexdigest()
    directory.mkdir(mode=0o700, exist_ok=True)
    rid = str(uuid4())
    raw = body if isinstance(body, bytes) else json.dumps(body or {
        "model": "controlled-model", "messages": [
            {"role": "system", "content": "System instruction"},
            {"role": "user", "content": "PRIVATE_REQUEST_ONLY"}],
        "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}],
        "temperature": 0, "stream": True}, ensure_ascii=False, indent=2).encode()
    meta = {"schema_version": 1, "type": "deveco_request", "adapter": "deveco", "request_id": rid,
        "invocation_id": invocation or str(uuid4()), "session_id": sid, "user_message_id": "u",
        "assistant_message_id": aid, "purpose": purpose, "model_id": "controlled-model", "agent_id": "build",
        "body_ref": f"{rid}.body", "body_sha256": hashlib.sha256(raw).hexdigest(), "body_bytes": len(raw),
        "coverage": {"complete": True, "omitted_fields": [], "redacted_fields": []},
        "mapping": {"status": "provided", "basis": "host_processor_context"},
        "observed_at": "2026-11-26T10:00:01Z", "status": 200, **updates}
    body_path, meta_path = directory / meta["body_ref"], directory / f"{rid}.json"
    body_path.write_bytes(raw)
    body_path.chmod(0o600)
    meta_path.write_text(json.dumps(meta))
    meta_path.chmod(0o600)
    return root, meta, meta_path, body_path, raw


def view(run, root, rid=None):
    evidence = load_request_evidence(root, native_raw=run.raw, steps=run.steps)
    return inspect_prompt_construction(run.raw, run.steps, rid, evidence=evidence)


def test_complete_outbound_body_links_legacy_native_id_without_polluting_export(tmp_path):
    run = run_at(tmp_path)
    root, meta, _, _, raw = captured(tmp_path)
    before = json.dumps(run.to_dict(include_raw=True), sort_keys=True)
    data = view(run, root)
    selected = data["selected_request"]
    assert selected["evidence"] == "captured" and selected["scope"] == "recorded_request"
    assert selected["step_index"] == 1 and selected["assistant_message_id"] == "a"
    assert selected["native_mapping"]["status"] == "verified"
    assert selected["serialized_body"].encode() == raw
    assert selected["body_sha256"] == hashlib.sha256(raw).hexdigest()
    assert selected["request_body"] == json.loads(raw)
    assert selected["system"] == [{"role": "system", "content": "System instruction"}]
    assert selected["messages"] == [{"role": "user", "content": "PRIVATE_REQUEST_ONLY"}]
    assert selected["tools"] == json.loads(raw)["tools"]
    assert selected["parameters"] == {"model": "controlled-model", "temperature": 0, "stream": True}
    assert selected["evidence_quality"]["outbound_body_observed"] is True
    assert selected["evidence_quality"]["estimation"]["provider_request_verified"] is False
    assert selected["token_estimate"]["evidence"] == "estimated"
    assert "PRIVATE_REQUEST_ONLY" not in before
    assert json.dumps(run.to_dict(include_raw=True), sort_keys=True) == before
    assert str(root) not in json.dumps(data)
    assert "serialized_body" not in data["requests"][0]
    assert data["requests"][0]["id"] == meta["request_id"]
    assert data["evolution"][0]["observed_prompt_tokens"] is None


@pytest.mark.parametrize("updates", [
    {"assistant_message_id": "unknown"}, {"assistant_message_id": "u"}, {"assistant_message_id": None},
    {"user_message_id": "unknown"}, {"purpose": "title"}, {"purpose": "auxiliary"},
    {"mapping": {"status": "provided", "basis": "adjacent_timestamp"}},
    {"mapping": {"status": "unlinked", "basis": "absent"}},
])
def test_helper_or_unproven_identity_is_visible_without_native_step_link(tmp_path, updates):
    run = run_at(tmp_path)
    root, *_ = captured(tmp_path, **updates)
    selected = view(run, root)["selected_request"]
    assert selected["evidence"] == "captured"
    assert selected["step_index"] is None
    assert selected["native_mapping"]["status"] == "unlinked"
    assert selected["sources"][0]["step_index"] is None


def test_cross_session_records_are_not_read_or_joined(tmp_path):
    run = run_at(tmp_path)
    root, *_ = captured(tmp_path, sid="foreign", body={"model": "x", "messages": []})
    foreign = root / hashlib.sha256(b"foreign").hexdigest()
    # If traversed, this unsafe sidecar would fail the read. The foreign namespace is not opened.
    next(foreign.glob("*.json")).chmod(0o644)
    assert view(run, root) is None
    captured(tmp_path, aid="foreign:a")
    assert view(run, root)["selected_request"]["step_index"] is None


def test_retries_keep_physical_ids_and_never_duplicate_assistant_usage(tmp_path):
    run = run_at(tmp_path)
    invocation = str(uuid4())
    root, one, *_ = captured(tmp_path, invocation=invocation, status=429,
        observed_at="2026-11-26T10:00:01Z")
    _, two, *_ = captured(tmp_path, invocation=invocation, observed_at="2026-11-26T10:00:02Z")
    data = view(run, root)
    assert [row["id"] for row in data["requests"]] == [one["request_id"], two["request_id"]]
    assert {row["invocation_id"] for row in data["requests"]} == {invocation}
    assert [row["step_index"] for row in data["requests"]] == [1, 1]
    assert all(row["observed_prompt_tokens"] is None for row in data["evolution"])
    assert data["selected_request"]["changes"]["available"] is False
    # An explicit host predecessor enables comparison, rather than timestamp proximity.
    meta_path = root / hashlib.sha256(b"root").hexdigest() / f"{two['request_id']}.json"
    meta = json.loads(meta_path.read_text())
    meta["previous_request_id"] = one["request_id"]
    meta_path.write_text(json.dumps(meta))
    assert view(run, root)["selected_request"]["changes"]["available"] is True


@pytest.mark.parametrize("mutation,reason", [
    ({"body_sha256": "0" * 64}, "request_body_integrity"),
    ({"body_bytes": 1}, "request_body_integrity"),
    ({"coverage": {"complete": True, "omitted_fields": [], "redacted_fields": ["messages"]}}, "incomplete_request_body"),
    ({"body_ref": "../outside.body"}, "invalid_request_record"),
    ({"schema_version": 2}, "invalid_request_record"),
    ({"session_id": "foreign"}, "invalid_request_record"),
])
def test_invalid_integrity_coverage_and_namespace_fail_closed(tmp_path, mutation, reason):
    run = run_at(tmp_path)
    root, meta, meta_path, *_ = captured(tmp_path)
    meta.update(mutation)
    meta_path.write_text(json.dumps(meta))
    with pytest.raises(RequestCaptureError, match=reason):
        view(run, root)


@pytest.mark.parametrize("kind", ["body_link", "metadata_link", "hardlink", "public_file", "public_directory"])
def test_private_reader_rejects_unsafe_storage(tmp_path, kind):
    run = run_at(tmp_path)
    root, _, meta_path, body_path, raw = captured(tmp_path)
    if kind in {"body_link", "metadata_link"}:
        target = body_path if kind == "body_link" else meta_path
        outside = tmp_path / "outside"
        target.rename(outside)
        target.symlink_to(outside)
    elif kind == "hardlink":
        os.link(body_path, tmp_path / "other-link")
    elif kind == "public_file":
        body_path.chmod(0o644)
    else:
        root.chmod(0o755)
    with pytest.raises(RequestCaptureError):
        view(run, root)
    if kind != "metadata_link":
        assert raw


def test_bound_evidence_cannot_be_reused_for_another_native_source(tmp_path):
    run = run_at(tmp_path)
    root, *_ = captured(tmp_path)
    evidence = load_request_evidence(root, native_raw=run.raw, steps=run.steps)
    foreign = run_at(tmp_path, sid="foreign")
    with pytest.raises(RequestCaptureError, match="request_source_binding_changed"):
        inspect_prompt_construction(foreign.raw, foreign.steps, evidence=evidence)
    # Duplicate normalized step identities do not silently pick one matching index.
    steps = [*run.steps, dict(run.steps[-1])]
    evidence = load_request_evidence(root, native_raw=run.raw, steps=steps)
    assert inspect_prompt_construction(run.raw, steps, evidence=evidence)["selected_request"]["step_index"] is None


def test_capture_reader_is_explicit_and_does_not_spoof_native_formats(tmp_path, monkeypatch):
    run = run_at(tmp_path)
    root, *_ = captured(tmp_path)
    monkeypatch.setenv("TRAJVIZ_OBS_DEVECO_REQUEST_DIR", str(root))
    before = run.view("prompt_construction").data
    assert before["selected_request"]["evidence"] == "estimated"
    assert "PRIVATE_REQUEST_ONLY" not in json.dumps(before)
    with pytest.raises(RequestCaptureError, match="unsupported_native_source"):
        load_request_evidence(root, native_raw={"_claude_prompt_records": []}, steps=run.steps)
    with pytest.raises(ValueError, match="Unknown prompt construction request_id"):
        view(run, root, "not-a-request")
    with pytest.raises(TypeError):
        view(run, root, 1)


def test_complete_view_conforms_to_core_schema(tmp_path):
    import agent_trajviz
    import jsonschema
    run = run_at(tmp_path)
    root, *_ = captured(tmp_path)
    payload = run.view("prompt_construction").to_dict()
    payload["data"] = view(run, root)
    schema = json.loads((Path(agent_trajviz.__file__).parent / "schemas/prompt_construction.schema.json").read_text())
    jsonschema.validate(payload, schema)


TRACE = "1" * 32
TRIGGER = "2" * 16
PROCESSOR = "3" * 16
OUTER = "4" * 16


def journal(root, rows):
    target = root / "trace-spans.jsonl"
    target.write_text("".join(json.dumps({"schema_version": 1, **row}) + "\n" for row in rows))
    target.chmod(0o600)
    return target


def process_rows(*, sid="root", aid="a", name="SessionProcessor.process"):
    return [{"type": "span", "trace_id": TRACE, "span_id": TRIGGER,
             "parent_span_id": PROCESSOR, "name": "Plugin.trigger"},
            {"type": "span", "trace_id": TRACE, "span_id": PROCESSOR,
             "parent_span_id": OUTER, "name": name},
            {"type": "processor_identity", "trace_id": TRACE, "span_id": PROCESSOR,
             "session_id": sid, "assistant_message_id": aid}]


def pending_capture(tmp_path):
    return captured(tmp_path, aid=None, mapping={"status": "pending", "basis": "native_processor_span"},
                    trace_id=TRACE, span_id=TRIGGER)


def test_trace_parent_identity_links_after_export_arrives_without_time_guessing(tmp_path):
    run = run_at(tmp_path)
    root, *_ = pending_capture(tmp_path)
    before = view(run, root)["selected_request"]
    assert before["step_index"] is None
    rows = process_rows()
    # Identity logs can arrive before the corresponding spans; they do not alone authorize a step.
    journal(root, rows[2:])
    assert view(run, root)["selected_request"]["step_index"] is None
    # Arrival order is irrelevant; a fresh authorized snapshot completes the ancestor proof.
    journal(root, list(reversed(rows)))
    after = view(run, root)["selected_request"]
    assert after["step_index"] == 1 and after["assistant_message_id"] == "a"
    assert after["capture_assistant_message_id"] is None
    assert after["native_mapping"] == {"status": "verified", "basis": "native_processor_span",
                                        "reason": "native_session_and_message_id"}
    assert after["serialized_body"] == before["serialized_body"]


@pytest.mark.parametrize("failure", ["foreign_session", "wrong_assistant", "wrong_span_name", "cycle",
                                     "conflict", "missing_ancestor", "wrong_trace", "title"])
def test_trace_resolution_rejects_ambiguous_or_non_native_identity(tmp_path, failure):
    run = run_at(tmp_path)
    root, meta, meta_path, *_ = pending_capture(tmp_path)
    rows = process_rows()
    if failure == "foreign_session":
        rows[2]["session_id"] = "foreign"
    elif failure == "wrong_assistant":
        rows[2]["assistant_message_id"] = "u"
    elif failure == "wrong_span_name":
        rows[1]["name"] = "SomeOther.SessionProcessor.process"
    elif failure == "cycle":
        rows[0]["parent_span_id"] = TRIGGER
    elif failure == "conflict":
        rows.append({**rows[2], "assistant_message_id": "ambiguous"})
    elif failure == "missing_ancestor":
        rows = rows[:1] + rows[2:]
    elif failure == "wrong_trace":
        rows[2]["trace_id"] = "a" * 32
    else:
        meta["purpose"] = "title"
        meta_path.write_text(json.dumps(meta))
    journal(root, rows)
    selected = view(run, root)["selected_request"]
    assert selected["step_index"] is None and selected["native_mapping"]["status"] == "unlinked"


def test_trace_resolution_stops_at_nearest_processor_even_if_outer_matches(tmp_path):
    run = run_at(tmp_path)
    root, *_ = pending_capture(tmp_path)
    rows = process_rows(aid="unknown")
    rows.extend([{"type": "span", "trace_id": TRACE, "span_id": OUTER,
                  "parent_span_id": None, "name": "SessionProcessor.process"},
                 {"type": "processor_identity", "trace_id": TRACE, "span_id": OUTER,
                  "session_id": "root", "assistant_message_id": "a"}])
    journal(root, rows)
    assert view(run, root)["selected_request"]["step_index"] is None


def test_incomplete_transport_attempt_is_health_not_complete_prompt_evidence(tmp_path):
    run = run_at(tmp_path)
    root, meta, meta_path, body_path, _ = captured(tmp_path)
    meta.update(coverage={"complete": False}, body_ref=None, body_sha256=None, body_bytes=0,
                incomplete_reason="body_too_large")
    meta_path.write_text(json.dumps(meta))
    body_path.unlink()
    evidence = load_request_evidence(root, native_raw=run.raw, steps=run.steps)
    assert evidence.request_count == 0
    assert "incomplete body capture" in evidence.limitations[0]
    assert inspect_prompt_construction(run.raw, run.steps, evidence=evidence) is None


def test_trace_journal_rejects_symlink_and_unsupported_schema(tmp_path):
    run = run_at(tmp_path)
    root, *_ = pending_capture(tmp_path)
    target = journal(root, process_rows())
    target.write_text(json.dumps({"schema_version": 2, **process_rows()[0]}) + "\n")
    with pytest.raises(RequestCaptureError, match="unsupported_trace_schema"):
        view(run, root)
    outside = tmp_path / "outside-journal"
    target.rename(outside)
    target.symlink_to(outside)
    with pytest.raises(RequestCaptureError):
        view(run, root)


def test_live_prepared_request_retains_body_without_claiming_provider_success(tmp_path):
    run = run_at(tmp_path)
    root, _, _, _, raw = captured(tmp_path, status=None, dispatch_status="prepared")
    selected = view(run, root)["selected_request"]
    assert selected["serialized_body"].encode() == raw
    assert selected["dispatch_status"] == "prepared" and selected["status"] is None
    assert "proxy boundary" in selected["limitations"][0]
    assert selected["evidence"] == "captured"


def two_step_run(tmp_path):
    payload = native(messages=[message(mid="u", role="user"), message(mid="a"), message(mid="b")])
    return load_run(str(write(tmp_path, payload)))


def test_compaction_links_only_with_authoritative_native_assistant_proof(tmp_path):
    payload = native(messages=[message(mid="u", role="user"), message(mid="a")])
    payload["messages"][1]["info"].update(agent="compaction", summary=True)
    run = load_run(str(write(tmp_path, payload)))
    root, *_ = captured(tmp_path, purpose="compaction", aid=None,
        mapping={"status": "pending", "basis": "native_processor_span"}, trace_id=TRACE, span_id=TRIGGER)
    assert view(run, root)["selected_request"]["step_index"] is None
    journal(root, process_rows())
    selected = view(run, root)["selected_request"]
    assert selected["purpose"] == "compaction" and selected["step_index"] == 1
    assert selected["native_mapping"]["status"] == "verified"


def test_actual_native_boundary_comparison_has_no_invented_request_lineage(tmp_path):
    import agent_trajviz
    import jsonschema
    run = two_step_run(tmp_path)
    first_body = {"model": "x", "messages": [{"role": "user", "content": "Original instruction"}]}
    second_body = {**first_body, "messages": [*first_body["messages"],
        {"role": "user", "content": "Please add explicit verification feedback"}]}
    root, first, *_ = captured(tmp_path, aid="a", body=first_body)
    _, second, *_ = captured(tmp_path, aid="b", body=second_body)
    data = view(run, root, second["request_id"])
    selected = data["selected_request"]
    assert "previous_request_id" not in selected
    assert selected["comparison_request_id"] == first["request_id"]
    assert selected["changes"]["available"] is True
    assert selected["changes"]["lineage_verified"] is False
    assert selected["changes"]["evidence"] == "captured"
    assert selected["changes"]["basis"] == "previous_verified_native_assistant_request"
    assert selected["text_evolution"]["comparison_basis"] == selected["changes"]["basis"]
    added = [block["text"] for block in selected["text_evolution"]["blocks"] if block["change"] == "added"]
    assert added == ["Please add explicit verification feedback"]
    payload = run.view("prompt_construction").to_dict()
    payload["data"] = data
    jsonschema.validate(payload, json.loads((Path(agent_trajviz.__file__).parent / "schemas/prompt_construction.schema.json").read_text()))


def test_native_boundary_comparison_does_not_pick_arbitrary_retry_baseline(tmp_path):
    run = two_step_run(tmp_path)
    root, *_ = captured(tmp_path, aid="a")
    captured(tmp_path, aid="a")
    _, second, *_ = captured(tmp_path, aid="b")
    selected = view(run, root, second["request_id"])["selected_request"]
    assert selected["changes"]["available"] is False
    assert "comparison_request_id" not in selected


def test_explicit_request_predecessor_takes_priority_over_native_order_comparison(tmp_path):
    run = load_run(str(write(tmp_path, native(messages=[message(mid="u", role="user"),
        message(mid="a"), message(mid="b"), message(mid="c")]))))
    root, first, *_ = captured(tmp_path, aid="a", observed_at="2026-11-26T10:00:01Z")
    captured(tmp_path, aid="b", observed_at="2026-11-26T10:00:02Z")
    _, selected_meta, *_ = captured(tmp_path, aid="c", previous_request_id=first["request_id"],
                                   observed_at="2026-11-26T10:00:03Z")
    selected = view(run, root, selected_meta["request_id"])["selected_request"]
    assert selected["previous_request_id"] == first["request_id"]
    assert selected["changes"]["previous_request_id"] == first["request_id"]
    assert "comparison_request_id" not in selected


def test_native_comparison_does_not_skip_a_missing_captured_assistant_step(tmp_path):
    run = load_run(str(write(tmp_path, native(messages=[message(mid="u", role="user"),
        message(mid="a"), message(mid="b"), message(mid="c")]))))
    root, *_ = captured(tmp_path, aid="a")
    _, third, *_ = captured(tmp_path, aid="c")
    assert view(run, root, third["request_id"])["selected_request"]["changes"]["available"] is False
