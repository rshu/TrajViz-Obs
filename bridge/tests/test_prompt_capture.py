"""Read-only capture storage and component evolution use no models or network."""

from __future__ import annotations

import copy
import json
import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import prompt_capture as capture

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Prompt capture private storage currently requires POSIX")


def observation(**fields):
    return {"schema_version": 1, "adapter": "pi", "session_id": "private-session", "instance_id": "instance",
            "record_id": str(uuid4()), "sequence": 1, "observed_at": "2026-11-25T00:00:01.000Z",
            "stage": "context", "kind": "primary", "correlation": {"run_id": "run"},
            "data": {"messages": [{"role": "user", "content": "a synthetic prompt"}]}, **fields}


def test_private_atomic_immutable_records_and_order(tmp_path):
    record = observation(data={"messages": [{"role": "system", "content": "", "sections": {"z": "first", "a": "second"}}]})
    original = copy.deepcopy(record)
    file = Path(capture.write_record(str(tmp_path), record))
    assert record == original
    assert json.loads(file.read_text()) == record
    assert list(json.loads(file.read_text())["data"]["messages"][0]["sections"]) == ["z", "a"]
    assert stat.S_IMODE(file.stat().st_mode) == 0o600
    assert stat.S_IMODE(file.parent.stat().st_mode) == 0o700
    with pytest.raises(FileExistsError):
        capture.write_record(str(tmp_path), {**record, "data": "replacement"})
    assert json.loads(file.read_text()) == original
    assert not list(tmp_path.rglob(".pending-*"))


@pytest.mark.parametrize("kind", ["root_link", "parent_link", "world_readable", "file_link", "hard_link"])
def test_rejects_unsafe_storage(tmp_path, kind):
    root = tmp_path / "capture"
    outside = tmp_path / "outside"
    outside.mkdir()
    record = observation()
    if kind in {"root_link", "parent_link"}:
        root.symlink_to(outside, target_is_directory=True)
        with pytest.raises((OSError, ValueError)):
            capture.write_record(str(root / "child" if kind == "parent_link" else root), record)
        assert list(outside.iterdir()) == []
    elif kind == "world_readable":
        root.mkdir(mode=0o755)
        with pytest.raises(capture.CaptureError):
            capture.write_record(str(root), record)
    else:
        file = Path(capture.write_record(str(root), record))
        target = outside / "untouched"
        target.write_text("original")
        if kind == "file_link":
            file.unlink()
            file.symlink_to(target)
        else:
            os.link(file, target.parent / "hardlink")
        with pytest.raises((OSError, ValueError)):
            capture.write_record(str(root), observation())
        assert target.read_text() == "original"


def test_parallel_instances_publish_complete_records(tmp_path):
    records = [observation() for _ in range(24)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        paths = list(pool.map(lambda record: capture.write_record(str(tmp_path), record), records))
    assert len(set(paths)) == len(records)
    assert all(json.loads(Path(file).read_text())["data"] == records[0]["data"] for file in paths)
    assert not list(tmp_path.rglob(".pending-*"))


def test_independent_processes_publish_complete_records(tmp_path):
    def write(_):
        return subprocess.run([sys.executable, "-I", capture.__file__, "write", "--root", str(tmp_path)],
                              input=json.dumps(observation()), text=True, capture_output=True, check=False,
                              env={**os.environ, "TRAJVIZ_OBS_PROMPT_CAPTURE": "1"})
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(write, range(12)))
    assert all(result.returncode == 0 and result.stdout.strip() == '{"ok":true}' for result in results)
    assert len(list(tmp_path.rglob("*.json"))) == 12
    assert all(path.stat().st_nlink == 1 for path in tmp_path.rglob("*.json"))


def test_limits_leave_explicit_gap(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "MAX_RECORDS", 1)
    file = Path(capture.write_record(str(tmp_path), observation()))
    with pytest.raises(capture.CaptureError, match="storage_limit"):
        capture.write_record(str(tmp_path), observation())
    assert json.loads((file.parent / "capture-health.json").read_text())["reason"] == "storage_limit"
    assert json.loads(file.read_text())["stage"] == "context"


def test_health_prefix_is_not_a_quota_bypass_and_marker_identity_is_reserved(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "MAX_RECORDS", 1)
    original = observation(record_id="capture-health-one")
    file = Path(capture.write_record(str(tmp_path), original))
    with pytest.raises(capture.CaptureError, match="storage_limit"):
        capture.write_record(str(tmp_path), observation(record_id="capture-health-two"))
    assert json.loads(file.read_text()) == original
    with pytest.raises(capture.CaptureError, match="reserved_identity"):
        capture.write_record(str(tmp_path), observation(record_id="capture-health"))


