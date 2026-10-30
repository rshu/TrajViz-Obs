"""Version-checked compatibility shims for TrajViz internals.

The plugin ships against the published ``agent-trajviz>=0.3.1,<0.4``. These integrations need internal seams:

1. **The real context window everywhere.** ``context_pressure(window_limit=)`` and the ``context_*`` views take
   an override, but the issues, the brief and the export do not. :func:`load_run` writes the window
   into ``run.raw["metadata"]["context_window_limit"]`` before anything is computed, so every consumer measures
   against it (TrajViz then calls it ``declared``; :func:`window_info` reports ``override``).
2. **Session-mode labeling.** ``label_trajectory()`` has no ``chat=``; :func:`labeler_chat` patches the
   labeler's ``call_llm`` (and its retry sleep) for the duration of a capture or replay.
3. **A step cap on env-mode labeling.** ``label_trajectory()`` labels every step; :func:`labeler_step_cap`
   wraps its real ``call_llm`` so only the first steps reach the endpoint, as session mode labels them.
4. **Native Claude attribution evidence.** :func:`install_attribution_guard` refuses capability blame when
   the optional attribution parser cannot preserve native JSONL Read/Bash calls in an isolated capability probe.
5. **Local command display records.** Native conversion skips known command UI text, retaining source
   positions and tool results so inspecting a session does not become part of its work trajectory.
6. **OpenCode 2 records.** The versioned capture adapter normalizes the native message schema and
   retains explicit skill, timing and compaction observations without changing the original source.

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
import math
import os
import re
import sys
import tempfile
import types
import weakref
from collections.abc import Callable, Iterable, Iterator
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import agent_trajviz
from agent_trajviz import LoadError, Trajectory

from .errors import BridgeError
from .native_quality import project_analysis, project_brief, project_view  # noqa: F401 - shared projection API
from .native_records import normalize_native_records

#: The installed TrajViz version.
TRAJVIZ_VERSION: str = agent_trajviz.__version__

#: Supported releases: ``>=0.3.1,<0.4`` as ``[lowest, first unsupported)``.
_SUPPORTED = ((0, 3, 1), (0, 4, 0))

#: ``LoadError`` codes that mean "no assistant step yet" for the session being written.
_EMPTY_CODES = frozenset({"unknown_format", "no_steps"})

#: Runs whose window :func:`load_run` set, with that window.
_OVERRIDES: weakref.WeakKeyDictionary[Trajectory, int] = weakref.WeakKeyDictionary()

# A reloadable export keeps its original provenance in ``run.source``. That value is content, not the
# path opened by this process, so it must never decide which input an artifact writer may replace.
_INPUT_PATHS: weakref.WeakKeyDictionary[Trajectory, Path] = weakref.WeakKeyDictionary()
_INPUT_FILES: weakref.WeakKeyDictionary[Trajectory, tuple[Path, ...]] = weakref.WeakKeyDictionary()
_CONSUMED_INPUTS: ContextVar[list[Path] | None] = ContextVar("trajviz_obs_consumed_inputs", default=None)


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
    """Refuse a TrajViz outside ``>=0.3.1,<0.4``, whose internals the shims were not written against."""
    release = _release(TRAJVIZ_VERSION)
    if release is None or not _SUPPORTED[0] <= release < _SUPPORTED[1]:
        raise BridgeError("unsupported_trajviz",
                          f"agent-trajviz {TRAJVIZ_VERSION} is installed; this bridge needs >=0.3.1,<0.4.",
                          version=TRAJVIZ_VERSION, required=">=0.3.1,<0.4")


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
    _install_native_record_filter()
    from .opencode import install as install_opencode

    install_opencode()
    _install_dsh_input_tracking()
    from .pi import install as install_pi

    install_pi()
    from .archive import install as install_archive

    install_archive()
    return list(loaded)


def _install_dsh_input_tracking() -> None:
    """Capture only files the native converter actually validated and merged."""
    from agent_trajviz.insight import loaders

    original = loaders._convert_dsh_to_internal
    if getattr(original, "_trajviz_obs_input_tracking", False):
        return
    if not {"events", "source_path", "child_event_lists"} <= inspect.signature(original).parameters.keys():
        raise BridgeError("unsupported_trajviz", "TrajViz's DeepSeek converter changed; update the plugin.")

    @functools.wraps(original)
    def convert(events, *, source_path=None, child_event_lists=None, child_warnings=None):
        result = original(events, source_path=source_path, child_event_lists=child_event_lists,
                          child_warnings=child_warnings)
        consumed = _CONSUMED_INPUTS.get()
        if consumed is not None and source_path:
            consumed.append(Path(source_path).absolute())
            consumed.extend(Path(path).absolute() for _, path in result.get("_dsh_merged_sources", []) if path)
        return result

    convert._trajviz_obs_input_tracking = True
    loaders._convert_dsh_to_internal = convert


def _install_native_record_filter() -> None:
    """Normalize at the shared native conversion seam, including dashboard uploads and ZIPs."""
    check_supported()
    from agent_trajviz.insight import loaders

    original = getattr(loaders, "convert_claude_code_events", None)
    if getattr(original, "_trajviz_obs_ui_filter", False):
        return
    required = {"events", "source_path", "child_event_lists", "child_warnings"}
    try:
        parameters = inspect.signature(original).parameters if callable(original) else {}
    except (TypeError, ValueError):
        parameters = {}
    if not required <= parameters.keys():
        raise BridgeError("unsupported_trajviz", "TrajViz's native Claude Code converter changed; "
                          "update the plugin before analyzing this session.")

    @functools.wraps(original)
    def convert(events, *, source_path=None, child_event_lists=None, child_warnings=None):
        if not isinstance(events, list):
            return original(events, source_path=source_path, child_event_lists=child_event_lists,
                            child_warnings=child_warnings)
        parent, ignored = normalize_native_records(events)
        children = {}
        for agent, bundle in (child_event_lists or {}).items():
            records = bundle.get("events") if isinstance(bundle, dict) else bundle
            if not isinstance(records, list):
                children[agent] = bundle
                continue
            records, child_ignored = normalize_native_records(records)
            ignored += child_ignored
            children[agent] = {**bundle, "events": records} if isinstance(bundle, dict) else records
        result = original(parent, source_path=source_path, child_event_lists=children,
                          child_warnings=child_warnings)
        if ignored:
            result["_trajviz_obs_ignored_ui_records"] = ignored
        return result

    convert._trajviz_obs_ui_filter = True
    loaders.convert_claude_code_events = convert


def load_run(path: str, *, window_limit: int | None = None, current: bool = False,
             session_id: str | None = None) -> Trajectory:
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
    captured = _CONSUMED_INPUTS.set([])
    try:
        try:
            run = agent_trajviz.load(path, **({"session_id": session_id} if session_id is not None else {}))
        except LoadError as exc:
            if current and exc.code in _EMPTY_CODES:
                raise EmptySession(str(exc), path=exc.path or path) from exc
            raise
    finally:
        consumed = tuple(_CONSUMED_INPUTS.get() or ())
        _CONSUMED_INPUTS.reset(captured)
    if current and not any(step.get("role") == "assistant" for step in run.steps):
        raise EmptySession("The current session has no assistant step yet.", path=path)
    _INPUT_PATHS[run] = Path(path).absolute() if run.format == "dsh" and Path(path).is_dir() else selected
    if run.format == "otlp":
        _INPUT_FILES[run] = tuple(dict.fromkeys(_otlp_input_files(selected, run.raw)))
    elif run.format == "dsh":
        _INPUT_FILES[run] = tuple(dict.fromkeys([_INPUT_PATHS[run], *consumed]))
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
    from agent_trajviz.insight.formats.dsh_native import resolve_native_dsh_path
    from agent_trajviz.insight.formats.otlp_files import looks_like_otlp_source

    if Path(path).is_dir() and looks_like_otlp_source(path):
        return Path(path).absolute()
    native = resolve_native_dsh_path(path)
    if native:
        if Path(native).name != "session.jsonl":
            return Path(path).absolute()
        try:
            with open(native, "rb") as handle:
                header = json.loads(handle.readline(4096).decode("utf-8-sig"))
            from agent_trajviz.insight.loaders import detect_format

            if detect_format([header]) == "dsh":
                return Path(path).absolute()
        except (OSError, ValueError, UnicodeError):
            pass
    selected, _ = resolve_session_path(path)
    return Path(selected).absolute()


def _otlp_input_files(source: Path, raw: dict) -> list[Path]:
    """Actual capture inputs, excluding paths claimed by an exported trajectory."""
    files = [source]
    candidates = ([source / name for name in ("manifest.json", "otlp-manifest.json")
                   if (source / name).is_file()] if source.is_dir() else [source])
    if len(candidates) != 1 or candidates[0].suffix.lower() == ".zip":
        return files
    manifest_path = candidates[0]
    try:
        with manifest_path.open("rb") as handle:
            data = handle.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            return files
        document = json.loads(data.decode("utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        return files
    if not isinstance(document, dict) or document.get("type") != "agent-trajviz-otlp-bundle":
        return files
    files.append(manifest_path)
    # Use the loaded manifest's member names, not absolute source_files labels.
    # A normalized export can retain an unrelated capture's provenance, but
    # its actual file above is not a manifest and never enters this branch.
    manifest = raw.get("_otel", {}).get("source", {}).get("manifest", {})
    if not isinstance(manifest, dict):
        return files
    for name in [*manifest.get("traces", []), *manifest.get("logs", [])]:
        if not isinstance(name, str) or not name or any(char in name for char in ("\\", "\x00", ":")):
            continue
        member = Path(name)
        if member.is_absolute() or any(part in {"", ".", ".."} for part in name.split("/")):
            continue
        files.append(manifest_path.parent / member)
    return files


def input_path(run: Trajectory) -> Path | None:
    """The file actually opened by :func:`load_run`, independent of an export's embedded provenance.

    Runs reconstructed from a saved current-session request use the original transcript assigned by the
    bridge. Other directly constructed trajectories retain their caller-supplied source as a fallback.
    """
    return _INPUT_PATHS.get(run) or (Path(run.source) if run.source else None)


def input_files(run: Trajectory) -> tuple[Path, ...]:
    """Actual protected inputs; exported provenance cannot name unrelated files."""
    from .outputs import session_files

    if run in _INPUT_FILES:
        return _INPUT_FILES[run]
    source = input_path(run)
    if source is None:
        return ()
    actual = source.resolve()
    if run.format == "otlp":
        files = [source]
        if source.is_dir():
            # Some callers retain a validated directory capture after its
            # manifest is removed. Only local regular members of that actual
            # input directory can be recovered from loader-produced labels.
            labels = run.raw.get("_otel", {}).get("source", {}).get("source_files", [])
            for label in labels[:256] if isinstance(labels, list) else []:
                if not isinstance(label, str):
                    continue
                member = Path(label)
                if not member.is_absolute():
                    member = source / member
                if not member.is_relative_to(source) or not member.resolve().is_relative_to(actual):
                    continue
                relative = member.relative_to(source)
                if any(source.joinpath(*relative.parts[:index]).is_symlink()
                       for index in range(1, len(relative.parts) + 1)):
                    continue
                if member.is_file():
                    files.append(member)
        return tuple(dict.fromkeys(files))
    metadata = run.raw.get("metadata")
    session_id = metadata.get("session_id") if isinstance(metadata, dict) else None
    files = [source, *session_files(actual, session_id=session_id if run.format == "claude_code" else None)]
    if run.format == "claude_code" and not actual.name.startswith("agent-"):
        files.extend(path for path in actual.parent.glob("agent-*.jsonl")
                     if not path.is_symlink() and path.is_file())
    return tuple(dict.fromkeys(files))


def window_info(run: Trajectory) -> dict[str, Any]:
    """``{"window", "window_source", "window_note"}``: ``override`` when :func:`load_run` set it, else TrajViz's."""
    if run in _OVERRIDES:
        return {"window": _OVERRIDES[run], "window_source": "override", "window_note": ""}
    stats = run.context_pressure()["stats"]
    return {"window": stats.get("window_limit"), "window_source": stats.get("window_source"),
            "window_note": stats.get("window_note") or ""}


