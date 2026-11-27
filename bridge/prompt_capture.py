"""Private, read-only prompt observations and composition diffs. No network or TrajViz dependency."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

MAX_RECORD_BYTES = 4 * 1024 * 1024
MAX_INSTANCE_BYTES = 128 * 1024 * 1024
MAX_RECORDS = 4096
ADAPTERS = {"pi", "claude-code", "opencode", "deveco"}
IDENTITY = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


class CaptureError(ValueError):
    """Only fixed reason codes cross the subprocess boundary."""


def encoded(value) -> bytes:
    # Key order is evidence too: named system sections and provider schemas may be ordered.
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def fingerprint(value) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


def validate(record: dict) -> None:
    if not isinstance(record, dict) or record.get("schema_version") != 1 or record.get("adapter") not in ADAPTERS:
        raise CaptureError("invalid_record")
    for key in ("instance_id", "record_id"):
        if not isinstance(record.get(key), str) or not IDENTITY.fullmatch(record[key]):
            raise CaptureError("invalid_identity")
    if record["record_id"] == "capture-health":
        raise CaptureError("reserved_identity")
    for key, limit in (("session_id", 4096), ("stage", 128), ("observed_at", 128)):
        if not isinstance(record.get(key), str) or not record[key] or len(record[key]) > limit or "\0" in record[key]:
            raise CaptureError("invalid_record")
    if "data" not in record or len(encoded(record)) > MAX_RECORD_BYTES:
        raise CaptureError("record_too_large")
    for value in (record.get("sequence", 0), _mapping(record.get("correlation")).get("observation_sequence", 0)):
        if type(value) is not int or not 0 <= value <= 2 ** 53 - 1:
            raise CaptureError("invalid_sequence")


def _private(metadata) -> None:
    if not stat.S_ISDIR(metadata.st_mode) or (hasattr(os, "getuid") and metadata.st_uid != os.getuid()):
        raise CaptureError("unsafe_directory")
    if os.name == "posix" and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise CaptureError("directory_not_private")


@contextmanager
def private_directory(root: str, *, create: bool = True):
    """Walk from an absolute anchor without following directory links on POSIX."""
    target = Path(root)
    if not target.is_absolute() or ".." in target.parts or "\0" in root:
        raise CaptureError("invalid_directory")
    if os.name != "posix":
        # Fail closed until an ACL-backed implementation has native validation.
        raise CaptureError("unsupported_private_storage")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current = os.open(target.anchor, flags)
    try:
        for name in target.parts[1:]:
            try:
                child = os.open(name, flags, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(name, 0o700, dir_fd=current)
                except FileExistsError:
                    pass
                child = os.open(name, flags, dir_fd=current)
            os.close(current)
            current = child
        _private(os.fstat(current))
        yield current
    finally:
        os.close(current)


@contextmanager
def child_directory(parent: int, name: str):
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
    except FileExistsError:
        pass
    descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        _private(os.fstat(descriptor))
        yield descriptor
    finally:
        os.close(descriptor)


def _publish(directory: int, name: str, body: bytes, *, replace: bool = False) -> None:
    temporary = f".pending-{uuid4()}"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        if replace:
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        else:
            # A hard-link publication is atomic and refuses an existing destination.
            os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


@contextmanager
def instance_lock(directory: int):
    import fcntl

    try:
        descriptor = os.open(".lock", os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    except FileExistsError:
        descriptor = os.open(".lock", os.O_RDWR | os.O_NOFOLLOW, dir_fd=directory)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise CaptureError("unsafe_lock")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def remove_abandoned_writes(directory: int) -> None:
    """The instance lock proves no live writer owns a pending publication here."""
    for name in os.listdir(directory):
        if not re.fullmatch(r"\.pending-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", name):
            continue
        metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077):
            raise CaptureError("unsafe_pending_record")
        os.unlink(name, dir_fd=directory)


def write_record(root: str, record: dict) -> str:
    validate(record)
    body = encoded(record) + b"\n"
    session = hashlib.sha256(record["session_id"].encode()).hexdigest()[:32]
    with private_directory(root) as root_fd, child_directory(root_fd, record["adapter"]) as adapter_fd, \
            child_directory(adapter_fd, session) as session_fd, \
            child_directory(session_fd, record["instance_id"]) as directory, instance_lock(directory):
        remove_abandoned_writes(directory)
        files = [name for name in os.listdir(directory) if name.endswith(".json") and name != "capture-health.json"]
        total = 0
        for name in files:
            metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise CaptureError("unsafe_record")
            total += metadata.st_size
        if len(files) >= MAX_RECORDS or total + len(body) > MAX_INSTANCE_BYTES:
            _publish(directory, "capture-health.json", encoded({"stage": "capture.status", "reason": "storage_limit"}), replace=True)
            raise CaptureError("storage_limit")
        _publish(directory, record["record_id"] + ".json", body)
    return str(Path(root) / record["adapter"] / session / record["instance_id"] / (record["record_id"] + ".json"))


def _mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def _kind(role: str, content) -> str:
    if role in {"system", "developer"}:
        return "system"
    if role in {"tool", "toolResult", "function_call_output"}:
        return "tool_result"
    if isinstance(content, list) and content and all(
            isinstance(block, dict) and block.get("type") in {"tool_result", "tool-result"} for block in content):
        return "tool_result"
    return "conversation"


def components(record: dict) -> list[dict]:
    """Physical request contributions, never speculative rule/skill classifications."""
    data = _mapping(record.get("data"))
    payload = data.get("payload")
    if isinstance(payload, dict):
        data = payload
        if isinstance(data.get("response"), dict):
            data = data["response"]
    rows = []

    def add(identity: str, kind: str, value, *, role: str | None = None, source=None):
        row = {"id": identity, "kind": kind, "sha256": fingerprint(value), "content": value,
               "utf8_bytes": len(encoded(value)), "measurement": "not_measured"}
        if role:
            row["role"] = role
        if source is not None:
            row["source"] = source
        rows.append(row)

    system = data.get("system", data.get("instructions"))
    if system is not None:
        for index, value in enumerate(system if isinstance(system, list) else [system]):
            add(f"system:{index}", "system", value, role="system")
    messages = data.get("messages", data.get("input", []))
    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]
    if isinstance(messages, list):
        occurrences = {}
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                add(f"message:{index}", "opaque", message)
                continue
            role = str(message.get("role") or message.get("type") or "unknown")
            native = message.get("id") or message.get("tool_call_id") or message.get("toolCallId") or message.get("call_id")
            identity = f"message:{role}:{native}" if isinstance(native, str) else f"message:{role}:{index}"
            occurrences[identity] = occurrences.get(identity, 0) + 1
            identity += f":{occurrences[identity]}"
            # A Pi system entry contains sections/tool declarations and later patches.
            content = message.get("content", message)
            add(identity, _kind(role, content), message, role=role)
    tools = data.get("tools", [])
    if isinstance(tools, dict):
        tools = [{"name": name, **_mapping(value)} for name, value in tools.items()]
    if isinstance(tools, list):
        seen = {}
        for index, tool in enumerate(tools):
            value = _mapping(tool)
            name = value.get("name") or _mapping(value.get("function")).get("name") or str(index)
            seen[name] = seen.get(name, 0) + 1
            add(f"tool:{name}:{seen[name]}", "tool_definition", tool)
    if not rows and record.get("stage", "").startswith("component."):
        add(record["stage"], "component_observation", data, source=data.get("source"))
    for index, row in enumerate(rows):
        row["position"] = index
    return rows


def changes(before: list[dict], after: list[dict]) -> dict:
    previous = {row["id"]: row for row in before}
    current = {row["id"]: row for row in after}
    result = {name: [] for name in ("added", "removed", "modified", "moved", "unchanged")}
    for row in after:
        old = previous.get(row["id"])
        if old is None:
            result["added"].append(row["id"])
        elif old["sha256"] != row["sha256"]:
            result["modified"].append(row["id"])
        else:
            result["unchanged"].append(row["id"])
        if old is not None and old["position"] != row["position"]:
            result["moved"].append(row["id"])
    result["removed"] = [row["id"] for row in before if row["id"] not in current]
    return result


def pi_effective_state(record: dict) -> list[dict] | None:
    """Replay Pi's named section and tool patches without changing the physical transcript."""
    messages = _mapping(record.get("data")).get("messages")
    if record["adapter"] != "pi" or not isinstance(messages, list):
        return None
    sections, tools, appended = {}, {}, []
    exposed = False
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "system":
            continue
        exposed = True
        content = message.get("content")
        if content:
            appended.append(content)
        for name, value in _mapping(message.get("sections")).items():
            if value is None:
                sections.pop(name, None)
            else:
                sections[name] = value
        for tool in message.get("toolsRemoved", []):
            if isinstance(tool, dict):
                tools.pop(tool.get("name"), None)
        for tool in message.get("toolsAdded", []):
            if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                tools[tool["name"]] = tool
    if not exposed:
        return None
    rows = []
    for identity, kind, content in [
        *[(f"system:content:{index}", "system", value) for index, value in enumerate(appended)],
        *[(f"system:section:{name}", "system_section", value) for name, value in sections.items()],
        *[(f"tool:{name}", "tool_definition", value) for name, value in tools.items()],
    ]:
        rows.append({"id": identity, "kind": kind, "position": len(rows), "sha256": fingerprint(content),
                     "content": content, "utf8_bytes": len(encoded(content)), "measurement": "not_measured"})
    return rows


