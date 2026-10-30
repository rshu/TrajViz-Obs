"""OpenCode 2 session materializations at the shared TrajViz loading boundary.

The native source stays in the converted document. Only work messages become
trajectory steps; UI/control records remain available in raw exports. The
capture adapter retains confirmed session families and whole-step timing,
which the upstream single-export parser does not consume.
"""

from __future__ import annotations

import copy
import functools
import inspect
import io
import json
import math
import tempfile
import types
import zipfile
from pathlib import Path
from typing import Any

NATIVE_KEY = "_trajviz_obs_opencode_v2"
NORMALIZER_VERSION = 1
MAX_DEPTH = 32
MAX_BUNDLE_BYTES = 256 * 1024 * 1024
_SPAWN_TOOLS = frozenset({"agent", "subagent", "task"})


def attribution_source(path: str) -> dict | None:
    """Native V2 input for the optional parser guard; never extract an archive."""
    try:
        if Path(path).suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as archive:
                files = [f for f in archive.infolist() if not f.is_dir()]
                if len(files) != 1 or files[0].file_size > MAX_BUNDLE_BYTES:
                    return None
                payload = json.loads(archive.read(files[0]))
        else:
            with open(path, encoding="utf-8-sig") as handle:
                payload = json.load(handle)
        return payload if is_native(payload) else None
    except (OSError, ValueError, RecursionError, zipfile.BadZipFile):
        return None


@functools.lru_cache(maxsize=16)
def attribution_supported(parser, capture: bool) -> bool:
    """Probe actual native tool evidence in an isolated, uncached reference layout."""
    if not isinstance(parser, types.FunctionType):
        return False
    try:
        config = parser.__globals__["config"]
        session = {"id": "ses_probe", "projectID": "probe", "cost": 0,
                   "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
                   "location": {"directory": "/controlled"},
                   "time": {"created": 1790000000000, "updated": 1790000001000}}
        tools = [{"type": "tool", "id": name, "name": name,
                  "time": {"created": 1790000000000, "ran": 1790000000001, "completed": 1790000000010},
                  "state": {"status": "completed", "input": params,
                            "content": [{"type": "text", "text": "1 passed"}]}}
                 for name, params in [("read", {"path": "src/probe.py"}),
                                      ("shell", {"command": "pytest -q tests/test_probe.py"})]]
        messages = [{"type": "user", "id": "msg_user", "text": "Read the source and run its tests.",
                     "time": {"created": 1790000000000}},
                    {"type": "assistant", "id": "msg_probe", "agent": "build",
                     "model": {"providerID": "probe", "id": "probe"}, "content": tools,
                     "time": {"created": 1790000000000, "completed": 1790000001000}}]
        payload = ({"source_format": "opencode_v2", "schema_version": 1, "directory": "/controlled", "session": session,
                    "messages": messages, "children": []} if capture else {"info": session, "messages": messages})
        with tempfile.TemporaryDirectory(prefix="trajviz-obs-attribution-probe-") as folder:
            root = Path(folder)
            path = root / "opencode" / "native-probe.json"
            path.parent.mkdir()
            path.write_text(json.dumps(payload), encoding="utf-8")
            isolated_config = types.SimpleNamespace(**{**vars(config), "TRAJECTORY_DIR": root})
            isolated = types.FunctionType(parser.__code__, {**parser.__globals__, "config": isolated_config},
                                          parser.__name__, parser.__defaults__, parser.__closure__)
            isolated.__kwdefaults__ = parser.__kwdefaults__
            parsed = isolated("opencode", "native-probe")
            calls = {(step.native_tool, step.canonical_action) for step in parsed.steps if step.type == "tool_call"}
            return {("read", "read"), ("shell", "run_test")} <= calls
    except Exception:  # noqa: BLE001 - an unverified parser cannot issue a capability verdict
        return False


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _number(value: Any) -> bool:
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def _count(value: Any) -> bool:
    return _number(value) and value >= 0 and (isinstance(value, int) or value.is_integer())


def is_native(payload: Any) -> bool:
    """Recognize our versioned capture or a native flat-message OpenCode export."""
    if not isinstance(payload, dict) or NATIVE_KEY in payload:
        return False
    if payload.get("source_format") == "opencode_v2":
        return True
    session = payload.get("session") or payload.get("info")
    return (isinstance(session, dict) and isinstance(session.get("id"), str)
            and any(isinstance(m, dict) and m.get("type") in {"assistant", "user", "compaction"}
                    and "info" not in m for m in _list(payload.get("messages"))))


