"""``doctor``: what this environment can do (Python, TrajViz, plugins, extras, attribution, LLM settings)."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import os
import platform
from typing import Any

import agent_trajviz
from agent_trajviz import attribution

from . import compat
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
    with contextlib.chdir(cwd) if cwd else contextlib.nullcontext():
        config = compat.llm_config_from_env(load_dotenv=True)
    return {"available": True, "ready": bool(config.ready), "missing": list(config.missing), "reason": None}


def _plugins() -> dict[str, Any]:
    """The TrajViz plugins loaded (names), or, when one failed, its error (``loaded`` is then empty)."""
    try:
        return {"loaded": compat.load_plugins(), "error": None}
    except BridgeError as exc:
        return {"loaded": [], "error": exc.message}


def doctor_info(cwd: str | None) -> dict[str, Any]:
    """Describe the environment; LLM settings are read with *cwd*'s ``.env`` (names only, never values)."""
    if cwd and not os.path.isdir(cwd):
        raise BridgeError("not_found", f"No such directory: {cwd}", path=cwd)
    compat.configure_attribution_environment()
    ok, reason = attribution.available()
    extras = {extra: all(_importable(name) for name in names) for extra, names in EXTRAS.items()}
    return {
        "python": platform.python_version(),
        "trajviz": {"version": agent_trajviz.__version__, "path": os.path.dirname(agent_trajviz.__file__)},
        "plugins": _plugins(),
        "extras": extras,
        "attribution": {"available": ok, "reason": compat.attribution_message(reason)},
        "llm_env": _llm_env(cwd, extras["llm"]),
        "reference_root": os.environ.get("TRAJVIZ_REFERENCE_ROOT") or None,
    }


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("doctor", help="Report Python, TrajViz, extras, attribution and LLM configuration.")
    p.add_argument("--cwd", default=None, help="Directory whose .env is read for LLM settings (default: CWD).")


@register("doctor")
def _doctor(args: argparse.Namespace) -> dict:
    return doctor_info(args.cwd)
