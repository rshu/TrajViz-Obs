"""``resolve`` and ``sessions``: find a session transcript and list this project's sessions.

Native listing reads at most the first 50 lines for a transcript's start time
and counts newlines. Capture listing uses TrajViz's bounded saved-OTLP reader.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from . import paths
from .cli import register
from .errors import BridgeError

#: Lines read from the top of a transcript when looking for its start time.
STARTED_LINES = 50
#: Most rows one ``--limit`` asks for (``sessions``, and ``view`` which pages on with ``--offset``).
MAX_LIMIT = 500
#: Most bytes the listed sessions take in the stdout JSON; later rows are left out and counted in ``cut``.
LIST_BYTES = 200 * 1024

_CHUNK = 1 << 20


@dataclass(frozen=True)
class Resolved:
    """A session to analyze: the transcript (or session directory) and where it lives."""

    path: str
    session_id: str
    project_dir: str
    kind: Literal["file", "dir"]


def _project_dir(cwd: str, config_dir: Path) -> Path:
    return paths.projects_dir(config_dir) / paths.project_slug(cwd)


def _transcripts(folder: Path) -> list[tuple[str, os.stat_result]]:
    """``(id, stat)`` of every ``<id>.jsonl`` directly in *folder*, newest first."""
    try:
        entries = list(os.scandir(folder))
    except (FileNotFoundError, NotADirectoryError):
        return []
    found = [(e.name[: -len(".jsonl")], e.stat()) for e in entries if e.name.endswith(".jsonl") and e.is_file()]
    return sorted(found, key=lambda item: (-item[1].st_mtime_ns, item[0]))


def _timestamp(line: bytes) -> str | None:
    try:
        record = json.loads(line)
    except ValueError:
        return None
    value = record.get("timestamp") if isinstance(record, dict) else None
    return value if isinstance(value, str) and value else None


def _started(path: Path) -> str | None:
    """The first ``timestamp`` in the first :data:`STARTED_LINES` lines, else None."""
    with path.open("rb") as handle:
        for _ in range(STARTED_LINES):
            line = handle.readline()
            if not line:
                break
            if found := _timestamp(line):
                return found
    return None


def _records(path: Path) -> int:
    """Lines in *path*, counting an unterminated last line (a record being written) as one."""
    count, last = 0, b"\n"
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            count += chunk.count(b"\n")
            last = chunk[-1:]
    return count + (last != b"\n")


def _resolved_file(path: Path) -> Resolved:
    return Resolved(path=str(path), session_id=path.stem, project_dir=str(path.parent), kind="file")


def _looks_like_path(spec: str) -> bool:
    return "/" in spec or os.sep in spec or spec.startswith("~") or spec in (".", "..") or spec.endswith(
        (".jsonl", ".json"))


def _kind(target: Path) -> Literal["file", "dir"] | None:
    """Whether *target* is a file or a directory (None for neither); ``OSError`` when the OS cannot look it up."""
    return "file" if target.is_file() else "dir" if target.is_dir() else None


def resolve_session(spec: str, *, cwd: str, config_dir: Path) -> Resolved:
    """Resolve *spec*: a path to a file or session directory (relative to *cwd*), else an id or unique id
    prefix among this project's sessions.

    A path the OS cannot look up (a name too long, a folder on the way that cannot be searched) is
    ``not_found``; a spec that is not path-like is then tried as an id.
    """
    target = Path(os.path.abspath(os.path.join(cwd, os.path.expanduser(spec)))) if spec else None
    try:
        kind = _kind(target) if target is not None else None
    except OSError as exc:
        if _looks_like_path(spec):
            raise BridgeError("not_found", f"Cannot look up {target}: {exc.strerror or exc}",
                              path=str(target)) from exc
        kind = None
    if kind == "file":
        return _resolved_file(target)
    if kind == "dir":
        return Resolved(path=str(target), session_id=target.name, project_dir=str(target.parent), kind="dir")
    if target is not None and _looks_like_path(spec):
        raise BridgeError("not_found", f"No such file or session directory: {target}", path=str(target))
    project = _project_dir(cwd, config_dir)
    matches = [(sid, st) for sid, st in _transcripts(project) if spec and sid.startswith(spec)]
    exact = [(sid, st) for sid, st in matches if sid == spec]
    if exact or len(matches) == 1:
        return _resolved_file(project / f"{(exact or matches)[0][0]}.jsonl")
    if not matches:
        raise BridgeError("not_found", f"No session matching {spec!r} in {project}", path=str(project))
    candidates = [{"id": sid, "started": _started(project / f"{sid}.jsonl"), "size": st.st_size}
                  for sid, st in matches]
    raise BridgeError("ambiguous", f"{len(matches)} sessions start with {spec!r}; give more of the id.",
                      candidates=candidates)


def resolve_session_id(session_id: str, *, cwd: str, config_dir: Path) -> Resolved:
    """Find ``<session_id>.jsonl`` (an exact id) in this project's folder, else in any project's folder."""
    projects = paths.projects_dir(config_dir)
    if session_id in ("", ".", "..") or "/" in session_id or "\\" in session_id:
        raise BridgeError("not_found", f"Not a session id: {session_id!r}", path=str(projects))
    folders = [_project_dir(cwd, config_dir)]
    if projects.is_dir():
        folders += sorted(p for p in projects.iterdir() if p.is_dir() and p != folders[0])
    for folder in folders:
        candidate = folder / f"{session_id}.jsonl"
        with contextlib.suppress(OSError):  # a name too long, or a folder that cannot be searched: not here
            if candidate.is_file():
                return _resolved_file(candidate)
    raise BridgeError("not_found", f"No transcript for session {session_id} under {projects}", path=str(projects))


def list_sessions(*, cwd: str, config_dir: Path, limit: int) -> list[dict[str, Any]]:
    """This project's sessions, newest first, without loading any transcript."""
    project = _project_dir(cwd, config_dir)
    rows = []
    for sid, st in _transcripts(project)[:limit]:
        path = project / f"{sid}.jsonl"
        rows.append({"id": sid, "path": str(path), "mtime_ms": st.st_mtime_ns // 1_000_000, "size": st.st_size,
                     "records": _records(path), "started": _started(path)})
    return rows


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise ValueError(text)
    return value


