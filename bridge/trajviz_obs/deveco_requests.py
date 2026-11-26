"""Opt-in DevEco HTTP-body evidence, scoped to a host-selected native source.

This reader never discovers a capture root from imported data or environment.
The caller must authorize the root against its pinned input before calling it.
Raw bodies live in private sidecars and in the caller's private cache, never in
``Trajectory.raw`` or ordinary trajectory exports. Provider internals and billed
usage cannot be recovered from an observed outgoing HTTP body.
"""
from __future__ import annotations

import copy
import hashlib
import inspect
import json
import math
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import prompt_capture as capture

from .deveco import NATIVE_KEY

MAX_REQUESTS = 512
MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_METADATA_BYTES = 64 * 1024
MAX_SESSIONS = 64
MAX_TRACE_BYTES = 16 * 1024 * 1024
MAX_TRACE_ROWS = 65536
MAX_ANCESTORS = 128
_TRACE_ID = re.compile(r"[0-9a-f]{32}\Z")
_SPAN_ID = re.compile(r"[0-9a-f]{16}\Z")
_BASES = frozenset({"host_processor_context", "native_processor_span"})
_PURPOSES = frozenset({"assistant", "compaction", "title", "auxiliary"})
_BODY_LIMIT = "The captured bytes are the serialized HTTP body observed at the local proxy boundary, before provider-side processing."
_USAGE_LIMIT = "Native assistant usage is not apportioned across HTTP attempts; per-request billed usage is unavailable."


class RequestCaptureError(capture.CaptureError):
    """Fixed reason codes; no captured text is included in exceptions."""


@dataclass(frozen=True)
class RequestEvidence:
    """Private snapshot; consumers must not place this object in raw exports."""
    _records: tuple[dict[str, Any], ...] = field(default_factory=tuple, repr=False)
    _binding: str = field(default="", repr=False)
    _traces: dict = field(default_factory=dict, repr=False)
    limitations: tuple[str, ...] = ()

    @property
    def request_count(self) -> int:
        return len(self._records)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _identity(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _json(body: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise RequestCaptureError("duplicate_json_key")
            result[key] = value
        return result
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise RequestCaptureError("invalid_json") from exc
    if not isinstance(value, dict):
        raise RequestCaptureError("invalid_json_object")
    return value


def _file(directory: int, name: str, limit: int) -> bytes:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) & 0o077):
            raise RequestCaptureError("unsafe_request_file")
        if not 0 <= before.st_size <= limit:
            raise RequestCaptureError("request_file_too_large")
        result = bytearray()
        while len(result) <= limit:
            block = os.read(descriptor, min(65536, limit + 1 - len(result)))
            if not block:
                break
            result.extend(block)
        after = os.fstat(descriptor)
        if len(result) != before.st_size or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise RequestCaptureError("request_file_changed")
        return bytes(result)
    finally:
        os.close(descriptor)