def dashboard_window_callback(callback: Callable, transcript: str, window: int | None, *,
                              session_id: str | None = None, display_path: str | None = None) -> Callable:
    """Give one Insight load callback defaults for its preloaded source only.

    TrajViz does not expose a dashboard loader option. Clone the two small function objects that
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
                          "selected session context. Update the plugin or use /obs context.")
    target = Path(display_path or transcript).resolve()
    native_load = ui_load.load_session
    default_session_id = session_id

    def load_selected(path: str, format_hint: str = "", *, session_id: str | None = None):
        if not path or Path(path).resolve() != target:
            return native_load(path, format_hint=format_hint, **({"session_id": session_id} if session_id else {}))
        selected_session_id = session_id or default_session_id
        try:
            run = load_run(transcript, window_limit=window, session_id=selected_session_id)
        except LoadError:
            # Keep TrajViz's format/read error classification and visible banners unchanged.
            return native_load(transcript, format_hint=format_hint,
                               **({"session_id": selected_session_id} if selected_session_id else {}))
        if format_hint in agent_trajviz.FORMATS and run.format != format_hint:
            return native_load(transcript, format_hint=format_hint,
                               **({"session_id": selected_session_id} if selected_session_id else {}))
        try:
            return session_from_run(run, transcript)
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
    """Probe the uncached attribution parser without changing its reference root or reading a real trajectory.

    Some attribution parser versions send every non-Pi JSONL file through their Codex adapter. They silently lose
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


