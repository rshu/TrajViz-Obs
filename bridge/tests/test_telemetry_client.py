"""Test the plugin telemetry transport without contacting an external service."""

from __future__ import annotations

import http.client
import json
import os
import sqlite3
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta
from email.message import Message
from email.utils import format_datetime
from threading import Event
from uuid import uuid4

import pytest

import telemetry_client as client
from telemetry_protocol import DURATION_BUCKETS, duration_bucket, validate_payload
from trajviz_obs.dashboard_usage import DashboardUsage

ENDPOINT = "http://127.0.0.1:18752/v1/events"
TOKEN = "a" * 42 + "E"
ROTATED = "b" * 42 + "I"
ACCEPTED = client._Delivery("accepted")
ARGV = ["--adapter", "claude-code", "--surface", "terminal", "--feature", "overview",
        "--outcome", "success", "--duration-ms", "350", "--version", "0.3.8"]


@pytest.fixture
def opted_in(monkeypatch, tmp_path):
    state = tmp_path / "telemetry"
    monkeypatch.setenv("TRAJVIZ_OBS_TELEMETRY", "1")
    monkeypatch.setenv("TRAJVIZ_OBS_TELEMETRY_URL", ENDPOINT)
    monkeypatch.setenv("TRAJVIZ_OBS_TELEMETRY_STATE_DIR", str(state))
    monkeypatch.delenv("TRAJVIZ_OBS_TELEMETRY_TOKEN", raising=False)
    return state


@pytest.mark.parametrize("url", [
    "https://user:secret@example.test/v1/events", "https://example.test/v1/events?token=x",
    "https://example.test/v1/events#fragment", "http://example.test/v1/events",
    "https://example.test/other", "https://example.test:0/v1/events",
    "https://example.test:90000/v1/events", "https://[bad/v1/events", "https://e\nxample.test/v1/events",
])
def test_unusable_endpoints_never_create_local_state(opted_in, monkeypatch, url):
    monkeypatch.setenv("TRAJVIZ_OBS_TELEMETRY_URL", url)
    assert client.main(ARGV) == 0
    assert not opted_in.exists()


@pytest.mark.parametrize("consent", [None, "0", "true", "yes", " 1", "1 "])
def test_disabled_sender_never_creates_state(opted_in, monkeypatch, consent):
    if consent is None:
        monkeypatch.delenv("TRAJVIZ_OBS_TELEMETRY")
    else:
        monkeypatch.setenv("TRAJVIZ_OBS_TELEMETRY", consent)
    assert client.main(ARGV) == 0
    assert not opted_in.exists()


@pytest.mark.parametrize("url", [
    "https://collector.example.test:9443/v1/events",
    "http://127.0.0.1/v1/events",
    "http://localhost:18752/v1/events",
    "http://[::1]:18752/v1/events",
])
def test_allowed_endpoints_keep_their_exact_configured_scope(url):
    settings = client.usage_settings({"TRAJVIZ_OBS_TELEMETRY": "1", "TRAJVIZ_OBS_TELEMETRY_URL": url}, "opencode")
    assert settings.configuration == "ready" and settings.endpoint == url


@pytest.mark.parametrize("adapter", ["claude-code", "opencode"])
def test_consent_controls_the_local_fallback_without_sending_a_remote_token(opted_in, monkeypatch, capsys, adapter):
    monkeypatch.delenv("TRAJVIZ_OBS_TELEMETRY")
    monkeypatch.delenv("TRAJVIZ_OBS_TELEMETRY_URL")
    monkeypatch.setenv("TRAJVIZ_OBS_TELEMETRY_TOKEN", TOKEN)
    sent = []

    def send(endpoint, batch, *, token):
        sent.append((endpoint, batch["adapter"], token))
        return ACCEPTED

    monkeypatch.setattr(client, "_send", send)
    assert client.main([*ARGV, "--adapter", adapter]) == 0
    assert not opted_in.exists()

    assert client.main(["--set-consent", "on", "--adapter", adapter]) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["configuration"] == "ready"
    assert response["credential_configured"] is False
    assert response["queue_state"] == "absent"
    marker = opted_in / f"{adapter}-consent"
    assert list(opted_in.iterdir()) == [marker]
    assert marker.read_bytes() == b"on\n"
    if os.name != "nt":
        assert marker.stat().st_mode & 0o777 == 0o600
    assert not sent

    dashboard = DashboardUsage.from_environment(adapter, "0.3.8")
    assert dashboard is not None
    assert dashboard._target() == ("http://127.0.0.1:8765/v1/events", opted_in, None)
    assert client.main([*ARGV, "--adapter", adapter]) == 0
    assert sent == [("http://127.0.0.1:8765/v1/events", adapter, None)]