def _native(raw: dict, steps: list[dict]) -> tuple[set[str], dict, dict, str]:
    """Use legacy native info.id, not often-empty normalized message_id."""
    if (not isinstance(raw, dict) or raw.get("_deveco_format") is not True
            or raw.get("_trajviz_obs_deveco_normalizer_version") != 1
            or not isinstance(raw.get(NATIVE_KEY), dict)):
        raise RequestCaptureError("unsupported_native_source")
    source = raw[NATIVE_KEY]
    if source.get("source_format") != "deveco_v1" or type(source.get("schema_version")) is not int or source[
            "schema_version"] != 1:
        raise RequestCaptureError("unsupported_native_source")
    root = _text(raw.get("info", {}).get("id")) if isinstance(raw.get("info"), dict) else ""
    if not root or source.get("session", {}).get("id") != root:
        raise RequestCaptureError("invalid_native_source")
    messages = raw.get("messages")
    if not isinstance(messages, list):
        raise RequestCaptureError("invalid_native_source")
    native, duplicates = {}, set()
    for position, message in enumerate(messages):
        info = message.get("info") if isinstance(message, dict) else None
        if not isinstance(info, dict):
            continue
        sid, mid = _text(info.get("sessionID")) or root, _text(info.get("id"))
        if not sid or not mid:
            continue
        key = sid, mid
        if key in native:
            duplicates.add(key)
        native[key] = (position, info)
    for key in duplicates:
        native.pop(key, None)
    indexed, repeated = {}, set()
    for step in steps:
        if not isinstance(step, dict):
            continue
        key = (_text(step.get("session_id")), _text(step.get("id")) or _text(step.get("message_id")))
        original = native.get(key)
        index, raw_index = step.get("index"), step.get("raw_index")
        if (not original or type(index) is not int or type(raw_index) is not int or raw_index != original[0]
                or step.get("role") != original[1].get("role")):
            continue
        if key in indexed or any(item.get("index") == index for item in indexed.values()):
            repeated.add(key)
            for prior, item in indexed.items():
                if item.get("index") == index:
                    repeated.add(prior)
        indexed[key] = step
    for key in repeated:
        indexed.pop(key, None)
    sessions = {root, *(sid for sid, _ in indexed)}
    if len(sessions) > MAX_SESSIONS:
        raise RequestCaptureError("too_many_native_sessions")
    # Cache binding is based on source/native ownership, never timestamps or response text.
    binding = hashlib.sha256(json.dumps({"root": root, "owners": sorted(
        (sid, mid, item[1].get("role"), item[0]) for (sid, mid), item in native.items()),
        "steps": sorted((sid, mid, item.get("index"), item.get("raw_index"), item.get("role"))
                        for (sid, mid), item in indexed.items())}, sort_keys=True).encode()).hexdigest()
    return sessions, native, indexed, binding


def _validate_record(record: dict, sid: str, stem: str) -> None:
    if (type(record.get("schema_version")) is not int or record["schema_version"] != 1
            or record.get("type") != "deveco_request" or record.get("adapter") != "deveco"
            or record.get("session_id") != sid or not _identity(stem) or record.get("request_id") != stem
            or not _identity(record.get("invocation_id")) or record.get("purpose") not in _PURPOSES
            or record.get("body_ref") != f"{stem}.body"
            or type(record.get("body_bytes")) is not int or not 0 <= record["body_bytes"] <= MAX_BODY_BYTES
            or not isinstance(record.get("body_sha256"), str) or len(record["body_sha256"]) != 64):
        raise RequestCaptureError("invalid_request_record")
    coverage = record.get("coverage")
    if (not isinstance(coverage, dict) or coverage.get("complete") is not True
            or coverage.get("omitted_fields") != [] or coverage.get("redacted_fields") != []):
        raise RequestCaptureError("incomplete_request_body")
    if record.get("assistant_message_id") is not None and not isinstance(record["assistant_message_id"], str):
        raise RequestCaptureError("invalid_request_record")


