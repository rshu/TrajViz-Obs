"""Native display projections keep missing observations distinct from measured zero."""

import copy
import json
from pathlib import Path

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
    def write(messages, children=None, *, session_time=True):
        path = tmp_path / "native.json"
        path.write_text(json.dumps({"source_format": "opencode_v2", "schema_version": 1,
            "session": {"id": "root", **({"time": {"created": BASE, "updated": BASE + 2000}} if session_time else {})},
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
    assert "tokens_total: unknown" in brief and "?tok" in brief and "native_source_evidence:" in brief
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


def test_recorded_elapsed_clock_survives_missing_assistant_duration(native_run):
    _, run = native_run([message("start", completed=None),
                         {**message("later", completed=None), "time": {"created": BASE + 2100}}], session_time=False)
    assert project_analysis(run).metrics["total_duration"] is None
    assert project_analysis(run).metrics["wall_clock"] == 2


def test_missing_native_clock_is_unknown_instead_of_a_zero_duration(native_run):
    _, run = native_run([message(completed=None)], session_time=False)
    assert run.metrics["wall_clock"] == 0
    assert project_analysis(run).metrics["wall_clock"] is None
    summary = project_view(run, run.view("summary"))
    assert summary.data["wall_clock_s"] is None
    jsonschema.validate(summary.to_dict(), tv.view_schema("summary"))


@pytest.mark.parametrize("reverse", [False, True])
def test_native_comparison_exports_flag_missing_usage_without_fabricating_costs(native_run, tmp_path, reverse):
    from trajviz_obs.compare_cmd import compare_sessions

    unknown_path, unknown = native_run([message(completed=None)], session_time=False)
    # Keep both loaded run identities independent from the fixture's reused path.
    other = tmp_path / "known.json"
    payload = json.loads(unknown_path.read_text())
    payload["messages"] = [message(tokens=usage(0, 0, 0, 0, 0))]
    other.write_text(json.dumps(payload))
    canonical = copy.deepcopy(unknown.to_dict(include_raw=True))
    result = compare_sessions(unknown, str(other), anchor_patch=None, fuzzy=False, reverse=reverse,
                              out_dir=tmp_path / "compared")
    unknown_side = "reference" if reverse else "compared"
    known_side = "compared" if reverse else "reference"
    summary = result["summary"]
    assert summary["outcome"][f"{unknown_side}_tokens_observed"] is False
    assert summary["outcome"][f"{unknown_side}_duration_s_observed"] is False
    assert summary["outcome"][f"{known_side}_tokens_observed"] is True
    assert summary["metric_coverage"][f"{unknown_side}.tokens.total"]["status"] == "unknown"
    expected_tokens = "| tokens | ? | 0 |" if reverse else "| tokens | 0 | ? |"
    assert expected_tokens in result["text"]
    assert "unobserved costs are unknown, not zero" in result["text"]
    saved = json.loads(Path(result["path"]).read_text())["views"]
    for view in saved.values():
        jsonschema.validate(view, tv.view_schema(view["id"]))
    assert saved["segment_costs"]["availability"]["status"] == "partial"
    assert unknown.to_dict(include_raw=True) == canonical


def test_comparing_two_unknown_native_costs_keeps_action_alignment_available(native_run, tmp_path):
    from trajviz_obs.compare_cmd import compare_sessions

    path, run = native_run([message(completed=None)], session_time=False)
    other = tmp_path / "other.json"
    other.write_bytes(path.read_bytes())
    result = compare_sessions(run, str(other), anchor_patch=None, fuzzy=False, reverse=False,
                              out_dir=tmp_path / "compared")
    assert "| tokens | ? | ? |" in result["text"]
    assert "Alignment" in result["text"]
    saved = json.loads(Path(result["path"]).read_text())["views"]
    assert saved["comparison_summary"]["availability"]["status"] == "partial"
    assert saved["segment_costs"]["availability"]["status"] == "unavailable"
    for view in saved.values():
        jsonschema.validate(view, tv.view_schema(view["id"]))


@pytest.mark.parametrize("no_self", [False, True])
def test_native_scorecards_and_timelines_preserve_unknowns_and_measured_zero(native_run, tmp_path, no_self):
    from trajviz_obs.compare_cmd import scorecard

    unknown_path, unknown = native_run([message(completed=None)], session_time=False)
    other = tmp_path / "zero.json"
    payload = json.loads(unknown_path.read_text())
    payload["messages"] = [message(tokens=usage(0, 0, 0, 0, 0))]
    other.write_text(json.dumps(payload))
    before = unknown_path.read_bytes(), other.read_bytes(), copy.deepcopy(unknown.to_dict(include_raw=True))
    result = scorecard(None if no_self else unknown, [str(unknown_path), str(other)] if no_self else [str(other)],
                       fuzzy=False, labels=["unknown", "zero"], no_self=no_self, out_dir=tmp_path / "scorecard")
    missing, measured = result["rows"]
    assert missing["tokens"] is None and missing["tokens_observed"] is False
    assert missing["wall_clock_s"] is None and missing["wall_clock_fmt"] == "?"
    assert missing["peak_occupancy"] is None and missing["peak_pct"] is None
    assert measured["tokens"] == 0 and measured["tokens_observed"] is True
    assert "| unknown | no | 1 | ? | ? |" in result["text"]
    saved = json.loads(Path(result["path"]).read_text())["views"]
    for view in saved.values():
        jsonschema.validate(view, tv.view_schema(view["id"]))
    timelines = saved["run_timelines"]["data"]["runs"]
    assert timelines[0]["segments"][0]["tokens"] is None
    assert timelines[1]["segments"][0]["tokens"] == 0
    assert before == (unknown_path.read_bytes(), other.read_bytes(), unknown.to_dict(include_raw=True))


def test_partial_native_scorecard_keeps_observed_timeline_segments(native_run, tmp_path):
    from trajviz_obs.compare_cmd import scorecard

    path, run = native_run([message("known", usage(0, 0, 0, 0, 0)),
                            {**message("unknown"), "agent": "plan"}])
    other = tmp_path / "other.json"
    other.write_bytes(path.read_bytes())
    result = scorecard(run, [str(other)], fuzzy=False, labels=None, no_self=False, out_dir=tmp_path / "scorecard")
    row = result["rows"][0]
    assert row["tokens"] is None and row["data_quality"]["fields"]["tokens.total"]["status"] == "partial"
    saved = json.loads(Path(result["path"]).read_text())["views"]
    for run in saved["run_timelines"]["data"]["runs"]:
        assert [segment["tokens"] for segment in run["segments"]] == [0, None]


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
    assert "unknown tok" in brief and "?tok" in brief and "native_source_evidence:" in brief
    assert "- insight:" not in brief


def test_pending_native_brief_formats_generated_unknowns_without_rewriting_source(native_run):
    from agent_trajviz.insight import assistant

    phrase = "Source text keeps None failed, None errors, Nones, and (None%)."
    pending = {"type": "tool", "name": "shell", "id": "pending", "state": {
        "status": "running", "input": {"command": "echo controlled"}},
        "time": {"created": BASE + 200, "ran": BASE + 300}}
    path, run = native_run([{"type": "user", "id": "request", "text": phrase},
                           message(content=[pending], completed=None)], session_time=False)
    original = copy.deepcopy(run.to_dict(include_raw=True))
    source = path.read_bytes()
    builders = {name: getattr(assistant, name) for name in (
        "_session_header", "_metrics_lines", "_agent_lines", "_error_step_lines", "_compose_brief")}
    brief = project_brief(run)
    assert "wall_clock: unknown" in brief
    assert "tool_calls: 1 (unknown success, unknown failed)" in brief
    assert "fresh_input_tokens: unknown (unknown)" in brief
    assert "tokens_total: unknown" in brief and "avg_duration_s: unknown" in brief
    assert "unknown tok, unknown, 1 tools, unknown errors, cache unknown, gen unknown tok/s" in brief
    assert "Tool outcomes are incomplete; missing results are not evidence of success." in brief
    assert "(no tool errors)" not in brief
    assert phrase in brief  # No replacement over transcript previews or the complete brief.
    assert builders == {name: getattr(assistant, name) for name in builders}
    assert project_analysis(run).metrics["tool_fail"] is None
    assert run.to_dict(include_raw=True) == original and path.read_bytes() == source


def test_brief_preserves_a_measured_zero_elapsed_without_assistant_completion(native_run):
    path, _ = native_run([message(tokens=usage(0, 0, 0, 0, 0), completed=None)])
    document = json.loads(path.read_text())
    document["session"]["time"]["updated"] = BASE
    document["messages"][0]["time"]["created"] = BASE
    path.write_text(json.dumps(document))
    run = load_run(str(path))
    brief = project_brief(run)
    assert "wall_clock: 0s" in brief and "tokens_total: 0" in brief
    assert "avg_duration_s: unknown" in brief  # Elapsed and per-model timing have separate evidence.


def test_brief_preserves_measured_zero_failures_and_duration(native_run):
    tool = {"type": "tool", "name": "shell", "id": "zero", "state": {
        "status": "completed", "input": {"command": "true"}, "metadata": {"exit": 0}},
        "time": {"created": BASE + 100, "ran": BASE + 100, "completed": BASE + 100}}
    _, run = native_run([message(tokens=usage(0, 0, 0, 0, 0), content=[tool], completed=100)])
    brief = project_brief(run)
    assert "100.0% success, 0 failed" in brief
    assert "tokens_total: 0" in brief and "avg_duration_s: 0.0" in brief
    assert "0 tok, 0.0s, 1 tools, 0 errors" in brief


@pytest.mark.parametrize("pending", [False, True])
def test_capped_brief_keeps_native_limits_and_does_not_sum_unknowns_as_zero(native_run, pending):
    from agent_trajviz.insight import assistant

    children = [{"session": {"id": f"child-{index}"}, "messages": [message(completed=None, content=[{
        "type": "tool", "name": "shell", "id": f"pending-{index}",
        "state": {"status": "running", "input": {"command": "echo controlled"}},
        "time": {"created": BASE + 200, "ran": BASE + 300}}])], "children": []}
                for index in range(assistant._AGENT_LINE_CAP + 2)]
    if not pending:
        for child in children:
            child["messages"] = [message(tokens=usage())]
    spawns = [{"type": "tool", "name": "subagent", "id": f"spawn-{index}", "state": {"status": "completed",
              "metadata": {"sessionID": child["session"]["id"]}, "input": {}},
               "time": {"created": BASE + 200, "ran": BASE + 300, "completed": BASE + 500}}
              for index, child in enumerate(children)]
    _, run = native_run([message(tokens=usage(), content=spawns)], children)
    agents = project_brief(run).split("## AGENTS\n", 1)[1].split("\n## FILES", 1)[0]
    assert len([line for line in agents.splitlines() if "assistant steps," in line
                and "more sub-agents not listed" not in line]) == assistant._AGENT_LINE_CAP
    omitted = next(line for line in agents.splitlines() if "more sub-agents not listed" in line)
    if pending:
        assert "unknown tok" in omitted and "unknown errors" in omitted
        assert "- insight:" not in agents
    else:
        assert agents.strip() == "\n".join(assistant._agent_lines(project_analysis(run).agent_summaries))


@pytest.mark.parametrize("session_id", ["A", "B"])
def test_capture_brief_formats_unknowns_without_changing_authoritative_evidence(captured_sessions, session_id):
    run = load_run(str(captured_sessions), session_id=session_id)
    before = captured_sessions.read_bytes()
    canonical = copy.deepcopy(run.to_dict(include_raw=True))
    metrics = copy.deepcopy(run.metrics)
    brief = project_brief(run)
    assert "None" not in brief
    assert "telemetry_evidence:" in brief and "native_source_evidence:" not in brief
    assert "failure_chains: unknown" in brief
    assert "file activity was not captured" in brief
    assert any("?tok" in line for line in brief.splitlines() if line.startswith("0 user"))
    if session_id == "A":
        assert "tokens_total: 9" in brief and "tokens_cache_read: 0" in brief
        assert "tokens_reasoning: 0" in brief
    else:
        assert "tokens_total: unknown" in brief and "tokens_input: 7" in brief
        assert "fresh_input_tokens: 7 (unknown)" in brief
        assert "unknown failed" in brief and "unknown errors" in brief
        assert "Tool outcomes are incomplete; missing results are not evidence of success." in brief
        assert "(no tool errors)" not in brief
    assert run.metrics == metrics and run.to_dict(include_raw=True) == canonical
    assert captured_sessions.read_bytes() == before


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