@pytest.mark.parametrize(("url", "token", "expected"), [
    ("http://non-loopback.example/v1/events", None, "invalid_endpoint"),
    ("https://collector.example/v1/events", "invalid-secret", "invalid_token"),
])
def test_rejected_enable_does_not_change_the_saved_choice(opted_in, monkeypatch, capsys, url, token, expected):
    assert client.main(["--set-consent", "off", "--adapter", "claude-code"]) == 0
    capsys.readouterr()
    marker = opted_in / "claude-code-consent"
    monkeypatch.setenv("TRAJVIZ_OBS_TELEMETRY_URL", url)
    if token is not None:
        monkeypatch.setenv("TRAJVIZ_OBS_TELEMETRY_TOKEN", token)
    assert client.main(["--set-consent", "on", "--adapter", "claude-code"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["configuration"] == expected
    assert url not in output.out and "invalid-secret" not in output.out and not output.err
    assert marker.read_bytes() == b"off\n"
    assert client.delivery_status(client.os.environ, "claude-code")["configuration"] == "disabled"


def test_failed_atomic_save_preserves_previous_consent(opted_in, monkeypatch, capsys):
    assert client.main(["--set-consent", "on", "--adapter", "claude-code"]) == 0
    capsys.readouterr()

    def failed_replace(*_args):
        raise OSError("synthetic write failure")

    monkeypatch.setattr(client.os, "replace", failed_replace)
    assert client.main(["--set-consent", "off", "--adapter", "claude-code"]) == 1
    output = capsys.readouterr()
    assert not output.out and not output.err
    marker = opted_in / "claude-code-consent"
    assert list(opted_in.iterdir()) == [marker]
    assert marker.read_bytes() == b"on\n"


def test_destination_is_private_persistent_and_independent_of_consent(opted_in, monkeypatch, capsys):
    adapter = ["--adapter", "claude-code"]
    saved = "https://192.0.2.10:9443/v1/events"
    default = "http://127.0.0.1:8765/v1/events"
    monkeypatch.delenv("TRAJVIZ_OBS_TELEMETRY")
    monkeypatch.delenv("TRAJVIZ_OBS_TELEMETRY_URL")
    assert client.main(["--destination", *adapter]) == 0
    status = json.loads(capsys.readouterr().out)
    assert (status["destination"], status["destination_source"], status["configuration"]) == (
        default, "default", "disabled",
    )
    assert not opted_in.exists()

    assert client.main(["--set-destination", saved, *adapter]) == 0
    status = json.loads(capsys.readouterr().out)
    assert (status["destination"], status["destination_source"], status["configuration"]) == (saved, "saved", "disabled")
    marker, = opted_in.iterdir()
    assert marker.name == "claude-code-destination.json"
    if os.name != "nt":
        assert marker.stat().st_mode & 0o777 == 0o600
    assert client.main(["--destination", "--adapter", "opencode"]) == 0
    assert json.loads(capsys.readouterr().out)["destination"] == default

    monkeypatch.setenv("TRAJVIZ_OBS_TELEMETRY", "1")
    assert client.delivery_status(client.os.environ, "claude-code")["configuration"] == "ready"
    assert client.delivery_status(client.os.environ, "opencode")["configuration"] == "missing_endpoint"
    assert client.main(["--set-consent", "off", *adapter]) == 0
    assert saved not in capsys.readouterr().out
    monkeypatch.setenv("TRAJVIZ_OBS_TELEMETRY_URL", ENDPOINT)
    assert client.main(["--set-destination", "default", *adapter]) == 0
    status = json.loads(capsys.readouterr().out)
    assert (status["destination"], status["destination_source"], status["configuration"]) == (ENDPOINT, "launch", "disabled")
    assert not marker.exists()
    assert (opted_in / "claude-code-consent").read_bytes() == b"off\n"


@pytest.mark.parametrize("base", [
    "https://telemetry.example.com",
    "https://127.0.0.1",
    "https://[::1]:8443/",
    "https://[::ffff:192.0.2.10]",
    "https://[fe80::1%25en0]:8443/",
    "http://127.0.0.1:8765",
])
def test_destination_accepts_base_url_and_saves_exact_event_endpoint(base, opted_in, capsys):
    endpoint = f"{base.rstrip('/')}/v1/events"
    assert client.main(["--set-destination", base, "--adapter", "claude-code"]) == 0
    assert json.loads(capsys.readouterr().out)["destination"] == endpoint
    marker = opted_in / "claude-code-destination.json"
    assert json.loads(marker.read_text())["url"] == endpoint


def test_destination_switch_never_forwards_an_unbound_launch_token(opted_in, monkeypatch, capsys):
    adapter = ["--adapter", "claude-code"]
    base = "https://collector.example.test"
    saved = f"{base}/v1/events"
    marker = opted_in / "claude-code-destination.json"
    attempts = []

    def send(endpoint, _payload, *, token):
        attempts.append((endpoint, token))
        return ACCEPTED

    monkeypatch.setattr(client, "_send", send)
    monkeypatch.setenv("TRAJVIZ_OBS_TELEMETRY_TOKEN", TOKEN)
    assert client.main(ARGV) == 0
    assert client.main(["--set-destination", base, *adapter]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["credential_configured"] is False
    assert client.main(ARGV) == 0
    assert attempts == [(ENDPOINT, TOKEN), (saved, None)]

    assert client.main(["--set-destination", saved, "--use-launch-token", *adapter]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["credential_configured"] is True
    assert TOKEN not in output and TOKEN not in marker.read_text()
    assert client.main(ARGV) == 0
    assert attempts[-1] == (saved, TOKEN)

    monkeypatch.setenv("TRAJVIZ_OBS_TELEMETRY_TOKEN", ROTATED)
    assert client.main(ARGV) == 0
    assert attempts == [(ENDPOINT, TOKEN), (saved, None), (saved, TOKEN)]
    assert client.delivery_status(client.os.environ, "claude-code")["configuration"] == "invalid_token"
    monkeypatch.setenv("TRAJVIZ_OBS_TELEMETRY_URL", saved)
    assert client.main(ARGV) == 0
    assert attempts[-1] == (saved, ROTATED)


@pytest.mark.parametrize("tamper", ["url", "adapter"])
def test_destination_binding_cannot_move_to_another_endpoint_or_adapter(opted_in, monkeypatch, capsys, tamper):
    adapter = "claude-code"
    bound = "https://collector.example.test/v1/events"
    monkeypatch.setenv("TRAJVIZ_OBS_TELEMETRY_TOKEN", TOKEN)
    assert client.main(["--set-destination", bound, "--use-launch-token", "--adapter", adapter]) == 0
    capsys.readouterr()
    marker = opted_in / "claude-code-destination.json"
    document = json.loads(marker.read_text())
    assert document["token_binding"] != client._credential_fingerprint(TOKEN)
    if tamper == "url":
        document["url"] = "https://another.example.test/v1/events"
    else:
        adapter = "opencode"
        marker = opted_in / "opencode-destination.json"
    marker.write_text(json.dumps(document))

    monkeypatch.setattr(client, "_send", lambda *_args, **_kwargs: pytest.fail("invalid binding delivered"))
    assert client.main([*ARGV, "--adapter", adapter]) == 0
    assert not list(opted_in.glob("*.sqlite3"))
    assert client.main(["--destination", "--adapter", adapter]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["destination"] == document["url"]
    assert status["configuration"] == "invalid_token" and status["credential_invalid"] is True


def test_legacy_destination_binding_needs_an_explicit_launch_url_or_rebinding(opted_in, monkeypatch, capsys):
    adapter = "claude-code"
    bound = "https://collector.example.test/v1/events"
    monkeypatch.setenv("TRAJVIZ_OBS_TELEMETRY_TOKEN", TOKEN)
    assert client.main(["--set-consent", "on", "--adapter", adapter]) == 0
    capsys.readouterr()
    marker = opted_in / "claude-code-destination.json"
    marker.write_text(json.dumps({"url": bound, "token_hash": client._credential_fingerprint(TOKEN)}))
    assert client.queue_usage(bound, opted_in, adapter=adapter, surface="terminal", feature="help",
                              outcome="success", duration_ms=0, version="0.3.8")

    status = client.delivery_status(client.os.environ, adapter)
    assert status["configuration"] == "invalid_token" and status["queued"] == 1
    with monkeypatch.context() as local:
        local.setenv("TRAJVIZ_OBS_TELEMETRY_URL", bound)
        assert client.usage_settings(client.os.environ, adapter).token == TOKEN

    assert client.main(["--set-destination", bound, "--use-launch-token", "--adapter", adapter]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["configuration"] == "ready" and status["queued"] == 1 and status["credential_invalid"] is False
    assert (opted_in / "claude-code-consent").read_bytes() == b"on\n"


@pytest.mark.parametrize("token", [None, ROTATED])
def test_destination_query_reports_invalid_credential_while_consent_is_off(opted_in, monkeypatch, capsys, token):
    adapter = ["--adapter", "claude-code"]
    monkeypatch.setenv("TRAJVIZ_OBS_TELEMETRY_TOKEN", TOKEN)
    assert client.main(["--set-destination", "https://collector.example.test", "--use-launch-token", *adapter]) == 0
    assert client.main(["--set-consent", "off", *adapter]) == 0
    capsys.readouterr()
    if token is None:
        monkeypatch.delenv("TRAJVIZ_OBS_TELEMETRY_TOKEN")
    else:
        monkeypatch.setenv("TRAJVIZ_OBS_TELEMETRY_TOKEN", token)
    assert client.main(["--destination", *adapter]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["configuration"] == "disabled" and status["credential_invalid"] is True
    assert "credential_invalid" not in client.delivery_status(client.os.environ, "claude-code")


def test_destination_query_reports_invalid_launch_credential_while_consent_is_off(opted_in, monkeypatch, capsys):
    monkeypatch.delenv("TRAJVIZ_OBS_TELEMETRY")
    monkeypatch.setenv("TRAJVIZ_OBS_TELEMETRY_TOKEN", "invalid-launch-secret")
    assert client.main(["--destination", "--adapter", "claude-code"]) == 0
    output = capsys.readouterr().out
    status = json.loads(output)
    assert status["configuration"] == "disabled" and status["destination"] == ENDPOINT
    assert status["credential_invalid"] is True and "invalid-launch-secret" not in output
    assert not opted_in.exists()


def test_invalid_destination_updates_preserve_the_previous_route(opted_in, monkeypatch, capsys):
    adapter = ["--adapter", "opencode"]
    saved = "https://collector.example.test/v1/events"
    assert client.main(["--set-destination", saved, *adapter]) == 0
    capsys.readouterr()

    for command, error in [
        (["--set-destination", "http://192.0.2.10:8443"], "invalid_endpoint"),
        (["--set-destination", "http://192.0.2.10:8443/v1/events"], "invalid_endpoint"),
        (["--set-destination", "https://host.example/dashboard"], "invalid_endpoint"),
        (["--set-destination", "https://host.example/?secret=example"], "invalid_endpoint"),
        (["--set-destination", "https://collector.example.test:/v1/events"], "invalid_endpoint"),
        (["--set-destination", "https://collector!.example.test/v1/events"], "invalid_endpoint"),
        (["--set-destination", "https://collector.example.test%3a9443"], "invalid_endpoint"),
        (["--set-destination", "https://user%40collector.example.test"], "invalid_endpoint"),
        (["--set-destination", "https://[::1%3a123]"], "invalid_endpoint"),
        (["--set-destination", "https://host.example/v1/events?secret=example"], "invalid_endpoint"),
        (["--set-destination", ENDPOINT, "--use-launch-token"], "missing_token"),
        (["--set-destination", "default", "--use-launch-token"], "invalid_arguments"),
    ]:
        assert client.main([*command, *adapter]) == 0
        output = capsys.readouterr().out
        status = json.loads(output)
        assert status["destination_error"] == error and status["destination"] == saved
        assert "secret=example" not in output

    monkeypatch.setenv("TRAJVIZ_OBS_TELEMETRY_TOKEN", "invalid-secret")
    assert client.main(["--set-destination", ENDPOINT, "--use-launch-token", *adapter]) == 0
    output = capsys.readouterr().out
    status = json.loads(output)
    assert status["destination_error"] == "invalid_token" and status["destination"] == saved
    assert "invalid-secret" not in output
    (opted_in / "opencode-destination.json").write_text('{"url":"wrong"}')
    monkeypatch.setattr(client, "_send", lambda *_args, **_kwargs: pytest.fail("invalid destination delivered"))
    assert client.main([*ARGV, *adapter]) == 0
    assert client.main(["--destination", *adapter]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["destination"] is None and status["destination_source"] == "invalid"
    assert status["configuration"] == "invalid_endpoint" and not list(opted_in.glob("*.sqlite3"))
    assert client.main(["--set-destination", "default", *adapter]) == 0
    assert json.loads(capsys.readouterr().out)["destination"] == ENDPOINT


def test_destination_change_routes_actions_from_an_existing_dashboard(opted_in, monkeypatch, capsys):
    new_destination = "http://127.0.0.1:18753/v1/events"
    sending_old, release_old, sending_new = Event(), Event(), Event()
    dashboard = DashboardUsage.from_environment("claude-code", "0.3.8")
    assert dashboard is not None
    attempts = []

    def send(endpoint, payload, *, token):
        attempts.append((endpoint, token, [event["feature"] for event in payload["events"]]))
        if endpoint == ENDPOINT:
            sending_old.set()
            release_old.wait(timeout=5)
            return client._Delivery("retry", "network")
        sending_new.set()
        return ACCEPTED

    monkeypatch.setattr(client, "_send", send)
    dashboard._enqueue("dashboard.render")
    try:
        assert sending_old.wait(timeout=5)
        assert client.main(["--set-destination", new_destination, "--adapter", "claude-code"]) == 0
        assert json.loads(capsys.readouterr().out)["destination"] == new_destination
        dashboard._enqueue("dashboard.tab")
    finally:
        release_old.set()
    assert sending_new.wait(timeout=5)
    client.flush_usage(ENDPOINT, opted_in, "claude-code", env=os.environ)
    assert attempts == [(ENDPOINT, None, ["dashboard.render"]), (new_destination, None, ["dashboard.tab"])]
    with closing(sqlite3.connect(client._queue_path(opted_in, "claude-code", ENDPOINT))) as queue:
        assert queue.execute("SELECT count(*) FROM pending").fetchone() == (1,)


def test_rebinding_a_stale_credential_activates_an_existing_opted_in_dashboard(opted_in, monkeypatch, capsys):
    adapter = ["--adapter", "claude-code"]
    endpoint = "https://collector.example.test/v1/events"
    bind = ["--set-destination", endpoint, "--use-launch-token", *adapter]
    monkeypatch.setenv("TRAJVIZ_OBS_TELEMETRY_TOKEN", TOKEN)
    assert client.main(["--set-consent", "on", *adapter]) == 0
    assert client.main(bind) == 0
    capsys.readouterr()
    monkeypatch.setenv("TRAJVIZ_OBS_TELEMETRY_TOKEN", ROTATED)

    attempts = []
    sent = Event()

    def send(target, payload, *, token):
        attempts.append((target, token, [event["feature"] for event in payload["events"]]))
        sent.set()
        return ACCEPTED

    monkeypatch.setattr(client, "_send", send)
    dashboard = DashboardUsage.from_environment("claude-code", "0.3.8")
    assert dashboard is not None
    dashboard._enqueue("dashboard.render")
    assert not list(opted_in.glob("*.sqlite3"))

    assert client.main(bind) == 0
    assert json.loads(capsys.readouterr().out)["configuration"] == "ready"
    dashboard._enqueue("dashboard.tab")
    assert sent.wait(timeout=5)
    assert attempts == [(endpoint, ROTATED, ["dashboard.tab"])]


def test_switching_destination_between_claim_and_send_releases_the_old_lease(opted_in, monkeypatch, capsys):
    adapter = "opencode"
    assert client.queue_usage(ENDPOINT, opted_in, adapter=adapter, surface="desktop", feature="help",
                              outcome="success", duration_ms=0, version="0.5.4")
    claim = client._claim

    def claim_then_switch(*args, **kwargs):
        result = claim(*args, **kwargs)
        assert result is not None
        assert client.main(["--set-destination", "https://collector.example.test/v1/events", "--adapter", adapter]) == 0
        assert json.loads(capsys.readouterr().out)["destination_source"] == "saved"
        return result

    monkeypatch.setattr(client, "_claim", claim_then_switch)
    monkeypatch.setattr(client, "_send", lambda *_args, **_kwargs: pytest.fail("old destination delivery"))
    client.flush_usage(ENDPOINT, opted_in, adapter, env=os.environ)
    with closing(sqlite3.connect(client._queue_path(opted_in, adapter, ENDPOINT))) as queue:
        assert queue.execute("SELECT lease, lease_until FROM sender").fetchone() == (None, 0)
        assert queue.execute("SELECT count(*) FROM pending").fetchone() == (1,)


def test_disable_pauses_a_running_dashboard_and_only_its_adapters_queue(opted_in, monkeypatch, capsys):
    attempts = []

    def send(_endpoint, payload, *, token):
        attempts.append(payload)
        if len(attempts) == 1:
            return client._Delivery("retry", "network")
        return ACCEPTED

    monkeypatch.setattr(client, "_send", send)
    dashboard = DashboardUsage.from_environment("claude-code", "0.3.8")
    assert dashboard is not None
    assert client.main(ARGV) == 0
    assert client.main(["--set-consent", "off", "--adapter", "claude-code"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["configuration"] == "disabled" and status["queued"] == 1
    queue = client._queue_path(opted_in, "claude-code", ENDPOINT)
    with closing(sqlite3.connect(queue)) as db, db:
        db.execute("UPDATE sender SET retry_at = 0")

    # This object launched while opt-in came from the environment.
    dashboard._enqueue("dashboard.render")
    client.flush_usage(ENDPOINT, opted_in, "claude-code")
    assert client.main(ARGV) == 0
    assert len(attempts) == 1
    assert client.delivery_status(client.os.environ, "claude-code")["queued"] == 1
    assert DashboardUsage.from_environment("claude-code", "0.3.8") is None

    assert client.main([*ARGV, "--adapter", "opencode"]) == 0
    assert attempts[-1]["adapter"] == "opencode"
    monkeypatch.delenv("TRAJVIZ_OBS_TELEMETRY")
    assert client.main(["--set-consent", "on", "--adapter", "claude-code"]) == 0
    assert json.loads(capsys.readouterr().out)["queued"] == 1
    assert len(attempts) == 2
    assert client.main([*ARGV, "--feature", "help"]) == 0
    assert len(attempts) == 3 and attempts[-1]["adapter"] == "claude-code"
    assert attempts[-1]["events"][0]["id"] == attempts[0]["events"][0]["id"]
    assert client.delivery_status(client.os.environ, "claude-code")["queued"] == 0


def test_disabling_between_claim_and_send_releases_the_lease(opted_in, monkeypatch):
    adapter = "opencode"
    assert client.queue_usage(ENDPOINT, opted_in, adapter=adapter, surface="desktop", feature="help",
                              outcome="success", duration_ms=0, version="0.5.4")
    claim = client._claim

    def claim_then_disable(*args, **kwargs):
        result = claim(*args, **kwargs)
        assert result is not None
        client._write_consent(opted_in, adapter, "off")
        return result

    monkeypatch.setattr(client, "_claim", claim_then_disable)
    monkeypatch.setattr(client, "_send", lambda *_args, **_kwargs: pytest.fail("disabled delivery"))
    client.flush_usage(ENDPOINT, opted_in, adapter)
    with closing(sqlite3.connect(client._queue_path(opted_in, adapter, ENDPOINT))) as db:
        assert db.execute("SELECT lease, lease_until FROM sender").fetchone() == (None, 0)
        assert db.execute("SELECT count(*) FROM pending").fetchone() == (1,)


@pytest.mark.parametrize("statement", ["BEGIN IMMEDIATE", "INSERT INTO pending"])
def test_disabling_during_queue_write_does_not_record_an_event(opted_in, monkeypatch, capsys, statement):
    adapter = "claude-code"
    open_queue = client._open_queue
    writing, resume = Event(), Event()

    def enqueue(feature):
        return client.queue_usage(ENDPOINT, opted_in, adapter=adapter, surface="terminal", feature=feature,
                                  outcome="success", duration_ms=0, version="0.3.8")

    def traced_queue(*args):
        connection = open_queue(*args)

        def trace(sql):
            if sql.startswith(statement):
                writing.set()
                resume.wait(timeout=5)

        connection.set_trace_callback(trace)
        return connection

    assert enqueue("help")
    monkeypatch.setattr(client, "_open_queue", traced_queue)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(enqueue, "overview")
        try:
            assert writing.wait(timeout=5)
            assert client.main(["--set-consent", "off", "--adapter", adapter]) == 0
            status = json.loads(capsys.readouterr().out)
            assert status["configuration"] == "disabled" and status["queued"] == 1
        finally:
            resume.set()
        assert not pending.result(timeout=5)

    assert client.main(["--set-consent", "on", "--adapter", adapter]) == 0
    assert json.loads(capsys.readouterr().out)["queued"] == 1
    sent = []

    def send(_endpoint, payload, *, token):
        sent.extend(event["feature"] for event in payload["events"])
        return ACCEPTED

    monkeypatch.setattr(client, "_open_queue", open_queue)
    monkeypatch.setattr(client, "_send", send)
    client.flush_usage(ENDPOINT, opted_in, adapter)
    assert sent == ["help"]


def test_activation_and_commands_share_an_id_only_within_the_same_adapter_and_endpoint(opted_in, monkeypatch):
    sent = []

    def send(url, payload, *, token):
        sent.append((url, validate_payload(payload)))
        assert token is None
        return ACCEPTED

    monkeypatch.setattr(client, "_send", send)
    activation = ["--feature", "activation", "--duration-ms", "0"]
    assert client.main([*ARGV, *activation]) == client.main([*ARGV, *activation]) == client.main(ARGV) == 0
    assert len(sent) == 2
    assert [batch["events"][0]["feature"] for _, batch in sent] == ["activation", "overview"]
    ids = [batch["installation_id"] for _, batch in sent]
    assert ids[0] == ids[1]
    assert sent[1][1]["events"][0]["duration_bucket"] == "100ms_1s"
    assert client.main([*ARGV, "--adapter", "opencode", "--surface", "desktop"]) == 0
    monkeypatch.setenv("TRAJVIZ_OBS_TELEMETRY_URL", "https://collector.example.test/v1/events")
    assert client.main(ARGV) == 0
    assert len({batch["installation_id"] for _, batch in sent}) == 3


def test_sender_retries_with_the_same_event_ids_after_backoff(opted_in, monkeypatch):
    batches = []

    def send(_endpoint, payload, *, token):
        batches.append(payload)
        return ACCEPTED if len(batches) > 1 else client._Delivery("retry", "network")

    monkeypatch.setattr(client, "_send", send)
    assert client.main(ARGV) == client.main(ARGV) == 0
    assert len(batches) == 1
    with closing(sqlite3.connect(next(opted_in.glob("*.sqlite3")))) as database, database:
        database.execute("UPDATE sender SET retry_at = 0")
    assert client.main([*ARGV, "--feature", "dashboard.html"]) == 0
    retry = batches[1]
    assert len(retry["events"]) == 3
    assert retry["events"][0]["id"] == batches[0]["events"][0]["id"]
    assert retry["installation_id"] == batches[0]["installation_id"]
    with closing(sqlite3.connect(next(opted_in.glob("*.sqlite3")))) as database:
        assert database.execute("SELECT COUNT(*) FROM pending").fetchone() == (0,)


def test_retry_after_is_honored_and_bounded_with_jitter(monkeypatch):
    deadline = format_datetime(datetime.fromtimestamp(120, client._UTC), usegmt=True)
    assert client._retry_after(deadline, 100) == 20
    assert client._retry_after(deadline, 200) == 0
    assert client._retry_after("60", 100) == 60
    assert client._retry_after("99999999", 100) == client.MAX_RETRY_SECONDS
    assert client._retry_after("invalid", 100) is None
    monkeypatch.setattr(client.random, "uniform", lambda low, high: (low + high) / 2)
    assert 30 < client._retry_delay(1, None) <= 36
    assert 120 < client._retry_delay(1, 120) <= 144
    assert client._retry_delay(8, 99999999) == client.MAX_RETRY_SECONDS


@pytest.mark.parametrize(("code", "kind", "failure"), [
    (200, "blocked", "endpoint"), (204, "accepted", None),
    (400, "payload", "payload_rejected"), (413, "payload", "payload_rejected"), (422, "payload", "payload_rejected"),
    (401, "blocked", "authentication"), (403, "blocked", "authentication"),
    (404, "blocked", "endpoint"), (302, "blocked", "endpoint"), (415, "blocked", "endpoint"),
    (408, "retry", "collector_unavailable"), (425, "retry", "collector_unavailable"), (429, "retry", "throttled"),
    (500, "retry", "collector_unavailable"), (501, "blocked", "endpoint"), (503, "retry", "collector_unavailable"),
    (505, "blocked", "endpoint"),
])
def test_http_results_are_classified(code, kind, failure):
    response = client._http_result(code, "120")
    assert (response.kind, response.failure) == (kind, failure)
    assert response.retry_after == (120 if kind == "retry" else None)


def test_sender_uses_bearer_header_and_keeps_retry_hint(monkeypatch):
    headers = Message()
    headers["Retry-After"] = "44"
    seen = []

    class Opener:
        def open(self, request, *, timeout):
            seen.append(request)
            assert timeout == client.NETWORK_TIMEOUT
            raise urllib.error.HTTPError(request.full_url, 429, "not used", headers, None)

    monkeypatch.setattr(client.urllib.request, "build_opener", lambda *_: Opener())
    assert client._send(ENDPOINT, {}, token=TOKEN) == client._Delivery("retry", "throttled", 429, 44)
    request, = seen
    assert request.full_url == ENDPOINT
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"
    assert TOKEN.encode() not in request.data


@pytest.mark.parametrize("error", [TimeoutError(), http.client.BadStatusLine("invalid status line")])
def test_sender_treats_timeout_and_malformed_http_as_retryable(monkeypatch, error):
    class Opener:
        def open(self, *_args, **_kwargs):
            raise error

    monkeypatch.setattr(client.urllib.request, "build_opener", lambda *_: Opener())
    assert client._send(ENDPOINT, {}, token=TOKEN) == client._Delivery("retry", "network")


def test_invalid_token_never_sends_or_creates_state(opted_in, monkeypatch, capsys):
    monkeypatch.setenv("TRAJVIZ_OBS_TELEMETRY_TOKEN", "\r\ninvalid-secret")
    monkeypatch.setattr(client, "_send", lambda *_args, **_kwargs: pytest.fail("unexpected delivery"))
    assert client.main(ARGV) == 0
    assert client.main(["--status", "--adapter", "claude-code"]) == 0
    output = capsys.readouterr().out
    status = json.loads(output)
    assert status["configuration"] == "invalid_token"
    assert status["credential_configured"] is True
    assert status["queue_state"] == "absent" and status["queued"] == 0
    assert "invalid-secret" not in output and ENDPOINT not in output
    assert not opted_in.exists()


def test_rotated_credential_retries_without_resetting_identity(opted_in, monkeypatch):
    monkeypatch.setenv("TRAJVIZ_OBS_TELEMETRY_TOKEN", TOKEN)
    sent = []

    def send(_endpoint, payload, *, token):
        sent.append((payload, token))
        return ACCEPTED if token == ROTATED else client._http_result(401)

    monkeypatch.setattr(client, "_send", send)
    assert client.main(ARGV) == client.main(ARGV) == 0
    assert len(sent) == 1
    status = client.delivery_status(client.os.environ, "claude-code")
    assert status["queued"] == 2 and status["last_failure"] == "authentication"
    assert status["last_http_status"] == 401 and status["retry_at"] is not None
    assert status["last_success_at"] is None

    monkeypatch.setenv("TRAJVIZ_OBS_TELEMETRY_TOKEN", ROTATED)
    status = client.delivery_status(client.os.environ, "claude-code")
    assert status["retry_at"] is None
    assert client.main(ARGV) == 0
    assert len(sent) == 2 and sent[1][1] == ROTATED
    assert sent[1][0]["installation_id"] == sent[0][0]["installation_id"]
    assert sent[1][0]["events"][0]["id"] == sent[0][0]["events"][0]["id"]
    status = client.delivery_status(client.os.environ, "claude-code")
    assert status["queued"] == 0 and status["last_success_at"] is not None
    assert not any(value in json.dumps(status) for value in (TOKEN, ROTATED, ENDPOINT, sent[0][0]["installation_id"]))
    queue, = opted_in.glob("*.sqlite3")
    assert TOKEN.encode() not in queue.read_bytes() and ROTATED.encode() not in queue.read_bytes()


def test_permanent_payload_rejection_isolates_one_row_across_bounded_runs(opted_in, monkeypatch):
    sent = []
    delivered = []
    for feature in ["help", *(["overview"] * 16)]:
        assert client.queue_usage(ENDPOINT, opted_in, adapter="claude-code", surface="terminal", feature=feature,
                                  outcome="success", duration_ms=350, version="0.3.8")

    def send(_endpoint, payload, *, token):
        events = payload["events"]
        sent.append(len(events))
        if any(event["feature"] == "help" for event in events):
            return client._http_result(400)
        delivered.extend(event["id"] for event in events)
        return ACCEPTED

    monkeypatch.setattr(client, "_send", send)
    for _ in range(5):
        before = len(sent)
        client.flush_usage(ENDPOINT, opted_in, "claude-code")
        assert len(sent) - before <= client.MAX_BATCHES_PER_RUN
    assert sent[:5] == [17, 8, 4, 2, 1]
    assert len(set(delivered)) == len(delivered) == 16
    status = client.delivery_status(client.os.environ, "claude-code")
    assert status["queued"] == 0 and status["rejected"] == 1
    assert status["last_failure"] == "payload_rejected" and status["last_http_status"] == 400
    assert status["last_success_at"] is not None


def test_status_reads_legacy_queue_without_migrating_it(opted_in, monkeypatch):
    monkeypatch.setattr(client, "_send", lambda *_args, **_kwargs: pytest.fail("unexpected delivery"))
    assert client.delivery_status(client.os.environ, "claude-code")["queue_state"] == "absent"
    assert not opted_in.exists()
    assert client.queue_usage(ENDPOINT, opted_in, adapter="claude-code", surface="terminal", feature="help",
                              outcome="success", duration_ms=350, version="0.3.8")
    queue, = opted_in.glob("*.sqlite3")
    with closing(sqlite3.connect(queue)) as database, database:
        database.execute("DROP TABLE sender_health")
        installation = database.execute("SELECT installation_id FROM sender").fetchone()[0]
    before = queue.read_bytes()
    monkeypatch.delenv("TRAJVIZ_OBS_TELEMETRY")
    status = client.delivery_status(client.os.environ, "claude-code")
    assert status["configuration"] == "disabled"
    assert status["queue_state"] == "present" and status["queued"] == 1
    assert status["last_success_at"] is None and installation not in json.dumps(status)
    assert queue.read_bytes() == before
    assert client.queue_usage(ENDPOINT, opted_in, adapter="claude-code", surface="terminal", feature="help",
                              outcome="success", duration_ms=350, version="0.3.8")
    with closing(sqlite3.connect(queue)) as database:
        assert database.execute("SELECT installation_id FROM sender").fetchone()[0] == installation
        assert database.execute("SELECT count(*) FROM pending").fetchone()[0] == 2
        assert database.execute("SELECT rejected FROM sender_health").fetchone()[0] == 0


def test_status_handles_incomplete_health_schema_without_mutating_queue(opted_in, monkeypatch, capsys):
    monkeypatch.setattr(client, "_send", lambda *_args, **_kwargs: pytest.fail("unexpected delivery"))
    assert client.queue_usage(ENDPOINT, opted_in, adapter="claude-code", surface="terminal", feature="help",
                              outcome="success", duration_ms=0, version="0.3.8")
    queue, = opted_in.glob("*.sqlite3")
    with closing(sqlite3.connect(queue)) as database, database:
        database.execute("DROP TABLE sender_health")
        database.execute("CREATE TABLE sender_health (singleton INTEGER PRIMARY KEY)")
        database.execute("INSERT INTO sender_health VALUES (1)")
    before = queue.read_bytes()
    assert client.main(["--status", "--adapter", "claude-code"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["queue_state"] == "unavailable"
    assert queue.read_bytes() == before


def test_queue_is_bounded_and_a_single_sender_claims_a_batch(opted_in, monkeypatch):
    monkeypatch.setattr(client, "MAX_PENDING", 2)
    now, cutoff = "2026-10-20", "2026-10-13"
    timestamp = datetime.fromisoformat(now).replace(tzinfo=client._UTC).timestamp()
    event = {"day": now, "surface": "desktop", "feature": "view",
             "outcome": "success", "duration_bucket": "lt_100ms", "version": "0.5.4"}
    with (closing(client._open_queue(opted_in, "opencode", ENDPOINT)) as database,
          closing(client._open_queue(opted_in, "opencode", ENDPOINT)) as second):
        ids = [uuid4().hex for _ in range(4)]
        for event_id, day in zip(ids, ("2026-10-12", now, now, now), strict=True):
            client._record(database, {**event, "id": event_id, "day": day}, cutoff, opted_in, "opencode")
        lease, batch = client._claim(database, "opencode", timestamp)
        assert [row["id"] for row in batch["events"]] == ids[-2:]
        assert client._claim(second, "opencode", timestamp - 0.1) is None
        assert client._claim(second, "opencode", timestamp) is None
        next_lease, _ = client._claim(second, "opencode", timestamp + client.LEASE_SECONDS)
        client._finish(database, lease, batch, ACCEPTED, timestamp + 20)
        assert database.execute("SELECT COUNT(*) FROM pending").fetchone() == (2,)
        client._finish(second, next_lease, batch, ACCEPTED, timestamp + 20)
        assert database.execute("SELECT COUNT(*) FROM pending").fetchone() == (0,)
        assert database.execute("SELECT dropped, expired FROM sender_health").fetchone() == (1, 1)


def test_corrected_clock_unblocks_valid_events(opted_in):
    today, cutoff = "2026-11-11", "2026-11-04"
    timestamp = datetime.fromisoformat(today).replace(tzinfo=client._UTC).timestamp()
    event = {"id": uuid4().hex, "day": today, "surface": "desktop", "feature": "help",
             "outcome": "success", "duration_bucket": "lt_100ms", "version": "0.5.4"}
    with closing(client._open_queue(opted_in, "opencode", ENDPOINT)) as database:
        client._record(database, {**event, "id": uuid4().hex, "day": "2026-11-20"}, cutoff, opted_in, "opencode")
        with database:
            database.execute("UPDATE sender SET lease = 'old', lease_until = ?, retry_at = ?, failures = 7",
                             (timestamp + 900_000, timestamp + 900_000))
        client._record(database, event, cutoff, opted_in, "opencode")
        lease, payload = client._claim(database, "opencode", timestamp)
        assert lease != "old"
        assert payload["events"] == [event]
        assert database.execute("SELECT failures FROM sender").fetchone() == (0,)


def test_flush_discards_out_of_window_events_after_suspend(opted_in, monkeypatch):
    instant = datetime(2026, 11, 11, 12, tzinfo=client._UTC)
    event = {"surface": "desktop", "feature": "help", "outcome": "success",
             "duration_bucket": "lt_100ms", "version": "0.5.4"}
    days = [(instant.date() + timedelta(days=offset)).isoformat()
            for offset in (-client.BACKFILL_DAYS - 1, -client.BACKFILL_DAYS, 1, 2)]
    with closing(client._open_queue(opted_in, "opencode", ENDPOINT)) as database:
        for day in days:
            client._record(database, {**event, "id": uuid4().hex, "day": day}, days[0], opted_in, "opencode")
        assert database.execute("SELECT count(*) FROM pending").fetchone() == (4,)

    class Clock(datetime):
        @classmethod
        def now(cls, _timezone):
            return instant

    sent = []

    def send(_endpoint, batch, *, token):
        sent.extend(row["day"] for row in batch["events"])
        return ACCEPTED

    monkeypatch.setattr(client, "datetime", Clock)
    monkeypatch.setattr(client, "_send", send)
    client.flush_usage(ENDPOINT, opted_in, "opencode")
    assert sent == days[1:3]
    status = client.delivery_status(client.os.environ, "opencode")
    assert status["queued"] == 0 and status["expired"] == 2


def test_malformed_local_row_does_not_block_healthy_events(opted_in, monkeypatch):
    for feature in ("help", "overview"):
        assert client.queue_usage(ENDPOINT, opted_in, adapter="claude-code", surface="terminal",
                                  feature=feature, outcome="success", duration_ms=0, version="0.3.8")
    queue, = opted_in.glob("*.sqlite3")
    with closing(sqlite3.connect(queue)) as database, database:
        database.execute("UPDATE pending SET body = 'invalid JSON' WHERE rowid = 1")
    sent = []

    def send(_endpoint, batch, *, token):
        sent.extend(row["feature"] for row in batch["events"])
        return ACCEPTED

    monkeypatch.setattr(client, "_send", send)
    client.flush_usage(ENDPOINT, opted_in, "claude-code")
    assert sent == ["overview"]
    status = client.delivery_status(client.os.environ, "claude-code")
    assert status["queued"] == 0 and status["rejected"] == 1
    assert status["last_failure"] == "payload_rejected" and status["last_http_status"] is None


def test_concurrent_flushes_do_not_send_the_same_pending_batch(opted_in, monkeypatch):
    assert client.queue_usage(ENDPOINT, opted_in, adapter="opencode", surface="desktop", feature="help",
                              outcome="success", duration_ms=0, version="0.5.4")
    sending, release = Event(), Event()
    attempts = []

    def send(_endpoint, batch, *, token):
        attempts.append(batch)
        sending.set()
        assert release.wait(timeout=5)
        return ACCEPTED

    monkeypatch.setattr(client, "_send", send)
    with ThreadPoolExecutor(max_workers=5) as pool:
        active = pool.submit(client.flush_usage, ENDPOINT, opted_in, "opencode")
        try:
            assert sending.wait(timeout=5)
            concurrent = [pool.submit(client.flush_usage, ENDPOINT, opted_in, "opencode") for _ in range(4)]
            for worker in concurrent:
                worker.result(timeout=3)
        finally:
            release.set()
        active.result(timeout=5)
    assert len(attempts) == 1
    assert client.delivery_status(client.os.environ, "opencode")["queued"] == 0


def test_dashboard_persists_actions_while_delivery_is_stalled(opted_in, monkeypatch):
    sending = Event()
    release = Event()

    def stalled_send(_endpoint, _payload, *, token):
        sending.set()
        release.wait(timeout=5)
        return client._Delivery("retry", "network")

    monkeypatch.setattr(client, "_send", stalled_send)
    dashboard = DashboardUsage.from_environment("claude-code", "0.3.8")
    assert dashboard is not None
    dashboard._enqueue("dashboard.render")
    try:
        assert sending.wait(timeout=5)
        dashboard._enqueue("dashboard.tab")
        queue, = opted_in.glob("client-*.sqlite3")
        with closing(sqlite3.connect(queue)) as database:
            assert database.execute("SELECT count(*) FROM pending").fetchone() == (2,)
    finally:
        release.set()


def test_protocol_accepts_only_fixed_fields_and_duration_buckets():
    assert [duration_bucket(ms) for ms in (0, 100, 1000, 5000, 30_000)] == list(DURATION_BUCKETS)
    event = {"id": uuid4().hex, "day": "2026-11-11", "surface": "terminal", "feature": "view",
             "outcome": "success", "duration_bucket": "lt_100ms", "version": "0.3.8"}
    batch = {"schema_version": 1, "adapter": "claude-code", "installation_id": uuid4().hex, "events": [event]}
    assert validate_payload(batch) is batch
    for invalid in ({**event, "args": "private string"}, {**event, "feature": "private-string"},
                    {**event, "day": "2026-02-31"}, {**event, "version": "private version"}):
        with pytest.raises(ValueError):
            validate_payload({**batch, "events": [invalid]})