def load_request_evidence(capture_root: str | Path, *, native_raw: dict, steps: list[dict]) -> RequestEvidence:
    """Read bounded private sidecars for native sessions authorized by this source.

    Only the caller's explicit root is read. No directories are created, no
    symlinks are followed, and no body_ref can escape its session directory.
    Invalid records fail closed rather than being relabeled complete captures.
    """
    sessions, _, _, binding = _native(native_raw, steps)
    records, seen, total, metadata_total, health, traces = [], set(), 0, 0, [], {}
    try:
        with capture.private_directory(str(capture_root), create=False) as root:
            try:
                traces = _trace_index(_file(root, "trace-spans.jsonl", MAX_TRACE_BYTES))
            except FileNotFoundError:
                pass
            for sid in sorted(sessions):
                name = hashlib.sha256(sid.encode()).hexdigest()
                try:
                    directory = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
                except FileNotFoundError:
                    continue
                try:
                    capture._private(os.fstat(directory))
                    names = []
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            if not entry.name.endswith(".json"):
                                continue
                            names.append(entry.name)
                            if len(records) + len(names) > MAX_REQUESTS:
                                raise RequestCaptureError("too_many_requests")
                    for filename in sorted(names):
                        body = _file(directory, filename, MAX_METADATA_BYTES)
                        metadata_total += len(body)
                        if metadata_total > MAX_METADATA_BYTES * MAX_REQUESTS:
                            raise RequestCaptureError("request_catalog_too_large")
                        record = _json(body)
                        stem = filename[:-5]
                        if (type(record.get("schema_version")) is int and record["schema_version"] == 1
                                and record.get("type") == "deveco_request" and record.get("adapter") == "deveco"
                                and record.get("session_id") == sid and record.get("request_id") == stem
                                and _identity(stem) and isinstance(record.get("coverage"), dict)
                                and record["coverage"].get("complete") is False):
                            health.append("Some HTTP attempts have incomplete body capture and are excluded from prompt evidence.")
                            continue
                        _validate_record(record, sid, stem)
                        if stem in seen:
                            raise RequestCaptureError("duplicate_request_id")
                        seen.add(stem)
                        body = _file(directory, record["body_ref"], MAX_BODY_BYTES)
                        total += len(body)
                        if total > MAX_TOTAL_BYTES:
                            raise RequestCaptureError("request_capture_too_large")
                        if len(body) != record["body_bytes"] or hashlib.sha256(body).hexdigest() != record["body_sha256"]:
                            raise RequestCaptureError("request_body_integrity")
                        records.append({"metadata": record, "body": body, "json": _json(body)})
                finally:
                    os.close(directory)
    except FileNotFoundError:
        # A not-yet-created root is normal during live startup. Missing files
        # after a manifest exists must not be interpreted as an empty capture.
        if records:
            raise RequestCaptureError("missing_request_file") from None
        if Path(capture_root).exists():
            raise RequestCaptureError("missing_request_file") from None
    except (OSError, capture.CaptureError) as exc:
        if isinstance(exc, RequestCaptureError):
            raise
        raise RequestCaptureError("unsafe_request_capture") from exc
    return RequestEvidence(_records=tuple(records), _binding=binding, _traces=traces,
                           limitations=tuple(dict.fromkeys(health)))


def _timestamp(value: Any) -> int | float | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            return None
        result = stamp.timestamp() * 1000
        return result if math.isfinite(result) else None
    except (ValueError, OverflowError):
        return None


def _trace_index(body: bytes) -> dict:
    """Index safe collector rows without accepting identities from HTTP headers."""
    spans, identities = {}, {}
    lines = body.splitlines()
    if len(lines) > MAX_TRACE_ROWS:
        raise RequestCaptureError("too_many_trace_rows")
    for line in lines:
        if not line.strip():
            continue
        record = _json(line)
        if type(record.get("schema_version")) is not int or record["schema_version"] != 1:
            raise RequestCaptureError("unsupported_trace_schema")
        trace_id, span_id = record.get("trace_id"), record.get("span_id")
        if (not isinstance(trace_id, str) or not _TRACE_ID.fullmatch(trace_id)
                or not isinstance(span_id, str) or not _SPAN_ID.fullmatch(span_id)
                or trace_id == "0" * 32 or span_id == "0" * 16):
            raise RequestCaptureError("invalid_trace_identity")
        key = trace_id, span_id
        if record.get("type") == "span":
            parent = record.get("parent_span_id")
            if (parent is not None and (not isinstance(parent, str) or not _SPAN_ID.fullmatch(parent))
                    or not isinstance(record.get("name"), str)):
                raise RequestCaptureError("invalid_trace_span")
            value, index = (parent, record["name"]), spans
        elif record.get("type") == "processor_identity":
            if not _text(record.get("session_id")) or not _text(record.get("assistant_message_id")):
                raise RequestCaptureError("invalid_processor_identity")
            value, index = (record["session_id"], record["assistant_message_id"]), identities
        else:
            raise RequestCaptureError("unsupported_trace_record")
        # Duplicate identical exports are harmless. Conflicting definitions make
        # this identity unusable, regardless of journal/arrival order.
        index[key] = value if key not in index or index[key] == value else None
    return {"spans": spans, "identities": identities}


