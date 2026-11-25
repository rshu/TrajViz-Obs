"""Escaped, read-only views of captured prompt composition and verified changes."""

from __future__ import annotations

import difflib
import hashlib
import html
import json
from collections import OrderedDict

MAX_CONTENT_CHARACTERS = 24_000
MAX_DIFF_LINES = 240
MAX_VISIBLE_COMPONENTS = 180
MAX_TIMELINE_ITEMS = 100
_MISSING = object()

EXPLORER_CSS = """
#obs-prompt-explorer { color:var(--ov-text,#0f172a); min-width:0; font-size:14px; line-height:1.5; }
#obs-prompt-explorer * { box-sizing:border-box; }
#obs-prompt-explorer h2,#obs-prompt-explorer h3,#obs-prompt-explorer h4 { color:inherit; margin:0; line-height:1.35; }
#obs-prompt-explorer h2 { font-size:19px; }
#obs-prompt-explorer h3 { font-size:15px; }
#obs-prompt-explorer h4 { font-size:13px; }
#obs-prompt-explorer p { margin:0; }
#obs-prompt-explorer button { font:inherit; color:inherit; cursor:pointer; }
#obs-prompt-explorer button:focus-visible,#obs-prompt-explorer summary:focus-visible {
 outline:2px solid var(--ov-accent,#2563eb); outline-offset:3px; }
#obs-prompt-explorer .pe-muted { color:var(--ov-muted,#64748b); font-size:12px; }
#obs-prompt-explorer .pe-heading { display:flex; justify-content:space-between; gap:12px; align-items:flex-start; flex-wrap:wrap; }
#obs-prompt-explorer .pe-header { display:grid; gap:7px; margin-bottom:18px; }
#obs-prompt-explorer .pe-meta { display:flex; flex-wrap:wrap; gap:5px 14px; align-items:baseline; font-size:12px; color:var(--ov-muted,#64748b); }
#obs-prompt-explorer .pe-meta > span { min-width:0; max-width:100%; overflow-wrap:anywhere; }
#obs-prompt-explorer .pe-badge { display:inline-flex; align-items:center; border:1px solid var(--ov-border,#dce3ef);
 border-radius:5px; padding:2px 7px; font-size:11px; font-weight:600; line-height:1.6; overflow-wrap:anywhere; }
#obs-prompt-explorer .pe-evidence { display:flex; flex-wrap:wrap; gap:5px; align-items:center; }
#obs-prompt-explorer .pe-note { padding:10px 12px; background:var(--background-fill-secondary,#f1f5f9);
 border-left:3px solid var(--ov-accent,#2563eb); border-radius:0 5px 5px 0; font-size:12px; }
#obs-prompt-explorer .pe-note.pe-warning { border-left-color:#b7791f; }
#obs-prompt-explorer .pe-space { display:grid; gap:12px; }
#obs-prompt-explorer .pe-composition { display:grid; gap:10px; margin-bottom:18px; }
#obs-prompt-explorer .pe-stack { height:22px; display:flex; overflow:hidden; border-radius:4px;
 background:var(--background-fill-secondary,#e2e8f0); }
#obs-prompt-explorer .pe-segment { height:100%; flex-shrink:0; }
#obs-prompt-explorer .pe-legend { display:flex; flex-wrap:wrap; gap:8px 20px; font-size:12px; }
#obs-prompt-explorer .pe-legend-item { display:inline-flex; align-items:center; gap:6px; }
#obs-prompt-explorer .pe-dot { width:9px; height:9px; display:inline-block; border-radius:2px; flex-shrink:0; }
#obs-prompt-explorer .pe-total { font-weight:650; font-variant-numeric:tabular-nums; }
#obs-prompt-explorer .pe-timeline-section { display:grid; gap:9px; margin-bottom:20px; min-width:0; }
#obs-prompt-explorer .pe-timeline { display:flex; gap:6px; overflow-x:auto; padding:4px 3px 9px; max-width:100%; min-width:0; }
#obs-prompt-explorer .pe-timeline-item { display:grid; gap:5px; flex:0 0 92px; min-height:70px; align-content:start;
 text-align:left; padding:8px; background:var(--background-fill-primary,transparent); border:1px solid var(--ov-border,#dce3ef); border-radius:6px; }
#obs-prompt-explorer .pe-timeline-item.is-selected { border-color:var(--ov-accent,#2563eb); box-shadow:inset 0 0 0 1px var(--ov-accent,#2563eb); }
#obs-prompt-explorer .pe-timeline-item strong { font-size:12px; font-weight:650; }
#obs-prompt-explorer .pe-navigation { display:flex; flex-wrap:wrap; justify-content:space-between; gap:8px; align-items:center; }
#obs-prompt-explorer .pe-navigation-buttons { display:flex; flex-wrap:wrap; gap:6px; }
#obs-prompt-explorer .pe-nav-button { padding:6px 10px; min-height:34px; border:1px solid var(--ov-border,#dce3ef);
 border-radius:5px; background:var(--background-fill-primary,transparent); font-size:12px; }
#obs-prompt-explorer .pe-nav-button:disabled { cursor:default; color:var(--ov-muted,#64748b); opacity:.65; }
#obs-prompt-explorer .pe-page-status { display:grid; gap:5px; margin:0 0 14px; padding:8px 0;
 border-bottom:1px solid var(--ov-border,#dce3ef); }
#obs-prompt-explorer .pe-mini-stack { display:flex; height:12px; overflow:hidden; border-radius:2px;
 background:var(--background-fill-secondary,#e2e8f0); }
#obs-prompt-explorer .pe-timeline-size { font-size:10px; color:var(--ov-muted,#64748b); white-space:nowrap; }
#obs-prompt-explorer .pe-layout { display:grid; gap:18px; grid-template-columns:minmax(230px,.75fr) minmax(0,1.6fr); align-items:start; }
#obs-prompt-explorer .pe-components-heading { display:flex; flex-wrap:wrap; justify-content:space-between; align-items:baseline; gap:6px; margin-bottom:8px; }
#obs-prompt-explorer .pe-components { display:grid; gap:5px; max-height:640px; overflow-y:auto; padding:3px; }
#obs-prompt-explorer .pe-component { display:grid; gap:4px; width:100%; min-width:0; padding:9px 10px;
 text-align:left; border:1px solid var(--ov-border,#dce3ef); border-radius:5px; background:var(--background-fill-primary,transparent); }
#obs-prompt-explorer .pe-component.is-selected { border-color:var(--ov-accent,#2563eb); background:color-mix(in srgb,var(--ov-accent,#2563eb) 7%,transparent); }
#obs-prompt-explorer .pe-component-name { display:flex; gap:6px; align-items:baseline; font-size:12px; font-weight:600; overflow-wrap:anywhere; }
#obs-prompt-explorer .pe-position { color:var(--ov-muted,#64748b); font-size:11px; font-weight:400; flex-shrink:0; }
#obs-prompt-explorer .pe-component-meta { display:flex; flex-wrap:wrap; gap:4px 9px; align-items:center; font-size:11px; color:var(--ov-muted,#64748b); }
#obs-prompt-explorer .pe-change { font-size:10px; font-weight:650; }
#obs-prompt-explorer .pe-added { color:var(--ov-success,#15803d); }
#obs-prompt-explorer .pe-removed { color:var(--ov-bad,#b91c1c); }
#obs-prompt-explorer .pe-modified { color:var(--ov-accent,#2563eb); }
#obs-prompt-explorer .pe-moved { color:var(--ov-warn,#92600c); }
#obs-prompt-explorer .pe-detail { min-width:0; display:grid; gap:11px; }
#obs-prompt-explorer .pe-detail-heading { display:grid; gap:5px; }
#obs-prompt-explorer .pe-detail-title { overflow-wrap:anywhere; }
#obs-prompt-explorer .pe-detail dl { display:grid; grid-template-columns:max-content minmax(0,1fr); gap:4px 12px; margin:0; font-size:12px; }
#obs-prompt-explorer .pe-detail dt { color:var(--ov-muted,#64748b); }
#obs-prompt-explorer .pe-detail dd { margin:0; overflow-wrap:anywhere; }
#obs-prompt-explorer pre { margin:0; white-space:pre-wrap; overflow-wrap:anywhere; word-break:break-word;
 font:12px/1.6 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; background:var(--background-fill-secondary,#f8fafc);
 color:inherit; border:1px solid var(--ov-border,#dce3ef); padding:12px; border-radius:5px; max-height:520px; overflow:auto; }
#obs-prompt-explorer details { min-width:0; }
#obs-prompt-explorer summary { cursor:pointer; font-size:12px; font-weight:600; margin-bottom:7px; }
#obs-prompt-explorer .pe-diff-line { display:block; min-height:1.6em; padding:0 5px; }
#obs-prompt-explorer .pe-diff-add { background:color-mix(in srgb,#16a34a 12%,transparent); }
#obs-prompt-explorer .pe-diff-remove { background:color-mix(in srgb,#dc2626 12%,transparent); }
#obs-prompt-explorer .pe-diff-header { color:var(--ov-muted,#64748b); }
#obs-prompt-explorer .pe-content-pair { display:grid; gap:12px; grid-template-columns:repeat(2,minmax(0,1fr)); }
#obs-prompt-explorer .pe-content-pair section { display:grid; align-content:start; gap:7px; min-width:0; }
#obs-prompt-explorer .pe-change-summary { display:flex; flex-wrap:wrap; gap:5px 14px; font-size:12px; }
#obs-prompt-explorer .pe-empty { padding:24px 0; display:grid; gap:9px; max-width:720px; }
#obs-prompt-explorer .pe-truncated { font-size:11px; color:var(--ov-muted,#64748b); margin-top:6px; }
@media (max-width:800px) {
 #obs-prompt-explorer .pe-layout { grid-template-columns:minmax(200px,.8fr) minmax(0,1.4fr); gap:12px; }
 #obs-prompt-explorer .pe-content-pair { grid-template-columns:minmax(0,1fr); }
}
@media (max-width:560px) {
 #obs-prompt-explorer .pe-layout { grid-template-columns:minmax(0,1fr); }
 #obs-prompt-explorer .pe-components { max-height:300px; }
 #obs-prompt-explorer .pe-detail dl { grid-template-columns:minmax(0,1fr); gap:2px; }
 #obs-prompt-explorer .pe-detail dd { margin-bottom:5px; }
 #obs-prompt-explorer .pe-timeline-item { flex-basis:82px; }
 #obs-prompt-explorer pre { padding:9px; }
}
"""

