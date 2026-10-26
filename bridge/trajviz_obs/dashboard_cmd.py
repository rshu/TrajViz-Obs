"""``dashboard`` and ``converge``: TrajViz's Insight and Converge dashboards on 127.0.0.1.

Both print ``{"url": …}`` as their one stdout line once the server is up, then serve until terminated. Claude
Code's process API starts children with stdin already closed, so EOF must not stop a managed dashboard.
Standalone pipe owners can opt into EOF shutdown with ``--exit-on-stdin``. The Insight dashboard opens with the session already
loaded: a page-load event sets the trajectory file control to the transcript and clicks the dashboard's own
Load button. The transcript is served in place (a Gradio static path) rather than as a cache copy, so TrajViz
reads it at its real path and finds ``<id>/subagents/`` beside it. A missing ``app`` extra is reported as
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
import threading
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

from .cli import register
from .compat import dashboard_server_options, dashboard_window_callback, load_plugins
from .dashboard_ui import decorate_dashboard, presentation
from .errors import BridgeError
from .sessions import _positive_int

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


def _apply_context_window(app: gr.Blocks, transcript_path: str, window: int | None) -> None:
    """Adapt the app's own loader only for its selected transcript, before it computes any views."""
    if window is None:
        return
    callbacks = [block for block in app.fns.values()
                 if getattr(block.fn, "__module__", "") == "agent_trajviz.insight.ui.load"
                 and getattr(block.fn, "__name__", "") == "_do_load"]
    if len(callbacks) != 1:
        raise BridgeError("dashboard_incompatible", "TrajViz's dashboard loader changed; cannot preserve the "
                          "Claude Code context window. Update the plugin or use /obs context.")
    callbacks[0].fn = dashboard_window_callback(callbacks[0].fn, transcript_path, window)


def _add_labels_preload(app: gr.Blocks, transcript_path: str, labels_path: str) -> bool:
    """Restore labels through the native reset/change handlers, after its normal trajectory reset.

    The native Load click resets label charts and clears the label file. Supplying the known sidecar as
    that reset's last output makes the existing file-change handler draw all five label charts. Its new
    trajectory input prevents that sidecar from leaking into a different uploaded run.
    """
    import gradio as gr

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
    callback = callbacks[0]
    reset = callback.fn
    selected = Path(transcript_path).resolve()

    def reset_labels(upload_obj):
        path = upload_obj if isinstance(upload_obj, str) else getattr(upload_obj, "name", None)
        same_run = bool(path) and Path(path).resolve() == selected
        outputs = list(reset())
        outputs[-1] = gr.update(value=labels_path if same_run else None)
        return tuple(outputs)

    gr.set_static_paths([labels_path])
    callback.fn = reset_labels
    callback.inputs = [trajectory_file]
    return True


def build_preloaded_insight(transcript_path: str, *, window: int | None = None,
                            labels_path: str | None = None) -> gr.Blocks:
    """The Insight dashboard that loads *transcript_path* when the page opens (unchanged if that cannot be wired)."""
    app = _dashboard().insight_app()
    _apply_context_window(app, transcript_path, window)
    _add_preload(app, transcript_path)
    if labels_path is not None:
        _add_labels_preload(app, transcript_path, labels_path)
    return decorate_dashboard(app, "insight")


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
                  window: int | None = None, labels_path: str | None = None) -> None:
    """Serve the preloaded Insight dashboard until terminated; ``{"url"}`` is the first stdout line.

    When the preload cannot be wired (a TrajViz release changed the upload row) the dashboard still starts and
    the line carries ``"preloaded": false``.
    """
    _check_port(port)
    dashboard = _dashboard()
    app = dashboard.insight_app()
    _apply_context_window(app, transcript_path, window)
    preloaded = _add_preload(app, transcript_path)
    labels_preloaded = labels_path is not None and _add_labels_preload(app, transcript_path, labels_path)
    app = decorate_dashboard(app, "insight")
    try:
        with contextlib.redirect_stdout(sys.stderr):  # Gradio's prints: the URL stays the first stdout line
            _, url, _ = app.launch(**dashboard_server_options(port), prevent_thread_lock=True, quiet=True,
                                   allowed_paths=[transcript_path, *([labels_path] if labels_path is not None else [])],
                                   **presentation("insight", dashboard.insight_presentation()))
        _announce(url, **({} if preloaded else {"preloaded": False}),
                  **({"labels_preloaded": labels_preloaded} if labels_path is not None else {}))
        _wait_for_shutdown(exit_on_stdin=exit_on_stdin)
    finally:
        with contextlib.redirect_stdout(sys.stderr):
            app.close()


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
    path = Path(args.transcript).resolve()
    if not path.is_file():
        raise BridgeError("not_found", f"No such transcript: {path}", path=str(path))
    load_plugins()  # as TrajViz's own dashboard command does on start, so the app reads plugins' formats
    labels = args.labels.resolve() if args.labels is not None else None
    if labels is not None:
        if not labels.is_file():
            raise BridgeError("not_found", f"No such labels sidecar: {labels}", path=str(labels))
        import agent_trajviz as tv

        tv.label_views(labels)  # Surface invalid sidecars before announcing a running dashboard.
    serve_insight(str(path), args.port, exit_on_stdin=args.exit_on_stdin,
                  window=args.window, labels_path=str(labels) if labels is not None else None)


@register("converge")
def _serve_converge(args: argparse.Namespace) -> None:
    _dashboard()
    root = Path(args.data_root).resolve()
    if not root.is_dir():
        raise BridgeError("not_found", f"No such folder: {root}", path=str(root))
    load_plugins()
    serve_converge(str(root), args.port, exit_on_stdin=args.exit_on_stdin)
