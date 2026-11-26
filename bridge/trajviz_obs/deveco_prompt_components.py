"""Exact JSON-value inventory and conservative comparisons of saved requests.

Pointers locate values within one captured body. They are never message identity.
Component sizes and hashes use canonical JSON; the original wire bytes and their
SHA-256 remain separately available on the parent request.
"""
from __future__ import annotations

import copy
import hashlib
import json
from bisect import bisect_left
from collections import Counter, defaultdict
from difflib import SequenceMatcher

MAX_INVENTORY_COMPONENTS = 4096
_SIZE_BASIS = "canonical_json_utf8"
_LIMITATIONS = [
    "Components retain exact decoded JSON values. Component sizes and hashes use canonical JSON, not original wire slices.",
    "JSON pointers locate values in one snapshot; array indices do not establish component identity.",
    "Anonymous messages are matched by exact content and sequence. An unidentifiable replacement is shown as removed and added, not an inferred update.",
    "Moved marks relative ordering changes, not index shifts caused only by insertion or removal. It can overlap unchanged or updated content.",
    "A comparison shows observed body differences; it does not establish causal influence or model attention.",
]


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _pointer(*parts) -> str:
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts)


def _key(kind: str, value) -> str:
    return kind + ":" + hashlib.sha256(_canonical(value)).hexdigest()


def _native_message_key(message, collection: str) -> str | None:
    if not isinstance(message, dict):
        return None
    for field in ("id", "message_id", "tool_call_id"):
        value = message.get(field)
        if isinstance(value, str) and value:
            return _key("message", [collection, field, value])
    return None


def component_inventory_coverage(detail: dict) -> dict:
    """Count entries before allocating per-component content or source records."""
    body = detail.get("request_body") if isinstance(detail, dict) else None
    if not isinstance(body, dict):
        return {"complete": False, "limit": MAX_INVENTORY_COMPONENTS, "total_components": None,
                "loaded_components": 0, "raw_request_available": False, "size_basis": _SIZE_BASIS}
    total = 0
    for field, value in body.items():
        total += len(value) if field in {"system", "instructions", "tools", "messages", "input"} and isinstance(value, list) and value else 1
    return {"complete": total <= MAX_INVENTORY_COMPONENTS, "limit": MAX_INVENTORY_COMPONENTS,
            "total_components": total, "loaded_components": min(total, MAX_INVENTORY_COMPONENTS),
            "raw_request_available": True, "size_basis": _SIZE_BASIS}


def build_component_inventory(detail: dict) -> list[dict]:
    """Retain every top-level body field, separating ordered prompt collections."""
    body = detail.get("request_body")
    if not isinstance(body, dict):
        return []
    request_id = detail.get("request_id") or detail.get("id")
    inventory = []

    def add(group, kind, label, value, parts, *, match_key=None, role=None,
            collection=None, collection_index=None):
        if len(inventory) >= MAX_INVENTORY_COMPONENTS:
            return
        pointer = _pointer(*parts)
        canonical = _canonical(value)
        inventory.append({
            "id": _key("component", [request_id, pointer]), "match_key": match_key,
            "group": group, "kind": kind, "label": str(label), "role": role,
            "position": len(inventory), "json_pointer": pointer, "request_path": pointer,
            "collection": collection, "collection_index": collection_index,
            "content": copy.deepcopy(value), "utf8_bytes": len(canonical),
            "content_sha256": hashlib.sha256(canonical).hexdigest(), "size_basis": _SIZE_BASIS,
            "source": {"kind": "outbound_http_body", "request_id": request_id, "json_pointer": pointer},
        })

    for field, value in body.items():
        if len(inventory) >= MAX_INVENTORY_COMPONENTS:
            break
        if field in {"system", "instructions"}:
            if isinstance(value, list) and value:
                for index, block in enumerate(value):
                    if len(inventory) >= MAX_INVENTORY_COMPONENTS:
                        break
                    add("system", "system_instruction", f"{field} block {index + 1}", block, (field, index),
                        match_key=_native_message_key(block, field), collection=_pointer(field), collection_index=index)
            else:
                add("system", "system_instruction", field, value, (field,), match_key=_key("field", field))
        elif field == "tools" and isinstance(value, list) and value:
            for index, tool in enumerate(value):
                if len(inventory) >= MAX_INVENTORY_COMPONENTS:
                    break
                function = tool.get("function") if isinstance(tool, dict) else None
                name = (tool.get("name") or (function.get("name") if isinstance(function, dict) else None)) if isinstance(tool, dict) else None
                name = name if isinstance(name, str) and name else None
                add("tools", "tool_definition", name or f"Tool {index + 1}", tool, (field, index),
                    match_key=_key("tool", name) if name else None,
                    collection=_pointer(field), collection_index=index)
        elif field in {"messages", "input"} and isinstance(value, list) and value:
            for index, message in enumerate(value):
                if len(inventory) >= MAX_INVENTORY_COMPONENTS:
                    break
                role = message.get("role") if isinstance(message, dict) else None
                role = role if isinstance(role, str) else None
                group = "system" if role in {"system", "developer"} else "messages"
                kind = "system_instruction" if group == "system" else "message"
                label = f"{role.capitalize()} message {index + 1}" if role else f"{field} item {index + 1}"
                add(group, kind, label, message, (field, index), role=role,
                    match_key=_native_message_key(message, field), collection=_pointer(field), collection_index=index)
        else:
            group = "tools" if field == "tools" else "messages" if field in {"messages", "input"} else "parameters"
            kind = "tool_definition" if group == "tools" else "message" if group == "messages" else "parameter"
            add(group, kind, field, value, (field,), match_key=_key("field", field))
    return inventory


