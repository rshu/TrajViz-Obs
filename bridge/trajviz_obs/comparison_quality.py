"""Evidence-aware copies of native comparison results for CLI and browser views.

The engine's canonical native records retain numeric placeholders. Display and
aggregate only observed costs, while leaving those records and OTLP handling
unchanged. A measured zero remains a measurement.
"""

from __future__ import annotations

import copy
import math
from bisect import bisect_left

from agent_trajviz import Trajectory

from .native_quality import _observed, project_analysis

_ALIGNMENT_COST_METRICS = ("reference_recall", "behavioral_precision", "alignment_f1", "overhead_ratio")


def _native(run: Trajectory) -> bool:
    return run.format != "otlp" and not isinstance(run.raw.get("_otel"), dict)


def _number(value) -> bool:
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def _tool_clocks(run: Trajectory) -> list[bool]:
    """Observation checks at the same timing seam as canonicalize_steps."""
    from agent_trajviz.converge.canonical import _PLANNING_TOOLS

    known = []
    for step in run.steps:
        for call in step.get("tool_calls", []):
            # Planning tools are REASON actions, excluded from weighted alignment.
            if call.get("tool_name") in _PLANNING_TOOLS:
                continue
            start, end = call.get("time_start"), call.get("time_end")
            observed = _number(start) and _number(end) and end >= start
            if not observed:
                duration = call.get("duration_ms")
                if duration is None:
                    metadata = call.get("metadata")
                    duration = metadata.get("totalDurationMs") if isinstance(metadata, dict) else None
                observed = _number(duration) and duration >= 0
            known.append(bool(observed))
    return known


def _coverage(observed: int, total: int) -> dict:
    return {"status": "observed" if observed == total else "partial" if observed else "unknown",
            "observed": observed, "partial": 0, "total": total,
            "reasons": {"not_recorded_or_invalid": total - observed} if observed < total else {}}


def metric_coverage(run: Trajectory) -> dict:
    """Native token totals, elapsed clock and canonical tool-latency coverage."""
    analysis = project_analysis(run)
    clocks = _tool_clocks(run)
    return {"tokens.total": copy.deepcopy(analysis.data_quality["fields"]["tokens.total"]),
            "duration": _coverage(int(analysis.metrics.get("wall_clock") is not None), 1),
            "tool_timing": _coverage(sum(clocks), len(clocks))}


def project_report(report: dict, reference: Trajectory, compared: Trajectory) -> dict:
    """Copy a native report with unknown costs excluded from display and statistics.

    The browser accepts nullable display values. CLI exporters keep their
    published integer fields and use these values to add observation flags.
    """
    result = copy.deepcopy(report)
    if not _native(reference) or not _native(compared):
        return result
    coverage = {}
    for side, run in (("reference", reference), ("compared", compared)):
        fields = metric_coverage(run)
        tokens = fields["tokens.total"]["status"] == "observed"
        metrics = project_analysis(run).metrics
        elapsed = metrics.get("wall_clock")
        timing = fields["tool_timing"]["status"] == "observed"
        coverage[side] = {"tokens": tokens, "elapsed": elapsed is not None, "tool_timing": timing,
                          "costs": tokens and timing}
        outcome = result.setdefault("outcome", {})
        outcome[f"{side}_tokens"] = metrics["tokens"]["total"] if tokens else None
        outcome[f"{side}_duration_s"] = elapsed
    complete = all(side["costs"] for side in coverage.values())
    result["_obs_cost_coverage"] = coverage
    result["_obs_costs_complete"] = complete
    alignment = result.get("alignment", {})
    if not complete:
        for name in _ALIGNMENT_COST_METRICS:
            if name in alignment:
                alignment[name] = None
    if not coverage["compared"]["costs"] and "harmful_ratio" in alignment:
        alignment["harmful_ratio"] = None
    harmful = alignment.get("harmful_cost", {})
    if not coverage["compared"]["tokens"] and "tokens" in harmful:
        harmful["tokens"] = None
    if not coverage["compared"]["tool_timing"] and "latency_ms" in harmful:
        harmful["latency_ms"] = None
    segments = result.get("segments", {})
    if not complete:
        for segment in segments.get("segment_comparison", []):
            for field in ("recall", "precision", "overhead"):
                if field in segment:
                    segment[field] = None
    for side in ("reference", "compared"):
        if not coverage[side]["tokens"]:
            for segment in segments.get(f"{side}_segments", []):
                segment["tokens"] = None
    for pattern in result.get("patterns", []):
        costs = pattern.get("estimated_extra_cost", {})
        if not coverage["compared"]["tokens"] and "tokens" in costs:
            costs["tokens"] = None
        if not coverage["compared"]["tool_timing"] and "latency_ms" in costs:
            costs["latency_ms"] = None
    return result


