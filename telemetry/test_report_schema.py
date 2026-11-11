"""Check the portable usage report against its published contract."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import date
from pathlib import Path
from uuid import uuid4

from jsonschema import Draft202012Validator

from bridge.telemetry_protocol import (
    _VERSION,
    ADAPTERS,
    DURATION_BUCKETS,
    FEATURES,
    OUTCOMES,
    SURFACES,
    validate_payload,
)

from .collector import DEFAULT_RETENTION_DAYS, _record, init_database
from .report import REPORT_FORMAT, REPORT_SCHEMA_VERSION, report

SCHEMA = Path(__file__).with_name("report.schema.json")


def test_report_schema_matches_format_and_ingest_allowlists():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    assert schema["properties"]["format"]["const"] == REPORT_FORMAT
    assert schema["properties"]["schema_version"]["const"] == REPORT_SCHEMA_VERSION
    definitions = schema["$defs"]
    assert definitions["adapter"]["enum"] == list(ADAPTERS)
    assert definitions["surface_name"]["enum"] == list(SURFACES)
    assert definitions["feature_name"]["enum"] == [feature for feature in FEATURES if feature != "activation"]
    assert definitions["outcome"]["enum"] == list(OUTCOMES)
    assert definitions["duration_bucket"]["enum"] == list(DURATION_BUCKETS)
    assert definitions["plugin_version"]["pattern"] == f"^{_VERSION.pattern}$"


def test_empty_and_populated_reports_match_schema(tmp_path):
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
    database = tmp_path / "collector.sqlite3"
    today = date(2026, 11, 11)
    init_database(database)

    empty = report(database, today=today, days=1)
    validator.validate(empty)
    assert not empty["features"]
    assert all(row["rate"] is None for row in empty["retention"])

    for adapter, surface, feature, outcome, bucket, version in (
        ("claude-code", "terminal", "overview", "success", "100ms_1s", "0.3.8"),
        ("claude-code", "browser", "dashboard.render", "observed", "lt_100ms", "0.3.8"),
        ("opencode", "desktop", "pane.tab", "cancelled", "lt_100ms", "0.5.4-rc.1"),
    ):
        batch = validate_payload({
            "schema_version": 1, "adapter": adapter, "installation_id": uuid4().hex, "events": [{
                "id": uuid4().hex, "day": today.isoformat(), "surface": surface, "feature": feature,
                "outcome": outcome, "duration_bucket": bucket, "version": version,
            }],
        })
        _record(database, batch, today, DEFAULT_RETENTION_DAYS)

    populated = report(database, today=today, days=14)
    validator.validate(populated)
    assert len(populated["features"]) == 3
    assert populated["features"][0]["p95_duration_bucket"] is None

    for key, invalid in (("as_of", "2026-02-30"), ("schema_version", 2), ("retention_days", 31)):
        assert not validator.is_valid({**populated, key: invalid})

    with_identifier = deepcopy(populated)
    with_identifier["features"][0]["installation_id"] = uuid4().hex
    assert not validator.is_valid(with_identifier)
