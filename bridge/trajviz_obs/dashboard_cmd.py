"""``dashboard`` and ``converge``: TrajViz's Insight and Converge dashboards on 127.0.0.1.

Both print ``{"url": …}`` as their one stdout line once the server is up, then serve until terminated. Claude
Code's process API starts children with stdin already closed, so EOF must not stop a managed dashboard.
Standalone pipe owners can opt into EOF shutdown with ``--exit-on-stdin``. The Insight dashboard opens with the session already
loaded: a page-load event sets the trajectory file control to the transcript and clicks the dashboard's own
Load button. Files keep their real paths; directories use an explicit manifest or private display export
whose callbacks read the original source. A missing ``app`` extra is reported as
``missing_extra``, and a ``--port`` already in use as ``port_busy`` (checked before the app is built), before
anything else is printed.
"""

from __future__ import annotations

import argparse
import contextlib
import inspect
import json
import signal
import socket
import sys
import tempfile
import threading
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

from agent_trajviz import LoadError

from .cli import register
from .compat import (
    dashboard_server_options,
    dashboard_window_callback,
    input_path,
    install_attribution_guard,
    load_plugins,
    load_run,
    resolve_input_path,
)
from .dashboard_ui import decorate_dashboard, presentation
from .errors import BridgeError
from .sessions import _positive_int, capture_session_id

if TYPE_CHECKING:
    import gradio as gr

#: Clicks the Insight dashboard's Load button 1500 ms after the page loads (the button's id is TrajViz's own),
#: unless it was clicked already: TrajViz re-clicks it itself when the file control changes, and a second click
#: would run the whole load (report, sidebar, attribution) twice.
PRELOAD_JS = (
    "() => { let clicked = false;"
    " document.addEventListener('click', (e) => {"
    " if (e.target instanceof Element && e.target.closest('#tv-load-btn')) clicked = true; }, true);"
    " setTimeout(() => { if (clicked) return;"
    " const b = document.querySelector('#tv-load-btn button, #tv-load-btn'); if (b) b.click(); }, 1500); }"
)


def _dashboard() -> ModuleType:
    """``agent_trajviz.dashboard``; its import raises ``MissingExtraError`` without the ``app`` extra."""
    install_attribution_guard()
    from agent_trajviz import dashboard

    return dashboard


def _trajectory_file(app: gr.Blocks) -> gr.File | None:
    import gradio as gr

    for block in app.blocks.values():
        if isinstance(block, gr.File) and str(block.label or "").startswith("Trajectory"):
            return block
    return None


def _add_preload(app: gr.Blocks, transcript_path: str) -> bool:
    """Add the page-load preload of *transcript_path* to *app*; ``False`` when the file control is not found."""
    import gradio as gr

    file = _trajectory_file(app)
    if file is None:
        return False
    # Without this Gradio hands TrajViz a copy in its cache folder, where the sub-agent logs are not.
    gr.set_static_paths([transcript_path])
    with app:
        app.load(fn=lambda: transcript_path, outputs=[file], js=PRELOAD_JS)
    return True


def _apply_context_window(app: gr.Blocks, transcript_path: str, window: int | None, *,
                          session_id: str | None = None, display_path: str | None = None,
                          force: bool = False) -> None:
    """Adapt the app's own loader only for its selected transcript, before it computes any views."""
    if window is None and session_id is None and display_path is None and not force:
        return
    callbacks = [block for block in app.fns.values()
                 if getattr(block.fn, "__module__", "") == "agent_trajviz.insight.ui.load"
                 and getattr(block.fn, "__name__", "") == "_do_load"]
    if len(callbacks) != 1:
        raise BridgeError("dashboard_incompatible", "TrajViz's dashboard loader changed; cannot preserve the "
                          "Claude Code context window. Update the plugin or use /obs context.")
    callbacks[0].fn = dashboard_window_callback(callbacks[0].fn, transcript_path, window,
                                               session_id=session_id, display_path=display_path)


