"""Write portable usage reports without exposing or replacing the source database."""

from __future__ import annotations

import json
from pathlib import Path

from bridge.trajviz_obs.errors import BridgeError
from bridge.trajviz_obs.outputs import protect_inputs, staged_path

_VIEWER = Path(__file__).with_name("viewer.html")
_PLACEHOLDER = "__TRAJVIZ_EMBEDDED_REPORT__"
_DATA_BLOCK = f'<script id="embedded-report" type="application/json">{_PLACEHOLDER}</script>'
_MAX_VIEWER_DATA = 5 * 1024 * 1024


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
        raise ValueError("The report exceeds the 5 MiB viewer limit. Use --json or shorten the reporting window.")
    return template.replace(_DATA_BLOCK, _DATA_BLOCK.replace(_PLACEHOLDER, data), 1)


def export_report(payload: dict, destination: Path, *, database: Path, as_html: bool) -> Path:
    destination = destination.expanduser().absolute()
    database = database.resolve()
    sidecars = [database.with_name(database.name + suffix) for suffix in ("-wal", "-shm", "-journal")]
    try:
        protect_inputs([destination], [database, *sidecars, _VIEWER, _VIEWER.with_name("report.schema.json")])
    except BridgeError as exc:
        raise ValueError("Report output would replace the source database, a SQLite sidecar, or a format file.") from exc
    contents = render_html(payload) if as_html else report_json(payload)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with staged_path(destination) as staged:
        staged.write_text(contents, encoding="utf-8")
    return destination
