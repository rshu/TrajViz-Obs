"""Nullable cost presentation for the pinned native comparison dashboards.

Keep native controls, error handling, alignment and behavioral sections. Scope
the evidence-aware builders to each app's callbacks; never patch upstream
module globals or turn absent source measurements into numeric values.
"""

from __future__ import annotations

import copy
import functools
import html
import inspect
import types

from agent_trajviz import Trajectory

from .comparison_quality import project_report, project_scorecard, project_timeline
from .errors import BridgeError


def _scoped(function, replacements):
    original = inspect.unwrap(function)
    if not isinstance(original, types.FunctionType) or any(
        name not in original.__globals__ or name not in original.__code__.co_names for name in replacements
    ):
        raise BridgeError("dashboard_incompatible", "TrajViz's comparison builders changed; update the plugin.")
    cloned = types.FunctionType(original.__code__, {**original.__globals__, **replacements},
                                original.__name__, original.__defaults__, original.__closure__)
    cloned.__kwdefaults__ = original.__kwdefaults__
    return functools.update_wrapper(cloned, original)


def _number(value, spec=","):
    return "unknown" if value is None else html.escape(format(value, spec))


def _elapsed(value):
    return "unknown" if value is None else f"{value:.1f}s"


def _table(headers, rows):
    return ('<table class="cvg-outcome-table"><thead><tr>'
            + "".join(f"<th>{html.escape(str(label))}</th>" for label in headers)
            + "</tr></thead><tbody>"
            + "".join("<tr>" + "".join(f"<td>{value}</td>" for value in row) + "</tr>" for row in rows)
            + "</tbody></table>")


def _replace_outcome_table(markup, table, *, index=0):
    """Replace a named native table, retaining the surrounding native report.

    This is a checked component boundary, not numeric text replacement: file
    names, evidence, milestones, styles and other tables stay byte-for-byte.
    """
    marker = '<table class="cvg-outcome-table">'
    position = 0
    for _ in range(index + 1):
        start = markup.find(marker, position)
        if start < 0:
            raise BridgeError("dashboard_incompatible", "TrajViz's comparison table changed; update the plugin.")
        position = markup.find("</table>", start)
        if position < 0:
            raise BridgeError("dashboard_incompatible", "TrajViz returned an incomplete comparison table.")
        position += len("</table>")
    return markup[:start] + table + markup[position:]


def render_pairwise(report):
    from agent_trajviz.converge.rendering import build_comparison_report_html

    outcome = report.get("outcome", {})
    missing = any(outcome.get(f"{side}_{key}") is None
                  for side in ("reference", "compared") for key in ("tokens", "duration_s"))
    unknown_patterns = not report.get("_obs_cost_coverage", {}).get("compared", {}).get("tokens", True)
    # The upstream renderer requires integers. Its placeholders are confined to
    # the tables replaced below and are never presented as observations.
    display = copy.deepcopy(report)
    for side in ("reference", "compared"):
        if outcome.get(f"{side}_tokens") is None:
            display["outcome"][f"{side}_tokens"] = 0
    for pattern in display.get("patterns", []):
        cost = pattern.get("estimated_extra_cost", {})
        if cost.get("tokens") is None:
            cost["tokens"] = 0
    markup = build_comparison_report_html(display)
    if missing:
        rows = []
        for title, key, render in (("Format", "format", lambda value: html.escape(str(value or "—"))),
                                   ("Steps", "steps", _number), ("Total Tokens", "tokens", _number),
                                   ("Total Tool Calls", "tool_calls", _number),
                                   ("Duration", "duration_s", _elapsed)):
            rows.append([html.escape(title), *(render(outcome.get(f"{side}_{key}"))
                                                for side in ("reference", "compared"))])
        headers = ["Metric", *(outcome.get(f"{side}_filename") or report.get(f"{side}_agent") or side
                               for side in ("reference", "compared"))]
        markup = _replace_outcome_table(markup, _table(headers, rows))
    if unknown_patterns and report.get("patterns"):
        rows = [[html.escape(str(pattern.get("type", "unknown"))), str(len(pattern.get("steps", []))),
                 "unknown", html.escape("; ".join(pattern.get("evidence", [])[:3]))]
                for pattern in report["patterns"]]
        markup = _replace_outcome_table(markup, _table(["Pattern", "Steps", "Extra Tokens", "Evidence"], rows), index=1)
    if not report.get("_obs_costs_complete", True):
        markup += '<p class="cvg-note">Cost evidence is incomplete. Unknown costs are not measured zeros.</p>'
    return markup