def _capture_session(run) -> str | None:
    native = run.raw.get("metadata") or {}
    telemetry = run.raw.get("_otel") or {}
    return telemetry.get("session_id") or native.get("session_id") or next(
        (step.get("session_id") for step in run.steps if not step.get("is_sub_agent") and step.get("session_id")), None)


def _upload_path(upload_obj) -> str | None:
    if isinstance(upload_obj, str):
        return upload_obj
    return upload_obj.get("path") if isinstance(upload_obj, dict) else getattr(upload_obj, "name", None)


def _apply_capture_default(app: gr.Blocks, transcript_path: str, display_path: str, session_id: str | None) -> None:
    if session_id is None and display_path == transcript_path:
        return
    callbacks = [block for block in app.fns.values()
                 if getattr(block.fn, "__module__", "") == "agent_trajviz.insight.ui.load"
                 and getattr(block.fn, "__name__", "") == "_inspect_sessions"]
    if len(callbacks) != 1:
        raise BridgeError("dashboard_incompatible", "TrajViz's capture selector changed; cannot preload this session.")
    original = callbacks[0].fn
    target = Path(display_path).resolve()

    def inspect_sessions(upload_obj):
        path = _upload_path(upload_obj)
        if not path or Path(path).resolve() != target:
            return original(upload_obj)
        result = original(transcript_path)
        choices = result.get("choices", []) if isinstance(result, dict) else []
        identities = {choice[1] if isinstance(choice, (tuple, list)) else choice for choice in choices}
        return {**result, "value": session_id} if session_id is not None and session_id in identities else result

    callbacks[0].fn = inspect_sessions


def _cached_label_notice(transcript_path: str, labels_path: str, *, session_id: str | None = None, run=None) -> str | None:
    """An old native label layout must not silently describe different normalized steps.

    Labels may cover only a prefix or assistant steps, so total counts are not an identity check.
    Every format and normalized export uses the same identity checks.
    """
    notice = "Cached labels use an earlier step layout. Run /obs label again to refresh them."
    try:
        run = run if run is not None else load_run(transcript_path, **({"session_id": session_id} if session_id else {}))
        if session_id is not None and _capture_session(run) != session_id:
            return notice
        document = json.loads(Path(labels_path).read_text(encoding="utf-8"))
        if not isinstance(document, dict) or not isinstance(document.get("steps"), list):
            return notice
        for label in document["steps"]:
            if not isinstance(label, dict):
                return notice
            index = label.get("index")
            if type(index) is not int or not 0 <= index < len(run.steps):
                return notice
            step = run.steps[index]
            if (type(label.get("raw_index")) is not int or label["raw_index"] != step.get("raw_index")
                    or label.get("role") != step.get("role")
                    or label.get("agent", label.get("executor_id", "")) != (step.get("agent") or "")):
                return notice
            for key in ("session_id", "time_created_ms"):
                if label.get(key) not in (None, "") and label[key] != step.get(key):
                    return notice
    except (OSError, ValueError, KeyError, TypeError, LoadError, BridgeError):
        return "Cached labels could not be restored. Run /obs label again, or load a labels file manually."
    return None


