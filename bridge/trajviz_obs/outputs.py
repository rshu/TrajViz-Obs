"""Keep generated artifacts from replacing the session files they describe."""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path

from .errors import BridgeError


@contextlib.contextmanager
def staged_path(destination: Path) -> Iterator[Path]:
    """A private staging file beside *destination*, atomically installed only after the writer succeeds.

    Exclusive creation prevents both concurrent writers sharing a staging path and a preexisting symlink
    redirecting a temporary write. The caller creates the destination directory and protects its inputs.
    """
    descriptor, name = tempfile.mkstemp(prefix=".trajviz-obs-", suffix=".tmp", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        yield temporary
        os.replace(temporary, destination)
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink()


def session_files(transcript: Path, *, session_id: str | None = None) -> list[Path]:
    """The explicit transcript and regular files in its known subagent layout; never follow sidecar links."""
    found = [transcript]
    sessions = {transcript.with_suffix("")}
    if isinstance(session_id, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", session_id):
        sessions.add(transcript.parent / session_id)
    for session in sorted(sessions):
        root = session / "subagents"
        if session.is_symlink() or root.is_symlink() or not root.is_dir():
            continue
        for folder, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(folder, d))]
            found += [path for name in files if not (path := Path(folder, name)).is_symlink() and path.is_file()]
    return [transcript, *sorted(set(found[1:]))]


def protect_inputs(destinations: Iterable[Path], sources: Iterable[Path]) -> None:
    """Refuse source aliases at output or staging paths before writing any part of an export."""
    resolved: dict[Path, Path] = {}
    inodes: dict[tuple[int, int], Path] = {}
    for source in sources:
        resolved[source.resolve()] = source
        with contextlib.suppress(OSError):
            stat = source.stat()
            inodes[stat.st_dev, stat.st_ino] = source
    for dest in destinations:
        for output in (dest, dest.with_name(dest.name + ".tmp")):
            source = resolved.get(output.resolve())
            if source is None:
                with contextlib.suppress(OSError):
                    stat = output.stat()
                    source = inodes.get((stat.st_dev, stat.st_ino))
            if source is not None:
                raise BridgeError("bad_output", f"Output path would overwrite input session file: {source}",
                                  path=str(dest))
