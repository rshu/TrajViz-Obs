"""Native comparison costs remain unknown until their source evidence exists."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import agent_trajviz as tv
import jsonschema
import pytest

from trajviz_obs.comparison_quality import project_report, project_scorecard, project_timeline
from trajviz_obs.compat import load_run

BASE = 1790000000000
USAGE = {"input": 7, "output": 2, "reasoning": 1, "cache": {"read": 3, "write": 0}}


@pytest.fixture
def native_run(tmp_path):
    sequence = 0

    def make(*, tokens=USAGE, elapsed=1000, assistant_duration=1000, tool_duration=200, tools=True,
             failed=False, partial=False, planning=False):
        nonlocal sequence
        sequence += 1
        clock = {"created": BASE}
        if assistant_duration is not None:
            clock["completed"] = BASE + assistant_duration
        tool_clock = {"created": BASE, "ran": BASE}
        if tool_duration is not None:
            tool_clock["completed"] = BASE + tool_duration
        message = {"type": "assistant", "id": "message", "agent": "build", "model": {"id": "controlled"},
                   "time": clock, "content": [{"type": "text", "text": "Controlled work"}]}
        if tokens is not None:
            message["tokens"] = tokens
        if tools:
            message["content"].append({"type": "tool", "name": "TodoWrite" if planning else "shell", "id": "tool",
                "time": tool_clock, "state": {"status": "error" if failed else "completed",
                "input": {"command": "pytest -q"}, "content": [{"type": "text", "text": "Controlled result"}]}})
        messages = [message]
        if partial:
            messages.append({**copy.deepcopy(message), "id": "missing-usage", "agent": "plan"})
            messages[-1].pop("tokens", None)
        session = {"id": f"run-{sequence}"}
        if elapsed is not None:
            session["time"] = {"created": BASE, "updated": BASE + elapsed}
        path = tmp_path / f"native-{sequence}.json"
        path.write_text(json.dumps({"source_format": "opencode_v2", "schema_version": 1,
                                   "session": session, "messages": messages, "children": []}))
        return path, load_run(str(path))

    return make


def report(reference, compared):
    return tv.compare(reference=reference, compared=compared)


def test_complete_native_report_is_unchanged_apart_from_coverage(native_run):
    paths_runs = [native_run(), native_run()]
    reference, compared = [run for _, run in paths_runs]
    original = report(reference, compared)
    before = [path.read_bytes() for path, _ in paths_runs]
    canonical = [copy.deepcopy(run.to_dict(include_raw=True)) for _, run in paths_runs]
    projected = project_report(original, reference, compared)
    assert projected["_obs_costs_complete"] is True
    assert all(all(side.values()) for side in projected["_obs_cost_coverage"].values())
    assert {key: value for key, value in projected.items() if not key.startswith("_obs_")} == original
    assert [path.read_bytes() for path, _ in paths_runs] == before
    assert [run.to_dict(include_raw=True) for _, run in paths_runs] == canonical


@pytest.mark.parametrize("partial", [False, True])
def test_unknown_or_partial_tokens_do_not_become_zero_costs(native_run, partial):
    _, reference = native_run()
    _, compared = native_run(tokens=USAGE if partial else None, partial=partial)
    original = report(reference, compared)
    original["patterns"] = [{"type": "redundant_search", "steps": [0], "estimated_extra_cost": {"steps": 1, "tokens": 0}}]
    before = copy.deepcopy(original)
    projected = project_report(original, reference, compared)
    assert projected["outcome"]["compared_tokens"] is None
    assert projected["outcome"]["reference_tokens"] == 13
    assert projected["_obs_cost_coverage"]["compared"] == {
        "tokens": False, "elapsed": True, "tool_timing": True, "costs": False}
    assert all(projected["alignment"][key] is None for key in (
        "reference_recall", "behavioral_precision", "alignment_f1", "overhead_ratio", "harmful_ratio"))
    assert projected["alignment"]["harmful_cost"]["tokens"] is None
    assert projected["alignment"]["harmful_cost"]["latency_ms"] == before["alignment"]["harmful_cost"]["latency_ms"]
    assert projected["patterns"][0]["estimated_extra_cost"] == {"steps": 1, "tokens": None}
    assert projected["ref_milestones"] == original["ref_milestones"]
    assert all(segment["overhead"] is None for segment in projected["segments"]["segment_comparison"])
    assert original == before


def test_measured_zero_tokens_and_clocks_are_not_unknown(native_run):
    zero = {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}}
    _, run = native_run(tokens=zero, elapsed=0, assistant_duration=0, tool_duration=0)
    original = report(run, run)
    projected = project_report(original, run, run)
    assert projected["_obs_costs_complete"] is True
    assert projected["outcome"]["reference_tokens"] == 0
    assert projected["outcome"]["reference_duration_s"] == 0
    assert projected["alignment"] == original["alignment"]


def test_missing_tool_latency_masks_weighted_metrics_independently_of_elapsed(native_run):
    _, reference = native_run()
    _, compared = native_run(tool_duration=None)
    projected = project_report(report(reference, compared), reference, compared)
    assert projected["outcome"]["compared_tokens"] == 13
    assert projected["outcome"]["compared_duration_s"] == 1
    assert projected["_obs_cost_coverage"]["compared"] == {
        "tokens": True, "elapsed": True, "tool_timing": False, "costs": False}
    assert projected["alignment"]["alignment_f1"] is None
    assert projected["alignment"]["harmful_ratio"] is None
    assert projected["alignment"]["harmful_cost"]["tokens"] == 0
    assert projected["alignment"]["harmful_cost"]["latency_ms"] is None


def test_compared_only_harmful_metrics_remain_observed_when_reference_cost_is_unknown(native_run):
    _, reference = native_run(tokens=None)
    _, compared = native_run()
    original = report(reference, compared)
    projected = project_report(original, reference, compared)
    assert projected["alignment"]["alignment_f1"] is None
    assert projected["alignment"]["harmful_ratio"] == original["alignment"]["harmful_ratio"]
    assert projected["alignment"]["harmful_cost"] == original["alignment"]["harmful_cost"]


@pytest.mark.parametrize("elapsed", [0, 1000])
def test_elapsed_clock_does_not_require_an_assistant_completion(native_run, elapsed):
    _, run = native_run(elapsed=elapsed, assistant_duration=None, tools=False)
    projected = project_report(report(run, run), run, run)
    assert projected["outcome"]["compared_duration_s"] == elapsed / 1000
    assert projected["_obs_cost_coverage"]["compared"]["elapsed"] is True
    assert projected["_obs_costs_complete"] is True


def test_missing_elapsed_clock_is_not_a_zero_measurement(native_run):
    _, run = native_run(elapsed=None, assistant_duration=None, tools=False)
    projected = project_report(report(run, run), run, run)
    assert projected["outcome"]["compared_duration_s"] is None
    assert projected["_obs_cost_coverage"]["compared"]["elapsed"] is False
    assert projected["_obs_costs_complete"] is True


def test_planning_tools_do_not_require_latency_for_nonreason_action_costs(native_run):
    _, run = native_run(planning=True, tool_duration=None)
    assert project_report(report(run, run), run, run)["_obs_costs_complete"] is True


def test_unknown_costs_are_excluded_before_batch_aggregation_and_intervention(native_run):
    from agent_trajviz.converge.batch import BatchResult, aggregate_reports
    from agent_trajviz.converge.intervention import (
        build_intervention_report,
        compute_metric_deltas,
        detect_guardrail_regressions,
    )

    _, known = native_run()
    _, unknown = native_run(tokens=None)
    measured = project_report(report(known, known), known, known)
    missing = project_report(report(unknown, unknown), unknown, unknown)
    aggregate = aggregate_reports([BatchResult("known", measured), BatchResult("missing", missing)])
    assert aggregate["compared_count"] == 2
    assert aggregate["metrics"]["alignment_f1"]["count"] == 1
    assert aggregate["metrics"]["harmful_ratio"]["mean"] == 0
    paired = [(BatchResult("task", measured), BatchResult("task", missing))]
    deltas = compute_metric_deltas(paired)
    assert deltas == {}
    intervention = build_intervention_report("before", "after", paired, [], [], deltas, {},
                                             detect_guardrail_regressions(deltas))
    assert intervention["paired_tasks"] == 1
    assert intervention["intervention_effect"] == {}
    assert intervention["recommendation"] == "No comparable metric observations are available."


def test_scorecard_preserves_observed_zero_rate_and_undefined_no_tool_rate(native_run):
    _, no_tools = native_run(tools=False)
    _, failed = native_run(failed=True, tool_duration=0)
    original = tv.compare_runs([no_tools, failed])
    before = copy.deepcopy(original)
    projected = project_scorecard(original, [no_tools, failed])
    assert projected["rows"][0]["tool_success_pct"] is None
    assert projected["rows"][1]["tool_success_pct"] == 0
    assert projected["rows"][1]["tokens"] == 13
    assert original == before


def test_scorecard_and_timeline_keep_per_segment_unknowns_without_mutation(native_run):
    _, run = native_run(partial=True, tools=False)
    original = tv.compare_runs([run, run])
    before = copy.deepcopy(original)
    projected = project_scorecard(original, [run, run])
    assert all(row["tokens"] is None for row in projected["rows"])
    for lane in projected["timelines"]["runs"]:
        assert [segment["tokens"] for segment in lane["segments"]] == [13, None]
    assert original == before


def test_cli_integer_placeholders_have_cost_observation_flags_and_schema_valid_views(native_run, tmp_path):
    from trajviz_obs.compare_cmd import compare_sessions

    reference_path, _ = native_run()
    _, compared = native_run(tokens=None)
    result = compare_sessions(compared, str(reference_path), anchor_patch=None, fuzzy=False, reverse=False,
                              out_dir=tmp_path / "artifacts")
    summary = result["summary"]
    assert isinstance(summary["outcome"]["compared_tokens"], int)
    assert summary["outcome"]["compared_tokens_observed"] is False
    assert summary["alignment"]["alignment_f1_observed"] is False
    assert "F1 ?" in result["text"]
    saved = json.loads(Path(result["path"]).read_text())["views"]
    assert all(segment["overhead_observed"] is False
               for segment in saved["segment_costs"]["data"]["segment_comparison"])
    for view in saved.values():
        jsonschema.validate(view, tv.view_schema(view["id"]))


def test_cli_and_scorecard_preserve_zero_elapsed_without_assistant_duration(native_run, tmp_path):
    from trajviz_obs.compare_cmd import compare_sessions

    path, run = native_run(elapsed=0, assistant_duration=None, tools=False)
    result = compare_sessions(run, str(path), anchor_patch=None, fuzzy=False, reverse=False,
                              out_dir=tmp_path / "artifacts")
    assert result["summary"]["outcome"]["reference_duration_s"] == 0
    assert result["summary"]["outcome"]["reference_duration_s_observed"] is True
    assert "| wall clock | 0.0s | 0.0s |" in result["text"]
    rows = project_scorecard(tv.compare_runs([run, run]), [run, run])["rows"]
    assert all(row["wall_clock_s"] == 0 and row["wall_clock_fmt"] != "?" for row in rows)


@pytest.mark.parametrize("missing", [False, True])
def test_similarity_keeps_canonical_numbers_with_cost_observation_flags(native_run, missing):
    _, reference = native_run()
    _, compared = native_run(tokens=None if missing else USAGE)
    original = tv.compare_runs([reference, compared])
    projected = project_scorecard(original, [reference, compared])
    behavior = projected["behavior"]
    first, second = behavior["run_ids"]
    assert behavior["similarity"] == original["behavior"]["similarity"]
    assert behavior["similarity_observed"] == {
        first: {first: True, second: not missing}, second: {first: not missing, second: True}}
    assert "similarity_observed" not in original["behavior"]
    jsonschema.validate(tv.run_group_views(projected)["run_behavior"].to_dict(), tv.view_schema("run_behavior"))


def test_cli_similarity_text_hides_an_unknown_weighted_score(native_run, tmp_path):
    from trajviz_obs.compare_cmd import scorecard

    _, known = native_run()
    unknown_path, _ = native_run(partial=True)
    result = scorecard(known, [str(unknown_path)], fuzzy=False, labels=["known", "partial"], no_self=False,
                       out_dir=tmp_path / "artifacts")
    assert "Similarity to baseline known: partial ?" in result["text"]
    behavior = json.loads(Path(result["path"]).read_text())["views"]["run_behavior"]
    assert all(isinstance(score, (int, float)) for values in behavior["data"]["similarity"].values()
               for score in values.values())
    jsonschema.validate(behavior, tv.view_schema("run_behavior"))


def test_otlp_projection_is_left_to_the_upstream_contract(captured_sessions):
    run = load_run(str(captured_sessions), session_id="B")
    original = {"outcome": {"reference_tokens": 0}, "alignment": {"alignment_f1": 0}}
    assert project_report(original, run, run) == original
    data = {"runs": [{"run_id": "B", "segments": [{"start": 0, "end": 100, "tokens": None}]}]}
    assert project_timeline(data, [{"run_id": "B", "steps": run.steps, "format": "otlp"}]) == data
