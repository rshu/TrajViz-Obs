"""Where things live: the cache and artifact folders, and Claude Code's session store."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path


def _home(env: Mapping[str, str]) -> Path:
    return Path(env["HOME"]) if env.get("HOME") else Path.home()


def cache_root(env: Mapping[str, str]) -> Path:
    """``$XDG_CACHE_HOME``, else ``$HOME/.cache``, else ``%LOCALAPPDATA%`` on Windows (the mod's order)."""
    if env.get("XDG_CACHE_HOME"):
        return Path(env["XDG_CACHE_HOME"])
    if env.get("HOME"):
        return Path(env["HOME"]) / ".cache"
    if env.get("OS") == "Windows_NT" and env.get("LOCALAPPDATA"):
        return Path(env["LOCALAPPDATA"])
    return Path.home() / ".cache"


def artifacts_dir(cache_root: Path, session_id: str) -> Path:
    """``<cache>/trajviz-obs/sessions/<session-id>/``; *session_id* must be one plain path component."""
    if session_id in ("", ".", "..") or "/" in session_id or "\\" in session_id:
        raise ValueError(f"Not a session id: {session_id!r}")
    return cache_root / "trajviz-obs" / "sessions" / session_id


def config_dir(env: Mapping[str, str]) -> Path:
    """Claude Code's configuration folder: ``$CLAUDE_CONFIG_DIR``, else ``~/.claude``."""
    if env.get("CLAUDE_CONFIG_DIR"):
        return Path(env["CLAUDE_CONFIG_DIR"]).expanduser()
    return _home(env) / ".claude"


def projects_dir(config_dir: Path) -> Path:
    """The folder holding one sub-folder of session transcripts per project."""
    return config_dir / "projects"


def project_slug(cwd: str) -> str:
    """Claude Code's project folder name: *cwd* with every character outside ``[A-Za-z0-9]`` replaced by ``-``."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)
