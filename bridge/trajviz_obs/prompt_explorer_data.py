"""Session-scoped, read-only data for the prompt explorer.

The dashboard supplies host-authorized session IDs. Browser uploads, metadata,
and control values must never choose capture paths or authorize a session.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import secrets
import stat
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path

import prompt_capture as capture

MAX_UI_BYTES = 32 * 1024 * 1024
MAX_UI_RECORDS = 1024
MAX_UI_COMPONENTS = 8192
MAX_SESSIONS = 64
MAX_CATALOG_ENTRIES = 65536
MAX_CATALOG_BYTES = 32 * 1024 * 1024
MAX_NATIVE_INDEX_BYTES = 4 * 1024 * 1024
MAX_CURSORS = 256
CURSOR_TTL_SECONDS = 3600
_CURSORS: OrderedDict[str, tuple] = OrderedDict()
_CURSOR_LOCK = threading.Lock()
MAX_DIFF_CHARS = 64 * 1024
MAX_DIFF_LINES = 1000
ADAPTER_LABELS = {"pi": "Pi", "claude-code": "Claude Code", "opencode": "OpenCode", "deveco": "DevEco Code"}
STAGE_LABELS = {
    "context": "Engine context", "request.context": "Engine context", "provider.request": "Provider payload",
    "response": "Response evidence", "request.complete": "Response evidence", "lifecycle.compaction": "Compaction",
    "capture.gap": "Capture gap",
    "component.compose": "System composition", "component.section": "System sections",
    "component.context": "Initial context", "component.attachment": "Attachments",
    "component.skill": "Expanded skills", "component.tool": "Tool descriptions",
}
_GAP_REASONS = {"storage_limit", "queue_full", "record_too_large", "context_unavailable", "capture_unavailable",
                "write_failed", "size_limit", "read_timeout", "read_failed", "capture_busy", "invalid_json",
                "record_exceeds_page_budget", "record_structure_limit", "writer_loss"}


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


def _file_metadata(metadata, *, native: bool = False) -> None:
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_uid != os.getuid()
            or not native and stat.S_IMODE(metadata.st_mode) & 0o077):
        raise capture.CaptureError("unsafe_input")


def _read_file(path: Path, limit: int, *, native: bool = False, expected=None) -> bytes:
    with capture.private_directory(str(path.parent), create=False) as directory:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            _file_metadata(metadata, native=native)
            if expected is not None and (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns) != expected:
                raise capture.CaptureError("capture_busy")
            if metadata.st_size > limit:
                raise capture.CaptureError("excessive_input")
            value = source.read(limit + 1)
            if len(value) > limit:
                raise capture.CaptureError("excessive_input")
            return value


def _catalog(root: Path, adapter: str, sessions: set[str], native_root: Path | None) -> list[dict]:
    """Bounded metadata only: never decode an unselected prompt body."""
    rows, catalog_bytes = [], 0

    def add(path, metadata, session, kind="capture", entry=None, sequence=None):
        nonlocal catalog_bytes
        _file_metadata(metadata, native=kind == "native")
        catalog_bytes += len(str(path).encode()) + 256 + (len(capture.encoded(entry)) if entry else 0)
        if len(rows) >= MAX_CATALOG_ENTRIES or catalog_bytes > MAX_CATALOG_BYTES:
            raise capture.CaptureError("excessive_catalog")
        identity = capture.fingerprint([kind, str(path), session, sequence])
        rows.append({"path": path, "session": session, "kind": kind, "entry": entry, "sequence": sequence,
                     "size": metadata.st_size, "key": (metadata.st_mtime_ns, identity),
                     "expected": (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)})

    def walk(directory, path, session, depth=0):
        if depth > 2:
            raise capture.CaptureError("excessive_input_depth")
        with os.scandir(directory) as entries:
            for entry in entries:
                metadata = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode):
                    raise capture.CaptureError("unsafe_input")
                if stat.S_ISDIR(metadata.st_mode):
                    with _existing_child(directory, entry.name) as child:
                        walk(child, path / entry.name, session, depth + 1)
                elif entry.name.endswith(".json"):
                    add(path / entry.name, metadata, session)

    with capture.private_directory(str(root), create=False) as descriptor:
        try:
            with _existing_child(descriptor, adapter) as adapter_directory:
                for session in sorted(sessions):
                    namespace = hashlib.sha256(session.encode()).hexdigest()[:32]
                    try:
                        with _existing_child(adapter_directory, namespace) as session_directory:
                            walk(session_directory, root / adapter / namespace, session)
                    except FileNotFoundError:
                        continue
        except FileNotFoundError:
            pass
    if adapter == "claude-code" and native_root is not None:
        index = _read_file(native_root / "index.jsonl", MAX_NATIVE_INDEX_BYTES, native=True)
        catalog_bytes += len(index)
        if catalog_bytes > MAX_CATALOG_BYTES:
            raise capture.CaptureError("excessive_catalog")
        with capture.private_directory(str(native_root), create=False) as directory:
            for sequence, line in enumerate(index.splitlines()):
                entry = json.loads(line)
                if not isinstance(entry, dict) or entry.get("session_id") not in sessions:
                    continue
                filename = entry.get("request_file")
                if not isinstance(filename, str):
                    raise capture.CaptureError("invalid_native_index")
                target = Path(filename)
                if (target.is_absolute() and target.parent != native_root) or target.name != str(target) and not target.is_absolute():
                    raise capture.CaptureError("invalid_native_index")
                # Ignore unrecognized index fields; none can become prompt content or a UI label.
                entry = {key: entry[key] for key in ("timestamp", "session_id", "query_source", "model", "request_id",
                         "message_id", "message_uuid", "request_file", "response_file") if key in entry}
                if len(capture.encoded(entry)) > 16384:
                    raise capture.CaptureError("invalid_native_index")
                metadata = os.stat(target.name, dir_fd=directory, follow_symlinks=False)
                add(native_root / target.name, metadata, entry["session_id"], "native", entry, sequence)
    return sorted(rows, key=lambda row: row["key"])


def _remember_cursor(scope: str, first: tuple, last: tuple) -> str:
    token = secrets.token_urlsafe(24)
    now = time.monotonic()
    with _CURSOR_LOCK:
        for old, value in list(_CURSORS.items()):
            if now - value[0] > CURSOR_TTL_SECONDS:
                del _CURSORS[old]
        _CURSORS[token] = (now, scope, first, last)
        while len(_CURSORS) > MAX_CURSORS:
            _CURSORS.popitem(last=False)
    return token


def _cursor_window(scope: str, token: str) -> tuple:
    with _CURSOR_LOCK:
        value = _CURSORS.get(token) if isinstance(token, str) else None
        if value is None or value[1] != scope or time.monotonic() - value[0] > CURSOR_TTL_SECONDS:
            raise capture.CaptureError("invalid_cursor")
        _CURSORS.move_to_end(token)
        return value[2], value[3]


def _omission(item: dict, adapter: str, reason: str) -> dict:
    return {"schema_version": 1, "adapter": adapter, "session_id": item["session"],
            "instance_id": "page-omission", "record_id": capture.fingerprint(item["key"])[:32],
            "sequence": 0, "observed_at": "", "stage": "capture.gap", "kind": "unavailable",
            "correlation": {}, "data": {"reason": reason, "provenance": {"boundary": "page_file_read", "payload_exposed": False}}}


def _component_entries(row: dict) -> int:
    """Cheap bound before component dictionaries, hashes and diff lists allocate."""
    data = _mapping(row.get("data"))
    if isinstance(data.get("payload"), dict):
        data = data["payload"]
        if isinstance(data.get("response"), dict):
            data = data["response"]
    system = data.get("system", data.get("instructions"))
    messages = data.get("messages", data.get("input", []))
    tools = data.get("tools", [])
    total = len(system) if isinstance(system, list) else int(system is not None)
    total += len(messages) if isinstance(messages, list) else int(isinstance(messages, str))
    total += len(tools) if isinstance(tools, (dict, list)) else 0
    if row["adapter"] == "pi" and isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict) and message.get("role") == "system":
                total += len(_mapping(message.get("sections"))) + len(message.get("toolsAdded", [])) + int(bool(message.get("content")))
            if total > MAX_UI_COMPONENTS:
                break
    return max(total, 1)


def _record_key(row: dict, record_id: str | None = None) -> tuple:
    return row["adapter"], row["session_id"], row["instance_id"], row["record_id"] if record_id is None else record_id


def _read_page(items: list[dict], adapter: str, native_root: Path | None) -> tuple[list[dict], list[dict], dict]:
    records, gaps, positions = [], [], {}
    for position, item in enumerate(items):
        if item["size"] > capture.MAX_RECORD_BYTES + 1 or item["size"] * 3 + 512 > MAX_UI_BYTES:
            row = _omission(item, adapter, "record_exceeds_page_budget")
        else:
            raw = _read_file(item["path"], min(capture.MAX_RECORD_BYTES + 1, MAX_UI_BYTES),
                             native=item["kind"] == "native", expected=item["expected"])
            try:
                value = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                if not isinstance(value, dict):
                    raise TypeError
                if item["kind"] == "native":
                    row = capture.claude_native_request(native_root, item["entry"], item["sequence"], value)
                else:
                    row = value
                    if row.get("session_id") is not None and row.get("session_id") != item["session"]:
                        continue
                    if row.get("stage") == "capture.status":
                        notice = {**_gap(row, adapter, {item["session"]}), "_page_position": position}
                        if type(row.get("sequence")) is int and 0 <= row["sequence"] <= 2 ** 53 - 1:
                            notice["_writer_sequence"] = row["sequence"]
                        gaps.append(notice)
                        continue
                    capture.validate(row)
                    if row["adapter"] != adapter:
                        continue
                if _component_entries(row) > MAX_UI_COMPONENTS:
                    row = _omission(item, adapter, "record_structure_limit")
            except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
                row = _omission(item, adapter, "invalid_json")
        records.append(row)
        positions[_record_key(row)] = position
        if row["stage"] == "capture.gap":
            gaps.append({**_gap(row, adapter, {item["session"]}), "_page_position": position})
    capture.link_claude_native_requests(records)
    return records, gaps, positions


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


def _health_counts(value) -> dict:
    return {key: count for key, count in _mapping(value).items() if key in {"dropped", "failed"}
            and type(count) is int and 0 <= count <= 2 ** 53 - 1}


def _writer_health(raw_records: list[dict], records: list[dict], gaps: list[dict]) -> tuple[dict, list[dict]]:
    """Writer counters describe loss, never which exact request was omitted.

    Replay in actual writer enqueue order, which can differ from provider hook
    observation order. Counter epochs span every lane in that writer/session.
    """
    groups, totals, epochs = {}, {}, {}
    for row in raw_records:
        writer = (row["adapter"], row["session_id"], row["instance_id"])
        groups.setdefault(writer, []).append(row)
    for gap in gaps:
        writer = (gap.get("adapter"), gap.get("session_id"), gap.get("instance_id"))
        if gap.get("stage") == "capture.status" and "_writer_sequence" in gap and all(isinstance(value, str) and value for value in writer):
            groups.setdefault(writer, []).append({"adapter": writer[0], "session_id": writer[1], "instance_id": writer[2],
                "record_id": "health-status", "sequence": gap["_writer_sequence"], "observed_at": "",
                "capture_health": _health_counts(gap)})
    for writer, rows in groups.items():
        cumulative = {"dropped": 0, "failed": 0}
        for row in sorted(rows, key=lambda item: (item.get("sequence", 0), item["observed_at"], item["record_id"])):
            counts = _health_counts(row.get("capture_health"))
            for key, value in cumulative.items():
                cumulative[key] = max(value, counts.get(key, 0))
            epochs[_record_key(row)] = (cumulative["dropped"], cumulative["failed"])
        if any(cumulative.values()):
            totals[writer] = cumulative
    remaining = []
    for gap in gaps:
        counts = _health_counts(gap)
        writer = (gap.get("adapter"), gap.get("session_id"), gap.get("instance_id"))
        if gap.get("stage") == "capture.status" and all(isinstance(value, str) and value for value in writer) and any(counts.values()):
            cumulative = totals.setdefault(writer, {"dropped": 0, "failed": 0})
            for key, value in cumulative.items():
                cumulative[key] = max(value, counts.get(key, 0))
        else:
            remaining.append({key: value for key, value in gap.items() if key != "_writer_sequence"})
    for row in records:
        value = row.get("capture_health")
        row["capture_health"] = _health_counts(value) if isinstance(value, dict) else None
    for (adapter, session, instance), counts in sorted(totals.items()):
        remaining.append({"stage": "capture.status", "adapter": adapter, "session_id": session,
                          "instance_id": instance, "reason": "writer_loss", "scope": "writer",
                          "attribution": "request_unknown", **counts})
    return epochs, remaining


def load_dataset(capture_root: Path | None, *, session_ids: set[str], adapter: str | None = None,
                 claude_raw: Path | None = None, cursor: str | None = None, direction: str = "latest") -> dict:
    """Read a bounded window for host-authorized native sessions only.

    Cursors reference a bounded process-local registry, contain no paths, and are
    valid only for this exact host scope. The browser cannot authorize a source.
    """
    if capture_root is None:
        return _empty("disabled", "Prompt capture is off. Enable it in the agent's launch environment to record new requests.")
    if os.name != "posix":
        return _empty("unavailable", "Private prompt capture requires a POSIX host (macOS or Linux).")
    if adapter not in ADAPTER_LABELS or not isinstance(session_ids, (set, frozenset)) or not session_ids or len(session_ids) > MAX_SESSIONS or any(
            not isinstance(value, str) or not value or len(value) > 4096 or "\0" in value for value in session_ids):
        return _empty("unavailable", "Prompt capture is available for the dashboard's original native session only.")
    if direction not in {"latest", "older", "newer", "current"}:
        return _empty("unavailable", "This prompt history window is not available. Return to the latest observations.")
    try:
        root = Path(capture_root)
        native = Path(claude_raw) if claude_raw is not None and adapter == "claude-code" else None
        if not root.is_absolute() or native is not None and not native.is_absolute():
            return _empty("unavailable", "The prompt capture directory is not available.")
        scope = capture.fingerprint([str(root), adapter, sorted(session_ids), str(native) if native else None])
        window = _cursor_window(scope, cursor) if direction != "latest" else None
        catalog = _catalog(root, adapter, session_ids, native)
        candidates = catalog
        if window:
            first, last = window
            candidates = [item for item in catalog if item["key"] < first] if direction == "older" else [
                item for item in catalog if item["key"] > last] if direction == "newer" else [
                item for item in catalog if first <= item["key"] <= last]
        forward = direction == "newer"
        chosen, reserved = [], 0
        for item in candidates if forward else reversed(candidates):
            cost = min(item["size"] * 3 + 512, MAX_UI_BYTES)
            if chosen and (len(chosen) >= MAX_UI_RECORDS or reserved + cost > MAX_UI_BYTES):
                break
            chosen.append(item)
            reserved += cost
        chosen.sort(key=lambda item: item["key"])
        initial_positions = {id(item): index for index, item in enumerate(chosen)}
        raw_records, gaps, positions = _read_page(chosen, adapter, native)
        while sum(_component_entries(row) for row in raw_records) > MAX_UI_COMPONENTS and len(chosen) > 1:
            dropped = chosen.pop(-1 if forward else 0)
            doomed = {key for key, value in positions.items() if value == initial_positions[id(dropped)]}
            raw_records = [row for row in raw_records if _record_key(row) not in doomed]
            gaps = [gap for gap in gaps if gap.get("_page_position") != initial_positions[id(dropped)]]
        records = capture.summarize_records(raw_records, include_content=True)
        # Projection overhead depends on component structure. Tighten the visible
        # window without rereading bodies if metadata's conservative estimate was low.
        while len(capture.encoded(records)) + len(records) * 512 > MAX_UI_BYTES:
            if len(chosen) > 1:
                dropped = chosen.pop(-1 if forward else 0)
                # positions use the original chosen-window order, not catalog indices.
                doomed = {key for key, value in positions.items() if value == initial_positions[id(dropped)]}
                raw_records = [row for row in raw_records if _record_key(row) not in doomed]
                gaps = [gap for gap in gaps if gap.get("_page_position") != initial_positions[id(dropped)]]
            elif chosen:
                replacement = _omission(chosen[0], adapter, "record_exceeds_page_budget")
                raw_records = [replacement]
                positions[_record_key(replacement)] = initial_positions[id(chosen[0])]
                gaps = [_gap(replacement, adapter, {chosen[0]["session"]})]
                if len(capture.encoded(capture.summarize_records(raw_records, include_content=True))) + 512 > MAX_UI_BYTES:
                    raise capture.CaptureError("excessive_page")
            else:
                break
            records = capture.summarize_records(raw_records, include_content=True)
        gaps = [{key: value for key, value in gap.items() if key != "_page_position"} for gap in gaps]
        writer_epochs, gaps = _writer_health(raw_records, records, gaps)
        # Empty page-edge snapshots are unknown predecessors, not mass additions.
        has_older = bool(chosen and catalog[0]["key"] < chosen[0]["key"])
        omissions = {positions[_record_key(row)] for row in records if row["stage"] == "capture.gap" or row.get("omitted_reason")}
        lookup_raw = {_record_key(row): row for row in records}
        for row in records:
            identity = _record_key(row)
            old = lookup_raw.get(_record_key(row, row["baseline_record_id"])) if row.get("baseline_record_id") else None
            epoch = writer_epochs.get(identity, (0, 0))
            writer_gap = epoch != writer_epochs.get(_record_key(old), (0, 0)) if old else any(epoch)
            separated = old is not None and any(min(positions[identity], positions[_record_key(old)]) < point <
                max(positions[identity], positions[_record_key(old)]) for point in omissions)
            if row["stage"] == "capture.gap" or row.get("omitted_reason"):
                row["comparison_status"] = "capture_incomplete"
                if "effective_state_changes" in row:
                    row["effective_state_changes"] = None
            elif writer_gap and row.get("changes") is not None and row["components"]:
                row.update({"baseline_record_id": None, "changes": None, "baseline_relation": None, "comparison_status": "capture_gap"})
                if "effective_state_changes" in row:
                    row["effective_state_changes"] = None
            elif (row["comparison_status"] == "initial_baseline" and (has_older or
                    row["correlation"].get("previous_message_id"))) or separated:
                row.update({"baseline_record_id": None, "changes": None, "baseline_relation": None, "comparison_status": "page_boundary"})
                if "effective_state_changes" in row:
                    row["effective_state_changes"] = None
            elif "effective_state" in row:
                row["effective_state_changes"] = capture.changes(old.get("effective_state", []) if old else [], row["effective_state"])
        if len(capture.encoded(records)) > MAX_UI_BYTES or len(records) > MAX_UI_RECORDS:
            # A single very fragmented projection can exceed its raw size substantially.
            # Refuse it explicitly; no private body is silently truncated or compared.
            return _empty("limit", "This prompt window exceeds the safe display limit. Use the JSON inspector for this capture.")
        if not chosen:
            result = _empty("empty", "No prompt observations are available in this history window.")
            result["page"] = {"has_older": False, "has_newer": bool(catalog), "observations": 0, "known_observations": len(catalog)}
            return result
        token = _remember_cursor(scope, chosen[0]["key"], chosen[-1]["key"])
        has_newer = catalog[-1]["key"] > chosen[-1]["key"]
        page = {"has_older": has_older, "has_newer": has_newer, "older_cursor": token if has_older else None,
                "newer_cursor": token if has_newer else None, "current_cursor": token,
                "observations": len(chosen), "known_observations": len(catalog),
                "position_start": next(index for index, item in enumerate(catalog) if item is chosen[0]) + 1,
                "position_end": next(index for index, item in enumerate(catalog) if item is chosen[-1]) + 1,
                "omitted_oversized": sum(row.get("omitted_reason") == "record_exceeds_page_budget" for row in records),
                "comparison_boundary": "visible_page_only", "order": "capture_file_time"}
    except FileNotFoundError:
        return _empty("empty", "No prompt observations have been recorded for this session yet.")
    except capture.CaptureError as error:
        if str(error) == "invalid_cursor":
            result = _empty("unavailable", "This prompt history window has expired. Return to the latest observations.")
            result["reason"] = "cursor_expired"
            return result
        if str(error).startswith("excessive_"):
            return _empty("limit", "The prompt metadata catalog exceeds the safe input limit. Use the JSON inspector for the full capture.")
        result = _empty("unavailable", "Prompt observations could not be read safely. The capture files were not changed.")
        result["reason"] = "capture_busy" if str(error) == "capture_busy" else "capture_unavailable"
        return result
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
        return _empty("unavailable", "Prompt observations could not be read safely. The capture files were not changed.")
    if not records:
        result = _empty("empty", "No prompt observations have been recorded for this session yet.")
        result.update({"capture_gaps": gaps, "page": page})
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
    result.update({"records": records, "lanes": list(lanes.values()), "capture_gaps": gaps, "page": page,
                   "markers": [{key: row[key] for key in ("selection_id", "adapter", "session_id", "instance_id", "stage", "observed_at", "correlation")}
                               for row in records if row["stage"].startswith("lifecycle.") or row["stage"] == "capture.gap"]})
    if len(capture.encoded(result)) > MAX_UI_BYTES:
        return _empty("limit", "This prompt window exceeds the safe display limit. Use the JSON inspector for this capture.")
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
    comparable = selected["baseline"] is not None and "uncompared" not in row["changes"]
    if comparable:
        for line in difflib.unified_diff(before_lines, after_lines, fromfile="Previous", tofile="Selected", lineterm=""):
            if len(lines) >= MAX_DIFF_LINES:
                clipped = True
                break
            lines.append(line)
    return {**row, "comparison_available": comparable, "before_text": before[:MAX_DIFF_CHARS], "after_text": after[:MAX_DIFF_CHARS],
            "diff_lines": lines, "truncated": clipped, "limit_chars": MAX_DIFF_CHARS}