def _session(payload: dict) -> dict:
    return _dict(payload.get("session")) or _dict(payload.get("info"))


def _tokens(value: Any) -> dict:
    tokens = copy.deepcopy(_dict(value))
    cache = _dict(tokens.get("cache"))
    values = [tokens.get("input"), tokens.get("output"), tokens.get("reasoning"),
              cache.get("read"), cache.get("write")]
    # V2 defines these as disjoint components. Missing usage remains missing.
    if all(_count(v) for v in values):
        tokens["total"] = sum(values)
    return tokens


def _content_text(content: Any) -> str:
    if not isinstance(content, list):
        return content if isinstance(content, str) else ""
    return "\n".join(p["text"] if isinstance(p, dict) and isinstance(p.get("text"), str)
                     else json.dumps(p, ensure_ascii=False) for p in content)


def _error_text(value: Any) -> str:
    message = value if isinstance(value, str) else _dict(value).get("message")
    return message if isinstance(message, str) else ""


def _lifecycle(messages: list) -> tuple[str | None, list[str]]:
    """The current root outcome and observed failure history, as in TrajViz 0.3.1."""
    terminal = None
    history = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        kind = message.get("type")
        error = _error_text(message.get("error"))
        if kind in {"user", "assistant", "shell", "compaction"}:
            terminal = error or None
        if kind == "idle":
            terminal = (error or f"Session turn {message['outcome']}.") if message.get("outcome") in {
                "failed", "interrupted"} else None
            error = terminal or ""
        if error:
            history.append(error)
    return terminal, history


def _tool(part: dict, sid: str, mid: str) -> dict:
    state = copy.deepcopy(_dict(part.get("state")))
    error = state.get("error")
    error_type = part.get("error_type")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        # Native errors are structured. Human diagnostics need the message;
        # retain the complete object beside the untouched source envelope.
        state["error"] = error["message"]
        state["metadata"] = {**_dict(state.get("metadata")), "_trajviz_obs_native_error": error}
        error_type = error_type or error.get("type")
    time = _dict(part.get("time"))
    state["time"] = {"start": time.get("ran"), "end": time.get("completed")}
    if "content" in state:
        state["output"] = _content_text(state["content"])
    exit_code = _dict(state.get("metadata")).get("exit")
    if _number(exit_code) and exit_code != 0:
        # V2 shell tools complete successfully as host operations even when
        # the child command fails. Every diagnostic must see that exit status.
        state["status"] = "error"
        state.setdefault("error", f"Exit code {exit_code}")
    name = part.get("name", "?")
    return {**copy.deepcopy(part), "tool": name, "tool_name": "Bash" if name == "shell" else name,
            "callID": f"{sid}:{part.get('id', '')}", "sessionID": sid, "messageID": mid,
            "state": state, "error_type": error_type}


def _message(message: dict, session: dict, *, parent: str, depth: int) -> dict | None:
    kind = message.get("type")
    if kind not in {"assistant", "user", "compaction", "skill"}:
        return None
    # Failed/in-progress compactions are not a changed context window.
    if kind == "compaction" and message.get("status") != "completed":
        return None
    sid, native_mid = session["id"], message.get("id")
    if not isinstance(native_mid, str) or not native_mid:
        raise ValueError("OpenCode 2 message is missing its native id.")
    mid = f"{sid}:{native_mid}"
    model = _dict(message.get("model")) or _dict(session.get("model"))
    info = {**copy.deepcopy(message), "id": mid, "sessionID": sid, "role": "system" if kind == "skill" else kind,
            "modelID": model.get("id", ""), "providerID": model.get("providerID", ""),
            "model": {"modelID": model.get("id", ""), "providerID": model.get("providerID", "")},
            "tokens": _tokens(message.get("tokens")), "isSubAgent": depth > 0,
            "parentSessionID": parent, "sessionDepth": depth, "sessionTitle": session.get("title", ""),
            "agent": message.get("agent") or session.get("agent") or "build"}
    if kind == "assistant" and message.get("error"):
        info.setdefault("finish", "error")
    if kind in {"user", "skill"}:
        # These are prompt records, rather than synthetic or tool-output rows.
        info["tokens"] = {"total": 0, "input": 0, "output": 0, "reasoning": 0,
                          "cache": {"read": 0, "write": 0}}
        parts = ([{"type": "text", "text": message.get("text", "")}]
                 if kind == "user" else [{"type": "opencode_skill", "skill": message.get("name")
                                          or message.get("skill", ""), "native_id": native_mid}])
        if kind == "user":
            parts.extend({"type": "opencode_skill", "skill": skill.get("name") or skill.get("id", ""),
                          "native_id": f"{native_mid}:{skill.get('id', '')}"}
                         for skill in _list(message.get("skills")) if isinstance(skill, dict))
    elif kind == "compaction":
        parts = [{"type": "compaction", "summary": message.get("summary", ""),
                  "reason": message.get("reason", ""), "sessionID": sid, "messageID": mid}]
    else:
        if not isinstance(message.get("content"), list):
            raise ValueError("OpenCode 2 assistant message is missing its content array.")
        parts = [_tool(p, sid, mid) if p.get("type") == "tool" else copy.deepcopy(p)
                 for p in _list(message.get("content")) if isinstance(p, dict)]
    return {"type": kind, "message_id": mid, "info": info, "parts": parts}


