"""Bundle a native session and its subagents for upload into a TrajViz dashboard.

Preserve the parent transcript and every file under ``<session-id>/subagents/`` in the native directory
layout. Output is replaced atomically and must not overwrite any session input.
"""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

from .cli import register
from .errors import BridgeError
from .outputs import protect_inputs, session_files, staged_path
from .views_cmd import _artifacts

#: Most member names ``bundle`` lists on stdout.
MEMBERS_LISTED = 200


def _file_dest(dest: Path) -> Path:
    """*dest* with its folder created; an existing folder at *dest* is refused (an explicit file is wanted)."""
    if dest.is_dir():
        raise BridgeError("bad_path", f"{dest} is a folder; give a file path.", path=str(dest))
    dest.parent.mkdir(parents=True, exist_ok=True)
    return dest


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
    p = sub.add_parser("bundle", help="The transcript and its sub-agent logs as a ZIP for a hosted dashboard.")
    p.add_argument("--transcript", required=True, help="The session transcript.")
    p.add_argument("--artifacts", type=Path, default=None,
                   help="The session's artifact folder (default: <cache>/trajviz-obs/sessions/<session-id>).")
    p.add_argument("--out", type=Path, default=None, help="The file (default: <artifacts>/bundle.zip).")


@register("bundle")
def _bundle(args: argparse.Namespace) -> dict:
    return bundle_session(args.transcript, args.out or _artifacts(args) / "bundle.zip")
