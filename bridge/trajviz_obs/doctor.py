"""``doctor``: what this environment can do (Python, TrajViz, extras, DECAF, LLM settings)."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import os
import platform
from typing import Any

import agent_trajviz
from agent_trajviz import attribution, llm

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


def doctor_info(cwd: str | None) -> dict[str, Any]:
    """Describe the environment; LLM settings are read with *cwd*'s ``.env`` (names only, never values)."""
    if cwd and not os.path.isdir(cwd):
        raise BridgeError("not_found", f"No such directory: {cwd}", path=cwd)
    ok, reason = attribution.available()
    with contextlib.chdir(cwd) if cwd else contextlib.nullcontext():
        config = llm.config_from_env(load_dotenv=True)
    return {
        "python": platform.python_version(),
        "trajviz": {"version": agent_trajviz.__version__, "path": os.path.dirname(agent_trajviz.__file__)},
        "extras": {extra: all(_importable(name) for name in names) for extra, names in EXTRAS.items()},
        "decaf": {"available": ok, "reason": reason},
        "llm_env": {"ready": bool(config.ready), "missing": list(config.missing)},
        "reference_root": os.environ.get("TRAJVIZ_REFERENCE_ROOT") or None,
    }


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("doctor", help="Report Python, TrajViz, extras, DECAF and LLM configuration.")
    p.add_argument("--cwd", default=None, help="Directory whose .env is read for LLM settings (default: CWD).")


@register("doctor")
def _doctor(args: argparse.Namespace) -> dict:
    return doctor_info(args.cwd)