def _add_labels_preload(app: gr.Blocks, transcript_path: str, labels_path: str, *,
                        session_id: str | None = None, display_path: str | None = None) -> bool:
    """Restore labels through the native reset/change handlers, after its normal trajectory reset.

    The reset runs after loading and sees the actual trajectory state. Source and session identity
    must match before supplying the sidecar to the existing five-chart label handler.
    """
    import gradio as gr
    from agent_trajviz import Trajectory

    trajectory_file = _trajectory_file(app)
    label_files = [block for block in app.blocks.values()
                   if isinstance(block, gr.File) and block.label == "Labels (optional)"]
    if trajectory_file is None or len(label_files) != 1:
        return False
    labels_file = label_files[0]
    callbacks = [block for block in app.fns.values()
                 if getattr(block.fn, "__module__", "") == "agent_trajviz.insight.ui.overview_tab"
                 and getattr(block.fn, "__name__", "") == "_reset_labels"
                 and not block.inputs and block.outputs and block.outputs[-1] is labels_file]
    if len(callbacks) != 1 or inspect.signature(callbacks[0].fn).parameters:
        return False
    loaders = [block for block in app.fns.values() if getattr(block.fn, "__name__", "") == "_do_load"]
    if len(loaders) != 1:
        return False
    loader = loaders[0]
    slots = inspect.getclosurevars(loader.fn).nonlocals.get("slots")
    state_run = slots.get("state_run") if isinstance(slots, dict) else None
    selectors = [block for block in loader.inputs if isinstance(block, gr.Dropdown) and block.label == "Captured session"]
    if not isinstance(state_run, gr.State) or state_run not in loader.outputs or len(selectors) != 1:
        return False
    callback = callbacks[0]
    reset = callback.fn
    selected = Path(display_path or transcript_path).resolve()
    source = Path(transcript_path).resolve()
    try:
        initial = load_run(transcript_path, **({"session_id": session_id} if session_id else {}))
    except LoadError as exc:
        if session_id is None and exc.code == "ambiguous_session":
            return False
        raise
    expected_session = session_id or _capture_session(initial)

    def reset_labels(upload_obj, captured_session=None, loaded_run=None):
        path = _upload_path(upload_obj)
        actual_source = input_path(loaded_run) if isinstance(loaded_run, Trajectory) else None
        same_run = (bool(path) and Path(path).resolve() == selected and actual_source is not None
                    and actual_source.resolve() == source and _capture_session(loaded_run) == expected_session
                    and (not captured_session or captured_session == expected_session))
        outputs = list(reset())
        notice = _cached_label_notice(transcript_path, labels_path, session_id=expected_session,
                                      run=loaded_run) if same_run else None
        outputs[-1] = gr.update(value=labels_path if same_run and notice is None else None)
        if notice:
            outputs[1] = f"<div style='padding:1em;color:var(--ov-muted);'>{notice}</div>"
        return tuple(outputs)

    gr.set_static_paths([labels_path])
    callback.fn = reset_labels
    callback.inputs = [trajectory_file, selectors[0], state_run]
    callback.targets = [(None, "then")]
    callback.trigger_after = loader._id
    return True


def _display_source(transcript_path: str, session_id: str | None, display_path: str | None):
    """A Gradio-compatible file; analysis still reads the original selected source."""
    source = Path(transcript_path).absolute()
    if display_path is not None:
        display = Path(display_path).absolute()
        if not display.is_file():
            raise BridgeError("not_found", f"No such dashboard display file: {display}", path=str(display))
        return str(display), None
    if source.is_file():
        if session_id is not None:
            load_run(str(source), session_id=session_id)
        return str(source), None
    if not source.is_dir():
        raise BridgeError("not_found", f"No such transcript: {source}", path=str(source))
    manifests = [source / name for name in ("manifest.json", "otlp-manifest.json") if (source / name).is_file()]
    if session_id is None and len(manifests) == 1:
        from agent_trajviz import list_sessions

        try:
            if list_sessions(str(source)):
                return str(manifests[0]), None
        except LoadError:
            pass
    run = load_run(str(source), session_id=session_id)
    if run.format == "otlp" and len(manifests) == 1:
        return str(manifests[0]), None
    temporary = tempfile.TemporaryDirectory(prefix="trajviz-obs-dashboard-")
    try:
        display = Path(temporary.name) / "trajectory.json"
        display.write_text(json.dumps(run.to_dict(include_raw=True), allow_nan=False), encoding="utf-8")
        return str(display), temporary
    except BaseException:
        temporary.cleanup()
        raise


