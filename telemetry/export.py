"""Write portable usage reports without exposing or replacing the source database."""

from __future__ import annotations

import json
from contextlib import suppress
from pathlib import Path
from shutil import copy2
from tempfile import TemporaryDirectory

from bridge.trajviz_obs.errors import BridgeError
from bridge.trajviz_obs.outputs import protect_inputs, staged_path

from .credentials import registry_path

_VIEWER = Path(__file__).with_name("viewer.html")
_PLACEHOLDER = "__TRAJVIZ_EMBEDDED_REPORT__"
_DATA_BLOCK = f'<script id="embedded-report" type="application/json">{_PLACEHOLDER}</script>'
_MAX_VIEWER_DATA = 5 * 1024 * 1024


class ReportTooLarge(ValueError):
    """An aggregate snapshot exceeds the HTML viewer's capacity."""


def report_json(payload: dict, *, compact: bool = False) -> str:
    if compact:
        return json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    return json.dumps(payload, ensure_ascii=True, allow_nan=False, indent=2) + "\n"


def render_html(payload: dict) -> str:
    template = _VIEWER.read_text(encoding="utf-8")
    if template.count(_DATA_BLOCK) != 1:
        raise ValueError("The usage viewer is missing its report data block.")
    # A literal '<' in script raw text can close the element before JSON.parse runs.
    data = report_json(payload, compact=True).replace("<", r"\u003c")
    if len(data) > _MAX_VIEWER_DATA:
        raise ReportTooLarge("The report exceeds the 5 MiB viewer limit. Use --json or shorten the reporting window.")
    return template.replace(_DATA_BLOCK, _DATA_BLOCK.replace(_PLACEHOLDER, data), 1)


def live_json_path(database: Path) -> Path:
    return database.with_name(f"{database.name}.report.json")


def _html_json_path(html: Path) -> Path:
    if html.suffix.lower() in {".html", ".htm"}:
        return html.with_suffix(".json")
    return html.with_name(f"{html.name}.json")


def _write_html_pair(destination: Path, html: str, json_path: Path, contents: str) -> None:
    # Both files are staged before either is replaced. Commit JSON first so new HTML has its copy.
    # Roll back a failed HTML replacement. A crash between the two renames can still split the pair.
    with TemporaryDirectory(prefix=".trajviz-obs-rollback-", dir=destination.parent) as directory:
        backup = Path(directory) / "previous.json"
        json_installed = False
        try:
            with staged_path(destination) as html_file:
                with staged_path(json_path) as json_file:
                    html_file.write_text(html, encoding="utf-8")
                    json_file.write_text(contents, encoding="utf-8")
                    with suppress(FileNotFoundError):
                        copy2(json_path, backup, follow_symlinks=False)
                json_installed = True
        except OSError:
            if json_installed:
                if backup.exists() or backup.is_symlink():
                    backup.replace(json_path)
                else:
                    json_path.unlink(missing_ok=True)
            raise


def export_report(payload: dict, destination: Path, *, database: Path, as_html: bool,
                  credentials: Path | None = None) -> Path:
    destination = destination.expanduser().absolute()
    database = database.resolve()
    sidecars = [database.with_name(database.name + suffix) for suffix in ("-wal", "-shm", "-journal")]
    protected = [database, *sidecars, _VIEWER, _VIEWER.with_name("report.schema.json")]
    if (registry := registry_path(credentials, required=False)) is not None:
        protected.append(registry)
    json_path = _html_json_path(destination) if as_html else destination
    try:
        if as_html:
            protect_inputs([destination], [json_path])
            protect_inputs([destination, json_path], [*protected, live_json_path(database)])
        else:
            protect_inputs([json_path], protected)
    except BridgeError as exc:
        raise ValueError(
            "Report output would replace the source database, a SQLite sidecar, a credential or format file, "
            "or another report output."
        ) from exc
    for path in {destination, json_path}:
        if path.is_dir():
            raise IsADirectoryError(f"Report output is a directory: {path}")
    html = render_html(payload) if as_html else None
    contents = report_json(payload)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if html is None:
        with staged_path(json_path) as staged:
            staged.write_text(contents, encoding="utf-8")
    else:
        _write_html_pair(destination, html, json_path, contents)
    return destination