def project_timeline(data: dict, runs: list[dict]) -> dict:
    """Copy a run timeline, preserving known segment zeros and unknown gaps."""
    result = copy.deepcopy(data)
    source_runs = {run["run_id"]: run for run in runs}
    for lane in result.get("runs", []):
        source = source_runs.get(lane["run_id"], {})
        steps = source.get("steps", [])
        if source.get("format") == "otlp" or any(isinstance(step.get("_otel"), dict) for step in steps):
            continue
        missing = sorted(step["index"] for step in steps if step.get("role") == "assistant"
                         and not _observed(step, "tokens.total"))
        for segment in lane.get("segments", []):
            first = bisect_left(missing, segment["start"])
            if first < len(missing) and missing[first] <= segment["end"]:
                segment["tokens"] = None
    return result


def project_scorecard(result: dict, sources: list[Trajectory | str]) -> dict:
    """Copy nullable scorecard and timeline metrics without altering canonical runs."""
    result = copy.deepcopy(result)
    timeline_sources = []
    cost_observations = {}
    for row, run in zip(result["rows"], sources, strict=True):
        if row.get("error"):
            continue
        if not isinstance(run, Trajectory):
            cost_observations[row["run_id"]] = False
            continue
        if not _native(run):
            # Authoritative telemetry availability remains the engine's concern.
            cost_observations[row["run_id"]] = True
            continue
        analysis = project_analysis(run)
        fields = analysis.data_quality["fields"]
        row["data_quality"] = copy.deepcopy(analysis.data_quality)
        row["tokens_observed"] = fields["tokens.total"]["status"] == "observed"
        cost_observations[row["run_id"]] = row["tokens_observed"] and all(_tool_clocks(run))
        if not row["tokens_observed"]:
            row.update(tokens=None, peak_occupancy=None, peak_pct=None)
        if fields["tool_outcomes"]["status"] != "observed" or not row.get("tool_calls"):
            row["tool_success_pct"] = None
        elapsed = analysis.metrics.get("wall_clock")
        if elapsed is None:
            row.update(wall_clock_s=None, wall_clock_fmt="?")
        elif row.get("wall_clock_s") != elapsed:
            from agent_trajviz.insight.metrics import wall_clock_fmt

            row["wall_clock_s"], row["wall_clock_fmt"] = wall_clock_fmt({"wall_clock": elapsed, "total_duration": None})
        timeline_sources.append({"run_id": row["run_id"], "steps": run.steps})
    result["timelines"] = project_timeline(result["timelines"], timeline_sources)
    behavior = result.get("behavior")
    if isinstance(behavior, dict):
        # The published schema requires numeric similarity cells. Keep those
        # canonical values and mark which cost-weighted pairs can be displayed.
        # A run's diagonal identity is structural and does not require costs.
        behavior["similarity_observed"] = {
            a: {b: a == b or (cost_observations.get(a, False) and cost_observations.get(b, False))
                for b in values} for a, values in behavior.get("similarity", {}).items()}
    return result
