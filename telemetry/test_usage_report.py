"""Check distinct rolling activity and outcome populations in portable reports."""

from datetime import date, timedelta
from uuid import uuid4

from bridge.telemetry_protocol import (
    AGENT_VERSION_SCHEMA,
    SCHEMA_VERSION,
    validate_payload,
)

from .collector import (
    DEFAULT_RETENTION_DAYS,
    _record,
    database_connection,
    init_database,
)
from .report import report

TODAY = date(2026, 11, 18)


def _record_events(database, adapter, installation, events, *, host=None, plugin="0.5.4"):
    rows = []
    for offset, feature, outcome in events:
        rows.append({
            "id": uuid4().hex,
            "day": (TODAY + timedelta(days=offset)).isoformat(),
            "surface": "browser" if outcome == "observed" else "terminal",
            "feature": feature,
            "outcome": outcome,
            "duration_bucket": "lt_100ms" if outcome == "observed" or feature == "activation" else (
                "gte_30s" if outcome == "cancelled" else "1s_5s"
            ),
            "version": plugin,
            **({"agent_version": host} if host is not None else {}),
        })
    for row in rows:
        batch = validate_payload({
            "schema_version": AGENT_VERSION_SCHEMA if host is not None else SCHEMA_VERSION,
            "adapter": adapter, "installation_id": installation, "events": [row],
        })
        _record(database, batch, date.fromisoformat(row["day"]), DEFAULT_RETENTION_DAYS)


def test_rolling_activity_deduplicates_and_counts_returns_before_each_window(tmp_path):
    database = tmp_path / "usage.sqlite3"
    init_database(database)
    shared, week_boundary, month_boundary, dormant, future = (uuid4().hex for _ in range(5))
    _record_events(database, "claude-code", shared, [
        (-33, "activation", "success"), (-4, "overview", "success"), (0, "overview", "success"),
    ])
    _record_events(database, "claude-code", week_boundary, [
        (-6, "activation", "success"), (-5, "overview", "success"), (0, "overview", "success"),
    ])
    _record_events(database, "claude-code", dormant, [(-32, "activation", "success")])
    _record_events(database, "opencode", shared, [(-29, "activation", "success"), (-7, "overview", "success")])
    _record_events(database, "opencode", month_boundary, [(-30, "activation", "success"), (-6, "overview", "success")])
    _record_events(database, "opencode", future, [(1, "activation", "success")])

    data = report(database, today=TODAY, days=1, retained_on=TODAY + timedelta(days=1))
    rolling = data["usage"]["rolling"]
    assert rolling["last_7_days"] == {
        "start": (TODAY - timedelta(days=6)).isoformat(), "active_installations": 3, "returning_installations": 2,
    }
    assert rolling["last_30_days"] == {
        "start": (TODAY - timedelta(days=29)).isoformat(), "active_installations": 4, "returning_installations": 2,
    }
    claude, opencode, pi = rolling["by_adapter"]
    assert pi["last_7_days"]["active_installations"] == 0
    assert claude["adapter"] == "claude-code" and opencode["adapter"] == "opencode"
    assert [claude[window]["active_installations"] for window in ("last_7_days", "last_30_days")] == [2, 2]
    assert [opencode[window]["active_installations"] for window in ("last_7_days", "last_30_days")] == [1, 2]
    assert [opencode[window]["returning_installations"] for window in ("last_7_days", "last_30_days")] == [1, 1]
    assert [row["new_installations"] for row in data["daily"]] == [0, 0, 0]
    assert shared not in str(data)


def test_usage_outcomes_split_agent_versions_and_keep_non_comparable_records(tmp_path):
    database = tmp_path / "usage.sqlite3"
    init_database(database)
    claude, opencode = uuid4().hex, uuid4().hex
    _record_events(database, "claude-code", claude, [
        (0, "analyze", "success"), (0, "analyze", "error"), (0, "analyze", "cancelled"),
        (0, "analyze", "empty"), (0, "analyze", "rejected"), (0, "analyze", "confirmation"),
        (0, "dashboard.render", "observed"), (0, "converge.tab", "observed"),
        (0, "converge-cli", "success"), (0, "pane.refresh", "cancelled"), (0, "activation", "success"),
    ], host="2.1.311")
    _record_events(database, "claude-code", claude, [(0, "analyze", "success")], host="2.1.312", plugin="0.5.5")
    _record_events(database, "claude-code", claude, [(0, "analyze", "error")])
    _record_events(database, "opencode", opencode, [(0, "analyze", "success")], host="2.2.5")

    data = report(database, today=TODAY, days=1)
    features = {(row["adapter"], row["agent_version"], row["feature"]): row for row in data["usage"]["features"]}
    analyze = features["claude-code", "2.1.311", "analyze"]
    assert analyze["category"] == "command" and analyze["installations"] == 1 and analyze["invocations"] == 6
    assert analyze["completed"] == 3
    assert [analyze[key] for key in ("success_rate", "error_rate", "cancelled_rate")] == [0.3333] * 3
    assert analyze["outcomes"] == {outcome: 1 for outcome in (
        "success", "error", "cancelled", "empty", "rejected", "confirmation",
    )}
    assert analyze["duration_buckets"] == {"1s_5s": 2}
    assert analyze["p50_duration_bucket"] == analyze["p95_duration_bucket"] == "1s_5s"
    assert features["claude-code", None, "analyze"]["error_rate"] == 1.0
    assert features["claude-code", "2.1.312", "analyze"]["success_rate"] == 1.0
    assert features["opencode", "2.2.5", "analyze"]["success_rate"] == 1.0
    assert features["claude-code", "2.1.311", "converge-cli"]["category"] == "command"
    pane = features["claude-code", "2.1.311", "pane.refresh"]
    assert pane["category"] == "pane" and pane["cancelled_rate"] == 1.0 and pane["p95_duration_bucket"] is None
    for feature in ("dashboard.render", "converge.tab"):
        browser = features["claude-code", "2.1.311", feature]
        assert browser["category"] == "dashboard" and browser["outcomes"] == {"observed": 1}
        assert browser["completed"] == 0 and browser["success_rate"] is None
    assert all(row["feature"] != "activation" for row in features.values())
    legacy = next(row for row in data["features"] if row["adapter"] == "claude-code" and row["feature"] == "analyze")
    assert legacy["invocations"] == 8 and legacy["executed"] == 4
    assert legacy["success_rate"] == legacy["error_rate"] == 0.5 and "cancelled_rate" not in legacy

    with database_connection(database) as connection:
        connection.execute("ALTER TABLE events DROP COLUMN agent_version")
    legacy_database = report(database, today=TODAY, days=1)
    assert all(row["agent_version"] is None for row in legacy_database["usage"]["features"])
    assert len(legacy_database["usage"]["features"]) == len(legacy_database["features"])
