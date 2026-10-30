"""The snapshot document: what the overview card, the pane and the status line draw, in one bounded JSON object.

Every number comes from a TrajViz view (computed with ``run.views(only=VIEWS)``) except the enrichments the
bridge reads itself (title, prompts, sub-agent names), which sit beside them. Series are downsampled to
``max_points`` (``--max-points`` takes at most :data:`MAX_POINTS`) and tables cut to their top N, so the document
stays small on any session.
"""

from __future__ import annotations

import argparse
import math
import os
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_trajviz import Trajectory

from .cli import register
from .compat import TRAJVIZ_VERSION, EmptySession, input_path, load_run, window_info
from .sessions import _positive_int
from .sidecars import AgentMeta, TranscriptFacts, agent_meta, scan_transcript
from .text import render_overview

SCHEMA_VERSION = 1

#: The TrajViz views the snapshot draws from; nothing else is computed.
VIEWS = (
    "summary", "session", "metrics", "health", "issues", "bottlenecks", "context_pressure", "token_usage",
    "step_durations", "phases", "agents", "agent_swimlane", "tool_outcomes", "workflow", "diagnostics",
    "file_interactions", "plan", "skill_calls", "tool_sequences", "failure_patterns", "error_clusters",
    "failure_chains", "fruitless_streaks", "tool_selection", "edit_thrash", "repeated_searches",
    "phase_regressions", "premature_compactions", "hotspots",
)

#: Pattern views copied into ``patterns`` (each list cut to :data:`TOP_PATTERNS`; ``hotspots`` to :data:`TOP_HOTSPOTS`).
PATTERN_VIEWS = (
    "tool_sequences", "failure_patterns", "error_clusters", "failure_chains", "fruitless_streaks", "tool_selection",
    "edit_thrash", "repeated_searches", "phase_regressions", "premature_compactions",
)

TOP_TOOLS, TOP_ISSUES, TOP_BOTTLENECKS, TOP_PATTERNS, TOP_HOTSPOTS, TOP_FILES = 12, 10, 5, 8, 5, 8
TIMELINE_STEPS = 60
#: Longest step list kept inside one issue or pattern row.
ROW_STEPS = 50
#: Longest agent label taken from a ``.meta.json`` description, and longest timeline preview.
LABEL_CHARS, PREVIEW_CHARS, EVIDENCE_CHARS = 40, 120, 80
#: Text copied from engine views is a display preview. Commands and errors can be megabytes long.
DISPLAY_TEXT_CHARS = 512
#: Tool-call input keys tried, in order, for an issue's evidence excerpt.
EVIDENCE_KEYS = ("command", "file_path", "path", "notebook_path", "pattern", "url", "query")
#: Segment budget shared by the swimlane's lanes, as a multiple of ``max_points``.
LANE_BUDGET = 4
#: Largest ``--max-points``, which bounds every series, the phase band and each swimlane lane.
MAX_POINTS = 2000


def downsample(values: list[float | None], n: int) -> list[float | None]:
    """*values* in at most *n* equal buckets, each its maximum (``None`` when it holds only ``None``).

    Applied to a list of step numbers, it gives the last step of each bucket: the index list that goes
    alongside the downsampled values.
    """
    if n < 1:
        raise ValueError(f"n must be positive, not {n}")
    if len(values) <= n:
        return list(values)
    size = math.ceil(len(values) / n)
    out: list[float | None] = []
    for start in range(0, len(values), size):
        present = [v for v in values[start:start + size] if v is not None]
        out.append(max(present) if present else None)
    return out


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _transcript(path: str, session_id: str | None, sha256: str | None) -> dict[str, Any]:
    try:
        st = os.stat(path)
        size, mtime_ms = st.st_size, st.st_mtime_ns // 1_000_000
    except OSError:
        size = mtime_ms = None
    return {"path": path, "session_id": session_id or Path(path).stem, "size": size, "mtime_ms": mtime_ms,
            "sha256": sha256}


def empty_snapshot(reason: str, transcript_path: str) -> dict:
    """The document for a session with nothing to analyze yet (no assistant step)."""
    return {"schema_version": SCHEMA_VERSION, "empty": True, "reason": reason,
            "transcript": _transcript(transcript_path, None, None)}


