"""Display native evidence gaps without changing TrajViz's canonical records.

TrajViz 0.3.1 publishes nullable OTLP views, while native parsers retain numeric
placeholders and per-step missing-field labels. These projections use that
native evidence for plugin JSON, UI and model briefs; upstream APIs are untouched.
"""

from __future__ import annotations

import copy
import functools
import json
import math
import types
import weakref
from collections import defaultdict
from dataclasses import replace
from functools import cached_property

TOKEN_LABELS = {"total": "Total Tokens", "input": "Input Tokens", "output": "Output Tokens",
                "reasoning": "Reasoning Tokens", "cache_read": "Cache Read", "cache_write": "Cache Write"}
_PROJECTED = weakref.WeakKeyDictionary()


def _number(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def _observed(step, field):
    if field == "duration":
        return _number(step.get("duration")) and step["duration"] >= 0
    key = field.removeprefix("tokens.")
    value = (step.get("tokens") or {}).get(key)
    return (TOKEN_LABELS[key] not in step.get("_metrics_unavailable_fields", []) and _number(value)
            and value >= 0 and (isinstance(value, int) or value.is_integer()))


def _usage_steps(steps):
    return [step for step in steps if step.get("role") == "assistant" or (step.get("role") == "compaction" and (
        step.get("_trajviz_obs_opencode_v2") or any(value and _number(value) for value in step["tokens"].values())))]


def _outcome(call):
    from agent_trajviz.insight.telemetry_quality import tool_field_observed

    return tool_field_observed(call, "tool_outcomes") and call.get("status") not in {
        None, "", "?", "pending", "running", "streaming"}


def _coverage(steps):
    model = _usage_steps(steps)
    fields = {}
    for field in [*(f"tokens.{key}" for key in TOKEN_LABELS), "duration"]:
        population = [step for step in model if step.get("role") == "assistant"] if field == "duration" else model
        observed = sum(_observed(step, field) for step in population)
        fields[field] = {"status": "observed" if population and observed == len(population) else "partial" if observed else "unknown",
                         "observed": observed, "partial": 0, "total": len(population),
                         "reasons": {"not_recorded_or_invalid": len(population) - observed} if observed < len(population) else {}}
    calls = [call for step in steps for call in step.get("tool_calls", [])]
    observed = sum(_outcome(call) for call in calls)
    fields["tool_outcomes"] = {"status": "observed" if observed == len(calls) else "partial" if observed else "unknown",
                               "observed": observed, "partial": 0, "total": len(calls),
                               "reasons": {"outcome_not_recorded": len(calls) - observed} if observed < len(calls) else {}}
    return {"source": "native", "basis": "source_records", "capture_complete": None, "fields": fields}


def _complete(quality, *fields):
    return all(quality["fields"][field]["status"] == "observed" for field in fields)


def _project_metrics(metrics, quality):
    result = copy.deepcopy(metrics)
    result.update(data_quality=quality, metric_coverage={field: row["status"] for field, row in quality["fields"].items()})
    for key in TOKEN_LABELS:
        if not quality["fields"][f"tokens.{key}"]["observed"]:
            result["tokens"][key] = None
    direct = {"input_tokens": "input", "output_tokens": "output", "cache_read_tokens": "cache_read",
              "non_cache_tokens": "input"}
    for key, token in direct.items():
        if not quality["fields"][f"tokens.{token}"]["observed"]:
            result[key] = None
    dependencies = {
        "avg_cache_ratio": ("tokens.total", "tokens.cache_read"),
        "non_cache_ratio": ("tokens.total", "tokens.input"), "output_input_ratio": ("tokens.input", "tokens.output"),
        "output_tokens_per_sec": ("tokens.output", "duration"),
        "tokens_per_second": ("tokens.total", "duration"), "median_tokens_per_second": ("tokens.total", "duration"),
        "avg_tokens_per_step": ("tokens.total",), "avg_tokens_per_assistant_step": ("tokens.total",),
        "median_step_tokens": ("tokens.total",), "p95_step_tokens": ("tokens.total",),
        "tokens_per_tool": ("tokens.total",),
    }
    for key, fields in dependencies.items():
        if not _complete(quality, *fields):
            result[key] = None
    if not quality["fields"]["duration"]["observed"]:
        for key in ("total_duration", "avg_duration", "median_duration", "p95_duration", "max_duration"):
            result[key] = None
    if not _complete(quality, "tool_outcomes"):
        result["tool_observed_failures"] = metrics.get("tool_fail", 0)
        result["tool_observed_successes"] = metrics.get("tool_success", 0)
        for key in ("tool_fail", "tool_success", "tool_success_rate", "tool_system_failure_rate", "command_failures",
                    "command_success_rate"):
            result[key] = None
    result["reasoning_tokens_reported"] = bool(quality["fields"]["tokens.reasoning"]["observed"])
    return result


def _project_row(row, step):
    result = copy.deepcopy(row)
    fields = {"tokens_total": "total", "tokens_input": "input", "tokens_output": "output", "cache_read": "cache_read",
              "non_cache_tokens": "input", "tok_total": "total", "non_cache_tok": "input"}
    for key, token in fields.items():
        if key in result and not _observed(step, f"tokens.{token}"):
            result[key] = None
    for key, tokens in {"cache_ratio": ("total", "cache_read"), "output_input_ratio": ("input", "output"),
                        "out_in_ratio": ("input", "output"), "tokens_per_sec": ("total",),
                        "tok_per_s": ("total",), "non_cache_per_sec": ("input",)}.items():
        if key in result and not all(_observed(step, f"tokens.{token}") for token in tokens):
            result[key] = None
    if any(not _outcome(call) for call in step.get("tool_calls", [])) and "errors" in result:
        result["observed_errors"], result["errors"] = result["errors"], None
    from agent_trajviz.insight.metrics import tool_call_duration_ms

    calls = step.get("tool_calls", [])
    observed_clocks = sum(tool_call_duration_ms(call) is not None for call in calls)
    if calls and not observed_clocks:
        for key in ("tool_time_sum", "tool_time_ms"):
            if key in result:
                result[key] = None
    if observed_clocks < len(calls) and "tool_time_share" in result:
        result["tool_time_share"] = None
    return result


class _Projection:
    def __init__(self, run):
        self._original = run._analysis
        self.steps = run.steps

    def __getattr__(self, name):
        return getattr(self._original, name)

    @cached_property
    def data_quality(self):
        return _coverage(self.steps)

    @cached_property
    def metrics(self):
        return _project_metrics(self._original.metrics, self.data_quality)

    @cached_property
    def verdicts(self):
        rows = copy.deepcopy(self._original.verdicts)
        for row in rows:
            dependencies = {"Cache Efficiency": ("tokens.total", "tokens.cache_read"),
                            "Throughput": ("tokens.output", "duration"),
                            "Tool Success": ("tool_outcomes",), "Errors": ("tool_outcomes",)}[row["metric"]]
            missing = [key for key in dependencies if not _complete(self.data_quality, key)]
            if missing:
                row.update(status="unavailable", label="N/A", detail="Insufficient native source evidence: " + ", ".join(
                    f"{key} ({self.data_quality['fields'][key]['observed']}/{self.data_quality['fields'][key]['total']})"
                    for key in missing))
        return rows

    @cached_property
    def agent_summaries(self):
        from agent_trajviz.insight.metrics import effective_agent

        grouped = defaultdict(list)
        for step in self.steps:
            # Agent summaries aggregate assistant work by effective identity;
            # timeline identities also split sessions and plan/build modes.
            if step.get("role") == "assistant":
                grouped[effective_agent(step)].append(step)
        rows = copy.deepcopy(self._original.agent_summaries)
        for row in rows:
            quality = _coverage(grouped[row["agent_id"]])
            row["data_quality"] = quality
            for key, token in {"total_tokens": "total", "input_tokens": "input", "output_tokens": "output",
                "reasoning_tokens": "reasoning", "cache_read_tokens": "cache_read", "cache_write_tokens": "cache_write"}.items():
                if not quality["fields"][f"tokens.{token}"]["observed"]:
                    row[key] = None
            if not quality["fields"]["duration"]["observed"]:
                row["total_duration_s"] = None
            if not _complete(quality, "tokens.total", "tokens.cache_read"):
                row["cache_efficiency_pct"] = None
            if not _complete(quality, "tokens.output", "duration"):
                row["output_tokens_per_sec"] = None
            if not _complete(quality, "tool_outcomes"):
                row["observed_errors"], row["error_count"] = row["error_count"], None
                row["unknown_outcomes"] = quality["fields"]["tool_outcomes"]["total"] - quality["fields"]["tool_outcomes"]["observed"]
        return rows

    @cached_property
    def message_rows(self):
        steps = {step["index"]: step for step in self.steps}
        return [_project_row(row, steps[row["index"]]) for row in self._original.message_rows]

    @cached_property
    def step_analytics(self):
        steps = {step["index"]: step for step in self.steps}
        return [_project_row(row, steps[row["index"]]) for row in self._original.step_analytics]

    @cached_property
    def hotspots(self):
        steps = {step["index"]: step for step in self.steps}
        result = {key: [_project_row(row, steps[row["index"]]) for row in rows]
                  for key, rows in self._original.hotspots.items()}
        result["heaviest"] = [row for row in result["heaviest"] if row["tokens_total"] is not None]
        result["lowest_cache_read"] = [row for row in result["lowest_cache_read"] if row["cache_ratio"] is not None]
        return result


def project_analysis(run):
    """An analysis proxy for plugin surfaces; OTLP keeps its authoritative coverage."""
    if run.format == "otlp" or isinstance(run.raw.get("_otel"), dict):
        return run._analysis
    if run not in _PROJECTED:
        _PROJECTED[run] = _Projection(run)
    return _PROJECTED[run]


def project_view(run, view):
    """A schema-2 native display view with measured zeros and unknown gaps distinct."""
    projection = project_analysis(run)
    if projection is run._analysis:
        return view
    direct = {"metrics": "metrics", "health": "verdicts", "data_quality": "data_quality", "agents": "agent_summaries",
              "steps": "message_rows", "step_analytics": "step_analytics", "hotspots": "hotspots"}
    data = copy.deepcopy(getattr(projection, direct[view.id]) if view.id in direct else view.data)
    steps = {step["index"]: step for step in run.steps}
    if view.id == "summary":
        data.update(tokens=copy.deepcopy(projection.metrics["tokens"]), data_quality=copy.deepcopy(projection.data_quality),
                    tool_success_rate=projection.metrics.get("tool_success_rate"),
                    output_tokens_per_sec=projection.metrics.get("output_tokens_per_sec"),
                    health={row["metric"]: row["status"] for row in projection.verdicts})
    elif view.id == "token_usage":
        for row in data["steps"]:
            step = steps[row["step"]]
            for key, token in {"fresh_input": "input", "cache_read": "cache_read", "cache_write": "cache_write",
                "output": "output", "reasoning": "reasoning", "total": "total", "usable_total": "total"}.items():
                if not _observed(step, f"tokens.{token}"):
                    row[key] = None
    elif view.id == "agent_tokens":
        from agent_trajviz.insight.chart_data import agent_tokens

        data = agent_tokens(projection.agent_summaries)
    elif view.id == "workflow":
        # Published schema 2 still requires an integer workflow token count.
        # Explicit observation metadata lets the UI suppress that placeholder.
        for row in data["steps"]:
            row["tokens_observed"] = _observed(steps[row["step"]], "tokens.total")
    elif view.id == "agent_swimlane":
        for lane in data["lanes"]:
            for segment in lane["segments"]:
                if any(not _observed(steps[index], "tokens.total") for index in range(
                        segment["start"], segment["end"] + 1) if index in steps):
                    segment["tokens"] = None
    elif view.id == "step_durations" and not projection.data_quality["fields"]["duration"]["observed"]:
        data["average_s"] = None
    dependencies = {"token_usage": tuple(f"tokens.{key}" for key in TOKEN_LABELS),
                    "agent_tokens": tuple(f"tokens.{key}" for key in TOKEN_LABELS),
                    "agent_swimlane": ("tokens.total",),
                    "step_durations": ("duration",), "bottlenecks": ("duration",),
                    "tool_outcomes": ("tool_outcomes",)}.get(view.id)
    availability = view.availability
    if dependencies:
        fields = {key: projection.data_quality["fields"][key] for key in dependencies}
        states = {row["status"] for row in fields.values()}
        status = "observed" if states == {"observed"} else "partial" if any(
            row["observed"] for row in fields.values()) else "unavailable"
        availability = {"status": status, "basis": "source_records", "fields": fields}
    return replace(view, data=data, availability=availability)


def project_brief(run):
    """The existing brief with native evidence-aware formatters scoped to this call."""
    from agent_trajviz import llm
    from agent_trajviz.insight import assistant

    projection = project_analysis(run)
    if projection is run._analysis and run.format != "otlp":
        return llm.brief(run)

    def clone(function, replacements):
        copied = types.FunctionType(function.__code__, {**function.__globals__, **replacements},
                                    function.__name__, function.__defaults__, function.__closure__)
        copied.__kwdefaults__ = function.__kwdefaults__
        return functools.update_wrapper(copied, function)

    if run.format == "otlp":
        def captured_timeline_row(step):
            text = assistant._timeline_row(step)
            if step.get("_otel", {}).get("fields", {}).get("tokens.total") != "observed":
                text = text.replace(f" {(step.get('tokens') or {}).get('total', 0)}tok ", " ?tok ", 1)
            return text

        replacements = {"_timeline_lines": clone(assistant._timeline_lines, {"_timeline_row": captured_timeline_row})}
        if run.view("file_interactions").availability.get("status") == "unavailable":
            replacements["_file_lines_from_interactions"] = lambda _rows: ["Unavailable: file activity was not captured."]
        if projection.data_quality["fields"]["tool_outcomes"]["status"] != "observed":
            original_errors = assistant._error_step_lines
            replacements["_error_step_lines"] = lambda rows: [*original_errors(rows),
                "Tool outcomes are incomplete; missing results are not evidence of success."]
        compose = clone(assistant._compose_brief, replacements)
        return clone(assistant.build_analysis_brief_from_session, {"_compose_brief": compose})(projection)

    def metric_lines(metrics, steps):
        lines = assistant._metrics_lines(metrics, [step for step in _usage_steps(steps)
                                                  if _observed(step, "tokens.total")])
        lines.append("native_source_evidence: " + json.dumps(projection.data_quality, ensure_ascii=False))
        lines.append("Null means unobserved. Partial totals sum observed source records; unavailable checks are not success.")
        return lines

    def timeline_row(step):
        text = assistant._timeline_row(step)
        if step.get("role") == "assistant" and not _observed(step, "tokens.total"):
            text = text.replace(f" {(step.get('tokens') or {}).get('total', 0)}tok ", " ?tok ", 1)
        return text

    insights = assistant.generate_agent_insights

    def agent_insights(rows):
        if any(not _complete(row["data_quality"], "tokens.total", "tokens.cache_read", "tool_outcomes")
               for row in rows):
            return []
        return insights(rows)

    compose = clone(assistant._compose_brief, {
        "_metrics_lines": metric_lines,
        "_timeline_lines": clone(assistant._timeline_lines, {"_timeline_row": timeline_row}),
        "_agent_lines": clone(assistant._agent_lines, {"generate_agent_insights": agent_insights}),
    })
    return clone(assistant.build_analysis_brief_from_session, {"_compose_brief": compose})(projection)