def render_scorecard(result, *, include_behavior=True):
    from agent_trajviz.insight import run_group

    rows = result.get("rows") or []
    nullable = any(not row.get("error") and any(row.get(key) is None
                   for key in ("tokens", "wall_clock_s", "tool_success_pct")) for row in rows)
    if not nullable:
        return run_group.build_run_group_scorecard_html(result, include_behavior=include_behavior)
    flags = run_group._best_worst_flags(rows)
    parts = [(f'<div class="cvg-note">{sum(not row.get("error") for row in rows)} of {len(rows)} run(s) loaded'
             ' · unknown means unrecorded or incomplete evidence · green = best, amber = worst among observed values'
             ' · Finished means normal stop, not task correctness</div>')]
    if result.get("error"):
        parts.append(f'<p>{html.escape(str(result["error"]))}</p>')
    headers = ["Condition", "Format", "Finished", "Steps", "Time", "Tokens", "Tools", "Success %", "Peak ctx", "Compacts"]
    parts.append('<table class="cvg-outcome-table"><thead><tr>'
                 + "".join(f"<th>{label}</th>" for label in headers) + "</tr></thead><tbody>")
    for row in rows:
        rid = row.get("run_id", "")
        label = html.escape(str(row.get("label") or rid))
        if row.get("error"):
            parts.append(f'<tr><td>{label}</td><td colspan="9">{html.escape(str(row["error"]))}</td></tr>')
            continue
        peak = _number(row.get("peak_occupancy"))
        if row.get("peak_occupancy") is not None and row.get("peak_pct") is not None:
            peak += f' ({_number(row["peak_pct"], "g")}%)'
        cells = [("", label), ("", html.escape(str(row.get("format") or "—"))),
                 ("", "✓" if row.get("finished") else "✗"), ("steps", _number(row.get("steps"))),
                 ("wall_clock_s", "unknown" if row.get("wall_clock_s") is None
                  else html.escape(str(row.get("wall_clock_fmt") or _elapsed(row["wall_clock_s"])))),
                 ("tokens", _number(row.get("tokens"))), ("", _number(row.get("tool_calls"))),
                 ("tool_success_pct", "unknown" if row.get("tool_success_pct") is None
                  else _number(row["tool_success_pct"], "g") + "%"),
                 ("peak_occupancy", peak), ("compactions", _number(row.get("compactions")))]
        parts.append("<tr>" + "".join(f"<td{run_group._cell_class(rid, key, flags)}>{text}</td>"
                                     for key, text in cells) + "</tr>")
    parts.append("</tbody></table>")
    if include_behavior:
        parts.append(render_behavior(result))
    return "".join(parts)


def _similarity(behavior):
    from agent_trajviz.insight import run_group

    observed = behavior.get("similarity_observed", {})
    if not observed or all(value for row in observed.values() for value in row.values()):
        return run_group._render_similarity_html(behavior)
    ids, labels = behavior.get("run_ids", []), behavior.get("labels", {})
    matrix = behavior.get("similarity", {})
    rows = [[html.escape(str(labels.get(a, a))),
             *(_number(matrix.get(a, {}).get(b), ".2f") if observed.get(a, {}).get(b) else "unknown" for b in ids)]
            for a in ids]
    return ('<h4>Behavioral similarity (cost-weighted alignment F1)</h4>'
            '<p>Unknown pairs lack complete token or tool timing evidence. Diagonal values denote the same run.</p>'
            + _table(["", *(labels.get(rid, rid) for rid in ids)], rows))


def render_behavior(result):
    from agent_trajviz.insight import run_group

    body = _scoped(run_group._render_behavior_html, {"_render_similarity_html": _similarity})
    return body(result.get("behavior") if result else None)


def _run(raw, steps, path="", fmt=None):
    from agent_trajviz.insight.loaders import detect_format

    return Trajectory(format=fmt or detect_format(raw), steps=steps, raw=raw, source=path or None)


def _report_from_steps(ref_raw, cmp_raw, ref_steps, cmp_steps, **kwargs):
    from agent_trajviz.converge.alignment import build_comparison_report_from_steps

    report = build_comparison_report_from_steps(ref_raw, cmp_raw, ref_steps, cmp_steps, **kwargs)
    return project_report(report, _run(ref_raw, ref_steps, kwargs.get("ref_path", "")),
                          _run(cmp_raw, cmp_steps, kwargs.get("cmp_path", "")))


