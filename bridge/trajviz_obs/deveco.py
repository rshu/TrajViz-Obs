"""DevEco Code's versioned, read-only legacy info/parts session capture.

The engine already understands the message and tool records. This adapter only
establishes the product/session boundary and confirmed child relationships; it
does not reinterpret these records as OpenCode V2 or estimate missing usage.
"""

from __future__ import annotations

import copy
import functools
import inspect
import io
import json
import math
import zipfile
from typing import Any

from agent_trajviz import LoadError

NATIVE_KEY = "_trajviz_obs_deveco_v1"
MAX_DEPTH = 32
MAX_BUNDLE_BYTES = 256 * 1024 * 1024
_SPAWN_TOOLS = frozenset({"task", "subagent", "agent"})


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> bool:
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def _count(value: Any) -> bool:
    return _number(value) and value >= 0 and (isinstance(value, int) or value.is_integer())


def is_native(payload: Any) -> bool:
    return isinstance(payload, dict) and payload.get("source_format") == "deveco_v1"


def _invalid(message: str) -> None:
    raise LoadError(message, code="invalid_deveco")


def normalize(payload: Any) -> Any:
    """Project one explicit DevEco capture without changing the native evidence."""
    if not is_native(payload):
        return payload
    if NATIVE_KEY in payload:
        _invalid("DevEco Code native captures cannot contain the reserved normalization marker.")
    documents: dict[str, dict] = {}
    warnings = [row for row in payload.get("warnings", []) if isinstance(row, str)] if isinstance(
        payload.get("warnings", []), list) else []

    def validate(doc: dict, depth: int) -> None:
        if depth > MAX_DEPTH:
            _invalid("DevEco Code session nesting exceeds the supported depth.")
        if not is_native(doc) or type(doc.get("schema_version")) is not int or doc["schema_version"] != 1:
            _invalid("Unsupported DevEco Code capture schema; expected deveco_v1 with schema_version 1.")
        session = doc.get("session")
        if not isinstance(session, dict) or not isinstance(session.get("id"), str) or not session["id"]:
            _invalid("DevEco Code capture requires a native session id.")
        sid = session["id"]
        if sid in documents:
            _invalid("DevEco Code capture contains duplicate or cyclic session identities.")
        if not isinstance(doc.get("directory"), str) or not isinstance(doc.get("messages"), list):
            _invalid("DevEco Code capture requires a directory string and native messages array.")
        if not isinstance(doc.get("children", []), list):
            _invalid("DevEco Code children must be native capture objects.")
        if "window" in doc and (not _count(doc["window"]) or doc["window"] < 1):
            _invalid("DevEco Code context window must be a positive token count.")
        documents[sid] = doc
        seen: set[str] = set()
        for message in doc["messages"]:
            info = _dict(message.get("info")) if isinstance(message, dict) else {}
            mid = info.get("id")
            if not isinstance(mid, str) or not mid or mid in seen:
                _invalid("DevEco Code messages require unique native ids within each session.")
            seen.add(mid)
            if info.get("role") not in {"user", "assistant"} or info.get("sessionID", sid) != sid:
                _invalid("DevEco Code message role or native session identity is invalid.")
            if not isinstance(message.get("parts"), list) or any(not isinstance(p, dict) for p in message["parts"]):
                _invalid("DevEco Code messages require their native parts array.")
        for child in doc.get("children", []):
            if not isinstance(child, dict):
                _invalid("DevEco Code children must be native capture objects.")
            validate(child, depth + 1)

    validate(payload, 0)
    root = payload["session"]["id"]
    messages: list[dict] = []
    manifest: list[dict] = []

    def collect(doc: dict, parent: str, depth: int) -> None:
        session = doc["session"]
        sid = session["id"]
        manifest.append({"info": copy.deepcopy(session), "depth": depth, "parent_session_id": parent})
        spawned: set[str] = set()
        for native in doc["messages"]:
            message = copy.deepcopy(native)
            info = message["info"]
            info.setdefault("sessionID", sid)
            if depth:
                info.update(isSubAgent=True, parentSessionID=parent, sessionDepth=depth,
                            sessionTitle=session.get("title", ""))
            else:
                # Host-provided message fields cannot turn an unrelated session
                # into a prompt-authorized child without its native ancestry.
                info.pop("isSubAgent", None)
                info.pop("parentSessionID", None)
                info.pop("sessionDepth", None)
            if any(part.get("type") == "compaction" for part in message["parts"]):
                message["type"] = "compaction"
            messages.append(message)
            for part in message["parts"]:
                if part.get("type") != "tool" or str(part.get("tool", "")).lower() not in _SPAWN_TOOLS:
                    continue
                metadata = _dict(_dict(part.get("state")).get("metadata"))
                child = metadata.get("sessionID") or metadata.get("sessionId")
                if isinstance(child, str) and child and child not in {sid, root}:
                    spawned.add(child)
        for child in doc.get("children", []):
            identity = child["session"]["id"]
            if (identity in spawned and child["session"].get("parentID") == sid
                    and not child["session"].get("fork")):
                collect(child, sid, depth + 1)
            else:
                warnings.append(f"DevEco Code session {identity} has no confirmed native subagent spawn; "
                                "retained only in native data.")
        present = {child["session"]["id"] for child in doc.get("children", [])}
        for identity in sorted(spawned - present):
            warnings.append(f"DevEco Code subagent {identity} was referenced but its messages were unavailable.")

    collect(payload, "", 0)
    if messages and all(_number(_dict(message["info"].get("time")).get("created")) for message in messages):
        messages.sort(key=lambda message: message["info"]["time"]["created"])
    info = copy.deepcopy(payload["session"])
    info["directory"] = payload["directory"]
    return {"info": info, "messages": messages, "session_manifest": manifest,
            "_source_format": "deveco", "_deveco_format": True,
            "_trajviz_obs_deveco_normalizer_version": 1, NATIVE_KEY: copy.deepcopy(payload),
            "_load_warnings": list(dict.fromkeys(warnings))}