def _evidence(run: Trajectory, steps: list, *, kind: str = "", title: str = "") -> str:
    """``"<tool>: <command or path excerpt>"`` of the first step's first tool call; ``""`` when there is none."""
    index = steps[0] if steps else None
    if not isinstance(index, int) or not 0 <= index < len(run.steps):
        return ""
    calls = run.steps[index].get("tool_calls") or []
    if not calls or not isinstance(calls[0], dict):
        return ""
    call = calls[0]
    if kind == "error":
        from agent_trajviz.insight.shell_cmd import tool_chart_name
        from agent_trajviz.insight.tool_failure import tool_call_failed

        failed = [c for c in calls if isinstance(c, dict) and tool_call_failed(c)]
        if failed:
            call = next((c for c in failed if title.startswith(tool_chart_name(c) + ":")), failed[0])
    tool = str(call.get("tool_name") or "tool")
    params = call.get("input") if isinstance(call.get("input"), dict) else {}
    excerpt = next((params[k] for k in EVIDENCE_KEYS if isinstance(params.get(k), str) and params[k].strip()), "")
    excerpt = " ".join(excerpt.split())
    return f"{tool}: {_clip(excerpt, EVIDENCE_CHARS)}" if excerpt else tool


def _row(row: Any) -> Any:
    """A pattern or issue row with every list inside it cut to :data:`ROW_STEPS`."""
    if not isinstance(row, dict):
        return row
    return {k: v[:ROW_STEPS] if isinstance(v, list) else v for k, v in row.items()}


def _top(data: Any, n: int) -> Any:
    """A list view's first *n* rows; for a dict view, each of its lists cut the same way."""
    if isinstance(data, list):
        return [_row(r) for r in data[:n]]
    if isinstance(data, dict):
        return {k: [_row(r) for r in v[:n]] if isinstance(v, list) else v for k, v in data.items()}
    return data


def _label(agent_id: str, label: str, meta: AgentMeta | None) -> str:
    """TrajViz's label, or the ``.meta.json`` description when TrajViz has only the (possibly clipped) raw id."""
    if not (agent_id and meta and meta.description):
        return label
    stem = label.removesuffix("…")
    if label == agent_id or (stem != label and stem and agent_id.startswith(stem)):
        return _clip(meta.description, LABEL_CHARS)
    return label


def _merge_segments(segments: list[dict], cap: int) -> list[dict]:
    """``{start, end}`` per segment; above *cap*, consecutive segments are joined into *cap* or fewer spans."""
    spans = [{"start": s["start"], "end": s["end"]} for s in segments]
    if len(spans) <= cap:
        return spans
    size = math.ceil(len(spans) / cap)
    return [{"start": spans[i]["start"], "end": spans[min(i + size, len(spans)) - 1]["end"]}
            for i in range(0, len(spans), size)]


def _phase_band(phases: list[dict], n: int) -> list[dict]:
    """``{name, start, end}`` per phase; above *n* phases, the dominant phase of each of *n* step buckets."""
    rows = [{"name": p["name"], "start": p["start_idx"], "end": p["end_idx"]} for p in phases]
    if len(rows) <= n:
        return rows
    low, high = rows[0]["start"], max(r["end"] for r in rows)
    size = math.ceil((high - low + 1) / n)
    band: list[dict] = []
    first = 0
    for start in range(low, high + 1, size):
        end = min(start + size - 1, high)
        while first < len(rows) and rows[first]["end"] < start:
            first += 1
        weight: Counter[str] = Counter()
        for r in rows[first:]:
            if r["start"] > end:
                break
            weight[r["name"]] += min(r["end"], end) - max(r["start"], start) + 1
        if not weight:
            continue
        name = weight.most_common(1)[0][0]
        if band and band[-1]["name"] == name and band[-1]["end"] == start - 1:
            band[-1]["end"] = end
        else:
            band.append({"name": name, "start": start, "end": end})
    return band