def _pairs(before: list[dict], after: list[dict]) -> tuple[dict[int, int], dict[int, str]]:
    """Match stable unique identities, then exact values without index matching."""
    pairs, bases = {}, {}
    old_keys, new_keys = defaultdict(list), defaultdict(list)
    for index, row in enumerate(before):
        if row.get("match_key"):
            old_keys[row["match_key"]].append(index)
    for index, row in enumerate(after):
        if row.get("match_key"):
            new_keys[row["match_key"]].append(index)
    used = set()
    for key, indices in new_keys.items():
        if len(indices) == 1 and len(old_keys.get(key, [])) == 1:
            new, old = indices[0], old_keys[key][0]
            pairs[new], bases[new] = old, "stable_identity"
            used.add(old)
    # Exact matching is restricted to the same captured collection. Duplicate
    # values remain separate occurrences; matching never implies native identity.
    collections = {row.get("collection") for row in [*before, *after]}
    for collection in collections:
        old = [i for i, row in enumerate(before) if i not in used and row.get("collection") == collection]
        new = [i for i, row in enumerate(after) if i not in pairs and row.get("collection") == collection]
        token = lambda row: (row["group"], row.get("match_key"), row["content_sha256"])
        old_tokens, new_tokens = [token(before[i]) for i in old], [token(after[i]) for i in new]
        old_counts, new_counts = Counter(old_tokens), Counter(new_tokens)
        # Unique exact values can be matched even when they changed order.
        old_unique = {value: old[index] for index, value in enumerate(old_tokens) if old_counts[value] == 1}
        for index, value in enumerate(new_tokens):
            if new_counts[value] == 1 and value in old_unique:
                ni, oi = new[index], old_unique[value]
                pairs[ni], bases[ni] = oi, "exact_content"
                used.add(oi)
        old = [i for i in old if i not in used]
        new = [i for i in new if i not in pairs]
        # SequenceMatcher may become quadratic for huge repeat-heavy histories.
        # A monotonic occurrence queue has the same conservative identity claim
        # for such inputs and avoids an unbounded alignment computation.
        if len(old) * len(new) > 1_000_000:
            queues = defaultdict(list)
            for oi in old:
                queues[token(before[oi])].append(oi)
            offsets = defaultdict(int)
            last = -1
            for ni in new:
                value = token(after[ni]); candidates = queues[value]
                offset = offsets[value]
                while offset < len(candidates) and candidates[offset] <= last:
                    offset += 1
                if offset < len(candidates):
                    oi = candidates[offset]
                    pairs[ni], bases[ni] = oi, "exact_content_sequence"
                    used.add(oi); last = oi; offset += 1
                offsets[value] = offset
        else:
            matcher = SequenceMatcher(None, [token(before[i]) for i in old], [token(after[i]) for i in new], autojunk=False)
            for match in matcher.get_matching_blocks():
                for offset in range(match.size):
                    oi, ni = old[match.a + offset], new[match.b + offset]
                    pairs[ni], bases[ni] = oi, "exact_content_sequence"
                    used.add(oi)
    return pairs, bases


