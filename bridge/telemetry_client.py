"""Record and deliver opt-in plugin usage without loading the TrajViz runtime."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import random
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

# Both hosts launch this entry point with -I. Use only the protocol beside it.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from telemetry_protocol import (
    ADAPTERS,
    BACKFILL_DAYS,
    FEATURES,
    MAX_BATCH,
    OUTCOMES,
    SCHEMA_VERSION,
    SURFACES,
    duration_bucket,
    validate_event,
    validate_payload,
)

MAX_PENDING = 512
LEASE_SECONDS = 15
NETWORK_TIMEOUT = 3
MAX_BATCHES_PER_RUN = 3
MAX_RETRY_SECONDS = 3600
CLOCK_ROLLBACK_TOLERANCE = 5
_TOKEN = re.compile(r"[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]")
_FAILURES = frozenset({"network", "throttled", "collector_unavailable", "authentication", "endpoint", "payload_rejected"})
# The standalone sender also runs on system Python 3.9 before the managed runtime exists.
_UTC = timezone.utc  # noqa: UP017


def configured_endpoint(env: Mapping[str, str]) -> str | None:
    if env.get("TRAJVIZ_OBS_TELEMETRY") != "1":
        return None
    return _endpoint(env.get("TRAJVIZ_OBS_TELEMETRY_URL", ""))


def _endpoint(url: str) -> str | None:
    if not url or len(url) > 2048:
        return None
    if any(ord(char) < 33 or ord(char) > 126 or char in "\\?#" for char in url):
        return None
    try:
        parsed = urlsplit(url)
        if parsed.port == 0:
            return None
    except ValueError:
        return None
    loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    if (not parsed.hostname or parsed.username is not None or parsed.path != "/v1/events"
            or (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback))):
        return None
    return url


def configured_token(env: Mapping[str, str]) -> str | None:
    token = env.get("TRAJVIZ_OBS_TELEMETRY_TOKEN") or None
    if token is not None and _TOKEN.fullmatch(token) is None:
        raise ValueError("invalid telemetry credential")
    return token


def state_directory(env: Mapping[str, str]) -> Path:
    override = env.get("TRAJVIZ_OBS_TELEMETRY_STATE_DIR")
    if override:
        directory = Path(override)
        if not directory.is_absolute():
            raise ValueError("telemetry state directory must be absolute")
        return directory
    if os.name == "nt" and (local := env.get("LOCALAPPDATA")):
        root = Path(local)
    else:
        root = Path(env.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    if not root.is_absolute():
        raise ValueError("telemetry state directory must be absolute")
    return root / "trajviz-obs" / "telemetry"


def _queue_path(directory: Path, adapter: str, endpoint: str) -> Path:
    scope = hashlib.sha256(endpoint.encode("ascii")).hexdigest()[:24]
    return directory / f"client-v{SCHEMA_VERSION}-{adapter}-{scope}.sqlite3"


def _open_queue(directory: Path, adapter: str, endpoint: str) -> sqlite3.Connection:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = _queue_path(directory, adapter, endpoint)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.is_symlink() or not path.is_file():
            raise ValueError("invalid telemetry queue file") from None
    else:
        os.close(descriptor)
    return sqlite3.connect(path, timeout=0.4)


def _ensure_health(connection: sqlite3.Connection) -> None:
    # A separate row upgrades legacy queues without touching their identity or pending observations.
    connection.execute("""
        CREATE TABLE IF NOT EXISTS sender_health (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            last_success_at REAL, last_failure_at REAL, last_failure TEXT, last_http_status INTEGER,
            dropped INTEGER NOT NULL DEFAULT 0, expired INTEGER NOT NULL DEFAULT 0,
            rejected INTEGER NOT NULL DEFAULT 0, batch_limit INTEGER NOT NULL,
            rejected_credential TEXT
        )
    """)
    connection.execute("INSERT OR IGNORE INTO sender_health (singleton, batch_limit) VALUES (1, ?)", (MAX_BATCH,))


def _expire_pending(connection: sqlite3.Connection, cutoff: str, latest: str) -> None:
    # A corrected clock can leave queued dates the collector will reject.
    expired = connection.execute("DELETE FROM pending WHERE day < ? OR day > ?", (cutoff, latest)).rowcount
    if expired:
        connection.execute("UPDATE sender_health SET expired = expired + ?", (expired,))
    connection.execute("DELETE FROM activation WHERE day < ? OR day > ?", (cutoff, latest))


def _record(connection: sqlite3.Connection, event: dict, cutoff: str) -> None:
    latest = (date.fromisoformat(event["day"]) + timedelta(days=1)).isoformat()
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS sender (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                installation_id TEXT NOT NULL,
                lease TEXT, lease_until REAL NOT NULL DEFAULT 0,
                retry_at REAL NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0
            )
        """)
        connection.execute("CREATE TABLE IF NOT EXISTS activation (day TEXT PRIMARY KEY)")
        connection.execute("CREATE TABLE IF NOT EXISTS pending (id TEXT UNIQUE, day TEXT NOT NULL, body TEXT NOT NULL)")
        connection.execute("INSERT OR IGNORE INTO sender (singleton, installation_id) VALUES (1, ?)", (uuid4().hex,))
        _ensure_health(connection)
        _expire_pending(connection, cutoff, latest)
        if event["feature"] == "activation":
            recorded = connection.execute("INSERT OR IGNORE INTO activation (day) VALUES (?)", (event["day"],))
            if not recorded.rowcount:
                return
        connection.execute("INSERT INTO pending VALUES (?, ?, ?)",
                           (event["id"], event["day"], json.dumps(event, separators=(",", ":"))))
        dropped = connection.execute("""
            DELETE FROM pending WHERE rowid < (
                SELECT rowid FROM pending ORDER BY rowid DESC LIMIT 1 OFFSET ?
            )
        """, (MAX_PENDING - 1,)).rowcount
        if dropped:
            connection.execute("UPDATE sender_health SET dropped = dropped + ?", (dropped,))