def build_preloaded_insight(transcript_path: str, *, window: int | None = None,
                            labels_path: str | None = None, session_id: str | None = None,
                            display_path: str | None = None) -> gr.Blocks:
    """The Insight dashboard that loads *transcript_path* when the page opens (unchanged if that cannot be wired)."""
    display, temporary = _display_source(transcript_path, session_id, display_path)
    alias = display if Path(display).resolve() != Path(transcript_path).resolve() else None
    try:
        app = _dashboard().insight_app()
        from .label_quality import install_chart

        install_chart()
        _apply_context_window(app, transcript_path, window, session_id=session_id, display_path=alias,
                              force=labels_path is not None)
        _apply_capture_default(app, transcript_path, display, session_id)
        preloaded = _add_preload(app, display)
        labels_preloaded = labels_path is not None and _add_labels_preload(app, transcript_path, labels_path,
            session_id=session_id, display_path=display)
        page = decorate_dashboard(app, "insight")
        page._obs_display_path, page._obs_display_owner = display, temporary
        page._obs_preloaded, page._obs_labels_preloaded = preloaded, labels_preloaded
        return page
    except BaseException:
        if temporary is not None:
            temporary.cleanup()
        raise


def _check_port(port: int | None) -> None:
    """Raise ``BridgeError("port_busy")`` when 127.0.0.1 cannot bind *port*; ``None`` (Gradio picks one) passes.

    Run before the app is built, so no other ``OSError`` is reported as the port. The probe binds with
    ``SO_REUSEADDR`` as Gradio's own check does, so a port whose old connections are still closing counts as free.
    """
    if port is None:
        return
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise BridgeError("port_busy", f"Port {port} on 127.0.0.1 is in use or cannot be bound; pass another "
                              "--port, or none to take a free one.", port=port) from exc


def _announce(url: str, **extra: object) -> None:
    sys.stdout.write(json.dumps({"url": url, **extra}) + "\n")
    sys.stdout.flush()


def _wait_for_shutdown(*, exit_on_stdin: bool) -> None:
    """Keep a managed child alive even with DEVNULL stdin; close its app cleanly on SIGTERM or Ctrl-C."""
    if exit_on_stdin:
        if sys.stdin is not None:
            for _ in sys.stdin:
                pass
        return
    stopped = threading.Event()
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        for sig in previous:
            signal.signal(sig, lambda _signum, _frame: stopped.set())
        stopped.wait()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def serve_insight(transcript_path: str, port: int | None, *, exit_on_stdin: bool = False,
                  window: int | None = None, labels_path: str | None = None, session_id: str | None = None,
                  display_path: str | None = None) -> None:
    """Serve the preloaded Insight dashboard until terminated; ``{"url"}`` is the first stdout line.

    When the preload cannot be wired (a TrajViz release changed the upload row) the dashboard still starts and
    the line carries ``"preloaded": false``.
    """
    _check_port(port)
    dashboard = _dashboard()
    app = build_preloaded_insight(transcript_path, window=window, labels_path=labels_path,
                                  session_id=session_id, display_path=display_path)
    try:
        with contextlib.redirect_stdout(sys.stderr):  # Gradio's prints: the URL stays the first stdout line
            _, url, _ = app.launch(**dashboard_server_options(port), prevent_thread_lock=True, quiet=True,
                                   allowed_paths=[app._obs_display_path, *([labels_path] if labels_path is not None else [])],
                                   **presentation("insight", dashboard.insight_presentation()))
        _announce(url, **({} if app._obs_preloaded else {"preloaded": False}),
                  **({"labels_preloaded": app._obs_labels_preloaded} if labels_path is not None else {}))
        _wait_for_shutdown(exit_on_stdin=exit_on_stdin)
    finally:
        try:
            with contextlib.redirect_stdout(sys.stderr):
                app.close()
        finally:
            if app._obs_display_owner is not None:
                app._obs_display_owner.cleanup()


