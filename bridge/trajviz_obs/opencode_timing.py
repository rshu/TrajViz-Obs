"""OpenCode 2 wall-clock timing from overlapping native tool intervals."""

from __future__ import annotations

import functools
import sys

from .opencode import NATIVE_KEY, _number


def _wait_seconds(step: dict, *, spawn_only: bool = False) -> float:
    """Union stamped waits; keep duration-only observations as an upper bound."""
    from agent_trajviz.insight import metrics

    windows = []
    unstamped = 0.0
    for call in step.get("tool_calls") or []:
        if not isinstance(call, dict) or (spawn_only and not metrics.is_spawn_tool_call(call)):
            continue
        duration = metrics.tool_call_duration_ms(call)
        if duration is None:
            continue
        window = metrics.tool_call_window_ms(call)
        if window is None:
            unstamped += duration / 1000
            continue
        start, end = window
        # Imported timestamps outside their owning message cannot inflate its
        # wall wait. Per-tool stats continue to expose the original interval.
        lo, hi = step.get("time_created_ms"), step.get("time_completed_ms")
        start = max(start, lo) if _number(lo) else start
        end = min(end, hi) if _number(hi) else end
        if end >= start:
            windows.append((start, end))
    total = unstamped
    if windows:
        windows.sort()
        start, end = windows[0]
        for next_start, next_end in windows[1:]:
            if next_start > end:
                total += (end - start) / 1000
                start, end = next_start, next_end
            else:
                end = max(end, next_end)
        total += (end - start) / 1000
    return total


def install() -> None:
    """Align shared analysis/chart consumers while preserving every other format."""
    from agent_trajviz.insight import analysis, analytics, chart_data, diagnostics, metrics

    spawn_wait = metrics.spawn_wait_seconds

    @functools.wraps(spawn_wait)
    def spawn_wait_seconds(step):
        return _wait_seconds(step, spawn_only=True) if step.get(NATIVE_KEY) else spawn_wait(step)

    metrics.spawn_wait_seconds = chart_data.spawn_wait_seconds = spawn_wait_seconds
    generation = metrics.generation_seconds

    @functools.wraps(generation)
    def generation_seconds(step):
        if not step.get(NATIVE_KEY):
            return generation(step)
        duration = step.get("duration")
        if not _number(duration):
            return None
        wait = min(_wait_seconds(step), max(0, duration))
        spawn = min(spawn_wait_seconds(step), wait)
        # The model's remainder subtracts every interval once, even when a
        # regular tool overlaps a subagent. Delegation stays out of tool stats.
        return max(0, duration - wait), wait - spawn

    metrics.generation_seconds = generation_seconds
    compute = analytics.compute_step_analytics

    @functools.wraps(compute)
    def compute_step_analytics(steps):
        rows = compute(steps)
        for step, row in zip(steps, rows, strict=True):
            if step.get(NATIVE_KEY):
                seconds = metrics.non_spawn_tool_seconds(step)
                row["tool_time_ms"] = seconds * 1000
                duration = step.get("duration")
                row["tool_time_share"] = round(seconds / duration, 4) if duration and duration > 0 else None
        return rows

    analytics.compute_step_analytics = compute_step_analytics
    analysis.compute_step_analytics = compute_step_analytics
    # The assistant needs the optional LLM extra. Future imports get the
    # patched analytics directly; an already-loaded module has a bound alias.
    assistant = sys.modules.get("agent_trajviz.insight.assistant")
    if assistant is not None:
        assistant.compute_step_analytics = compute_step_analytics
    decompose = diagnostics.decompose_hotspot_duration

    @functools.wraps(decompose)
    def decompose_hotspot_duration(step, analytics_row=None, idle_gap=None):
        result = decompose(step, analytics_row, idle_gap)
        duration = step.get("duration")
        if step.get(NATIVE_KEY) and _number(duration) and duration > 0:
            tool = min(_wait_seconds(step), duration)
            inference = max(0, duration - tool)
            total = duration + max(0, idle_gap or 0)
            result.update(tool_s=round(tool, 2), inference_s=round(inference, 2),
                          tool_pct=round(tool / total * 100, 1),
                          inference_pct=round(inference / total * 100, 1))
        return result

    diagnostics.decompose_hotspot_duration = decompose_hotspot_duration
