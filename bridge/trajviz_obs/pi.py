"""Pi's active branch and native runtime observations.

Saved sessions restore the final entry. A live extension capture additionally
records its actual leaf, which can move without appending a native entry.
"""

from __future__ import annotations

import copy
import functools
import inspect
import math
from datetime import UTC, datetime

from agent_trajviz import LoadError

CAPTURE_KEY = "_trajviz_obs_pi"


def _number(value) -> bool:
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _count(value) -> bool:
    # Pi serializes JavaScript numbers; larger counts are not exact observations.
    return _number(value) and value <= 2 ** 53 - 1 and (isinstance(value, int) or value.is_integer())


def _capture(header: dict) -> dict:
    if CAPTURE_KEY not in header:
        return {}
    capture = header[CAPTURE_KEY]
    if not isinstance(capture, dict) or type(capture.get("schema_version")) is not int or capture["schema_version"] != 1:
        raise LoadError("Unsupported Pi runtime capture schema.", code="unsupported_schema")
    if "leaf_id" in capture and capture["leaf_id"] is not None and (
            not isinstance(capture["leaf_id"], str) or not capture["leaf_id"]):
        raise LoadError("Pi runtime capture requires a valid leaf id or null.", code="invalid_pi_branch")
    if "context_window" in capture and (not _count(capture["context_window"]) or capture["context_window"] < 1):
        raise LoadError("Pi context window must be a positive token count.", code="invalid_pi_capture")
    if "context_tokens" in capture and capture["context_tokens"] is not None and not _count(capture["context_tokens"]):
        raise LoadError("Pi context usage must be a nonnegative token count or null.", code="invalid_pi_capture")
    return capture


def active_branch(events: list) -> list:
    """Select one validated native parent chain without altering source entries."""
    if not events or not isinstance(events[0], dict) or events[0].get("type") != "session":
        return events
    capture = _capture(events[0])
    version = events[0].get("version")
    if type(version) is not int or version < 2:
        if capture:
            raise LoadError("Pi runtime captures require session version 2 or 3.", code="unsupported_schema")
        return events
    if version not in {2, 3}:
        raise LoadError("Unsupported Pi tree version; expected 2 or 3.", code="unsupported_schema")
    entries = {}
    leaf = None
    for event in events[1:]:
        if not isinstance(event, dict) or event.get("type") == "session":
            raise LoadError("Pi tree entries require one leading session header.", code="invalid_pi_branch")
        identity, parent = event.get("id"), event.get("parentId")
        if (not isinstance(identity, str) or not identity or identity in entries or "parentId" not in event
                or (parent is not None and (not isinstance(parent, str) or not parent))):
            raise LoadError("Pi tree entries require unique ids and explicit parentId links.", code="invalid_pi_branch")
        entries[identity] = event
        leaf = identity
    if "leaf_id" in capture:
        leaf = capture["leaf_id"]
    branch, seen = [], set()
    while leaf is not None:
        if leaf in seen:
            raise LoadError("Pi active branch contains a parent cycle.", code="invalid_pi_branch")
        if leaf not in entries:
            raise LoadError("Pi active branch references a missing parent.", code="invalid_pi_branch")
        seen.add(leaf)
        entry = entries[leaf]
        branch.append(entry)
        leaf = entry["parentId"]
    return [events[0], *reversed(branch)]


def excludes_branch(path: str) -> bool:
    """Whether the actual selected source excludes historical entries."""
    from agent_trajviz.insight.loaders import read_payload

    try:
        events, _, error = read_payload(path)
        return not error and isinstance(events, list) and len(active_branch(events)) < len(events)
    except (LoadError, OSError, ValueError, UnicodeError):
        return False  # the upstream identity/read gates retain their normal refusal