def _convert(raw: dict, native_convert) -> dict:
    normalized = normalize(raw)
    if not isinstance(normalized, dict) or NATIVE_KEY not in normalized or normalized.get("_deveco_format") is not True:
        _invalid("DevEco Code conversion requires an explicit versioned native capture.")
    result = native_convert(normalized)
    native = result[NATIVE_KEY]
    result["metadata"].update(agent="deveco", generator_name="deveco", source_format="deveco_v1")
    window = native.get("window")
    if _count(window) and window > 0:
        result["metadata"]["context_window_limit"] = int(window)
    # Session.updated is a native session write clock, never a substitute for
    # an unfinished assistant message's completion time.
    clock = _dict(native["session"].get("time"))
    if not all(_number(clock.get(key)) for key in ("created", "updated")):
        result.get("timing", {}).pop("total_duration", None)
    assistants = [row["info"] for row in result["messages"] if row["info"].get("role") == "assistant"]
    usage = {}
    for source, target in (("input", "prompt_tokens"), ("output", "completion_tokens"), ("total", "total_tokens")):
        if assistants and all(_count(_dict(row.get("tokens")).get(source)) for row in assistants):
            usage[target] = sum(row["tokens"][source] for row in assistants)
    result["token_usage"] = usage
    result["_load_warnings"] = [row.replace("OpenCode session ", "DevEco Code session ", 1)
                                for row in result.get("_load_warnings", [])]
    return result


def install() -> None:
    """Reuse the pinned engine's legacy parser for every native load path."""
    from agent_trajviz import plugins
    from agent_trajviz.insight import loaders, parser

    original = loaders._normalize_payload
    if getattr(original, "_trajviz_obs_deveco_v1", False):
        return
    if set(inspect.signature(original).parameters) != {"payload", "file_path"}:
        raise RuntimeError("TrajViz's input normalization changed; update the DevEco Code adapter.")
    # The public custom-format parser discards raw documents. Register at the
    # same native conversion seam used by the bridge's other adapters instead.
    loaders.FORMAT_LABELS["deveco"] = "DevEco Code"
    plugins._LABELS["deveco"] = "DevEco Code"

    @functools.wraps(original)
    def normalize_payload(payload, file_path):
        return original(normalize(payload), file_path)

    normalize_payload._trajviz_obs_deveco_v1 = True
    loaders._normalize_payload = normalize_payload
    apply_format = loaders._apply_format

    @functools.wraps(apply_format)
    def apply(payload, fmt, *, source_path=None):
        if fmt == "deveco":
            return _convert(payload, loaders._convert_opencode_metadata)
        return apply_format(payload, fmt, source_path=source_path)

    loaders._apply_format = apply
    fill_duration = parser._fill_missing_last_step_duration

    @functools.wraps(fill_duration)
    def fill_missing_duration(steps, raw):
        if NATIVE_KEY not in raw:
            fill_duration(steps, raw)
            return
        messages = raw.get("messages", [])
        for step in steps:
            index = step.get("raw_index", -1)
            message = messages[index] if isinstance(index, int) and 0 <= index < len(messages) else {}
            tokens = _dict(_dict(message.get("info")).get("tokens"))
            for key, label in {"total": "Total Tokens", "input": "Input Tokens", "output": "Output Tokens",
                               "reasoning": "Reasoning Tokens", "cache_read": "Cache Read",
                               "cache_write": "Cache Write"}.items():
                value = _dict(tokens.get("cache")).get(key.removeprefix("cache_")) if key.startswith(
                    "cache_") else tokens.get(key)
                if not _count(value) and label not in step["_metrics_unavailable_fields"]:
                    step["_metrics_unavailable_fields"].append(label)

    parser._fill_missing_last_step_duration = fill_missing_duration
    zip_loader = loaders._load_zip_trajectory

    @functools.wraps(zip_loader)
    def load_zip(file_path, format_hint, source_sha, source_data=None):
        data = source_data
        if data is None:
            with open(file_path, "rb") as source:
                data = source.read()
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = [member for member in archive.infolist() if not member.is_dir()]
            if len(members) == 1 and members[0].filename.lower().endswith(".json"):
                member = members[0]
                if member.file_size <= MAX_BUNDLE_BYTES:
                    try:
                        payload = json.loads(archive.read(member))
                    except (ValueError, UnicodeError):
                        payload = None
                    if is_native(payload):
                        if format_hint and format_hint != "deveco":
                            return loaders._format_mismatch_error(format_hint, "deveco")
                        result = _convert(payload, loaders._convert_opencode_metadata)
                        result.update(_source_format="deveco", _source_path=file_path,
                                      _source_sha256=source_sha)
                        return result
        return zip_loader(file_path, format_hint, source_sha, source_data)

    loaders._load_zip_trajectory = load_zip
