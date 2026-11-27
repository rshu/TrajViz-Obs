"""DevEco-only presentation of captured prompt snapshots and comparison events.

The pinned renderer still owns chart construction. This wrapper labels evidence
coverage and independent agent contexts without changing trace values or native
TrajViz module globals.
"""
from __future__ import annotations

import html
from collections import Counter

_COLORS = ("#2563eb", "#b45309", "#7c3aed", "#0d9488", "#be185d", "#475569", "#9333ea", "#0369a1")
_LANES = ("Instructions", "Tools", "Context edits / compaction")


def _text(en: str, zh: str, language: str) -> str:
    return zh if language.lower().startswith("zh") else en


def _context(row: dict) -> tuple:
    value = row.get("context_id")
    if isinstance(value, str) and value:
        return ("context", value)
    return tuple(str(row.get(key) or "") for key in ("session_id", "agent_id", "branch_id"))


def _actor_priority(row: dict) -> tuple:
    quality = row.get("evidence_quality") or {}
    mapping = row.get("native_mapping") or quality.get("native_mapping") or {}
    return (mapping.get("status") == "verified", row.get("actor_kind") in {"main", "subagent"},
            bool(row.get("actor_name")), bool(row.get("agent_id")))


def _contexts(rows: list[dict], language: str) -> tuple[dict, list[dict]]:
    owners, representatives, used, switches = {}, {}, Counter(), []
    previous = None
    for row in rows:
        key = _context(row)
        if key not in representatives or _actor_priority(row) > _actor_priority(representatives[key]):
            # A title/auxiliary request may precede the row that identifies this context.
            representatives[key] = row
        if previous is not None and key != _context(previous):
            switches.append({"order": row["order"], "from": previous["id"], "to": row["id"]})
        previous = row
    for key, row in representatives.items():
        name = row.get("actor_name") or row.get("agent_id")
        if row.get("actor_kind") == "main":
            base = _text("Main agent", "主代理", language)
        elif isinstance(name, str) and name.strip():
            base = name.strip()[:40]
        else:
            base = _text("Unidentified subagent", "未识别子代理", language)
        used[base] += 1
        label = base if used[base] == 1 else f"{base} · {used[base]}"
        owners[key] = {"label": label, "color": _COLORS[len(owners) % len(_COLORS)]}
    return owners, switches


def _event_source(event: dict, language: str) -> str:
    evidence = event.get("evidence")
    if evidence == "observed_snapshot":
        return _text("Observed in captured snapshot", "在捕获快照中观察到", language)
    if evidence == "snapshot_comparison":
        return _text("Derived from request comparison", "由请求对比得出", language)
    if evidence == "observed_transcript":
        return _text("Observed in native transcript", "在原生记录中观察到", language)
    if event.get("native_event") is True or evidence in {"native", "native_event", "recorded_event"}:
        return _text("Native recorded event", "原生记录事件", language)
    return _text("Event source unspecified", "事件来源未标明", language)


def _event_label(event: dict, language: str, core) -> str:
    kind = event.get("kind")
    if kind == "system_snapshot":
        label = _text("System instructions present", "包含系统指令", language)
    elif kind == "tool_snapshot":
        label = _text("Tool schemas present", "包含工具 Schema", language)
    elif kind == "context_edit" and event.get("evidence") == "snapshot_comparison":
        label = _text("Prompt message history changed", "提示词消息历史发生变化", language)
    else:
        label = core.translate(core.event_label(event), language)
    count = event.get("count")
    if type(count) is int and count > 1:
        label += f" ×{count}"
    target = event.get("tool_name") or event.get("target_id") or event.get("name") or event.get("section")
    if target:
        label += ": " + html.escape(str(target)[:160])
    label += "<br>" + _event_source(event, language)
    steps = list(dict.fromkeys(ref["step_index"] for ref in event.get("source_refs", [])
                              if isinstance(ref, dict) and type(ref.get("step_index")) is int))
    if steps:
        label += "<br>" + _text("Source native steps: ", "来源原生步骤：", language)
        label += ", ".join(f"#{step}" for step in steps[:4])
    return label


def _coverage(row: dict) -> dict:
    value = row.get("prompt_event_coverage")
    return value if isinstance(value, dict) else {}


def _empty_lane(rows: list[dict], lane: str, language: str) -> str:
    key = {"Instructions": "instructions", "Tools": "tools", "Context edits / compaction": "context_edits"}[lane]
    applicable = [row for row in rows if _coverage(row).get("applicable") is not False]
    states = [_coverage(row).get(key, "unavailable") for row in applicable]
    if lane == "Context edits / compaction":
        if states and all(value == "native" for value in states):
            return _text("No recorded context-edit / compaction events", "未记录上下文编辑或压缩事件", language)
        return _text("Compaction events unavailable", "压缩事件数据不可用", language)
    if states and "compared" in states and all(value in {"compared", "snapshot_only"} for value in states):
        return _text("No changes observed in compared requests", "已对比请求中未观察到变化", language)
    return _text("Event timeline unavailable", "事件时间线不可用", language)