def _tokens(usage: dict) -> dict:
    """Retain measured zero counters without assigning zeros to missing fields."""
    tokens = {key: usage[key] for key in ("input", "output", "reasoning") if _count(usage.get(key))}
    tokens["cache"] = {key: usage[native] for key, native in (("read", "cacheRead"), ("write", "cacheWrite"))
                       if _count(usage.get(native))}
    if _count(usage.get("totalTokens")):
        tokens["total"] = usage["totalTokens"]
    components = ("input", "output", "cacheRead", "cacheWrite")
    if (all(_count(usage.get(key)) for key in ("input", "output"))
            and all(key not in usage or _count(usage[key]) for key in components)):
        # Legacy Pi recordings omit cache fields. Preserve the engine's
        # component total while marking the omitted cache counters unknown.
        tokens["total"] = max(sum(usage.get(key, 0) for key in components), tokens.get("total", 0))
    return tokens


def _user_evidence(entry: dict) -> tuple[str, dict] | None:
    """Conversation contributions from Pi's user shell and extensions, not model tools."""
    message = entry.get("message")
    message = message if isinstance(message, dict) else {}
    role = message.get("role")
    if role == "bashExecution":
        fields = {key: copy.deepcopy(message[key]) for key in (
            "command", "output", "exitCode", "cancelled", "truncated", "fullOutputPath", "excludeFromContext")
            if key in message}
        lines = [f"User shell command: {message.get('command', '')}", "Output:", str(message.get("output") or "(no output)")]
        if message.get("cancelled") is True:
            lines.append("Command cancelled by user.")
        elif message.get("exitCode") is not None:
            lines.append(f"Exit code: {message['exitCode']}")
        if message.get("truncated") is True:
            lines.append("Output truncated." + (f" Full output: {message['fullOutputPath']}" if message.get("fullOutputPath") else ""))
        if message.get("excludeFromContext") is True:
            lines.append("Excluded from the model context.")
        return "\n".join(lines), {"native_role": role, **fields}
    if role == "custom" or entry.get("type") == "custom_message":
        native = message if role == "custom" else entry
        content = native.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        texts = [str(block.get("text", "")) for block in blocks or []
                 if isinstance(block, dict) and block.get("type") == "text"] if isinstance(blocks, list) else []
        fields = {key: copy.deepcopy(native[key]) for key in ("customType", "display", "details") if key in native}
        label = f"Extension message: {native.get('customType') or 'custom'}"
        if native.get("display") is False:
            label += " (hidden in Pi)"
        return "\n".join([label, *texts]), {"native_role": "custom", **fields}
    return None


def is_user_prompt(step: dict) -> bool:
    """A human prompt, excluding shell and extension evidence represented on the user side."""
    return step.get("role") == "user" and not any(
        isinstance(part.get("metadata"), dict) and part["metadata"].get("native_role") in {"bashExecution", "custom"}
        for part in step.get("parts", []) if isinstance(part, dict))


def _conversion_input(selected: list) -> list:
    """Bound the engine's numeric casts while keeping the source untouched."""
    normalized = copy.deepcopy(selected)
    used = {entry.get("id") for entry in normalized if isinstance(entry, dict) and isinstance(entry.get("id"), str)}
    for index, entry in enumerate(normalized):
        if not isinstance(entry, dict):
            continue
        if entry.get("type") != "session" and (not isinstance(entry.get("id"), str) or not entry["id"]):
            # Linear v1 recordings can omit ids. A private conversion identity
            # associates native observations without guessing from message text.
            identity = f"_trajviz_obs_pi_source_{index}"
            while identity in used:
                identity = "_" + identity
            entry["id"] = identity
            used.add(identity)
        evidence = _user_evidence(entry)
        if evidence is not None:
            original = entry.get("message")
            original = original if isinstance(original, dict) else {}
            entry["type"] = "message"
            entry["message"] = {"role": "user", "content": evidence[0],
                                "timestamp": original.get("timestamp", entry.get("timestamp"))}
        message = entry.get("message")
        message = message if isinstance(message, dict) else {}
        for holder in (entry, message):
            usage = holder.get("usage")
            if isinstance(usage, dict):
                for key in ("input", "output", "reasoning", "cacheRead", "cacheWrite", "totalTokens"):
                    if key in usage and not _count(usage[key]):
                        usage.pop(key)
        if "durationMs" in message and not _number(message["durationMs"]):
            message.pop("durationMs")
        if isinstance(message.get("timestamp"), (int, float)) and not _number(message["timestamp"]):
            message.pop("timestamp")
    return normalized