def serve_converge(data_root: str, port: int | None, *, exit_on_stdin: bool = False) -> None:
    """Serve the Converge dashboard over *data_root* until terminated; ``{"url"}`` is the first stdout line."""
    _check_port(port)
    dashboard = _dashboard()
    app = decorate_dashboard(dashboard.converge_app(data_root=data_root), "converge")
    try:
        with contextlib.redirect_stdout(sys.stderr):  # Gradio's prints: the URL stays the first stdout line
            _, url, _ = app.launch(**dashboard_server_options(port),
                                   prevent_thread_lock=True, quiet=True,
                                   **presentation("converge", dashboard.converge_presentation()))
        _announce(url)
        _wait_for_shutdown(exit_on_stdin=exit_on_stdin)
    finally:
        with contextlib.redirect_stdout(sys.stderr):
            app.close()


def _port(text: str) -> int:
    """``--port`` (an argparse type): a TCP port number from 1 to 65535."""
    try:
        value = int(text)
    except ValueError:
        value = 0
    if not 1 <= value <= 65535:
        raise argparse.ArgumentTypeError(f"expected a port number from 1 to 65535, got {text!r}")
    return value


def add_parser(sub: argparse._SubParsersAction) -> None:
    port_help = "The port, 1 to 65535 (default: Gradio's first free one from 7860)."
    stdin_help = "Stop when stdin closes (standalone pipe use; default: serve until terminated)."
    p = sub.add_parser("dashboard", help="The Insight dashboard with the session preloaded, until terminated.")
    p.add_argument("--transcript", required=True, help="The session transcript.")
    p.add_argument("--window", type=_positive_int, default=None, help="The preloaded session's context window.")
    p.add_argument("--session-id", type=capture_session_id, help="The exact session inside a saved OTLP capture.")
    p.add_argument("--labels", type=Path, default=None, help="A labels sidecar to preload for this session.")
    p.add_argument("--port", type=_port, default=None, help=port_help)
    p.add_argument("--exit-on-stdin", action="store_true", help=stdin_help)

    p = sub.add_parser("converge", help="The Converge comparison dashboard, until terminated.")
    p.add_argument("--data-root", required=True, help="The folder batch manifests may name files in.")
    p.add_argument("--port", type=_port, default=None, help=port_help)
    p.add_argument("--exit-on-stdin", action="store_true", help=stdin_help)


@register("dashboard")
def _serve_dashboard(args: argparse.Namespace) -> None:
    _dashboard()
    # The same directory selector accepted by /obs and /obs view must preload
    # its real parent file, so Gradio keeps its sibling subagent discovery.
    path = resolve_input_path(args.transcript)
    if not path.exists():
        raise BridgeError("not_found", f"No such transcript: {path}", path=str(path))
    load_plugins()  # as TrajViz's own dashboard command does on start, so the app reads plugins' formats
    try:
        load_run(str(path), session_id=args.session_id)
    except LoadError as exc:
        if args.session_id is not None or exc.code != "ambiguous_session":
            raise
    labels = args.labels.resolve() if args.labels is not None else None
    if labels is not None:
        if not labels.is_file():
            raise BridgeError("not_found", f"No such labels sidecar: {labels}", path=str(labels))
        import agent_trajviz as tv

        tv.label_views(labels)  # Surface invalid sidecars before announcing a running dashboard.
    serve_insight(str(path), args.port, exit_on_stdin=args.exit_on_stdin,
                  window=args.window, labels_path=str(labels) if labels is not None else None,
                  session_id=args.session_id)


@register("converge")
def _serve_converge(args: argparse.Namespace) -> None:
    _dashboard()
    root = Path(args.data_root).resolve()
    if not root.is_dir():
        raise BridgeError("not_found", f"No such folder: {root}", path=str(root))
    load_plugins()
    serve_converge(str(root), args.port, exit_on_stdin=args.exit_on_stdin)