def evolution_chart(data: dict, selected_id: str, language: str = "en"):
    """Render verified DevEco coverage; pass every other source through unchanged."""
    from agent_trajviz.insight.ui import prompt_evolution as core

    rows = data.get("evolution") or []
    capture_backed = (data.get("format") == "deveco"
                      and any(isinstance(row.get("prompt_event_coverage"), dict) for row in rows))
    # The pinned core accepts only zh-CN; keep the wrapper's Chinese aliases consistent.
    if capture_backed and language.lower().startswith("zh"):
        language = "zh-CN"
    figure = core.evolution_chart(data, selected_id, language)
    if figure is None or not capture_backed:
        return figure
    owners, switches = _contexts(rows, language)
    by_id = {row["id"]: row for row in rows}
    sequence_name = core.translate("Request sequence", language)
    lane_names = {core.translate(lane, language): lane for lane in _LANES}
    for trace in figure.data:
        if trace.name == sequence_name:
            trace.marker.color = [owners[_context(by_id[item[0]])]["color"] for item in trace.customdata]
            trace.customdata = [[item[0], ("<br>".join(_event_label(event, language, core)
                                 for event in by_id[item[0]].get("prompt_events", [])
                                 if core._lane(event) == "Request sequence") or item[1])
                                 + "<br>" + _text("Agent context: ", "代理上下文：", language)
                                 + html.escape(owners[_context(by_id[item[0]])]["label"])
                                 + ("<br>" + _text("Native assistant step: ", "原生助手步骤：", language)
                                    + f"#{by_id[item[0]]['step_index']}"
                                    if type(by_id[item[0]].get("step_index")) is int else "")]
                                for item in trace.customdata]
        elif trace.name in lane_names:
            lane = lane_names[trace.name]
            trace.customdata = [[item[0], "<br>".join(
                _event_label(event, language, core) for event in by_id[item[0]].get("prompt_events", [])
                if core._lane(event) == lane)] for item in trace.customdata]
    for annotation in figure.layout.annotations or ():
        if annotation.text == core.translate("Not recorded", language) and annotation.y in lane_names:
            annotation.text = _empty_lane(rows, lane_names[annotation.y], language)
        if (annotation.xref == "paper" and annotation.yref == "paper"
                and annotation.text and core.translate("Each bar belongs to one agent context.", language) in annotation.text):
            # Retain chronological/source-order wording, then explain independent contexts.
            first = annotation.text.split("<br>")[0]
            note = _text("Colored request markers identify agent contexts; dotted separators mark switches.",
                         "请求标记颜色区分代理上下文，虚线表示上下文切换。", language)
            legend = " · ".join(f"<span style='color:{owner['color']}'>●</span> "
                                + html.escape(owner["label"]) for owner in list(owners.values())[:6])
            if len(owners) > 6:
                legend += _text(f" · +{len(owners) - 6} more contexts (hover markers)",
                                f" · 另有{len(owners) - 6}个上下文（悬停查看）", language)
            annotation.text = first + "<br>" + note + "<br>" + legend
            top = figure.layout.margin.t or 85
            inner = max(1, (figure.layout.height or 520) - top - (figure.layout.margin.b or 0))
            annotation.y = 1 + (top - 32) / inner
            annotation.yanchor = "middle"
            annotation.font.size = 9
    measured = any(row.get("observed_prompt_tokens") is not None for row in rows)
    composed = any(isinstance(row.get("composition"), dict) for row in rows)
    panel_count = 1 + int(measured) + int(composed)
    events_axis = "xaxis" + (str(panel_count) if panel_count > 1 else "")
    axis = figure.layout[events_axis]
    axis.title.text = _text("Captured request order", "捕获请求顺序", language)
    if axis.tickvals is not None:
        by_order = {row["order"]: row for row in rows}
        axis.ticktext = ["#" + str(order) + "<br>" + html.escape(owners[_context(by_order[order])]["label"])
                         for order in axis.tickvals]
    if len(owners) > 1:
        composition_row = 1 + int(measured) if composed else 1
        xref = "x" + (str(composition_row) if composition_row > 1 else "")
        yref = "y" + (str(composition_row) if composition_row > 1 else "") + " domain"
        for switch in switches[:128]:
            figure.add_shape(type="line", x0=switch["order"] - .5, x1=switch["order"] - .5,
                             y0=0, y1=1, xref=xref, yref=yref,
                             line={"color": "#b7c6d1", "width": 1, "dash": "dot"},
                             layer="below", name="deveco-context-switch")
    meta = dict(figure.layout.meta or {})
    meta["deveco_context_switches"] = switches
    meta["deveco_event_sources"] = "Captured snapshots, explicitly labeled comparisons, and native events when provided."
    figure.layout.meta = meta
    return figure
