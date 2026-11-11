"""SQLite-backed WSGI receiver for opt-in usage telemetry."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from math import ceil
from pathlib import Path
from time import monotonic, sleep, time

from bridge.telemetry_protocol import BACKFILL_DAYS, MAX_BODY, validate_payload

from .credentials import CredentialRegistry, registry_path

DEFAULT_RETENTION_DAYS = 365
EVENTS_PATH = "/v1/events"
DEFAULT_REQUESTS_PER_MINUTE = 60
DEFAULT_GLOBAL_REQUESTS_PER_MINUTE = 600
_DATABASE_TIMEOUT_SECONDS = 5
_HEALTH_OUTCOMES = ("accepted", "rejected", "unauthorized", "throttled", "error")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS installations (
    adapter TEXT NOT NULL,
    installation_id TEXT NOT NULL,
    first_day TEXT NOT NULL,
    last_day TEXT NOT NULL,
    PRIMARY KEY (adapter, installation_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS installations_last_day ON installations(last_day);

CREATE TABLE IF NOT EXISTS events (
    adapter TEXT NOT NULL,
    installation_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    day TEXT NOT NULL,
    surface TEXT NOT NULL,
    feature TEXT NOT NULL,
    outcome TEXT NOT NULL,
    duration_bucket TEXT NOT NULL,
    version TEXT NOT NULL,
    PRIMARY KEY (adapter, installation_id, event_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS events_day ON events(day);
CREATE UNIQUE INDEX IF NOT EXISTS daily_activation
    ON events(adapter, installation_id, day) WHERE feature = 'activation';

CREATE TABLE IF NOT EXISTS ingestion_metrics (
    day TEXT NOT NULL,
    outcome TEXT NOT NULL,
    requests INTEGER NOT NULL,
    events INTEGER NOT NULL,
    PRIMARY KEY (day, outcome)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS ingestion_quota (
    scope TEXT PRIMARY KEY,
    minute INTEGER NOT NULL,
    requests INTEGER NOT NULL
) WITHOUT ROWID;
"""


def utc_day() -> date:
    return datetime.now(UTC).date()


def retention_cutoff(today: date, retention_days: int) -> str:
    if not 32 <= retention_days <= 365:
        raise ValueError("Retention must be between 32 and 365 days.")
    return (today - timedelta(days=retention_days - 1)).isoformat()


@contextmanager
def database_connection(database: Path, *, read_only: bool = False,
                        timeout: float = _DATABASE_TIMEOUT_SECONDS) -> Iterator[sqlite3.Connection]:
    target = f"{database.resolve().as_uri()}?mode=ro" if read_only else database
    connection = sqlite3.connect(target, uri=read_only, timeout=timeout)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            if not read_only:
                connection.execute("PRAGMA secure_delete = ON")
            yield connection
    finally:
        connection.close()