def _observations(selected: list, normalized: list, result: dict) -> None:
    """Restore native fields absent from the engine's portable Pi converter."""
    by_id = {converted.get("id"): (index, entry) for index, (entry, converted) in enumerate(zip(selected, normalized))
             if isinstance(entry, dict) and isinstance(converted, dict) and converted.get("id")}
    results, pending, orphaned = {}, {}, []
    usages, auxiliary = [], []
    for entry, converted in zip(selected, normalized):
        if not isinstance(entry, dict):
            continue
        message = entry.get("message")
        message = message if isinstance(message, dict) else {}
        role, kind = message.get("role"), entry.get("type")
        if kind == "message" and role == "assistant":
            content = message.get("content")
            calls = [part for part in content if isinstance(part, dict) and part.get("type") == "toolCall"] \
                if isinstance(content, list) else []
            for index, call in enumerate(calls):
                identity = call.get("id")
                if isinstance(identity, str) and identity:
                    pending[identity] = (converted.get("id"), index)
        if kind == "message" and role == "toolResult":
            identity = message.get("toolCallId")
            owner = pending.pop(identity, None) if isinstance(identity, str) and identity else None
            if owner is not None:
                results[owner] = message
            else:
                orphaned.append(message)
        usage = message.get("usage") if kind == "message" else entry.get("usage")
        if role == "assistant" or kind in {"usage", "compaction", "branch_summary"} or (
                role == "toolResult" and isinstance(usage, dict)):
            usages.append(usage)
            if isinstance(usage, dict) and (role == "toolResult" or kind != "message"):
                auxiliary.append({"entry_id": entry.get("id"), "kind": role or kind,
                                  "included_in_model_totals": role != "toolResult", "usage": copy.deepcopy(usage)})
    costs = [usage["cost"].get("total") if isinstance(usage, dict) and isinstance(usage.get("cost"), dict)
             else None for usage in usages]
    if usages and all(_number(cost) for cost in costs) and _number(sum(costs)):
        result["_pi_reported_cost_usd"] = sum(costs)
        result["_pi_reported_cost_source"] = "provider" if all(
            usage["cost"].get("totalFromProvider") is True for usage in usages) else "estimate"
    if auxiliary:
        result["_pi_auxiliary_usage"] = auxiliary
    orphaned = iter(orphaned)
    for converted in result.get("messages", []):
        info = converted.get("info") or {}
        conversion_id = info.get("id")
        source_index, source = by_id.get(conversion_id, (-1, {}))
        if source and (not isinstance(source.get("id"), str) or not source["id"]):
            info["id"] = ""
            info["rawIndex"] = source_index
        evidence = _user_evidence(source)
        if evidence is not None:
            info["native_role"] = evidence[1]["native_role"]
            for part in converted.get("parts", []):
                if part.get("type") == "text":
                    part.setdefault("metadata", {}).update(evidence[1])
        message = source.get("message")
        message = message if isinstance(message, dict) else {}
        usage = message.get("usage") if source.get("type") == "message" else source.get("usage")
        if isinstance(usage, dict):
            # The engine accepts old Pi records with incomplete usage. Its
            # numeric defaults must not turn absent native counters into zero.
            info["tokens"] = _tokens(usage)
        duration = message.get("durationMs")
        start = info.get("time", {}).get("created")
        if message.get("role") == "assistant":
            if message.get("stopReason") == "pending":
                info.get("time", {}).pop("completed", None)
            elif _number(duration) and _number(start):
                info["time"]["completed"] = start + duration
        tool_index = 0
        for part in converted.get("parts", []):
            if part.get("type") not in {"tool", "tool_call"}:
                continue
            # Providers can reuse a call id in later turns. Match the result to
            # its owning assistant/part, not to the last result with that id.
            tool = next(orphaned, None) if not source and part.get("type") == "tool" \
                else results.get((conversion_id, tool_index))
            tool_index += 1
            if tool is None:
                # A command can inspect between tool call and result. No result
                # proves neither a failure nor a cancellation, including saved files.
                part["status"] = "pending"
                part.pop("error", None)
                if isinstance(part.get("state"), dict):
                    part["state"]["status"] = "pending"
                    part["state"].pop("error", None)
                continue
            details = {}
            if isinstance(tool.get("nestedCalls"), dict):
                details["pi_nested_calls"] = copy.deepcopy(tool["nestedCalls"])
            if isinstance(tool.get("usage"), dict):
                details["pi_usage"] = copy.deepcopy(tool["usage"])
            if details:
                state = part.setdefault("state", {})
                state.setdefault("status", part.get("status"))
                state.setdefault("input", part.get("input", {}))
                state.setdefault("output", part.get("output", ""))
                state.setdefault("metadata", {}).update(details)