def configure_attribution_environment() -> None:
    """Pass the public parser checkout setting to the upstream discovery API before it imports."""
    configured = os.environ.get("TRAJVIZ_ATTRIBUTION_PATH", "").strip()
    if configured:
        os.environ["AWE_DECAF_PATH"] = configured


def attribution_message(text: str | None) -> str | None:
    """Use public attribution terminology without rewriting paths or upstream API identifiers."""
    if text is None:
        return None
    text = re.sub(r"(?<![\w/\\.-])AWE_DECAF_PATH(?![\w/\\-]|\.[\w])",
                  "TRAJVIZ_ATTRIBUTION_PATH", text)
    unavailable = re.fullmatch(
        r"(?:DECAF unavailable \()?DECAF (import|configuration) failed \((\w+)\)\)?", text,
        flags=re.IGNORECASE,
    )
    if unavailable:
        phase, error = unavailable.groups()
        if phase.lower() == "import":
            return (f"Attribution parser unavailable ({error}). Set TRAJVIZ_ATTRIBUTION_PATH to its checkout "
                    "and TRAJVIZ_REFERENCE_ROOT to the reference data.")
        return (f"Attribution parser configuration failed ({error}). Check TRAJVIZ_ATTRIBUTION_PATH "
                "and TRAJVIZ_REFERENCE_ROOT.")
    text = re.sub(r"(?<![\w/\\.-])decaf(?![\w/\\-]|\.[\w])", "attribution", text, flags=re.IGNORECASE)
    text = re.sub(r"\b([Aa]) attribution\b", lambda match: match[1] + "n attribution", text)
    return re.sub(r"(^|[.!?]\s+|>\s*)attribution\b", lambda match: match[1] + "Attribution", text)