def normalize(payload: Any) -> Any:
    """Return the shared OpenCode analysis envelope without mutating native input."""
    if not is_native(payload):
        return payload
    if payload.get("source_format") == "opencode_v2" and payload.get("schema_version") != 1:
        raise ValueError("Unsupported OpenCode materialization schema; expected schema_version 1.")
    root = _session(payload)
    if not isinstance(root.get("id"), str) or not root["id"] or not isinstance(payload.get("messages"), list):
        raise ValueError("OpenCode 2 materialization needs a session id and messages array.")
    sources: dict[str, dict] = {}
    warnings = [w for w in _list(payload.get("warnings")) if isinstance(w, str)]
    terminal_error, _ = _lifecycle(_list(payload.get("messages")))
    history_errors: list[str] = []

    def index(doc: dict, depth: int) -> None:
        session = _session(doc)
        sid = session.get("id")
        if not isinstance(sid, str) or not sid or sid in sources or depth > MAX_DEPTH:
            return
        sources[sid] = doc
        for child in _list(doc.get("children")):
            if isinstance(child, dict):
                index(child, depth + 1)

    index(payload, 0)
    messages: list[dict] = []
    manifest: list[dict] = []
    seen: set[str] = set()
    native_messages: set[tuple[str, str]] = set()

    def collect(sid: str, parent: str, depth: int) -> None:
        if sid in seen or depth > MAX_DEPTH:
            return
        seen.add(sid)
        doc = sources[sid]
        session = _session(doc)
        if depth and session.get("fork"):
            warnings.append(f"Fork {sid} was retained in native data, not counted as a subagent.")
            return
        manifest.append({"info": copy.deepcopy(session), "depth": depth, "parent_session_id": parent})
        _, errors = _lifecycle(_list(doc.get("messages")))
        history_errors.extend(errors)
        children: set[str] = set()
        agent = next((m["agent"] for m in _list(doc.get("messages"))
                      if isinstance(m, dict) and m.get("type") == "assistant" and m.get("agent")),
                     session.get("agent") or "build")
        for message in _list(doc.get("messages")):
            if not isinstance(message, dict):
                continue
            key = sid, message.get("id", "")
            if key in native_messages:
                continue
            native_messages.add(key)
            if message.get("agent"):
                agent = message["agent"]
            converted = _message(message, {**session, "agent": agent}, parent=parent, depth=depth)
            if converted is not None:
                messages.append(converted)
            for part in _list(message.get("content")):
                if not isinstance(part, dict) or str(part.get("name", "")).lower() not in _SPAWN_TOOLS:
                    continue
                child = _dict(_dict(part.get("state")).get("metadata")).get("sessionID")
                if isinstance(child, str) and child and child not in {sid, root["id"]}:
                    children.add(child)
        for child in sorted(children):
            if child in sources:
                collect(child, sid, depth + 1)
            else:
                warnings.append(f"Subagent {child} was referenced but its messages were unavailable.")

    collect(root["id"], "", 0)
    for sid in sources.keys() - seen:
        warnings.append(f"Session {sid} has no confirmed subagent spawn; retained only in native data.")
    # Preserve source order for equal timestamps and deterministic child merges.
    messages.sort(key=lambda m: (_dict(m["info"].get("time")).get("created")
                                if _number(_dict(m["info"].get("time")).get("created")) else math.inf))
    info = copy.deepcopy(root)
    location = _dict(root.get("location"))
    info["directory"] = payload.get("directory") or location.get("directory") or ""
    info["version"] = payload.get("host_version", "2")
    history_errors = list(dict.fromkeys(history_errors))
    warnings.extend(f"OpenCode model/session error: {error}" for error in history_errors)
    return {"info": info, "messages": messages, "session_manifest": manifest,
            "_load_warnings": list(dict.fromkeys(warnings)), NATIVE_KEY: copy.deepcopy(payload),
            "_opencode_v2_errors": [terminal_error] if terminal_error else [],
            "_opencode_v2_history_errors": history_errors,
            "_trajviz_obs_normalizer_version": NORMALIZER_VERSION}


