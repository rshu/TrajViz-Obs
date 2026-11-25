"""Session-scoped, read-only data for the prompt explorer.

The dashboard supplies host-authorized session IDs. Browser uploads, metadata,
and control values must never choose capture paths or authorize a session.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path

import prompt_capture as capture

MAX_UI_BYTES = 32 * 1024 * 1024
MAX_UI_RECORDS = 1024
MAX_SESSIONS = 64
MAX_DIFF_CHARS = 64 * 1024
MAX_DIFF_LINES = 1000
ADAPTER_LABELS = {"pi": "Pi", "claude-code": "Claude Code", "opencode": "OpenCode"}
STAGE_LABELS = {
    "context": "Engine context", "request.context": "Engine context", "provider.request": "Provider payload",
    "response": "Response evidence", "request.complete": "Response evidence", "lifecycle.compaction": "Compaction",
    "capture.gap": "Capture gap",
    "component.compose": "System composition", "component.section": "System sections",
    "component.context": "Initial context", "component.attachment": "Attachments",
    "component.skill": "Expanded skills", "component.tool": "Tool descriptions",
}
_GAP_REASONS = {"storage_limit", "queue_full", "record_too_large", "context_unavailable", "capture_unavailable",
                "write_failed", "size_limit", "read_timeout", "read_failed", "capture_busy", "invalid_json"}


def _mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def _empty(status: str, message: str) -> dict:
    return {"status": status, "message": message, "records": [], "lanes": [], "markers": [], "capture_gaps": [],
            "limits": {"records": MAX_UI_RECORDS, "bytes": MAX_UI_BYTES}}


@contextmanager
def _existing_child(parent: int, name: str):
    descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        capture._private(os.fstat(descriptor))
        yield descriptor
    finally:
        os.close(descriptor)


def _selected_roots(root: Path, adapter: str, sessions: set[str]) -> list[tuple[Path, str]]:
    """Preflight only the fixed adapter/session namespaces, without following links."""
    paths = []
    count, total = 0, 0

    def walk(directory: int, depth: int = 0):
        nonlocal count, total
        if depth > 2:
            raise capture.CaptureError("excessive_input_depth")
        for name in os.listdir(directory):
            metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                raise capture.CaptureError("unsafe_input")
            if stat.S_ISDIR(metadata.st_mode):
                with _existing_child(directory, name) as child:
                    walk(child, depth + 1)
            elif name.endswith(".json"):
                if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                        or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077):
                    raise capture.CaptureError("unsafe_input")
                count += 1
                total += metadata.st_size
                if count > MAX_UI_RECORDS or total > MAX_UI_BYTES or metadata.st_size > capture.MAX_RECORD_BYTES + 1:
                    raise capture.CaptureError("excessive_input")

    with capture.private_directory(str(root), create=False) as descriptor:
        try:
            with _existing_child(descriptor, adapter) as adapter_directory:
                for session in sorted(sessions):
                    namespace = hashlib.sha256(session.encode()).hexdigest()[:32]
                    try:
                        with _existing_child(adapter_directory, namespace) as session_directory:
                            walk(session_directory)
                    except FileNotFoundError:
                        continue
                    paths.append((root / adapter / namespace, session))
        except FileNotFoundError:
            pass
    return paths


def _selection_id(record: dict) -> str:
    return capture.fingerprint([record[key] for key in ("adapter", "session_id", "instance_id", "record_id")])[:32]


def _lane(record: dict) -> tuple[str, str]:
    correlation = _mapping(record.get("correlation"))
    lineage = correlation.get("lineage_id", correlation.get("run_id"))
    identity = [record["adapter"], record["session_id"], record["instance_id"], record["stage"], record.get("kind"),
                correlation.get("agent_id"), lineage, correlation.get("branch_id")]
    stage = STAGE_LABELS.get(record["stage"], "Component observations" if record["stage"].startswith("component.") else record["stage"])
    agent = correlation.get("agent_id")
    scope = str(agent) if agent else "Main" if correlation.get("scope") == "main" else "Session"
    purpose = "Unlinked observation" if record["stage"].startswith("component.") else record.get("kind") or "Unknown purpose"
    label = f"{ADAPTER_LABELS[record['adapter']]} · {stage} · {purpose} · {scope}"
    return capture.fingerprint(identity)[:32], label


def _gap(record: dict, adapter: str, sessions: set[str]) -> dict:
    """Health notices are not a route for arbitrary raw record data into the UI."""
    result = {"stage": record.get("stage", "capture.status"), "adapter": adapter}
    session = record.get("session_id")
    if session in sessions:
        result["session_id"] = session
    elif len(sessions) == 1:
        result["session_id"] = next(iter(sessions))
    for key in ("instance_id", "observed_at"):
        if isinstance(record.get(key), str):
            result[key] = record[key]
    data = _mapping(record.get("data"))
    reason = record.get("reason") or data.get("reason")
    result["reason"] = reason if reason in _GAP_REASONS else "capture_incomplete"
    for key in ("omitted_records", "failed", "dropped"):
        value = data.get(key)
        if type(value) is int and value >= 0:
            result[key] = value
    return result


def load_dataset(capture_root: Path | None, *, session_ids: set[str], adapter: str | None = None,
                 claude_raw: Path | None = None) -> dict:
    """Read a trusted launch root for explicitly authorized native sessions only.

    The caller owns launch opt-in and original-trajectory authorization. Passing
    ``None`` disables the reader without filesystem access. There is no file
    browser and no session discovery from capture contents.
    """
    if capture_root is None:
        return _empty("disabled", "Prompt capture is off. Enable it in the agent's launch environment to record new requests.")
    if adapter not in ADAPTER_LABELS or not isinstance(session_ids, (set, frozenset)) or not session_ids or len(session_ids) > MAX_SESSIONS or any(
            not isinstance(value, str) or not value or len(value) > 4096 or "\0" in value for value in session_ids):
        return _empty("unavailable", "Prompt capture is available for the dashboard's original native session only.")
    try:
        root = Path(capture_root)
        if not root.is_absolute():
            return _empty("unavailable", "The prompt capture directory is not available.")
        paths = _selected_roots(root, adapter, session_ids)
        records, gaps = [], []
        consumed_bytes = 0
        consumed_records = 0
        projected_bytes = 0
        for path, session in paths:
            report = capture.inspect_records(path, include_content=True,
                claude_raw=claude_raw if adapter == "claude-code" else None,
                max_records=MAX_UI_RECORDS - consumed_records, max_bytes=MAX_UI_BYTES - consumed_bytes,
                session_ids={session})
            consumed_bytes += report["inspection"]["bytes"]
            consumed_records += report["inspection"]["records"]
            projected_bytes += len(capture.encoded(report))
            if consumed_bytes > MAX_UI_BYTES or projected_bytes > MAX_UI_BYTES or consumed_records > MAX_UI_RECORDS:
                raise capture.CaptureError("excessive_input")
            records.extend(row for row in report["records"] if row["adapter"] == adapter and row["session_id"] == session)
            gaps.extend(_gap(row, adapter, {session}) for row in report["capture_gaps"]
                        if row.get("session_id") is None or row.get("session_id") == session)
    except FileNotFoundError:
        return _empty("empty", "No prompt observations have been recorded for this session yet.")
    except capture.CaptureError as error:
        if str(error).startswith("excessive_"):
            return _empty("limit", "This session exceeds the explorer's safe input limit. Use the JSON inspector for the full capture.")
        return _empty("unavailable", "Prompt observations could not be read safely. The capture files were not changed.")
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
        return _empty("unavailable", "Prompt observations could not be read safely. The capture files were not changed.")
    if not records:
        result = _empty("empty", "No prompt observations have been recorded for this session yet.")
        result["capture_gaps"] = gaps
        return result
    lookup = {}
    for row in records:
        row["selection_id"] = _selection_id(row)
        key = (row["adapter"], row["session_id"], row["instance_id"], row["record_id"])
        if key in lookup:
            return _empty("unavailable", "Prompt observations contain duplicate identities and cannot be compared safely.")
        lookup[key] = row["selection_id"]
    lanes = {}
    for row in records:
        row["baseline_selection_id"] = lookup.get((row["adapter"], row["session_id"], row["instance_id"], row.get("baseline_record_id")))
        lane_id, label = _lane(row)
        row["lane_id"] = lane_id
        lane = lanes.setdefault(lane_id, {"id": lane_id, "label": label, "adapter": row["adapter"],
            "session_id": row["session_id"], "stage": row["stage"], "kind": row.get("kind"),
            "agent_id": _mapping(row.get("correlation")).get("agent_id"),
            "lineage_id": _mapping(row.get("correlation")).get("lineage_id"), "record_ids": []})
        lane["record_ids"].append(row["selection_id"])
        row["lane_index"] = len(lane["record_ids"])
    result = _empty("ready", "Sizes show serialized UTF-8 bytes. Component token counts are not measured.")
    labels = [lane["label"] for lane in lanes.values()]
    for lane in lanes.values():
        if labels.count(lane["label"]) > 1:
            record = next(row for row in records if row["lane_id"] == lane["id"])
            lineage = _mapping(record.get("correlation")).get("lineage_id", _mapping(record.get("correlation")).get("run_id"))
            suffix = f"{lane['session_id'][-8:]}/{record['instance_id'][-6:]}"
            if isinstance(lineage, str):
                suffix += "/" + lineage[-6:]
            lane["label"] += " · " + suffix
    result.update({"records": records, "lanes": list(lanes.values()), "capture_gaps": gaps,
                   "markers": [{key: row[key] for key in ("selection_id", "adapter", "session_id", "instance_id", "stage", "observed_at", "correlation")}
                               for row in records if row["stage"].startswith("lifecycle.") or row["stage"] == "capture.gap"]})
    return result


def select_record(dataset: dict, selection_id: str | None = None, *, effective: bool = False) -> dict:
    """Select only an identity already present in the authorized server-side dataset."""
    records = dataset.get("records", [])
    candidates = [row for row in records if row.get("selection_id") == selection_id]
    if not candidates and selection_id:
        candidates = [row for row in records if row.get("record_id") == selection_id]
    if selection_id and len(candidates) != 1:
        return {"record": None, "baseline": None, "timeline": [], "components": [], "effective": False}
    record = candidates[0] if candidates else next((row for row in records if row["stage"] in {"context", "request.context"}), records[0] if records else None)
    if record is None:
        return {"record": None, "baseline": None, "timeline": [], "components": [], "effective": False}
    baseline = next((row for row in records if row.get("selection_id") == record.get("baseline_selection_id")), None)
    use_effective = effective and "effective_state" in record
    field = "effective_state" if use_effective else "components"
    delta = record.get("effective_state_changes" if use_effective else "changes")
    before = {row["id"]: row for row in (baseline or {}).get(field, [])}
    after = {row["id"]: row for row in record.get(field, [])}
    components = []
    for identity in [*after, *(key for key in before if key not in after)]:
        tags = [key for key in ("added", "removed", "modified", "moved", "unchanged") if identity in (delta or {}).get(key, [])]
        if baseline is None and record.get("comparison_status") == "initial_baseline":
            tags = ["baseline"]
        elif delta is None:
            tags = ["uncompared"]
        components.append({"id": identity, "before": before.get(identity), "after": after.get(identity), "changes": tags})
    return {"record": record, "baseline": baseline, "timeline": [row for row in records if row.get("lane_id") == record["lane_id"]],
            "components": components, "effective": use_effective}


def component_detail(dataset: dict, selection_id: str, component_id: str, *, effective: bool = False) -> dict | None:
    """A bounded text diff for one component; no reads or mutations."""
    selected = select_record(dataset, selection_id, effective=effective)
    row = next((row for row in selected["components"] if row["id"] == component_id), None)
    if row is None:
        return None

    def text(component):
        value = component.get("content") if component else ""
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)

    before, after = text(row["before"]), text(row["after"])
    clipped = len(before) > MAX_DIFF_CHARS or len(after) > MAX_DIFF_CHARS
    before_lines = before[:MAX_DIFF_CHARS].splitlines()
    after_lines = after[:MAX_DIFF_CHARS].splitlines()
    lines = []
    for line in difflib.unified_diff(before_lines, after_lines, fromfile="Previous", tofile="Selected", lineterm=""):
        if len(lines) >= MAX_DIFF_LINES:
            clipped = True
            break
        lines.append(line)
    return {**row, "before_text": before[:MAX_DIFF_CHARS], "after_text": after[:MAX_DIFF_CHARS],
            "diff_lines": lines, "truncated": clipped, "limit_chars": MAX_DIFF_CHARS}
