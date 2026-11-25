"""Synthetic DevEco native records retain provenance, scope and missing evidence."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import zipfile

import agent_trajviz as tv
import pytest

from trajviz_obs.compat import EmptySession, load_plugins, load_run
from trajviz_obs.deveco import NATIVE_KEY, normalize

BASE = 1_790_000_000_000


def message(mid="assistant", *, sid="root", role="assistant", completed=200, tokens=None, parts=None):
    info = {"id": mid, "sessionID": sid, "role": role, "agent": "build",
            "time": {"created": BASE + 100}, "modelID": "native-model", "providerID": "native-provider"}
    if completed is not None:
        info["time"]["completed"] = BASE + completed
    if tokens is not None:
        info["tokens"] = tokens
    return {"info": info, "parts": parts if parts is not None else [{"type": "text", "text": "Native answer"}]}


def native(sid="root", *, messages=None, children=None, parent=None):
    session = {"id": sid, "directory": "/controlled/project", "title": "Controlled DevEco session",
               "time": {"created": BASE, "updated": BASE + 9000}, "version": "1.0"}
    if parent:
        session["parentID"] = parent
    return {"source_format": "deveco_v1", "schema_version": 1, "directory": "/controlled/project",
            "session": session, "messages": [message(sid=sid)] if messages is None else messages,
            "children": children or []}


def write(tmp_path, payload, name="deveco.json"):
    target = tmp_path / name
    target.write_text(json.dumps(payload))
    return target


def task(child, key="sessionId"):
    return {"type": "tool", "id": "part-task", "callID": "call-task", "tool": "task",
            "state": {"status": "completed", "input": {"prompt": "Check it"}, "output": "Checked",
                      "metadata": {key: child}, "time": {"start": BASE + 120, "end": BASE + 180}}}


def test_native_normalization_copies_records_and_keeps_other_formats_unchanged():
    payload = native()
    before = copy.deepcopy(payload)
    result = normalize(payload)
    assert payload == before
    assert result[NATIVE_KEY] == before
    assert result[NATIVE_KEY] is not payload
    assert result["messages"] == payload["messages"]
    assert normalize(result) is result
    other = {"info": {"id": "legacy"}, "messages": []}
    assert normalize(other) is other


def test_load_run_and_native_loader_keep_deveco_identity_and_raw(tmp_path):
    payload = native(messages=[message(role="user", mid="user"), message(tokens={
        "input": 20, "output": 5, "total": 29, "reasoning": 1, "cache": {"read": 3, "write": 1}})])
    payload["window"] = 12345
    path = write(tmp_path, payload)
    before = path.read_bytes()
    run = load_run(str(path))
    assert run.format == "deveco" and run.format_label == "DevEco Code"
    assert tv.FORMATS["deveco"] == "DevEco Code"
    assert run.raw["metadata"]["agent"] == "deveco"
    assert run.raw["metadata"]["generator_name"] == "deveco"
    assert run.raw["metadata"]["session_id"] == "root"
    assert run.raw["metadata"]["context_window_limit"] == 12345
    assert run.raw[NATIVE_KEY] == payload
    assert [step["role"] for step in run.steps] == ["user", "assistant"]
    assert run.steps[-1]["tokens"] == {"total": 29, "input": 20, "output": 5,
                                      "cache_read": 3, "cache_write": 1, "reasoning": 1}
    assert run.steps[-1]["duration"] == pytest.approx(0.1)
    assert run.sha256 == hashlib.sha256(before).hexdigest()
    assert path.read_bytes() == before
    assert tv.load(str(path)).raw[NATIVE_KEY] == payload
    assert tv.load(str(path), format="deveco").format == "deveco"
    with pytest.raises(tv.LoadError, match="mismatch|not opencode"):
        tv.load(str(path), format="opencode")


def test_running_response_and_unreported_usage_remain_unknown(tmp_path):
    payload = native(messages=[message(completed=None, parts=[{"type": "step-start"},
                                                           {"type": "text", "text": "In progress"}])])
    run = load_run(str(write(tmp_path, payload)))
    step = run.steps[0]
    assert step["duration"] is None and step["time_completed_ms"] is None
    assert {"Total Tokens", "Input Tokens", "Output Tokens", "Reasoning Tokens", "Cache Read", "Cache Write"}.issubset(
        step["_metrics_unavailable_fields"])
    assert run.raw["token_usage"] == {}
    assert run.raw[NATIVE_KEY] == payload
    assert any("DevEco Code session root" in warning for warning in run.warnings)


def test_missing_duration_is_not_filled_even_without_step_start(tmp_path):
    run = load_run(str(write(tmp_path, native(messages=[message(completed=None)]))))
    assert run.steps[0]["duration"] is None
    assert run.steps[0]["time_completed_ms"] is None


def test_reported_zero_is_distinct_from_missing_counters(tmp_path):
    payload = native(messages=[message(tokens={"input": 0, "output": 0, "cache": {"read": 0}})])
    run = load_run(str(write(tmp_path, payload)))
    unavailable = run.steps[0]["_metrics_unavailable_fields"]
    assert "Input Tokens" not in unavailable and "Output Tokens" not in unavailable and "Cache Read" not in unavailable
    assert "Total Tokens" in unavailable and "Reasoning Tokens" in unavailable and "Cache Write" in unavailable
    assert run.raw["token_usage"] == {"prompt_tokens": 0, "completion_tokens": 0}


def test_native_tool_error_and_compaction_parts_are_preserved(tmp_path):
    part = {"type": "tool", "id": "native-part", "callID": "native-call", "tool": "bash",
            "state": {"status": "error", "input": {"command": "false"}, "error": "Intentional failure",
                      "time": {"start": BASE + 120, "end": BASE + 180}}}
    payload = native(messages=[message(parts=[part]), message(mid="compact", role="user", parts=[
        {"type": "compaction", "auto": True, "summary": "Native checkpoint"}])])
    run = load_run(str(write(tmp_path, payload)))
    call = run.steps[0]["tool_calls"][0]
    assert call["tool_name"] == "bash" and call["input"] == {"command": "false"}
    assert call["status"] == "error" and call["error"] == "Intentional failure" and call["output"] == ""
    assert run.steps[1]["is_compaction_checkpoint"] is True
    assert run.raw[NATIVE_KEY] == payload


@pytest.mark.parametrize("key", ["sessionID", "sessionId"])
def test_only_native_spawn_and_parent_agreement_authorize_children(tmp_path, key):
    confirmed = native("child", parent="root", messages=[message(sid="child", mid="child-answer")])
    unrelated = native("unrelated", parent="root")
    wrong_parent = native("wrong", parent="other")
    fork = native("fork", parent="root")
    fork["session"]["fork"] = "branch"
    payload = native(messages=[message(parts=[task("child", key), task("wrong"), task("fork")])],
                     children=[confirmed, unrelated, wrong_parent, fork])
    run = load_run(str(write(tmp_path, payload)))
    assert {step["session_id"] for step in run.steps} == {"root", "child"}
    child = next(step for step in run.steps if step["session_id"] == "child")
    assert child["is_sub_agent"] is True and child["parent_session_id"] == "root"
    assert child["session_depth"] == 1
    assert run.raw["metadata"]["sub_agent_count"] == 1
    assert len(run.raw[NATIVE_KEY]["children"]) == 4
    assert any("no confirmed native subagent spawn" in row for row in run.warnings)


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(schema_version=2),
    lambda p: p.update(schema_version=True),
    lambda p: p.update(directory=None),
    lambda p: p.update(children="invalid"),
    lambda p: p.update(window=0),
    lambda p: p.update(window=True),
    lambda p: p["session"].update(id=""),
    lambda p: p["messages"][0]["info"].update(sessionID="other"),
    lambda p: p["messages"][0].update(parts=None),
    lambda p: p["messages"].append(copy.deepcopy(p["messages"][0])),
])
def test_invalid_native_envelopes_fail_explicitly(tmp_path, mutation):
    payload = native()
    mutation(payload)
    with pytest.raises(tv.LoadError) as error:
        load_run(str(write(tmp_path, payload)))
    assert error.value.code == "invalid_deveco"


def test_duplicate_sessions_fail_instead_of_authorizing_a_branch(tmp_path):
    payload = native(children=[native("root")])
    with pytest.raises(tv.LoadError, match="duplicate or cyclic"):
        load_run(str(write(tmp_path, payload)))


def test_current_empty_source_uses_existing_empty_session_contract(tmp_path):
    with pytest.raises(EmptySession):
        load_run(str(write(tmp_path, native(messages=[]))), current=True)


def test_window_override_and_normalized_export_reload(tmp_path):
    payload = native()
    payload["window"] = 1000
    run = load_run(str(write(tmp_path, payload)), window_limit=2000)
    assert run.raw["metadata"]["context_window_limit"] == 2000
    exported = write(tmp_path, run.to_dict(include_raw=True), "export.json")
    restored = load_run(str(exported))
    assert restored.format == "deveco" and restored.raw[NATIVE_KEY] == payload
    assert restored.steps == run.steps


def test_native_zip_uses_same_converter_and_original_identity(tmp_path):
    payload = native()
    path = tmp_path / "deveco.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("session.json", json.dumps(payload))
    run = load_run(str(path))
    assert run.format == "deveco" and run.raw[NATIVE_KEY] == payload
    assert run.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


def test_install_is_idempotent_and_legacy_opencode_keeps_its_format(tmp_path):
    from agent_trajviz.insight import loaders

    load_plugins()
    normalizer = loaders._normalize_payload
    load_plugins()
    assert loaders._normalize_payload is normalizer
    payload = {"info": native()["session"], "messages": [message()]}
    run = load_run(str(write(tmp_path, payload, "legacy.json")))
    assert run.format == "opencode" and NATIVE_KEY not in run.raw


@pytest.mark.skipif(os.name != "posix", reason="Private prompt capture requires POSIX")
def test_prompt_explorer_uses_only_the_deveco_adapter_and_verified_child_sessions(tmp_path):
    from uuid import uuid4

    import prompt_capture as capture
    from trajviz_obs.prompt_explorer import _adapter, scoped_dataset

    child = native("child", parent="root")
    payload = native(messages=[message(parts=[task("child")])], children=[child, native("unrelated", parent="root")])
    source = write(tmp_path, payload)
    run = load_run(str(source))
    assert _adapter(run) == "deveco"
    root = tmp_path / "private-capture"
    for adapter, sid, text in [("deveco", "root", "ROOT"), ("deveco", "child", "CHILD"),
                               ("deveco", "unrelated", "UNRELATED"), ("opencode", "root", "OTHER_ADAPTER")]:
        capture.write_record(str(root), {"schema_version": 1, "adapter": adapter, "session_id": sid,
            "instance_id": "instance", "record_id": str(uuid4()), "sequence": 1,
            "observed_at": "2026-11-25T00:00:01Z", "stage": "context", "kind": "primary",
            "correlation": {}, "data": {"messages": [{"role": "user", "content": text}]}})
    result = scoped_dataset(run, source.resolve(), root, session_id="root")
    assert result["status"] == "ready"
    assert {row["adapter"] for row in result["records"]} == {"deveco"}
    assert {row["session_id"] for row in result["records"]} == {"root", "child"}
    assert "UNRELATED" not in json.dumps(result) and "OTHER_ADAPTER" not in json.dumps(result)


@pytest.mark.skipif(os.name != "posix", reason="Private prompt capture requires POSIX")
@pytest.mark.parametrize("has_capture", [False, True])
def test_stale_live_native_source_is_visible_without_inventing_prompt_records(tmp_path, has_capture):
    import prompt_capture as capture
    from trajviz_obs.prompt_explorer import scoped_dataset
    from trajviz_obs.prompt_explorer_render import render_explorer

    payload = native()
    source = write(tmp_path, payload)
    run = load_run(str(source))
    root = tmp_path / "captures"
    record_path = None
    if has_capture:
        record_path = capture.write_record(str(root), {
            "schema_version": 1, "adapter": "deveco", "session_id": "root", "instance_id": "instance",
            "record_id": "retained-prompt", "sequence": 1, "observed_at": "2026-11-25T00:00:01Z",
            "stage": "context", "kind": "primary", "correlation": {},
            "data": {"messages": [{"role": "user", "content": "RETAINED_PROMPT_EVIDENCE"}]},
        })
    stale = copy.deepcopy(payload)
    stale["source_health"] = {"status": "stale", "reason": "native_refresh_failed"}
    warning = ("Live DevEco refresh failed; this is the last successfully captured native session. "
               "Child-session discovery may be stale. Retry obs overview or dashboard.")
    stale["warnings"] = [warning]
    write(tmp_path, stale)
    stale_run = load_run(str(source))
    assert warning in stale_run.warnings
    assert stale_run.raw[NATIVE_KEY]["source_health"] == stale["source_health"]
    result = scoped_dataset(run, source.resolve(), root, session_id="root", reload_source=True)
    assert result["source_health"] == stale["source_health"]
    assert len(result["records"]) == int(has_capture)
    assert not result.get("capture_gaps")
    html = render_explorer(result)
    assert "Live DevEco session refresh failed" in html and "child-session discovery may be stale" in html
    assert 'Retry the obs tool with command "overview" or "dashboard"' in html
    if has_capture:
        assert result["records"][0]["record_id"] == "retained-prompt"
        assert result["records"][0]["stage"] == "context"
        assert "RETAINED_PROMPT_EVIDENCE" in html and "DevEco Code" in html
        with open(record_path, "rb") as observed:
            before = observed.read()
    else:
        assert "No prompt snapshots yet" in html
    unauthorized = scoped_dataset(stale_run, tmp_path / "unrelated.json", root, session_id="root")
    assert "source_health" not in unauthorized and not unauthorized["records"]
    write(tmp_path, payload)
    recovered = scoped_dataset(run, source.resolve(), root, session_id="root", reload_source=True)
    assert "source_health" not in recovered
    assert "Live DevEco session refresh failed" not in render_explorer(recovered)
    assert len(recovered["records"]) == int(has_capture)
    if has_capture:
        with open(record_path, "rb") as observed:
            assert observed.read() == before


def test_reserved_native_marker_cannot_bypass_envelope_validation(tmp_path):
    payload = native()
    payload[NATIVE_KEY] = {"session": {"id": "untrusted"}}
    payload["schema_version"] = 999
    with pytest.raises(tv.LoadError) as error:
        load_run(str(write(tmp_path, payload)))
    assert error.value.code == "invalid_deveco"


def test_forced_deveco_format_requires_explicit_native_provenance(tmp_path):
    load_plugins()
    path = write(tmp_path, {"messages": [message()]}, "unmarked.json")
    with pytest.raises(tv.LoadError) as error:
        tv.load(str(path), format="deveco")
    assert error.value.code == "invalid_deveco"


def test_all_36_registered_views_support_native_deveco_without_fabricating_missing_evidence(tmp_path):
    measured = {"input": 0, "output": 5, "total": 8, "reasoning": 1, "cache": {"read": 3, "write": 0}}
    pending = message(mid="pending", completed=None, parts=[{"type": "step-start"},
                                                          {"type": "text", "text": "Still running"}])
    pending["info"]["time"]["created"] = BASE + 300
    child = native("child", parent="root", messages=[message(sid="child", mid="child-answer", tokens=measured)])
    payload = native(messages=[message(mid="user", role="user"),
                               message(mid="measured", tokens=measured, parts=[task("child")]), pending],
                     children=[child])
    path = write(tmp_path, payload)
    source_bytes = path.read_bytes()
    run = load_run(str(path))
    unknown = next(row for row in run.steps if row["id"] == "pending")
    assert unknown["time_completed_ms"] is None and unknown["duration"] is None
    missing_before = set(unknown["_metrics_unavailable_fields"])
    assert {"Total Tokens", "Input Tokens", "Output Tokens", "Reasoning Tokens", "Cache Read", "Cache Write"} <= missing_before
    assert "reasoning" not in unknown["tokens"]
    assert run.raw["token_usage"] == {}
    catalog = tuple(tv.VIEW_CATALOG)
    assert len(catalog) == 36
    views = {name: run.view(name) for name in catalog}
    for name, view in views.items():
        assert json.loads(view.to_json())["id"] == name
    assert views["summary"].data["format"] == "deveco"
    assert views["summary"].data["format_label"] == "DevEco Code"
    assert run.format == "deveco" and run.format_label == "DevEco Code"
    assert unknown["time_completed_ms"] is None and unknown["duration"] is None
    assert set(unknown["_metrics_unavailable_fields"]) == missing_before
    assert "reasoning" not in unknown["tokens"] and run.raw["token_usage"] == {}
    assert run.raw[NATIVE_KEY] == payload and path.read_bytes() == source_bytes