def _credential_fingerprint(token: str | None) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest() if token else ""


def _pending_events(connection: sqlite3.Connection, limit: int, now: float) -> list[dict]:
    events = []
    malformed = []
    with closing(connection.execute("SELECT id, body FROM pending ORDER BY rowid LIMIT ?", (MAX_PENDING,))) as rows:
        for event_id, body in rows:
            try:
                event = validate_event(json.loads(body))
            except (ValueError, TypeError, RecursionError):
                malformed.append((event_id,))
                continue
            if event["id"] != event_id:
                malformed.append((event_id,))
                continue
            events.append(event)
            if len(events) >= limit:
                break
    if malformed:
        connection.executemany("DELETE FROM pending WHERE id = ?", malformed)
        connection.execute("""
            UPDATE sender_health SET rejected = rejected + ?, last_failure = 'payload_rejected',
            last_failure_at = ?, last_http_status = NULL
        """, (len(malformed), now))
    return events


def _claim(connection: sqlite3.Connection, adapter: str, now: float, token: str | None = None) -> tuple[str, dict] | None:
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        _ensure_health(connection)
        # Clock corrections must not turn a short lease or retry into a multi-day wait.
        connection.execute("UPDATE sender SET lease = NULL, lease_until = 0 WHERE lease_until > ?",
                           (now + LEASE_SECONDS + CLOCK_ROLLBACK_TOLERANCE,))
        connection.execute("UPDATE sender SET retry_at = 0, failures = 0 WHERE retry_at > ?",
                           (now + MAX_RETRY_SECONDS + CLOCK_ROLLBACK_TOLERANCE,))
        state = connection.execute("""
            SELECT installation_id, batch_limit FROM sender JOIN sender_health USING (singleton)
            WHERE lease_until <= ? AND (retry_at <= ? OR rejected_credential != ?)
        """, (now, now, _credential_fingerprint(token))).fetchone()
        if state is None:
            return None
        # A process may resume a queued flush days after the event was recorded.
        today = datetime.fromtimestamp(now, _UTC).date()
        _expire_pending(connection, (today - timedelta(days=BACKFILL_DAYS)).isoformat(),
                        (today + timedelta(days=1)).isoformat())
        events = _pending_events(connection, max(1, min(state[1], MAX_BATCH)), now)
        if not events:
            return None
        payload = validate_payload({"schema_version": SCHEMA_VERSION, "adapter": adapter,
                                    "installation_id": state[0], "events": events})
        lease = uuid4().hex
        connection.execute("UPDATE sender SET lease = ?, lease_until = ?", (lease, now + LEASE_SECONDS))
        return lease, payload


