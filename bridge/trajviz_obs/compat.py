"""The one place TrajViz internals are touched, each shim behind :func:`check_supported`.

The plugin ships against the published ``agent-trajviz>=0.2.0,<0.3``. These integrations need internal seams:

1. **The real context window everywhere.** ``context_pressure(window_limit=)`` and the ``context_*`` views take
   an override, but the issues, the brief and the export do not. :func:`load_run` writes the window
   into ``run.raw["metadata"]["context_window_limit"]`` before anything is computed, so every consumer measures
   against it (TrajViz then calls it ``declared``; :func:`window_info` reports ``override``).
2. **Session-mode labeling.** ``label_trajectory()`` has no ``chat=``; :func:`labeler_chat` patches the
   labeler's ``call_llm`` (and its retry sleep) for the duration of a capture or replay.
3. **A step cap on env-mode labeling.** ``label_trajectory()`` labels every step; :func:`labeler_step_cap`
   wraps its real ``call_llm`` so only the first steps reach the endpoint, as session mode labels them.
4. **Native Claude attribution evidence.** :func:`install_attribution_guard` refuses capability blame when
   the optional DECAF parser cannot preserve native JSONL Read/Bash calls in an isolated capability probe.

It also runs TrajViz's plugins (public API): ``load()`` reads only the formats already registered, so
:func:`load_plugins` runs ``agent_trajviz.load_plugins()`` once per process before the first load or view lookup,
as TrajViz's own command does on start.
"""

from __future__ import annotations

import contextlib
import copy
import functools
import inspect
import json
import re
import sys
import tempfile
import types
import weakref
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
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

# A reloadable export keeps its original provenance in ``run.source``. That value is content, not the
# path opened by this process, so it must never decide which input an artifact writer may replace.
_INPUT_PATHS: weakref.WeakKeyDictionary[Trajectory, Path] = weakref.WeakKeyDictionary()


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


@functools.cache
def _plugin_outcome() -> tuple[tuple[str, ...], str | None]:
    """The one ``agent_trajviz.load_plugins()`` call of this process: ``(names loaded, None)`` or ``((), error)``."""
    try:
        with contextlib.redirect_stdout(sys.stderr):
            return tuple(agent_trajviz.load_plugins()), None
    except agent_trajviz.PluginError as exc:
        return (), str(exc)


def load_plugins() -> list[str]:
    """Run the installed TrajViz plugins (formats, views) once per process; the names of those loaded.

    Later calls repeat the first outcome without running any hook again. A plugin whose hook fails raises
    ``BridgeError("plugin_error")``, on this call and every later one. What a hook prints goes to stderr, so
    stdout keeps only the bridge's one JSON document.
    """
    loaded, error = _plugin_outcome()
    if error is not None:
        raise BridgeError("plugin_error", error)
    return list(loaded)


def load_run(path: str, *, window_limit: int | None = None, current: bool = False) -> Trajectory:
    """``agent_trajviz.load(path)`` with the engine's window (when given) set before anything is computed.

    The installed TrajViz plugins are loaded first (:func:`load_plugins`). With *current* (the session being
    written), a file with no assistant step yet raises :class:`EmptySession` instead of TrajViz's ``LoadError``.
    """
    check_supported()
    load_plugins()
    if window_limit is not None and window_limit < 1:
        raise ValueError(f"window_limit must be a positive token count, not {window_limit}")
    if current:
        try:
            Path(path).stat()
        except FileNotFoundError as exc:
            # Claude can invoke the first /obs command before creating this session's transcript.
            raise EmptySession("The current session transcript has not been created yet.", path=path) from exc
    selected = resolve_input_path(path)
    try:
        run = agent_trajviz.load(path)
    except LoadError as exc:
        if current and exc.code in _EMPTY_CODES:
            raise EmptySession(str(exc), path=exc.path or path) from exc
        raise
    if current and not any(step.get("role") == "assistant" for step in run.steps):
        raise EmptySession("The current session has no assistant step yet.", path=path)
    _INPUT_PATHS[run] = selected
    if window_limit is not None:
        metadata = run.raw.get("metadata")
        if not isinstance(metadata, dict):
            metadata = run.raw["metadata"] = {}
        metadata["context_window_limit"] = window_limit
        _OVERRIDES[run] = window_limit
    return run


