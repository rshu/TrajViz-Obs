"""Check portable export, browser embedding, and SQLite input protection."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from http.client import HTTPConnection
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

import pytest

from . import export
from .collector import create_app, utc_day
from .export import export_report, render_html

ROOT = Path(__file__).resolve().parents[1]


def embedded_text(page: str) -> str:
    block = re.search(r'<script id="embedded-report" type="application/json">(.*?)</script>', page, re.DOTALL)
    assert block is not None
    return block[1]


def test_cli_exports_the_same_snapshot_as_json_and_html(tmp_path):
    database = tmp_path / "usage.sqlite3"
    create_app(database)
    base = [sys.executable, "-m", "telemetry", "--database", str(database), "report", "--days", "2"]

    def run(*args):
        return subprocess.run([*base, *args], cwd=ROOT, capture_output=True, text=True, check=True, timeout=10)

    snapshot = json.loads(run().stdout)
    assert snapshot["format"] == "trajviz-obs-usage-report"
    assert snapshot["schema_version"] == 1
    assert snapshot["retention_days"] == 365
    assert snapshot["engagement"][0]["average_days_per_engaged_installation"] is None
    json_file = tmp_path / "exports" / "usage.json"
    html_file = tmp_path / "exports" / "usage.html"
    assert run("--json", str(json_file)).stdout.strip() == str(json_file)
    assert run("--html", str(html_file)).stdout.strip() == str(html_file)
    assert json.loads(json_file.read_text()) == snapshot
    assert json.loads(embedded_text(html_file.read_text())) == snapshot
    assert not list(html_file.parent.glob(".trajviz-obs-*.tmp"))


def test_cli_serves_live_report_and_ingests_events_on_the_same_port(tmp_path, monkeypatch):
    monkeypatch.delenv("TRAJVIZ_OBS_INGEST_CREDENTIALS", raising=False)
    command = [sys.executable, "-m", "telemetry", "--database", str(tmp_path / "usage.sqlite3"),
               "serve", "--port", "0"]
    log = tmp_path / "server.log"
    with log.open("wb") as stderr, subprocess.Popen(command, cwd=ROOT, stderr=stderr) as worker:
        try:
            deadline = monotonic() + 10
            while monotonic() < deadline:
                lines = log.read_text(encoding="utf-8").splitlines()
                if len(lines) >= 2 or worker.poll() is not None:
                    break
                sleep(0.03)
            assert len(lines) >= 2, lines
            address = re.fullmatch(r"http://127\.0\.0\.1:(\d+)/v1/events", lines[0])
            assert address is not None, lines
            port = int(address[1])
            assert lines[1].endswith(f"http://127.0.0.1:{port}/")

            def request(method, path="/", *, body=None, headers=None):
                connection = HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    connection.request(method, path, body=body, headers=headers or {})
                    with connection.getresponse() as response:
                        return response.status, response.headers, response.read()
                finally:
                    connection.close()

            status, headers, page = request("GET")
            assert status == 200
            assert headers.get_content_type() == "text/html"
            assert headers["Cache-Control"] == "no-store"
            assert json.loads(embedded_text(page.decode("utf-8")))["features"] == []
            head_status, head_headers, head_body = request("HEAD")
            assert (head_status, head_body) == (200, b"")
            assert head_headers["Content-Type"] == headers["Content-Type"]
            assert int(head_headers["Content-Length"]) == len(page)
            post_status, post_headers, _ = request("POST")
            assert post_status == 405
            assert set(post_headers["Allow"].replace(" ", "").split(",")) == {"GET", "HEAD"}
            assert request("GET", headers={"Host": f"example.invalid:{port}"})[0] == 403
            assert request("GET", "/missing")[0] == 404
            assert request("GET", "/v1/events")[0] == 405

            payload = json.dumps({
                "schema_version": 1,
                "adapter": "claude-code",
                "installation_id": uuid4().hex,
                "events": [{
                    "id": uuid4().hex, "day": utc_day().isoformat(), "surface": "terminal",
                    "feature": "help", "outcome": "success", "duration_bucket": "lt_100ms", "version": "1.0.0",
                }],
            })
            status, _, body = request("POST", "/v1/events", body=payload, headers={"Content-Type": "application/json"})
            assert (status, body) == (204, b"")
            status, _, page = request("GET")
            assert status == 200
            snapshot = json.loads(embedded_text(page.decode("utf-8")))
            assert [(row["adapter"], row["feature"], row["invocations"]) for row in snapshot["features"]] == [
                ("claude-code", "help", 1),
            ]
        finally:
            if worker.poll() is None:
                worker.terminate()
                try:
                    worker.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    worker.kill()


def test_embedded_json_cannot_terminate_its_html_element():
    payload = {"probe": "</ScRiPt><img src=x onerror=alert(1)> & 😀\u2028"}
    data = embedded_text(render_html(payload))
    assert "<" not in data
    assert json.loads(data) == payload


def test_oversized_html_export_leaves_a_previous_report_intact(tmp_path, monkeypatch):
    monkeypatch.setattr(export, "_MAX_VIEWER_DATA", 16)
    database = tmp_path / "usage.sqlite3"
    destination = tmp_path / "report.html"
    destination.write_text("previous report")
    payload = {"value": "<" * 10}
    with pytest.raises(ValueError, match="Use --json"):
        export_report(payload, destination, database=database, as_html=True)
    assert destination.read_text() == "previous report"
    json_path = export_report(payload, tmp_path / "report.json", database=database, as_html=False)
    assert json.loads(json_path.read_text()) == payload


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_export_refuses_the_database_and_sqlite_sidecars(tmp_path, suffix):
    database = tmp_path / "usage.sqlite3"
    protected = database.with_name(database.name + suffix)
    protected.write_bytes(b"existing data")
    with pytest.raises(ValueError, match="SQLite sidecar"):
        export_report({}, protected, database=database, as_html=False)
    assert protected.read_bytes() == b"existing data"


def test_export_refuses_a_database_hardlink_and_preserves_a_previous_report(tmp_path):
    database = tmp_path / "usage.sqlite3"
    create_app(database)
    alias = tmp_path / "database-alias.html"
    alias.hardlink_to(database)
    with pytest.raises(ValueError, match="source database"):
        export_report({}, alias, database=database, as_html=True)
    assert alias.samefile(database)

    destination = tmp_path / "report.json"
    destination.write_text("previous report")
    with pytest.raises(ValueError, match="JSON compliant"):
        export_report({"number": float("nan")}, destination, database=database, as_html=False)
    assert destination.read_text() == "previous report"
