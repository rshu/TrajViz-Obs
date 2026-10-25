"""``view``, ``json`` and ``schemas``: TrajViz's views drawn inline, exported as files, and their JSON Schemas.

``view`` always writes the full view to ``<out_dir>/<id>.json`` (the layout :func:`agent_trajviz.export_json`
writes) and prints only a page of it: a markdown table for table and summary views, text sparklines or bars
for the charts that have them, else the chart's rows as a table; with ``--json``, the data itself when the file
is at most :data:`INLINE_JSON_BYTES`. A page is at most ``MAX_LIMIT`` rows, and the inline part takes at most
:data:`INLINE_STDOUT_BYTES` on stdout: a longer page is cut, with a last line naming how many rows were cut.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
from collections.abc import Callable
from operator import itemgetter
from pathlib import Path
from typing import Any

import agent_trajviz as tv
from agent_trajviz import Trajectory

from . import paths
from .cli import register
from .compat import EmptySession, load_plugins, load_run, protect_run_outputs
from .errors import BridgeError
from .outputs import staged_path
from .sessions import MAX_LIMIT, _positive_int, page_limit
from .text import _clip, _num, render_series, render_table

#: Largest view file whose data ``--json`` puts inline (the output-size rule for stdout).
INLINE_JSON_BYTES = 256 * 1024
#: Most bytes the inline part (``table``, ``series`` or ``data``) takes in the stdout JSON, escapes included; the
#: rest of the document is small, so the answer stays under the 256 KB output rule.
INLINE_STDOUT_BYTES = 200 * 1024
#: Longest bar, and longest bar label.
BAR_WIDTH, LABEL_CHARS = 30, 40

#: How each dict-shaped view becomes the rows a table pages through (a list view is its own rows; any other
#: dict, a summary among them, is one ``{key, value}`` row per key).
_ROWS: dict[str, Callable[[Any], list]] = {
    "hotspots": lambda d: [{"list": name, **row} for name, rows in d.items() for row in rows],
    "plan": lambda d: d["metrics"]["items"],
    "context_usage": lambda d: [{"bucket": k, "tokens": v} for k, v in d["buckets"].items()],
    "context_pressure": lambda d: [{"agent": a["label"], **p} for a in d["series"]["agents"] for p in a["points"]],
    **{vid: itemgetter(key) for vid, key in {
        "workflow": "steps", "failure_chains": "chains", "error_clusters": "clusters", "token_usage": "steps",
        "step_durations": "steps", "tool_usage": "tools", "tool_durations": "tools", "tool_outcomes": "calls",
        "skill_calls": "links", "agent_tokens": "agents", "agent_swimlane": "lanes",
        "file_interactions": "interactions"}.items()},
}

#: Charts drawn as sparklines: ``(group key or None, fields)``, one line per field (per group).
_SPARKS: dict[str, tuple[str | None, tuple[str, ...]]] = {
    "token_usage": (None, ("fresh_input", "cache_read", "cache_write", "output", "reasoning", "total")),
    "step_durations": (None, ("duration_s",)),
    "context_pressure": ("agent", ("occupancy",)),
}

#: Charts drawn as bars: ``(label key, value key)`` over their rows.
_BARS: dict[str, tuple[str, str]] = {
    "tool_usage": ("tool", "calls"),
    "tool_durations": ("tool", "total_s"),
    "agent_tokens": ("label", "total"),
    "context_usage": ("bucket", "tokens"),
}


def catalog() -> list[dict]:
    """Every single-run view, in TrajViz's order (plugins' views included): ``{id, kind, title, description,
    options}``."""
    load_plugins()
    return [{"id": vid, "kind": e["kind"], "title": e["title"], "description": e["description"],
             "options": dict(e["options"])} for vid, e in tv.VIEW_CATALOG.items()]


def _options_help(view_id: str) -> dict[str, str]:
    load_plugins()
    if view_id not in tv.VIEW_CATALOG:
        raise BridgeError("unknown_view", f"No view {view_id!r}; `view` with no id lists them.",
                          views=list(tv.VIEW_CATALOG))
    return dict(tv.VIEW_CATALOG[view_id]["options"])


def _bad_option(view_id: str, problem: str, options: dict[str, str], **extra: Any) -> BridgeError:
    takes = "; ".join(f"{k}: {v}" for k, v in options.items())
    return BridgeError("bad_option", f"{problem} View {view_id!r} takes " + (f"{takes}." if takes else "no options."),
                       view=view_id, options=options, **extra)


def _window(text: str) -> int | float:
    try:
        value: int | float = int(text)
    except ValueError:
        value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(text)
    return value


def coerce_options(view_id: str, raw: dict[str, str], run: Trajectory) -> dict:
    """*raw* (``k=v`` strings) as the view's keyword options.

    ``snapshot_step`` becomes an int, ``window_limit`` a positive number as written (int or float), and
    ``agent`` one of the keys the ``context_pressure`` view lists (``main`` names ``__main__``). An option the
    view does not take, or a value that does not fit, raises ``BridgeError("bad_option")`` carrying the
    catalog's option help; an unknown view raises ``BridgeError("unknown_view")``.
    """
    options = _options_help(view_id)
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in options:
            raise _bad_option(view_id, f"No option {key!r}.", options)
        if key == "snapshot_step":
            try:
                out[key] = int(value)
            except ValueError:
                raise _bad_option(view_id, f"snapshot_step must be a step number, not {value!r}.", options) from None
        elif key == "window_limit":
            try:
                out[key] = _window(value)
            except ValueError:
                raise _bad_option(view_id, f"window_limit must be a positive token count, not {value!r}.",
                                  options) from None
        elif key == "agent":
            agents = [a["key"] for a in run.view("context_pressure").data["agents"]]
            agent = "__main__" if value == "main" else value
            if agent not in agents:
                raise _bad_option(view_id, f"No agent {value!r} in this session; agents: {', '.join(agents)}.",
                                  options, agents=agents)
            out[key] = agent
        else:
            out[key] = value
    return out


def _rows(view_id: str, data: Any) -> list[dict]:
    if view_id in _ROWS:
        data = _ROWS[view_id](data)
    if isinstance(data, dict):
        return [{"key": k, "value": v} for k, v in data.items()]
    if not isinstance(data, list):
        return [{"value": data}]
    return [row if isinstance(row, dict) else {"value": row} for row in data]


def _groups(rows: list[dict], group: str | None) -> dict[str, list[dict]]:
    """*rows* by their *group* value (one group of all rows when *group* is None)."""
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(str(row.get(group)) if group else "", []).append(row)
    return groups


def _sparklines(rows: list[dict], group: str | None, fields: tuple[str, ...], *, limit: int, offset: int) -> str:
    """One sparkline per field (per group), lines ``offset`` to ``offset + limit``, then a points footer."""
    lines = []
    for name, members in _groups(rows, group).items():
        for field in fields:
            values = [row.get(field) for row in members]
            present = [v for v in values if isinstance(v, int | float) and not isinstance(v, bool)]
            label = " ".join(part for part in (name, "" if group and len(fields) == 1 else field) if part)
            lines.append((label, render_series(values), _num(max(present)) if present else "?"))
    page = lines[offset:offset + limit]
    width = max((len(label) for label, _, _ in page), default=0)
    steps = [row["step"] for row in rows if isinstance(row.get("step"), int)]
    footer = f"{len(rows)} points" + (f" · steps {min(steps)}–{max(steps)}" if steps else "")
    if len(page) < len(lines):
        footer += f" · lines {offset + 1}–{offset + len(page)} of {len(lines)}" if page else f" · lines 0 of {len(lines)}"
    return "\n".join([*(f"{label:<{width}} {spark} max {peak}" for label, spark, peak in page), footer])


def _bars(rows: list[dict], label_key: str, value_key: str, *, limit: int, offset: int) -> str:
    def value(row: dict) -> float:
        v = row.get(value_key)
        return v if isinstance(v, int | float) and not isinstance(v, bool) else 0

    page = rows[offset:offset + limit]
    if not page:
        return f"rows 0 of {len(rows)}"
    top = max(value(row) for row in rows) or 1
    labels = [_clip(str(row.get(label_key)), LABEL_CHARS) for row in page]
    width = max(len(label) for label in labels)
    lines = [f"{label:<{width}} {'█' * max(1 if value(row) > 0 else 0, round(value(row) / top * BAR_WIDTH))} "
             f"{_num(row.get(value_key))}" for label, row in zip(labels, page, strict=True)]
    return "\n".join(lines) + f"\n\nrows {offset + 1}–{offset + len(page)} of {len(rows)}"


def _write_json(path: Path, payload: Any) -> int:
    """*payload* written atomically as indented JSON (as ``export_json`` writes it); returns the file's size."""
    serialized = json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    with staged_path(path) as tmp:
        tmp.write_text(serialized, encoding="utf-8")
    return path.stat().st_size


