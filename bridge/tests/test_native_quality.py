"""Native display projections keep missing observations distinct from measured zero."""

import copy
import json

import agent_trajviz as tv
import jsonschema
import pytest

from trajviz_obs.compat import load_run, project_analysis, project_brief, project_view

BASE = 1790000000000


def usage(input=7, output=2, reasoning=1, read=3, write=0):
    return {"input": input, "output": output, "reasoning": reasoning, "cache": {"read": read, "write": write}}


def message(mid="a", tokens=None, *, content=None, completed=1000):
    result = {"type": "assistant", "id": mid, "agent": "build", "model": {"id": "controlled"},
              "time": {"created": BASE + 100}, "content": content or [{"type": "text", "text": "Controlled reply"}]}
    if completed is not None:
        result["time"]["completed"] = BASE + completed
    if tokens is not None:
        result["tokens"] = tokens
    return result


@pytest.fixture
def native_run(tmp_path):
    def write(messages, children=None):
        path = tmp_path / "native.json"
        path.write_text(json.dumps({"source_format": "opencode_v2", "schema_version": 1,
            "session": {"id": "root", "time": {"created": BASE, "updated": BASE + 2000}},
            "messages": messages, "children": children or []}))
        return path, load_run(str(path))
    return write


def test_all_missing_native_usage_and_outcomes_are_unknown_without_changing_canonical_data(native_run):
    tool = {"type": "tool", "name": "shell", "id": "active", "state": {"status": "running",
            "input": {"command": "echo controlled"}}, "time": {"created": BASE + 200, "ran": BASE + 300}}
    path, run = native_run([message(content=[tool], completed=None)])
    original = copy.deepcopy(run.to_dict(include_raw=True))
    before = path.read_bytes()
    analysis = project_analysis(run)
    assert set(analysis.metrics["tokens"].values()) == {None}
    assert analysis.metrics["total_duration"] is None and analysis.metrics["tool_fail"] is None
    assert analysis.metrics["tool_observed_failures"] == 0
    assert all(row["status"] == "unavailable" for row in analysis.verdicts)
    assert analysis.data_quality["source"] == "native"
    assert analysis.data_quality["fields"]["tokens.input"]["observed"] == 0
    assert project_view(run, run.view("token_usage")).data["steps"][0]["total"] is None
    assert project_view(run, run.view("workflow")).data["steps"][0]["tokens_observed"] is False
    assert project_view(run, run.view("agents")).data[0]["total_tokens"] is None
    assert project_view(run, run.view("steps")).data[0]["tool_time_sum"] is None
    assert project_view(run, run.view("tool_outcomes")).data["calls"][0]["ok"] is None
    brief = project_brief(run)
    assert "tokens_total: None" in brief and "?tok" in brief and "native_source_evidence:" in brief
    assert "step_tokens_min/max/avg:" not in brief
    assert run.to_dict(include_raw=True) == original and path.read_bytes() == before


def test_partial_native_totals_sum_only_observed_records_and_keep_per_step_gaps(native_run):
    _, run = native_run([message("known", usage(2, 0, 0, 0, 0)), message("partial", {"input": 7})])
    analysis = project_analysis(run)
    assert analysis.metrics["tokens"] == {"total": 2, "input": 9, "output": 0, "reasoning": 0,
                                          "cache_read": 0, "cache_write": 0}
    assert analysis.data_quality["fields"]["tokens.total"]["status"] == "partial"
    assert analysis.data_quality["fields"]["tokens.input"]["status"] == "observed"
    assert analysis.metrics["avg_cache_ratio"] is None and analysis.metrics["output_tokens_per_sec"] is None
    assert next(row for row in analysis.verdicts if row["metric"] == "Cache Efficiency")["status"] == "unavailable"
    view = project_view(run, run.view("token_usage"))
    assert view.data["steps"][0]["total"] == 2 and view.data["steps"][1]["total"] is None
    assert view.data["steps"][1]["fresh_input"] == 7
    assert view.availability["status"] == "partial"
    assert "Partial totals sum observed source records" in project_brief(run)


def test_measured_native_zero_remains_zero(native_run):
    _, run = native_run([message(tokens=usage(0, 0, 0, 0, 0))])
    analysis = project_analysis(run)
    assert set(analysis.metrics["tokens"].values()) == {0}
    assert analysis.metrics["tool_fail"] == 0
    assert analysis.data_quality["fields"]["tokens.total"]["status"] == "observed"
    assert project_view(run, run.view("token_usage")).data["steps"][0]["total"] == 0
    assert "tokens_total: 0" in project_brief(run)


def test_compaction_usage_keeps_real_totals_without_changing_assistant_throughput_population(native_run):
    compact = {"id": "compact", "type": "compaction", "status": "completed", "reason": "manual",
               "summary": "Controlled summary", "tokens": usage(1, 1, 0, 0, 0), "time": {"created": BASE + 1200}}
    _, run = native_run([message(tokens=usage()), compact])
    analysis = project_analysis(run)
    assert analysis.metrics["tokens"]["total"] == 15
    assert analysis.data_quality["fields"]["tokens.total"]["total"] == 2
    assert analysis.data_quality["fields"]["duration"] == {
        "status": "observed", "observed": 1, "partial": 0, "total": 1, "reasons": {}}
    assert analysis.metrics["output_tokens_per_sec"] == run.metrics["output_tokens_per_sec"]


