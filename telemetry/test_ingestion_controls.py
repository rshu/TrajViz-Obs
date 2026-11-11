"""Exercise authenticated collector ingestion and identifier-free delivery monitoring."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from uuid import uuid4
from wsgiref.util import setup_testing_defaults

import pytest

from . import collector
from .collector import (
    create_app,
    create_production_app,
    database_connection,
    ingestion_status,
)
from .credentials import (
    REGISTRY_ENV,
    CredentialRegistry,
    issue_credential,
    list_credentials,
    revoke_credential,
)
from .test_collector import batch, event

TODAY = date(2026, 11, 11)


def submit(app, payload=None, *, token=None, body=None, **overrides):
    content = json.dumps(payload).encode("utf-8") if body is None else body
    environ = {}
    setup_testing_defaults(environ)
    environ.update({"PATH_INFO": collector.EVENTS_PATH, "REQUEST_METHOD": "POST", "CONTENT_TYPE": "application/json",
                    "CONTENT_LENGTH": str(len(content)), "wsgi.input": io.BytesIO(content), **overrides})
    if token is not None:
        environ["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    answer = []

    def start_response(status, headers):
        answer.append((int(status.split(" ", 1)[0]), dict(headers)))

    assert b"".join(app(environ, start_response)) == b""
    return answer[0]


@pytest.fixture(autouse=True)
def collector_clock(monkeypatch):
    monkeypatch.delenv(REGISTRY_ENV, raising=False)
    monkeypatch.setattr(collector, "utc_day", lambda: TODAY)


def test_production_requires_an_absolute_configured_registry(tmp_path, monkeypatch):
    database = tmp_path / "usage.sqlite"
    with pytest.raises(ValueError, match=REGISTRY_ENV):
        create_production_app(database)
    monkeypatch.setenv(REGISTRY_ENV, "relative.json")
    with pytest.raises(ValueError, match=REGISTRY_ENV):
        create_app(database)
    monkeypatch.setenv(REGISTRY_ENV, str(tmp_path / "absent.json"))
    with pytest.raises(FileNotFoundError):
        create_production_app(database)
    assert not database.exists()


def test_credentials_are_only_stored_as_hashes_and_updates_are_atomic(tmp_path):
    registry = tmp_path / "ingest.json"
    names = [f"sender-{i}" for i in range(12)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        tokens = list(pool.map(lambda name: issue_credential(registry, name), names))
    assert len(set(tokens)) == len(tokens)
    raw = registry.read_text(encoding="utf-8")
    assert json.loads(raw) == {"version": 1, "credentials": {
        name: hashlib.sha256(token.encode("ascii")).hexdigest() for name, token in zip(names, tokens, strict=True)
    }}
    assert all(len(token) == 43 and token not in raw for token in tokens)
    if os.name != "nt":
        assert registry.stat().st_mode & 0o777 == 0o600
    assert list_credentials(registry) == tuple(sorted(names))
    authenticator = CredentialRegistry(registry)
    assert all(authenticator.authenticate(f"Bearer {token}") is not None for token in tokens)
    with pytest.raises(ValueError, match="already exists"):
        issue_credential(registry, names[0])
    with ThreadPoolExecutor(max_workers=6) as pool:
        assert all(pool.map(lambda name: revoke_credential(registry, name), names))
    assert not list_credentials(registry)
    assert all(authenticator.authenticate(f"Bearer {token}") is None for token in tokens)
    assert not revoke_credential(registry, names[0])


def test_revocation_and_corrupt_registry_fail_closed_before_reading_upload(tmp_path):
    class NeverRead:
        def read(self, _length):
            pytest.fail("an unauthorized upload body must not be read")

    registry = tmp_path / "ingest.json"
    database = tmp_path / "usage.sqlite"
    token = issue_credential(registry, "pilot")
    app = create_production_app(database, credentials=registry)
    payload = batch(uuid4().hex, event(TODAY))
    status, headers = submit(app, payload, **{"wsgi.input": NeverRead()})
    assert status == 401 and headers["WWW-Authenticate"].startswith("Bearer ")
    assert submit(app, payload, token=token)[0] == 204
    assert revoke_credential(registry, "pilot")
    assert submit(app, payload, token=token, **{"wsgi.input": NeverRead()})[0] == 401

    replacement = issue_credential(registry, "pilot")
    assert submit(app, payload, token=replacement)[0] == 204
    registry.write_text('{"credentials":', encoding="utf-8")
    assert submit(app, payload, token=replacement, **{"wsgi.input": NeverRead()})[0] == 503
    registry.unlink()
    assert submit(app, payload, token=replacement, **{"wsgi.input": NeverRead()})[0] == 503
    health = ingestion_status(database, today=TODAY, days=1)
    assert health["totals"] == {"accepted_requests": 2, "accepted_events": 1, "unauthorized_requests": 2,
                                "rejected_requests": 0, "throttled_requests": 0, "error_requests": 2}
    assert all(value not in json.dumps(health) for value in (token, replacement, "pilot", payload["installation_id"]))


def test_credential_and_global_limits_are_shared_across_workers(tmp_path, monkeypatch):
    registry = tmp_path / "ingest.json"
    database = tmp_path / "usage.sqlite"
    alice, bob, charlie = (issue_credential(registry, name) for name in ("alice", "bob", "charlie"))
    settings = {"credentials": registry, "requests_per_minute": 2, "global_requests_per_minute": 3}
    first = create_production_app(database, **settings)
    second = create_production_app(database, **settings)
    clock = 95.25
    monkeypatch.setattr(collector, "time", lambda: clock)

    def send(app, token):
        return submit(app, batch(uuid4().hex, event(TODAY)), token=token)

    assert send(first, alice)[0] == send(second, alice)[0] == 204
    status, headers = send(first, alice)
    assert status == 429 and headers["Retry-After"] == "25"
    assert send(second, bob)[0] == 204  # Alice's rejected attempt did not consume Bob's share.
    status, headers = send(first, charlie)
    assert status == 429 and headers["Retry-After"] == "25"
    clock = 120.0
    assert send(second, charlie)[0] == 204
    health = ingestion_status(database, today=TODAY, days=1)
    assert health["totals"]["accepted_requests"] == health["totals"]["accepted_events"] == 4
    assert health["totals"]["throttled_requests"] == 2
    with database_connection(database, read_only=True) as connection:
        quota = connection.execute("SELECT scope, minute, requests FROM ingestion_quota ORDER BY scope").fetchall()
    assert len(quota) == 2 and all(row[1:] == (2, 1) for row in quota)
    assert all(token not in str(tuple(map(tuple, quota))) for token in (alice, bob, charlie))


def test_concurrent_workers_cannot_overspend_one_credential(tmp_path, monkeypatch):
    registry = tmp_path / "ingest.json"
    database = tmp_path / "usage.sqlite"
    token = issue_credential(registry, "concurrent")
    apps = [create_production_app(database, credentials=registry, requests_per_minute=5) for _ in range(3)]
    monkeypatch.setattr(collector, "time", lambda: 100.0)
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(lambda i: submit(apps[i % 3], batch(uuid4().hex, event(TODAY)), token=token)[0],
                                 range(18)))
    assert statuses.count(204) == 5 and statuses.count(429) == 13
    counts = ingestion_status(database, today=TODAY, days=1)["totals"]
    assert counts["accepted_requests"] == 5 and counts["throttled_requests"] == 13


def test_ingestion_status_distinguishes_retries_rejections_and_unknown_history(tmp_path, monkeypatch):
    database = tmp_path / "usage.sqlite"
    with database_connection(database) as connection:
        connection.execute("CREATE TABLE old_receiver_schema (name TEXT)")
    assert not ingestion_status(database, today=TODAY, days=1)["monitoring_available"]
    with database_connection(database, read_only=True) as connection:
        assert [row[0] for row in connection.execute("SELECT name FROM sqlite_schema")] == ["old_receiver_schema"]

    app = create_app(database, retention_days=32)
    payload = batch(uuid4().hex, event(TODAY), event(TODAY, "dashboard"))
    assert submit(app, payload)[0] == submit(app, payload)[0] == 204
    assert submit(app, body=b"{invalid json")[0] == 400
    assert submit(app, payload, PATH_INFO="/report")[0] == 404

    def failing_record(*_args, **_kwargs):
        raise sqlite3.OperationalError("synthetic collector failure")

    monkeypatch.setattr(collector, "_record", failing_record)
    assert submit(app, payload)[0] == 503
    health = ingestion_status(database, today=TODAY, days=2, retention_days=32)
    assert health["monitoring_available"]
    assert health["daily"][0]["day"] == (TODAY - timedelta(days=1)).isoformat()
    assert health["totals"] == {"accepted_requests": 2, "accepted_events": 2, "unauthorized_requests": 0,
                                "rejected_requests": 1, "throttled_requests": 0, "error_requests": 1}

    collector.prune(database, TODAY + timedelta(days=32), retention_days=32)
    with database_connection(database, read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ingestion_metrics").fetchone()[0] == 0