def claude_native_records(root: Path, sessions: set[str], *, max_records: int | None = None,
                          max_bytes: int | None = None, _inspection: dict | None = None) -> list[dict]:
    """Read Claude's own local successful-request index; never infer request/step joins."""
    record_limit, byte_limit = _inspection_limits(max_records, max_bytes)

    def read_file(directory: int, name: str, limit: int) -> bytes:
        if not stat.S_ISREG(os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode):
            raise CaptureError("unsafe_native_input")
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_uid != os.getuid():
                raise CaptureError("unsafe_native_input")
            if metadata.st_size > limit:
                raise CaptureError("excessive_input")
            value = source.read(limit + 1)
            if len(value) > limit:
                raise CaptureError("excessive_input")
            return value

    records, total = [], 0
    with private_directory(str(root), create=False) as directory:
        index = read_file(directory, "index.jsonl", min(MAX_RECORD_BYTES, byte_limit))
        total += len(index)
        for sequence, line in enumerate(index.splitlines()):
            entry = json.loads(line)
            if not isinstance(entry, dict) or entry.get("session_id") not in sessions:
                continue
            filename = entry.get("request_file")
            if not isinstance(filename, str):
                raise CaptureError("invalid_native_index")
            target = Path(filename)
            if (target.is_absolute() and target.parent != root) or target.name != str(target) and not target.is_absolute():
                raise CaptureError("invalid_native_index")
            if len(records) >= record_limit:
                raise CaptureError("excessive_input")
            body = read_file(directory, target.name, min(MAX_RECORD_BYTES, byte_limit - total))
            total += len(body)
            if total > byte_limit:
                raise CaptureError("excessive_input")
            records.append(claude_native_request(root, entry, sequence, json.loads(body)))
    link_claude_native_requests(records)
    if _inspection is not None:
        _inspection.update({"records": len(records), "bytes": total})
    return records


