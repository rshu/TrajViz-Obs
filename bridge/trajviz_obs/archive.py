"""Reload the portable, single-trajectory archives produced by ``/obs bundle``."""

from __future__ import annotations

import functools
import inspect
import json
import stat
import zipfile
from pathlib import Path

from agent_trajviz import LoadError

from .errors import BridgeError

MAX_BYTES = 256 * 1024 * 1024


def install() -> None:
    """Keep validation in the public export loader; add only the bounded archive transport."""
    from agent_trajviz import api

    original = api._read_exported
    if getattr(original, "_trajviz_obs_archive", False):
        return
    if set(inspect.signature(original).parameters) != {"path", "bounded"}:
        raise BridgeError("unsupported_trajviz", "TrajViz's export loader changed; update the plugin.")

    @functools.wraps(original)
    def read_exported(path, *, bounded=False):
        if Path(path).suffix.lower() != ".zip":
            return original(path, bounded=bounded)
        try:
            with zipfile.ZipFile(path) as archive:
                members = archive.infolist()
                if len(members) != 1 or members[0].filename != "trajectory.json":
                    return original(path, bounded=bounded)
                member = members[0]
                if stat.S_ISLNK(member.external_attr >> 16):
                    raise LoadError("Symlinked normalized archive member.", code="unsafe_bundle", path=str(path))
                if member.file_size > MAX_BYTES:
                    raise LoadError("Normalized archive exceeds the 256 MiB limit.", code="size_limit", path=str(path))
                with archive.open(member) as handle:
                    data = handle.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise LoadError("Normalized archive exceeds the 256 MiB limit.", code="size_limit", path=str(path))
                document = json.loads(data)
                if isinstance(document, dict) and document.get("type") == api.TRAJECTORY_TYPE:
                    return document
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, RecursionError):
            pass
        return original(path, bounded=bounded)

    read_exported._trajviz_obs_archive = True
    api._read_exported = read_exported
