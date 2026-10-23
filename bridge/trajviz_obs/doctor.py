"""``doctor``: what this environment can do (Python, TrajViz, extras, DECAF, LLM settings)."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import os
import platform
from typing import Any

import agent_trajviz
from agent_trajviz import MissingExtraError, attribution

from .cli import register
from .errors import BridgeError

#: Import names of each TrajViz extra, as in ``agent_trajviz._extras.EXTRAS`` (pinned by a test).
EXTRAS: dict[str, tuple[str, ...]] = {
    "charts": ("plotly", "pygments"),
    "llm": ("requests",),
    "app": ("gradio", "plotly", "pygments", "requests"),
}


def _importable(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _llm_env(cwd: str | None, llm_extra: bool) -> dict[str, Any]:
    """LLM readiness; ``agent_trajviz.llm`` is imported only here, since it needs the ``llm`` extra."""
    unavailable = {"available": False, "ready": False, "missing": [],
                   "reason": "The 'llm' extra is not installed (pip install \"agent-trajviz[llm]\")."}
    if not llm_extra:
        return unavailable
    try:
        from agent_trajviz import llm
    except MissingExtraError as exc:
        return {**unavailable, "reason": str(exc)}
    with contextlib.chdir(cwd) if cwd else contextlib.nullcontext():
        config = llm.config_from_env(load_dotenv=True)
    return {"available": True, "ready": bool(config.ready), "missing": list(config.missing), "reason": None}


def doctor_info(cwd: str | None) -> dict[str, Any]:
    """Describe the environment; LLM settings are read with *cwd*'s ``.env`` (names only, never values)."""
    if cwd and not os.path.isdir(cwd):
        raise BridgeError("not_found", f"No such directory: {cwd}", path=cwd)
    ok, reason = attribution.available()
    extras = {extra: all(_importable(name) for name in names) for extra, names in EXTRAS.items()}
    return {
        "python": platform.python_version(),
        "trajviz": {"version": agent_trajviz.__version__, "path": os.path.dirname(agent_trajviz.__file__)},
        "extras": extras,
        "decaf": {"available": ok, "reason": reason},
        "llm_env": _llm_env(cwd, extras["llm"]),
        "reference_root": os.environ.get("TRAJVIZ_REFERENCE_ROOT") or None,
    }


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("doctor", help="Report Python, TrajViz, extras, DECAF and LLM configuration.")
    p.add_argument("--cwd", default=None, help="Directory whose .env is read for LLM settings (default: CWD).")


@register("doctor")
def _doctor(args: argparse.Namespace) -> dict:
    return doctor_info(args.cwd)