def claude_native_request(root: Path, entry: dict, sequence: int, payload: dict) -> dict:
    """Project one privately read native request; callers authorize its indexed file."""
    if (not isinstance(entry.get("timestamp"), (str, int, float))
            or any(entry.get(key) is not None and not isinstance(entry[key], str)
                   for key in ("request_id", "message_id", "message_uuid", "query_source"))):
        raise CaptureError("invalid_native_index")
    if not isinstance(payload, dict):
        raise CaptureError("invalid_native_request")
    source = entry.get("query_source") or "unknown"
    previous_message = _mapping(payload.get("thread")).get("previous_message_id")
    if previous_message is not None and not isinstance(previous_message, str):
        raise CaptureError("invalid_native_thread")
    purpose = {"repl_main_thread": "primary", "compact": "compaction"}.get(source, source)
    row = {"schema_version": 1, "adapter": "claude-code", "session_id": entry["session_id"],
           "instance_id": "native-" + fingerprint(str(root))[:24], "record_id": fingerprint(entry)[:32],
           "sequence": sequence, "observed_at": str(entry["timestamp"]), "stage": "provider.request",
           "kind": purpose, "correlation": {"request_id": entry.get("request_id"),
           "message_id": entry.get("message_id"), "message_uuid": entry.get("message_uuid"),
           "query_source": source, "agent_id": source, "scope_basis": "native_query_source",
           "previous_message_id": previous_message, "request_linkage": "native_index", "step_linkage": "not_inferred"},
           "model": {"id": payload.get("model")}, "data": {"payload": payload,
           "provenance": {"point": "claude_native_request_log", "successful_index_entries_only": True,
                          "extended_thinking_may_be_redacted": True}}}
    validate(row)
    return row