def _processor_owner(meta: dict, traces: dict) -> tuple[str | None, str]:
    trace_id, span_id = meta.get("trace_id"), meta.get("span_id")
    if (not isinstance(trace_id, str) or not _TRACE_ID.fullmatch(trace_id)
            or not isinstance(span_id, str) or not _SPAN_ID.fullmatch(span_id)):
        return None, "processor_trace_pending"
    spans, identities = traces.get("spans", {}), traces.get("identities", {})
    visited = set()
    for _ in range(MAX_ANCESTORS):
        key = trace_id, span_id
        if key in visited:
            return None, "processor_trace_cycle"
        visited.add(key)
        span = spans.get(key)
        if span is None:
            return None, "processor_trace_pending_or_conflicting"
        parent, name = span
        if name == "SessionProcessor.process":
            owner = identities.get(key)
            if owner is None:
                return None, "processor_identity_pending_or_conflicting"
            if owner[0] != meta["session_id"]:
                return None, "processor_session_mismatch"
            return owner[1], "processor_ancestor"
        if not parent or parent == "0" * 16:
            return None, "processor_ancestor_not_recorded"
        span_id = parent
    return None, "processor_ancestry_limit"


def _mapping(meta: dict, native: dict, indexed: dict, traces: dict) -> tuple[dict, dict | None, str | None]:
    mapping = meta.get("mapping")
    aid = meta.get("assistant_message_id")
    if (meta.get("purpose") not in {"assistant", "compaction"} or not isinstance(mapping, dict)
            or mapping.get("basis") not in _BASES):
        return {"status": "unlinked", "basis": "absent", "reason": "no_authoritative_assistant_identity"}, None, aid
    basis = mapping["basis"]
    if basis == "native_processor_span" and mapping.get("status") in {"pending", "provided"}:
        resolved, reason = _processor_owner(meta, traces)
        if resolved is None:
            return {"status": "unlinked", "basis": basis, "reason": reason}, None, aid
        if aid and aid != resolved:
            return {"status": "unlinked", "basis": basis, "reason": "processor_identity_conflict"}, None, aid
        aid = resolved
    elif mapping.get("status") != "provided":
        return {"status": "unlinked", "basis": basis, "reason": "no_authoritative_assistant_identity"}, None, aid
    key = meta["session_id"], aid
    owner, step = native.get(key), indexed.get(key)
    user = native.get((meta["session_id"], meta.get("user_message_id")))
    if (not owner or owner[1].get("role") != "assistant" or not step
            or not user or user[1].get("role") != "user"):
        return {"status": "unlinked", "basis": basis, "reason": "native_owner_not_verified"}, None, aid
    return {"status": "verified", "basis": basis, "reason": "native_session_and_message_id"}, step, aid