@dataclass(frozen=True)
class _Delivery:
    kind: Literal["accepted", "retry", "blocked", "payload"]
    failure: str | None = None
    http_status: int | None = None
    retry_after: float | None = None


def _retry_delay(failures: int, hint: float | None) -> float:
    if hint is not None:
        base = max(1.0, min(MAX_RETRY_SECONDS, hint))
        return min(MAX_RETRY_SECONDS, base + random.uniform(0, min(30, base * 0.2)))
    base = min(MAX_RETRY_SECONDS, 30 * 2 ** (failures - 1))
    return random.uniform(min(base, MAX_RETRY_SECONDS * 0.8), min(MAX_RETRY_SECONDS, base * 1.2))


def _finish(connection: sqlite3.Connection, lease: str, payload: dict, delivery: _Delivery, now: float,
            token: str | None = None) -> None:
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        state = connection.execute("""
            SELECT failures, batch_limit, rejected_credential FROM sender JOIN sender_health USING (singleton)
            WHERE lease = ?
        """, (lease,)).fetchone()
        if state is None:
            return
        failures, batch_limit, previous_credential = state
        credential = _credential_fingerprint(token)
        retry_at = 0.0
        rejected = 0
        events = payload["events"]
        if delivery.kind == "accepted":
            connection.executemany("DELETE FROM pending WHERE id = ?", [(event["id"],) for event in events])
            batch_limit = min(MAX_BATCH, batch_limit * 2)
            failures = 0
            connection.execute("UPDATE sender_health SET last_success_at = ?", (now,))
        elif delivery.kind == "payload":
            # Keep valid siblings queued. Persist the smaller size across bounded flush calls.
            if len(events) == 1:
                rejected = connection.execute("DELETE FROM pending WHERE id = ?", (events[0]["id"],)).rowcount
                batch_limit = min(MAX_BATCH, batch_limit * 2)
            else:
                batch_limit = len(events) // 2
            failures = 0
        else:
            if previous_credential is not None and previous_credential != credential:
                failures = 0
            failures = min(failures + 1, 8)
            retry_at = now + _retry_delay(failures, delivery.retry_after)
        if delivery.failure is not None:
            connection.execute("""
                UPDATE sender_health SET last_failure_at = ?, last_failure = ?, last_http_status = ?, rejected = rejected + ?
            """, (now, delivery.failure, delivery.http_status, rejected))
        connection.execute("UPDATE sender_health SET batch_limit = ?, rejected_credential = ?",
                           (batch_limit, credential if delivery.failure == "authentication" else None))
        connection.execute("UPDATE sender SET lease = NULL, lease_until = 0, retry_at = ?, failures = ?",
                           (retry_at, failures))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _retry_after(value: str | None, now: float) -> float | None:
    if not value or len(value) > 128:
        return None
    value = value.strip()
    if value.isascii() and value.isdecimal():
        return min(int(value), MAX_RETRY_SECONDS)
    try:
        deadline = parsedate_to_datetime(value)
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=_UTC)
        return min(MAX_RETRY_SECONDS, max(0.0, deadline.timestamp() - now))
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _http_result(status: int, retry_after: str | None = None) -> _Delivery:
    if status == 204:
        return _Delivery("accepted")
    if status in (400, 413, 422):
        return _Delivery("payload", "payload_rejected", status)
    if status in (401, 403):
        return _Delivery("blocked", "authentication", status)
    if status in (408, 425, 429) or (500 <= status < 600 and status not in (501, 505)):
        reason = "throttled" if status == 429 else "collector_unavailable"
        return _Delivery("retry", reason, status, _retry_after(retry_after, datetime.now(_UTC).timestamp()))
    return _Delivery("blocked", "endpoint", status)