def install_attribution_guard() -> None:
    """Protect CLI and native dashboard attribution from an incompatible optional parser.

    The original diagnosis retains all reference, byte identity and cache provenance gates. Native
    Claude JSONL and OpenCode 2 are refused when the parser cannot preserve their diagnostic evidence.
    """
    check_supported()
    configure_attribution_environment()
    from agent_trajviz.insight import attribution

    native = attribution.diagnose
    if getattr(native, "_trajviz_obs_native_guard", False):
        return

    @functools.wraps(native)
    def guarded(*, agent, instance_id, source_path=None, fmt=None, expected_sha=None, merged_sources=0,
                reference_root=None):
        if attribution.DECAF_AVAILABLE and fmt == "pi" and source_path is not None:
            from .pi import excludes_branch

            if excludes_branch(source_path):
                return attribution.AttributionResult(False, mode="reference", agent=agent, instance_id=instance_id,
                    reason="Attribution cannot currently verify the selected active branch of this Pi session. "
                           "No capability verdict was issued.")
        if attribution.DECAF_AVAILABLE and fmt == "opencode" and source_path is not None:
            from .opencode import attribution_source, attribution_supported

            source = attribution_source(source_path)
            if source is not None:
                reason = None
                if source.get("children") or Path(source_path).suffix.lower() == ".zip":
                    reason = ("Attribution cannot currently certify an OpenCode 2 subagent family or ZIP bundle. "
                              "No capability verdict was issued; use a supported single-session native export.")
                else:
                    try:
                        from awe.adapters import _parse_normalized

                        supported = attribution_supported(_parse_normalized, source.get("source_format") == "opencode_v2")
                    except Exception:  # noqa: BLE001 - optional parser failure is unavailable, never blame
                        supported = False
                    if not supported:
                        reason = ("This attribution parser does not preserve native OpenCode 2 read/shell tool "
                                  "evidence. No capability verdict was issued. Update the attribution parser "
                                  "to one that supports OpenCode 2 session exports.")
                if reason:
                    return attribution.AttributionResult(False, mode="reference", agent=agent,
                                                         instance_id=instance_id, reason=reason)
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
                    reason="This attribution parser does not preserve native Claude Code JSONL Read/Bash tool evidence. "
                           "No capability verdict was issued. Update the attribution parser to one that supports native "
                           "Claude Code session transcripts.")
        result = native(agent=agent, instance_id=instance_id, source_path=source_path, fmt=fmt,
                        expected_sha=expected_sha, merged_sources=merged_sources, reference_root=reference_root)
        if result.available and not _finite_attribution(result):
            return attribution.AttributionResult(False, mode=result.mode, agent=agent, instance_id=instance_id,
                reason="Attribution returned non-finite numeric evidence. No capability verdict was issued.")
        reason = attribution_message(result.reason)
        notes = [attribution_message(note) for note in result.notes]
        if reason != result.reason or notes != result.notes:
            result = copy.copy(result)
            result.reason, result.notes = reason, notes
        return result

    guarded._trajviz_obs_native_guard = True
    attribution.diagnose = guarded