def _fork_timing(selected: list, result: dict) -> None:
    """Align a fork's envelope with the inherited steps it actually displays."""
    header = selected[0] if selected and isinstance(selected[0], dict) else {}
    if not header.get("parentSession") or not isinstance(header.get("timestamp"), str):
        return
    try:
        created = datetime.fromisoformat(header["timestamp"])
        if created.tzinfo is None:
            return
        created_ms = created.timestamp() * 1000
    except (ValueError, TypeError, OverflowError):
        return
    starts, finishes = [], []
    for message in result.get("messages", []):
        clock = (message.get("info") or {}).get("time") or {}
        start, end = clock.get("created"), clock.get("completed")
        if _number(start) and start > 0:
            starts.append(start)
            finishes.append(end if _number(end) and end >= start else start)
        for part in message.get("parts", []):
            end = (part.get("state", {}).get("time") or {}).get("end")
            if _number(end) and end > 0:
                finishes.append(end)
    if not starts or min(starts) >= created_ms:
        return
    start, finish = min(starts), max(finishes)
    def iso(stamp):
        return datetime.fromtimestamp(stamp / 1000, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    try:
        started, finished = iso(start), iso(finish)
    except (ValueError, OverflowError, OSError):
        return
    result["info"]["time"].update(created=start, updated=finish)
    result["metadata"]["timestamp_utc"] = started
    result["timing"] = {"total_duration": round((finish - start) / 1000, 3),
                        "started_at": started, "finished_at": finished}
    result["_pi_inherited_timing"] = {"basis": "selected_branch_steps", "fork_created_at": header["timestamp"]}
    result.setdefault("_load_warnings", []).append(
        "Pi fork includes inherited history; elapsed time covers the selected recorded steps, not the fork file's age.")


def install() -> None:
    from agent_trajviz.insight import loaders

    original = loaders._convert_pi_to_internal
    if getattr(original, "_trajviz_obs_active_branch", False):
        return
    if set(inspect.signature(original).parameters) != {"events"}:
        raise RuntimeError("TrajViz's Pi converter changed; update the plugin.")

    @functools.wraps(original)
    def convert(events):
        selected = active_branch(events)
        normalized = _conversion_input(selected)
        result = original(normalized)
        result["_pi_entries"] = copy.deepcopy(events)
        if selected is not events:
            result["_pi_active_entry_ids"] = [entry["id"] for entry in selected[1:]]
            indices = {entry["id"]: index for index, entry in enumerate(events)
                       if isinstance(entry, dict) and entry.get("type") != "session"}
            for message in result.get("messages", []):
                info = message.get("info") or {}
                if info.get("id") in indices:
                    info["rawIndex"] = indices[info["id"]]
        capture = _capture(events[0]) if events and isinstance(events[0], dict) else {}
        if capture:
            result[CAPTURE_KEY] = copy.deepcopy(capture)
            if "context_window" in capture:
                result.setdefault("metadata", {})["context_window_limit"] = int(capture["context_window"])
        _observations(selected, normalized, result)
        _fork_timing(selected, result)
        return result

    convert._trajviz_obs_active_branch = True
    loaders._convert_pi_to_internal = convert