def _group_scorecard(paths, **kwargs):
    from agent_trajviz.insight import chart_data, run_group

    result = run_group.build_run_group_scorecard(paths, **kwargs)
    if not result.get("rows"):
        return result
    baseline = kwargs.get("baseline_raw")
    loaded = ([baseline] if isinstance(baseline, run_group.LoadedRun) and baseline.steps
              and "_error" not in baseline.raw else []) + list(paths)
    sources = [_run(item.raw, item.steps or [], item.path, item.format) if isinstance(item, run_group.LoadedRun)
               else item for item in loaded]
    projected = project_scorecard({"rows": result["rows"], "behavior": result.get("behavior"),
                                   "timelines": chart_data.run_timelines(result["timeline_runs"])}, sources)
    result = {**result, "rows": projected["rows"], "behavior": projected.get("behavior")}
    # An absent rate (including a run with no calls) is not a measured 0%.
    result["rows"] = [({**row, "tool_success_pct": None} if not row.get("error") and not row.get("tool_calls") else row)
                      for row in result["rows"]]
    return result


def _group_timeline(runs, *, dark=False):
    from agent_trajviz.insight import chart_data
    from agent_trajviz.insight.charts.swimlanes import draw_run_timelines

    return draw_run_timelines(project_timeline(chart_data.run_timelines(runs), runs), dark=dark)


def _unknown_cost_figure():
    from agent_trajviz.converge.charts import _empty_figure

    return _empty_figure(message="Cost unknown: token or tool timing evidence is incomplete")


def _file_report(ref_file, cmp_file, token_rate=50.0, fuzzy_commands=False, anchor_patch=None, task_id="",
                 ref_labels=None, cmp_labels=None):
    from agent_trajviz.converge import alignment

    from .compat import load_run

    # The legacy file entrypoint bypasses public loaders for canonical exports.
    # Load once through the same format registry as /obs, then use the native
    # comparison pipeline over those exact records (also preserves subagents).
    reference, compared = load_run(ref_file), load_run(cmp_file)
    report = alignment.build_comparison_report_from_steps(
        reference.raw, compared.raw, reference.steps, compared.steps, token_rate=token_rate,
        fuzzy_commands=fuzzy_commands, anchor_files=alignment._parse_anchor_files(anchor_patch),
        ref_path=str(ref_file), cmp_path=str(cmp_file), task_id=task_id,
        ref_labels=ref_labels, cmp_labels=cmp_labels)
    return project_report(report, reference, compared)


def _converge_pairwise(original):
    from agent_trajviz.converge import app, charts

    @functools.wraps(original)
    def compare_uploads(ref_upload, cmp_upload, anchor_upload, rate, fuzzy):
        ref, cmp = app._upload_path(ref_upload), app._upload_path(cmp_upload)
        if ref is None or cmp is None:
            return original(ref_upload, cmp_upload, anchor_upload, rate, fuzzy)
        anchor = app._upload_path(anchor_upload)
        try:
            report = _file_report(ref_file=ref, cmp_file=cmp, token_rate=app._token_rate(rate),
                                  fuzzy_commands=bool(fuzzy), anchor_patch=anchor, task_id="")
            segments = report.get("segments", {})
            known = report.get("_obs_costs_complete", True)
            return (render_pairwise(report), charts.build_milestone_timeline_chart(
                        report.get("ref_milestones", {}), report.get("cmp_milestones", {})),
                    charts.build_segment_cost_chart(segments, segments.get("milestone_order_matches", False))
                    if known else _unknown_cost_figure(),
                    charts.build_divergence_waterfall_chart(report.get("patterns", []))
                    if report.get("_obs_cost_coverage", {}).get("compared", {}).get("tokens", True)
                    else _unknown_cost_figure(),
                    {"__type__": "update", "value": charts.build_anchor_class_chart(report.get("anchor_analysis")),
                     "visible": report.get("anchor_analysis") is not None})
        except Exception as exc:  # noqa: BLE001 - same native visible error contract
            empty = charts._empty_figure(message="Comparison failed")
            return (app._error_html("Comparison", exc, app._upload_names(ref, cmp, anchor)), empty, empty, empty,
                    {"__type__": "update", "value": empty, "visible": False})
    return compare_uploads


