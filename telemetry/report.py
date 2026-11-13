"""Aggregate telemetry locally without returning installation or event identifiers."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from bridge.telemetry_protocol import ADAPTERS, DURATION_BUCKETS, valid_agent_version

from .collector import DEFAULT_RETENTION_DAYS, database_connection, retention_cutoff

REPORT_FORMAT = "trajviz-obs-usage-report"
REPORT_SCHEMA_VERSION = 3
_EXECUTED_OUTCOMES = ("success", "error")
_COMPLETED_OUTCOMES = (*_EXECUTED_OUTCOMES, "cancelled")


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


def _agent_version_sql(connection, columns: set[str]) -> str:
    if "agent_version" not in columns:
        return "NULL"

    def valid_version(value):
        return value if valid_agent_version(value) else None

    connection.create_function("report_agent_version", 1, valid_version, deterministic=True)
    return "report_agent_version(agent_version)"


def _activity(connection, start: str, today: date, days: int, monthly_start: str, columns: set[str],
              version: str) -> dict:
    address = ("CASE WHEN typeof(address_digest) = 'blob' AND length(address_digest) = 32 THEN address_digest END"
               if "address_digest" in columns else "NULL")
    # Adapter names and installation IDs are allowlisted, so ':' separates the
    # installation identity components unambiguously. Hex encodes the digest
    # only inside the count to identify distinct address/installation pairs.
    rows = connection.execute(
        f"""
        WITH base AS (
            SELECT adapter, installation_id, day, {address} AS address, {version} AS agent_version,
                   count(*) AS event_count
            FROM events WHERE day BETWEEN min(:start, :month) AND :end
            GROUP BY 1, 2, 3, 4, 5
        ), adapters(adapter) AS (VALUES (NULL), (:claude), (:opencode)),
        windows(scope, start) AS (VALUES ('today', :end), ('period', :start), ('last_30_days', :month)),
        scoped AS (
            SELECT scope, a.adapter AS group_adapter, NULL AS group_day, NULL AS group_version,
                   b.adapter, b.installation_id, b.address, b.agent_version, b.event_count
            FROM base b JOIN windows w ON b.day >= w.start
            JOIN adapters a ON a.adapter IS NULL OR a.adapter = b.adapter
            UNION ALL
            SELECT 'daily', a.adapter, b.day, NULL, b.adapter, b.installation_id, b.address, b.agent_version, b.event_count
            FROM base b JOIN adapters a ON a.adapter IS NULL OR a.adapter = b.adapter
            WHERE b.day >= :start
            UNION ALL
            SELECT 'version', adapter, NULL, agent_version, adapter, installation_id, address, agent_version, event_count
            FROM base WHERE day >= :start
        )
        SELECT scope, group_adapter, group_day, group_version,
               nullif(count(DISTINCT address), 0) AS distinct_addresses,
               count(DISTINCT adapter || ':' || installation_id) AS distinct_installations,
               nullif(count(DISTINCT CASE WHEN address IS NOT NULL
                     THEN hex(address) || ':' || adapter || ':' || installation_id END), 0)
                   AS distinct_address_installation_pairs,
               sum(event_count) AS events,
               sum(CASE WHEN address IS NOT NULL THEN event_count ELSE 0 END) AS events_with_address,
               sum(CASE WHEN agent_version IS NOT NULL THEN event_count ELSE 0 END) AS events_with_agent_version
        FROM scoped GROUP BY scope, group_adapter, group_day, group_version
        """,
        {"start": start, "month": monthly_start, "end": today.isoformat(), "claude": ADAPTERS[0], "opencode": ADAPTERS[1]},
    )
    aggregates = {}
    versions = []
    for row in rows:
        counts = dict(row)
        scope = counts.pop("scope")
        adapter = counts.pop("group_adapter")
        day = counts.pop("group_day")
        agent_version = counts.pop("group_version")
        if scope == "version":
            versions.append({"adapter": adapter, "agent_version": agent_version, **counts})
        else:
            aggregates[scope, adapter, day] = counts
    empty = {
        "distinct_addresses": None, "distinct_installations": 0, "distinct_address_installation_pairs": None,
        "events": 0, "events_with_address": 0, "events_with_agent_version": 0,
    }
    dates = [(today - timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)]

    def windows(adapter):
        result = {scope: aggregates.get((scope, adapter, None), empty.copy())
                  for scope in ("today", "period", "last_30_days")}
        result["daily"] = [{"day": day, **aggregates.get(("daily", adapter, day), empty)} for day in dates]
        return result

    versions.sort(key=lambda row: (row["adapter"], row["agent_version"] is None, row["agent_version"] or ""))
    return {**windows(None), "by_adapter": [{"adapter": adapter, **windows(adapter)} for adapter in ADAPTERS],
            "by_agent_version": versions}


def _rolling_activity(connection, weekly_start: str, monthly_start: str, end: str) -> dict:
    rows = connection.execute(
        """
        WITH active AS (
            SELECT adapter, installation_id, max(day) AS last_activity
            FROM events WHERE day BETWEEN :month AND :end
            GROUP BY adapter, installation_id
        )
        SELECT adapter, count(*) AS active_30,
               count(CASE WHEN first_day < :month THEN 1 END) AS returning_30,
               count(CASE WHEN last_activity >= :week THEN 1 END) AS active_7,
               count(CASE WHEN last_activity >= :week AND first_day < :week THEN 1 END) AS returning_7
        FROM active JOIN installations USING (adapter, installation_id)
        GROUP BY adapter
        """,
        {"week": weekly_start, "month": monthly_start, "end": end},
    )
    by_adapter = {row["adapter"]: dict(row) for row in rows}

    def windows(counts):
        return {
            f"last_{length}_days": {
                "start": start, "active_installations": counts.get(f"active_{length}", 0),
                "returning_installations": counts.get(f"returning_{length}", 0),
            }
            for length, start in ((7, weekly_start), (30, monthly_start))
        }

    # Installations are already scoped to their adapter.
    totals = {key: sum(row[key] for row in by_adapter.values())
              for key in ("active_7", "returning_7", "active_30", "returning_30")}
    return {**windows(totals), "by_adapter": [
        {"adapter": adapter, **windows(by_adapter.get(adapter, {}))} for adapter in ADAPTERS
    ]}


def _feature_category(feature: str) -> str:
    prefix = feature.partition(".")[0]
    return {"dashboard": "dashboard", "converge": "dashboard", "pane": "pane"}.get(prefix, "command")


def _features(connection, period: tuple[str, str], *, agent_version_sql: str | None = None) -> list[dict]:
    version = agent_version_sql or "NULL"
    rows = connection.execute(
        f"""
        SELECT adapter, feature, {version} AS host_version, count(*) AS invocations,
               count(DISTINCT installation_id) AS installations
        FROM events WHERE day BETWEEN ? AND ? AND feature != 'activation'
        GROUP BY adapter, feature, host_version ORDER BY adapter, invocations DESC, feature, host_version
        """,
        period,
    )
    features = {}
    for row in rows:
        feature = dict(row)
        host_version = feature.pop("host_version")
        key = (feature["adapter"], feature["feature"], host_version)
        if agent_version_sql is not None:
            feature.update(agent_version=host_version, category=_feature_category(feature["feature"]))
        features[key] = {**feature, "outcomes": {}, "duration_buckets": {}}
    dimensions = connection.execute(
        f"""
        SELECT adapter, feature, {version} AS host_version, outcome, duration_bucket, count(*) AS count
        FROM events WHERE day BETWEEN ? AND ? AND feature != 'activation'
        GROUP BY adapter, feature, host_version, outcome, duration_bucket
        """,
        period,
    )
    for row in dimensions:
        feature = features[row["adapter"], row["feature"], row["host_version"]]
        outcome, bucket, count = row["outcome"], row["duration_bucket"], row["count"]
        outcomes = feature["outcomes"]
        outcomes[outcome] = outcomes.get(outcome, 0) + count
        if outcome in _EXECUTED_OUTCOMES:
            buckets = feature["duration_buckets"]
            buckets[bucket] = buckets.get(bucket, 0) + count
    for feature in features.values():
        outcomes = feature["outcomes"]
        executed = sum(outcomes.get(outcome, 0) for outcome in _EXECUTED_OUTCOMES)
        eligible = executed
        if agent_version_sql is not None:
            eligible = sum(outcomes.get(outcome, 0) for outcome in _COMPLETED_OUTCOMES)
            feature.update(completed=eligible, cancelled_rate=_rate(outcomes.get("cancelled", 0), eligible))
        else:
            feature["executed"] = executed
        feature.update(success_rate=_rate(outcomes.get("success", 0), eligible),
                       error_rate=_rate(outcomes.get("error", 0), eligible),
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
    weekly_start = (today - timedelta(days=6)).isoformat()
    monthly_start = (today - timedelta(days=29)).isoformat()
    with database_connection(database, read_only=True) as connection:
        connection.execute("BEGIN")
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(events)")}
        agent_version_sql = _agent_version_sql(connection, columns)
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
            "activity": _activity(connection, start, today, days, monthly_start, columns, agent_version_sql),
            "usage": {
                "rolling": _rolling_activity(connection, weekly_start, monthly_start, period[1]),
                "features": _features(connection, period, agent_version_sql=agent_version_sql),
            },
        }