def _stdout_bytes(value: Any) -> int:
    """Bytes *value* takes in the stdout JSON (the CLI's ``json.dumps`` writes non-ASCII as ``\\uXXXX``)."""
    return len(json.dumps(value))


def _fit(render: Callable[[int], str], items: int, *, limit: int, offset: int, unit: str, path: Path) -> str:
    """``render(limit)``, a page of *items* rows (or lines) from *offset*; when that takes more than
    :data:`INLINE_STDOUT_BYTES` on stdout, the most of the page that fits, then a line naming how many were cut."""
    text = render(limit)
    if _stdout_bytes(text) <= INLINE_STDOUT_BYTES:
        return text
    page = max(0, min(limit, items - offset))

    def cut(shown: int) -> str:
        n = page - shown
        marker = (f"… {n} more {unit}{'' if n == 1 else 's'} cut (inline text is capped at "
                  f"{INLINE_STDOUT_BYTES // 1024} KB); the whole view is in {path}")
        return f"{render(shown)}\n{marker}" if shown else marker

    low, high = 0, page - 1  # the whole page does not fit; find the most rows that do, marker included
    while low < high:
        mid = (low + high + 1) // 2
        if _stdout_bytes(cut(mid)) <= INLINE_STDOUT_BYTES:
            low = mid
        else:
            high = mid - 1
    return cut(low)