def _finite_attribution(result) -> bool:
    """Invalid optional-backend numbers cannot become capability grades or JSON NaN."""
    from dataclasses import asdict

    def finite(value):
        if isinstance(value, float):
            return math.isfinite(value)
        if isinstance(value, dict):
            return all(finite(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return all(finite(item) for item in value)
        return True

    return finite(asdict(result))


def llm_config_from_env(*, load_dotenv: bool = False) -> Any:
    """The LLM settings without importing the LLM's chart presenters.

    TrajViz's public ``llm`` module imports the issue presenter, which imports Pygments even when only
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
    from .outputs import protect_inputs

    check_supported()
    protect_inputs(destinations, input_files(run))


@contextlib.contextmanager
def labeler_run(run: Trajectory) -> Iterator[None]:
    """Label the exact normalized steps already loaded, without reopening a changing runtime transcript.

    TrajViz's labeler accepts only a file, and its older internal loader cannot read public trajectory
    exports. Injecting its step-loading boundary supports both immutable cached exports and live inputs.
    The sidecar still records the original transcript path passed to ``label_trajectory``.
    """
    check_supported()
    from agent_trajviz.labeling import labeler

    from .label_quality import annotate_label

    original, serialize = labeler.load_all_steps, labeler._serialize_step
    if set(inspect.signature(serialize).parameters) != {"step", "fallback_index", "label", "label_source"}:
        raise BridgeError("unsupported_trajviz", "TrajViz's label serializer changed; update the plugin.")

    @functools.wraps(serialize)
    def serialize_step(step, *, fallback_index, label, label_source):
        return annotate_label(step, serialize(step, fallback_index=fallback_index, label=label,
                                              label_source=label_source))

    labeler.load_all_steps = lambda _path: copy.deepcopy(run.steps)
    labeler._serialize_step = serialize_step
    try:
        yield
    finally:
        labeler.load_all_steps = original
        labeler._serialize_step = serialize


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
