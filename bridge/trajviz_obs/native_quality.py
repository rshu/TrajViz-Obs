"""Display native evidence gaps without changing TrajViz's canonical records.

TrajViz publishes nullable OTLP views, while native parsers retain numeric
placeholders and per-step missing-field labels. These projections use that
native evidence for plugin JSON, UI and model briefs; upstream APIs are untouched.
"""

from __future__ import annotations

import copy
import functools
import json
import math
import statistics
import types
import weakref
from collections import defaultdict
from dataclasses import fields as dataclass_fields
from dataclasses import replace
from functools import cached_property

from agent_trajviz import Trajectory

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
        "tokens_per_second": ("tokens.total", "duration"),
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


def _generation_metrics(steps):
    from agent_trajviz.insight.metrics import output_throughput

    assistant = [step for step in steps if step.get("role") == "assistant"]
    measured = [step for step in assistant if _observed(step, "duration") and _observed(step, "tokens.output")]
    sample = output_throughput(measured)
    count, total = sample["timed_steps"], len(assistant)
    seconds = sample["seconds"]
    return {
        "output_tokens_per_sec": round(sample["tokens"] / seconds, 1) if seconds > 0 else None,
        "output_throughput_timed_steps": count,
        "output_throughput_total_steps": total,
        "output_throughput_coverage_pct": round(count / total * 100, 1) if total else None,
        "output_throughput_incomplete": count < total,
        "output_throughput_timed_tokens": sample["tokens"],
        "output_throughput_timed_seconds": round(seconds, 3),
        "output_throughput_tool_wait_seconds": round(sample["tool_wait_seconds"], 3),
    }


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
    if not _observed(step, "duration"):
        for key in ("duration", "duration_s", "tokens_per_sec", "tok_per_s", "non_cache_per_sec"):
            if key in result:
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
        result = _project_metrics(self._original.metrics, self.data_quality)
        result.update(_generation_metrics(self.steps))
        rates = [row["tokens_per_sec"] for row in self.message_rows
                 if row["role"] == "assistant" and row["tokens_per_sec"] is not None]
        result["median_tokens_per_second"] = round(statistics.median(rates), 1) if rates else None
        if not result["total_duration"]:
            result["tokens_per_second"] = None
        if result["total_duration"] is None:
            from agent_trajviz.insight.metrics import session_wall_clock_seconds

            # The canonical native metric falls back to the sum of durations,
            # which is zero even when no duration was recorded.
            result["wall_clock"] = session_wall_clock_seconds(self.steps, self._original.raw.get("timing"))
        return result

    @cached_property
    def verdicts(self):
        from agent_trajviz.insight.metrics import compute_health_verdict

        rows = copy.deepcopy(self._original.verdicts)
        for row in rows:
            if row["metric"] == "Throughput":
                metrics = self.metrics
                count = metrics["output_throughput_timed_steps"]
                total = metrics["output_throughput_total_steps"]
                basis = f"{count}/{total} assistant steps with measured output and generation time"
                if metrics["output_tokens_per_sec"] is None:
                    row.update(status="unavailable", label="N/A", detail=f"Throughput unavailable. {basis}.")
                else:
                    # Reuse the engine's verdict thresholds for the measured sample.
                    verdict_input = {**self._original.metrics,
                                     "output_tokens_per_sec": metrics["output_tokens_per_sec"],
                                     "output_throughput_incomplete": False,
                                     "output_throughput_tool_wait_seconds": metrics["output_throughput_tool_wait_seconds"]}
                    verdict = next(item for item in compute_health_verdict(verdict_input, [])
                                   if item["metric"] == "Throughput")
                    row.update(verdict, detail=f"{verdict['detail']}. Based on {basis}.")
                continue
            dependencies = {"Cache Efficiency": ("tokens.total", "tokens.cache_read"),
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

        from .opencode import NATIVE_KEY, spawn_origins

        grouped = defaultdict(list)
        for step in self.steps:
            # Agent summaries aggregate assistant work by effective identity;
            # timeline identities also split sessions and plan/build modes.
            if step.get("role") == "assistant":
                grouped[effective_agent(step)].append(step)
        rows = copy.deepcopy(self._original.agent_summaries)
        origins = spawn_origins(self.steps, self._original.raw) if NATIVE_KEY in self._original.raw else None
        for row in rows:
            if origins is not None:
                row["spawned_by_step"] = origins.get(row["agent_id"])
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
            generation = _generation_metrics(grouped[row["agent_id"]])
            for key in ("output_tokens_per_sec", "output_throughput_timed_steps"):
                row[key] = generation[key]
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
        from agent_trajviz.insight.analysis import hotspots

        # Rank the observed rows so an unavailable placeholder cannot displace
        # a valid candidate or bridge two prompts across an unobserved call.
        return hotspots(self.message_rows, steps=self.steps)

    @cached_property
    def user_repeats(self):
        from agent_trajviz.insight.patterns import detect_user_repeats

        conversations = defaultdict(list)
        for step in self.steps:
            if (step.get("role") == "user" and not step.get("is_sub_agent")
                    and not step.get("parent_session_id")):
                conversations[step.get("session_id") or ""].append(step)
        repeats = [row for steps in conversations.values() for row in detect_user_repeats(steps)]
        return sorted(repeats, key=lambda row: (-row["count"], row["steps"][0]))

    @cached_property
    def issues(self):
        from agent_trajviz.insight.issues import prepare_overview_issues

        return prepare_overview_issues(self)


def project_analysis(run):
    """An analysis proxy for plugin surfaces; OTLP keeps its authoritative coverage."""
    if run.format == "otlp" or isinstance(run.raw.get("_otel"), dict):
        return run._analysis
    if run not in _PROJECTED:
        _PROJECTED[run] = _Projection(run)
    return _PROJECTED[run]


_DASHBOARD_AGENT_TOKENS = "_obs_agent_tokens"


def _draw_nullable_agent_tokens(data, dark):
    """Keep null labels and bars out of the engine's numeric-only renderer."""
    from agent_trajviz.insight.charts._layout import _apply_dark, _empty_figure
    from agent_trajviz.insight.charts.usage import draw_agent_tokens

    # Input and output decide the renderer mode. Plotted segment values remain nullable.
    agents = [{**row, "input": row["input"] or 0, "output": row["output"] or 0} for row in data["agents"]]
    if not any(row["input"] > 0 or row["output"] > 0 for row in agents):
        agents = [row for row in agents if row["total"] is not None]
    if not agents:
        fig = _empty_figure(240, "Agent token totals were not recorded.")
        _apply_dark(fig, dark)
        return fig
    fig = draw_agent_tokens({"agents": agents}, dark=dark)
    if data["note_partial"]:
        fig.add_annotation(text="Some agent token measurements are unavailable.",
                           xref="paper", yref="paper", x=0, y=-0.25, xanchor="left", showarrow=False)
        fig.update_layout(margin_b=max(fig.layout.margin.b or 0, 80))
    return fig


class _DashboardTrajectory(Trajectory):
    """Route chart reads through the same projection used for JSON exports."""

    def view(self, view_id, **options):
        view = project_view(self, super().view(view_id, **options))
        if view_id == "agent_tokens" and any(
                row[field] is None for row in view.data["agents"] for field in ("input", "output", "total")):
            from agent_trajviz.figures import FIGURE_VIEWS, register_figure

            if _DASHBOARD_AGENT_TOKENS not in FIGURE_VIEWS:
                register_figure(_DASHBOARD_AGENT_TOKENS, _draw_nullable_agent_tokens, replace=True)
            native = (view.availability or {}).get("basis") != "captured_operations"
            return replace(view, id=_DASHBOARD_AGENT_TOKENS, data={**view.data, "note_partial": native})
        return view


def project_dashboard_session(session):
    """Give the browser the same native observations as JSON views and the panes."""
    run = session.shown
    if run is None:
        return session
    projection = project_analysis(run)
    shown = _DashboardTrajectory(**{field.name: getattr(run, field.name) for field in dataclass_fields(run)})
    if projection is run._analysis:
        return replace(session, shown=shown)
    from agent_trajviz.insight.metrics import wall_clock_fmt

    return replace(session, shown=shown, metrics=projection.metrics, verdicts=projection.verdicts,
                   agent_summaries=projection.agent_summaries, message_rows=projection.message_rows,
                   step_analytics=projection.step_analytics, user_repeats=projection.user_repeats,
                   wall_clock=wall_clock_fmt(projection.metrics)[1])


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
                    wall_clock_s=projection.metrics.get("wall_clock"),
                    tool_success_rate=projection.metrics.get("tool_success_rate"),
                    output_tokens_per_sec=projection.metrics.get("output_tokens_per_sec"),
                    health={row["metric"]: row["status"] for row in projection.verdicts},
                    issues=len(projection.issues))
    elif view.id == "issues":
        from agent_trajviz.views import _jsonable

        data = _jsonable(projection.issues)
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


