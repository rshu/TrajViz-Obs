"""Live capture refreshes preserve scope and tolerate in-progress publications."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from agent_trajviz import dashboard

import prompt_capture as capture
from trajviz_obs import prompt_explorer
from trajviz_obs.compat import load_run
from trajviz_obs.prompt_explorer_data import select_record

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Private capture storage requires POSIX")
BASE = 1_790_000_000_000
SESSION = "live-parent"
FIRST = "VISIBLE_INITIAL_PROMPT"
FOLLOWUP = "VISIBLE_FOLLOWUP_FILE_RESULT"
CHILD = "VISIBLE_NEW_CHILD_PROMPT"
UNRELATED = "UNRELATED_PRIVATE_SESSION"


def _native(adapter: str, session: str = SESSION, *, child: str | None = None):
    if adapter == "pi":
        return [
            {"type": "session", "version": 3, "id": session, "cwd": "/controlled",
             "timestamp": "2026-11-25T00:00:00Z"},
            {"type": "message", "id": "user", "parentId": None, "timestamp": "2026-11-25T00:00:01Z",
             "message": {"role": "user", "content": "Inspect the package", "timestamp": BASE}},
            {"type": "message", "id": "assistant", "parentId": "user", "timestamp": "2026-11-25T00:00:02Z",
             "message": {"role": "assistant", "api": "openai-completions", "provider": "test", "model": "test",
                         "content": [{"type": "text", "text": "Inspected"}], "timestamp": BASE + 1_000,
                         "stopReason": "stop", "usage": {"input": 5, "output": 2, "totalTokens": 7}}},
        ]
    if adapter == "claude-code":
        return [
            {"type": "user", "uuid": "user", "parentUuid": None, "sessionId": session,
             "timestamp": "2026-11-25T00:00:01Z", "message": {"role": "user", "content": "Inspect the package"}},
            {"type": "assistant", "uuid": "assistant", "parentUuid": "user", "sessionId": session,
             "timestamp": "2026-11-25T00:00:02Z", "message": {"role": "assistant", "model": "test",
             "content": [{"type": "text", "text": "Inspected"}], "usage": {"input_tokens": 5, "output_tokens": 2}}},
        ]
    content = [{"type": "text", "text": "Inspected"}]
    if child:
        content.append({"type": "tool", "id": "spawn", "name": "subagent",
                        "time": {"created": BASE + 200, "ran": BASE + 300, "completed": BASE + 900},
                        "state": {"status": "completed", "input": {"prompt": "Review this"},
                                  "content": [{"type": "text", "text": "Reviewed"}], "metadata": {"sessionID": child}}})
    return {"source_format": "opencode_v2", "schema_version": 1,
            "session": {"id": session, "title": "Live lifecycle"},
            "messages": [{"type": "user", "id": "user", "text": "Inspect the package"},
                         {"type": "assistant", "id": "assistant", "time": {"created": BASE, "completed": BASE + 1_000},
                          "content": content, "tokens": {"input": 5, "output": 2}}],
            "children": [_native("opencode", child)] if child else []}


def _source(path: Path, adapter: str, session: str = SESSION, *, child: str | None = None) -> None:
    value = _native(adapter, session, child=child)
    body = "\n".join(json.dumps(row) for row in value) + "\n" if adapter == "claude-code" else json.dumps(value)
    temporary = path.with_suffix(".pending")
    temporary.write_text(body)
    temporary.replace(path)


def _record(root: Path, adapter: str, number: int, *, session: str = SESSION,
            instance: str = "instance-one", text: str = FIRST, stage: str | None = None,
            lineage: str = "main-lineage", **fields) -> Path:
    return Path(capture.write_record(str(root), {
        "schema_version": 1, "adapter": adapter, "session_id": session, "instance_id": instance,
        "record_id": f"record-{number}", "sequence": number, "observed_at": f"2026-11-25T00:00:{number:02}Z",
        "stage": stage or ("request.context" if adapter == "claude-code" else "context"), "kind": "primary",
        "correlation": {"lineage_id": lineage, "agent_id": "main" if session == SESSION else "reviewer"},
        "data": {"messages": [{"role": "user", "content": text}]}, **fields,
    }))


def _callback(app, name):
    return next(entry.fn for entry in app.fns.values() if entry.api_name == name)


def _mounted(source: Path, root: Path):
    app = dashboard.insight_app()
    assert prompt_explorer.mount_prompt_explorer(app, transcript_path=str(source), session_id=SESSION, capture_root=root)
    return _callback(app, "obs_prompt_refresh"), _callback(app, "obs_prompt_request")


@pytest.mark.parametrize("adapter", ["pi", "claude-code", "opencode"])
def test_dashboard_opened_before_capture_sees_appends_and_preserves_selected_request(tmp_path, adapter):
    source, root = tmp_path / "live.jsonl", tmp_path / "captures"
    _source(source, adapter)
    run = load_run(str(source))
    refresh, choose = _mounted(source, root)
    before = refresh(run, {}, "physical")
    assert before[0]["records"] == []
    first = _record(root, adapter, 1)
    _record(root, adapter, 1, session="unrelated", text=UNRELATED)
    initial = refresh(run, before[4], "physical")
    assert len(initial[0]["records"]) == 1 and FIRST in initial[5]
    selected = initial[4]["request"]
    _record(root, adapter, 2, text=FOLLOWUP)
    snapshot = {file: hashlib.sha256(file.read_bytes()).digest() for file in (source, *root.rglob("*.json"))}
    following = refresh(run, initial[4], "physical", initial[0])
    latest = following[0]["records"][-1]
    assert following[4]["request"] == latest["selection_id"]
    pinned = choose(run, following[0], following[4], "physical", selected)
    assert pinned[4]["follow_latest"] is False
    later = refresh(run, pinned[4], "physical", pinned[0])
    assert len(later[0]["records"]) == 2 and later[4]["request"] == selected
    assert latest["baseline_selection_id"] == selected
    detail = choose(run, later[0], later[4], "physical", latest["selection_id"])
    assert FOLLOWUP in detail[5] and UNRELATED not in json.dumps(detail, default=str)
    assert all(hashlib.sha256(file.read_bytes()).digest() == digest for file, digest in snapshot.items())
    assert first.is_file()


def test_refresh_ignores_private_pending_files_without_publishing_partial_content(tmp_path):
    source, root = tmp_path / "live.json", tmp_path / "captures"
    _source(source, "opencode")
    first = _record(root, "opencode", 1)
    refresh, _ = _mounted(source, root)
    run = load_run(str(source))
    pending = first.parent / ".pending-00000000-0000-0000-0000-000000000001"
    pending.write_text('{"data":{"messages":["UNPUBLISHED_PRIVATE_PARTIAL')
    pending.chmod(0o600)
    result = refresh(run, {}, "physical")
    assert result[0]["status"] == "ready"
    assert [row["record_id"] for row in result[0]["records"] if row.get("stage") == "context"] == ["record-1"]
    assert "UNPUBLISHED_PRIVATE_PARTIAL" not in json.dumps(result, default=str)
    assert pending.read_text().endswith("UNPUBLISHED_PRIVATE_PARTIAL")


def test_publication_hardlink_window_fails_closed_then_recovers_without_changing_files(tmp_path):
    source, root = tmp_path / "live.json", tmp_path / "captures"
    _source(source, "opencode")
    first = _record(root, "opencode", 1)
    refresh, _ = _mounted(source, root)
    run = load_run(str(source))
    initial = refresh(run, {}, "physical")
    pending = first.parent / ".pending-00000000-0000-0000-0000-000000000001"
    os.link(first, pending)
    in_progress = refresh(run, initial[4], "physical")
    assert in_progress[0]["status"] == "unavailable" and not in_progress[0]["records"]
    assert FIRST not in in_progress[5]
    pending.unlink()
    recovered = refresh(run, in_progress[4], "physical")
    assert recovered[0]["status"] == "ready" and FIRST in recovered[5]


def test_partial_json_is_a_visible_gap_and_complete_replacement_recovers(tmp_path):
    source, root = tmp_path / "live.json", tmp_path / "captures"
    _source(source, "opencode")
    first = _record(root, "opencode", 1)
    refresh, _ = _mounted(source, root)
    run = load_run(str(source))
    partial = first.parent / "record-2.json"
    partial.write_text('{"data":{"messages":["UNFINISHED_PRIVATE_BODY')
    partial.chmod(0o600)
    result = refresh(run, {}, "physical")
    assert result[0]["status"] == "ready"
    assert [row["record_id"] for row in result[0]["records"] if row.get("stage") == "context"] == ["record-1"]
    assert any(gap["reason"] == "invalid_json" for gap in result[0]["capture_gaps"])
    assert "UNFINISHED_PRIVATE_BODY" not in json.dumps(result, default=str)
    partial.unlink()
    _record(root, "opencode", 2, text=FOLLOWUP)
    recovered = refresh(run, result[4], "physical")
    assert len(recovered[0]["records"]) == 2 and not recovered[0]["capture_gaps"]


def test_restarted_or_rebranched_captures_do_not_diff_against_a_previous_execution(tmp_path):
    source, root = tmp_path / "live.json", tmp_path / "captures"
    _source(source, "pi")
    _record(root, "pi", 1)
    _record(root, "pi", 2, text="Retried request")
    _record(root, "pi", 3, text="Resumed process", instance="instance-two")
    _record(root, "pi", 4, text="Forked request", lineage="fork-lineage")
    result = prompt_explorer.scoped_dataset(load_run(str(source)), source, root, session_id=SESSION)
    records = {record["record_id"]: record for record in result["records"]}
    assert records["record-2"]["baseline_selection_id"] == records["record-1"]["selection_id"]
    for identity in ("record-3", "record-4"):
        record = records[identity]
        assert record["baseline_selection_id"] is None
        assert all(component["changes"] == ["baseline"] for component in select_record(result, record["selection_id"])["components"])


@pytest.mark.parametrize("adapter", ["pi", "claude-code", "opencode"])
def test_native_provider_omissions_block_comparison_across_the_missing_request(tmp_path, adapter):
    source, root = tmp_path / "live.jsonl", tmp_path / "captures"
    _source(source, adapter)
    _record(root, adapter, 1, stage="provider.request", data={"payload": {"messages": [{"role": "user", "content": FIRST}]}})
    _record(root, adapter, 2, stage="provider.request", data={"payload": None, "omitted_reason": "read_timeout"})
    _record(root, adapter, 3, stage="provider.request", data={"payload": {"messages": [{"role": "user", "content": FOLLOWUP}]}})
    refresh, _ = _mounted(source, root)
    result = refresh(load_run(str(source)), {}, "physical")
    records = {row["record_id"]: row for row in result[0]["records"]}
    assert records["record-2"]["comparison_status"] == "capture_incomplete"
    after = records["record-3"]
    assert after["comparison_status"] == "page_boundary"
    assert after["baseline_selection_id"] is None and after["changes"] is None
    assert all(component["changes"] == ["uncompared"] for component in select_record(result[0], after["selection_id"])["components"])


@pytest.mark.parametrize("adapter", ["pi", "claude-code", "opencode"])
def test_active_writer_loss_is_visible_deduplicated_and_blocks_only_the_new_gap(tmp_path, adapter):
    source, root = tmp_path / "live.jsonl", tmp_path / "captures"
    _source(source, adapter)
    _record(root, adapter, 1, capture_health={"dropped": 0, "failed": 0})
    _record(root, adapter, 2, text=FOLLOWUP, capture_health={"dropped": 2, "failed": 1})
    _record(root, adapter, 3, text="Later complete request", capture_health={"dropped": 2, "failed": 1})
    refresh, _ = _mounted(source, root)
    result = refresh(load_run(str(source)), {}, "physical")
    records = {row["record_id"]: row for row in result[0]["records"]}
    assert all(row["stage"] != "capture.status" for row in result[0]["records"])
    notices = [row for row in result[0]["capture_gaps"] if row.get("reason") == "writer_loss"]
    assert len(notices) == 1
    assert notices[0]["scope"] == "writer"
    assert notices[0]["attribution"] == "request_unknown"
    assert notices[0]["dropped"] == 2 and notices[0]["failed"] == 1
    assert "writer" in result[5].lower()
    after_gap = records["record-2"]
    assert after_gap["comparison_status"] == "capture_gap"
    assert after_gap["baseline_selection_id"] is None and after_gap["changes"] is None
    after_complete = records["record-3"]
    assert after_complete["comparison_status"] == "compared"
    assert after_complete["baseline_selection_id"] == after_gap["selection_id"]


@pytest.mark.parametrize("adapter", ["pi", "claude-code", "opencode"])
def test_invalid_writer_health_fields_are_not_promoted_to_explorer_metadata(tmp_path, adapter):
    source, root = tmp_path / "live.jsonl", tmp_path / "captures"
    _source(source, adapter)
    _record(root, adapter, 1, capture_health={"dropped": -1, "failed": True, "private": "PRIVATE_HEALTH_METADATA"})
    _record(root, adapter, 2, capture_health={"dropped": 1.5, "failed": "PRIVATE_HEALTH_METADATA"})
    refresh, _ = _mounted(source, root)
    result = refresh(load_run(str(source)), {}, "physical")
    assert result[0]["status"] == "ready"
    assert "PRIVATE_HEALTH_METADATA" not in json.dumps(result, default=str)
    assert not any(row.get("reason") == "writer_loss" for row in result[0]["capture_gaps"])
    assert all(not row.get("capture_health") for row in result[0]["records"])


def test_live_refresh_discovers_new_native_child_without_authorizing_unrelated_sessions(tmp_path):
    source, root = tmp_path / "live.json", tmp_path / "captures"
    _source(source, "opencode")
    run = load_run(str(source))
    _record(root, "opencode", 1)
    refresh, _ = _mounted(source, root)
    initial = refresh(run, {}, "physical")
    _record(root, "opencode", 2, session="new-child", text=CHILD)
    _record(root, "opencode", 3, session="unrelated", text=UNRELATED)
    _source(source, "opencode", child="new-child")
    result = refresh(run, initial[4], "physical")
    serialized = json.dumps(result, default=str)
    assert CHILD in serialized and UNRELATED not in serialized
    assert {record["session_id"] for record in result[0]["records"]} == {SESSION, "new-child"}


@pytest.mark.parametrize("replacement", ["different-session", "different-adapter", "partial"])
def test_live_refresh_revalidates_replaced_or_incomplete_native_source(tmp_path, replacement):
    source, root = tmp_path / "live.json", tmp_path / "captures"
    _source(source, "opencode")
    run = load_run(str(source))
    _record(root, "opencode", 1)
    _record(root, "pi", 1, text=UNRELATED)
    refresh, _ = _mounted(source, root)
    initial = refresh(run, {}, "physical")
    assert FIRST in initial[5]
    if replacement == "different-session":
        _source(source, "opencode", "different-session")
    elif replacement == "different-adapter":
        _source(source, "pi")
    else:
        source.write_text('{"source_format":"opencode_v2","messages":[')
    result = refresh(run, initial[4], "physical")
    assert result[0]["records"] == []
    assert not any(text in json.dumps(result, default=str) for text in (FIRST, UNRELATED))
    _source(source, "opencode")
    assert FIRST in refresh(run, result[4], "physical")[5]