def view_payload(run: Trajectory, view_id: str, options: dict, *, limit: int, offset: int, out_dir: Path,
                 inline_json: bool) -> dict:
    """One view (*options* already coerced): written whole to ``out_dir/<id>.json``, a page of it inline (at most
    :data:`INLINE_STDOUT_BYTES` on stdout)."""
    _options_help(view_id)
    view = run.view(view_id, **options)
    path = out_dir / f"{view_id}.json"
    protect_run_outputs(run, [path])
    out_dir.mkdir(parents=True, exist_ok=True)
    size = _write_json(path, view.to_dict())
    rows = _rows(view_id, view.data)
    paging = {"limit": limit, "offset": offset, "path": path}
    if inline_json and size <= INLINE_JSON_BYTES and _stdout_bytes(view.data) <= INLINE_STDOUT_BYTES:
        inline: dict[str, Any] = {"data": view.data}
    elif view.kind == "chart" and view_id in _SPARKS:
        group, fields = _SPARKS[view_id]
        inline = {"series": _fit(lambda n: _sparklines(rows, group, fields, limit=n, offset=offset),
                                 len(_groups(rows, group)) * len(fields), unit="line", **paging)}
    elif view.kind == "chart" and view_id in _BARS:
        inline = {"series": _fit(lambda n: _bars(rows, *_BARS[view_id], limit=n, offset=offset), len(rows),
                                 unit="row", **paging)}
    else:
        inline = {"table": _fit(lambda n: render_table(rows, limit=n, offset=offset), len(rows), unit="row", **paging)}
    return {"view": {"id": view.id, "title": view.title, "kind": view.kind, "description": view.description},
            "path": str(path), "bytes": size, "rows_total": len(rows), "inline": inline}


def export(run: Trajectory, out_dir: Path, *, only: list[str] | None, with_trajectory: bool) -> dict:
    """Every view (or those in *only*) as ``<id>.json`` plus ``index.json``; ``trajectory.json`` only with
    *with_trajectory* (it is tens of MB for a long session)."""
    for view_id in only or ():
        _options_help(view_id)
    names = [f"{view_id}.json" for view_id in (only if only is not None else tv.VIEW_CATALOG)]
    protect_run_outputs(run, [out_dir / name for name in [*names, "index.json",
                                                        *(["trajectory.json"] if with_trajectory else [])]])
    out_dir.mkdir(parents=True, exist_ok=True)
    # TrajViz's exporter uses fixed <name>.tmp files. Give each invocation a private directory so its
    # internal staging cannot collide with another export or follow an existing staging symlink.
    with tempfile.TemporaryDirectory(prefix=".trajviz-obs-", dir=out_dir) as scratch:
        written = tv.export_json(run if with_trajectory else run.views(only=only), scratch,
                                 only=only if with_trajectory else None)
        final = [out_dir / path.name for path in written]
        for temporary, destination in zip(written, final, strict=True):
            os.replace(temporary, destination)
    return {"path": str(out_dir), "files": [p.name for p in final], "bytes": sum(p.stat().st_size for p in final)}


