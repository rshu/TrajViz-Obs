"""Actual native browser callbacks distinguish observations from placeholders."""

from __future__ import annotations

import copy
import html
import json
import re
import runpy
from pathlib import Path

import pytest
from agent_trajviz import dashboard

from trajviz_obs.compat import load_run
from trajviz_obs.dashboard_ui import decorate_dashboard


def rows(markup):
    return [[html.unescape(re.sub(r"<[^>]+>", "", cell)).strip()
             for cell in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row, re.DOTALL)]
            for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", markup, re.DOTALL)]


@pytest.fixture
def controls(tmp_path):
    fixtures = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/e2e_fixtures.py"))
    paths = fixtures["prepare_cost_inputs"](tmp_path)
    runs = {key: load_run(str(paths[key])) for key in ("missing", "zero", "positive", "partial")}
    return paths, runs


def callback(app, name):
    return next(function.fn for function in app.fns.values() if function.api_name == name)


def test_insight_pairwise_missing_partial_zero_and_positive_costs(controls):
    paths, runs = controls
    app = decorate_dashboard(dashboard.insight_app(), "insight")
    compare = callback(app, "on_run_comparison")
    before = {key: copy.deepcopy(run.to_dict(include_raw=True)) for key, run in runs.items()}
    for kind in ("missing", "partial", "positive"):
        report, _, _, status = compare(str(paths["zero"]), None, "", None, None, runs[kind], False)
        assert "Comparison complete" in status
        expected = "20" if kind == "positive" else "unknown"
        assert ["Total Tokens", "0", expected] in rows(report)
        assert ["Duration", "0.0s", "unknown" if kind == "missing" else "1.1s"] in rows(report)
    assert before == {key: run.to_dict(include_raw=True) for key, run in runs.items()}


def test_scorecard_keeps_unknown_cells_out_of_rankings_and_timeline_costs(controls):
    paths, runs = controls
    app = decorate_dashboard(dashboard.insight_app(), "insight")
    build = callback(app, "on_run_group_scorecard")
    report, timeline, behavior = build([str(paths[key]) for key in ("zero", "positive", "partial")], "", False,
                                       runs["missing"])
    data = rows(report)[1:]
    assert data[0][4:6] == ["unknown", "unknown"]
    assert data[1][4:6] == ["0s", "0"]
    assert data[1][7] == "0%"  # Two observed failed tools, not an undefined rate.
    assert data[2][5] == "20" and data[3][5] == "unknown"
    assert "unknown means unrecorded" in report and "Tool coverage" in behavior
    similarity = rows(behavior)
    assert any(row[0] == data[0][0] and row[1] == "1.00" and "unknown" in row for row in similarity if row)
    assert "cost-weighted alignment F1" in behavior
    assert "Tokens unavailable" in str(timeline["value"].to_plotly_json())
    assert "0 tokens" in str(timeline["value"].to_plotly_json())
    # No Overview baseline is also supported; unreadable uploads remain error rows.
    other, _, _ = build([str(paths["zero"]), str(paths["missing"]), str(paths["missing"].parent / "absent.json")],
                         "", False, None)
    assert "2 of 3 run(s) loaded" in other and "absent.json" in rows(other)[-1][1]


def test_absent_tool_rate_is_unknown_while_a_measured_failure_rate_is_zero(controls):
    paths, runs = controls
    path = paths["zero"].parent / "no-tools.json"
    payload = json.loads(paths["zero"].read_text())
    for message in payload["messages"]:
        message["content"] = [{"type": "text", "text": "Controlled completion without tools"}]
    path.write_text(json.dumps(payload))
    app = decorate_dashboard(dashboard.insight_app(), "insight")
    report, _, _ = callback(app, "on_run_group_scorecard")([str(path)], "", False, runs["zero"])
    data = rows(report)[1:]
    assert data[0][5:8] == ["0", "2", "0%"]
    assert data[1][5:8] == ["0", "0", "unknown"]