def resolve_input_path(path: str) -> Path:
    """Resolve a directory's selected input, even when its contents cannot be loaded as a trajectory."""
    check_supported()
    from agent_trajviz.insight.formats.claude_files import resolve_session_path

    selected, _ = resolve_session_path(path)
    return Path(selected).absolute()


def input_path(run: Trajectory) -> Path | None:
    """The file actually opened by :func:`load_run`, independent of an export's embedded provenance.

    Runs reconstructed from a saved current-session request use the original transcript assigned by the
    bridge. Other directly constructed trajectories retain their caller-supplied source as a fallback.
    """
    return _INPUT_PATHS.get(run) or (Path(run.source) if run.source else None)


def window_info(run: Trajectory) -> dict[str, Any]:
    """``{"window", "window_source", "window_note"}``: ``override`` when :func:`load_run` set it, else TrajViz's."""
    if run in _OVERRIDES:
        return {"window": _OVERRIDES[run], "window_source": "override", "window_note": ""}
    stats = run.context_pressure()["stats"]
    return {"window": stats.get("window_limit"), "window_source": stats.get("window_source"),
            "window_note": stats.get("window_note") or ""}


def dashboard_window_callback(callback: Callable, transcript: str, window: int) -> Callable:
    """Give one Insight load callback the host's window for its preloaded file only.

    TrajViz 0.2 does not expose a dashboard loader option. Clone the two small function objects that
    dispatch loading, retaining their code and closures while replacing only their globals mappings.
    Other apps, library calls and files uploaded later keep TrajViz's original loader. Applying the
    metadata before ``session_from_run`` also gives issues and charts the same window as ``/obs``.
    """
    check_supported()
    from agent_trajviz.insight.session import LoadError as SessionLoadError
    from agent_trajviz.insight.session import session_from_run
    from agent_trajviz.insight.ui import load as ui_load

    if (not isinstance(callback, types.FunctionType) or callback.__name__ != "_do_load"
            or callback.__module__ != ui_load.__name__
            or callback.__globals__.get("do_load") is not ui_load.do_load
            or ui_load.do_load.__globals__.get("load_session") is not ui_load.load_session):
        raise BridgeError("dashboard_incompatible", "TrajViz's dashboard loader changed; cannot preserve the "
                          "Claude Code context window. Update the plugin or use /obs context.")
    target = Path(transcript).resolve()
    native_load = ui_load.load_session

    def load_selected(path: str, format_hint: str = ""):
        if not path or Path(path).resolve() != target:
            return native_load(path, format_hint=format_hint)
        try:
            run = load_run(path, window_limit=window)
        except LoadError:
            # Keep TrajViz's format/read error classification and visible banners unchanged.
            return native_load(path, format_hint=format_hint)
        if format_hint in agent_trajviz.FORMATS and run.format != format_hint:
            return native_load(path, format_hint=format_hint)
        try:
            return session_from_run(run, path)
        except Exception as exc:  # noqa: BLE001 - same visible failure contract as the native loader
            return SessionLoadError(code="analysis_error", message=(
                f"The file loaded but could not be analysed ({type(exc).__name__}: {str(exc)[:160]})."))

    def clone(function: types.FunctionType, replacements: dict[str, Any]) -> types.FunctionType:
        copied = types.FunctionType(function.__code__, {**function.__globals__, **replacements},
                                    function.__name__, function.__defaults__, function.__closure__)
        copied.__kwdefaults__ = function.__kwdefaults__
        copied.__annotations__ = function.__annotations__
        return copied

    return clone(callback, {"do_load": clone(ui_load.do_load, {"load_session": load_selected})})