def write_schemas(out_dir: Path) -> dict:
    """The JSON Schema of every view TrajViz knows (plugins' views included), as ``<id>.schema.json``."""
    load_plugins()
    out_dir.mkdir(parents=True, exist_ok=True)
    files, total = [], 0
    for view_id, schema in tv.view_schemas().items():
        name = f"{view_id}.schema.json"
        total += _write_json(out_dir / name, schema)
        files.append(name)
    return {"path": str(out_dir), "files": files, "bytes": total}


def _artifacts(args: argparse.Namespace) -> Path:
    """``--artifacts``, else ``<cache>/trajviz-obs/sessions/<session-id>/`` for the transcript."""
    if args.artifacts is not None:
        return args.artifacts
    transcript = Path(args.transcript)
    session_id = transcript.name if transcript.is_dir() else transcript.stem
    return paths.artifacts_dir(paths.cache_root(os.environ), session_id)


def _load(args: argparse.Namespace) -> Trajectory:
    return load_run(args.transcript, window_limit=args.window, current=args.kind == "current")


def _offset(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = -1
    if value < 0:
        raise argparse.ArgumentTypeError(f"expected a row number of 0 or more, got {text!r}")
    return value


def _session_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--transcript", required=True, help="The session transcript (or session directory).")
    p.add_argument("--window", type=_positive_int, default=None,
                   help="The real context window in tokens (default: TrajViz's own estimate).")
    p.add_argument("--artifacts", type=Path, default=None,
                   help="The session's artifact folder (default: <cache>/trajviz-obs/sessions/<session-id>).")
    p.add_argument("--kind", choices=("current", "other"), default="other",
                   help="'current' (the session being written) answers {\"empty\": true} before its first step.")


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("view", help="The view catalog, or one view drawn inline and written to views/<id>.json.")
    _session_args(p)
    p.add_argument("view_id", nargs="?", metavar="ID", help="The view (default: list the catalog).")
    p.add_argument("--option", action="append", default=[], metavar="K=V", help="A view option (repeatable).")
    p.add_argument("--limit", type=page_limit, default=25,
                   help=f"Rows drawn inline (default: 25, at most {MAX_LIMIT}; --offset pages on).")
    p.add_argument("--offset", type=_offset, default=0, help="First row drawn inline (default: 0).")
    p.add_argument("--json", action="store_true",
                   help="The view's data inline when its file is at most 256 KB (and it fits in 200 KB on stdout).")

    p = sub.add_parser("json", help="Every view (or the named ones) as <id>.json plus index.json.")
    _session_args(p)
    p.add_argument("--out", type=Path, default=None, help="The folder (default: <artifacts>/views).")
    p.add_argument("--view", action="append", default=None, dest="views", metavar="ID", help="A view (repeatable).")
    p.add_argument("--with-trajectory", action="store_true", help="Add TrajViz's reloadable trajectory.json.")

    p = sub.add_parser("schemas", help="The JSON Schema of every view, written as files.")
    p.add_argument("--out", type=Path, default=None, help="The folder (default: <cache>/trajviz-obs/schemas).")


def _parse_options(view_id: str, pairs: list[str]) -> dict[str, str]:
    raw: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise _bad_option(view_id, f"An option is written k=v, not {pair!r}.", _options_help(view_id))
        raw[key] = value
    return raw


@register("view")
def _view(args: argparse.Namespace) -> dict:
    if args.view_id is None:
        return {"views": catalog()}
    _options_help(args.view_id)
    raw = _parse_options(args.view_id, args.option)
    try:
        run = _load(args)
    except EmptySession as exc:
        return {"empty": True, "reason": exc.reason}
    return view_payload(run, args.view_id, coerce_options(args.view_id, raw, run), limit=args.limit,
                        offset=args.offset, out_dir=_artifacts(args) / "views", inline_json=args.json)


@register("json")
def _json(args: argparse.Namespace) -> dict:
    for view_id in args.views or ():
        _options_help(view_id)
    try:
        run = _load(args)
    except EmptySession as exc:
        return {"empty": True, "reason": exc.reason}
    return export(run, args.out or _artifacts(args) / "views", only=args.views, with_trajectory=args.with_trajectory)


@register("schemas")
def _schemas(args: argparse.Namespace) -> dict:
    return write_schemas(args.out or paths.cache_root(os.environ) / "trajviz-obs" / "schemas")
