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
    assert dataset(tmp_path)["status"] == "limit"


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
    # Each selected namespace reads the shared bounded index, then only its own body.
    actual_bytes = sum(path.stat().st_size for path in [*source_paths, *bodies]) + len(sessions) * index_file.stat().st_size
    monkeypatch.setattr(explorer, "MAX_UI_BYTES", actual_bytes - 1)
    result = explorer.load_dataset(root, session_ids=sessions, adapter="claude-code", claude_raw=native)
    assert result["status"] == "limit" and not result["records"]