def dashboard_server_options(port: int | None) -> dict[str, Any]:
    """The native dashboard's login and upload cap, pinned to loopback for plugin-managed servers."""
    check_supported()
    from agent_trajviz import _serve

    return _serve.server_kwargs(host="127.0.0.1", port=port, share=False,
                                max_file_size=_serve.DEFAULT_MAX_FILE_SIZE, auth=_serve.auth_from_env())


@functools.lru_cache(maxsize=16)
def _attribution_reads_native_claude(parser: Callable) -> bool:
    """Probe DECAF's uncached parser without changing its reference root or reading a real trajectory.

    Some DECAF versions send every non-Pi JSONL file through their Codex adapter. They silently lose
    Claude's native ``tool_use`` blocks, then attribute missing test execution to the agent. A tiny native
    Read/Bash transcript must retain both tool identities and their canonical actions before that parser
    may diagnose this format. Cache by the actual function object so a replaced parser is checked again.
    """
    if not isinstance(parser, types.FunctionType):
        return False
    try:
        config = parser.__globals__["config"]
        with tempfile.TemporaryDirectory(prefix="trajviz-obs-attribution-probe-") as folder:
            root = Path(folder)
            path = root / "claude_code" / "native-probe.jsonl"
            path.parent.mkdir()
            records = [
                {"type": "user", "sessionId": "native-probe", "uuid": "user", "parentUuid": None,
                 "message": {"role": "user", "content": "Read the source and run its tests."}},
                {"type": "assistant", "sessionId": "native-probe", "uuid": "read", "parentUuid": "user",
                 "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "read-call",
                    "name": "Read", "input": {"file_path": "src/probe.py"}}]}},
                {"type": "user", "sessionId": "native-probe", "uuid": "read-result", "parentUuid": "read",
                 "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "read-call",
                    "content": "def probe(): pass", "is_error": False}]}},
                {"type": "assistant", "sessionId": "native-probe", "uuid": "test", "parentUuid": "read-result",
                 "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "test-call",
                    "name": "Bash", "input": {"command": "pytest -q tests/test_probe.py"}}]}},
                {"type": "user", "sessionId": "native-probe", "uuid": "test-result", "parentUuid": "test",
                 "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "test-call",
                    "content": "1 passed\nExit code 0", "is_error": False}]}},
            ]
            path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
            isolated_config = types.SimpleNamespace(**{**vars(config), "TRAJECTORY_DIR": root})
            isolated = types.FunctionType(parser.__code__, {**parser.__globals__, "config": isolated_config},
                                          parser.__name__, parser.__defaults__, parser.__closure__)
            isolated.__kwdefaults__ = parser.__kwdefaults__
            parsed = isolated("claude_code", "native-probe")
            calls = {(step.native_tool, step.canonical_action) for step in parsed.steps if step.type == "tool_call"}
            return {("Read", "read"), ("Bash", "run_test")} <= calls
    except Exception:  # noqa: BLE001 - an unverifiable parser must never support a capability verdict
        return False


def install_attribution_guard() -> None:
    """Protect CLI and native dashboard attribution from an incompatible optional DECAF parser.

    The original diagnosis retains all reference, byte identity and cache provenance gates. Only modern
    native Claude JSONL is refused when the actual DECAF parser cannot preserve its diagnostic evidence.
    """
    check_supported()
    from agent_trajviz.insight import attribution

    native = attribution.diagnose
    if getattr(native, "_trajviz_obs_native_guard", False):
        return

    @functools.wraps(native)
    def guarded(*, agent, instance_id, source_path=None, fmt=None, expected_sha=None, merged_sources=0,
                reference_root=None):
        if (attribution.DECAF_AVAILABLE and fmt == "claude_code" and source_path is not None
                and Path(source_path).suffix.lower() == ".jsonl"):
            try:
                from awe.adapters import _parse_normalized

                supported = _attribution_reads_native_claude(_parse_normalized)
            except Exception:  # noqa: BLE001 - optional module/API failure is unavailable, never blame
                supported = False
            if not supported:
                return attribution.AttributionResult(
                    False, mode="reference", agent=agent, instance_id=instance_id,
                    reason="This DECAF parser does not preserve native Claude Code JSONL Read/Bash tool evidence. "
                           "No capability verdict was issued. Update DECAF to a parser that supports native "
                           "Claude Code session transcripts.")
        return native(agent=agent, instance_id=instance_id, source_path=source_path, fmt=fmt,
                      expected_sha=expected_sha, merged_sources=merged_sources, reference_root=reference_root)

    guarded._trajviz_obs_native_guard = True
    attribution.diagnose = guarded


