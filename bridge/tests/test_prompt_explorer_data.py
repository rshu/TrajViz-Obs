"""The browser can select captured identities, never discover or authorize files."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

import prompt_capture as capture
from trajviz_obs import prompt_explorer_data as explorer

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Private prompt capture requires POSIX")


def observation(**fields):
    return {"schema_version": 1, "adapter": "pi", "session_id": "authorized", "instance_id": "instance",
            "record_id": str(uuid4()), "sequence": 1, "observed_at": "2026-11-25T00:00:01Z",
            "stage": "context", "kind": "primary", "correlation": {"lineage_id": "lineage", "run_id": "run"},
            "data": {"messages": [{"role": "user", "content": "AUTHORIZED_PROMPT"}]}, **fields}


def dataset(root, **options):
    return explorer.load_dataset(root, session_ids={"authorized"}, adapter="pi", **options)


def test_disabled_or_unscoped_reader_performs_no_io(monkeypatch, tmp_path):
    monkeypatch.setattr(capture, "private_directory", lambda *_args, **_kwargs: pytest.fail("reader accessed filesystem"))
    assert explorer.load_dataset(None, session_ids={"authorized"}, adapter="pi")["status"] == "disabled"
    assert explorer.load_dataset(tmp_path, session_ids=set(), adapter="pi")["status"] == "unavailable"
    assert explorer.load_dataset(tmp_path, session_ids={"authorized"})["status"] == "unavailable"
    assert explorer.load_dataset(tmp_path, session_ids={"../outside"}, adapter="unknown")["status"] == "unavailable"


def test_unsupported_storage_platform_returns_guidance_without_filesystem_access(monkeypatch, tmp_path):
    monkeypatch.setattr(capture, "private_directory", lambda *_args, **_kwargs: pytest.fail("reader accessed filesystem"))
    with monkeypatch.context() as platform:
        platform.setattr(explorer.os, "name", "nt")
        result = explorer.load_dataset(tmp_path, session_ids={"authorized"}, adapter="pi")
    assert result["status"] == "unavailable" and "POSIX host" in result["message"]


def test_only_the_authorized_adapter_and_session_namespace_is_read(tmp_path):
    capture.write_record(str(tmp_path), observation())
    unrelated = Path(capture.write_record(str(tmp_path), observation(session_id="unrelated",
                     data={"messages": [{"role": "user", "content": "UNRELATED_PRIVATE_PROMPT"}]})))
    other_adapter = Path(capture.write_record(str(tmp_path), observation(adapter="opencode",
                     data={"messages": [{"role": "user", "content": "OTHER_ADAPTER_PROMPT"}]})))
    unrelated.chmod(0o644)
    other_adapter.chmod(0o644)
    result = dataset(tmp_path)
    serialized = json.dumps(result)
    assert result["status"] == "ready"
    assert len(result["records"]) == 1
    assert "AUTHORIZED_PROMPT" in serialized
    assert "UNRELATED_PRIVATE_PROMPT" not in serialized and "OTHER_ADAPTER_PROMPT" not in serialized
    assert "unrelated" not in serialized


def test_missing_capture_does_not_create_files_or_discover_other_sessions(tmp_path):
    capture.write_record(str(tmp_path), observation(session_id="other"))
    before = sorted(str(path) for path in tmp_path.rglob("*"))
    result = dataset(tmp_path)
    assert result["status"] == "empty"
    assert sorted(str(path) for path in tmp_path.rglob("*")) == before
    absent = tmp_path / "never-created"
    assert dataset(absent)["status"] == "empty"
    assert not absent.exists()


@pytest.mark.parametrize("kind", ["root_link", "session_link", "fifo", "hardlink", "world_readable"])
def test_unsafe_selected_storage_fails_without_exposing_paths(tmp_path, kind):
    root = tmp_path / "private-capture"
    file = Path(capture.write_record(str(root), observation()))
    target = root
    if kind == "root_link":
        target = tmp_path / "alias"
        target.symlink_to(root, target_is_directory=True)
    elif kind == "session_link":
        namespace = root / "pi" / hashlib.sha256(b"authorized").hexdigest()[:32]
        moved = root / "pi" / "moved"
        namespace.rename(moved)
        namespace.symlink_to(moved, target_is_directory=True)
    elif kind == "fifo":
        os.mkfifo(file.parent / "hazard.json", 0o600)
    elif kind == "hardlink":
        os.link(file, tmp_path / "linked")
    else:
        file.chmod(0o644)
    result = dataset(target)
    assert result["status"] == "unavailable"
    assert not result["records"]
    assert str(root) not in result["message"]


def test_input_budgets_are_checked_before_parsing(tmp_path, monkeypatch):
    capture.write_record(str(tmp_path), observation())
    capture.write_record(str(tmp_path), observation(sequence=2))
    monkeypatch.setattr(explorer, "MAX_UI_RECORDS", 1)
    monkeypatch.setattr(capture, "inspect_records", lambda *_args, **_kwargs: pytest.fail("oversized input was parsed"))
    result = dataset(tmp_path)
    assert result["status"] == "ready"
    assert len(result["records"]) == 1
    assert result["page"]["has_older"] and result["page"]["known_observations"] == 2


def test_source_inspector_honors_smaller_byte_and_record_budgets(tmp_path):
    capture.write_record(str(tmp_path), observation())
    capture.write_record(str(tmp_path), observation(sequence=2))
    for options in ({"max_bytes": 1}, {"max_records": 1}):
        with pytest.raises(capture.CaptureError, match="excessive_input"):
            capture.inspect_records(tmp_path, **options)
    for options in ({"max_bytes": -1}, {"max_records": True}):
        with pytest.raises(capture.CaptureError, match="invalid_input_limit"):
            capture.inspect_records(tmp_path, **options)


def test_same_raw_id_in_different_instances_has_distinct_ui_identities(tmp_path):
    capture.write_record(str(tmp_path), observation(record_id="same", instance_id="one"))
    capture.write_record(str(tmp_path), observation(record_id="same", instance_id="two"))
    result = dataset(tmp_path)
    assert len({row["selection_id"] for row in result["records"]}) == 2
    assert len(result["lanes"]) == 2
    assert explorer.select_record(result, "same")["record"] is None
    selected = explorer.select_record(result, result["records"][0]["selection_id"])
    assert selected["record"] is result["records"][0]
    assert len(selected["timeline"]) == 1


def test_effective_state_diff_uses_proven_baseline_and_preserves_removed_content(tmp_path):
    system = {"role": "system", "content": "", "sections": {"rules": "old rule"},
              "toolsAdded": [{"name": "read"}, {"name": "write"}]}
    messages = [system, {"role": "user", "content": "task"}]
    first = observation(data={"messages": messages})
    capture.write_record(str(tmp_path), first)
    second = observation(sequence=2, correlation={"lineage_id": "lineage", "run_id": "next-run"},
        data={"messages": [*messages, {"role": "system", "content": "", "sections": {"rules": "new rule"},
                                      "toolsRemoved": [{"name": "write"}]}]})
    capture.write_record(str(tmp_path), second)
    result = dataset(tmp_path)
    original = copy.deepcopy(result)
    selected = explorer.select_record(result, result["records"][1]["selection_id"], effective=True)
    assert selected["baseline"]["record_id"] == first["record_id"]
    assert selected["record"]["baseline_selection_id"] == selected["baseline"]["selection_id"]
    removed = next(row for row in selected["components"] if row["id"] == "tool:write")
    assert removed["changes"] == ["removed"] and removed["before"]["content"] == {"name": "write"}
    detail = explorer.component_detail(result, selected["record"]["selection_id"], "system:section:rules", effective=True)
    assert "-old rule" in detail["diff_lines"] and "+new rule" in detail["diff_lines"]
    assert result == original


def test_initial_observation_is_a_baseline_and_unlinked_components_are_uncompared(tmp_path):
    capture.write_record(str(tmp_path), observation())
    capture.write_record(str(tmp_path), observation(stage="component.section", data={"input": {"name": "rule"}, "output": "text"}))
    result = dataset(tmp_path)
    context = next(row for row in result["records"] if row["stage"] == "context")
    component = next(row for row in result["records"] if row["stage"] == "component.section")
    assert all(row["changes"] == ["baseline"] for row in explorer.select_record(result, context["selection_id"])["components"])
    assert all(row["changes"] == ["uncompared"] for row in explorer.select_record(result, component["selection_id"])["components"])


def test_component_boundaries_and_restarted_captures_have_distinct_lane_labels(tmp_path):
    for stage, instance in [("component.section", "instance"), ("component.tool", "instance"),
                            ("context", "one"), ("context", "two")]:
        capture.write_record(str(tmp_path), observation(stage=stage, instance_id=instance))
    result = dataset(tmp_path)
    assert len({lane["label"] for lane in result["lanes"]}) == len(result["lanes"])


def test_deveco_component_lanes_identify_observation_stages_without_pairing(tmp_path):
    stages = {
        "component.system": ("System sections", {"system": ["rules"]}),
        "component.messages": ("Message history", {"messages": [{"role": "user", "content": "history"}]}),
        "component.user_message": ("User message", {"messages": [{"role": "user", "content": "new task"}]}),
        "component.parameters": ("Generation parameters", {"parameters": {"temperature": 0.5}}),
        "component.compaction": ("Compaction input", {"context": ["compaction context"]}),
    }
    for stage, (_label, data) in stages.items():
        for sequence in (1, 2):
            capture.write_record(str(tmp_path), observation(adapter="deveco", stage=stage, sequence=sequence, data=data))
    result = explorer.load_dataset(tmp_path, session_ids={"authorized"}, adapter="deveco")
    assert result["status"] == "ready"
    assert {lane["stage"]: lane["label"] for lane in result["lanes"]} == {
        stage: f"DevEco Code · {label} · Unlinked observation · Session" for stage, (label, _data) in stages.items()
    }
    for row in result["records"]:
        assert row["comparison_status"] == "unlinked_component"
        assert row["baseline_selection_id"] is None and row["changes"] is None
        selected = explorer.select_record(result, row["selection_id"])
        assert selected["components"]
        assert all(component["changes"] == ["uncompared"] for component in selected["components"])


def test_gaps_control_frames_and_compaction_do_not_become_context_removals(tmp_path):
    capture.write_record(str(tmp_path), observation())
    capture.write_record(str(tmp_path), observation(stage="provider.request", data={"payload": {"messages": [{"role": "user", "content": "one"}]}}))
    capture.write_record(str(tmp_path), observation(stage="provider.request", sequence=2, data={"payload": None, "omitted_reason": "read_timeout"}))
    capture.write_record(str(tmp_path), observation(stage="lifecycle.compaction", kind="compaction", data={"provenance": {"request_payload_exposed": False}}))
    capture.write_record(str(tmp_path), observation(stage="capture.gap", data={"reason": "queue_full", "omitted_records": 2, "secret": "GAP_CONTENT_NOT_PUBLIC"}))
    result = dataset(tmp_path)
    provider = next(row for row in result["records"] if row.get("omitted_reason") == "read_timeout")
    assert provider["changes"] is None
    assert len(result["lanes"]) == 4
    assert {row["stage"] for row in result["markers"]} == {"lifecycle.compaction", "capture.gap"}
    assert result["capture_gaps"][0]["omitted_records"] == 2
    assert "GAP_CONTENT_NOT_PUBLIC" not in json.dumps(result)


def test_component_diff_is_bounded_without_truncating_stored_content(tmp_path, monkeypatch):
    first = observation(data={"system": "a" * 100})
    capture.write_record(str(tmp_path), first)
    capture.write_record(str(tmp_path), observation(sequence=2, data={"system": "b" * 100}))
    result = dataset(tmp_path)
    monkeypatch.setattr(explorer, "MAX_DIFF_CHARS", 10)
    detail = explorer.component_detail(result, result["records"][1]["selection_id"], "system:0")
    assert detail["truncated"] and len(detail["before_text"]) == 10 and len(detail["after_text"]) == 10
    assert len(result["records"][0]["components"][0]["content"]) == 100
    assert explorer.component_detail(result, result["records"][1]["selection_id"], "not-captured") is None


def test_misplaced_sessions_cannot_drive_native_body_reads(tmp_path, monkeypatch):
    root, native = tmp_path / "capture", tmp_path / "native"
    native.mkdir(mode=0o700)
    authorized = Path(capture.write_record(str(root), observation(adapter="claude-code")))
    other = Path(capture.write_record(str(root), observation(adapter="claude-code", session_id="unrelated",
                 data={"messages": [{"role": "user", "content": "DO_NOT_SERVE"}]})))
    misplaced = authorized.parent / "misplaced.json"
    shutil.copyfile(other, misplaced)
    misplaced.chmod(0o600)
    (native / "safe.request.json").write_text(json.dumps({"messages": [{"role": "user", "content": "native authorized"}]}))
    os.mkfifo(native / "unrelated.request.json", 0o600)
    entries = [{"session_id": session, "timestamp": "2026-11-25T00:00:00Z", "request_file": filename}
               for session, filename in [("authorized", "safe.request.json"), ("unrelated", "unrelated.request.json")]]
    (native / "index.jsonl").write_text("\n".join(json.dumps(entry) for entry in entries))
    opened = []
    original = os.open

    def observe(name, *args, **kwargs):
        opened.append(str(name))
        return original(name, *args, **kwargs)

    monkeypatch.setattr(os, "open", observe)
    result = explorer.load_dataset(root, adapter="claude-code", session_ids={"authorized"}, claude_raw=native)
    assert result["status"] == "ready" and len(result["records"]) == 2
    assert "unrelated.request.json" not in opened
    assert "DO_NOT_SERVE" not in json.dumps(result)


def test_native_import_honors_budget_before_loading_a_body(tmp_path):
    native = tmp_path / "native"
    native.mkdir(mode=0o700)
    (native / "body.request.json").write_text(json.dumps({"messages": "x" * 1000}))
    line = json.dumps({"session_id": "authorized", "timestamp": "now", "request_file": "body.request.json"})
    (native / "index.jsonl").write_text(line)
    with pytest.raises(capture.CaptureError, match="excessive_input"):
        capture.claude_native_records(native, {"authorized"}, max_bytes=len(line.encode()) + 10)


def test_inspection_usage_counts_health_index_and_native_body_bytes(tmp_path):
    root, native = tmp_path / "capture", tmp_path / "native"
    native.mkdir(mode=0o700)
    file = Path(capture.write_record(str(root), observation(adapter="claude-code")))
    health = file.parent / "capture-health.json"
    health.write_text(json.dumps({"stage": "capture.status", "reason": "storage_limit"}))
    health.chmod(0o600)
    body = native / "request.request.json"
    body.write_text(json.dumps({"messages": [{"role": "user", "content": "body"}]}))
    index = native / "index.jsonl"
    index.write_text(json.dumps({"session_id": "authorized", "timestamp": "now", "request_file": body.name}))
    report = capture.inspect_records(root, claude_raw=native)
    assert report["inspection"] == {"records": 3,
        "bytes": sum(path.stat().st_size for path in [file, health, body, index])}


def test_combined_scope_budget_uses_raw_native_inputs_not_small_projections(tmp_path, monkeypatch):
    root, native = tmp_path / "capture", tmp_path / "native"
    native.mkdir(mode=0o700)
    source_paths, bodies, entries = [], [], []
    sessions = {"authorized", "another"}
    for index, session in enumerate(sorted(sessions)):
        source_paths.append(Path(capture.write_record(str(root), observation(adapter="claude-code", session_id=session))))
        body = native / f"{index}.request.json"
        body.write_text(json.dumps({"messages": [{"role": "user", "content": "small"}], "unprojected_diagnostics": "x" * 4000}))
        bodies.append(body)
        entries.append({"session_id": session, "timestamp": "now", "request_file": body.name})
    index_file = native / "index.jsonl"
    index_file.write_text("\n".join(json.dumps(row) for row in entries))
    # The page must account for large unprojected native fields, not its small component projection.
    actual_bytes = sum(path.stat().st_size for path in [*source_paths, *bodies]) + len(sessions) * index_file.stat().st_size
    monkeypatch.setattr(explorer, "MAX_UI_BYTES", actual_bytes - 1)
    result = explorer.load_dataset(root, session_ids=sessions, adapter="claude-code", claude_raw=native)
    assert result["status"] == "ready"
    assert result["page"]["observations"] < result["page"]["known_observations"]
    assert result["page"]["omitted_oversized"] == 1
    assert result["records"][0]["omitted_reason"] == "record_exceeds_page_budget"


def publish(root, index, **fields):
    record = observation(sequence=index, observed_at=f"2026-11-25T00:00:{index:02d}Z", **fields)
    path = Path(capture.write_record(str(root), record))
    os.utime(path, ns=(1_000_000_000 + index, 1_000_000_000 + index))
    return record, path


def test_bounded_windows_traverse_older_newer_and_stay_current_after_append(tmp_path, monkeypatch):
    for index in range(1, 10):
        publish(tmp_path, index)
    monkeypatch.setattr(explorer, "MAX_UI_RECORDS", 3)
    latest = dataset(tmp_path)
    assert [row["record_id"] for row in latest["records"]] == [
        json.loads(path.read_text())["record_id"] for path in sorted(tmp_path.rglob("*.json"), key=lambda path: path.stat().st_mtime_ns)[-3:]]
    assert latest["page"]["position_start"] == 7 and latest["page"]["position_end"] == 9
    assert latest["page"]["has_older"] and not latest["page"]["has_newer"]
    assert latest["records"][0]["comparison_status"] == "page_boundary"
    assert latest["records"][0]["changes"] is None
    older = dataset(tmp_path, cursor=latest["page"]["older_cursor"], direction="older")
    assert older["page"]["position_start"] == 4 and older["page"]["position_end"] == 6
    assert older["page"]["has_older"] and older["page"]["has_newer"]
    newer = dataset(tmp_path, cursor=older["page"]["newer_cursor"], direction="newer")
    assert [row["record_id"] for row in newer["records"]] == [row["record_id"] for row in latest["records"]]
    publish(tmp_path, 10)
    current = dataset(tmp_path, cursor=older["page"]["current_cursor"], direction="current")
    assert [row["record_id"] for row in current["records"]] == [row["record_id"] for row in older["records"]]
    refreshed = dataset(tmp_path)
    assert refreshed["page"]["position_start"] == 8 and refreshed["page"]["position_end"] == 10
    earliest = dataset(tmp_path, cursor=older["page"]["older_cursor"], direction="older")
    assert not earliest["page"]["has_older"] and earliest["records"][0]["comparison_status"] == "initial_baseline"
    assert all(row["baseline_selection_id"] is None or row["baseline_selection_id"] in {
        candidate["selection_id"] for candidate in older["records"]} for row in older["records"])


def test_cursor_is_opaque_scoped_bounded_and_expires(tmp_path, monkeypatch):
    publish(tmp_path, 1)
    initial = dataset(tmp_path)
    token = initial["page"]["current_cursor"]
    assert str(tmp_path) not in token and "authorized" not in token
    for options in ({"cursor": "forged", "direction": "current"}, {"cursor": token, "direction": "bad"}):
        assert dataset(tmp_path, **options)["status"] == "unavailable"
    expanded = explorer.load_dataset(tmp_path, session_ids={"authorized", "new-child"}, adapter="pi", cursor=token, direction="current")
    assert expanded["reason"] == "cursor_expired" and not expanded["records"]
    other = tmp_path / "other"
    capture.write_record(str(other), observation())
    assert dataset(other, cursor=token, direction="current")["reason"] == "cursor_expired"
    monkeypatch.setattr(explorer, "MAX_CURSORS", 2)
    for _ in range(3):
        dataset(tmp_path)
    assert len(explorer._CURSORS) <= 2
    token = dataset(tmp_path)["page"]["current_cursor"]
    monkeypatch.setattr(explorer, "CURSOR_TTL_SECONDS", -1)
    assert dataset(tmp_path, cursor=token, direction="current")["reason"] == "cursor_expired"


def test_catalog_limit_is_separate_and_precedes_all_prompt_body_reads(tmp_path, monkeypatch):
    publish(tmp_path, 1)
    publish(tmp_path, 2)
    monkeypatch.setattr(explorer, "MAX_CATALOG_ENTRIES", 1)
    monkeypatch.setattr(explorer, "_read_file", lambda *_args, **_kwargs: pytest.fail("catalog overflow read a prompt body"))
    result = dataset(tmp_path)
    assert result["status"] == "limit" and not result["records"]
    assert str(tmp_path) not in result["message"]


def test_oversized_record_is_an_explicit_browsable_gap_not_silently_skipped(tmp_path, monkeypatch):
    _, small = publish(tmp_path, 1)
    _, large = publish(tmp_path, 2, data={"messages": [{"role": "user", "content": "PRIVATE_BODY" * 2000}]})
    monkeypatch.setattr(explorer, "MAX_UI_BYTES", 4096)
    opened = []
    original = explorer._read_file

    def record_read(path, *args, **kwargs):
        opened.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(explorer, "_read_file", record_read)
    result = dataset(tmp_path)
    assert result["status"] == "ready" and result["page"]["omitted_oversized"] == 1
    assert result["records"][0]["omitted_reason"] == "record_exceeds_page_budget"
    assert large not in opened and "PRIVATE_BODY" not in json.dumps(result)
    older = dataset(tmp_path, cursor=result["page"]["older_cursor"], direction="older")
    assert older["status"] == "ready" and small in opened
    assert older["records"][0]["components"][0]["content"]["content"] == "AUTHORIZED_PROMPT"


def test_projection_expansion_is_bounded_even_when_the_raw_file_fits(tmp_path, monkeypatch):
    publish(tmp_path, 1, data={"messages": [{"role": "user"} for _ in range(30)]})
    monkeypatch.setattr(explorer, "MAX_UI_BYTES", 5000)
    result = dataset(tmp_path)
    assert result["status"] == "ready"
    assert result["records"][0]["omitted_reason"] == "record_exceeds_page_budget"
    assert len(capture.encoded(result)) <= explorer.MAX_UI_BYTES


def test_partial_json_gap_blocks_comparisons_across_the_missing_observation(tmp_path):
    _, first = publish(tmp_path, 1)
    partial = first.parent / "partial.json"
    partial.write_text('{"data":"DO_NOT_EXPOSE_PARTIAL')
    partial.chmod(0o600)
    os.utime(partial, ns=(1_000_000_002, 1_000_000_002))
    second, _ = publish(tmp_path, 3)
    result = dataset(tmp_path)
    assert result["status"] == "ready"
    gap = next(row for row in result["records"] if row["stage"] == "capture.gap")
    assert gap["omitted_reason"] == "invalid_json" and "DO_NOT_EXPOSE_PARTIAL" not in json.dumps(result)
    after = next(row for row in result["records"] if row["record_id"] == second["record_id"])
    assert after["comparison_status"] == "page_boundary" and after["changes"] is None
    assert after["baseline_selection_id"] is None


def test_native_pages_preserve_exact_parents_and_never_read_other_page_bodies(tmp_path, monkeypatch):
    root, native = tmp_path / "capture", tmp_path / "native"
    native.mkdir(mode=0o700)
    publish(root, 0, adapter="claude-code")
    entries, bodies = [], []
    for index, parent in enumerate([None, "message-0", "message-0", "message-2"]):
        path = native / f"{index}.request.json"
        payload = {"messages": [{"role": "user", "content": f"NATIVE_PRIVATE_{index}"}]}
        if parent:
            payload["thread"] = {"previous_message_id": parent}
        path.write_text(json.dumps(payload))
        os.utime(path, ns=(1_000_000_001 + index, 1_000_000_001 + index))
        bodies.append(path)
        entries.append({"session_id": "authorized", "timestamp": str(index), "request_file": path.name,
                        "message_id": f"message-{index}", "query_source": "sdk"})
    (native / "index.jsonl").write_text("\n".join(json.dumps(row) for row in entries))
    monkeypatch.setattr(explorer, "MAX_UI_RECORDS", 2)
    opened = []
    original = explorer._read_file

    def record_read(path, *args, **kwargs):
        opened.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(explorer, "_read_file", record_read)
    options = {"adapter": "claude-code", "session_ids": {"authorized"}, "claude_raw": native}
    latest = explorer.load_dataset(root, **options)
    assert latest["status"] == "ready"
    a, b = latest["records"]
    assert a["correlation"]["message_id"] == "message-2" and a["comparison_status"] == "page_boundary"
    assert b["baseline_selection_id"] == a["selection_id"]
    assert bodies[0] not in opened and bodies[1] not in opened
    assert "NATIVE_PRIVATE_0" not in json.dumps(latest) and "NATIVE_PRIVATE_1" not in json.dumps(latest)
    older = explorer.load_dataset(root, **options, cursor=latest["page"]["older_cursor"], direction="older")
    a, b = older["records"]
    assert b["correlation"]["message_id"] == "message-1" and b["baseline_selection_id"] == a["selection_id"]
    assert a["selection_id"] not in {row["selection_id"] for row in latest["records"]}


def test_fragmented_component_limits_precede_projection_and_still_allow_history_browsing(tmp_path, monkeypatch):
    publish(tmp_path, 1)
    publish(tmp_path, 2, data={"messages": [{"role": "user", "content": "fragment"} for _ in range(20)]})
    monkeypatch.setattr(explorer, "MAX_UI_COMPONENTS", 8)
    projected = []
    original = capture.components

    def count_projection(row):
        projected.append(row["stage"])
        return original(row)

    monkeypatch.setattr(capture, "components", count_projection)
    result = dataset(tmp_path)
    assert result["status"] == "ready"
    assert next(row for row in result["records"] if row["stage"] == "capture.gap")["omitted_reason"] == "record_structure_limit"
    assert projected.count("context") == 1


def test_component_page_budget_preserves_full_session_identity_when_records_share_ids(tmp_path, monkeypatch):
    publish(tmp_path, 1, session_id="authorized", record_id="shared")
    publish(tmp_path, 2, session_id="another", record_id="shared")
    monkeypatch.setattr(explorer, "MAX_UI_COMPONENTS", 1)
    result = explorer.load_dataset(tmp_path, adapter="pi", session_ids={"authorized", "another"})
    assert result["status"] == "ready" and len(result["records"]) == 1
    assert result["records"][0]["session_id"] == "another"
    assert result["page"]["has_older"] and result["records"][0]["comparison_status"] == "page_boundary"


def test_writer_health_is_live_deduplicated_and_blocks_cross_lane_counter_increases(tmp_path):
    first, _ = publish(tmp_path, 1, capture_health={"dropped": 0, "failed": 0})
    publish(tmp_path, 2, stage="provider.request", data={"payload": {"messages": [{"role": "user", "content": "provider"}]}},
            capture_health={"dropped": 1, "failed": 2, "details": "PRIVATE_HEALTH_DETAIL"})
    affected, _ = publish(tmp_path, 3, capture_health={"dropped": 1, "failed": 2})
    following, _ = publish(tmp_path, 4, capture_health={"dropped": 1, "failed": 2})
    result = dataset(tmp_path)
    notices = [row for row in result["capture_gaps"] if row["reason"] == "writer_loss"]
    assert notices == [{"stage": "capture.status", "adapter": "pi", "session_id": "authorized", "instance_id": "instance",
                        "reason": "writer_loss", "scope": "writer", "attribution": "request_unknown", "dropped": 1, "failed": 2}]
    rows = {row["record_id"]: row for row in result["records"]}
    assert rows[first["record_id"]]["comparison_status"] == "initial_baseline"
    assert rows[affected["record_id"]]["comparison_status"] == "capture_gap"
    assert rows[affected["record_id"]]["changes"] is None and rows[affected["record_id"]]["baseline_selection_id"] is None
    assert rows[following["record_id"]]["comparison_status"] == "compared"
    assert rows[following["record_id"]]["baseline_record_id"] == affected["record_id"]
    assert "PRIVATE_HEALTH_DETAIL" not in json.dumps(result)


def test_terminal_health_merges_with_live_notice_and_invalid_counter_fields_are_not_exposed(tmp_path):
    publish(tmp_path, 1, capture_health={"dropped": 1, "failed": False, "path": "PRIVATE_HEALTH_PATH"})
    publish(tmp_path, 2, capture_health={"dropped": -10, "failed": "PRIVATE_HEALTH_ERROR"})
    publish(tmp_path, 3, stage="capture.status", data={"dropped": 1, "failed": 3, "error": "PRIVATE_TERMINAL_ERROR"})
    result = dataset(tmp_path)
    assert len(result["capture_gaps"]) == 1
    notice = result["capture_gaps"][0]
    assert notice["reason"] == "writer_loss" and notice["dropped"] == 1 and notice["failed"] == 3
    assert result["records"][0]["capture_health"] == {"dropped": 1}
    assert result["records"][1]["capture_health"] == {}
    assert all(secret not in json.dumps(result) for secret in ["PRIVATE_HEALTH_PATH", "PRIVATE_HEALTH_ERROR", "PRIVATE_TERMINAL_ERROR"])


def test_writer_health_counter_epochs_use_enqueue_order_and_keep_restarts_isolated(tmp_path):
    publish(tmp_path, 1, stage="provider.request", correlation={"lineage_id": "same", "observation_sequence": 20},
            data={"payload": {"messages": [{"role": "user", "content": "earlier enqueue"}]}}, capture_health={"dropped": 0})
    publish(tmp_path, 2, stage="provider.request", correlation={"lineage_id": "same", "observation_sequence": 10},
            data={"payload": {"messages": [{"role": "user", "content": "later enqueue"}]}}, capture_health={"dropped": 1})
    clean, _ = publish(tmp_path, 3, instance_id="restarted", capture_health={"dropped": 0, "failed": 0})
    result = dataset(tmp_path)
    affected = [row for row in result["records"] if row["instance_id"] == "instance"]
    assert all(row["comparison_status"] == "capture_gap" and row["changes"] is None for row in affected)
    assert next(row for row in result["records"] if row["record_id"] == clean["record_id"])["comparison_status"] == "initial_baseline"
    assert len(result["capture_gaps"]) == 1 and result["capture_gaps"][0]["instance_id"] == "instance"


def test_writer_gap_suppresses_effective_and_text_diffs_without_inventing_an_empty_baseline(tmp_path):
    system = {"role": "system", "content": "", "sections": {"rules": "known rule"}}
    publish(tmp_path, 1, data={"messages": [system]}, capture_health={"dropped": 0})
    affected, _ = publish(tmp_path, 2, data={"messages": [system, {"role": "system", "content": "", "sections": {"rules": "changed rule"}}]},
                          capture_health={"dropped": 1})
    result = dataset(tmp_path)
    row = next(row for row in result["records"] if row["record_id"] == affected["record_id"])
    assert row["comparison_status"] == "capture_gap" and row["effective_state_changes"] is None
    selected = explorer.select_record(result, row["selection_id"], effective=True)
    assert selected["baseline"] is None and all(component["changes"] == ["uncompared"] for component in selected["components"])
    detail = explorer.component_detail(result, row["selection_id"], "system:section:rules", effective=True)
    assert detail["after_text"] == "changed rule" and detail["diff_lines"] == []
    assert detail["comparison_available"] is False


def test_terminal_counter_increase_blocks_later_baselines_without_leaking_internal_sequence(tmp_path):
    publish(tmp_path, 1, capture_health={"dropped": 0})
    publish(tmp_path, 2, stage="capture.status", data={"dropped": 1, "failed": 0})
    following, _ = publish(tmp_path, 3)
    result = dataset(tmp_path)
    row = next(row for row in result["records"] if row["record_id"] == following["record_id"])
    assert row["comparison_status"] == "capture_gap" and row["changes"] is None
    assert len(result["capture_gaps"]) == 1 and "_writer_sequence" not in json.dumps(result)
