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
import json
import os
import signal
import socket
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

from .cli import register
from .compat import load_plugins
from .errors import BridgeError

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


def build_preloaded_insight(transcript_path: str) -> gr.Blocks:
    """The Insight dashboard that loads *transcript_path* when the page opens (unchanged if that cannot be wired)."""
    app = _dashboard().insight_app()
    _add_preload(app, transcript_path)
    return app


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


def serve_insight(transcript_path: str, port: int | None, *, exit_on_stdin: bool = False) -> None:
    """Serve the preloaded Insight dashboard until terminated; ``{"url"}`` is the first stdout line.

    When the preload cannot be wired (a TrajViz release changed the upload row) the dashboard still starts and
    the line carries ``"preloaded": false``.
    """
    _check_port(port)
    dashboard = _dashboard()
    app = dashboard.insight_app()
    preloaded = _add_preload(app, transcript_path)
    try:
        with contextlib.redirect_stdout(sys.stderr):  # Gradio's prints: the URL stays the first stdout line
            _, url, _ = app.launch(server_name="127.0.0.1", server_port=port, prevent_thread_lock=True, quiet=True,
                                   allowed_paths=[os.path.dirname(transcript_path)],
                                   **dashboard.insight_presentation())
        _announce(url, **({} if preloaded else {"preloaded": False}))
        _wait_for_shutdown(exit_on_stdin=exit_on_stdin)
    finally:
        with contextlib.redirect_stdout(sys.stderr):
            app.close()


def serve_converge(data_root: str, port: int | None, *, exit_on_stdin: bool = False) -> None:
    """Serve the Converge dashboard over *data_root* until terminated; ``{"url"}`` is the first stdout line."""
    _check_port(port)
    dashboard = _dashboard()
    import gradio as gr

    try:
        with contextlib.redirect_stdout(sys.stderr):  # Gradio's prints: the URL stays the first stdout line
            _, url, _ = dashboard.launch_converge(host="127.0.0.1", port=port, data_root=data_root,
                                                  prevent_thread_lock=True, quiet=True)
        _announce(url)
        _wait_for_shutdown(exit_on_stdin=exit_on_stdin)
    finally:
        with contextlib.redirect_stdout(sys.stderr):
            gr.close_all()


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
    serve_insight(str(path), args.port, exit_on_stdin=args.exit_on_stdin)


@register("converge")
def _serve_converge(args: argparse.Namespace) -> None:
    _dashboard()
    root = Path(args.data_root).resolve()
    if not root.is_dir():
        raise BridgeError("not_found", f"No such folder: {root}", path=str(root))
    serve_converge(str(root), args.port, exit_on_stdin=args.exit_on_stdin)