def _context(run: Trajectory, pressure: dict, n: int) -> tuple[dict, dict]:
    """The ``context`` block and the ``series.context`` block (the main agent's occupancy by step)."""
    stats = pressure["stats"]
    metadata = run.raw.get("metadata", {})
    root_session = metadata.get("session_id") if isinstance(metadata, dict) else None
    agents = pressure["series"]["agents"]
    main = next((a for a in agents if a.get("key") == "__main__"), None)
    if main is None and root_session:
        main = next((a for a in agents if a.get("agent_id") == root_session), None)
    points = main["points"] if main else []
    window = window_info(run)
    current = points[-1]["occupancy"] if points else None
    from .opencode import NATIVE_KEY

    if NATIVE_KEY in run.raw:
        latest = next((s for s in reversed(run.steps) if s.get("session_id") == root_session
                       and s.get("role") in {"assistant", "compaction"}), None)
        # Compaction usage bills the summary request, not its resulting window.
        # An interrupted response may likewise leave no measured prompt usage.
        if latest and (latest.get("role") == "compaction" or latest.get("_metrics_unavailable_fields")):
            current = None
        window["window_note"] = ((window.get("window_note") or "") + " Latest logged model request; "
                                 "post-compaction occupancy is unknown until another measured response.").strip()
    pct = round(current / window["window"] * 100, 1) if current is not None and window["window"] else None
    context = {**window, "current_occupancy": current, "current_pct": pct,
               "peak_occupancy": stats.get("peak_occupancy"), "peak_pct": stats.get("peak_pct"),
               "compaction_count": stats.get("compaction_count"), "largest_drop": stats.get("largest_drop")}
    series = {"turns": downsample([p["step"] for p in points], n),
              "occupancy": downsample([p["occupancy"] for p in points], n),
              "compactions": [c["step"] for c in (main["compactions"] if main else [])][:n]}
    return context, series


def _by_tool(calls: list[dict]) -> list[dict]:
    counts: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    for call in calls:
        counts[call["tool"]] += 1
        errors[call["tool"]] += not call.get("ok", True)
    ranked = sorted(counts, key=lambda tool: (-counts[tool], tool))[:TOP_TOOLS]
    return [{"tool": tool, "calls": counts[tool], "errors": errors[tool]} for tool in ranked]


def _files(data: dict) -> dict:
    reads: Counter[str] = Counter()
    writes: Counter[str] = Counter()
    kinds: Counter[str] = Counter()
    for item in data["interactions"]:
        kinds[item["type"]] += 1
        if item["type"] == "read":
            reads[item["path"]] += 1
        elif item["type"] == "write":
            writes[item["path"]] += 1
    touched = sorted(set(reads) | set(writes), key=lambda f: (-(reads[f] + writes[f]), f))[:TOP_FILES]
    return {"unique": len({i["path"] for i in data["interactions"] if i.get("path")}), "reads": kinds["read"],
            "writes": kinds["write"], "searches": kinds["search"],
            "top": [{"file": f, "reads": reads[f], "writes": writes[f]} for f in touched]}


def _skills(links: list[dict]) -> list[dict]:
    calls: Counter[str] = Counter()
    for link in links:
        calls[link["skill"]] += link["calls"]
    return [{"skill": s, "calls": c} for s, c in sorted(calls.items(), key=lambda item: (-item[1], item[0]))]


def _timeline(run: Trajectory, workflow: list[dict]) -> list[dict]:
    rows = []
    for w in workflow[-TIMELINE_STEPS:]:
        step = run.steps[w["step"]] if 0 <= w["step"] < len(run.steps) else {}
        rows.append({"step": w["step"], "role": w["role"], "category": w["category"], "agent": w["agent"],
                     "errors": step.get("error_count", 0), "tool_calls": w["tool_calls"],
                     "duration_s": w["duration_s"], "tokens": w["tokens"],
                     "preview": _clip(" ".join(str(step.get("text_preview") or "").split()), PREVIEW_CHARS)})
    return rows


def _artifacts(folder: Path | None) -> dict:
    def existing(path: Path | None) -> str | None:
        return str(path) if path is not None and path.exists() else None

    root = folder if folder is not None and folder.is_dir() else None
    return {"dir": existing(root), "views": existing(root and root / "views"), "labels": existing(root and root / "labels.json")}


def _display_previews(value: Any, path: tuple[str | int, ...], clipped: list[str]) -> Any:
    """Bound display text recursively without changing numbers, keys, routing ids or provenance paths.

    A native failed Bash command can become a tool label and a pattern signature. Limiting only the number
    of rows therefore leaves several copies of an arbitrarily large command in a small snapshot. Keep the
    full data in the engine/export; this makes independent preview copies for the card and pane.
    """
    if isinstance(value, str):
        if (path[0] in {"transcript", "artifacts"}
                or path[-1] in {"id", "agent_id", "session_id", "sha256"}):
            return value
        if len(value) > DISPLAY_TEXT_CHARS:
            clipped.append(".".join(str(part)[:80] for part in path))
            return _clip(value, DISPLAY_TEXT_CHARS)
        return value
    if isinstance(value, dict):
        return {key: _display_previews(item, (*path, key), clipped) for key, item in value.items()}
    if isinstance(value, list):
        return [_display_previews(item, (*path, index), clipped) for index, item in enumerate(value)]
    return value