def link_claude_native_requests(records: list[dict]) -> None:
    """Resolve only observed, exact native ancestry; never join agent-type siblings."""
    native = [row for row in records if _mapping(_mapping(row.get("data")).get("provenance")).get("point") == "claude_native_request_log"]
    by_message = {}
    for row in native:
        message = row["correlation"].get("message_id")
        if message:
            key = (row["session_id"], message)
            if key in by_message:
                raise CaptureError("invalid_native_thread")
            by_message[key] = row
    for row in native:
        current, seen = row, set()
        while current["correlation"].get("previous_message_id"):
            previous = current["correlation"]["previous_message_id"]
            if previous in seen:
                raise CaptureError("invalid_native_thread")
            seen.add(previous)
            parent = by_message.get((row["session_id"], previous))
            if parent is None:
                origin = "unobserved-" + previous
                break
            current = parent
        else:
            origin = current["record_id"]
        row["correlation"]["lineage_id"] = "native-thread-" + fingerprint(origin)[:24]


def _inspection_limits(max_records: int | None, max_bytes: int | None) -> tuple[int, int]:
    records = MAX_RECORDS if max_records is None else max_records
    size = MAX_INSTANCE_BYTES if max_bytes is None else max_bytes
    if type(records) is not int or type(size) is not int or records < 0 or size < 0:
        raise CaptureError("invalid_input_limit")
    return min(records, MAX_RECORDS), min(size, MAX_INSTANCE_BYTES)