def _record_detail(entry: dict, native: dict, indexed: dict, root_session: str, traces: dict) -> dict:
    meta, body = entry["metadata"], entry["json"]
    mapping, step, aid = _mapping(meta, native, indexed, traces)
    sid = meta["session_id"]
    agent = _text(step.get("agent")) if step is not None and step.get("is_sub_agent") else ""
    source = {"kind": "outbound_http_body", "reference": meta["request_id"], "session_id": sid,
              "agent_id": agent, "source_path": "", "raw_index": step["raw_index"] if step else None,
              "step_index": step["index"] if step else None, "body_sha256": meta["body_sha256"]}
    system, messages, system_refs, message_refs = [], [], [], []
    top_system = body.get("system")
    if isinstance(top_system, (str, list)):
        system.extend(top_system if isinstance(top_system, list) else [top_system])
        system_refs.extend([{**source, "field": "system"}] * len(system))
    instructions = body.get("instructions")
    if isinstance(instructions, str):
        system.append(instructions)
        system_refs.append({**source, "field": "instructions"})
    message_field = "messages" if isinstance(body.get("messages"), list) else "input"
    raw_messages = body.get(message_field)
    if isinstance(raw_messages, (list, str)):
        for index, message in enumerate(raw_messages if isinstance(raw_messages, list) else [raw_messages]):
            ref = {**source, "field": message_field, "message_index": index}
            instruction = isinstance(message, dict) and message.get("role") in {"system", "developer"}
            (system if instruction else messages).append(copy.deepcopy(message))
            (system_refs if instruction else message_refs).append(ref)
    tools = body.get("tools") if isinstance(body.get("tools"), list) else []
    separated = {"system", "instructions", "tools", message_field}
    parameters = {key: copy.deepcopy(value) for key, value in body.items() if key not in separated}
    limitations = [_BODY_LIMIT, _USAGE_LIMIT,
                   "Exact bytes and JSON fields are retained; component grouping is a display projection of that body.",
                   "Character-based component sizes are estimates; outbound observation does not verify provider framing."]
    if mapping["status"] != "verified":
        limitations.append("This request has no verified native assistant step; it remains visible without a step link.")
    quality = {"status": "complete", "content_complete": True, "missing_references": [],
               "unsupported_settings": [], "transport_omitted": True,
               "estimation": {"token_counts": "character_estimate", "provider_request_verified": False},
               "outbound_body_observed": True, "body_sha256_verified": True,
               "native_mapping": mapping, "limitations": [_BODY_LIMIT, _USAGE_LIMIT]}
    detail = {"id": meta["request_id"], "label": f"{meta['purpose']} · {meta['request_id'][:8]}",
              "evidence": "captured", "scope": "recorded_request",
              "model": _text(body.get("model")) or _text(meta.get("model_id")),
              "session_id": sid, "agent_id": agent, "step_index": step["index"] if step else None,
              "timestamp_ms": _timestamp(meta.get("observed_at")), "missing_references": [],
              "context_id": "deveco-body:" + sid, "actor_kind": "main" if sid == root_session else "subagent" if agent else "unknown_child",
              "actor_name": agent, "source_path": "", "raw_index": step["raw_index"] if step else None,
              "invocation_id": meta["invocation_id"], "request_id": meta["request_id"],
              "assistant_message_id": aid, "capture_assistant_message_id": meta.get("assistant_message_id"),
              "user_message_id": meta.get("user_message_id"),
              "purpose": meta["purpose"], "status": meta.get("status"), "dispatch_status": meta.get("dispatch_status"),
              "native_mapping": mapping,
              "message_count": len(messages), "evidence_quality": quality,
              "body_sha256": meta["body_sha256"], "body_bytes": meta["body_bytes"],
              "coverage": copy.deepcopy(meta["coverage"]), "system": copy.deepcopy(system),
              "tools": copy.deepcopy(tools), "messages": messages, "parameters": parameters,
              "system_refs": system_refs, "tools_refs": [{**source, "field": "tools", "tool_index": index}
                                                        for index in range(len(tools))],
              "message_refs": message_refs, "sources": [source], "limitations": limitations,
              "serialized_body": entry["body"].decode("utf-8"), "request_body": copy.deepcopy(body),
              "unsupported_fields": []}
    previous = meta.get("previous_request_id")
    if _identity(previous):
        detail["previous_request_id"] = previous
    return detail


def _comparison_row(rows: list[dict], selected: dict, indexed: dict) -> dict | None:
    """A factual pair of bodies, distinct from a recorded request predecessor.

    The immediately earlier native assistant step must have exactly one verified
    physical request. Helpers, missing captures and retries never choose a
    baseline by UUID, arrival order or timestamp.
    """
    index = selected.get("step_index")
    if (selected.get("native_mapping", {}).get("status") != "verified" or type(index) is not int
            or selected.get("purpose") not in {"assistant", "compaction"}):
        return None
    sid = selected["session_id"]
    earlier = [step["index"] for (session, _), step in indexed.items()
               if session == sid and step.get("role") == "assistant" and step["index"] < index]
    if not earlier:
        return None
    previous_index = max(earlier)
    candidates = [row for row in rows if row.get("session_id") == sid
                  and row.get("agent_id") == selected.get("agent_id") and row.get("step_index") == previous_index
                  and row.get("native_mapping", {}).get("status") == "verified"
                  and row.get("purpose") in {"assistant", "compaction"}]
    return candidates[0] if len(candidates) == 1 else None