def test_abandoned_publications_are_cleaned_under_the_instance_lock(tmp_path):
    file = Path(capture.write_record(str(tmp_path), observation()))
    partial = file.parent / f".pending-{uuid4()}"
    partial.write_bytes(b"x" * 4096)
    partial.chmod(0o600)
    # A kill after link publication can leave two links to an otherwise valid record.
    linked = file.parent / f".pending-{uuid4()}"
    os.link(file, linked)
    assert file.stat().st_nlink == 2
    capture.write_record(str(tmp_path), observation())
    assert not list(file.parent.glob(".pending-*"))
    assert file.stat().st_nlink == 1
    assert len(list(file.parent.glob("*.json"))) == 2


def test_identity_and_snapshot_data_are_bounded(tmp_path):
    for fields in ({"record_id": "../escape"}, {"instance_id": "a/b"}, {"adapter": "other"}, {"session_id": ""},
                   {"data": {"notfinite": float("nan")}}, {"data": "x" * capture.MAX_RECORD_BYTES}):
        with pytest.raises((ValueError, TypeError)):
            capture.write_record(str(tmp_path), observation(**fields))
    assert not list(tmp_path.iterdir())


def test_component_evolution_and_effective_pi_state(tmp_path):
    system = {"role": "system", "content": "base", "sections": {"rules": "rule-v1", "skills": "skill"},
              "toolsAdded": [{"name": "read", "parameters": {"type": "object"}}, {"name": "write"}]}
    messages = [system, {"id": "user-1", "role": "user", "content": "work"}]
    first = observation(data={"messages": messages})
    capture.write_record(str(tmp_path), first)
    second = observation(sequence=2, observed_at="2026-11-25T00:00:02.000Z", data={"messages": [*messages,
        {"role": "system", "content": "", "sections": {"rules": "rule-v2", "skills": None},
         "toolsRemoved": [{"name": "write"}], "toolsAdded": [{"name": "grep"}]},
        {"role": "toolResult", "toolCallId": "call-1", "content": "file-v2"}]})
    capture.write_record(str(tmp_path), second)
    report = capture.inspect_records(tmp_path)
    before, after = report["records"]
    assert after["baseline_record_id"] == first["record_id"]
    assert after["changes"]["unchanged"] == [row["id"] for row in before["components"]]
    assert len(after["changes"]["added"]) == 2
    effective = after["effective_state_changes"]
    assert effective["modified"] == ["system:section:rules"]
    assert effective["added"] == ["tool:grep"]
    assert effective["removed"] == ["system:section:skills", "tool:write"]
    assert "content" not in after["components"][0]
    assert capture.inspect_records(tmp_path, include_content=True)["records"][0]["components"][0]["content"] == system


def test_duplicate_content_identity_modification_and_reordering():
    before = capture.components(observation(data={"tools": [{"name": "a", "description": "same"}, {"name": "b", "description": "same"}]}))
    after = capture.components(observation(data={"tools": [{"name": "b", "description": "same"}, {"name": "a", "description": "changed"}]}))
    delta = capture.changes(before, after)
    assert delta == {"added": [], "removed": [], "modified": ["tool:a:1"], "moved": ["tool:b:1", "tool:a:1"], "unchanged": ["tool:b:1"]}


def test_omitted_payload_is_a_gap_not_removal_and_continuations_are_labeled(tmp_path):
    first = observation(stage="provider.request", data={"payload": {"input": "original"}})
    capture.write_record(str(tmp_path), first)
    capture.write_record(str(tmp_path), observation(stage="provider.request", sequence=2, data={"payload": None, "omitted_reason": "size_limit"}))
    capture.write_record(str(tmp_path), observation(stage="provider.request", sequence=3,
        data={"payload": {"previous_response_id": "native-id", "input": "next"}}))
    records = sorted(capture.inspect_records(tmp_path)["records"], key=lambda row: row["correlation"].get("sequence", 0))
    gap = next(row for row in records if row["omitted_reason"])
    assert gap["changes"] is None
    continuation = next(row for row in records if row.get("remote_context"))
    assert continuation["baseline_record_id"] == first["record_id"]
    assert continuation["view"] == "provider_payload_at_hook"


def test_async_body_completion_does_not_reverse_native_observation_order(tmp_path):
    earlier = observation(adapter="opencode", sequence=2, observed_at="2026-11-25T00:00:03Z",
                          correlation={"observation_sequence": 1}, data={"messages": [{"role": "user", "content": "first"}]})
    later = observation(adapter="opencode", sequence=1, observed_at="2026-11-25T00:00:02Z",
                        correlation={"observation_sequence": 2}, data={"messages": [{"role": "user", "content": "next"}]})
    capture.write_record(str(tmp_path), later)
    capture.write_record(str(tmp_path), earlier)
    records = capture.inspect_records(tmp_path)["records"]
    assert [row["record_id"] for row in records] == [earlier["record_id"], later["record_id"]]
    assert records[1]["baseline_record_id"] == earlier["record_id"]