def _send(endpoint: str, payload: dict, *, token: str | None = None) -> _Delivery:
    # The configured endpoint receives the batch directly and cannot redirect it.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    headers = {"Content-Type": "application/json", "User-Agent": f"TrajViz-Obs-Telemetry/{SCHEMA_VERSION}"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(endpoint, data=json.dumps(payload, separators=(",", ":")).encode("ascii"),
                                     headers=headers, method="POST")
    try:
        with opener.open(request, timeout=NETWORK_TIMEOUT) as response:
            return _http_result(response.status, response.headers.get("Retry-After"))
    except urllib.error.HTTPError as error:
        with error:
            return _http_result(error.code, error.headers.get("Retry-After") if error.headers else None)
    except (OSError, http.client.HTTPException):
        return _Delivery("retry", "network")


def queue_usage(endpoint: str, directory: Path, *, adapter: str, surface: str, feature: str,
                outcome: str, duration_ms: int, version: str) -> bool:
    """Persist a validated event without waiting for the collector."""
    now = datetime.now(_UTC)
    try:
        event = validate_event({"id": uuid4().hex, "day": now.date().isoformat(), "surface": surface,
                                "feature": feature, "outcome": outcome,
                                "duration_bucket": duration_bucket(duration_ms), "version": version})
        cutoff = (now.date() - timedelta(days=BACKFILL_DAYS)).isoformat()
        with closing(_open_queue(directory, adapter, endpoint)) as connection:
            _record(connection, event, cutoff)
    except (OSError, sqlite3.Error, ValueError):
        return False
    return True


def flush_usage(endpoint: str, directory: Path, adapter: str, *, token: str | None = None) -> None:
    """Deliver a bounded number of queued batches if the retry lease is available."""
    if token is not None and _TOKEN.fullmatch(token) is None:
        return
    try:
        with closing(_open_queue(directory, adapter, endpoint)) as connection:
            for _ in range(MAX_BATCHES_PER_RUN):
                claim = _claim(connection, adapter, datetime.now(_UTC).timestamp(), token)
                if claim is None:
                    break
                lease, payload = claim
                delivery = _send(endpoint, payload, token=token)
                _finish(connection, lease, payload, delivery, datetime.now(_UTC).timestamp(), token)
                if delivery.kind in ("retry", "blocked"):
                    break
    except (OSError, sqlite3.Error, ValueError):
        # Telemetry is optional. An unavailable endpoint or queue never changes a plugin command.
        return


