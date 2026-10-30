"""Keep label sidecar measurements honest without changing their numeric schema."""

from __future__ import annotations

import functools
import inspect
import math
import sys
import types


def annotate_label(step: dict, record: dict) -> dict:
    from .native_quality import _observed

    telemetry = step.get("_otel")
    if isinstance(telemetry, dict):
        coverage = {field: telemetry.get("fields", {}).get(field, "unknown")
                    for field in ("tokens.total", "duration")}
    else:
        coverage = {field: "observed" if _observed(step, field) else "unknown"
                    for field in ("tokens.total", "duration")}
    return {**record, "tokens_observed": coverage["tokens.total"] == "observed",
            "duration_observed": coverage["duration"] == "observed", "source_coverage": coverage}


def label_coverage(document: dict) -> dict | None:
    """Coverage of classified assistant labels; legacy unannotated sidecars stay unchanged."""
    steps = [step for step in document.get("steps", []) if isinstance(step, dict)
             and step.get("role", "assistant") == "assistant" and step.get("phase") != "unknown"
             and step.get("action") != "unknown"]
    if not any("tokens_observed" in step or "duration_observed" in step for step in steps):
        return None
    fields = {}
    for field, marker in (("tokens.total", "tokens_observed"), ("duration", "duration_observed")):
        observed = sum(step.get(marker) is True for step in steps)
        fields[field] = {"status": "observed" if observed == len(steps) else "partial" if observed else "unknown",
                         "observed": observed, "partial": 0, "total": len(steps),
                         "reasons": {"not_recorded": len(steps) - observed} if observed < len(steps) else {}}
    return fields


def install_chart() -> None:
    """Postprocess the native timeline figure only; callers have loaded chart extras."""
    from agent_trajviz.insight.charts import label_charts

    original = label_charts.build_label_timeline_chart
    if getattr(original, "_trajviz_obs_label_quality", False):
        _install_payload()
        return
    if set(inspect.signature(original).parameters) != {"steps", "dark"}:
        raise RuntimeError("TrajViz's label timeline changed; update the plugin.")

    @functools.wraps(original)
    def timeline(steps, dark=False):
        normalized = []
        for step in steps:
            value = step.get("tokens_total", 0)
            try:
                valid = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            except OverflowError:
                valid = False
            row = step if valid else {**step, "tokens_total": 0, "tokens_observed": False}
            if row.get("duration_observed") is False:
                row = {**row, "duration_s": None}
            normalized.append(row)
        figure = original(normalized, dark=dark)
        for trace in figure.data:
            hover = trace.hovertext
            if hover is None or len(hover) != len(normalized):
                continue
            hover = list(hover)
            text = list(trace.text) if trace.text is not None and len(trace.text) == len(normalized) else None
            for index, step in enumerate(normalized):
                if not isinstance(hover[index], str):
                    continue
                if step.get("tokens_observed") is False:
                    value = step.get("tokens_total", 0)
                    hover[index] = hover[index].replace(f"Tokens: {value:,}", "Tokens: unknown")
                if step.get("duration_observed") is False:
                    value = step.get("duration_s") or 0
                    hover[index] = hover[index].replace(f"Duration: {value:.1f}s", "Duration: unknown")
                    if text is not None and isinstance(text[index], str):
                        text[index] = text[index].replace(f"({value:.1f}s)", "(duration unknown)")
            trace.hovertext = hover
            if text is not None:
                trace.text = text
        return figure

    timeline._trajviz_obs_label_quality = True
    label_charts.build_label_timeline_chart = timeline
    facade = sys.modules.get("agent_trajviz.insight.charts")
    if facade is not None:
        facade.build_label_timeline_chart = timeline
    presenter = sys.modules.get("agent_trajviz.insight.presenters.label_ui")
    if presenter is not None:
        presenter.build_label_timeline_chart = timeline
    _install_payload()


def _install_payload() -> None:
    from agent_trajviz.insight.presenters import label_ui

    original = label_ui.build_label_ui_payload
    if getattr(original, "_trajviz_obs_label_quality", False):
        return

    @functools.wraps(original)
    def payload(file_path, dark=False):
        captured = {}

        def load(path):
            data = label_ui.load_labeled_json(path)
            captured["data"] = data
            return data

        build = types.FunctionType(original.__code__, {**original.__globals__, "load_labeled_json": load},
                                   original.__name__, original.__defaults__, original.__closure__)
        build.__kwdefaults__ = original.__kwdefaults__
        result = build(file_path, dark=dark)
        coverage = label_coverage(captured.get("data", {}))
        if coverage and coverage["duration"]["status"] != "observed":
            note = ("Partial timing: totals include observed durations only." if coverage["duration"]["observed"] else
                    "Timing unavailable: no measured durations.")
            for key in ("phase_duration_fig", "action_duration_fig"):
                figure = result[key]
                figure.add_annotation(text=note, xref="paper", yref="paper", x=0, y=-0.23,
                                      xanchor="left", showarrow=False, font={"size": 11})
                figure.update_layout(margin={"b": max(80, figure.layout.margin.b or 0)})
            result["status_html"] += f"<p>{note}</p>"
        return result

    payload._trajviz_obs_label_quality = True
    label_ui.build_label_ui_payload = payload
    overview = sys.modules.get("agent_trajviz.insight.ui.overview_tab")
    if overview is not None:
        overview.build_label_ui_payload = payload