_KINDS = {
    "system": ("System instructions", "#64748b"),
    "system_section": ("System sections", "#64748b"),
    "tool_definition": ("Tool definitions", "#8b5cf6"),
    "conversation": ("Conversation", "#2563eb"),
    "tool_result": ("Tool results", "#0d9488"),
    "component_observation": ("Component observation", "#b7791f"),
    "opaque": ("Other captured content", "#94a3b8"),
}
_ADAPTERS = {"pi": "Pi", "claude-code": "Claude Code", "opencode": "OpenCode", "deveco": "DevEco Code"}
_CHANGE_LABELS = {"added": "Added", "removed": "Removed", "modified": "Changed", "moved": "Moved", "unchanged": "Unchanged"}


def _escape(value) -> str:
    return html.escape(str(value), quote=True)


def _mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def _bytes(value) -> int | None:
    return value if type(value) is int and value >= 0 else None


def format_bytes(value: int | None) -> str:
    if value is None:
        return "Size unknown"
    if value < 1024:
        return f"{value:,} B"
    if value < 1024 * 1024:
        return f"{value / 1024:,.1f} KiB"
    return f"{value / (1024 * 1024):,.2f} MiB"


def component_token(identity: str) -> str:
    """Controls carry generated identifiers, never captured text or arbitrary native IDs."""
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _selection(record: dict) -> str:
    return str(record.get("selection_id") or record.get("record_id") or "")


