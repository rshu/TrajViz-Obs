"""``report`` and ``bundle``: TrajViz's HTML report as one file, and the session as a ZIP for a hosted dashboard.

The report goes to ``<artifacts>/report.html`` (overwritten each time) unless ``--out`` names another file; it
loads plotly.js from Plotly's CDN when opened, or embeds it with ``--offline``. The bundle holds the parent
transcript and every file under ``<session-id>/subagents/``, the layout TrajViz's dashboards accept as an upload.
"""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

import agent_trajviz as tv
from agent_trajviz import ReportError, Trajectory

from .cli import register
from .compat import EmptySession, protect_run_outputs
from .errors import BridgeError
from .outputs import protect_inputs, session_files, staged_path
from .views_cmd import _artifacts, _load, _session_args

#: Most member names ``bundle`` lists on stdout.
MEMBERS_LISTED = 200


def _file_dest(dest: Path) -> Path:
    """*dest* with its folder created; an existing folder at *dest* is refused (an explicit file is wanted)."""
    if dest.is_dir():
        raise BridgeError("bad_path", f"{dest} is a folder; give a file path.", path=str(dest))
    dest.parent.mkdir(parents=True, exist_ok=True)
    return dest


def write_report_file(run: Trajectory, dest: Path, *, dark: bool, offline: bool) -> dict:
    """TrajViz's HTML report for *run* written to the file *dest* (replaced atomically): ``{"path", "bytes"}``.

    ``MissingExtraError`` (no ``charts`` extra) propagates; TrajViz's ``ReportError`` becomes
    ``BridgeError("report_failed")``.
    """
    protect_run_outputs(run, [dest])
    dest = _file_dest(dest)
    try:
        with staged_path(dest) as tmp:
            tv.write_report(run, tmp, dark=dark, inline_plotly=offline)
    except ReportError as exc:
        raise BridgeError("report_failed", str(exc), path=run.source) from exc
    return {"path": str(dest), "bytes": dest.stat().st_size}


def bundle_session(transcript_path: str, dest: Path) -> dict:
    """The transcript and every file under ``<id>/subagents/`` as a ZIP at *dest*: ``{"path", "bytes", "members",
    "members_total"}``.

    Members are stored as ``<id>.jsonl`` and ``<id>/subagents/…`` (relative to the transcript's folder);
    ``members`` names the first :data:`MEMBERS_LISTED` of them (the ZIP holds them all, ``members_total`` counts
    them), so the answer does not grow with the session.
    """
    transcript = Path(transcript_path)
    if not transcript.is_file():
        raise BridgeError("not_found", f"No such transcript: {transcript}", path=str(transcript))
    files = session_files(transcript)
    members = [path.relative_to(transcript.parent).as_posix() for path in files]
    protect_inputs([dest], files)
    dest = _file_dest(dest)
    with staged_path(dest) as tmp, zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, member in zip(files, members, strict=True):
            archive.write(path, member)
    return {"path": str(dest), "bytes": dest.stat().st_size, "members": members[:MEMBERS_LISTED],
            "members_total": len(members)}


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("report", help="TrajViz's HTML report, written to report.html in the artifact folder.")
    _session_args(p)
    p.add_argument("--out", type=Path, default=None, help="The file (default: <artifacts>/report.html).")
    p.add_argument("--dark", action="store_true", help="The dark theme.")
    p.add_argument("--offline", action="store_true", help="Embed plotly.js (default: loaded from Plotly's CDN).")

    p = sub.add_parser("bundle", help="The transcript and its sub-agent logs as a ZIP for a hosted dashboard.")
    p.add_argument("--transcript", required=True, help="The session transcript.")
    p.add_argument("--artifacts", type=Path, default=None,
                   help="The session's artifact folder (default: <cache>/trajviz-obs/sessions/<session-id>).")
    p.add_argument("--out", type=Path, default=None, help="The file (default: <artifacts>/bundle.zip).")


@register("report")
def _report(args: argparse.Namespace) -> dict:
    try:
        run = _load(args)
    except EmptySession as exc:
        return {"empty": True, "reason": exc.reason}
    return write_report_file(run, args.out or _artifacts(args) / "report.html", dark=args.dark, offline=args.offline)


@register("bundle")
def _bundle(args: argparse.Namespace) -> dict:
    return bundle_session(args.transcript, args.out or _artifacts(args) / "bundle.zip")