def test_pi_lineage_compares_across_runs_and_compaction_but_not_branch_switch(tmp_path):
    for sequence, (run, lineage, text) in enumerate([("one", "branch", "first"), ("two", "branch", "next"),
                                                   ("three", "fork", "fork")]):
        capture.write_record(str(tmp_path), observation(sequence=sequence, correlation={"run_id": run, "lineage_id": lineage},
            data={"messages": [{"role": "user", "content": text}]}))
    rows = capture.inspect_records(tmp_path)["records"]
    assert rows[1]["baseline_record_id"] == rows[0]["record_id"]
    assert rows[2]["baseline_record_id"] is None


def test_unlinked_components_and_control_frames_do_not_invent_context_changes(tmp_path):
    capture.write_record(str(tmp_path), observation(stage="component.section", data={"input": {"name": "one"}, "output": "first"}))
    capture.write_record(str(tmp_path), observation(stage="component.section", sequence=2, data={"input": {"name": "two"}, "output": "next"}))
    capture.write_record(str(tmp_path), observation(stage="provider.request", data={"payload": {"type": "response.cancel"}}))
    rows = capture.inspect_records(tmp_path)["records"]
    assert all(row["changes"] is None and row["baseline_record_id"] is None for row in rows)


def test_native_claude_requests_remain_separate_from_normalized_context(tmp_path):
    root = tmp_path / "capture"
    native = tmp_path / "native"
    native.mkdir(mode=0o700)
    original = observation(adapter="claude-code", stage="request.context")
    capture.write_record(str(root), original)
    payload = {"system": [{"type": "text", "text": "native system"}], "messages": [{"role": "user", "content": "native user"}],
               "tools": [{"name": "read", "input_schema": {"type": "object"}}], "model": "native-model"}
    request = native / "native.request.json"
    request.write_text(json.dumps(payload))
    index = {"timestamp": "2026-11-25T00:00:02Z", "session_id": original["session_id"], "query_source": "repl_main_thread",
             "request_id": "native-request", "message_uuid": "native-message", "request_file": str(request)}
    (native / "index.jsonl").write_text(json.dumps(index) + "\n" + json.dumps({**index, "session_id": "other"}) + "\n")
    report = capture.inspect_records(root, claude_raw=native, include_content=True)
    assert report["count"] == 2
    raw = next(row for row in report["records"] if row["stage"] == "provider.request")
    assert raw["correlation"]["request_id"] == "native-request"
    assert raw["correlation"]["step_linkage"] == "not_inferred"
    assert raw["composition"] == {"system": 1, "conversation": 1, "tool_definition": 1}
    assert raw["components"][0]["content"] == payload["system"][0]
    assert raw["baseline_record_id"] is None
    assert report == capture.inspect_records(root, claude_raw=native, include_content=True)
    (native / "index.jsonl").write_text(json.dumps({**index, "request_file": str(tmp_path / "outside.request.json")}))
    with pytest.raises(capture.CaptureError, match="invalid_native_index"):
        capture.inspect_records(root, claude_raw=native)


def test_native_threads_do_not_merge_parallel_agents_with_the_same_type(tmp_path):
    root, native = tmp_path / "capture", tmp_path / "native"
    native.mkdir(mode=0o700)
    capture.write_record(str(root), observation(adapter="claude-code"))
    entries = []
    for index, (message, previous) in enumerate([("a", None), ("b", None), ("a-next", "a")]):
        request = native / f"{index}.request.json"
        request.write_text(json.dumps({"model": "m", "messages": [{"role": "user", "content": message}],
                                       "thread": {"previous_message_id": previous} if previous else {"type": "new"}}))
        entries.append({"session_id": "private-session", "timestamp": f"2026-11-25T00:00:0{index}Z",
                        "request_file": request.name, "query_source": "agent:builtin:Explore", "message_id": message})
    (native / "index.jsonl").write_text("\n".join(json.dumps(entry) for entry in entries))
    rows = [row for row in capture.inspect_records(root, claude_raw=native)["records"] if row["stage"] == "provider.request"]
    assert rows[0]["baseline_record_id"] is rows[1]["baseline_record_id"] is None
    assert rows[2]["baseline_record_id"] == rows[0]["record_id"]
    assert rows[2]["remote_context"] == "retained_not_reconstructed"


