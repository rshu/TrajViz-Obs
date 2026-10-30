"""``converge-cli``, ``collect``, ``attribution`` and ``brief``: TrajViz's own CLIs and results, passed through.

``converge-cli -- ARGS…`` runs ``agent-trajviz-converge`` (``agent_trajviz.converge.cli.main``) in process with
its output captured: a JSON report goes to ``<artifacts>/converge/<n>.json`` (the next free number), any other
output (``--output summary``, a usage or input error) comes back as text. ``collect -- ARGS…`` runs
``agent-trajviz-collect`` the same way and returns what it printed. Both answer the tool's own exit status in
``exit``; the bridge itself exits 0. ``attribution`` is TrajViz's attribution result (``available`` false
with a reason when the attribution setup or reference data is missing); ``brief`` is the statistics brief TrajViz's
LLM features reason over.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from agent_trajviz import Trajectory
from agent_trajviz import attribution as tv_attribution
from agent_trajviz.collect import __main__ as collect_main
from agent_trajviz.converge import cli as converge_cli

from .cli import register
from .compat import EmptySession, install_attribution_guard, load_plugins
from .views_cmd import _load, _session_args

#: Most bytes of a pass-through's stdout or stderr returned inline (UTF-8).
OUTPUT_CAP = 64 * 1024
#: Appended to output cut at :data:`OUTPUT_CAP`.
TRUNCATED = "\n… (truncated)"


class _BoundedCapture(io.StringIO):
    """Keep only an output prefix while a command prints, with UTF-8 bytes as the budget."""

    def __init__(self) -> None:
        super().__init__()
        self._bytes = 0
        self._truncated = False

    def write(self, text: str) -> int:
        if not self._truncated:
            data = text.encode("utf-8")
            remaining = OUTPUT_CAP - self._bytes
            if len(data) > remaining:
                data = data[:remaining]
                self._truncated = True
            kept = data.decode("utf-8", "ignore")
            self._bytes += len(kept.encode("utf-8"))
            super().write(kept)
        return len(text)

    def getvalue(self) -> str:
        text = super().getvalue()
        if not self._truncated:
            return text
        keep = OUTPUT_CAP - len(TRUNCATED.encode("utf-8"))
        return text.encode("utf-8")[:keep].decode("utf-8", "ignore") + TRUNCATED


def _exit_status(code: Any, err: io.StringIO) -> int:
    """``SystemExit.code`` as an exit status: ``None`` is 0, a message is written to *err* and is 1."""
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    print(code, file=err)
    return 1


def _captured(main: Callable[[list[str]], int | None], argv: Sequence[str], *,
              bound_stdout: bool = False) -> tuple[int, str, str]:
    """``main(argv)`` with stdout and stderr captured: ``(exit status, stdout, stderr)``.

    ``SystemExit`` (argparse's usage errors, ``--help``, ``--version``) becomes the status. A log handler the
    entry point adds to the ``agent_trajviz`` logger (bound to the captured stderr) is removed afterwards and
    the logger's level restored.
    """
    out, err = _BoundedCapture() if bound_stdout else io.StringIO(), _BoundedCapture()
    logger = logging.getLogger("agent_trajviz")
    handlers, level = list(logger.handlers), logger.level
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                status = main(list(argv))
            except SystemExit as exc:
                status = _exit_status(exc.code, err)
    finally:
        for handler in [h for h in logger.handlers if h not in handlers]:
            logger.removeHandler(handler)
        logger.setLevel(level)
    return (0 if status is None else status), out.getvalue(), err.getvalue()


def _cap(text: str) -> str:
    """*text* cut to at most :data:`OUTPUT_CAP` UTF-8 bytes, marked when cut."""
    data = text.encode("utf-8")
    if len(data) <= OUTPUT_CAP:
        return text
    keep = OUTPUT_CAP - len(TRUNCATED.encode("utf-8"))
    return data[:keep].decode("utf-8", "ignore") + TRUNCATED


def _is_json(text: str) -> bool:
    if not text.lstrip().startswith(("{", "[")):
        return False
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


def _write_numbered(folder: Path, text: str) -> Path:
    """*text* written to ``<folder>/<n>.json``, *n* one past the highest number there (claimed exclusively)."""
    folder.mkdir(parents=True, exist_ok=True)
    n = max((int(p.stem) for p in folder.glob("*.json") if p.stem.isdigit()), default=0) + 1
    while True:
        path = folder / f"{n}.json"
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(text)
        except FileExistsError:
            n += 1
            continue
        return path


def run_converge_cli(argv: list[str], out_dir: Path) -> dict:
    """``agent-trajviz-converge argv`` in process: ``{"exit", "path", "text"}``.

    A JSON report (the converge CLI's default output) is written to ``<out_dir>/converge/<n>.json`` and named
    in ``path`` (``text`` is ``None``); otherwise ``path`` is ``None`` and ``text`` is what the run printed
    (stdout on success, stderr on failure), at most :data:`OUTPUT_CAP` bytes.
    """
    # The CLI reads files in this fresh bridge process, so install the same
    # native converters used by the session views and browser dashboards.
    load_plugins()
    status, stdout, stderr = _captured(converge_cli.main, argv)
    if status == 0 and _is_json(stdout):
        return {"exit": 0, "path": str(_write_numbered(out_dir / "converge", stdout)), "text": None}
    text = stdout if status == 0 or not stderr.strip() else stderr
    return {"exit": status, "path": None, "text": _cap(text)}


def run_collect(argv: list[str]) -> dict:
    """``agent-trajviz-collect argv`` in process: ``{"exit", "stdout", "stderr"}``, each capped at 64 KB."""
    status, stdout, stderr = _captured(collect_main.main, argv, bound_stdout=True)
    return {"exit": status, "stdout": _cap(stdout), "stderr": _cap(stderr)}


def attribution(run: Trajectory, *, reference_root: str | None, agent: str | None, instance_id: str | None) -> dict:
    """``agent_trajviz.attribution.diagnose`` of *run*, as TrajViz returns it."""
    install_attribution_guard()
    return tv_attribution.diagnose(run, agent=agent, instance_id=instance_id, reference_root=reference_root)


def brief(run: Trajectory) -> dict:
    """``{"brief": llm.brief(run)}``: the statistics brief TrajViz's LLM features send (needs the llm extra)."""
    from agent_trajviz import llm

    return {"brief": llm.brief(run)}


def _passed_args(args: list[str]) -> list[str]:
    """The arguments after ``--`` (argparse keeps the separator at the front of a remainder)."""
    return args[1:] if args[:1] == ["--"] else args


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("converge-cli", help="agent-trajviz-converge; a JSON report goes to <artifacts>/converge/.")
    p.add_argument("--artifacts", type=Path, required=True, help="The artifact folder JSON reports are written to.")
    p.add_argument("args", nargs=argparse.REMAINDER, metavar="-- ARGS", help="The converge CLI's arguments.")

    p = sub.add_parser("collect", help="agent-trajviz-collect, its output returned.")
    p.add_argument("args", nargs=argparse.REMAINDER, metavar="-- ARGS", help="The collect CLI's arguments.")

    p = sub.add_parser("attribution", help="Failure attribution (with an explanation when it cannot run).")
    _session_args(p)
    p.add_argument("--reference-root", default=None, metavar="DIR",
                   help="The attribution reference set (default: TRAJVIZ_REFERENCE_ROOT).")
    p.add_argument("--agent", default=None, help="The agent (default: the transcript's folder name).")
    p.add_argument("--instance", default=None, help="The task instance (default: the transcript's file name).")

    p = sub.add_parser("brief", help="The statistics brief TrajViz's LLM features reason over.")
    _session_args(p)


@register("converge-cli")
def _converge_cli(args: argparse.Namespace) -> dict:
    return run_converge_cli(_passed_args(args.args), args.artifacts)


@register("collect")
def _collect(args: argparse.Namespace) -> dict:
    return run_collect(_passed_args(args.args))


@register("attribution")
def _attribution(args: argparse.Namespace) -> dict:
    try:
        run = _load(args)
    except EmptySession as exc:
        return {"empty": True, "reason": exc.reason}
    return attribution(run, reference_root=args.reference_root, agent=args.agent, instance_id=args.instance)


@register("brief")
def _brief(args: argparse.Namespace) -> dict:
    try:
        run = _load(args)
    except EmptySession as exc:
        return {"empty": True, "reason": exc.reason}
    return brief(run)