def _title(component: dict) -> str:
    identity = str(component.get("id") or "Captured component")
    content = component.get("content")
    data = _mapping(content)
    name = data.get("name") or _mapping(data.get("function")).get("name")
    if component.get("kind") == "tool_definition" and isinstance(name, str):
        return name[:180]
    if identity.startswith("system:section:"):
        return identity[len("system:section:"):][:180]
    role = component.get("role")
    if role:
        label = {"toolResult": "Tool result", "tool_result": "Tool result"}.get(str(role), str(role).replace('_', ' ').capitalize())
        return f"{label} message"
    if identity.startswith("component."):
        return identity.removeprefix("component.").replace("_", " ").capitalize()
    return identity[:180]


def _rows(record: dict, mode: str) -> list[dict]:
    key = "effective_state" if mode == "effective" else "components"
    return [row for row in record.get(key, []) if isinstance(row, dict)]


def _composition(rows: list[dict], *, mini: bool = False) -> str:
    groups: OrderedDict[str, int] = OrderedDict()
    unknown = 0
    for row in rows:
        size = _bytes(row.get("utf8_bytes"))
        if size is None:
            unknown += 1
            continue
        kind = row.get("kind") if row.get("kind") in _KINDS else "opaque"
        groups[kind] = groups.get(kind, 0) + size
    total = sum(groups.values())
    segments = []
    for kind, size in groups.items():
        if not size or not total:
            continue
        label, color = _KINDS[kind]
        segments.append(f'<span class="pe-segment" style="width:{100 * size / total:.8f}%;background:{color}" '
                        f'title="{_escape(label)}: {_escape(format_bytes(size))}"></span>')
    description = "; ".join(f"{_KINDS[kind][0]}: {format_bytes(size)}" for kind, size in groups.items()) or "No measured component sizes"
    bar = f'<div class="{"pe-mini-stack" if mini else "pe-stack"}" role="img" aria-label="{_escape(description)}">{"".join(segments)}</div>'
    if mini:
        return bar + f'<span class="pe-timeline-size">{_escape(format_bytes(total) if not unknown else "Partial size")}</span>'
    legend = "".join(f'<span class="pe-legend-item"><span class="pe-dot" style="background:{_KINDS[kind][1]}"></span>'
                     f'{_escape(_KINDS[kind][0])} <strong>{_escape(format_bytes(size))}</strong></span>' for kind, size in groups.items())
    note = f" · {unknown} component size{'s' if unknown != 1 else ''} unknown" if unknown else ""
    return ('<section class="pe-composition" aria-label="Captured composition">'
            f'<div class="pe-heading"><h3>Composition</h3><span class="pe-total">{_escape(format_bytes(total))}{_escape(note)}</span></div>'
            f'{bar}<div class="pe-legend">{legend}</div>'
            '<p class="pe-muted">Serialized UTF-8 bytes of captured components. These are not token counts or context-window occupancy.</p></section>')


