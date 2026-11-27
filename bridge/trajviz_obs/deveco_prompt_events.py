"""Small, source-backed event markers derived from captured request snapshots.

These markers describe observed values and body differences. They never claim
that DevEco emitted a native instruction/tool/context-edit event, or that a
smaller request proves compaction. The caller selects a unique native baseline;
this helper does not select one from timestamps or directory order.
"""
from __future__ import annotations

from collections import defaultdict

from .deveco_prompt_components import (
    build_component_inventory,
    compare_component_inventory,
    component_inventory_coverage,
)

MAX_EVENT_SOURCE_REFS = 32
_DELTA_BASIS = "previous_verified_native_assistant_request"


def _identity(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 512


def _request_id(detail: dict) -> str | None:
    value = detail.get("request_id") or detail.get("id")
    return value if _identity(value) else None


def _valid(detail: object) -> tuple[bool, str]:
    if not isinstance(detail, dict):
        return False, "request_unavailable"
    if detail.get("purpose") not in {"assistant", "compaction"}:
        return False, "not_an_assistant_request"
    if (detail.get("scope") != "recorded_request" or not isinstance(detail.get("request_body"), dict)
            or not isinstance(detail.get("coverage"), dict) or detail["coverage"].get("complete") is not True):
        return False, "complete_body_unavailable"
    quality = detail.get("evidence_quality") or {}
    if (not isinstance(quality, dict) or quality.get("outbound_body_observed") is not True
            or quality.get("body_sha256_verified") is not True):
        return False, "body_integrity_unverified"
    if (not isinstance(detail.get("native_mapping"), dict) or detail["native_mapping"].get("status") != "verified"
            or not _identity(detail.get("session_id")) or not _identity(detail.get("assistant_message_id"))
            or _request_id(detail) is None or type(detail.get("step_index")) is not int or detail["step_index"] < 0):
        return False, "native_owner_unverified"
    if not component_inventory_coverage(detail)["complete"]:
        return False, "component_inventory_partial"
    return True, "complete_verified_snapshot"


def _coverage(status: str, reason: str, *, applicable: bool = True) -> dict:
    return {"instructions": status, "tools": status, "context_edits": "unavailable",
            "message_differences": "compared" if status == "compared" else "unavailable",
            "basis": _DELTA_BASIS if status == "compared" else "captured_request_snapshot",
            "reason": reason, "applicable": applicable, "native_events": False}


def _source(detail: dict, row: dict, side: str) -> dict:
    return {"kind": "outbound_http_body", "request_id": _request_id(detail),
            "step_index": detail["step_index"], "json_pointer": row["json_pointer"],
            "content_sha256": row["content_sha256"], "side": side}


def _event(kind: str, current: dict, rows: list[dict], *, previous: dict | None = None,
           action: str = "snapshot", group: str, evidence: str) -> dict:
    refs = []
    possible_refs = 0
    for row in rows:
        for side, detail in (("before", previous), ("after", current)):
            value = row.get(side)
            if value is not None and detail is not None:
                possible_refs += 1
                if len(refs) < MAX_EVENT_SOURCE_REFS:
                    refs.append(_source(detail, value, side))
    return {"kind": kind, "evidence": evidence,
            "basis": _DELTA_BASIS if evidence == "snapshot_comparison" else "captured_request_snapshot",
            "native_event": False, "lineage_verified": False, "action": action,
            "group": group, "unit": "components", "count": len(rows),
            "request_id": _request_id(current), "session_id": current["session_id"],
            "step_index": current["step_index"],
            "baseline_request_id": _request_id(previous) if previous is not None else None,
            "source_refs": refs, "source_refs_complete": possible_refs == len(refs),
            "source_refs_truncated": possible_refs != len(refs),
            "source_ref_count": possible_refs}


def _present(row: dict) -> bool:
    # Empty aggregate fields are retained in the exact inventory, but they are
    # not one instruction or tool schema and must not become fake removals.
    value = row.get("content")
    return value is not None and value != "" and value != []


def _inventory(detail: dict) -> list[dict]:
    cached = detail.get("component_inventory")
    total = component_inventory_coverage(detail)["total_components"]
    if (isinstance(cached, list) and len(cached) == total
            and all(isinstance(row, dict) and isinstance(row.get("content_sha256"), str)
                    and isinstance(row.get("json_pointer"), str) for row in cached)):
        return cached
    return build_component_inventory(detail)


def _snapshot_events(current: dict, inventory: list[dict]) -> list[dict]:
    result = []
    for group, kind in (("system", "system_snapshot"), ("tools", "tool_snapshot")):
        rows = [{"before": None, "after": row} for row in inventory if row["group"] == group and _present(row)]
        if rows:
            result.append(_event(kind, current, rows, group=group, evidence="observed_snapshot"))
    return result


def _tool_result(row: dict) -> bool:
    value = row.get("content")
    if not isinstance(value, dict):
        return False
    if value.get("role") in {"tool", "function"} or value.get("type") == "function_call_output":
        return True
    content = value.get("content")
    return isinstance(content, list) and any(isinstance(block, dict)
        and block.get("type") in {"tool_result", "function_call_output"} for block in content)


def build_prompt_events(current: dict, previous: dict | None = None, *,
                        baseline_status: str = "unavailable") -> dict:
    """Return metadata-only markers and explicit coverage for one request.

    ``baseline_status='available'`` means the caller found exactly one captured
    request at the preceding native assistant boundary in this same source.
    Supplying an arbitrary previous request without that assertion produces
    snapshot-presence markers only. Ambiguous retries, missing captures and first
    snapshots therefore cannot be mislabeled as empty prior prompts.

    Chronological events compare consecutive assistant boundaries only. A
    request detail may compare retries within one assistant boundary when an
    explicit retry predecessor was recorded; those comparisons remain separate
    from the assistant-boundary events generated here.
    """
    valid, reason = _valid(current)
    if not valid:
        applicable = isinstance(current, dict) and current.get("purpose") in {"assistant", "compaction"}
        return {"events": [], "coverage": _coverage("unavailable", reason, applicable=applicable)}
    current_inventory = _inventory(current)
    previous_valid, previous_reason = _valid(previous)
    can_compare = baseline_status == "available" and previous_valid
    if can_compare:
        if current["session_id"] != previous["session_id"]:
            can_compare, previous_reason = False, "different_session"
        elif current.get("agent_id") != previous.get("agent_id"):
            can_compare, previous_reason = False, "different_agent"
        elif (previous["step_index"] >= current["step_index"]
              or previous["assistant_message_id"] == current["assistant_message_id"]
              or _request_id(previous) == _request_id(current)):
            can_compare, previous_reason = False, "not_a_previous_assistant_boundary"
    if not can_compare:
        reason = previous_reason if baseline_status == "available" else str(baseline_status)[:80]
        return {"events": _snapshot_events(current, current_inventory),
                "coverage": _coverage("snapshot_only", reason)}

    # The exact inventory retains empty aggregate fields, but event counts track
    # present prompt components. Filter both sides before matching so clearing a
    # field yields a removal, and an empty message collection adds no message.
    # Attach shallow wrappers without mutating or copying the caller's payload.
    current_view = {**current, "component_inventory": [row for row in current_inventory if _present(row)]}
    previous_view = {**previous, "component_inventory": [row for row in _inventory(previous) if _present(row)]}
    delta = compare_component_inventory(current_view, previous_view, basis=_DELTA_BASIS)
    if not delta["available"]:
        return {"events": _snapshot_events(current, current_inventory),
                "coverage": _coverage("snapshot_only", "comparison_unavailable")}

    grouped = defaultdict(list)
    for row in delta["rows"]:
        value = row["after"] or row["before"]
        group, status = value["group"], row["status"]
        if status == "unchanged":
            continue
        if group in {"system", "tools"}:
            if not _present(value):
                continue
            if status == "moved":
                kind = "system_changed" if group == "system" else "tools_changed"
            else:
                prefix = "system_section" if group == "system" else "tool"
                kind = prefix + "_" + status
            grouped[(kind, status, group)].append(row)
        elif group == "messages":
            if status == "added":
                kind = "tool_results" if _tool_result(value) else "context_message"
            else:
                kind = "context_edit"
            grouped[(kind, status, group)].append(row)
    events = [_event(kind, current, rows, previous=previous, action=action, group=group,
                     evidence="snapshot_comparison")
              for (kind, action, group), rows in grouped.items()]
    return {"events": events, "coverage": _coverage("compared", "verified_native_baseline")}
