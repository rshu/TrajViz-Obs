"""Versioned allowlist shared by the plugin sender and the usage collector."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

SCHEMA_VERSION = 1
MAX_BATCH = 32
MAX_BODY = 16 * 1024
BACKFILL_DAYS = 7
ADAPTERS = ("claude-code", "opencode")
SURFACES = ("terminal", "desktop", "headless", "browser", "unknown")
OUTCOMES = ("success", "error", "cancelled", "empty", "rejected", "confirmation", "observed")
DURATION_BUCKETS = ("lt_100ms", "100ms_1s", "1s_5s", "5s_30s", "gte_30s")
BROWSER_FEATURES = (
    "dashboard.render", "dashboard.tab", "dashboard.inspect", "dashboard.load", "dashboard.export",
    "dashboard.compare", "dashboard.analyze", "dashboard.attribution",
    "converge.render", "converge.tab", "converge.compare",
)
FEATURES = (
    "activation", "overview", "pane", "pane.close", "pane.refresh", "pane.tab", "pane.expand",
    "issues", "health", "patterns", "agents", "context", "timeline", "view",
    "json", "bundle", "schemas", "dashboard", "dashboard.html", "dashboard.stop",
    "converge", "converge.stop", "compare", "scorecard", "labels", "converge-cli",
    "analyze", "fixes", "label", "attribution", "collect", "sessions", "watch",
    "statusline", "setup", "doctor", "help", "brief", "tool", *BROWSER_FEATURES,
)

_UUID4 = re.compile(r"[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}")
_VERSION = re.compile(r"(?:0|[1-9][0-9]{0,3})(?:\.(?:0|[1-9][0-9]{0,3})){2}(?:-(?:alpha|beta|rc)\.[0-9]{1,3})?")
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_EVENT_FIELDS = {"id", "day", "surface", "feature", "outcome", "duration_bucket", "version"}
_PAYLOAD_FIELDS = {"schema_version", "adapter", "installation_id", "events"}


def duration_bucket(milliseconds: int) -> str:
    """Convert a command duration to one of the published histogram buckets."""
    if type(milliseconds) is not int or milliseconds < 0:
        raise ValueError("invalid duration")
    return DURATION_BUCKETS[sum(milliseconds >= boundary for boundary in (100, 1000, 5000, 30_000))]


def _matches(pattern: re.Pattern[str], value: Any) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def validate_event(event: Any) -> dict:
    if not isinstance(event, dict) or set(event) != _EVENT_FIELDS:
        raise ValueError("invalid event fields")
    if not _matches(_UUID4, event["id"]) or not _matches(_DAY, event["day"]):
        raise ValueError("invalid event identifier or day")
    date.fromisoformat(event["day"])
    if (event["surface"] not in SURFACES or event["feature"] not in FEATURES
            or event["outcome"] not in OUTCOMES or event["duration_bucket"] not in DURATION_BUCKETS
            or not _matches(_VERSION, event["version"])):
        raise ValueError("invalid event value")
    if event["feature"] == "activation" and (event["outcome"], event["duration_bucket"]) != ("success", "lt_100ms"):
        raise ValueError("invalid activation")
    if event["feature"] in BROWSER_FEATURES:
        if (event["surface"], event["outcome"], event["duration_bucket"]) != ("browser", "observed", "lt_100ms"):
            raise ValueError("invalid browser observation")
    elif event["surface"] == "browser" or event["outcome"] == "observed":
        raise ValueError("unexpected browser observation")
    return event


def validate_payload(payload: Any) -> dict:
    if not isinstance(payload, dict) or set(payload) != _PAYLOAD_FIELDS:
        raise ValueError("invalid batch fields")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported schema version")
    if payload["adapter"] not in ADAPTERS or not _matches(_UUID4, payload["installation_id"]):
        raise ValueError("invalid adapter or installation identifier")
    events = payload["events"]
    if not isinstance(events, list) or not 1 <= len(events) <= MAX_BATCH:
        raise ValueError("invalid event batch size")
    ids = {validate_event(event)["id"] for event in events}
    if len(ids) != len(events):
        raise ValueError("duplicate identifier within batch")
    return payload