def _content(value) -> tuple[str, bool]:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    clipped = len(text) > MAX_CONTENT_CHARACTERS
    return text[:MAX_CONTENT_CHARACTERS], clipped


def _pre(value=_MISSING, *, unavailable: str = "Not present in this snapshot") -> str:
    if value is _MISSING:
        return f'<p class="pe-muted">{_escape(unavailable)}</p>'
    text, clipped = _content(value)
    note = '<p class="pe-truncated">Preview limited to 24,000 characters. The captured record is unchanged.</p>' if clipped else ""
    return f'<pre>{_escape(text)}</pre>{note}'


def _diff(before, after) -> str:
    old, old_clipped = _content(before) if before is not _MISSING else ("", False)
    new, new_clipped = _content(after) if after is not _MISSING else ("", False)
    old_lines, new_lines = old.splitlines(), new.splitlines()
    clipped = old_clipped or new_clipped or len(old_lines) > MAX_DIFF_LINES or len(new_lines) > MAX_DIFF_LINES
    lines = difflib.unified_diff(old_lines[:MAX_DIFF_LINES], new_lines[:MAX_DIFF_LINES], fromfile="Before", tofile="After", lineterm="", n=3)
    rendered = []
    for line in lines:
        style = "pe-diff-header" if line.startswith(("---", "+++", "@@")) else "pe-diff-add" if line.startswith("+") else "pe-diff-remove" if line.startswith("-") else ""
        rendered.append(f'<span class="pe-diff-line {style}">{_escape(line) or " "}</span>')
    body = f'<pre class="pe-diff" aria-label="Component changes">{"".join(rendered)}</pre>' if rendered else '<p class="pe-muted">No text differences in this preview.</p>'
    if clipped:
        body += '<p class="pe-truncated">Diff preview is limited to 240 lines and 24,000 characters per side. Compare the local records for the complete content.</p>'
    return body


def _same_lane(record: dict, selected: dict) -> bool:
    if record.get("lane_id") is not None and selected.get("lane_id") is not None:
        return record["lane_id"] == selected["lane_id"]
    keys = ("adapter", "session_id", "instance_id", "stage", "kind")
    a, b = _mapping(record.get("correlation")), _mapping(selected.get("correlation"))
    return (all(record.get(key) == selected.get(key) for key in keys)
            and a.get("agent_id") == b.get("agent_id") and a.get("branch_id") == b.get("branch_id")
            and a.get("lineage_id", a.get("run_id")) == b.get("lineage_id", b.get("run_id")))


def _baseline(records: list[dict], selected: dict) -> dict | None:
    identity = selected.get("baseline_selection_id")
    if identity:
        return next((row for row in records if _selection(row) == identity and _same_lane(row, selected)), None)
    identity = selected.get("baseline_record_id")
    candidates = [row for row in records if row.get("record_id") == identity and _same_lane(row, selected)] if identity else []
    return candidates[0] if len(candidates) == 1 else None


def _evidence(record: dict) -> tuple[str, str]:
    stage = str(record.get("stage") or "")
    if stage == "provider.request":
        return "Provider request parameters", "This is the captured API payload at the recorded boundary. It may be a continuation delta; earlier context can remain on the provider."
    if stage.startswith("component."):
        return "Unlinked component observation", "This component was observed independently. Its request association is not proven, so it is not compared with another request."
    if stage in {"context", "request.context"}:
        if record.get("adapter") == "claude-code":
            return ("Normalized engine context",
                    ("System instructions and tool declarations may be exposed separately as unlinked observations. "
                     "This view is not a reconstructed full API request."))
        return "Engine context", "Captured before provider-specific transformations. Later handlers may change this context."
    if stage == "lifecycle.compaction":
        return "Compaction result", "This records the native compaction event. Its internal summarizer prompt may not be exposed by the host."
    return "Capture observation", "This observation does not by itself prove a model request was dispatched."