def init_database(database: Path) -> None:
    database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        descriptor = os.open(database, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(descriptor)
    with database_connection(database) as connection:
        deadline = monotonic() + _DATABASE_TIMEOUT_SECONDS
        while True:
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                break
            except sqlite3.OperationalError as error:
                # Competing mode changes on a fresh database can return busy
                # immediately instead of invoking SQLite's busy timeout.
                if error.sqlite_errorcode & 0xFF != sqlite3.SQLITE_BUSY or monotonic() >= deadline:
                    raise
                sleep(0.01)
        connection.executescript(_SCHEMA)


def _prune(connection: sqlite3.Connection, cutoff: str) -> None:
    connection.execute("DELETE FROM events WHERE day < ?", (cutoff,))
    connection.execute("DELETE FROM installations WHERE last_day < ?", (cutoff,))
    connection.execute("DELETE FROM ingestion_metrics WHERE day < ?", (cutoff,))


def prune(database: Path, today: date, retention_days: int) -> None:
    cutoff = retention_cutoff(today, retention_days)
    with database_connection(database) as connection:
        _prune(connection, cutoff)
        connection.execute("DELETE FROM ingestion_quota WHERE minute != ?", (int(time() // 60),))
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _count_ingestion(connection: sqlite3.Connection, today: date, outcome: str, events: int = 0) -> None:
    connection.execute(
        """
        INSERT INTO ingestion_metrics(day, outcome, requests, events) VALUES (?, ?, 1, ?)
        ON CONFLICT(day, outcome) DO UPDATE SET requests = requests + 1, events = events + excluded.events
        """,
        (today.isoformat(), outcome, events),
    )


def ingestion_status(database: Path, *, today: date, days: int = 7,
                     retention_days: int = DEFAULT_RETENTION_DAYS) -> dict:
    """Read identifier-free request and event counts without migrating the database."""
    retention_cutoff(today, retention_days)
    if not 1 <= days <= retention_days:
        raise ValueError("Status days must be within the configured retention window.")
    start = today - timedelta(days=days - 1)
    end = today.isoformat()
    empty = {f"{outcome}_requests": 0 for outcome in _HEALTH_OUTCOMES}
    empty["accepted_events"] = 0
    daily = {(start + timedelta(days=i)).isoformat(): empty.copy() for i in range(days)}
    with database_connection(database, read_only=True) as connection:
        connection.execute("BEGIN")
        available = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'ingestion_metrics'"
        ).fetchone() is not None
        if available:
            for row in connection.execute("""
                    SELECT day, outcome, requests, events FROM ingestion_metrics WHERE day BETWEEN ? AND ?
                    """, (start.isoformat(), end)):
                counts = daily[row["day"]]
                counts[f"{row['outcome']}_requests"] = row["requests"]
                if row["outcome"] == "accepted":
                    counts["accepted_events"] = row["events"]
    return {"period_start": start.isoformat(), "as_of": end, "monitoring_available": available,
            "totals": {key: sum(row[key] for row in daily.values()) for key in empty},
            "daily": [{"day": day, **row} for day, row in daily.items()]}


def _reserve_upload(database: Path, credential: str, per_credential: int, global_limit: int) -> int | None:
    """Reserve a request in the shared UTC-minute budget or return a Retry-After delay."""
    with database_connection(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        now = time()
        minute = int(now // 60)
        connection.execute("DELETE FROM ingestion_quota WHERE minute != ?", (minute,))
        hits = dict(connection.execute(
            "SELECT scope, requests FROM ingestion_quota WHERE scope IN ('global', ?)", (credential,),
        ))
        if hits.get(credential, 0) >= per_credential or hits.get("global", 0) >= global_limit:
            return max(1, ceil((minute + 1) * 60 - now))
        connection.executemany(
            """
            INSERT INTO ingestion_quota(scope, minute, requests) VALUES (?, ?, 1)
            ON CONFLICT(scope) DO UPDATE SET requests = requests + 1
            """,
            [(credential, minute), ("global", minute)],
        )
    return None


def _record(database: Path, batch: dict, today: date, retention_days: int, *, count_request: bool = False) -> None:
    adapter, installation_id = batch["adapter"], batch["installation_id"]
    first_day = (today - timedelta(days=BACKFILL_DAYS)).isoformat()
    last_day = (today + timedelta(days=1)).isoformat()
    if any(event["day"] > last_day for event in batch["events"]):
        raise ValueError("Event day is ahead of the accepted upload window.")
    # A sender can claim a batch just before UTC midnight. Acknowledge an
    # expired observation without discarding the rest of that batch.
    events = [event for event in batch["events"] if event["day"] >= first_day]
    if not events and not count_request:
        return

    with database_connection(database) as connection:
        _prune(connection, retention_cutoff(today, retention_days))
        accepted_days = []
        for event in events:
            written = connection.execute(
                """
                INSERT INTO events(adapter, installation_id, event_id, day, surface, feature, outcome,
                                   duration_bucket, version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING
                """,
                (adapter, installation_id, event["id"], event["day"], event["surface"], event["feature"],
                 event["outcome"], event["duration_bucket"], event["version"]),
            )
            if written.rowcount:
                accepted_days.append(event["day"])
        if accepted_days:
            connection.execute(
                """
                INSERT INTO installations(adapter, installation_id, first_day, last_day) VALUES (?, ?, ?, ?)
                ON CONFLICT(adapter, installation_id) DO UPDATE SET
                    first_day = min(installations.first_day, excluded.first_day),
                    last_day = max(installations.last_day, excluded.last_day)
                """,
                (adapter, installation_id, min(accepted_days), max(accepted_days)),
            )
        if count_request:
            _count_ingestion(connection, today, "accepted", len(accepted_days))


def _unique_fields(pairs: list[tuple[str, object]]) -> dict:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("Duplicate JSON field.")
    return value


def create_app(database: str | Path, *, retention_days: int = DEFAULT_RETENTION_DAYS,
               credentials: str | Path | None = None, require_auth: bool = False,
               requests_per_minute: int = DEFAULT_REQUESTS_PER_MINUTE,
               global_requests_per_minute: int = DEFAULT_GLOBAL_REQUESTS_PER_MINUTE) -> Callable:
    """Return a WSGI callable for loopback or an explicitly configured receiver."""
    config = registry_path(credentials, required=require_auth)
    registry = CredentialRegistry(config) if config is not None else None
    if (type(requests_per_minute) is not int or type(global_requests_per_minute) is not int
            or not 1 <= requests_per_minute <= global_requests_per_minute <= 60_000):
        raise ValueError("Ingestion limits require 1 <= requests per credential <= global requests <= 60000 per minute.")
    path = Path(database).expanduser().resolve()
    retention_cutoff(utc_day(), retention_days)
    init_database(path)
    prune(path, utc_day(), retention_days)

    def app(environ, start_response):
        today = utc_day()

        def answer(status: str, *, outcome: str | None = None,
                   extra_headers: tuple[tuple[str, str], ...] = ()) -> list[bytes]:
            if outcome is not None:
                # Keep rejection responses available if the metrics database itself is busy or offline.
                try:
                    with database_connection(path, timeout=0.1) as connection:
                        connection.execute("DELETE FROM ingestion_metrics WHERE day < ?",
                                           (retention_cutoff(today, retention_days),))
                        _count_ingestion(connection, today, outcome)
                except sqlite3.Error:
                    pass
            headers = [("Cache-Control", "no-store"), *extra_headers]
            if not status.startswith("204 "):
                headers.append(("Content-Length", "0"))
            start_response(status, headers)
            return []

        def unavailable() -> list[bytes]:
            return answer("503 Service Unavailable", outcome="error", extra_headers=(("Retry-After", "10"),))

        if environ["PATH_INFO"] != EVENTS_PATH:
            return answer("404 Not Found")
        if environ.get("QUERY_STRING"):
            return answer("404 Not Found", outcome="rejected")
        if environ["REQUEST_METHOD"] != "POST":
            return answer("405 Method Not Allowed", outcome="rejected", extra_headers=(("Allow", "POST"),))
        if registry is not None:
            try:
                key = registry.authenticate(environ.get("HTTP_AUTHORIZATION", ""))
                if key is None:
                    return answer("401 Unauthorized", outcome="unauthorized",
                                  extra_headers=(("WWW-Authenticate", 'Bearer realm="TrajViz Obs telemetry"'),))
                delay = _reserve_upload(path, key, requests_per_minute, global_requests_per_minute)
            except (OSError, ValueError, sqlite3.Error):
                return unavailable()
            if delay is not None:
                return answer("429 Too Many Requests", outcome="throttled", extra_headers=(("Retry-After", str(delay)),))
        if environ.get("CONTENT_TYPE", "").partition(";")[0].strip().lower() != "application/json":
            return answer("415 Unsupported Media Type", outcome="rejected")
        length = environ.get("CONTENT_LENGTH", "")
        if not length or not length.isascii() or not length.isdecimal():
            return answer("411 Length Required", outcome="rejected")
        if len(length) > len(str(MAX_BODY)) or int(length) > MAX_BODY:
            return answer("413 Content Too Large", outcome="rejected")
        try:
            size = int(length)
            body = environ["wsgi.input"].read(size)
            if len(body) != size:
                return answer("400 Bad Request", outcome="rejected")
            batch = validate_payload(json.loads(body.decode("utf-8"), object_pairs_hook=_unique_fields))
            _record(path, batch, today, retention_days, count_request=True)
        except (ValueError, OSError, RecursionError):
            return answer("400 Bad Request", outcome="rejected")
        except sqlite3.Error:
            return unavailable()
        return answer("204 No Content")

    return app


def create_production_app(database: str | Path, *, retention_days: int = DEFAULT_RETENTION_DAYS,
                          credentials: str | Path | None = None,
                          requests_per_minute: int = DEFAULT_REQUESTS_PER_MINUTE,
                          global_requests_per_minute: int = DEFAULT_GLOBAL_REQUESTS_PER_MINUTE) -> Callable:
    """Build the authenticated receiver for a production WSGI server behind an HTTPS proxy."""
    return create_app(database, retention_days=retention_days, credentials=credentials, require_auth=True,
                      requests_per_minute=requests_per_minute, global_requests_per_minute=global_requests_per_minute)