@pytest.mark.parametrize("invalid", [True, "7", float("nan"), -1, 1.5])
def test_malformed_native_counters_have_truthful_annotations_and_are_never_inferred_totals(native_run, invalid):
    path, run = native_run([message(tokens=usage(input=invalid))])
    before = path.read_bytes()
    missing = run.steps[0]["_metrics_unavailable_fields"]
    assert "Input Tokens" in missing and "Total Tokens" in missing
    analysis = project_analysis(run)
    assert analysis.metrics["tokens"]["input"] is None and analysis.metrics["tokens"]["total"] is None
    assert analysis.metrics["tokens"]["cache_write"] == 0
    if invalid == -1:
        assert run.steps[0]["tokens"]["input"] == -1
    assert path.read_bytes() == before


def test_native_missing_annotations_survive_normalized_export_reload(native_run, tmp_path):
    _, run = native_run([message(tokens={"input": 7})])
    export = tmp_path / "trajectory.json"
    export.write_text(json.dumps(run.to_dict(include_raw=True)))
    restored = load_run(str(export))
    assert restored.steps[0]["_metrics_unavailable_fields"] == run.steps[0]["_metrics_unavailable_fields"]
    assert project_analysis(restored).metrics["tokens"]["input"] == 7
    assert project_analysis(restored).metrics["tokens"]["total"] is None


def test_multiagent_native_brief_handles_an_unknown_child_without_fabricated_insights(native_run):
    child = {"session": {"id": "child"}, "messages": [message(completed=None)], "children": []}
    spawn = {"type": "tool", "name": "subagent", "id": "spawn", "state": {"status": "completed",
             "metadata": {"sessionID": "child"}, "input": {}},
             "time": {"created": BASE + 200, "ran": BASE + 300, "completed": BASE + 500}}
    _, run = native_run([message(tokens=usage(), content=[spawn])], [child])
    summaries = {row["agent_id"]: row for row in project_analysis(run).agent_summaries}
    assert summaries[""]["total_tokens"] == 13
    assert summaries[""]["total_duration_s"] == run._analysis.agent_summaries[0]["total_duration_s"]
    assert summaries["child"]["total_tokens"] is None
    assert summaries["child"]["total_duration_s"] is None
    brief = project_brief(run)
    assert "None tok" in brief and "?tok" in brief and "native_source_evidence:" in brief
    assert "- insight:" not in brief


def test_native_family_agent_coverage_uses_summary_identity_across_sessions_and_modes(native_run):
    from agent_trajviz.insight.timeline_agents import bind_timeline_agents

    child = {"session": {"id": "child"}, "messages": [
        {**message(tokens=usage(2, 0, 0, 0, 0)), "agent": "general"}], "children": []}
    spawn = {"type": "tool", "name": "subagent", "id": "spawn", "state": {"status": "completed",
             "metadata": {"sessionID": "child"}, "input": {}},
             "time": {"created": BASE + 200, "ran": BASE + 300, "completed": BASE + 500}}
    compact = {"id": "compact", "type": "compaction", "status": "completed", "reason": "manual",
               "summary": "Controlled summary", "tokens": usage(1, 1, 0, 0, 0), "time": {"created": BASE + 1200}}
    path, run = native_run([message(tokens=usage(), content=[spawn]),
        {**message("plan", usage(0, 0, 0, 0, 0)), "agent": "plan"}, compact], [child])
    before, canonical = path.read_bytes(), copy.deepcopy(run.to_dict(include_raw=True))
    _, _, timeline_agent = bind_timeline_agents(run.steps)
    assert {timeline_agent(step) for step in run.steps if step["role"] == "assistant"} == {
        "root::build", "root::plan", "child::general"}
    original = {row["agent_id"]: row for row in run._analysis.agent_summaries}
    projected = project_view(run, run.view("agents"))
    for row in projected.data:
        for field in ("total_tokens", "input_tokens", "output_tokens", "reasoning_tokens", "cache_read_tokens",
                      "cache_write_tokens", "total_duration_s", "cache_efficiency_pct", "output_tokens_per_sec",
                      "error_count"):
            assert row[field] == original[row["agent_id"]][field]
        expected = 2 if row["agent_id"] == "" else 1
        for field in ("tokens.total", "duration"):
            assert row["data_quality"]["fields"][field] == {
                "status": "observed", "observed": expected, "partial": 0, "total": expected, "reasons": {}}
    child_row = next(row for row in projected.data if row["agent_id"] == "child")
    assert child_row["total_tokens"] == 2 and child_row["output_tokens"] == 0
    jsonschema.validate(projected.to_dict(), tv.view_schema("agents"))
    assert path.read_bytes() == before and run.to_dict(include_raw=True) == canonical


@pytest.mark.parametrize("scenario", ["missing", "running", "partial"])
def test_projected_native_views_match_published_schema_two(native_run, scenario):
    rows = [message(completed=None)]
    if scenario == "running":
        rows = [message(completed=None, content=[{"type": "tool", "id": "t", "name": "shell",
                "state": {"status": "running", "input": {}}, "time": {"created": BASE + 200}}])]
    elif scenario == "partial":
        rows = [message("known", usage()), message("unknown", {"input": 7})]
    _, run = native_run(rows)
    for view in run.views().values():
        projected = project_view(run, view)
        assert projected.to_dict()["schema_version"] == 2
        jsonschema.validate(projected.to_dict(), tv.view_schema(view.id))
