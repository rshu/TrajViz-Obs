"""Regression tests for the ingest boundary, retention, and aggregate counts."""

from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4
from wsgiref.simple_server import make_server
from wsgiref.util import setup_testing_defaults

from bridge.telemetry_protocol import MAX_BATCH, MAX_BODY

from .__main__ import _QuietHandler
from .collector import (
    EVENTS_PATH,
    create_app,
    database_connection,
    init_database,
    prune,
)
from .report import report


def event(day: date, feature: str = "overview", *, outcome: str = "success", bucket: str = "lt_100ms",
          surface: str = "terminal", version: str = "0.3.8") -> dict:
    return {"id": uuid4().hex, "day": day.isoformat(), "feature": feature, "outcome": outcome,
            "surface": surface, "duration_bucket": bucket, "version": version}


def batch(installation: str, *events: dict, adapter: str = "claude-code") -> dict:
    return {"schema_version": 1, "adapter": adapter, "installation_id": installation, "events": list(events)}


class CollectorTest(unittest.TestCase):
    def setUp(self):
        self.today = date(2026, 11, 11)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "usage.sqlite"
        self.retention_days = 90
        self.app = create_app(self.database, retention_days=self.retention_days)

    def request(self, payload=None, *, body=None, arrival=None, **overrides):
        if body is None:
            body = json.dumps(payload).encode()
        environ = {}
        setup_testing_defaults(environ)
        environ.update({"PATH_INFO": EVENTS_PATH, "REQUEST_METHOD": "POST", "CONTENT_TYPE": "application/json",
                        "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body), **overrides})
        response = []

        def start_response(status, headers):
            response.append((int(status.split(" ", 1)[0]), dict(headers)))

        with patch("telemetry.collector.utc_day", return_value=arrival or self.today):
            content = b"".join(self.app(environ, start_response))
        return *response[0], content

    def count(self, table):
        with database_connection(self.database, read_only=True) as connection:
            statement = "SELECT count(*) FROM events" if table == "events" else "SELECT count(*) FROM installations"
            return connection.execute(statement).fetchone()[0]

    def test_receiver_rejects_invalid_requests_without_recording_or_echoing(self):
        installation = uuid4().hex
        valid = batch(installation, event(self.today))
        private = batch(installation, {**event(self.today), "prompt": "private transcript"})
        cases = [
            (valid, {"REQUEST_METHOD": "GET"}, 405),
            (valid, {"PATH_INFO": "/report"}, 404),
            (valid, {"QUERY_STRING": "extra"}, 404),
            (valid, {"CONTENT_TYPE": "text/plain"}, 415),
            (valid, {"CONTENT_LENGTH": ""}, 411),
            (valid, {"CONTENT_LENGTH": str(MAX_BODY + 1)}, 413),
            (valid, {"CONTENT_LENGTH": "10000"}, 400),
            (None, {"body": b"\xff"}, 400),
            (None, {"body": b'{"schema_version": 1, "schema_version": 1}'}, 400),
            (private, {}, 400),
            ({**valid, "schema_version": 99}, {}, 400),
            (batch(installation, *[event(self.today) for _ in range(MAX_BATCH + 1)]), {}, 400),
        ]
        for payload, options, expected in cases:
            with self.subTest(expected=expected, options=options):
                status, headers, content = self.request(payload, **options)
                self.assertEqual(status, expected)
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(content, b"")
        self.assertEqual(self.count("events"), 0)
        self.assertEqual(self.count("installations"), 0)

    def test_batch_retry_and_duplicate_daily_activation_are_idempotent(self):
        installation = uuid4().hex
        invocation = event(self.today)
        payload = batch(installation, event(self.today, "activation"),
                        event(self.today, "activation", version="0.3.9"), invocation)
        for _ in range(2):
            status, headers, content = self.request(payload)
            self.assertEqual((status, content), (204, b""))
            self.assertNotIn("Content-Length", headers)
        changed_retry = {**invocation, "day": (self.today - timedelta(days=1)).isoformat()}
        self.assertEqual(self.request(batch(installation, changed_retry))[0], 204)
        self.assertEqual(self.count("events"), 2)
        self.assertEqual(self.count("installations"), 1)
        self.assertEqual(report(self.database, today=self.today, days=1)["adoption"][0]["new_installations"], 1)

    def test_concurrent_batch_retries_do_not_double_count(self):
        payload = batch(uuid4().hex, event(self.today, "activation"), event(self.today))
        body = json.dumps(payload).encode()
        with patch("telemetry.collector.utc_day", return_value=self.today), ThreadPoolExecutor(max_workers=4) as pool:
            def send():
                environ = {}
                setup_testing_defaults(environ)
                environ.update({"PATH_INFO": EVENTS_PATH, "REQUEST_METHOD": "POST", "CONTENT_TYPE": "application/json",
                                "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body)})
                statuses = []
                list(self.app(environ, lambda status, headers: statuses.append(status)))
                return statuses

            responses = [pool.submit(send) for _ in range(12)]
            self.assertTrue(all(response.result() == ["204 No Content"] for response in responses))
        self.assertEqual(self.count("events"), 2)
        self.assertEqual(self.count("installations"), 1)

    def test_startup_retries_transient_wal_contention(self):
        with patch("telemetry.collector.database_connection") as connect:
            connection = connect.return_value.__enter__.return_value
            busy = sqlite3.OperationalError("database is locked")
            busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
            connection.execute.side_effect = [busy, None]
            init_database(self.database)
            self.assertEqual(connection.execute.call_count, 2)
            connection.executescript.assert_called_once()

    def test_upload_window_skips_expired_rows_and_rejects_implausible_future_days(self):
        installation = uuid4().hex
        oldest = self.today - timedelta(days=7)
        too_old = oldest - timedelta(days=1)
        tomorrow = self.today + timedelta(days=1)
        self.assertEqual(self.request(batch(installation, event(oldest), event(tomorrow + timedelta(days=1))))[0], 400)
        self.assertEqual(self.count("events"), 0)
        self.assertEqual(self.request(batch(installation, event(too_old)))[0], 204)
        self.assertEqual(self.count("installations"), 0)
        self.assertEqual(self.request(batch(installation, event(oldest), event(too_old), event(tomorrow)))[0], 204)
        self.assertEqual(self.count("events"), 2)
        summary = report(self.database, today=self.today, days=8)
        self.assertEqual(summary["features"][0]["invocations"], 1)
        self.assertEqual(summary["adoption"][0]["active_today"], 0)
        self.assertEqual(summary["adoption"][0]["new_installations"], 1)

    def test_multi_adapter_cohort_through_http(self):
        def day(offset):
            return self.today + timedelta(days=offset)

        shared, claude_returning, claude_passive, claude_dormant = (uuid4().hex for _ in range(4))
        open_passive, open_cancelled, open_new, open_tomorrow = (uuid4().hex for _ in range(4))

        def oc(installation, *events):
            return batch(installation, *({**item, "version": "0.5.4"} for item in events), adapter="opencode")

        def browser(offset):
            return event(day(offset), "dashboard.render", surface="browser", outcome="observed")

        claude_dashboard = batch(shared, browser(0), event(day(0), "dashboard", bucket="1s_5s", version="0.3.9"))
        visits = [
            (-80, batch(claude_dormant, event(day(-80), "activation"))),
            (-37, batch(shared, event(day(-37), "activation"), event(day(-37), "activation", version="0.3.9"))),
            (-35, oc(shared, event(day(-35), "activation"), event(day(-35), "pane.close"))),
            (-31, oc(open_cancelled, event(day(-31), "pane.refresh", outcome="empty", surface="desktop"))),
            (-30, batch(shared, event(day(-30), "activation"))),
            (-28, oc(shared, browser(-28))),
            (-10, oc(open_passive, event(day(-10), "activation"))),
            (-7, batch(shared, event(day(-7), bucket="100ms_1s"))),
            (-5, oc(shared, event(day(-5), "pane.expand", surface="desktop"))),
            (-4, batch(claude_returning, event(day(-4), "pane"))),
            (-4, batch(claude_returning, event(day(-10), "activation"))),  # Offline first observation arrives late.
            (-3, batch(claude_returning, event(day(-3), "pane.tab"))),
            (-2, oc(open_passive, event(day(-2), "activation"))),
            (-1, batch(claude_passive, event(day(-1), "activation"))),
            (-1, oc(open_cancelled, event(day(-1), "pane.refresh", outcome="cancelled", surface="desktop"))),
            (0, claude_dashboard),
            (0, claude_dashboard),  # Exact retry after an acknowledgement was lost.
            (0, batch(claude_returning, event(day(0), outcome="error", bucket="5s_30s"))),
            (0, batch(claude_passive, event(day(0), "activation"))),
            (0, oc(shared, event(day(0), "help", outcome="rejected"))),
            (0, oc(open_new, event(day(-8), "timeline"), event(day(0), "doctor"), browser(0))),
            (0, oc(open_tomorrow, event(day(1), "activation"))),
        ]
        opener = build_opener(ProxyHandler({}))
        with (patch("telemetry.collector.utc_day", return_value=self.today) as clock,
              make_server("127.0.0.1", 0, self.app, handler_class=_QuietHandler) as server):
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                for offset, payload in visits:
                    clock.return_value = day(offset)
                    request = Request(f"http://127.0.0.1:{server.server_port}{EVENTS_PATH}",
                                      data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
                    with opener.open(request, timeout=5) as response:
                        self.assertEqual((response.status, response.read()), (204, b""))
            finally:
                server.shutdown()
                worker.join(timeout=5)

        self.assertEqual(self.count("events"), 24)
        self.assertEqual(self.count("installations"), 9)
        stats = report(self.database, today=self.today, days=14)
        self.assertEqual(len(stats["daily"]), 28)
        self.assertEqual([(row["observed_installations"], row["new_installations"], row["active_today"],
                           row["active_last_30_days"], row["engaged_in_period"], row["activation_only_in_period"],
                           row["returning_in_period"], row["returning_engaged_in_period"]) for row in stats["adoption"]],
                         [(4, 2, 3, 3, 2, 1, 3, 2), (4, 2, 2, 4, 3, 1, 3, 2)])
        self.assertEqual([(row["invocations"], row["engaged_on_multiple_days"],
                           row["average_invocations_per_engaged_installation"],
                           row["average_days_per_engaged_installation"]) for row in stats["engagement"]],
                         [(6, 2, 3.0, 2.5), (5, 1, 1.67, 1.33)])
        self.assertEqual([(row["eligible_installations"], row["returned_installations"], row["rate"])
                          for row in stats["retention"]], [(3, 2, 0.6667), (2, 1, 0.5), (3, 1, 0.3333), (2, 2, 1.0)])
        features = {(row["adapter"], row["feature"]): row for row in stats["features"]}
        self.assertNotIn(("opencode", "timeline"), features)
        overview = features["claude-code", "overview"]
        self.assertEqual((overview["success_rate"], overview["p50_duration_bucket"], overview["p95_duration_bucket"]),
                         (0.5, "100ms_1s", "5s_30s"))
        for key, expected in [(('claude-code', 'dashboard.render'), {"observed": 1}),
                              (('opencode', 'dashboard.render'), {"observed": 1}),
                              (('opencode', 'pane.refresh'), {"cancelled": 1})]:
            feature = features[key]
            self.assertEqual((feature["outcomes"], feature["success_rate"], feature["p95_duration_bucket"]),
                             (expected, None, None))
        next_day = report(self.database, today=day(1), days=1)
        self.assertEqual(next_day["adoption"][1]["new_installations"], 1)
        prune(self.database, self.today, 32)
        self.assertEqual((self.count("events"), self.count("installations")), (20, 8))
        shortened = report(self.database, today=self.today, days=14, retention_days=32)
        self.assertEqual([(row["eligible_installations"], row["rate"]) for row in shortened["retention"]],
                         [(1, 1.0), (0, None), (2, 0.0), (1, 1.0)])

    def test_pruning_removes_inactive_ids_and_keeps_first_seen_for_retained_activity(self):
        active, inactive = uuid4().hex, uuid4().hex
        earliest = self.today - timedelta(days=45)
        recent = self.today - timedelta(days=24)
        self.assertEqual(self.request(batch(active, event(earliest)), arrival=earliest)[0], 204)
        self.assertEqual(self.request(batch(inactive, event(earliest)), arrival=earliest)[0], 204)
        self.assertEqual(self.request(batch(active, event(recent)), arrival=recent)[0], 204)
        prune(self.database, self.today, 32)
        self.assertEqual(self.count("events"), 1)
        self.assertEqual(self.count("installations"), 1)
        adoption = report(self.database, today=self.today, retention_days=32)["adoption"][0]
        self.assertEqual(adoption["observed_installations"], 1)
        self.assertEqual(adoption["new_installations"], 0)
        self.assertEqual(adoption["returning_engaged_in_period"], 1)

    def test_report_counts_activity_outcomes_versions_and_exact_day_returns(self):
        active, returning_late, activated = uuid4().hex, uuid4().hex, uuid4().hex
        first = self.today - timedelta(days=35)
        success = event(self.today, "dashboard", bucket="100ms_1s")
        error = event(self.today, "dashboard", outcome="error", bucket="gte_30s", surface="headless")
        empty = event(self.today, "dashboard", outcome="empty")
        for installation, activity in [
            (active, event(first, "activation")),
            (returning_late, event(first, "activation")),
            (active, event(first + timedelta(days=7), "activation")),
            (returning_late, event(first + timedelta(days=8))),
            (active, event(first + timedelta(days=30))),
        ]:
            self.assertEqual(self.request(batch(installation, activity), arrival=date.fromisoformat(activity["day"]))[0],
                             204)
        self.assertEqual(self.request(batch(active, success, error, empty))[0], 204)
        self.assertEqual(self.request(batch(activated, event(self.today, "activation", surface="desktop",
                                                            version="0.5.4"), adapter="opencode"))[0], 204)
        result = report(self.database, today=self.today, days=30)
        claude, opencode = result["adoption"]
        self.assertEqual(claude["active_today"], 1)
        self.assertEqual(claude["active_last_30_days"], 2)
        self.assertEqual(claude["returning_engaged_in_period"], 2)
        self.assertEqual(opencode["new_installations"], 1)
        self.assertEqual(opencode["activation_only_in_period"], 1)
        self.assertEqual(result["engagement"][0], {
            "adapter": "claude-code", "invocations": 5, "engaged_installations": 2,
            "engaged_on_multiple_days": 1, "average_invocations_per_engaged_installation": 2.5,
            "average_days_per_engaged_installation": 1.5,
        })
        feature = next(item for item in result["features"] if item["feature"] == "dashboard")
        self.assertEqual(feature["invocations"], 3)
        self.assertEqual(feature["executed"], 2)
        self.assertEqual(feature["success_rate"], 0.5)
        self.assertEqual(feature["error_rate"], 0.5)
        self.assertEqual(feature["p50_duration_bucket"], "100ms_1s")
        self.assertEqual(feature["p95_duration_bucket"], "gte_30s")
        self.assertEqual(feature["outcomes"], {"empty": 1, "error": 1, "success": 1})
        self.assertEqual([item["rate"] for item in result["retention"] if item["adapter"] == "claude-code"], [0.5, 0.5])
        self.assertIn({"adapter": "opencode", "version": "0.5.4", "installations": 1, "invocations": 0},
                      result["versions"])
        self.assertIn({"adapter": "claude-code", "surface": "headless", "installations": 1, "invocations": 1},
                      result["surfaces"])
        serialized = json.dumps(result)
        self.assertNotIn(active, serialized)
        self.assertNotIn(success["id"], serialized)

    def test_historical_report_requires_full_retained_window(self):
        report_day = self.today - timedelta(days=60)
        result = report(self.database, today=report_day, retained_on=self.today)
        self.assertEqual(result["as_of"], report_day.isoformat())
        self.assertEqual(len(result["daily"]), 60)
        with self.assertRaises(ValueError):
            report(self.database, today=report_day - timedelta(days=1), retained_on=self.today)


if __name__ == "__main__":
    unittest.main()