def build_snapshot(run: Trajectory, *, transcript_path: str, facts: TranscriptFacts,
                   agents_meta: dict[str, AgentMeta], artifacts: Path | None, max_points: int = 400,
                   duration_ms: int = 0) -> dict:
    """The snapshot document for *run* (see the contract in the bridge plan, Task 5).

    *duration_ms* is the time already spent (loading); the document's ``duration_ms`` adds this call's own.
    """
    started = time.monotonic()
    views = {view_id: view.data for view_id, view in run.views(only=VIEWS).items()}
    summary, session, metrics = views["summary"], views["session"], views["metrics"]
    pressure = views["context_pressure"]
    context, context_series = _context(run, pressure, max_points)
    tokens, durations = views["token_usage"]["steps"], views["step_durations"]["steps"]
    agents = views["agents"]
    lanes = views["agent_swimlane"]["lanes"]
    lane_cap = min(max_points, max(10, LANE_BUDGET * max_points // max(1, len(lanes))))
    plan = views["plan"]["metrics"]
    snap = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "duration_ms": duration_ms,
        "transcript": _transcript(transcript_path, session.get("session_id"), run.sha256),
        "trajviz": {"version": TRAJVIZ_VERSION, "format": run.format, "format_label": run.format_label,
                    "warnings": list(run.warnings)},
        "title": facts.title or session.get("title"),
        "task": facts.prompts[0] if facts.prompts else None,
        "counts": {"records": summary["steps"], "assistant_steps": summary["assistant_steps"],
                   "user_records": metrics.get("user_steps"), "prompts": facts.prompt_count,
                   "compactions": pressure["stats"].get("compaction_count"), "agents": summary["agents"],
                   "tool_calls": summary["tool_calls"], "issues": len(views["issues"])},
        "tokens": {k: summary["tokens"].get(k) for k in
                   ("total", "input", "output", "cache_read", "cache_write", "reasoning")},
        "timing": {"wall_clock_s": summary.get("wall_clock_s"), "started_at": session.get("started_at"),
                   "finished_at": session.get("finished_at"), "avg_step_s": metrics.get("avg_duration"),
                   "p95_step_s": metrics.get("p95_duration"),
                   "output_tokens_per_sec": metrics.get("output_tokens_per_sec"),
                   "tool_wait_share": metrics.get("tool_wait_share")},
        "tools": {"success_rate": summary.get("tool_success_rate"), "calls": summary["tool_calls"],
                  "failed": metrics.get("tool_fail"), "by_tool": _by_tool(views["tool_outcomes"]["calls"])},
        "health": [{k: h.get(k) for k in ("metric", "status", "label", "detail")} for h in views["health"]],
        "issues": [{"kind": i.get("kind"), "title": i.get("title"), "detail": i.get("detail"), "why": i.get("why"),
                    "steps": list(i.get("steps") or [])[:ROW_STEPS], "evidence": _evidence(
                        run, i.get("steps") or [], kind=i.get("kind", ""), title=i.get("title", ""))}
                   for i in views["issues"][:TOP_ISSUES]],
        "bottlenecks": [{"step": b.get("step_idx"), "duration_s": b.get("duration"), "cause": b.get("cause"),
                         "title": b.get("title"), "detail": b.get("detail")}
                        for b in views["bottlenecks"][:TOP_BOTTLENECKS]],
        "context": context,
        "series": {
            "tokens": {"steps": downsample([t["step"] for t in tokens], max_points),
                       "fresh": downsample([t["fresh_input"] for t in tokens], max_points),
                       "cache_read": downsample([t["cache_read"] for t in tokens], max_points),
                       "cache_write": downsample([t["cache_write"] for t in tokens], max_points),
                       "output": downsample([t["output"] for t in tokens], max_points)},
            "durations": {"steps": downsample([d["step"] for d in durations], max_points),
                          "seconds": downsample([d["duration_s"] for d in durations], max_points),
                          "outliers": [d["step"] for d in durations if d.get("outlier")][:max_points]},
            "context": context_series,
        },
        "phases": _phase_band(views["phases"], max_points),
        "agents": [{"id": a["agent_id"], "label": _label(a["agent_id"], a["label"], agents_meta.get(a["agent_id"])),
                    "type": (m.agent_type or None) if (m := agents_meta.get(a["agent_id"])) else None,
                    "description": (m.description or None) if m else None,
                    "steps": a["step_count"], "tokens": a["total_tokens"], "tool_calls": a["tool_call_count"],
                    "errors": a["error_count"], "duration_s": a["total_duration_s"],
                    "cache_pct": a["cache_efficiency_pct"], "gen_tps": a["output_tokens_per_sec"]}
                   for a in agents],
        "swimlane": {"total_steps": summary["steps"],
                     "lanes": [{"id": lane["id"], "label": _label(lane["id"], lane["label"], agents_meta.get(lane["id"])),
                                "segments": _merge_segments(lane["segments"], lane_cap)} for lane in lanes]},
        "patterns": {**{name: _top(views[name], TOP_PATTERNS) for name in PATTERN_VIEWS},
                     "hotspots": _top(views["hotspots"], TOP_HOTSPOTS)},
        "timeline": _timeline(run, views["workflow"]["steps"]),
        "diagnostics": views["diagnostics"],
        "files": _files(views["file_interactions"]),
        "plan": {"items": plan["total_items"], "done": sum(1 for i in plan["items"] if i.get("end_step") is not None),
                 "stalled": len(plan["stalled"])},
        "skills": _skills(views["skill_calls"]["links"]),
        "artifacts": _artifacts(artifacts),
        "text": "",
    }
    clipped: list[str] = []
    snap = _display_previews(snap, (), clipped)
    if clipped:
        fields = ", ".join(clipped[:4]) + (f" (+{len(clipped) - 4} more)" if len(clipped) > 4 else "")
        snap["trajviz"]["warnings"].append(
            f"Snapshot preview text clipped; full data: /obs view or /obs json. Fields: {fields}.")
    snap["text"] = render_overview(snap)
    snap["duration_ms"] = duration_ms + int((time.monotonic() - started) * 1000)
    return snap


def _points_limit(text: str) -> int:
    """``--max-points`` (an argparse type): a whole number from 1 to :data:`MAX_POINTS`."""
    try:
        value = int(text)
    except ValueError:
        value = 0
    if not 1 <= value <= MAX_POINTS:
        raise argparse.ArgumentTypeError(f"expected a whole number from 1 to {MAX_POINTS}, got {text!r}")
    return value


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("snapshot", help="The snapshot document the overview card, pane and status line draw.")
    p.add_argument("--transcript", required=True, help="The session transcript (or session directory).")
    p.add_argument("--window", type=_positive_int, default=None,
                   help="The real context window in tokens (default: TrajViz's own estimate).")
    p.add_argument("--artifacts", type=Path, default=None, help="The session's artifact folder (read, never created).")
    p.add_argument("--max-points", type=_points_limit, default=400,
                   help=f"Longest series kept (default: 400, at most {MAX_POINTS}).")
    p.add_argument("--kind", choices=("current", "other"), default="other",
                   help="'current' (the session being written) answers {\"empty\": true} before its first step.")


@register("snapshot")
def _snapshot(args: argparse.Namespace) -> dict:
    started = time.monotonic()
    try:
        run = load_run(args.transcript, window_limit=args.window, current=args.kind == "current")
    except EmptySession as exc:
        return empty_snapshot(exc.reason, args.transcript)
    source = input_path(run)
    is_file = source is not None and source.is_file()
    if run.format == "claude_code" and is_file:
        facts, meta = scan_transcript(str(source)), agent_meta(str(source))
    else:
        prompts = [str(step.get("text_preview") or "") for step in run.steps
                   if step.get("role") == "user" and not step.get("is_sub_agent")]
        metadata = run.raw.get("metadata", {})
        facts = TranscriptFacts(metadata.get("title") or None, [p[:200] for p in prompts[:20]], len(prompts), 0)
        meta = {}
    return build_snapshot(run, transcript_path=args.transcript, facts=facts, agents_meta=meta,
                          artifacts=args.artifacts, max_points=args.max_points,
                          duration_ms=int((time.monotonic() - started) * 1000))