def _page_status(dataset: dict) -> str:
    """Only explicit page summaries are rendered; opaque cursors and paths stay server-side."""
    page = _mapping(dataset.get("page"))
    if not page:
        return ""
    observations = _bytes(page.get("observations"))
    known = _bytes(page.get("known_observations"))
    oversized = _bytes(page.get("omitted_oversized")) or 0
    older, newer = page.get("has_older") is True, page.get("has_newer") is True
    parts = ['<section class="pe-page-status" aria-label="Capture page status">']
    if observations is not None:
        text = f"{observations:,} observation{'s' if observations != 1 else ''} loaded on this page"
        if known is not None and known >= observations:
            text += f" · {known:,} known in this session"
        parts.append(f'<p class="pe-muted">{_escape(text)}.</p>')
    availability = []
    if older:
        availability.append("Earlier captures available")
    if newer:
        availability.append("Later captures available")
    if availability:
        parts.append(f'<p class="pe-muted">{_escape(" · ".join(availability))}. Use page navigation to load them.</p>')
    if (older or newer) and page.get("comparison_boundary") == "visible_page_only":
        parts.append('<p class="pe-muted">Comparisons use observations on the loaded page; they do not bridge an unloaded page boundary.</p>')
    if oversized:
        parts.append(f'<p class="pe-muted">{oversized:,} oversized observation bod{"y is" if oversized == 1 else "ies are"} represented by capture-gap markers. Missing content is not an empty prompt.</p>')
    parts.append('</section>')
    return "".join(parts)


def _snapshot_badges(selected: dict, baseline: dict | None) -> str:
    stage = str(selected.get("stage") or "")
    provenance = _mapping(selected.get("provenance"))
    badges = []
    if provenance.get("point") == "claude_native_request_log":
        badges.append(("Native API log", "Request parameters from the host's local log. This is not a reconstruction of server-held context."))
    elif stage in {"context", "request.context", "provider.request"}:
        badges.append(("Host observation", "Captured at the named host boundary; later handlers or provider processing may differ."))
    elif stage.startswith("component."):
        badges.append(("Observed separately", "No request association is proven for this component observation."))
    if selected.get("remote_context"):
        badges.append(("Continuation payload", "Earlier context can remain on the provider and is not reconstructed here."))
    if selected.get("omitted_reason"):
        reason = selected["omitted_reason"]
        label = "Body not loaded" if reason == "record_exceeds_page_budget" else "No prompt components exposed" if reason == "no_prompt_components_exposed" else "Partial capture"
        badges.append((label, "The loaded evidence does not expose a complete prompt at this boundary."))
    elif selected.get("comparison_status") == "capture_gap":
        badges.append(("Capture gap", "The writer reported lost observations. Their request association is unknown; differences across that loss are not inferred."))
    elif selected.get("comparison_status") == "page_boundary":
        badges.append(("Page boundary", "An earlier comparable observation may be on another page; no comparison is inferred across that boundary."))
    elif baseline is not None:
        native = selected.get("baseline_relation") == "native_previous_message"
        badges.append(("Native predecessor" if native else "Comparable baseline",
                       "Linked by the native previous-message reference." if native else "An earlier observation in the same recorded lane."))
    else:
        badges.append(("No comparable baseline", "No verified predecessor is available in the loaded evidence."))
    boundary = provenance.get("hook") or provenance.get("point")
    if isinstance(boundary, str) and boundary and boundary != "claude_native_request_log":
        badges.append((f"Boundary: {boundary[:100]}", "Recorded host hook or capture point."))
    return '<div class="pe-evidence" aria-label="Capture evidence">' + "".join(
        f'<span class="pe-badge" title="{_escape(explanation)}">{_escape(label)}</span>' for label, explanation in badges) + '</div>'


def _component_badge(component: dict, selected: dict, mode: str) -> str:
    if mode == "effective":
        label, reason = "Replayed native state", "Derived from the recorded Pi section and tool patches. Current files are not reread."
    elif str(selected.get("stage") or "").startswith("component."):
        label, reason = "Separate observation", "The host exposed this component without a proven request association."
    else:
        label, reason = "Captured component", "This content is retained in the selected observation at its recorded boundary."
    source_label = "Source metadata recorded" if component.get("source") else "Source not attributed"
    return ('<div class="pe-evidence" aria-label="Component evidence">'
            f'<span class="pe-badge" title="{_escape(reason)}">{label}</span>'
            f'<span class="pe-badge">{source_label}</span><span class="pe-badge">Tokens unmeasured</span></div>')


