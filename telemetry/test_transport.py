"""Exercise the isolated sender and aggregate CLI through the real HTTP receiver."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Thread
from wsgiref.simple_server import make_server

from .__main__ import _QuietHandler
from .collector import (
    EVENTS_PATH,
    create_app,
    create_production_app,
    database_connection,
)

ROOT = Path(__file__).resolve().parents[1]


def send_event(sender, cwd, env, adapter, surface, feature, outcome="success", duration=0, version="0.3.8"):
    subprocess.run([sys.executable, "-I", str(sender),
                    "--adapter", adapter, "--surface", surface, "--feature", feature,
                    "--outcome", outcome, "--duration-ms", str(duration), "--version", version],
                   env=env, cwd=cwd, timeout=10, check=True, capture_output=True)


def test_isolated_senders_and_report(tmp_path, client_bridge):
    sender = client_bridge / "telemetry_client.py"
    database = tmp_path / "usage.sqlite3"
    queue = tmp_path / "queue"
    received = []
    collector = create_app(database)

    def app(environ, start_response):
        path = environ["PATH_INFO"]
        received.append((path, environ.get("HTTP_USER_AGENT")))
        if path == EVENTS_PATH and environ.get("SERVER_PORT") == str(redirect_port):
            start_response("302 Found", [("Location", "/redirect-target"), ("Content-Length", "0")])
            return []
        return collector(environ, start_response)

    with (make_server("127.0.0.1", 0, app, handler_class=_QuietHandler) as server,
          make_server("127.0.0.1", 0, app, handler_class=_QuietHandler) as redirect):
        redirect_port = redirect.server_port
        for listener in (server, redirect):
            Thread(target=listener.serve_forever, daemon=True).start()
        env = {**os.environ, "TRAJVIZ_OBS_TELEMETRY_URL": f"http://127.0.0.1:{server.server_port}{EVENTS_PATH}",
               "TRAJVIZ_OBS_TELEMETRY_STATE_DIR": str(queue), "TRAJVIZ_OBS_TELEMETRY": "0",
               "TRAJVIZ_OBS_TELEMETRY_TOKEN": "",
               "http_proxy": "http://127.0.0.1:1", "no_proxy": ""}

        def send(adapter, surface, feature, outcome="success", duration=0, version="0.3.8"):
            send_event(sender, tmp_path, env, adapter, surface, feature, outcome, duration, version)

        try:
            send("claude-code", "terminal", "help")
            assert not received
            assert not queue.exists()
            env["TRAJVIZ_OBS_TELEMETRY"] = "1"
            send("claude-code", "terminal", "activation")
            send("claude-code", "terminal", "activation")
            send("claude-code", "terminal", "help", duration=80)
            send("opencode", "unknown", "activation", version="0.5.4")
            send("opencode", "desktop", "dashboard", "error", 1264, "0.5.4")
            env["TRAJVIZ_OBS_TELEMETRY_URL"] = f"http://127.0.0.1:{redirect_port}{EVENTS_PATH}"
            send("opencode", "headless", "tool", version="0.5.4")
        finally:
            for listener in (server, redirect):
                listener.shutdown()

    assert received == [(EVENTS_PATH, "TrajViz-Obs-Telemetry/1")] * 5
    result = subprocess.run([sys.executable, "-m", "telemetry", "--database", str(database), "report", "--days", "1"],
                            cwd=ROOT, check=True, capture_output=True, timeout=10, text=True)
    summary = json.loads(result.stdout)
    assert [(entry["adapter"], entry["active_today"]) for entry in summary["adoption"]] == [
        ("claude-code", 1), ("opencode", 1),
    ]
    features = {entry["feature"]: entry for entry in summary["features"]}
    assert features["help"]["outcomes"] == {"success": 1}
    assert features["dashboard"]["p95_duration_bucket"] == "1s_5s"
    assert features["dashboard"]["error_rate"] == 1
    assert features.keys() == {"help", "dashboard"}
    with database_connection(database, read_only=True) as connection:
        identities = connection.execute("SELECT installation_id FROM installations").fetchall()
    assert len({row[0] for row in identities}) == 2
    assert all(row[0] not in result.stdout for row in identities)


def test_cohort_reconciles_after_lost_acknowledgement(tmp_path, client_bridge):
    sender = client_bridge / "telemetry_client.py"
    database = tmp_path / "cohort.sqlite3"
    collector = create_app(database)
    tab_attempts = []

    def app(environ, start_response):
        body = environ["wsgi.input"].read(int(environ["CONTENT_LENGTH"]))
        environ["wsgi.input"] = io.BytesIO(body)
        events = json.loads(body)["events"]
        tabs = [event["id"] for event in events if event["feature"] == "dashboard.tab"]
        lose_ack = tabs and not tab_attempts
        tab_attempts.extend(tabs)

        def respond(status, headers):
            # Simulate a transient edge failure after the collector commits the batch.
            if lose_ack and status.startswith("204"):
                start_response("503 Service Unavailable", [("Content-Length", "0")])
            else:
                start_response(status, headers)

        return collector(environ, respond)

    with make_server("127.0.0.1", 0, app, handler_class=_QuietHandler) as server:
        Thread(target=server.serve_forever, daemon=True).start()
        base_env = {**os.environ, "TRAJVIZ_OBS_TELEMETRY_URL": f"http://127.0.0.1:{server.server_port}{EVENTS_PATH}",
                    "TRAJVIZ_OBS_TELEMETRY_TOKEN": ""}

        def send(profile, adapter, surface, feature, outcome="success", *, enabled=True):
            env = {**base_env, "TRAJVIZ_OBS_TELEMETRY": str(int(enabled)),
                   "TRAJVIZ_OBS_TELEMETRY_STATE_DIR": str(tmp_path / profile)}
            version = "0.3.8" if adapter == "claude-code" else "0.5.4"
            send_event(sender, tmp_path, env, adapter, surface, feature, outcome, version=version)

        try:
            send("alice", "claude-code", "terminal", "activation")
            send("alice", "claude-code", "terminal", "help")
            send("alice", "claude-code", "browser", "dashboard.tab", "observed")
            send("alice", "claude-code", "terminal", "overview")
            assert len(tab_attempts) == 1
            # Advance only the retry deadline to avoid a wall-clock wait in this test.
            queue, = (tmp_path / "alice").glob("client-*.sqlite3")
            with closing(sqlite3.connect(queue)) as connection, connection:
                assert connection.execute("SELECT count(*) FROM pending").fetchone()[0] == 2
                connection.execute("UPDATE sender SET retry_at = 0")
            send("alice", "claude-code", "terminal", "pane.refresh", "empty")
            send("alice", "opencode", "unknown", "activation")
            send("alice", "opencode", "headless", "tool", "cancelled")
            send("bob", "opencode", "unknown", "activation")
            send("bob", "opencode", "desktop", "pane")
            send("bob", "opencode", "desktop", "pane.refresh", "error")
            send("bob", "opencode", "browser", "dashboard.render", "observed")
            send("carol", "claude-code", "headless", "activation")
            for adapter in ("claude-code", "opencode"):
                send("off", adapter, "terminal", "overview", enabled=False)
            assert not (tmp_path / "off").exists()
        finally:
            server.shutdown()

    assert len(tab_attempts) == 2 and tab_attempts[0] == tab_attempts[1]
    with database_connection(database, read_only=True) as connection:
        ids = {row[0] for row in connection.execute("SELECT installation_id FROM installations")}
        features = Counter(tuple(row) for row in connection.execute("SELECT adapter, feature FROM events"))
    assert len(ids) == 4
    assert features == {
        ("claude-code", "activation"): 2, ("claude-code", "help"): 1, ("claude-code", "dashboard.tab"): 1,
        ("claude-code", "overview"): 1, ("claude-code", "pane.refresh"): 1, ("opencode", "activation"): 2,
        ("opencode", "tool"): 1, ("opencode", "pane"): 1, ("opencode", "pane.refresh"): 1,
        ("opencode", "dashboard.render"): 1,
    }
    result = subprocess.run([sys.executable, "-m", "telemetry", "--database", str(database), "report", "--days", "2"],
                            cwd=ROOT, check=True, capture_output=True, timeout=10, text=True)
    summary = json.loads(result.stdout)
    assert [(row["adapter"], row["new_installations"], row["engaged_in_period"]) for row in summary["adoption"]] == [
        ("claude-code", 2, 1), ("opencode", 2, 2),
    ]
    assert [row["invocations"] for row in summary["engagement"]] == [4, 4]
    assert all(identity not in result.stdout for identity in ids)


def test_production_ingest_rotation_and_private_status(tmp_path, client_bridge):
    sender = client_bridge / "telemetry_client.py"
    database = tmp_path / "usage.sqlite3"
    registry = tmp_path / "credentials.json"

    def command(*args):
        return subprocess.run([sys.executable, "-m", "telemetry", *args], cwd=ROOT, timeout=10,
                              check=True, text=True, capture_output=True).stdout

    def credentials(*args):
        return command("credentials", "--file", str(registry), *args)

    claude_token = credentials("issue", "claude-pilot").strip()
    opencode_token = credentials("issue", "opencode-pilot").strip()
    assert set(credentials("list").splitlines()) == {"claude-pilot", "opencode-pilot"}
    app = create_production_app(database, credentials=registry)
    with make_server("127.0.0.1", 0, app, handler_class=_QuietHandler) as server:
        Thread(target=server.serve_forever, daemon=True).start()
        base = {**os.environ, "TRAJVIZ_OBS_TELEMETRY": "1",
                "TRAJVIZ_OBS_TELEMETRY_URL": f"http://127.0.0.1:{server.server_port}{EVENTS_PATH}"}
        claude = {**base, "TRAJVIZ_OBS_TELEMETRY_TOKEN": claude_token,
                  "TRAJVIZ_OBS_TELEMETRY_STATE_DIR": str(tmp_path / "claude")}
        opencode = {**base, "TRAJVIZ_OBS_TELEMETRY_TOKEN": opencode_token,
                    "TRAJVIZ_OBS_TELEMETRY_STATE_DIR": str(tmp_path / "opencode")}

        def status(env, adapter):
            result = subprocess.run([sys.executable, "-I", str(sender), "--status", "--adapter", adapter],
                                    env=env, cwd=tmp_path, timeout=10, check=True, text=True, capture_output=True)
            assert env["TRAJVIZ_OBS_TELEMETRY_TOKEN"] not in result.stdout + result.stderr
            return json.loads(result.stdout)

        try:
            assert status(claude, "claude-code")["queue_state"] == "absent"
            assert not (tmp_path / "claude").exists()
            send_event(sender, tmp_path, claude, "claude-code", "terminal", "help")
            send_event(sender, tmp_path, opencode, "opencode", "desktop", "help", version="0.5.4")
            assert status(claude, "claude-code")["last_success_at"] is not None
            assert status(opencode, "opencode")["queued"] == 0

            credentials("revoke", "claude-pilot")
            send_event(sender, tmp_path, claude, "claude-code", "terminal", "watch")
            blocked = status(claude, "claude-code")
            assert blocked["queued"] == 1 and blocked["last_http_status"] == 401

            replacement = credentials("issue", "claude-replacement").strip()
            claude["TRAJVIZ_OBS_TELEMETRY_TOKEN"] = replacement
            send_event(sender, tmp_path, claude, "claude-code", "terminal", "overview")
            assert status(claude, "claude-code")["queued"] == 0
        finally:
            server.shutdown()

    receiver_status = command("--database", str(database), "status", "--days", "2")
    totals = json.loads(receiver_status)["totals"]
    assert totals["accepted_requests"] == 3
    assert totals["accepted_events"] == 4
    assert totals["unauthorized_requests"] == 1
    for token in (claude_token, opencode_token, replacement):
        assert token not in receiver_status and token not in registry.read_text()
    with database_connection(database, read_only=True) as connection:
        observed = connection.execute("SELECT adapter, count(*) FROM installations GROUP BY adapter ORDER BY adapter")
        assert [tuple(row) for row in observed] == [("claude-code", 1), ("opencode", 1)]


def test_http_retry_after_and_permanent_rejection_do_not_block_healthy_events(tmp_path, client_bridge):
    sender = client_bridge / "telemetry_client.py"
    database = tmp_path / "usage.sqlite3"
    collector = create_app(database)
    attempts: list[list[str]] = []

    def app(environ, start_response):
        body = environ["wsgi.input"].read(int(environ["CONTENT_LENGTH"]))
        environ["wsgi.input"] = io.BytesIO(body)
        features = [event["feature"] for event in json.loads(body)["events"]]
        attempts.append(features)
        if len(attempts) == 1:
            start_response("429 Too Many Requests", [("Content-Length", "0"), ("Retry-After", "90")])
        elif "doctor" in features:
            start_response("400 Bad Request", [("Content-Length", "0")])
        else:
            return collector(environ, start_response)
        return []

    queue = tmp_path / "queue"
    with make_server("127.0.0.1", 0, app, handler_class=_QuietHandler) as server:
        Thread(target=server.serve_forever, daemon=True).start()
        env = {**os.environ, "TRAJVIZ_OBS_TELEMETRY": "1", "TRAJVIZ_OBS_TELEMETRY_TOKEN": "",
               "TRAJVIZ_OBS_TELEMETRY_URL": f"http://127.0.0.1:{server.server_port}{EVENTS_PATH}",
               "TRAJVIZ_OBS_TELEMETRY_STATE_DIR": str(queue)}

        def send(feature):
            send_event(sender, tmp_path, env, "opencode", "terminal", feature, version="0.5.4")

        def status():
            result = subprocess.run([sys.executable, "-I", str(sender), "--status", "--adapter", "opencode"],
                                    env=env, cwd=tmp_path, timeout=10, check=True, text=True, capture_output=True)
            return json.loads(result.stdout)

        try:
            started = datetime.now(UTC)
            for feature in ("help", "doctor", "watch"):
                send(feature)
            throttled = status()
            assert attempts == [["help"]]
            assert throttled["queued"] == 3 and throttled["last_http_status"] == 429
            assert datetime.fromisoformat(throttled["retry_at"]) >= started + timedelta(seconds=89)

            # Advance the client's persisted deadline without waiting 90 seconds in the suite.
            state_file, = queue.glob("client-*.sqlite3")
            with closing(sqlite3.connect(state_file)) as connection, connection:
                connection.execute("UPDATE sender SET retry_at = 0")
            for feature in ("overview", "pane", "bundle"):
                send(feature)
            recovered = status()
            assert ["doctor"] in attempts
            assert recovered["queued"] == 0 and recovered["rejected"] == 1
            assert recovered["last_success_at"] is not None
        finally:
            server.shutdown()

    with database_connection(database, read_only=True) as connection:
        observed = Counter(row[0] for row in connection.execute("SELECT feature FROM events"))
    assert observed == {"help": 1, "watch": 1, "overview": 1, "pane": 1, "bundle": 1}