def inspect_prompt_construction(raw: dict, steps: list[dict], request_id: str | None = None,
                                *, evidence: RequestEvidence) -> dict | None:
    """Compose a model-neutral 0.7.1 Prompt Evolution view, or request fallback.

    This deliberately does not pretend to be a Claude or Pi native record.
    Parent bridge integration checks source pinning and opts this cache in.
    """
    import agent_trajviz
    from agent_trajviz.insight import prompt_construction as core
    from agent_trajviz.insight.prompt_components import composition_summary
    from agent_trajviz.insight.prompt_text import build_text_evolution

    if agent_trajviz.__version__ != "0.7.1" or tuple(inspect.signature(core._components).parameters) != ("detail",):
        raise RequestCaptureError("unsupported_prompt_view_version")
    if request_id is not None and not isinstance(request_id, str):
        raise TypeError("request_id must be a string or None")
    _, native, indexed, binding = _native(raw, steps)
    if not isinstance(evidence, RequestEvidence) or evidence._binding != binding:
        raise RequestCaptureError("request_source_binding_changed")
    if not evidence._records:
        return None
    root = raw["info"]["id"]
    details = [_record_detail(entry, native, indexed, root, evidence._traces) for entry in evidence._records]
    detail_keys = {"system", "tools", "messages", "parameters", "system_refs", "tools_refs", "message_refs",
                   "sources", "limitations", "serialized_body", "request_body", "unsupported_fields"}
    rows = [{key: copy.deepcopy(value) for key, value in detail.items() if key not in detail_keys} for detail in details]
    # Directory enumeration and UUID filenames carry no temporal order. The
    # captured clock is presentation-only; it never supplies native ownership
    # or predecessor lineage. Missing clocks retain the deterministic catalog.
    if all(row["timestamp_ms"] is not None for row in rows):
        rows.sort(key=lambda row: (row["timestamp_ms"], row["id"]))
    rows, order = core._order_requests(rows)
    by_id = {detail["id"]: detail for detail in details}
    # An explicitly recorded predecessor is usable only in the same session and
    # source; self-links and unknown IDs never create a delta baseline.
    positions = {row["id"]: position for position, row in enumerate(rows)}
    for row in rows:
        previous = by_id.get(row.get("previous_request_id"))
        if previous is not None and (previous["session_id"] != row["session_id"]
                or positions[previous["id"]] >= positions[row["id"]]):
            row.pop("previous_request_id", None)
            by_id[row["id"]].pop("previous_request_id", None)
    wanted = request_id if request_id is not None else rows[-1]["id"]
    if wanted not in by_id:
        raise ValueError("Unknown prompt construction request_id")
    load = lambda identifier: copy.deepcopy(by_id[identifier])
    selected = load(wanted)
    selected["components"], selected["token_estimate"] = core._components(selected)
    previous_row = core._previous_row(rows, selected)
    comparison = None
    if previous_row is None and not selected.get("previous_request_id"):
        comparison = _comparison_row(rows, selected, indexed)
        previous_row = comparison
    if comparison is not None:
        selected["comparison_request_id"] = comparison["id"]
        selected["comparison_basis"] = "previous_verified_native_assistant_request"
        for row in rows:
            if row["id"] == selected["id"]:
                row["comparison_request_id"] = comparison["id"]
                row["comparison_basis"] = selected["comparison_basis"]
    previous = load(previous_row["id"]) if previous_row else None
    materialized = [selected]
    if previous is not None:
        previous["components"], previous["token_estimate"] = core._components(previous)
        materialized.append(previous)
    selected["changes"] = core._changes(selected, previous)
    if comparison is not None:
        selected["changes"].update({
            "evidence": "captured", "basis": "previous_verified_native_assistant_request",
            "comparison_request_id": comparison["id"], "lineage_verified": False,
            "reason": "Differences between captured bodies at consecutive native assistant boundaries. "
                      "Native order supplies a display baseline, not request-parent or causal proof."})
    selected["text_evolution"] = build_text_evolution(selected, previous["components"] if previous else [])
    selected["composition"] = composition_summary(selected["components"], selected["token_estimate"])
    # Pass no native steps: one assistant's aggregate usage can span multiple
    # invocations and physical retries, so assigning it per request duplicates it.
    evolution = core._evolution(rows, [])
    data = {"available": True, "requests": rows, "request_order": order, "selected_request": selected,
            "limitations": [_BODY_LIMIT, _USAGE_LIMIT, *evidence.limitations], "format": "deveco", "evolution": evolution}
    core._composition_history(data, load, materialized)
    return data