def test_native_sibling_forks_use_their_actual_predecessor_not_last_sibling(tmp_path):
    root, native = tmp_path / "capture", tmp_path / "native"
    native.mkdir(mode=0o700)
    capture.write_record(str(root), observation(adapter="claude-code"))
    entries = []
    for index, (message, previous) in enumerate([("parent", None), ("a", "parent"), ("b", "parent"), ("a-next", "a")]):
        request = native / f"{index}.request.json"
        request.write_text(json.dumps({"messages": [{"role": "user", "content": message}],
                                       "thread": {"previous_message_id": previous} if previous else {"type": "new"}}))
        entries.append({"session_id": "private-session", "timestamp": f"2026-11-25T00:00:0{index}Z",
                        "request_file": request.name, "query_source": "agent:builtin:Explore", "message_id": message})
    (native / "index.jsonl").write_text("\n".join(json.dumps(entry) for entry in entries))
    rows = [row for row in capture.inspect_records(root, claude_raw=native)["records"] if row["stage"] == "provider.request"]
    assert rows[0]["baseline_record_id"] is None
    assert rows[1]["baseline_record_id"] == rows[2]["baseline_record_id"] == rows[0]["record_id"]
    assert rows[3]["baseline_record_id"] == rows[1]["record_id"]
    assert rows[3]["baseline_relation"] == "native_previous_message"


@pytest.mark.parametrize("kind", ["capture", "native_index", "native_request"])
def test_inspection_refuses_fifos_without_blocking(tmp_path, kind):
    root, native = tmp_path / "capture", tmp_path / "native"
    native.mkdir(mode=0o700)
    capture.write_record(str(root), observation(adapter="claude-code"))
    arguments = [sys.executable, "-I", capture.__file__, "inspect", "--root", str(root)]
    if kind == "capture":
        os.mkfifo(root / "hazard.json", 0o600)
    else:
        arguments.extend(["--claude-raw", str(native)])
        if kind == "native_index":
            os.mkfifo(native / "index.jsonl", 0o600)
        else:
            os.mkfifo(native / "hazard.request.json", 0o600)
            (native / "index.jsonl").write_text(json.dumps({"timestamp": "2026-11-25T00:00:00Z",
                "session_id": "private-session", "request_file": "hazard.request.json"}))
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=2, check=False)
    assert result.returncode == 1
    assert json.loads(result.stdout) == {"ok": False, "reason": "capture_unavailable"}


def test_inspection_limits_include_health_files_and_aggregate_bytes(tmp_path, monkeypatch):
    capture.write_record(str(tmp_path), observation())
    (tmp_path / "health.json").write_text(json.dumps({"stage": "capture.status"}))
    (tmp_path / "health.json").chmod(0o600)
    monkeypatch.setattr(capture, "MAX_RECORDS", 1)
    with pytest.raises(capture.CaptureError, match="excessive_input"):
        capture.inspect_records(tmp_path)
    monkeypatch.setattr(capture, "MAX_RECORDS", 4096)
    monkeypatch.setattr(capture, "MAX_INSTANCE_BYTES", 1)
    with pytest.raises(capture.CaptureError, match="excessive_input"):
        capture.inspect_records(tmp_path)


def test_inspection_does_not_create_directories_or_follow_links(tmp_path):
    missing = tmp_path / "missing"
    with pytest.raises(FileNotFoundError):
        capture.inspect_records(missing)
    assert not missing.exists()
    root = tmp_path / "capture"
    file = Path(capture.write_record(str(root), observation()))
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(OSError):
        capture.inspect_records(alias)
    file.chmod(0o644)
    with pytest.raises(capture.CaptureError, match="unsafe_input"):
        capture.inspect_records(root)


@pytest.mark.parametrize("scope", ["session", "instance", "run", "agent", "branch", "stage", "kind"])
def test_diffs_never_cross_capture_scopes(tmp_path, scope):
    first = observation()
    second = observation(sequence=2)
    if scope in {"run", "agent", "branch"}:
        second["correlation"] = {**second["correlation"], scope + "_id": "different"}
    else:
        second[{"session": "session_id", "instance": "instance_id"}.get(scope, scope)] = "different"
    capture.write_record(str(tmp_path), first)
    capture.write_record(str(tmp_path), second)
    assert all(row["baseline_record_id"] is None for row in capture.inspect_records(tmp_path)["records"])


@pytest.mark.parametrize("flag", [None, "", "0", "true", "1"])
def test_private_subprocess_explicit_opt_in_and_no_content_in_output(tmp_path, flag):
    env = {**os.environ}
    if flag is None:
        env.pop("TRAJVIZ_OBS_PROMPT_CAPTURE", None)
    else:
        env["TRAJVIZ_OBS_PROMPT_CAPTURE"] = flag
    result = subprocess.run([sys.executable, "-I", str(Path(capture.__file__)), "write", "--root", str(tmp_path)],
                            input=json.dumps(observation()), text=True, capture_output=True, env=env, check=False)
    assert "synthetic prompt" not in result.stdout + result.stderr
    assert (result.returncode == 0) == (flag == "1")
    assert bool(list(tmp_path.rglob("*.json"))) == (flag == "1")
