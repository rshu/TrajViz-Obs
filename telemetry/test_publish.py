"""Exercise the periodic publisher and recovery from temporary failures."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

import pytest

from . import publish
from .collector import DEFAULT_RETENTION_DAYS, _record, init_database, utc_day
from .test_export import embedded_text

ROOT = Path(__file__).resolve().parents[1]


def _await_invocations(path: Path, count: int, worker: subprocess.Popen) -> None:
    deadline = monotonic() + 10
    while monotonic() < deadline:
        if worker.poll() is not None:
            pytest.fail(f"Publisher stopped with {worker.returncode}: {worker.stderr.read()}")
        try:
            snapshot = json.loads(embedded_text(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            pass
        else:
            if sum(row["invocations"] for row in snapshot["engagement"]) == count:
                return
        sleep(0.03)
    pytest.fail(f"Publisher did not write a report with {count} tracked actions.")


def test_publisher_refreshes_report_and_stops(tmp_path):
    database = tmp_path / "usage.sqlite3"
    html = tmp_path / "usage.html"
    init_database(database)
    command = [sys.executable, "-m", "telemetry", "--database", str(database), "publish",
               "--html", str(html), "--interval-seconds", "1"]
    with subprocess.Popen(command, cwd=ROOT, stderr=subprocess.PIPE, text=True) as worker:
        try:
            _await_invocations(html, 0, worker)
            today = utc_day()
            _record(database, {
                "adapter": "claude-code", "installation_id": uuid4().hex,
                "events": [{"id": uuid4().hex, "day": today.isoformat(), "surface": "terminal",
                            "feature": "help", "outcome": "success", "duration_bucket": "lt_100ms",
                            "version": "1.0.0"}],
            }, today, DEFAULT_RETENTION_DAYS)
            _await_invocations(html, 1, worker)
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
    init_database(database)
    html.write_text("previous report", encoding="utf-8")
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
            assert html.read_text(encoding="utf-8") == "previous report"
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(publish, operation, transient_failure)
    monkeypatch.setattr(publish, "sleep", advance_interval)
    with pytest.raises(KeyboardInterrupt):
        publish.publish_reports(database, html, days=1, retention_days=DEFAULT_RETENTION_DAYS, interval_seconds=1)
    assert attempts == 2
    assert str(error) in capsys.readouterr().err
    assert json.loads(embedded_text(html.read_text(encoding="utf-8")))["schema_version"] == 1