def page_limit(text: str) -> int:
    """``--limit`` (an argparse type): a whole number from 1 to :data:`MAX_LIMIT`."""
    try:
        value = int(text)
    except ValueError:
        value = 0
    if not 1 <= value <= MAX_LIMIT:
        raise argparse.ArgumentTypeError(f"expected a whole number from 1 to {MAX_LIMIT}, got {text!r}")
    return value


def capture_session_id(text: str) -> str:
    """An exact session id inside a saved OTLP capture, separate from the host session selector."""
    if not text.strip() or len(text) > 4096 or "\x00" in text:
        raise argparse.ArgumentTypeError("session-id must be a non-empty captured session id up to 4096 characters")
    return text


def _within(rows: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
    """The first *rows* whose JSON list (as the CLI prints it) takes at most *budget* bytes."""
    shown, used = [], 0
    for row in rows:
        used += len(json.dumps(row)) + 2  # the row and its ", " (or the list's brackets)
        if used > budget:
            break
        shown.append(row)
    return shown


def _add_location(p: argparse.ArgumentParser) -> None:
    p.add_argument("--cwd", default=None, help="The session's working directory (default: CWD).")
    p.add_argument("--config-dir", default=None,
                   help="Claude Code's configuration folder (default: $CLAUDE_CONFIG_DIR, else ~/.claude).")


def _location(args: argparse.Namespace) -> tuple[str, Path]:
    cwd = args.cwd or os.getcwd()
    return cwd, Path(args.config_dir) if args.config_dir else paths.config_dir(os.environ)


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("resolve", help="Find a session's transcript by path, id or unique id prefix.")
    _add_location(p)
    spec = p.add_mutually_exclusive_group(required=True)
    spec.add_argument("--session", help="A path to a file or session directory, or an id or prefix in this project.")
    spec.add_argument("--session-id", help="An exact session id, searched in every project's folder.")
    p = sub.add_parser("sessions", help="List this project's sessions, newest first.")
    _add_location(p)
    p.add_argument("--limit", type=page_limit, default=20,
                   help=f"Most sessions to list (default: 20, at most {MAX_LIMIT}).")
    p = sub.add_parser("capture-sessions", help="List selectable sessions inside a saved OTLP capture.")
    p.add_argument("--transcript", required=True, help="The saved capture file, manifest directory, or ZIP.")
    p.add_argument("--limit", type=page_limit, default=20,
                   help=f"Most captured sessions to list (default: 20, at most {MAX_LIMIT}).")


@register("resolve")
def _resolve(args: argparse.Namespace) -> dict:
    cwd, config = _location(args)
    if args.session_id is not None:
        return asdict(resolve_session_id(args.session_id, cwd=cwd, config_dir=config))
    return asdict(resolve_session(args.session, cwd=cwd, config_dir=config))


@register("sessions")
def _sessions(args: argparse.Namespace) -> dict:
    cwd, config = _location(args)
    rows = list_sessions(cwd=cwd, config_dir=config, limit=args.limit)
    shown = _within(rows, LIST_BYTES)
    out: dict[str, Any] = {"project_dir": str(_project_dir(cwd, config)), "sessions": shown}
    if len(shown) < len(rows):
        out["cut"] = len(rows) - len(shown)
    return out


@register("capture-sessions")
def _capture_sessions(args: argparse.Namespace) -> dict:
    import agent_trajviz as tv

    from .compat import load_plugins

    load_plugins()
    rows = tv.list_sessions(args.transcript)
    # IDs appear again in the readable text. Reserve room for that second copy.
    shown = _within(rows[:args.limit], LIST_BYTES // 2)
    cut = len(rows) - len(shown)
    lines = [f"{len(shown)} captured sessions in {args.transcript}:",
             *(str(row["id"]) for row in shown)]
    if cut:
        lines.append(f"… {cut} more")
    lines.append("Select one with --session <capture-path> --session-id <id>.")
    return {"path": args.transcript, "sessions": shown, "cut": cut, "text": "\n".join(lines)}
