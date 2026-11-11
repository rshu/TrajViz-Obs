"""Refresh an offline HTML report from the collector database."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from time import sleep

from .collector import utc_day
from .export import export_report
from .report import report


def publish_reports(database: Path, destination: Path, *, days: int, retention_days: int, interval_seconds: int) -> None:
    """Write a snapshot immediately and then wait between attempts."""
    if not 1 <= interval_seconds <= 2**31 - 1:
        raise ValueError("Report refresh interval must be between 1 and 2147483647 seconds.")

    while True:
        try:
            snapshot = report(database, today=utc_day(), days=days, retention_days=retention_days)
            export_report(snapshot, destination, database=database, as_html=True)
        except (OSError, sqlite3.Error) as exc:
            print(f"telemetry publish: {exc}", file=sys.stderr, flush=True)
        sleep(interval_seconds)
