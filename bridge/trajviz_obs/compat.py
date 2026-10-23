"""The one place TrajViz internals are touched, each shim behind :func:`check_supported`.

The plugin ships against the published ``agent-trajviz>=0.2.0,<0.3``. Two things have no public path there:

1. **The real context window everywhere.** ``context_pressure(window_limit=)`` and the ``context_*`` views take
   an override, but the issues, the brief, the report and the export do not. :func:`load_run` writes the window
   into ``run.raw["metadata"]["context_window_limit"]`` before anything is computed, so every consumer measures
   against it (TrajViz then calls it ``declared``; :func:`window_info` reports ``override``).
2. **Session-mode labeling.** ``label_trajectory()`` has no ``chat=``; :func:`labeler_chat` patches the
   labeler's ``call_llm`` (and its retry sleep) for the duration of a capture or replay.
3. **A step cap on env-mode labeling.** ``label_trajectory()`` labels every step; :func:`labeler_step_cap`
   wraps its real ``call_llm`` so only the first steps reach the endpoint, as session mode labels them.
"""

from __future__ import annotations

import contextlib
import inspect
import re
import types
import weakref
from collections.abc import Callable, Iterator
from typing import Any

import agent_trajviz
from agent_trajviz import LoadError, Trajectory

from .errors import BridgeError

#: The installed TrajViz version.
TRAJVIZ_VERSION: str = agent_trajviz.__version__

#: Supported releases: ``>=0.2.0,<0.3`` as ``[lowest, first unsupported)``.
_SUPPORTED = ((0, 2, 0), (0, 3, 0))

#: ``LoadError`` codes that mean "no assistant step yet" for the session being written.
_EMPTY_CODES = frozenset({"unknown_format", "no_steps"})

#: Runs whose window :func:`load_run` set, with that window.
_OVERRIDES: weakref.WeakKeyDictionary[Trajectory, int] = weakref.WeakKeyDictionary()


class EmptySession(BridgeError):
    """The current session has no assistant step yet, so there is nothing to analyze."""

    def __init__(self, reason: str, *, path: str | None = None) -> None:
        super().__init__("empty_session", reason, path=path)
        self.reason = reason


def _release(version: str) -> tuple[int, int, int] | None:
    match = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", version)
    if not match:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


def check_supported() -> None:
    """Refuse a TrajViz outside ``>=0.2.0,<0.3``, whose internals the shims were not written against."""
    release = _release(TRAJVIZ_VERSION)
    if release is None or not _SUPPORTED[0] <= release < _SUPPORTED[1]:
        raise BridgeError("unsupported_trajviz",
                          f"agent-trajviz {TRAJVIZ_VERSION} is installed; this bridge needs >=0.2.0,<0.3.",
                          version=TRAJVIZ_VERSION, required=">=0.2.0,<0.3")


def load_run(path: str, *, window_limit: int | None = None, current: bool = False) -> Trajectory:
    """``agent_trajviz.load(path)`` with the engine's window (when given) set before anything is computed.

    With *current* (the session being written), a file with no assistant step yet raises
    :class:`EmptySession` instead of TrajViz's ``LoadError``.
    """
    check_supported()
    if window_limit is not None and window_limit < 1:
        raise ValueError(f"window_limit must be a positive token count, not {window_limit}")
    try:
        run = agent_trajviz.load(path)
    except LoadError as exc:
        if current and exc.code in _EMPTY_CODES:
            raise EmptySession(str(exc), path=exc.path or path) from exc
        raise
    if window_limit is not None:
        metadata = run.raw.get("metadata")
        if not isinstance(metadata, dict):
            metadata = run.raw["metadata"] = {}
        metadata["context_window_limit"] = window_limit
        _OVERRIDES[run] = window_limit
    return run


def window_info(run: Trajectory) -> dict[str, Any]:
    """``{"window", "window_source", "window_note"}``: ``override`` when :func:`load_run` set it, else TrajViz's."""
    if run in _OVERRIDES:
        return {"window": _OVERRIDES[run], "window_source": "override", "window_note": ""}
    stats = run.context_pressure()["stats"]
    return {"window": stats.get("window_limit"), "window_source": stats.get("window_source"),
            "window_note": stats.get("window_note") or ""}


@contextlib.contextmanager
def labeler_chat(chat: Callable[[str, str], str]) -> Iterator[None]:
    """Route the labeler's model calls to ``chat(system_prompt, user_message)`` for the duration.

    Both messages are redacted first, as ``call_llm`` itself does. The labeler's ``time`` is swapped for a
    stand-in whose ``sleep`` is a no-op (its retry and pacing sleeps), leaving the process-wide
    ``time.sleep`` alone.
    """
    check_supported()
    from agent_trajviz._redact import redact
    from agent_trajviz.labeling import labeler, llm

    original_call, original_time = llm.call_llm, labeler.time
    signature = inspect.signature(original_call)

    def call_llm(*args: Any, **kwargs: Any) -> str:
        bound = signature.bind(*args, **kwargs).arguments
        system_prompt, _ = redact(bound["system_prompt"])
        user_message, _ = redact(bound["user_message"])
        return chat(system_prompt, user_message)

    quiet_time = types.SimpleNamespace(**{name: getattr(original_time, name) for name in dir(original_time)
                                          if not name.startswith("__")})
    quiet_time.sleep = lambda seconds: None
    llm.call_llm, labeler.time = call_llm, quiet_time
    try:
        yield
    finally:
        llm.call_llm, labeler.time = original_call, original_time


@contextlib.contextmanager
def labeler_step_cap(max_steps: int, fallback: str) -> Iterator[list[str]]:
    """Let the labeler's real ``call_llm`` classify only its first *max_steps* steps; later steps get *fallback*.

    A step is one step message: the labeler's retry resends the same message (each starts ``Step #<index>``),
    so it counts as the same step, and step *i* is session mode's prompt *i*. Yields a list that, on exit,
    holds ``"prompt <i>: <error class>"`` for each step whose last call raised (the class only, as the labeler
    logs it: a provider error can echo credentials or prompt text).
    """
    check_supported()
    from agent_trajviz.labeling import llm

    original_call = llm.call_llm
    signature = inspect.signature(original_call)
    state: dict[str, Any] = {"step": -1, "message": None}
    failures: dict[int, str] = {}
    errors: list[str] = []

    def call_llm(*args: Any, **kwargs: Any) -> str:
        message = signature.bind(*args, **kwargs).arguments["user_message"]
        if message != state["message"]:
            state["step"], state["message"] = state["step"] + 1, message
        step = state["step"]
        if step >= max_steps:
            return fallback
        try:
            reply = original_call(*args, **kwargs)
        except Exception as exc:
            failures[step] = type(exc).__name__
            raise
        failures.pop(step, None)
        return reply

    llm.call_llm = call_llm
    try:
        yield errors
    finally:
        llm.call_llm = original_call
        errors.extend(f"prompt {step}: {name}" for step, name in sorted(failures.items()))
