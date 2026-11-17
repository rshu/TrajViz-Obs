"""Prompt-aware timeline compression for the TrajViz 0.5.1 dashboard."""

from __future__ import annotations

import functools
import inspect
from bisect import bisect_right
from itertools import pairwise

_COMPRESSED_TITLE = "Execution Timeline (compressed)"
_COMPRESSED_AXIS = "Elapsed time from the first message · idle gaps compressed"
_PROMPT_WINDOW_S = 5.0
_PROMPT_GAP_CAP_S = 15.0
TIMELINE_TRANSLATIONS = {
    "Compress idle gaps (default)": "压缩空闲间隔（默认）",
    _COMPRESSED_TITLE: "执行时间线（压缩空闲间隔）",
    _COMPRESSED_AXIS: "距第一条消息的经过时间 · 空闲间隔已压缩",
    "Execution Timeline (wall-clock)": "执行时间线（实际经过时间）",
    "Time (s) — relative to the first message": "距第一条消息的时间（秒）",
}


@functools.cache
def timeline_builder():
    """Apply upstream's prompt-aware mapping without replacing chart rendering."""
    from agent_trajviz.i18n import localize_figure
    from agent_trajviz.insight.charts import gantt

    from .native_quality import _clone_native_function

    build = gantt.build_execution_gantt_chart
    original_axis = getattr(gantt, "_IdleGapAxis", None)
    if original_axis is None or "user_times" in inspect.signature(original_axis).parameters:
        return build  # Newer TrajViz versions contain the fix natively.

    def render(steps, dark=False, axis_mode=gantt.GANTT_AXIS_SKIP_IDLE):
        timed = [(step, gantt._epoch_ms(step.get("time_created_ms"))) for step in steps if isinstance(step, dict)]
        times = [t for _, t in timed if t is not None]
        if not times:
            return build(steps, dark=dark, axis_mode=axis_mode)
        origin = min(times)
        prompts = sorted((t - origin) / 1000.0 for step, t in timed
                         if t is not None and gantt.workflow_role(step) == "user")
        prompt_gaps = set()
        idle_gaps = []

        def idle_axis(spans, compress):
            # Include spans of parallel tools and agents when finding idle periods.
            axis = original_axis(spans, compress)
            merged = []
            for start, end in sorted(spans):
                if merged and start <= merged[-1][1]:
                    merged[-1][1] = max(end, merged[-1][1])
                else:
                    merged.append([start, end])
            gaps = {(start, end): width for start, end, width in axis.gaps}
            for (_, start), (end, _) in pairwise(merged):
                prompt = bisect_right(prompts, end) - 1
                if (end - start >= gantt._MIN_IDLE_GAP_S and prompt >= 0
                        and start < prompts[prompt] and end - prompts[prompt] <= _PROMPT_WINDOW_S):
                    prompt_gaps.add((start, end))
                    gaps[start, end] = min(end - start, _PROMPT_GAP_CAP_S) if compress else end - start
            idle_gaps.extend(sorted(gaps))
            axis.gaps = [(start, end, gaps[start, end]) for start, end in idle_gaps]
            shifts = [(start, end, end - start - width) for start, end, width in axis.gaps]

            def map_time(t):
                shown = t
                for start, end, shift in shifts:
                    if t >= end:
                        shown -= shift
                    elif t > start:
                        shown -= shift * (t - start) / (end - start)
                        break
                return shown

            axis._shifts = shifts
            axis.map = map_time
            return axis

        native = _clone_native_function(build, {"_IdleGapAxis": idle_axis})
        fig = native(steps, dark=dark, axis_mode=axis_mode)
        if fig is None:
            return None
        if fig.layout.xaxis.tickvals is not None:
            fig.update_layout(title_text=_COMPRESSED_TITLE, xaxis_title=_COMPRESSED_AXIS)
        bands = [trace for trace in fig.data if trace.name == gantt._WAIT_LEGEND]
        # Native bands and their annotations share chronological order.
        annotations = [annotation for annotation in fig.layout.annotations if annotation.y == gantt._WAIT_LANE_LABEL]
        for gap, trace, annotation in zip(idle_gaps, bands, annotations, strict=True):
            label = annotation.text.replace(" — waiting for user", "")
            if gap in prompt_gaps:
                parts = label.split("<br>", 1)
                parts[0] += " before user message"
                label = "<br>".join(parts)
            trace.hovertext = annotation.text = label
        return localize_figure(fig)

    return render
