"""``compare``, ``scorecard`` and ``labels``: TrajViz's two-run comparison, N-run scorecard and step-label views.

``compare`` runs ``tv.compare(reference=<other>, compared=<current>)``, the Insight dashboard's orientation
(``--reverse`` flips it), and writes every ``comparison_views()`` view to ``<artifacts>/compare/<other-stem>.json``.
``scorecard`` runs ``compare_runs()`` with the current session first (the baseline) unless ``--no-self`` and
writes every ``run_group_views()`` view to ``<artifacts>/compare/scorecard.json``. ``labels`` reads a labels
sidecar's views and, given a second sidecar, the phase comparison of the two. Each answers with a markdown
``text`` the mod prints.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

import agent_trajviz as tv
from agent_trajviz import Trajectory

from . import paths
from .cli import register
from .compat import EmptySession, load_plugins, load_run, protect_run_outputs, resolve_input_path
from .errors import BridgeError
from .outputs import protect_inputs, session_files
from .text import LINE_CHARS, _cell, _clip, _duration, _num
from .views_cmd import _artifacts, _load, _session_args, _write_json

#: Notes of a comparison shown in its text, and step numbers listed per divergence type.
TOP_NOTES, DIVERGENCE_STEPS = 3, 5

#: Scorecard columns drawn in the text: ``(row key, header)``.
_SCORE_COLUMNS = (("label", "run"), ("finished", "finished"), ("steps", "steps"), ("tokens", "tokens"),
                  ("wall_clock_fmt", "wall clock"), ("tool_calls", "tool calls"), ("tool_success_pct", "tool ok %"),
                  ("peak_pct", "peak %"), ("compactions", "compactions"))


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    return "\n".join(lines + ["| " + " | ".join(row) + " |" for row in rows])


def _yes_no(value: Any) -> str:
    return "?" if value is None else "yes" if value else "no"


def _views_file(path: Path, views: dict[str, tv.View], **extra: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(path, {**extra, "views": {vid: view.to_dict() for vid, view in views.items()}})
    return str(path)


# -- compare ---------------------------------------------------------------------

def _outcome_table(outcome: dict) -> str:
    def pair(key: str, render) -> list[str]:
        return [render(outcome.get(f"reference_{key}")), render(outcome.get(f"compared_{key}"))]

    return _md_table(["", "reference", "compared"], [
        ["finished", *pair("success", _yes_no)], ["steps", *pair("steps", _num)], ["tokens", *pair("tokens", _num)],
        ["wall clock", *pair("duration_s", _duration)], ["tool calls", *pair("tool_calls", _num)],
    ])


def _alignment(summary: dict) -> str:
    a = summary.get("alignment") or {}
    level = (summary.get("confidence") or {}).get("alignment")
    parts = [f"{name} {_num(a.get(key))}" for name, key in (
        ("F1", "alignment_f1"), ("recall", "reference_recall"), ("precision", "behavioral_precision"),
        ("overhead", "overhead_ratio"), ("harmful", "harmful_ratio"))]
    return f"Alignment{f' ({level})' if level else ''}: " + " · ".join(parts)


def _milestones(data: dict | None) -> str:
    ref, cmp_ = (data or {}).get("reference") or {}, (data or {}).get("compared") or {}

    def step(value: Any) -> str:
        return "–" if value is None else _num(value)

    parts = [f"{name.replace('_', ' ')} {step(ref.get(name))} → {step(cmp_.get(name))}"
             for name in dict.fromkeys([*ref, *cmp_])]
    return "Milestones (step, reference → compared): " + (" · ".join(parts) if parts else "none")


def _divergence(points: list | None) -> str:
    points = [p for p in points or [] if isinstance(p, dict)]
    steps: dict[str, list[int]] = {}
    for p in points:
        steps.setdefault(str(p.get("type")), []).extend(s for s in p.get("steps") or [] if isinstance(s, int))
    counts = Counter(str(p.get("type")) for p in points)
    parts = []
    for kind, count in counts.most_common():
        listed = sorted(set(steps[kind]))
        more = ", …" if len(listed) > DIVERGENCE_STEPS else ""
        noun = "step" if len(listed) == 1 else "steps"
        where = f" ({noun} {', '.join(map(str, listed[:DIVERGENCE_STEPS]))}{more})" if listed else ""
        parts.append(f"{kind} ×{count}{where}")
    return f"Divergence points ({len(points)}): " + (" · ".join(parts) if parts else "none")


def _compare_text(views: dict[str, tv.View]) -> str:
    summary = views["comparison_summary"].data
    outcome = summary.get("outcome") or {}
    head = (f"Comparison: reference `{outcome.get('reference_filename')}` → compared "
            f"`{outcome.get('compared_filename')}` (anchor: {summary.get('anchor_mode')}, "
            f"evidence: {summary.get('evidence_level')})")
    lines = [_alignment(summary), _milestones(views["milestone_timeline"].data),
             _divergence(views["divergence"].data)]
    notes = [f"- {_clip(note, LINE_CHARS)}" for note in (summary.get("notes") or [])[:TOP_NOTES]]
    return "\n".join([_clip(head, LINE_CHARS), "", _outcome_table(outcome), "",
                      *(_clip(line, LINE_CHARS) for line in lines), *(["Notes:", *notes] if notes else [])])


def compare_sessions(current: Trajectory, other_path: str, *, anchor_patch: str | None, fuzzy: bool, reverse: bool,
                     out_dir: Path) -> dict:
    """*current* against the run at *other_path*: ``{"path", "orientation", "summary", "text"}``.

    The reference is the other run and the compared run the current one (swapped with *reverse*). Every
    ``comparison_views()`` view goes to ``out_dir/compare/<other-stem>.json`` beside the orientation; a missing
    *anchor_patch* raises ``BridgeError("not_found")``.
    """
    if anchor_patch is not None and not Path(anchor_patch).is_file():
        raise BridgeError("not_found", f"No such anchor patch: {anchor_patch}", path=anchor_patch)
    other = load_run(other_path)
    destination = out_dir / "compare" / f"{Path(other_path).stem}.json"
    protect_run_outputs(current, [destination])
    protect_run_outputs(other, [destination])
    if anchor_patch is not None:
        protect_inputs([destination], [Path(anchor_patch)])
    (reference, ref_path), (compared, cmp_path) = (other, other_path), (current, current.source)
    if reverse:
        (reference, ref_path), (compared, cmp_path) = (compared, cmp_path), (reference, ref_path)
    report = tv.compare(reference=reference, compared=compared, fuzzy_commands=fuzzy, anchor_patch=anchor_patch)
    views = tv.comparison_views(report)
    orientation = {"reference": ref_path, "compared": cmp_path}
    path = _views_file(destination, views, orientation=orientation)
    return {"path": path, "orientation": orientation, "summary": views["comparison_summary"].data,
            "text": _compare_text(views)}


# -- scorecard -------------------------------------------------------------------

def _scorecard_text(views: dict[str, tv.View]) -> str:
    rows, behavior = views["run_scorecard"].data, views["run_behavior"].data
    columns = [*_SCORE_COLUMNS, *((("error", "error"),) if any(r.get("error") for r in rows) else ())]
    table = _md_table([header for _, header in columns], [
        [_yes_no(r.get(key)) if key == "finished" else
         _num(r.get(key)) if isinstance(r.get(key), int | float) else _cell(r.get(key)) for key, _ in columns]
        for r in rows])
    labels = behavior.get("labels") or {}
    baseline = behavior.get("baseline_run_id")
    similar = (behavior.get("similarity") or {}).get(baseline) or {}
    lines = [table, ""]
    if baseline is not None:
        name = labels.get(baseline, baseline)
        pairs = [f"{labels.get(rid, rid)} {_num(score)}" for rid, score in similar.items() if rid != baseline]
        lines.append(f"Similarity to baseline {name}: " + (" · ".join(pairs) if pairs else "none"))
    for rid, patterns in (behavior.get("patterns_vs_baseline") or {}).items():
        if patterns:
            found = ", ".join(f"{p.get('label') or p.get('type')} ×{_num(p.get('count'))}" for p in patterns)
            lines.append(f"Patterns vs baseline, {labels.get(rid, rid)}: {found}")
    return "\n".join(_clip(line, LINE_CHARS) if "\n" not in line else line for line in lines).rstrip()


def scorecard(current: Trajectory | None, others: list[str], *, fuzzy: bool, labels: list[str] | None, no_self: bool,
              out_dir: Path) -> dict:
    """``compare_runs()`` over *current* (first, the baseline; left out with *no_self*, when *current* may be
    ``None``) and *others*: ``{"path", "rows", "text"}``.

    Every ``run_group_views()`` view goes to ``out_dir/compare/scorecard.json``. A file TrajViz cannot read
    is a row with its ``error``; fewer than two runs raise ``BridgeError("compare_failed")``.
    """
    load_plugins()  # TrajViz loads the others itself (and with --no-self nothing went through load_run)
    sources: list[Trajectory | str] = [] if no_self else [current]
    destination = out_dir / "compare" / "scorecard.json"
    if current is not None:
        protect_run_outputs(current, [destination])
    for other in others:
        protect_inputs([destination], session_files(resolve_input_path(other)))
        try:
            loaded = load_run(other)
        except tv.LoadError:
            # The group API preserves unreadable runs as error rows.
            sources.append(other)
        else:
            protect_run_outputs(loaded, [destination])
            sources.append(loaded)
    try:
        result = tv.compare_runs(sources, labels=labels, fuzzy_commands=fuzzy)
    except ValueError as exc:
        raise BridgeError("compare_failed", str(exc)) from exc
    views = tv.run_group_views(result)
    path = _views_file(destination, views)
    return {"path": path, "rows": views["run_scorecard"].data, "text": _scorecard_text(views)}


# -- labels ----------------------------------------------------------------------

def _phases(counts: dict, durations: dict) -> dict:
    return {"counts": counts["counts"], "durations_s": durations["durations_s"]}


def _labels_text(summary: dict, phases: dict, comparison: dict | None) -> str:
    head = (f"Labels: {_num(summary.get('total'))} steps · {_num(summary.get('classified'))} classified "
            f"({_num(summary.get('classification_rate'))}%) · {_num(summary.get('unknown'))} unknown · "
            f"model {summary.get('model')} · taxonomy {summary.get('taxonomy_version')}")
    counts, durations = phases["counts"], phases["durations_s"]
    lines = [head, "", _md_table(["phase", "steps", "time"], [
        [_cell(name), _num(counts.get(name, 0)), _duration(durations.get(name))]
        for name in dict.fromkeys([*counts, *durations])])]
    if comparison is not None:
        ref, cmp_ = comparison["reference"], comparison["compared"]
        names = dict.fromkeys([*ref["counts"], *cmp_["counts"], *ref["durations_s"], *cmp_["durations_s"]])
        lines += ["", "Phases compared (reference: the other sidecar; compared: this one)", "",
                  _md_table(["phase", "reference steps", "compared steps", "reference time", "compared time"], [
                      [_cell(name), _num(ref["counts"].get(name, 0)), _num(cmp_["counts"].get(name, 0)),
                       _duration(ref["durations_s"].get(name, 0)), _duration(cmp_["durations_s"].get(name, 0))]
                      for name in names])]
    return "\n".join(lines)


def label_views_payload(sidecar: str, other: str | None) -> dict:
    """The label views of *sidecar*: ``{"summary", "phases", "comparison"?, "text"}``.

    ``phases`` is ``{"counts", "durations_s"}`` per phase. With *other*, ``comparison`` is
    ``label_comparison_views(other, sidecar)`` (the other sidecar the reference) as
    ``{"reference", "compared"}``, each ``{"name", "counts", "durations_s"}``. A file that is not a labels
    sidecar raises TrajViz's ``LoadError`` (code ``invalid_labels``).
    """
    views = tv.label_views(sidecar)
    summary = views["labels_summary"].data
    phases = _phases(views["label_phase_counts"].data, views["label_phase_durations"].data)
    out: dict[str, Any] = {"summary": summary, "phases": phases}
    if other is not None:
        pair = tv.label_comparison_views(other, sidecar)
        counts, durations = pair["label_phase_count_comparison"].data, pair["label_phase_duration_comparison"].data
        out["comparison"] = {side: {"name": counts[side]["name"], **_phases(counts[side], durations[side])}
                             for side in ("reference", "compared")}
        views.update(pair)
    out["views"] = {vid: view.to_dict() for vid, view in views.items()}
    out["text"] = _labels_text(summary, phases, out.get("comparison"))
    return out


def _label_sources(sidecar: Path) -> list[Path]:
    """Inputs label aggregation may read: the sidecar and its optional legacy trajectory fallback."""
    sources = [sidecar]
    document = json.loads(sidecar.read_text(encoding="utf-8"))
    trajectory = document.get("trajectory_file")
    if not trajectory or any(isinstance(step, dict) and step.get("role") == "user"
                             for step in document["steps"]):
        return sources
    candidate = Path(trajectory)
    if not candidate.is_file():
        candidate = sidecar.absolute().parent / candidate.name
    if candidate.is_file():
        sources.extend(session_files(candidate.resolve()))
    return sources


# -- subcommands -----------------------------------------------------------------

def _labels(text: str) -> list[str]:
    return [part.strip() for part in text.split(",")]


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("compare", help="The current session against another run (TrajViz's compare).")
    _session_args(p)
    p.add_argument("--other", required=True, help="The other run (the reference unless --reverse).")
    p.add_argument("--anchor-patch", default=None, metavar="P", help="A unified diff of the intended change.")
    p.add_argument("--fuzzy-commands", action="store_true", help="Match shell commands loosely.")
    p.add_argument("--reverse", action="store_true", help="The current session as the reference instead.")

    p = sub.add_parser("scorecard", help="A scorecard of the current session (the baseline) and other runs.")
    _session_args(p)
    p.add_argument("--others", required=True, nargs="+", metavar="P", help="The other runs.")
    p.add_argument("--fuzzy-commands", action="store_true", help="Match shell commands loosely.")
    p.add_argument("--labels", type=_labels, default=None, metavar="A,B", help="Run names, in order.")
    p.add_argument("--no-self", action="store_true", help="Leave the current session out (the first other leads).")

    p = sub.add_parser("labels", help="The label views of a step-labels sidecar, or two compared.")
    p.add_argument("--sidecar", required=True, help="The labels sidecar.")
    p.add_argument("--other", default=None, help="A second sidecar, the reference of the comparison.")
    p.add_argument("--artifacts", type=Path, default=None, help="The folder for the complete labels view JSON.")


@register("compare")
def _compare(args: argparse.Namespace) -> dict:
    try:
        run = _load(args)
    except EmptySession as exc:
        return {"empty": True, "reason": exc.reason}
    return compare_sessions(run, args.other, anchor_patch=args.anchor_patch, fuzzy=args.fuzzy_commands,
                            reverse=args.reverse, out_dir=_artifacts(args))


@register("scorecard")
def _scorecard(args: argparse.Namespace) -> dict:
    run = None
    if not args.no_self:
        try:
            run = _load(args)
        except EmptySession as exc:
            return {"empty": True, "reason": exc.reason}
    return scorecard(run, args.others, fuzzy=args.fuzzy_commands, labels=args.labels, no_self=args.no_self,
                     out_dir=_artifacts(args))


@register("labels")
def _labels_cmd(args: argparse.Namespace) -> dict:
    result = label_views_payload(args.sidecar, args.other)
    sidecars = [Path(args.sidecar), *([Path(args.other)] if args.other is not None else [])]
    identity = hashlib.sha256("\0".join(str(path.resolve()) for path in sidecars).encode()).hexdigest()[:24]
    folder = args.artifacts if args.artifacts is not None else (
        paths.cache_root(os.environ) / "trajviz-obs" / "label-views")
    destination = folder / f"labels-{identity}.json"
    protect_inputs([destination], [path for sidecar in sidecars for path in _label_sources(sidecar)])
    folder.mkdir(parents=True, exist_ok=True)
    _write_json(destination, {"views": result.pop("views")})
    result["path"] = str(destination)
    result["text"] += f"\nLabel views written to {destination}."
    return result