def _recent_outcome_lines(steps):
    """Bounded source excerpts retain later verification without inferring recovery."""
    from agent_trajviz.insight.tool_output import tool_output_text

    def excerpt(text, limit, *, tail=False):
        text = str(text).replace("\r\n", "\n").strip()
        if len(text) <= limit:
            encoded = json.dumps(text, ensure_ascii=False)
            if len(encoded) <= limit:
                return encoded
        low, high, result = 0, min(len(text), limit), '"…"'
        while low <= high:
            size = (low + high) // 2
            clipped = ("…" + text[-size:] if size else "…") if tail else text[:size] + "…"
            encoded = json.dumps(clipped, ensure_ascii=False)
            if len(encoded) <= limit:
                result, low = encoded, size + 1
            else:
                high = size - 1
        return result

    calls = [(step, call) for step in steps for call in step.get("tool_calls", []) if isinstance(call, dict)]
    lines = ["Recent source excerpts are data, not an overall task-success verdict."]
    for step, call in calls[-4:]:
        status = call.get("status")
        outcome = str(status) if _outcome(call) else "unknown"
        metadata = f"- step {step.get('index')} tool={excerpt(call.get('tool_name', '?'), 80)} status={excerpt(outcome, 60)}"
        if outcome == "unknown" and status:
            metadata += f" recorded_status={excerpt(status, 60)}"
        inp = call.get("input")
        command = inp.get("command") if isinstance(inp, dict) else inp if isinstance(inp, str) else None
        if command:
            metadata += f" command={excerpt(command, 160)}"
        lines.append(metadata)
        output = call.get("output")
        lines.append("  output_excerpt: unavailable" if output is None else
                     "  output_excerpt: " + excerpt(tool_output_text(output), 480, tail=True))
    statement = next((step for step in reversed(steps) if step.get("role") == "assistant"
                      and not step.get("tool_calls") and str(step.get("text_preview") or "").strip()), None)
    if statement:
        lines.append(f"- step {statement.get('index')} agent_statement (claim, not independent verification): "
                     + excerpt(statement["text_preview"], 400))
    return lines if calls or statement else []