def install() -> None:
    """Adapt all native loads, including browser uploads; leave other formats unchanged."""
    from agent_trajviz.insight import loaders

    original = loaders._normalize_payload
    if getattr(original, "_trajviz_obs_opencode_v2", False):
        return
    if set(inspect.signature(original).parameters) != {"payload", "file_path"}:
        raise RuntimeError("TrajViz's input normalization changed; update the plugin.")

    @functools.wraps(original)
    def normalize_payload(payload, file_path):
        return original(normalize(payload), file_path)

    normalize_payload._trajviz_obs_opencode_v2 = True
    loaders._normalize_payload = normalize_payload
    convert_metadata = loaders._convert_opencode_metadata

    @functools.wraps(convert_metadata)
    def convert_opencode(raw):
        result = convert_metadata(raw)
        window = _dict(result.get(NATIVE_KEY)).get("window")
        if _number(window) and window > 0 and float(window).is_integer():
            # Carry the host's actual model catalog limit through exports and
            # uploads. Explicit caller overrides are applied later by load_run.
            result["metadata"]["context_window_limit"] = int(window)
        return result

    loaders._convert_opencode_metadata = convert_opencode
    # A running V2 message has no completion yet. The generic export fallback
    # substitutes session.updated and would invent a completed model duration.
    from agent_trajviz.insight import parser

    fill_duration = parser._fill_missing_last_step_duration

    @functools.wraps(fill_duration)
    def fill_missing_duration(steps, raw):
        if NATIVE_KEY not in raw:
            fill_duration(steps, raw)
        else:
            messages = _list(raw.get("messages"))
            for step in steps:
                step[NATIVE_KEY] = True
                position = step.get("raw_index", -1)
                message = _dict(messages[position]) if 0 <= position < len(messages) else {}
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
            with open(file_path, "rb") as handle:
                data = handle.read()
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = [member for member in archive.infolist() if not member.is_dir()]
            if len(members) == 1 and members[0].filename.lower().endswith(".json"):
                member = members[0]
                if member.file_size > MAX_BUNDLE_BYTES:
                    return {"_error": "OpenCode session bundle exceeds the 256 MiB uncompressed limit."}
                try:
                    raw = json.loads(archive.read(member))
                except (ValueError, UnicodeError):
                    raw = None
                if is_native(raw):
                    if format_hint and format_hint != "opencode":
                        return loaders._format_mismatch_error(format_hint, "opencode")
                    result = loaders._convert_opencode_metadata(normalize(raw))
                    result.update(_source_path=file_path, _source_sha256=source_sha)
                    return result
        return zip_loader(file_path, format_hint, source_sha, source_data)

    loaders._load_zip_trajectory = load_zip
    _install_observations()
    from .opencode_timing import install as install_timing

    install_timing()


def _install_observations() -> None:
    """Use explicit V2 observations without inventing model tool calls or agent choices."""
    from agent_trajviz.insight import analysis, chart_data, context_usage

    premature = context_usage.detect_premature_compactions

    @functools.wraps(premature)
    def premature_compactions(steps, raw=None, *, window_limit=None):
        rows = premature(steps, raw, window_limit=window_limit)
        if not isinstance(raw, dict) or NATIVE_KEY not in raw:
            return rows
        manual = {step.get("index") for step in steps if step.get("compaction_reason") == "manual"}
        return [row for row in rows if row.get("step") not in manual]

    context_usage.detect_premature_compactions = premature_compactions
    analysis.detect_premature_compactions = premature_compactions
    skill_calls = chart_data.collect_skill_calls

    @functools.wraps(skill_calls)
    def collect_skills(steps, agent_id_of):
        from collections import Counter

        counts = skill_calls(steps, agent_id_of)
        native = Counter()
        for step in steps:
            if not isinstance(step, dict):
                continue
            for part in step.get("parts", []):
                if isinstance(part, dict) and part.get("type") == "opencode_skill":
                    name = _dict(part.get("raw")).get("skill")
                    if isinstance(name, str) and name:
                        native[agent_id_of(step), name] += 1
        # Native activation and its invoking Skill tool describe one action.
        for key, count in native.items():
            counts[key] = max(counts[key], count)
        return counts

    chart_data.collect_skill_calls = collect_skills
