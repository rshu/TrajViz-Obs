"""``dashboard`` and ``converge``: TrajViz's Insight and Converge dashboards on 127.0.0.1.

Both print ``{"url": …}`` as their one stdout line once the server is up, then serve until stdin closes (the mod
keeps the pipe open while the dashboard should run). The Insight dashboard opens with the session already
loaded: a page-load event sets the trajectory file control to the transcript and clicks the dashboard's own
Load button. The transcript is served in place (a Gradio static path) rather than as a cache copy, so TrajViz
reads it at its real path and finds ``<id>/subagents/`` beside it. A missing ``app`` extra is reported as
``missing_extra`` before anything is printed.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
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


def _announce(url: str, **extra: object) -> None:
    sys.stdout.write(json.dumps({"url": url, **extra}) + "\n")
    sys.stdout.flush()


def _wait_for_eof() -> None:
    if sys.stdin is None:
        return
    for _ in sys.stdin:
        pass


def serve_insight(transcript_path: str, port: int | None) -> None:
    """Serve the preloaded Insight dashboard until stdin closes; ``{"url"}`` is the first stdout line.

    When the preload cannot be wired (a TrajViz release changed the upload row) the dashboard still starts and
    the line carries ``"preloaded": false``.
    """
    dashboard = _dashboard()
    app = dashboard.insight_app()
    preloaded = _add_preload(app, transcript_path)
    try:
        # Gradio's own prints go to stderr so the URL document stays the first stdout line.
        with contextlib.redirect_stdout(sys.stderr):
            _, url, _ = app.launch(server_name="127.0.0.1", server_port=port, prevent_thread_lock=True, quiet=True,
                                   allowed_paths=[os.path.dirname(transcript_path)],
                                   **dashboard.insight_presentation())
        _announce(url, **({} if preloaded else {"preloaded": False}))
        _wait_for_eof()
    finally:
        with contextlib.redirect_stdout(sys.stderr):
            app.close()


def serve_converge(data_root: str, port: int | None) -> None:
    """Serve the Converge dashboard over *data_root* until stdin closes; ``{"url"}`` is the first stdout line."""
    dashboard = _dashboard()
    import gradio as gr

    try:
        with contextlib.redirect_stdout(sys.stderr):
            _, url, _ = dashboard.launch_converge(host="127.0.0.1", port=port, data_root=data_root,
                                                  prevent_thread_lock=True, quiet=True)
        _announce(url)
        _wait_for_eof()
    finally:
        with contextlib.redirect_stdout(sys.stderr):
            gr.close_all()


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("dashboard", help="The Insight dashboard with the session preloaded, until stdin closes.")
    p.add_argument("--transcript", required=True, help="The session transcript.")
    p.add_argument("--port", type=int, default=None, help="The port (default: Gradio's first free one from 7860).")

    p = sub.add_parser("converge", help="The Converge comparison dashboard, until stdin closes.")
    p.add_argument("--data-root", required=True, help="The folder batch manifests may name files in.")
    p.add_argument("--port", type=int, default=None, help="The port (default: Gradio's first free one from 7860).")


@register("dashboard")
def _serve_dashboard(args: argparse.Namespace) -> None:
    _dashboard()
    path = Path(args.transcript).resolve()
    if not path.is_file():
        raise BridgeError("not_found", f"No such transcript: {path}", path=str(path))
    load_plugins()  # as TrajViz's own dashboard command does on start, so the app reads plugins' formats
    serve_insight(str(path), args.port)


@register("converge")
def _serve_converge(args: argparse.Namespace) -> None:
    _dashboard()
    root = Path(args.data_root).resolve()
    if not root.is_dir():
        raise BridgeError("not_found", f"No such folder: {root}", path=str(root))
    serve_converge(str(root), args.port)