def inspect_records(root: Path, *, include_content: bool = False, claude_raw: Path | None = None,
                    max_records: int | None = None, max_bytes: int | None = None,
                    session_ids: set[str] | None = None) -> dict:
    """Compare only a proven same-session/instance/agent/run/branch and capture boundary."""
    record_limit, byte_limit = _inspection_limits(max_records, max_bytes)
    records = []
    gaps = []
    scanned, total_bytes = 0, 0

    def read_tree(directory: int, depth: int = 0):
        nonlocal scanned, total_bytes
        if depth > 4:
            raise CaptureError("excessive_input_depth")
        for name in sorted(os.listdir(directory)):
            metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                raise CaptureError("unsafe_input")
            if stat.S_ISDIR(metadata.st_mode):
                descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                try:
                    _private(os.fstat(descriptor))
                    yield from read_tree(descriptor, depth + 1)
                finally:
                    os.close(descriptor)
            elif name.endswith(".json"):
                if not stat.S_ISREG(metadata.st_mode):
                    raise CaptureError("unsafe_input")
                scanned += 1
                total_bytes += metadata.st_size
                if scanned > record_limit or total_bytes > byte_limit or metadata.st_size > MAX_RECORD_BYTES + 1:
                    raise CaptureError("excessive_input")
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(descriptor, "rb") as source:
                    actual = os.fstat(source.fileno())
                    if (not stat.S_ISREG(actual.st_mode) or actual.st_nlink != 1 or actual.st_uid != os.getuid()
                            or stat.S_IMODE(actual.st_mode) & 0o077):
                        raise CaptureError("unsafe_input")
                    raw = source.read(min(MAX_RECORD_BYTES + 2, byte_limit - total_bytes + metadata.st_size + 1))
                    if len(raw) > MAX_RECORD_BYTES + 1:
                        raise CaptureError("excessive_input")
                    total_bytes += len(raw) - metadata.st_size
                    if total_bytes > byte_limit:
                        raise CaptureError("excessive_input")
                    value = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(CaptureError("invalid_json")))
                    if not isinstance(value, dict):
                        raise CaptureError("invalid_record")
                    yield value

    with private_directory(str(root), create=False) as directory:
        inputs = list(read_tree(directory))
    for record in inputs:
        if session_ids is not None and record.get("session_id") is not None and record.get("session_id") not in session_ids:
            continue
        if record.get("stage") in {"capture.status", "capture.gap"}:
            gaps.append(record)
            if record.get("stage") == "capture.status":
                continue
        validate(record)
        records.append(record)
    native_inspection = {"records": 0, "bytes": 0}
    if claude_raw is not None:
        sessions = {row["session_id"] for row in records if row["adapter"] == "claude-code"}
        records.extend(claude_native_records(claude_raw, sessions, max_records=record_limit - scanned,
                                             max_bytes=byte_limit - total_bytes, _inspection=native_inspection))
        if len(records) > record_limit or sum(len(encoded(row)) for row in records) > byte_limit:
            raise CaptureError("excessive_input")
    output = summarize_records(records, include_content=include_content)
    return {"schema_version": 1, "records": output, "capture_gaps": gaps, "count": len(output),
            "inspection": {"records": scanned + native_inspection["records"], "bytes": total_bytes + native_inspection["bytes"]}}


