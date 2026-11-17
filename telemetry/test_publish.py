"""Exercise the periodic publisher and recovery from temporary failures."""

from __future__ import annotations

import json
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

import pytest

from . import export, publish
from .collector import DEFAULT_RETENTION_DAYS, _record, init_database, utc_day
from .test_export import embedded_text

ROOT = Path(__file__).resolve().parents[1]


def _await_invocations(html: Path, json_copy: Path, count: int, worker: subprocess.Popen) -> None:
    deadline = monotonic() + 10
    while monotonic() < deadline:
        if worker.poll() is not None:
            pytest.fail(f"Publisher stopped with {worker.returncode}: {worker.stderr.read()}")
        try:
            html_report = json.loads(embedded_text(html.read_text(encoding="utf-8")))
            json_report = json.loads(json_copy.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        else:
            if html_report == json_report and sum(row["invocations"] for row in json_report["engagement"]) == count:
                return
        sleep(0.03)
    pytest.fail(f"Publisher did not write matching HTML and JSON reports with {count} tracked actions.")


def _seed_previous_report(database: Path, html: Path, json_copy: Path) -> tuple[str, str]:
    # A different window makes it possible to detect either half being replaced early.
    previous = publish.report(database, today=utc_day(), days=2, retention_days=DEFAULT_RETENTION_DAYS)
    old_html, old_json = export.render_html(previous), export.report_json(previous)
    html.write_text(old_html, encoding="utf-8")
    json_copy.write_text(old_json, encoding="utf-8")
    return old_html, old_json


def test_publisher_refreshes_report_and_stops(tmp_path):
    database = tmp_path / "usage.sqlite3"
    html = tmp_path / "usage.html"
    json_copy = tmp_path / "usage.json"
    init_database(database)
    command = [sys.executable, "-m", "telemetry", "--database", str(database), "publish",
               "--html", str(html), "--interval-seconds", "1"]
    with subprocess.Popen(command, cwd=ROOT, stderr=subprocess.PIPE, text=True) as worker:
        try:
            _await_invocations(html, json_copy, 0, worker)
            if sys.platform != "win32":
                assert stat.S_IMODE(json_copy.stat().st_mode) == 0o600
            today = utc_day()
            _record(database, {
                "adapter": "claude-code", "installation_id": uuid4().hex,
                "events": [{"id": uuid4().hex, "day": today.isoformat(), "surface": "terminal",
                            "feature": "help", "outcome": "success", "duration_bucket": "lt_100ms",
                            "version": "1.0.0"}],
            }, today, DEFAULT_RETENTION_DAYS)
            _await_invocations(html, json_copy, 1, worker)
        finally:
            worker.terminate()
            try:
                worker.wait(timeout=5)
            except subprocess.TimeoutExpired:
                worker.kill()
                raise
        if sys.platform != "win32":
            assert worker.returncode == 0
        assert not list(tmp_path.glob(".trajviz-obs-*.tmp"))


@pytest.mark.parametrize(("operation", "error"), [
    ("report", sqlite3.OperationalError("database temporarily busy")),
    ("export_report", OSError("report directory temporarily unwritable")),
])
def test_publisher_keeps_previous_report_until_recovery(tmp_path, monkeypatch, capsys, operation, error):
    database = tmp_path / "usage.sqlite3"
    html = tmp_path / "usage.html"
    json_copy = tmp_path / "usage.json"
    init_database(database)
    previous_html, previous_json = _seed_previous_report(database, html, json_copy)
    original = getattr(publish, operation)
    attempts = 0

    def transient_failure(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise error
        return original(*args, **kwargs)

    def advance_interval(seconds):
        assert seconds == 1
        if attempts == 1:
            assert html.read_text(encoding="utf-8") == previous_html
            assert json_copy.read_text(encoding="utf-8") == previous_json
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(publish, operation, transient_failure)
    monkeypatch.setattr(publish, "sleep", advance_interval)
    with pytest.raises(KeyboardInterrupt):
        publish.publish_reports(database, html, days=1, retention_days=DEFAULT_RETENTION_DAYS, interval_seconds=1)
    assert attempts == 2
    assert str(error) in capsys.readouterr().err
    assert json_copy.read_text(encoding="utf-8") != previous_json
    assert json.loads(embedded_text(html.read_text(encoding="utf-8"))) == json.loads(json_copy.read_text(encoding="utf-8"))


def test_publisher_recovers_after_html_snapshot_fits_again(tmp_path, monkeypatch, capsys):
    database = tmp_path / "usage.sqlite3"
    html = tmp_path / "usage.html"
    json_copy = tmp_path / "usage.json"
    init_database(database)
    previous_html, previous_json = _seed_previous_report(database, html, json_copy)
    original_limit = export._MAX_VIEWER_DATA
    monkeypatch.setattr(export, "_MAX_VIEWER_DATA", 1)

    def advance_interval(seconds):
        assert seconds == 1
        if export._MAX_VIEWER_DATA == 1:
            assert html.read_text(encoding="utf-8") == previous_html
            assert json_copy.read_text(encoding="utf-8") == previous_json
            monkeypatch.setattr(export, "_MAX_VIEWER_DATA", original_limit)
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(publish, "sleep", advance_interval)
    with pytest.raises(KeyboardInterrupt):
        publish.publish_reports(database, html, days=1, retention_days=DEFAULT_RETENTION_DAYS, interval_seconds=1)
    assert "viewer limit" in capsys.readouterr().err
    assert json_copy.read_text(encoding="utf-8") != previous_json
    assert json.loads(embedded_text(html.read_text(encoding="utf-8"))) == json.loads(json_copy.read_text(encoding="utf-8"))
