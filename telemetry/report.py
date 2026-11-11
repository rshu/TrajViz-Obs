"""Aggregate telemetry locally without returning installation or event identifiers."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from bridge.telemetry_protocol import ADAPTERS, DURATION_BUCKETS

from .collector import DEFAULT_RETENTION_DAYS, database_connection, retention_cutoff

REPORT_FORMAT = "trajviz-obs-usage-report"
REPORT_SCHEMA_VERSION = 1
_EXECUTED_OUTCOMES = ("success", "error")


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _percentile_bucket(buckets: dict[str, int], count: int, percentile: int) -> str | None:
    if count == 0:
        return None
    target = (count * percentile + 99) // 100
    for bucket in DURATION_BUCKETS:
        target -= buckets.get(bucket, 0)
        if target <= 0:
            return bucket
    return None


def _adoption(connection, start: str, end: str, cutoff: str, monthly_start: str) -> list[dict]:
    rows = connection.execute(
        """
        WITH installations_in_scope AS (
            SELECT i.adapter, count(*) AS observed_installations,
                   sum(first_day >= :start) AS new_installations
            FROM installations i
            WHERE first_day <= :end AND last_day >= :cutoff AND EXISTS (
                SELECT 1 FROM events e WHERE e.adapter = i.adapter
                AND e.installation_id = i.installation_id AND e.day BETWEEN :cutoff AND :end
            ) GROUP BY i.adapter
        ), activity AS (
            SELECT e.adapter,
                   count(DISTINCT CASE WHEN day = :end THEN installation_id END) AS active_today,
                   count(DISTINCT CASE WHEN day >= :month THEN installation_id END) AS active_last_30_days,
                   count(DISTINCT CASE WHEN day >= :start THEN installation_id END) AS active_in_period,
                   count(DISTINCT CASE WHEN day >= :start AND feature != 'activation'
                                       THEN installation_id END) AS engaged_in_period,
                   count(DISTINCT CASE WHEN day >= :start AND day > first_day
                                       THEN installation_id END) AS returning_in_period,
                   count(DISTINCT CASE WHEN day >= :start AND day > first_day AND feature != 'activation'
                                       THEN installation_id END) AS returning_engaged_in_period
            FROM events e JOIN installations USING (adapter, installation_id)
            WHERE day BETWEEN min(:start, :month) AND :end GROUP BY e.adapter
        )
        SELECT i.adapter, i.observed_installations, i.new_installations,
               coalesce(a.active_today, 0) AS active_today,
               coalesce(a.active_last_30_days, 0) AS active_last_30_days,
               coalesce(a.active_in_period, 0) AS active_in_period,
               coalesce(a.engaged_in_period, 0) AS engaged_in_period,
               coalesce(a.active_in_period - a.engaged_in_period, 0) AS activation_only_in_period,
               coalesce(a.returning_in_period, 0) AS returning_in_period,
               coalesce(a.returning_engaged_in_period, 0) AS returning_engaged_in_period
        FROM installations_in_scope i LEFT JOIN activity a USING (adapter)
        """,
        {"start": start, "end": end, "cutoff": cutoff, "month": monthly_start},
    )
    by_adapter = {row["adapter"]: dict(row) for row in rows}
    empty = {
        "observed_installations": 0, "new_installations": 0, "active_today": 0, "active_last_30_days": 0,
        "active_in_period": 0, "engaged_in_period": 0, "activation_only_in_period": 0,
        "returning_in_period": 0, "returning_engaged_in_period": 0,
    }
    return [by_adapter.get(adapter, {"adapter": adapter, **empty}) for adapter in ADAPTERS]


def _daily(connection, start: str, today: date, days: int) -> list[dict]:
    rows = connection.execute(
        """
        SELECT day, e.adapter, count(DISTINCT installation_id) AS active_installations,
               count(DISTINCT CASE WHEN feature != 'activation' THEN installation_id END) AS engaged_installations,
               count(DISTINCT CASE WHEN first_day = day THEN installation_id END) AS new_installations,
               count(DISTINCT CASE WHEN first_day < day THEN installation_id END) AS returning_installations,
               sum(feature != 'activation') AS invocations
        FROM events e JOIN installations USING (adapter, installation_id)
        WHERE day BETWEEN ? AND ? GROUP BY day, e.adapter
        """,
        (start, today.isoformat()),
    )
    by_day = {(row["day"], row["adapter"]): dict(row) for row in rows}
    empty = {
        "active_installations": 0, "engaged_installations": 0, "new_installations": 0, "returning_installations": 0,
        "invocations": 0,
    }
    dates = ((today - timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1))
    return [by_day.get((day, adapter), {"day": day, "adapter": adapter, **empty})
            for day in dates for adapter in ADAPTERS]


def _features(connection, period: tuple[str, str]) -> list[dict]:
    rows = connection.execute(
        """
        SELECT adapter, feature, count(*) AS invocations, count(DISTINCT installation_id) AS installations
        FROM events WHERE day BETWEEN ? AND ? AND feature != 'activation'
        GROUP BY adapter, feature ORDER BY adapter, invocations DESC, feature
        """,
        period,
    )
    features = {(row["adapter"], row["feature"]): {**dict(row), "outcomes": {}, "duration_buckets": {}}
                for row in rows}
    dimensions = connection.execute(
        """
        SELECT adapter, feature, outcome, duration_bucket, count(*) AS count
        FROM events WHERE day BETWEEN ? AND ? AND feature != 'activation'
        GROUP BY adapter, feature, outcome, duration_bucket
        """,
        period,
    )
    for row in dimensions:
        feature = features[row["adapter"], row["feature"]]
        outcome, bucket, count = row["outcome"], row["duration_bucket"], row["count"]
        outcomes = feature["outcomes"]
        outcomes[outcome] = outcomes.get(outcome, 0) + count
        if outcome in _EXECUTED_OUTCOMES:
            buckets = feature["duration_buckets"]
            buckets[bucket] = buckets.get(bucket, 0) + count
    for feature in features.values():
        outcomes = feature["outcomes"]
        executed = sum(outcomes.get(outcome, 0) for outcome in _EXECUTED_OUTCOMES)
        feature.update(executed=executed, success_rate=_rate(outcomes.get("success", 0), executed),
                       error_rate=_rate(outcomes.get("error", 0), executed),
                       p50_duration_bucket=_percentile_bucket(feature["duration_buckets"], executed, 50),
                       p95_duration_bucket=_percentile_bucket(feature["duration_buckets"], executed, 95))
    return list(features.values())


def _engagement(connection, period: tuple[str, str]) -> list[dict]:
    rows = connection.execute(
        """
        WITH per_installation AS (
            SELECT adapter, count(*) AS invocations, count(DISTINCT day) AS engaged_days
            FROM events WHERE day BETWEEN ? AND ? AND feature != 'activation'
            GROUP BY adapter, installation_id
        )
        SELECT adapter, sum(invocations) AS invocations, count(*) AS engaged_installations,
               sum(engaged_days > 1) AS engaged_on_multiple_days,
               round(avg(invocations), 2) AS average_invocations_per_engaged_installation,
               round(avg(engaged_days), 2) AS average_days_per_engaged_installation
        FROM per_installation GROUP BY adapter
        """,
        period,
    )
    by_adapter = {row["adapter"]: dict(row) for row in rows}
    empty = {
        "invocations": 0, "engaged_installations": 0, "engaged_on_multiple_days": 0,
        "average_invocations_per_engaged_installation": None, "average_days_per_engaged_installation": None,
    }
    return [by_adapter.get(adapter, {"adapter": adapter, **empty}) for adapter in ADAPTERS]


def _retention(connection, cutoff: str, end: str) -> list[dict]:
    rows = connection.execute(
        """
        WITH offsets(offset) AS (VALUES (7), (30))
        SELECT i.adapter, offset AS days_after_first_seen, count(*) AS eligible_installations,
               sum(EXISTS (
                   SELECT 1 FROM events e
                   WHERE e.adapter = i.adapter AND e.installation_id = i.installation_id
                   AND e.day = date(i.first_day, '+' || offsets.offset || ' days')
               )) AS returned_installations
        FROM installations i CROSS JOIN offsets
        WHERE i.first_day >= ? AND date(i.first_day, '+' || offsets.offset || ' days') <= ?
        GROUP BY i.adapter, offset
        """,
        (cutoff, end),
    )
    by_offset = {(row["adapter"], row["days_after_first_seen"]): dict(row) for row in rows}
    results = []
    for adapter in ADAPTERS:
        for offset in (7, 30):
            row = by_offset.get((adapter, offset), {
                "adapter": adapter, "days_after_first_seen": offset,
                "eligible_installations": 0, "returned_installations": 0,
            })
            row["rate"] = _rate(row["returned_installations"], row["eligible_installations"])
            results.append(row)
    return results


def report(database: Path, *, today: date, days: int = 30, retention_days: int = DEFAULT_RETENTION_DAYS,
           retained_on: date | None = None) -> dict:
    """Return rolling UTC adoption, engagement, outcomes, and exact-day return rates."""
    retained_on = retained_on if retained_on is not None else today
    cutoff = retention_cutoff(retained_on, retention_days)
    if not 1 <= days <= retention_days:
        raise ValueError("Report days must be between 1 and the configured retention.")
    if not date.fromisoformat(cutoff) + timedelta(days=max(days, 30) - 1) <= today <= retained_on:
        raise ValueError("The requested report period and its 30-day activity window must fit within retention.")
    start = (today - timedelta(days=days - 1)).isoformat()
    period = (start, today.isoformat())
    monthly_start = (today - timedelta(days=29)).isoformat()
    with database_connection(database, read_only=True) as connection:
        connection.execute("BEGIN")
        surfaces = connection.execute(
            """
            SELECT adapter, surface, count(*) AS invocations, count(DISTINCT installation_id) AS installations
            FROM events WHERE day BETWEEN ? AND ? AND feature != 'activation'
            GROUP BY adapter, surface ORDER BY adapter, surface
            """,
            period,
        )
        versions = connection.execute(
            """
            SELECT adapter, version, sum(feature != 'activation') AS invocations,
                   count(DISTINCT installation_id) AS installations
            FROM events WHERE day BETWEEN ? AND ?
            GROUP BY adapter, version ORDER BY adapter, version
            """,
            period,
        )
        return {
            "format": REPORT_FORMAT, "schema_version": REPORT_SCHEMA_VERSION,
            "as_of": period[1], "period_start": start, "retained_from": cutoff, "retention_days": retention_days,
            "adoption": _adoption(connection, start, period[1], cutoff, monthly_start),
            "daily": _daily(connection, start, today, days),
            "engagement": _engagement(connection, period),
            "features": _features(connection, period),
            "surfaces": [dict(row) for row in surfaces],
            "versions": [dict(row) for row in versions],
            "retention": _retention(connection, cutoff, period[1]),
        }