def llm_config_from_env(*, load_dotenv: bool = False) -> Any:
    """The LLM settings without importing the LLM's chart presenters.

    TrajViz 0.2's public ``llm`` module imports the issue presenter, which imports Pygments even when only
    ``[llm]`` was installed. Doctor must still describe that minimal environment, so use the same lightweight
    configuration functions its public wrapper calls, without loading the rendering stack.
    """
    check_supported()
    from agent_trajviz.insight.llm_config import load_env_files, resolve_analysis_config

    if load_dotenv:
        load_env_files()
    return resolve_analysis_config()


def llm_env_value(name: str) -> str:
    """An endpoint setting with the same scalar cleanup as TrajViz's analysis configuration."""
    import os

    check_supported()
    from agent_trajviz.insight.llm_config import _clean_env_scalar

    return _clean_env_scalar(os.environ.get(name, ""))


def protect_run_outputs(run: Trajectory, destinations: Iterable[Path]) -> None:
    """Protect the loaded transcript and its known sidecars from generated artifact paths.

    Use the selected input recorded at load time; an export's embedded ``source`` and ``_source_path`` may
    name unrelated files. Native Claude discovery also accepts the real session id's tree and old sibling
    agent logs, including when the parent transcript was renamed or reached through a symlink.
    """
    from .outputs import protect_inputs, session_files

    check_supported()
    source = input_path(run)
    if source is None:
        return
    actual = source.resolve()
    metadata = run.raw.get("metadata")
    session_id = metadata.get("session_id") if isinstance(metadata, dict) else None
    files = [source, *session_files(actual, session_id=session_id if run.format == "claude_code" else None)]
    if run.format == "claude_code" and not actual.name.startswith("agent-"):
        files.extend(path for path in actual.parent.glob("agent-*.jsonl")
                     if not path.is_symlink() and path.is_file())
    protect_inputs(destinations, files)


@contextlib.contextmanager
def labeler_run(run: Trajectory) -> Iterator[None]:
    """Label the exact normalized steps already loaded, without reopening a changing runtime transcript.

    TrajViz's labeler accepts only a file, and its older internal loader cannot read public trajectory
    exports. Injecting its step-loading boundary supports both immutable cached exports and live inputs.
    The sidecar still records the original transcript path passed to ``label_trajectory``.
    """
    check_supported()
    from agent_trajviz.labeling import labeler

    original = labeler.load_all_steps
    labeler.load_all_steps = lambda _path: copy.deepcopy(run.steps)
    try:
        yield
    finally:
        labeler.load_all_steps = original


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

    Count entry into the labeler's per-step classifier, so retries stay in one step even when a small
    content limit gives different steps identical prompt text. Step *i* is session mode's prompt *i*.
    Yields a list that, on exit,
    holds ``"prompt <i>: <error class>"`` for each step whose last call raised (the class only, as the labeler
    logs it: a provider error can echo credentials or prompt text).
    """
    check_supported()
    from agent_trajviz.labeling import labeler, llm

    original_call, original_classify = llm.call_llm, labeler._classify
    state = {"step": -1}
    failures: dict[int, str] = {}
    errors: list[str] = []

    @functools.wraps(original_classify)
    def classify(*args: Any, **kwargs: Any) -> Any:
        state["step"] += 1
        return original_classify(*args, **kwargs)

    def call_llm(*args: Any, **kwargs: Any) -> str:
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

    llm.call_llm, labeler._classify = call_llm, classify
    try:
        yield errors
    finally:
        llm.call_llm, labeler._classify = original_call, original_classify
        errors.extend(f"prompt {step}: {name}" for step, name in sorted(failures.items()))
