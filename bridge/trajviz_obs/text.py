"""Text renderings: the overview the model reads (the snapshot's ``text``), and inline tables and sparklines.

The overview is drawn from the snapshot document alone, so it says nothing the card does not; the mod puts the
provenance line in front of it, and the whole stays within about 40 lines.
"""

from __future__ import annotations

import json
import math
from typing import Any

#: Sparkline glyphs, lowest to highest; :data:`NULL_GLYPH` marks a bucket with no value.
GLYPHS, NULL_GLYPH = "▁▂▃▄▅▆▇█", "·"
#: Longest overview line, issue title, warning and table cell; most step numbers listed per issue.
LINE_CHARS, TITLE_CHARS, WARNING_CHARS, CELL_CHARS = 240, 100, 80, 80
ISSUE_STEPS, TOP_HINTS, TOP_WARNINGS = 5, 3, 3


def _clip(text: str, limit: int) -> str:
    """*text* on one line, cut to *limit* characters."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _num(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return "?"
    return f"{value:,}" if isinstance(value, int) else f"{value:,.4g}" if abs(value) < 1000 else f"{value:,.0f}"


def _plural(count: Any, noun: str) -> str:
    return f"{_num(count)} {noun}{'' if count == 1 else 's'}"


def _duration(seconds: Any) -> str:
    if not isinstance(seconds, int | float):
        return "?"
    if seconds < 59.95:
        return f"{seconds:.1f}s"
    s = round(seconds)
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


def _health(snap: dict) -> str:
    pairs = [f"{h.get('metric')} {h.get('status') or '?'} ({h.get('label')})" for h in snap.get("health") or []]
    return "Health: " + (" · ".join(pairs) if pairs else "none")


def _numbers(snap: dict) -> str:
    counts, tokens = snap.get("counts") or {}, snap.get("tokens") or {}
    rate, timing = (snap.get("tools") or {}).get("success_rate"), snap.get("timing") or {}
    split = " · ".join(f"{name} {_num(tokens.get(key))}" for name, key in
                       (("fresh", "input"), ("cache read", "cache_read"), ("cache write", "cache_write"),
                        ("output", "output")))
    calls = _plural(counts.get("tool_calls"), "tool call") + (f" ({_num(rate)}% ok)" if rate is not None else "")
    return " · ".join([
        _plural(counts.get("assistant_steps"), "assistant step"), _plural(counts.get("prompts"), "prompt"),
        _plural(counts.get("agents"), "agent"), f"{_num(tokens.get('total'))} tokens ({split})", calls,
        f"wall clock {_duration(timing.get('wall_clock_s'))}",
    ])


def _context(snap: dict) -> str:
    ctx = snap.get("context") or {}
    pct, window = ctx.get("current_pct"), ctx.get("window")
    if pct is None or not window:
        line = "Context: ctx ?"
    else:
        assumed = " (assumed)" if ctx.get("window_source") == "default" else ""
        line = f"Context: {_num(pct)}% of {_num(window)}{assumed}"
    if ctx.get("window_note"):
        line += f" ({_clip(ctx['window_note'], LINE_CHARS)})"
    if ctx.get("peak_pct") is not None:
        line += f" · peak {_num(ctx['peak_pct'])}%"
    return line + f" · {_plural(ctx.get('compaction_count'), 'compaction')}"


def _steps(steps: list) -> str:
    shown = ", ".join(str(s) for s in steps[:ISSUE_STEPS])
    more = f" (+{len(steps) - ISSUE_STEPS} more)" if len(steps) > ISSUE_STEPS else ""
    return f"step{'' if len(steps) == 1 else 's'} {shown}{more}"


def _issues(snap: dict) -> list[str]:
    issues = snap.get("issues") or []
    if not issues:
        return ["Issues: none"]
    lines = ["Issues:"]
    for n, issue in enumerate(issues, 1):
        parts = [_clip(issue.get("title") or issue.get("kind") or "issue", TITLE_CHARS)]
        if issue.get("steps"):
            parts.append(_steps(issue["steps"]))
        if issue.get("evidence"):
            parts.append(issue["evidence"])
        lines.append(f"{n}. " + " — ".join(parts))
    return lines


def _bottlenecks(snap: dict) -> list[str]:
    rows = snap.get("bottlenecks") or []
    if not rows:
        return ["Bottlenecks: none"]
    return ["Bottlenecks:", *(f"- step {b.get('step')} ({_duration(b.get('duration_s'))}, {b.get('cause') or '?'}): "
                              f"{_clip(b.get('title') or '', TITLE_CHARS)}" for b in rows)]


def _hints(snap: dict) -> list[str]:
    hints: list[str] = []
    for issue in snap.get("issues") or []:
        why = " ".join(str(issue.get("why") or "").split())
        if why and why not in hints:
            hints.append(why)
    return ["Hints:", *(f"- {h}" for h in hints[:TOP_HINTS])] if hints else []


def _warnings(snap: dict) -> list[str]:
    warnings = list((snap.get("trajviz") or {}).get("warnings") or [])
    if not warnings:
        return []
    line = "Warnings: " + "; ".join(_clip(w, WARNING_CHARS) for w in warnings[:TOP_WARNINGS])
    return [line + (f" (+{len(warnings) - TOP_WARNINGS} more)" if len(warnings) > TOP_WARNINGS else "")]


def render_overview(snap: dict) -> str:
    """The compact markdown diagnosis of a snapshot: health, headline numbers, context, issues with their
    steps and evidence, bottlenecks, hints (the top issues' ``why``) and TrajViz's load warnings."""
    lines = [_health(snap), _numbers(snap), _context(snap), *_issues(snap), *_bottlenecks(snap), *_hints(snap),
             *_warnings(snap)]
    return "\n".join(_clip(line, LINE_CHARS) for line in lines)


def _cell(value: Any) -> str:
    if value is None:
        return ""
    text = json.dumps(value, ensure_ascii=False, default=str) if isinstance(value, dict | list) else str(value)
    return _clip(text, CELL_CHARS).replace("|", "\\|")


def render_table(rows: list[dict], *, limit: int, offset: int) -> str:
    """Rows ``offset`` to ``offset + limit`` as a markdown table over the union of their keys (first-seen order),
    nested values as JSON, then a ``rows N–M of T`` footer."""
    if limit < 1 or offset < 0:
        raise ValueError(f"limit must be positive and offset non-negative, not {limit} and {offset}")
    page = rows[offset:offset + limit]
    if not page:
        return f"rows 0 of {len(rows)}"
    keys = list(dict.fromkeys(k for row in page for k in row))
    lines = ["| " + " | ".join(_cell(k) for k in keys) + " |", "|" + "---|" * len(keys)]
    lines += ["| " + " | ".join(_cell(row.get(k)) for k in keys) + " |" for row in page]
    return "\n".join(lines) + f"\n\nrows {offset + 1}–{offset + len(page)} of {len(rows)}"


def render_series(values: list[float | None], *, width: int = 60) -> str:
    """A text sparkline of at most *width* glyphs (each the maximum of its bucket), scaled from the lowest value
    to the highest; ``·`` where a bucket holds no value."""
    from .snapshot import downsample  # snapshot imports this module

    clean = [v if isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v) else None
             for v in values]
    clean = downsample(clean, width)
    present = [v for v in clean if v is not None]
    if not present:
        return NULL_GLYPH * len(clean)
    low, span = min(present), max(present) - min(present)
    top = len(GLYPHS) - 1
    return "".join(NULL_GLYPH if v is None else GLYPHS[round((v - low) / span * top) if span else 0] for v in clean)