def summarize_records(records: list[dict], *, include_content: bool = False) -> list[dict]:
    """Project already validated observations, with comparisons confined to this set.

    The bounded UI reader selects private files before decoding; the CLI reader
    retains its full-input limits. Neither caller invents a cross-page baseline.
    """
    records = list(records)
    # HTTP body clones can finish out of order. Prefer the order observed at hook entry.
    records.sort(key=lambda row: (row["adapter"], row["instance_id"],
                                 _mapping(row.get("correlation")).get("observation_sequence", row.get("sequence", 0)),
                                 row["observed_at"], row["record_id"]))
    previous = {}
    previous_state = {}
    native_parents = {}
    for record in records:
        if _mapping(_mapping(record.get("data")).get("provenance")).get("point") == "claude_native_request_log":
            message = _mapping(record.get("correlation")).get("message_id")
            if message:
                key = (record["session_id"], record["instance_id"], message)
                native_parents.setdefault(key, []).append(record)
    output = []
    for record in records:
        correlation = _mapping(record.get("correlation"))
        scope = (record["adapter"], record["session_id"], record["instance_id"], record["stage"], record.get("kind"),
                 correlation.get("agent_id"), correlation.get("lineage_id", correlation.get("run_id")), correlation.get("branch_id"))
        rows = components(record)
        old = previous.get(scope)
        data = _mapping(record.get("data"))
        is_native = _mapping(data.get("provenance")).get("point") == "claude_native_request_log"
        if is_native:
            # Sibling requests may share a thread prefix. Compare the exact predecessor,
            # never the last sibling to finish or the last request of that agent type.
            parents = native_parents.get((record["session_id"], record["instance_id"], correlation.get("previous_message_id")), [])
            parent = parents[0] if len(parents) == 1 else None
            old = ((parent["record_id"], components(parent)) if parent is not None
                   and _mapping(parent.get("correlation")).get("query_source") == correlation.get("query_source") else None)
        omitted = data.get("omitted_reason") or (data.get("reason") if record["stage"] == "capture.gap" else None)
        if record["stage"] == "provider.request" and not rows and not omitted:
            omitted = "no_prompt_components_exposed"
        unlinked = record["stage"].startswith("component.")
        delta = None if omitted or unlinked else changes(old[1] if old else [], rows)
        if rows and not omitted and not unlinked and not is_native:
            previous[scope] = (record["record_id"], rows)
        summary = {key: record[key] for key in ("record_id", "adapter", "session_id", "instance_id", "stage", "observed_at")}
        summary.update({"kind": record.get("kind"), "correlation": correlation,
                        "baseline_record_id": old[0] if old else None, "changes": delta,
                        "components": rows if include_content else [{key: value for key, value in row.items() if key != "content"} for row in rows],
                        "capture_health": record.get("capture_health"), "model": record.get("model"),
                        "measurement": "component_tokens_not_measured", "omitted_reason": omitted,
                        "provenance": data.get("provenance"),
                        "view": "provider_payload_at_hook" if record["stage"] == "provider.request" else "semantic_observation"})
        summary["comparison_status"] = "unlinked_component" if unlinked else "capture_incomplete" if omitted else "compared" if old else "initial_baseline"
        summary["baseline_relation"] = "native_previous_message" if is_native and old else "previous_observation_in_lineage" if old else None
        summary["composition"] = {kind: sum(row["kind"] == kind for row in rows) for kind in dict.fromkeys(row["kind"] for row in rows)}
        payload = _mapping(data.get("payload"))
        continuation = (payload.get("previous_response_id") or _mapping(payload.get("response")).get("previous_response_id")
                        or _mapping(payload.get("thread")).get("previous_message_id"))
        if continuation:
            summary["remote_context"] = "retained_not_reconstructed"
        state = pi_effective_state(record)
        if state is not None:
            summary["effective_state"] = state if include_content else [
                {key: value for key, value in row.items() if key != "content"} for row in state]
            summary["effective_state_changes"] = changes(previous_state.get(scope, []), state)
            previous_state[scope] = state
        output.append(summary)
    return output


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    write = commands.add_parser("write", help="Private subprocess writer; takes one JSON record on stdin")
    write.add_argument("--root", required=True)
    inspect = commands.add_parser("inspect", help="Print composition and per-boundary changes as JSON")
    inspect.add_argument("--root", required=True)
    inspect.add_argument("--include-content", action="store_true", help="Include captured prompt content in the local report")
    inspect.add_argument("--claude-raw", type=Path, help="Also inspect matching requests from Claude's private native request-log directory")
    args = parser.parse_args(argv)
    try:
        if args.command == "write":
            if os.environ.get("TRAJVIZ_OBS_PROMPT_CAPTURE") != "1":
                raise CaptureError("capture_disabled")
            raw = sys.stdin.buffer.read(MAX_RECORD_BYTES + 1)
            if len(raw) > MAX_RECORD_BYTES:
                raise CaptureError("record_too_large")
            record = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(CaptureError("invalid_json")))
            write_record(args.root, record)
            print('{"ok":true}')
        else:
            print(json.dumps(inspect_records(Path(args.root), include_content=args.include_content, claude_raw=args.claude_raw), ensure_ascii=False, allow_nan=False))
        return 0
    except (OSError, ValueError, TypeError, RecursionError, OverflowError, KeyError):
        # Never echo exception messages: they can include filenames or prompt content.
        print('{"ok":false,"reason":"capture_unavailable"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