def _moved(before: list[dict], after: list[dict], pairs: dict[int, int], bases: dict[int, str]) -> set[int]:
    """Minimal relative-order moves; adding a message shifts no retained item."""
    by_collection = defaultdict(list)
    for ni, oi in sorted(pairs.items()):
        collection = after[ni].get("collection")
        if collection and before[oi].get("collection") == collection:
            by_collection[collection].append((ni, oi))
    moved = set()
    for items in by_collection.values():
        tails, tail_indices, parents = [], [], []
        for index, (_, oi) in enumerate(items):
            at = bisect_left(tails, oi)
            parent = tail_indices[at - 1] if at else -1
            if at == len(tails):
                tails.append(oi); tail_indices.append(index)
            else:
                tails[at], tail_indices[at] = oi, index
            parents.append(parent)
        keep = set()
        cursor = tail_indices[-1] if tail_indices else -1
        while cursor >= 0:
            keep.add(cursor); cursor = parents[cursor]
        for index, (ni, _) in enumerate(items):
            if index not in keep and bases[ni] != "exact_content_sequence":
                moved.add(ni)
    return moved


def compare_component_inventory(current: dict, previous: dict | None, *, basis: str = "unavailable",
                                lineage_verified: bool = False) -> dict:
    after = current.get("component_inventory")
    after = after if isinstance(after, list) else build_component_inventory(current)
    counts = dict.fromkeys(("added", "removed", "updated", "moved", "unchanged"), 0)
    complete = lambda detail: (isinstance(detail, dict) and isinstance(detail.get("request_body"), dict)
                              and (detail.get("coverage") or {}).get("complete") is True)
    current_coverage = component_inventory_coverage(current)
    previous_coverage = component_inventory_coverage(previous) if isinstance(previous, dict) else None
    partial_inventory = not current_coverage["complete"] or bool(previous_coverage and not previous_coverage["complete"])
    available = (complete(current) and complete(previous) and not partial_inventory
                 and current.get("session_id") == previous.get("session_id"))
    delta = {"available": available, "baseline_request_id": (previous.get("request_id") or previous.get("id")) if available else None,
             "basis": basis if available else "unavailable", "lineage_verified": bool(lineage_verified) if available else False,
             "reason": "Observed differences from the explicitly identified comparison baseline; chronology alone does not establish causation." if available else "No comparable complete captured baseline is available; missing evidence is not an empty prompt.",
             "counts": counts, "rows": [], "limitations": list(_LIMITATIONS)}
    if partial_inventory:
        delta["reason"] = "The component inventory reached its structural limit. No differences are inferred from a partial inventory; inspect the complete raw captured request JSON."
    if available:
        delta.update(baseline_step_index=previous.get("step_index"),
                     baseline_assistant_message_id=previous.get("assistant_message_id"),
                     baseline_session_id=previous.get("session_id"))
    if not available:
        delta["rows"] = [{"id": row["id"], "status": "uncompared", "changes": [], "before": None,
                          "after": row, "match_basis": "unavailable"} for row in after]
        return delta
    before = previous.get("component_inventory")
    before = before if isinstance(before, list) else build_component_inventory(previous)
    pairs, bases = _pairs(before, after)
    moved = _moved(before, after, pairs, bases)
    for ni, row in enumerate(after):
        old = before[pairs[ni]] if ni in pairs else None
        changes = ["added"] if old is None else ["unchanged" if old["content_sha256"] == row["content_sha256"] else "updated"]
        if ni in moved:
            changes.append("moved")
        for change in changes:
            counts[change] += 1
        status = "updated" if "updated" in changes else "moved" if "moved" in changes else changes[0]
        delta["rows"].append({"id": row["id"], "status": status, "changes": changes,
                              "before": old, "after": row, "match_basis": bases.get(ni, "not_matched")})
    used = set(pairs.values())
    for oi, row in enumerate(before):
        if oi not in used:
            counts["removed"] += 1
            delta["rows"].append({"id": row["id"], "status": "removed", "changes": ["removed"],
                                  "before": row, "after": None, "match_basis": "not_matched"})
    return delta