def test_converge_pairwise_unknown_charts_and_measured_zero(controls):
    paths, _ = controls
    app = decorate_dashboard(dashboard.converge_app(data_root=paths["missing"].parent), "converge")
    compare = callback(app, "compare_uploads")
    report, milestones, costs, waterfall, _ = compare(str(paths["zero"]), str(paths["missing"]), None, 50, False)
    assert ["Total Tokens", "0", "unknown"] in rows(report)
    assert ["Duration", "0.0s", "unknown"] in rows(report)
    assert not costs.data and "Cost unknown" in costs.layout.annotations[0].text
    assert not waterfall.data and "Cost unknown" in waterfall.layout.annotations[0].text
    assert milestones is not None
    complete = compare(str(paths["positive"]), str(paths["zero"]), None, 50, False)
    assert ["Total Tokens", "20", "0"] in rows(complete[0])
    assert ["Duration", "1.1s", "0.0s"] in rows(complete[0])
    assert "Cost evidence is incomplete" not in complete[0]


def test_converge_uses_public_loaders_for_canonical_session_exports(controls):
    paths, runs = controls
    canonical = {}
    for kind in ("missing", "zero"):
        path = paths[kind].with_name(f"{kind}-canonical.json")
        path.write_text(json.dumps(runs[kind].to_dict(include_raw=True)))
        canonical[kind] = str(path)
    app = decorate_dashboard(dashboard.converge_app(data_root=paths["missing"].parent), "converge")
    report = callback(app, "compare_uploads")(canonical["zero"], canonical["missing"], None, 50, False)[0]
    assert ["Total Tokens", "0", "unknown"] in rows(report)
    assert ["Duration", "0.0s", "unknown"] in rows(report)


def test_manifest_aggregates_exclude_unknown_costs_and_show_coverage(controls):
    paths, _ = controls
    root = paths["missing"].parent
    app = decorate_dashboard(dashboard.converge_app(data_root=root), "converge")
    before = {path: path.read_bytes() for path in paths.values()}
    report = callback(app, "do_batch")(str(paths["mixed_manifest"]), 50, False)
    assert "2 of 2 tasks compared" in report
    assert "Cost evidence: 1 of 2 comparisons complete" in report
    assert ["unknown", "20", "unknown", "1.1s", "unknown"] in rows(report)
    assert ["known", "20", "0", "1.1s", "0.0s"] in rows(report)
    intervention = callback(app, "do_before_after")(str(paths["before_manifest"]), str(paths["after_manifest"]), 50, False)
    assert "No comparable metric observations are available" in intervention
    assert "Cost evidence: 1 of 2 comparisons complete" in intervention
    assert "Metric Deltas" not in intervention
    assert "Before: task" in intervention and "After: task" in intervention
    assert before == {path: path.read_bytes() for path in paths.values()}


def test_failed_after_manifest_clears_partial_results_and_recovers(controls):
    paths, _ = controls
    app = decorate_dashboard(dashboard.converge_app(data_root=paths["missing"].parent), "converge")
    run = callback(app, "do_before_after")
    invalid = paths["missing"].with_name("invalid-manifest.json")
    invalid.write_text("null")
    result = run(str(paths["before_manifest"]), str(invalid), 50, False)
    assert "failed" in result and "Cost evidence:" not in result
    assert not rows(result)
    recovered = run(str(paths["before_manifest"]), str(paths["after_manifest"]), 50, False)
    assert "Cost evidence: 1 of 2" in recovered and "Before: task" in recovered and "After: task" in recovered


def test_changed_callback_contract_fails_instead_of_showing_native_zeros():
    from trajviz_obs.errors import BridgeError

    native = dashboard.insight_app()
    next(block for block in native.fns.values() if block.api_name == "on_run_comparison").api_name = "changed"
    with pytest.raises(BridgeError, match="comparison callbacks changed"):
        decorate_dashboard(native, "insight")


def test_adapters_are_app_scoped_idempotent_and_do_not_patch_upstream(controls):
    from agent_trajviz.converge import alignment
    from agent_trajviz.insight import comparison, run_group

    original = comparison.run_comparison, run_group.build_run_group_scorecard, alignment.build_comparison_report
    app = decorate_dashboard(dashboard.insight_app(), "insight")
    functions = {key: block.fn for key, block in app.fns.items()}
    from trajviz_obs.comparison_ui import adapt_comparisons

    adapt_comparisons(app, "insight")
    assert functions == {key: block.fn for key, block in app.fns.items()}
    assert original == (comparison.run_comparison, run_group.build_run_group_scorecard, alignment.build_comparison_report)
    assert getattr(callback(app, "on_run_group_scorecard"), "__tv_safe__", False)