def _timeline(records: list[dict], selected: dict, mode: str) -> str:
    lane = [record for record in records if _same_lane(record, selected)
            and (_rows(record, mode) or record.get("omitted_reason"))]
    if not lane:
        return ""
    index = next((index for index, row in enumerate(lane) if _selection(row) == _selection(selected)), 0)
    start = max(0, min(index - MAX_TIMELINE_ITEMS // 2, len(lane) - MAX_TIMELINE_ITEMS))
    visible = lane[start:start + MAX_TIMELINE_ITEMS]
    buttons = []
    for offset, row in enumerate(visible, start + 1):
        current = _selection(row) == _selection(selected)
        title = f"Select snapshot {offset}, {row.get('observed_at', 'time unknown')}"
        summary = ('<span class="pe-timeline-size pe-warning">Capture incomplete</span>'
                   if row.get("omitted_reason") else _composition(_rows(row, mode), mini=True))
        buttons.append(f'<button type="button" class="pe-timeline-item{" is-selected" if current else ""}" '
                       f'data-obs-prompt-request="{_escape(_selection(row))}" aria-label="{_escape(title)}" '
                       f'aria-current="{"step" if current else "false"}"><strong>Snapshot {offset}</strong>'
                       f'{summary}</button>')
    note = f"Showing {start + 1}–{start + len(visible)} of {len(lane)} snapshots." if len(visible) < len(lane) else f"{len(lane)} snapshots in this lane."
    navigation = []
    for direction, label, neighbor in (("previous", "Previous snapshot", lane[index - 1] if index > 0 else None),
                                       ("next", "Next snapshot", lane[index + 1] if index + 1 < len(lane) else None)):
        attribute = f'data-obs-prompt-request="{_escape(_selection(neighbor))}"' if neighbor else 'disabled aria-disabled="true"'
        navigation.append(f'<button type="button" class="pe-nav-button" data-obs-prompt-navigation="{direction}" {attribute}>{label}</button>')
    return (f'<section class="pe-timeline-section" aria-label="Prompt evolution"><div class="pe-heading"><h3>Evolution in this lane</h3>'
            f'<span class="pe-muted">{_escape(note)}</span></div><div class="pe-timeline">{"".join(buttons)}</div>'
            f'<div class="pe-navigation"><span class="pe-muted">Snapshot {index + 1} of {len(lane)} loaded in this lane</span>'
            f'<div class="pe-navigation-buttons">{"".join(navigation)}</div></div></section>')


def render_explorer(dataset: dict, selected_request: str | None = None, selected_component: str | None = None, *,
                    mode: str = "physical", changes_only: bool = False) -> str:
    """Render one selected capture. All interactions are selection-only data attributes."""
    records = [row for row in dataset.get("records", []) if isinstance(row, dict)]
    root = '<div id="obs-prompt-explorer">'
    if _mapping(dataset.get("source_health")).get("status") == "stale":
        root += ('<div class="pe-note pe-warning" role="status">Live DevEco session refresh failed. '
                 'The last successful native session is being used; child-session discovery may be stale. '
                 'Retry the obs tool with command "overview" or "dashboard". Available prompt observations remain readable below.</div>')
    if dataset.get("history_reset") is True:
        root += '<p class="pe-note pe-warning">Session history changed or the saved window expired. Showing newest captures.</p>'
    if not records:
        status = dataset.get("status", "empty")
        title = {"disabled": "Prompt capture is off", "unavailable": "Prompt capture is unavailable", "limit": "Capture limit reached"}.get(status, "No prompt snapshots yet")
        message = dataset.get("message") or "Capture a new model request to inspect its composition and changes. Existing uncaptured history cannot be reconstructed here."
        return root + _page_status(dataset) + f'<div class="pe-empty"><h2>{_escape(title)}</h2><p>{_escape(message)}</p><p class="pe-muted">Read-only · Local capture · Separate from usage telemetry</p></div></div>'
    selected = next((row for row in records if _selection(row) == selected_request), None)
    if selected is None:
        selected = next((row for row in reversed(records) if _rows(row, "physical")), records[-1])
    if mode != "effective" or "effective_state" not in selected:
        mode = "physical"
    rows = _rows(selected, mode)
    baseline = _baseline(records, selected)
    old_rows = _rows(baseline, mode) if baseline is not None else []
    changes = _mapping(selected.get("effective_state_changes" if mode == "effective" else "changes"))
    comparable = baseline is not None and selected.get("comparison_status") not in {"unlinked_component", "capture_incomplete", "capture_gap", "page_boundary"}
    old_by_id = {str(row.get("id")): row for row in old_rows}
    current_by_id = {str(row.get("id")): row for row in rows}
    removed = [old_by_id[str(identity)] for identity in changes.get("removed", []) if str(identity) in old_by_id] if comparable else []
    selectable = [*rows, *removed]
    changed_ids = [*changes.get("modified", []), *changes.get("added", []), *changes.get("removed", []), *changes.get("moved", [])] if comparable else []
    if changes_only and comparable:
        selectable = [row for row in selectable if row.get("id") in changed_ids]
    chosen = next((row for row in selectable if selected_component in {str(row.get("id")), component_token(str(row.get("id")))}), None)
    if chosen is None:
        chosen = next((row for identity in changed_ids for row in selectable if row.get("id") == identity), selectable[0] if selectable else None)
    boundary, explanation = _evidence(selected)
    adapter = _ADAPTERS.get(selected.get("adapter"), str(selected.get("adapter") or "Unknown agent"))
    correlation = _mapping(selected.get("correlation"))
    agent = correlation.get("agent_id")
    scope = "Main agent" if agent is None and correlation.get("scope") == "main" else str(agent) if agent else "Agent scope not recorded"
    model = _mapping(selected.get("model"))
    model_name = model.get("id") or model.get("modelID") or "Model not recorded"
    parts = [root, f'<header class="pe-header"><div class="pe-heading"><h2>Request composition</h2><span class="pe-badge">{_escape(boundary)}</span></div>',
             f'<div class="pe-meta"><span>{_escape(adapter)}</span><span>{_escape(scope)}</span><span>{_escape(model_name)}</span><span>{_escape(selected.get("observed_at", "Time not recorded"))}</span></div>',
             f'<p class="pe-muted">{_escape(explanation)}</p>{_snapshot_badges(selected, baseline if comparable else None)}</header>', _page_status(dataset)]
    if selected.get("remote_context"):
        parts.append('<div class="pe-note pe-warning">Earlier context is retained by the provider and is not reconstructed in this payload view. Missing fields here do not mean that context was removed.</div>')
    if mode == "effective":
        parts.append('<div class="pe-note">Active Pi system sections and tool declarations after applying the recorded patches. Conversation messages remain in the captured-structure view.</div>')
    elif selected.get("adapter") == "pi" and "effective_state" in selected:
        parts.append('<p class="pe-muted">Pi can record system and tool patches. Choose Pi active system / tools above to inspect the retained instructions and tool declarations.</p>')
    omitted = selected.get("omitted_reason")
    if omitted:
        if omitted == "record_exceeds_page_budget":
            message = "The observation body exceeds this page's preview budget and was not loaded. Use the local JSON inspector to inspect the retained capture."
        elif omitted == "no_prompt_components_exposed":
            message = "No prompt components were exposed by this observation. It may be a control frame; it does not establish an empty prompt or a context removal."
        else:
            message = "This capture is incomplete. Component removals and an empty prompt cannot be inferred from it."
        parts.append(f'<div class="pe-note pe-warning">{message}</div>')
    gaps = [gap for gap in dataset.get("capture_gaps", []) if isinstance(gap, dict)
            and gap.get("adapter") == selected.get("adapter") and gap.get("session_id") == selected.get("session_id")]
    if gaps:
        parts.append(f'<div class="pe-note pe-warning">{len(gaps)} capture gap or health observation{"s" if len(gaps) != 1 else ""} recorded for this session. Some intermediate evidence may be unavailable.</div>')
    if any(gap.get("reason") == "writer_loss" for gap in gaps):
        parts.append('<div class="pe-note pe-warning">The capture writer reported dropped or failed observations. Which requests were affected is unknown; this warning can apply beyond the selected lane.</div>')
    parts.append(_timeline(records, selected, mode))
    parts.append(_composition(rows) if rows else '<p class="pe-note">No prompt components were exposed at this boundary.</p>')
    if comparable:
        labels = "".join(f'<span class="pe-{kind}">{len(changes.get(kind, []))} {_CHANGE_LABELS[kind].lower()}</span>' for kind in ("added", "modified", "removed", "moved", "unchanged"))
        parts.append(f'<div class="pe-change-summary" aria-label="Changes from verified baseline">{labels}</div>')
        parts.append('<p class="pe-muted">Moved counts position shifts separately; it can overlap changed or unchanged content counts.</p>')
    else:
        reason = ("This is an unlinked observation; no request-to-request comparison is inferred." if selected.get("comparison_status") == "unlinked_component"
                  else "The baseline is outside this loaded page; no comparison is inferred across the page boundary." if selected.get("comparison_status") == "page_boundary"
                  else "No comparable baseline is available for this snapshot.")
        parts.append(f'<p class="pe-muted">{reason}</p>')
        if changes_only:
            parts.append('<p class="pe-muted">A changed-components filter needs a comparable baseline. Available captured components remain visible.</p>')
    if not selectable:
        if changes_only and comparable:
            parts.append('<p class="pe-note">No changed components in this snapshot. Captured content and component positions match the baseline.</p>')
        return "".join(parts) + '</div>'
    selected_index = next((index for index, row in enumerate(selectable) if row is chosen), 0)
    start = max(0, min(selected_index - MAX_VISIBLE_COMPONENTS // 2, len(selectable) - MAX_VISIBLE_COMPONENTS))
    buttons = []
    for row in selectable[start:start + MAX_VISIBLE_COMPONENTS]:
        identity = str(row.get("id"))
        statuses = [kind for kind in ("added", "modified", "removed", "moved") if comparable and identity in changes.get(kind, [])]
        if comparable and identity in changes.get("unchanged", []):
            statuses.append("unchanged")
        badges = "".join(f'<span class="pe-change pe-{kind}">{"Content same" if kind == "unchanged" else _CHANGE_LABELS[kind]}</span>' for kind in statuses)
        position = "Removed" if identity not in current_by_id else str(int(row.get("position", 0)) + 1)
        buttons.append(f'<button type="button" class="pe-component{" is-selected" if row is chosen else ""}" '
                       f'data-obs-prompt-component="{component_token(identity)}" aria-pressed="{str(row is chosen).lower()}">'
                       f'<span class="pe-component-name"><span class="pe-position">{_escape(position)}</span>{_escape(_title(row))}</span>'
                       f'<span class="pe-component-meta"><span>{_escape(format_bytes(_bytes(row.get("utf8_bytes"))))}</span>{badges}</span></button>')
    list_note = f" · showing {start + 1}–{start + len(buttons)}" if len(selectable) > len(buttons) else ""
    list_label = f"{len(selectable)} changed · {len(rows)} captured" if changes_only and comparable else f"{len(rows)} captured"
    parts.append(f'<div class="pe-layout"><section aria-label="Ordered components"><div class="pe-components-heading"><h3>{"Changed components" if changes_only and comparable else "Components"}</h3>'
                 f'<span class="pe-muted">{_escape(list_label)}{_escape(list_note)}</span></div><div class="pe-components">{"".join(buttons)}</div></section>')
    identity = str(chosen.get("id"))
    old, current = old_by_id.get(identity), current_by_id.get(identity)
    kind_label = _KINDS.get(chosen.get("kind"), _KINDS["opaque"])[0]
    parts.append(f'<section class="pe-detail" aria-label="Selected component"><div class="pe-detail-heading"><h3 class="pe-detail-title">{_escape(_title(chosen))}</h3>'
                 f'<p class="pe-muted">{_escape(kind_label)} · {_escape(format_bytes(_bytes(chosen.get("utf8_bytes"))))}</p></div>')
    parts.append(_component_badge(chosen, selected, mode))
    source = chosen.get("source")
    source_text = json.dumps(source, ensure_ascii=False) if isinstance(source, (dict, list)) else str(source) if source is not None else "Source not attributed"
    source_preview = source_text[:1500] + ("… (source preview limited)" if len(source_text) > 1500 else "")
    parts.append(f'<dl><dt>Identity</dt><dd>{_escape(identity)}</dd><dt>Source</dt><dd>{_escape(source_preview)}</dd>'
                 '<dt>Tokens</dt><dd>Not measured per component</dd></dl>')
    changed = comparable and identity in [*changes.get("added", []), *changes.get("modified", []), *changes.get("removed", [])]
    if changed:
        parts.append('<details open><summary>Changes from baseline</summary>' + _diff(old.get("content", _MISSING) if old else _MISSING, current.get("content", _MISSING) if current else _MISSING) + '</details>')
        parts.append('<details><summary>Before and after content</summary><div class="pe-content-pair"><section><h4>Before</h4>'
                     + _pre(old.get("content", _MISSING) if old else _MISSING) + '</section><section><h4>After</h4>'
                     + _pre(current.get("content", _MISSING) if current else _MISSING) + '</section></div></details>')
    else:
        if comparable:
            parts.append('<p class="pe-muted">Content unchanged from the verified baseline.</p>')
            if identity in changes.get("moved", []) and old is not None and current is not None:
                old_position, new_position = _bytes(old.get("position")), _bytes(current.get("position"))
                if old_position is not None and new_position is not None:
                    parts.append(f'<p class="pe-muted">Position shifted from {old_position + 1} to {new_position + 1}; the captured content is the same.</p>')
        parts.append(_pre(chosen.get("content", _MISSING), unavailable="Content was not retained in this capture"))
    parts.append('</section></div></div>')
    return "".join(parts)
