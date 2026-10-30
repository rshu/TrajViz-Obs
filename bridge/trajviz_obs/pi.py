"""Pi v2/v3 active-parent-chain selection before TrajViz 0.3.1 conversion.

SessionManager restores the final non-header entry as its leaf and getBranch
walks parentId to the root. Historical sibling branches remain source evidence.
"""

from __future__ import annotations

import copy
import functools
import inspect

from agent_trajviz import LoadError


def active_branch(events: list) -> list:
    """Select a valid recorded tree; leave legacy linear sessions unchanged."""
    if not events or not isinstance(events[0], dict) or events[0].get("type") != "session":
        return events
    version = events[0].get("version")
    if type(version) is not int or version < 2:
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
        result = original(selected)
        if selected is not events:
            result["_pi_entries"] = copy.deepcopy(events)
            result["_pi_active_entry_ids"] = [entry["id"] for entry in selected[1:]]
            indices = {entry["id"]: index for index, entry in enumerate(events)
                       if isinstance(entry, dict) and entry.get("type") != "session"}
            for message in result.get("messages", []):
                info = message.get("info") or {}
                if info.get("id") in indices:
                    info["rawIndex"] = indices[info["id"]]
        return result

    convert._trajviz_obs_active_branch = True
    loaders._convert_pi_to_internal = convert