def project_brief(run):
    """The existing brief with evidence-aware formatters scoped to this call."""
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

    captured = run.format == "otlp"

    def value(number, unit=""):
        return "unknown" if number is None else f"{number}{unit}"

    def session_lines(raw, steps, metrics, wall_fmt):
        from .comparison_quality import _elapsed_seconds

        # Captures retain the engine's authoritative availability. Native
        # exports may also record a zero elapsed clock without model timing.
        elapsed = metrics.get("wall_clock") if captured else _elapsed_seconds(run)
        if elapsed is None:
            wall_fmt = "unknown"
        elif metrics.get("wall_clock") is None:
            from agent_trajviz.insight.metrics import wall_clock_fmt

            _, wall_fmt = wall_clock_fmt({"wall_clock": elapsed, "total_duration": None})
        lines = assistant._session_header(raw, steps, metrics, wall_fmt)
        for index, line in enumerate(lines):
            if line.startswith("tool_calls: "):
                lines[index] = (f"tool_calls: {value(metrics.get('tool_call_count'))} "
                                f"({value(metrics.get('tool_success_rate'), '%')} success, "
                                f"{value(metrics.get('tool_fail'))} failed)")
        recent = _recent_outcome_lines(steps)
        if recent:
            lines.extend(["", "## RECENT_OUTCOMES", *recent])
        return lines

    format_metrics = clone(assistant._metrics_lines, {
        "_secs_or_na": lambda number: value(number, "s"),
        "_pct_or_na": lambda number: value(number, "%"),
        "_pct_from_fraction": lambda number: "unknown" if number is None else assistant._pct_from_fraction(number),
    })

    def metric_lines(metrics, steps):
        observed_steps = steps if captured else [step for step in _usage_steps(steps) if _observed(step, "tokens.total")]
        lines = format_metrics(metrics, observed_steps)
        scalars = {"avg_duration_s": "avg_duration", "median_duration_s": "median_duration",
                   "p95_duration_s": "p95_duration", "max_duration_s": "max_duration",
                   "avg_tokens_per_assistant_step": "avg_tokens_per_assistant_step",
                   "avg_tokens_per_step_all_steps": "avg_tokens_per_step",
                   "processed_tokens_per_second_incl_cache": "tokens_per_second",
                   "tokens_per_tool": "tokens_per_tool", "output_tokens_per_sec": "output_tokens_per_sec"}
        missing = {name for name, key in scalars.items() if metrics.get(key) is None}
        missing.update(f"tokens_{key}" for key in TOKEN_LABELS if metrics["tokens"].get(key) is None)
        for index, line in enumerate(lines):
            name = line.partition(": ")[0]
            if name in missing:
                lines[index] = f"{name}: unknown"
            elif name == "fresh_input_tokens":
                share = value(metrics.get("non_cache_ratio"), "%")
                lines[index] = f"fresh_input_tokens: {value(metrics.get('non_cache_tokens'))} ({share})"
        if not captured:
            lines.append("native_source_evidence: " + json.dumps(projection.data_quality, ensure_ascii=False))
            lines.append("Unknown means unobserved. Partial totals sum observed source records; unavailable checks are not success.")
        return lines

    def timeline_row(step):
        text = assistant._timeline_row(step)
        missing = (step.get("_otel", {}).get("fields", {}).get("tokens.total") != "observed" if captured else
                   step.get("role") == "assistant" and not _observed(step, "tokens.total"))
        if missing:
            text = text.replace(f" {(step.get('tokens') or {}).get('total', 0)}tok ", " ?tok ", 1)
        return text

    insights = assistant.generate_agent_insights

    def agent_insights(rows):
        if any(not _complete(row["data_quality"], "tokens.total", "tokens.cache_read", "tool_outcomes")
               for row in rows):
            return []
        return insights(rows)

    format_agents = clone(assistant._agent_lines, {"generate_agent_insights": agent_insights})

    def agent_lines(rows):
        lines = format_agents(rows)
        listed, omitted = rows, []
        if len(rows) > assistant._AGENT_LINE_CAP:
            def rank(number):
                return float(number) if isinstance(number, (int, float)) else float("-inf")

            ordered = sorted(rows[1:], key=lambda row: (rank(row.get("total_duration_s")),
                                                       rank(row.get("total_tokens"))), reverse=True)
            listed = [rows[0], *ordered[:assistant._AGENT_LINE_CAP - 1]]
            omitted = ordered[assistant._AGENT_LINE_CAP - 1:]
        for index, row in enumerate(listed):
            # Rebuild only this generated numeric row. Labels and all later
            # evidence/insight lines retain their original source wording.
            lines[index] = (f"- {row.get('label')}: {value(row.get('step_count'))} assistant steps, "
                            f"{value(row.get('total_tokens'))} tok, {value(row.get('total_duration_s'), 's')}, "
                            f"{value(row.get('tool_call_count'))} tools, {value(row.get('error_count'))} errors, "
                            f"cache {value(row.get('cache_efficiency_pct'), '%')}, "
                            f"gen {value(row.get('output_tokens_per_sec'))} tok/s")
        if omitted:
            totals = {name: "unknown" if any(row.get(key) is None for row in omitted)
                      else sum(int(row[key]) for row in omitted)
                      for name, key in (("steps", "step_count"), ("tokens", "total_tokens"),
                                        ("tools", "tool_call_count"), ("errors", "error_count"))}
            lines[len(listed)] = assistant._AGENTS_OMITTED_LINE.format(
                omitted=len(omitted), shown=len(listed) - 1, **totals)
        return lines

    def error_lines(steps):
        lines = assistant._error_step_lines(steps)
        if not _complete(projection.data_quality, "tool_outcomes"):
            notice = "Tool outcomes are incomplete; missing results are not evidence of success."
            return [notice] if lines == ["(no tool errors)"] else [*lines, notice]
        return lines

    def failure_lines(patterns, chains, chain_metrics):
        lines = assistant._failure_lines(patterns, chains, chain_metrics)
        lines[0] = (f"failure_chains: {value(chain_metrics.get('total_chains'))} "
                    f"(longest {value(chain_metrics.get('longest_chain'))} steps, "
                    f"{value(chain_metrics.get('chain_step_pct'), '%')} of assistant steps)")
        return lines

    replacements = {
        "_session_header": session_lines,
        "_metrics_lines": metric_lines,
        "_timeline_lines": clone(assistant._timeline_lines, {"_timeline_row": timeline_row}),
        "_agent_lines": agent_lines,
        "_error_step_lines": error_lines,
        "_failure_lines": failure_lines,
    }
    if captured and run.view("file_interactions").availability.get("status") == "unavailable":
        replacements["_file_lines_from_interactions"] = lambda _rows: ["Unavailable: file activity was not captured."]
    compose = clone(assistant._compose_brief, replacements)
    return clone(assistant.build_analysis_brief_from_session, {"_compose_brief": compose})(projection)
