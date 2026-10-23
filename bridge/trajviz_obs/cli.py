"""Subcommand registry, argument parsing and the JSON envelope on stdout.

Exit codes: 0 with the handler's document; 1 with ``{"error": ...}`` for a
reported failure; 2 for a usage error; 3 for an unexpected exception (the
traceback goes to stderr).
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import traceback
from collections.abc import Callable
from typing import Any

from agent_trajviz import LoadError, MissingExtraError

from .errors import BridgeError

Handler = Callable[[argparse.Namespace], dict]

#: Subcommand name -> handler, filled by :func:`register`.
SUBCOMMANDS: dict[str, Handler] = {}

#: Handler modules, each exposing ``add_parser(sub)``; every task appends its own.
MODULES: tuple[str, ...] = ("doctor", "sessions", "snapshot", "views_cmd", "report_cmd", "compare_cmd", "llm_cmd")


def register(name: str) -> Callable[[Handler], Handler]:
    """Decorator adding *handler* to :data:`SUBCOMMANDS` under *name*."""

    def decorate(handler: Handler) -> Handler:
        SUBCOMMANDS[name] = handler
        return handler

    return decorate


class _UsageError(Exception):
    pass


class _HelpRequested(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    """An argparse parser that raises instead of printing and exiting."""

    def error(self, message: str):
        raise _UsageError(f"{self.prog}: {message}")

    def print_help(self, file=None) -> None:
        raise _HelpRequested(self.format_help())


def load_modules() -> list[Any]:
    """Import every handler module (registering its subcommands) and return them."""
    return [importlib.import_module(f"{__package__}.{name}") for name in MODULES]


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="trajviz_obs_cli.py", description="TrajViz bridge for /obs (JSON on stdout).")
    sub = parser.add_subparsers(dest="command", metavar="SUBCOMMAND", required=True)
    for module in load_modules():
        module.add_parser(sub)
    for name in SUBCOMMANDS:
        if name not in sub.choices:
            sub.add_parser(name)
    return parser


def _write(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _emit(document: dict[str, Any]) -> None:
    _write(json.dumps(document))


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, **extra}}


def main(argv: list[str] | None = None) -> int:
    try:
        parser = build_parser()
        try:
            args = parser.parse_args(argv)
        except _UsageError as exc:
            _emit(_error("usage", str(exc)))
            return 2
        except _HelpRequested as exc:
            _emit({"help": str(exc)})
            return 0
        # Serialised before anything is written, so a bad document never leaves partial output.
        text = json.dumps(SUBCOMMANDS[args.command](args))
    except BridgeError as exc:
        _emit({"error": exc.to_dict()})
        return 1
    except MissingExtraError as exc:
        _emit(_error("missing_extra", str(exc), extra=exc.extra))
        return 1
    except LoadError as exc:
        _emit(_error("load_error", str(exc), trajviz_code=exc.code, path=exc.path))
        return 1
    except Exception as exc:  # noqa: BLE001 - every failure must still answer in JSON
        traceback.print_exc(file=sys.stderr)
        _emit(_error("internal", f"{type(exc).__name__}: {exc}"))
        return 3
    _write(text)
    return 0
