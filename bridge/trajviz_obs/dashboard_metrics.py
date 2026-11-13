"""Evidence-aware formatting for the native Deep Dive metric cards."""

from __future__ import annotations

from .native_quality import _clone_native_function

METRIC_TRANSLATIONS = {
    "Partial coverage: {observed}/{total} recorded": "记录不完整：已记录 {observed}/{total} 项",
    "Median uses assistant steps with both tokens and duration recorded.": "中位数仅使用同时记录了 Token 和耗时的助手步骤。",
    "Includes cache reads. Divided by summed step duration.": "包含缓存读取，分母为各步骤耗时之和。",
    "Requires complete token and timing records.": "需要完整的 Token 和耗时记录。",
    "Includes cache reads. Divided by summed assistant step duration.": "包含缓存读取，分母为各助手步骤耗时之和。",
    "Requires complete assistant token and timing records.": "需要完整的助手步骤 Token 和耗时记录。",
    "({count} total, {rate}% success)": "（共 {count} 次，成功率 {rate}%）",
    "({count} total, success rate unavailable)": "（共 {count} 次，成功率不可用）",
    "({count} total, success rate unavailable, {observed}/{count} outcomes recorded)":
        "（共 {count} 次，已记录 {observed}/{count} 次结果，成功率不可用）",
}

_TOKEN_LABELS = {
    "Total tokens": "total", "Input": "input", "Output": "output", "Reasoning": "reasoning",
    "Cache read": "cache_read", "Cache write": "cache_write",
}
_DURATION_LABELS = {"Avg duration", "Med / P95", "Max duration"}


def format_deep_dive_metrics(metrics, wall_clock, rows):
    """Keep native card groups while displaying the evidence behind their values."""
    from agent_trajviz.insight import formatting

    def metric_text(value):
        # Gradio strips data attributes from Markdown. This class lets the
        # dashboard restore localization for authored labels and hints only.
        return formatting.ui_text(value).replace("<span ", '<span class="obs-metric-text" ', 1)

    native_chip = _clone_native_function(formatting._metric_chip, {"ui_text": metric_text})
    tokens = metrics["tokens"]
    quality = metrics.get("data_quality", {})
    fields = quality.get("fields", {})
    native_sample = quality.get("source") == "native"
    assistant = [row for row in rows if row["role"] == "assistant"]

    def partial(observed, total):
        return f"Partial coverage: {observed}/{total} recorded" if 0 < observed < total else ""

    def coverage(field):
        evidence = fields.get(field)
        return partial(evidence["observed"], evidence["total"]) if evidence else ""

    def chip(label, value, *, wide=False, verdict=None, hint="", localize_label=True):
        if not localize_label:
            return native_chip(label, value, wide=wide, verdict=verdict, hint=hint, localize_label=False)
        if label in _TOKEN_LABELS:
            field = _TOKEN_LABELS[label]
            value = formatting._number(tokens[field])
            hint = coverage(f"tokens.{field}") or hint
        elif label == "Fresh input":
            value = formatting._number(metrics["non_cache_tokens"])
            if value != "N/A" and (tokens["total"] or 0) > 0 and metrics["non_cache_ratio"] is not None:
                value += f" ({metrics['non_cache_ratio']}%)"
            hint = coverage("tokens.input")
        elif label in _DURATION_LABELS:
            hint = coverage("duration")
        elif label == "Total processed tok/sec":
            if value != "N/A":
                hint = ("Includes cache reads. Divided by summed assistant step duration." if native_sample
                        else "Includes cache reads. Divided by summed step duration.")
            elif native_sample and any(row["tokens_total"] is None or row["duration"] is None for row in assistant):
                hint = "Requires complete assistant token and timing records."
            elif not native_sample and any(fields.get(field, {}).get("status") in {"partial", "unknown", "unavailable"}
                                           for field in ("tokens.total", "duration")):
                hint = "Requires complete token and timing records."
        elif label == "Median processed tok/sec" and value != "N/A":
            observed = sum(row["tokens_per_sec"] is not None for row in assistant)
            hint = partial(observed, len(assistant)) or "Median uses assistant steps with both tokens and duration recorded."
        elif label == "Tok/tool call" and not metrics["tool_call_count"]:
            value = "N/A"
        return native_chip(label, value, wide=wide, verdict=verdict, hint=hint)

    native = _clone_native_function(formatting.format_performance_md, {"_metric_chip": chip})
    markdown = native(metrics, wall_clock)
    if metrics["tool_call_count"] and metrics["tool_success_rate"] is None:
        outcomes = fields.get("tool_outcomes", {})
        count = metrics["tool_call_count"]
        observed = outcomes.get("observed", 0)
        detail = f", {observed}/{count} outcomes recorded" if observed else ""
        markdown = markdown.replace(f"**Tool calls** ({count} total, no success rate)",
                                    f"**Tool calls** ({count} total, success rate unavailable{detail})", 1)
    return markdown
