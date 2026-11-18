"""Exercise activity cohorts with legacy, partial, and replayed uploads."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest

from bridge.telemetry_protocol import AGENT_VERSION_SCHEMA, SCHEMA_HEADER

from . import collector
from .collector import DEFAULT_RETENTION_DAYS, create_app, database_connection, prune
from .export import render_html
from .report import report
from .test_collector import batch, event
from .test_ingestion_controls import TODAY, submit

KEY = "19" * 32
SAME_ADDRESS = "192.0.2.10"
OTHER_ADDRESS = "2001:db8::10"


@pytest.fixture(autouse=True)
def collector_settings(monkeypatch):
    monkeypatch.delenv(collector.ADDRESS_KEY_ENV, raising=False)
    monkeypatch.setattr(collector, "utc_day", lambda: TODAY)


def v2(installation, *events, adapter="claude-code"):
    return {**batch(installation, *events, adapter=adapter), "schema_version": AGENT_VERSION_SCHEMA}


def test_distinct_addresses_installations_pairs_and_versions(tmp_path):
    database = tmp_path / "usage.sqlite3"
    app = create_app(database, address_key=KEY)
    first = uuid4().hex
    second = uuid4().hex
    older = TODAY - timedelta(days=1)
    native = v2(first, {**event(TODAY), "agent_version": "2.1.311"})
    assert submit(app, native, REMOTE_ADDR=SAME_ADDRESS)[0] == 204
    assert submit(app, native, REMOTE_ADDR=OTHER_ADDRESS)[0] == 204
    assert submit(app, v2(first, {**event(TODAY, "pane"), "agent_version": "2.1.311"}),
                  REMOTE_ADDR=SAME_ADDRESS)[0] == 204
    assert submit(app, v2(second, {**event(TODAY), "agent_version": "2.1.312"}),
                  REMOTE_ADDR=f"::ffff:{SAME_ADDRESS}", HTTP_X_FORWARDED_FOR="203.0.113.71")[0] == 204
    assert submit(app, v2(first, {**event(TODAY), "agent_version": "2.1.312"}),
                  REMOTE_ADDR=OTHER_ADDRESS)[0] == 204
    assert submit(app, v2(first, {**event(TODAY, surface="desktop", version="0.5.4"), "agent_version": "2.2.7"},
                          adapter="opencode"), REMOTE_ADDR=SAME_ADDRESS)[0] == 204
    assert submit(app, v2(first, {**event(older), "agent_version": "2.1.311"}),
                  REMOTE_ADDR="192.0.2.99")[0] == 204
    assert submit(app, batch(uuid4().hex, event(TODAY)), REMOTE_ADDR="192.0.2.100")[0] == 204

    snapshot = report(database, today=TODAY, days=2)
    activity = snapshot["activity"]
    assert activity["period"] == {
        "distinct_addresses": 2, "distinct_installations": 4, "distinct_address_installation_pairs": 4,
        "events": 7, "events_with_address": 5, "events_with_agent_version": 6,
    }
    assert activity["today"] == {**activity["period"], "events": 6, "events_with_agent_version": 5}
    assert activity["last_30_days"] == activity["period"]
    assert activity["daily"][0] == {
        "day": older.isoformat(), "distinct_addresses": None, "distinct_installations": 1,
        "distinct_address_installation_pairs": None, "events": 1,
        "events_with_address": 0, "events_with_agent_version": 1,
    }
    claude, opencode, pi = activity["by_adapter"]
    assert pi["period"]["distinct_installations"] == 0
    assert claude["period"]["distinct_addresses"] == 2
    assert claude["period"]["distinct_installations"] == 3
    assert opencode["period"]["distinct_addresses"] == 1
    assert opencode["period"]["distinct_installations"] == 1
    by_version = {(row["adapter"], row["agent_version"]): row for row in activity["by_agent_version"]}
    assert by_version["claude-code", "2.1.311"]["distinct_addresses"] == 1
    assert by_version["claude-code", "2.1.312"]["distinct_address_installation_pairs"] == 2
    assert by_version["claude-code", None]["distinct_addresses"] is None
    assert by_version["opencode", "2.2.7"]["distinct_installations"] == 1
    assert snapshot["versions"][-1]["version"] == "0.5.4"

    encoded = json.dumps(snapshot)
    html = render_html(snapshot)
    with database_connection(database, read_only=True) as connection:
        digests = {row[0] for row in connection.execute("SELECT address_digest FROM events") if row[0] is not None}
        assert len(digests) == 2 and all(len(digest) == 32 for digest in digests)
    for value in (SAME_ADDRESS, OTHER_ADDRESS, first, second, KEY, *(digest.hex() for digest in digests)):
        assert value not in encoded
        assert value not in html


@pytest.mark.parametrize("peer", ["", "not-an-address", "::", "224.1.1.1", "fe80::1%en0", "192.0.2.1:80"])
def test_unusable_peer_cannot_be_replaced_by_a_client_header(tmp_path, peer):
    database = tmp_path / "usage.sqlite3"
    app = create_app(database, address_key=KEY)
    status, headers = submit(app, v2(uuid4().hex, event(TODAY)), REMOTE_ADDR=peer,
                             HTTP_X_FORWARDED_FOR=SAME_ADDRESS, HTTP_FORWARDED=f"for={SAME_ADDRESS}")
    assert status == 204 and headers[SCHEMA_HEADER] == str(AGENT_VERSION_SCHEMA)
    activity = report(database, today=TODAY)["activity"]["today"]
    assert activity["events_with_address"] == 0
    assert activity["distinct_addresses"] is None
    assert activity["distinct_installations"] == 1


def test_protocol_negotiation_and_address_collection_disabled_by_default(tmp_path):
    database = tmp_path / "usage.sqlite3"
    app = create_app(database)
    installation = uuid4().hex
    versioned = {**event(TODAY), "agent_version": "2.2.7"}
    legacy_status, legacy_headers = submit(app, batch(installation, event(TODAY)), REMOTE_ADDR=SAME_ADDRESS)
    assert (legacy_status, legacy_headers[SCHEMA_HEADER]) == (204, "2")
    for payload in (batch(installation, versioned), v2(installation, {**versioned, "agent_version": "private/path"}),
                    v2(installation, {**versioned, "ip": SAME_ADDRESS})):
        status, headers = submit(app, payload, REMOTE_ADDR=SAME_ADDRESS)
        assert (status, headers[SCHEMA_HEADER]) == (400, "2")
    assert submit(app, v2(installation, versioned), REMOTE_ADDR=SAME_ADDRESS)[0] == 204
    assert submit(app, v2(installation, event(TODAY)), REMOTE_ADDR=SAME_ADDRESS)[0] == 204
    totals = report(database, today=TODAY)["activity"]["today"]
    assert totals["events"] == 3
    assert totals["events_with_agent_version"] == 1
    assert totals["distinct_addresses"] is None


def test_old_database_reads_and_migrates_with_stable_address_key(tmp_path, monkeypatch):
    database = tmp_path / "usage.sqlite3"
    app = create_app(database)
    assert submit(app, batch(uuid4().hex, event(TODAY)))[0] == 204
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE events DROP COLUMN agent_version")
        connection.execute("ALTER TABLE events DROP COLUMN address_digest")
        connection.execute("DROP TABLE address_key_check")

    legacy = report(database, today=TODAY)["activity"]["today"]
    assert legacy["distinct_addresses"] is None
    assert legacy["distinct_installations"] == 1

    monkeypatch.setenv(collector.ADDRESS_KEY_ENV, KEY)
    with ThreadPoolExecutor(max_workers=4) as pool:
        apps = list(pool.map(lambda _: create_app(database), range(4)))
    with pytest.raises(ValueError, match="differs"):
        create_app(database, address_key="b1" * 32)
    with pytest.raises(ValueError, match="64 hexadecimal"):
        create_app(database, address_key="malformed")
    for app in apps:
        assert submit(app, v2(uuid4().hex, event(TODAY)), REMOTE_ADDR=SAME_ADDRESS)[0] == 204
    totals = report(database, today=TODAY)["activity"]["today"]
    assert totals["distinct_installations"] == 5
    assert totals["distinct_addresses"] == 1
    assert totals["events_with_address"] == 4
    tomorrow = TODAY + timedelta(days=1)
    monkeypatch.setattr(collector, "utc_day", lambda: tomorrow)
    assert submit(apps[0], v2(uuid4().hex, event(tomorrow)), REMOTE_ADDR=SAME_ADDRESS)[0] == 204
    rolling = report(database, today=tomorrow, days=1)["activity"]
    assert rolling["period"]["distinct_installations"] == 1
    assert rolling["last_30_days"]["distinct_installations"] == 6
    assert rolling["last_30_days"]["distinct_addresses"] == 1
    prune(database, tomorrow + timedelta(days=DEFAULT_RETENTION_DAYS), DEFAULT_RETENTION_DAYS)
    with database_connection(database, read_only=True) as connection:
        assert not connection.execute("SELECT 1 FROM events").fetchall()