def _manifest_html(renderer, *args, **kwargs):
    from agent_trajviz.converge import app, batch

    groups = []

    def run_manifest(manifest_path, rate, fuzzy, data_root):
        entries = batch.parse_manifest(manifest_path, base_dir=data_root, root=data_root)
        results = _scoped(batch.run_batch, {"build_comparison_report": _file_report})(
            entries, token_rate=rate, fuzzy_commands=bool(fuzzy))
        groups.append(results)
        return results

    markup = _scoped(renderer, {"_run_manifest": run_manifest})(*args, **kwargs)
    expected = 2 if renderer is app.before_after_report_html else 1
    if len(groups) != expected:
        return markup  # A failed manifest must not retain an unlabeled partial report.
    reports = [result.report for group in groups for result in group if result.report is not None]
    if not any(not report.get("_obs_costs_complete", True) for report in reports):
        return markup
    known = sum(report.get("_obs_costs_complete", True) for report in reports)
    note = (f'<div class="cvg-note">Cost evidence: {known} of {len(reports)} comparisons complete. '
            'Unknown cost-weighted metrics are excluded from aggregates and paired deltas; '
            'task counts still include their observed actions.</div>')
    rows = []
    for index, group in enumerate(groups):
        prefix = ("Before: " if index == 0 else "After: ") if len(groups) == 2 else ""
        for result in group:
            if result.report is None:
                continue
            outcome = result.report["outcome"]
            rows.append([html.escape(prefix + result.task_id),
                         *(_number(outcome.get(f"{side}_tokens")) for side in ("reference", "compared")),
                         *(_elapsed(outcome.get(f"{side}_duration_s")) for side in ("reference", "compared"))])
    return note + markup + _table(["Task", "Reference tokens", "Compared tokens", "Reference time", "Compared time"], rows)


def adapt_comparisons(app, kind):
    """Adapt only comparison callback functions; IDs, inputs and outputs stay native."""
    if getattr(app, "_obs_comparison_costs", False):
        return
    from agent_trajviz.converge import app as converge
    from agent_trajviz.insight import comparison
    from agent_trajviz.insight.ui.shared import safe_callback

    signatures = ({"on_run_comparison": ("ref_file", "anchor_file", "ref_format", "ref_labels_file",
                                         "cmp_labels_file", "overview_run", "dark", "ref_session"),
                   "on_run_group_scorecard": ("files", "format_hint", "dark", "overview_run")}
                  if kind == "insight" else
                  {"compare_uploads": ("ref_upload", "cmp_upload", "anchor_upload", "rate", "fuzzy"),
                   "do_batch": ("manifest_upload", "rate", "fuzzy"),
                   "do_before_after": ("before_upload", "after_upload", "rate", "fuzzy")})
    callbacks = {callback.api_name: callback for callback in app.fns.values()}
    if not set(signatures).intersection(callbacks) and "_do_load" not in callbacks:
        return  # Small presentation-only Blocks used without either dashboard.
    for name, parameters in signatures.items():
        callback = callbacks.get(name)
        if callback is None or tuple(inspect.signature(callback.fn).parameters) != parameters:
            raise BridgeError("dashboard_incompatible", "TrajViz's comparison callbacks changed; update the plugin.")
    for callback in app.fns.values():
        if getattr(callback.fn, "_obs_comparison_costs", False):
            continue
        original = callback.fn
        if kind == "insight" and callback.api_name == "on_run_comparison":
            run = _scoped(comparison.run_comparison, {"build_comparison_report_from_steps": _report_from_steps,
                                                     "build_comparison_report_html": render_pairwise})
            callback.fn = _scoped(callback.fn, {"run_comparison": run})
        elif kind == "insight" and callback.api_name == "on_run_group_scorecard":
            run = _scoped(callback.fn, {"build_run_group_scorecard": _group_scorecard,
                                       "build_run_group_scorecard_html": render_scorecard,
                                       "build_run_group_agent_timeline": _group_timeline,
                                       "build_run_group_behavior_html": render_behavior})
            callback.fn = safe_callback("Run group scorecard")(run)
        elif kind == "converge" and callback.api_name == "compare_uploads":
            callback.fn = _converge_pairwise(callback.fn)
        elif kind == "converge" and callback.api_name in ("do_batch", "do_before_after"):
            name = "batch_report_html" if callback.api_name == "do_batch" else "before_after_report_html"
            renderer = getattr(converge, name)
            callback.fn = _scoped(callback.fn, {name: functools.partial(_manifest_html, renderer)})
        if callback.fn is not original:
            callback.fn._obs_comparison_costs = True
    app._obs_comparison_costs = True
