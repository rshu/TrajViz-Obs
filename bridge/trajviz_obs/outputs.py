"""Keep generated artifacts from replacing the session files they describe."""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterable
from pathlib import Path

from .errors import BridgeError


def session_files(transcript: Path) -> list[Path]:
    """The explicit transcript and regular files in its known subagent layout; never follow sidecar links."""
    session = transcript.with_suffix("")
    root = session / "subagents"
    found = [transcript]
    if session.is_symlink() or root.is_symlink() or not root.is_dir():
        return found
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(folder, d))]
        found += [path for name in files if not (path := Path(folder, name)).is_symlink() and path.is_file()]
    return [transcript, *sorted(found[1:])]


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