def _time_label(timestamp: float | None) -> str | None:
    if type(timestamp) not in (int, float) or timestamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(timestamp, _UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def _queue_status(path: Path, token: str | None) -> dict:
    if path.is_symlink():
        raise ValueError("invalid telemetry queue file")
    if not path.exists():
        return {"queue_state": "absent", "queued": 0, "dropped": 0, "expired": 0, "rejected": 0}
    if not path.is_file():
        raise ValueError("invalid telemetry queue file")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.4)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        sender = connection.execute("SELECT retry_at FROM sender WHERE singleton = 1").fetchone()
        queued = connection.execute("SELECT count(*) FROM pending").fetchone()[0]
        has_health = connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sender_health'").fetchone()
        health = connection.execute("""
            SELECT last_success_at, last_failure_at, last_failure, last_http_status,
                   dropped, expired, rejected, rejected_credential
            FROM sender_health WHERE singleton = 1
        """).fetchone() if has_health else None
    status = {"queue_state": "present", "queued": queued, "dropped": 0, "expired": 0, "rejected": 0,
              "retry_at": _time_label(sender["retry_at"]) if sender else None}
    if health is not None:
        http_status = health["last_http_status"]
        failure = health["last_failure"]
        status.update({
            "last_success_at": _time_label(health["last_success_at"]), "last_failure_at": _time_label(health["last_failure_at"]),
            "last_failure": failure if failure in _FAILURES else None,
            "last_http_status": http_status if type(http_status) is int and 100 <= http_status <= 599 else None,
        })
        for name in ("dropped", "expired", "rejected"):
            count = health[name]
            status[name] = count if type(count) is int and count >= 0 else None
        rejected_credential = health["rejected_credential"]
        if rejected_credential is not None and rejected_credential != _credential_fingerprint(token):
            status["retry_at"] = None
    return status


def delivery_status(env: Mapping[str, str], adapter: str) -> dict:
    """Inspect only an existing queue for the selected endpoint, without sending or exposing identifiers."""
    if adapter not in ADAPTERS:
        raise ValueError("invalid telemetry adapter")
    url = env.get("TRAJVIZ_OBS_TELEMETRY_URL", "")
    endpoint = _endpoint(url)
    if env.get("TRAJVIZ_OBS_TELEMETRY") != "1":
        configuration = "disabled"
    elif not url:
        configuration = "missing_endpoint"
    else:
        configuration = "ready" if endpoint else "invalid_endpoint"
    try:
        token = configured_token(env)
    except ValueError:
        token = None
        if configuration == "ready":
            configuration = "invalid_token"
    status = {
        "adapter": adapter, "configuration": configuration,
        "credential_configured": bool(env.get("TRAJVIZ_OBS_TELEMETRY_TOKEN")),
        "queue_state": "unscoped", "queue_capacity": MAX_PENDING, "queued": None, "dropped": None, "expired": None,
        "rejected": None, "last_success_at": None, "last_failure_at": None, "last_failure": None, "last_http_status": None,
        "retry_at": None,
    }
    if endpoint is None:
        return status
    try:
        directory = state_directory(env)
    except (OSError, ValueError, RuntimeError):
        if configuration == "ready":
            status["configuration"] = "invalid_state_directory"
        status["queue_state"] = "unavailable"
        return status
    try:
        status.update(_queue_status(_queue_path(directory, adapter, endpoint), token))
    except (OSError, ValueError, sqlite3.Error):
        status["queue_state"] = "unavailable"
    return status


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if "--status" in arguments:
        parser = argparse.ArgumentParser(description="Inspect local usage telemetry delivery without sending an event.")
        parser.add_argument("--status", action="store_true", required=True)
        parser.add_argument("--adapter", required=True, choices=ADAPTERS)
        args = parser.parse_args(arguments)
        print(json.dumps(delivery_status(os.environ, args.adapter), allow_nan=False))
        return 0
    endpoint = configured_endpoint(os.environ)
    if endpoint is None:
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", required=True, choices=ADAPTERS)
    parser.add_argument("--surface", required=True, choices=SURFACES)
    parser.add_argument("--feature", required=True, choices=FEATURES)
    parser.add_argument("--outcome", required=True, choices=OUTCOMES)
    parser.add_argument("--duration-ms", required=True, type=int)
    parser.add_argument("--version", required=True)
    args = parser.parse_args(arguments)
    try:
        token = configured_token(os.environ)
        directory = state_directory(os.environ)
    except (OSError, ValueError, RuntimeError):
        return 0
    if queue_usage(endpoint, directory, adapter=args.adapter, surface=args.surface, feature=args.feature,
                   outcome=args.outcome, duration_ms=args.duration_ms, version=args.version):
        flush_usage(endpoint, directory, args.adapter, token=token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
