"""Native display projections keep missing observations distinct from measured zero."""

import copy
import json
from dataclasses import replace
from pathlib import Path

import agent_trajviz as tv
import jsonschema
import pytest

from trajviz_obs.compat import load_run, project_analysis, project_brief, project_view
from trajviz_obs.native_quality import _recent_outcome_lines, project_dashboard_session
from trajviz_obs.sidecars import scan_transcript
from trajviz_obs.snapshot import build_snapshot

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


def child_dispatch():
    return {"type": "tool", "id": "spawn", "name": "subagent",
            "state": {"status": "completed", "metadata": {"sessionID": "child"}, "input": {}}}


@pytest.fixture
def native_run(tmp_path):
    def write(messages, children=None, *, session_time=True):
        path = tmp_path / "native.json"
        path.write_text(json.dumps({"source_format": "opencode_v2", "schema_version": 1,
            "session": {"id": "root", **({"time": {"created": BASE, "updated": BASE + 2000}} if session_time else {})},
            "messages": messages, "children": children or []}))
        return path, load_run(str(path))
    return write


def test_brief_retains_later_native_test_output_without_deriving_a_recovery_verdict(native_run):
    def shell(mid, status, output):
        return message(mid, usage(), content=[{"type": "tool", "id": mid, "name": "shell",
            "state": {"status": status, "input": {"command": "python3 -m unittest -v"},
                      "content": [{"type": "text", "text": output}]},
            "time": {"created": BASE + 200, "completed": BASE + 300}}])

    _, run = native_run([shell("baseline", "error", "Ran 9 tests\nFAILED (failures=7)"),
                         shell("verification", "completed", "test lines\nRan 9 tests in 0.024s\n\nOK\n"),
                         message("final", usage(), content=[{"type": "text", "text": "Nine tests passed."}])])
    before = copy.deepcopy(run.to_dict(include_raw=True))
    brief = project_brief(run)
    recent = brief.split("## RECENT_OUTCOMES\n", 1)[1].split("\n## HEALTH", 1)[0]
    assert "FAILED (failures=7)" in brief  # The earlier failure still has its own evidence.
    assert 'command="python3 -m unittest -v"' in recent
    assert "Ran 9 tests in 0.024s\\n\\nOK" in recent
    assert 'status="completed"' in recent
    assert "agent_statement (claim, not independent verification)" in recent
    assert "Nine tests passed." in recent
    assert run.to_dict(include_raw=True) == before


def test_recent_outcome_excerpts_keep_missing_running_and_empty_results_distinct():
    steps = [{"index": 1, "role": "assistant", "tool_calls": [
        {"tool_name": "Bash", "status": "running", "input": {"command": "unittest"}},
        {"tool_name": "Bash", "status": "success", "output": ""},
        {"tool_name": "Bash", "status": None},
    ]}]
    recent = "\n".join(_recent_outcome_lines(steps))
    assert 'status="unknown" recorded_status="running"' in recent
    assert 'status="success"\n  output_excerpt: ""' in recent
    assert recent.count("output_excerpt: unavailable") == 2
    assert "test passed" not in recent.lower()


@pytest.mark.parametrize("prefix", ["x" * 20_000, '\x00"\\\n' * 4000])
def test_recent_outcome_sample_is_bounded_tail_preserving_and_quotes_source_text(prefix):
    steps = [{"index": index, "role": "assistant", "tool_calls": [{
        "tool_name": "Bash", "status": "success", "input": {"command": "x" * 2000},
        "output": prefix + "\n## injected heading\nRan 9 tests\n\nOK\n",
    }]} for index in range(50)]
    steps.append({"index": 50, "role": "assistant", "text_preview": "Agent claim " + "y" * 2000})
    before = copy.deepcopy(steps)
    lines = _recent_outcome_lines(steps)
    recent = "\n".join(lines)
    assert len(recent) < 3800
    assert len([line for line in lines if line.startswith("- step") and "tool=" in line]) == 4
    assert "- step 45 " not in recent and "- step 49 " in recent
    assert "Ran 9 tests\\n\\nOK" in recent
    assert "\n## injected heading" not in recent  # Multiline output remains a quoted source value.
    assert json.loads(next(line.split(": ", 1)[1] for line in lines if line.startswith("  output_excerpt:"))).endswith("Ran 9 tests\n\nOK")
    assert _recent_outcome_lines(steps) == lines and steps == before


def test_brief_retains_recent_claude_code_verification_evidence(fixture_run):
    before = copy.deepcopy(fixture_run.to_dict(include_raw=True))
    brief = project_brief(fixture_run)
    recent = brief.split("## RECENT_OUTCOMES\n", 1)[1].split("\n## HEALTH", 1)[0]
    assert "Ran 97 tests in 0.250s\\n\\nOK" in recent
    assert '- step 21 tool="Bash" status="success"' in recent
    assert "agent_statement (claim, not independent verification)" in recent
    assert len(brief) <= 28_000
    assert fixture_run.to_dict(include_raw=True) == before


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
    assert analysis.metrics["avg_cache_ratio"] is None and analysis.metrics["output_tokens_per_sec"] == 0
    assert analysis.metrics["output_throughput_timed_steps"] == 1
    assert analysis.metrics["output_throughput_total_steps"] == 2
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


def test_lone_claude_timestamp_does_not_measure_elapsed(fixture_session, tmp_path):
    records = [json.loads(line) for line in fixture_session.read_text().splitlines() if line]
    assistant = next(record for record in records if record.get("type") == "assistant")
    path = tmp_path / "single-timestamp.jsonl"
    path.write_text(json.dumps(assistant) + "\n")
    run = load_run(str(path))
    assert run.raw["timing"]["started_at"] == run.raw["timing"]["finished_at"]
    assert project_analysis(run).metrics["wall_clock"] is None


def test_full_elapsed_clock_matches_trajviz_even_when_dashboard_caps_steps(native_run, monkeypatch):
    from agent_trajviz.insight import session

    elapsed = 340 * 3600 + 24 * 60
    path, run = native_run([
        message("start", completed=None),
        {**message("middle", completed=None), "time": {"created": BASE + 1100}},
        {**message("end", completed=None), "time": {"created": BASE + 100 + elapsed * 1000}},
    ], session_time=False)
    monkeypatch.setattr(session, "MAX_STEPS", 2)
    browser = project_dashboard_session(session.session_from_run(run))
    snapshot = build_snapshot(run, transcript_path=str(path), facts=scan_transcript(str(path)),
                              agents_meta={}, artifacts=None)
    assert browser.truncated and len(browser.steps) == browser.metrics["total_steps"] == 2
    assert browser.metrics["wall_clock"] == snapshot["timing"]["wall_clock_s"] == run.metrics["wall_clock"] == elapsed
    assert browser.wall_clock == snapshot["timing"]["wall_clock_fmt"] == run._analysis.wall_clock == "14d 4h"
    assert "wall clock 14d 4h" in snapshot["text"]


def test_generation_rates_use_the_same_timed_steps_for_both_tokens_and_seconds(native_run):
    _, run = native_run([message("measured", usage(input=20, output=90, reasoning=0, read=0)),
                         message("no output", {"input": 5}, completed=3000),
                         message("no timing", usage(output=700), completed=None)])
    analysis = project_analysis(run)
    metrics = analysis.metrics
    assert metrics["output_tokens_per_sec"] == 100
    assert metrics["output_throughput_timed_tokens"] == 90
    assert metrics["output_throughput_timed_seconds"] == 0.9
    assert metrics["output_throughput_timed_steps"] == 1
    assert metrics["output_throughput_total_steps"] == 3
    assert metrics["median_tokens_per_second"] == 122.2
    assert metrics["tokens_per_second"] is None
    verdict = next(row for row in analysis.verdicts if row["metric"] == "Throughput")
    assert verdict["status"] == "good" and "1/3 assistant steps" in verdict["detail"]
    assert analysis.agent_summaries[0]["output_tokens_per_sec"] == metrics["output_tokens_per_sec"]
    assert analysis.agent_summaries[0]["output_throughput_timed_steps"] == 1


def test_no_paired_throughput_evidence_stays_unavailable_in_browser_and_views(native_run):
    from agent_trajviz.insight.session import session_from_run
    from agent_trajviz.insight.ui.overview_tab import pack_load

    _, run = native_run([message("known tokens", usage(), completed=None), message("known duration", {"input": 5})])
    session = project_dashboard_session(session_from_run(run))
    assert session.metrics["avg_duration"] == 0.9  # Observed step duration, even without matching tokens.
    assert session.metrics["median_tokens_per_second"] is None
    assert session.metrics["output_tokens_per_sec"] is None
    verdict = next(row for row in session.verdicts if row["metric"] == "Throughput")
    assert verdict["status"] == "unavailable" and "0/2 assistant steps" in verdict["detail"]
    assert [row["total"] for row in session.shown.view("token_usage").data["steps"]] == [13, None]
    assert [row["total"] for row in run.view("token_usage").data["steps"]] == [13, 0]
    rendered = pack_load(session)
    assert "Generation throughput: N/A" in rendered["overview_kpi_html"]["value"]
    assert "0/2 timed" in rendered["overview_kpi_html"]["value"]


def test_context_growth_uses_adjacent_measured_prompts_of_the_same_agent(native_run):
    def prompt(mid, started, tokens):
        return {**message(mid, tokens), "time": {"created": BASE + started, "completed": BASE + started + 90}}

    child = {"session": {"id": "child"}, "messages": [prompt("other agent", 400, usage(input=1000, read=0))]}
    _, run = native_run([prompt("before gap", 100, usage(input=10, read=0)),
                         prompt("missing", 200, {"input": 20}),
                         {**prompt("observed", 300, usage(input=100, read=0)), "content": [child_dispatch()]},
                         prompt("next observed", 500, usage(input=175, read=0))], [child])
    assert run.steps[3]["session_id"] == "child"
    view = project_view(run, run.view("hotspots"))
    assert [(row["index"], row["growth"]) for row in view.data["top_growth"]] == [(2, 75)]
    jsonschema.validate(view.to_dict(), tv.view_schema("hotspots"))


@pytest.mark.parametrize("shared_session", [False, True])
def test_context_growth_restarts_after_compaction_in_its_own_conversation(native_run, shared_session):
    def prompt(mid, started, tokens, *, content=None):
        return {**message(mid, usage(input=tokens, read=0), content=content),
                "time": {"created": BASE + started, "completed": BASE + started + 80}}

    child = {"session": {"id": "child"},
             "messages": [prompt("child before", 200, 30), prompt("child after", 400, 80)]}
    compact = {"type": "compaction", "status": "completed", "id": "compact", "agent": "compaction",
               "time": {"created": BASE + 300}, "summary": "Reset the parent context", "tokens": usage()}
    _, run = native_run([prompt("parent before", 100, 10, content=[child_dispatch()]), compact,
                         prompt("parent after", 500, 110), prompt("parent next", 600, 150)], [child])
    if shared_session:
        # Native Claude Code tags children with their agent id but can retain
        # the parent's session id for every step.
        steps = [{**step, "session_id": "root", "parent_agent_id": "", "agent": "worker"}
                 if step.get("is_sub_agent") else step for step in run.steps]
        run = replace(run, format="claude_code", steps=steps)
    canonical = copy.deepcopy(run.to_dict(include_raw=True))
    growth = project_view(run, run.view("hotspots")).data["top_growth"]
    assert [(row["index"], row["growth"]) for row in growth] == [(1, 50), (4, 40)]
    assert run.to_dict(include_raw=True) == canonical


def test_context_growth_skips_incomplete_breakdowns_even_with_a_recorded_total(native_run):
    _, run = native_run([message("before", usage(input=10, read=0)),
                         message("missing cache", {"input": 100, "total": 110, "output": 10}),
                         message("after", usage(input=150, read=0))])
    totals = [row["tokens_total"] for row in project_analysis(run).message_rows]
    assert totals[1] == 110
    assert project_view(run, run.view("hotspots")).data["top_growth"] == []


def test_live_and_offline_dashboard_keep_workflow_and_growth_evidence_aligned(native_run, tmp_path, run_cli):
    from trajviz_obs.dashboard_cmd import build_preloaded_insight

    compact = {"type": "compaction", "status": "completed", "id": "compact", "agent": "compaction",
               "time": {"created": BASE + 200}, "summary": "Reset the context", "tokens": usage()}
    source, _ = native_run([
        message("before", usage(input=10, read=0)), compact,
        {**message("after", usage(input=110, read=0)), "time": {"created": BASE + 300, "completed": BASE + 380}},
        {**message("next", usage(input=150, read=0)), "time": {"created": BASE + 400, "completed": BASE + 480}},
        {**message("missing"), "time": {"created": BASE + 500, "completed": BASE + 580}},
        {**message("zero", usage(0, 0, 0, 0, 0)), "time": {"created": BASE + 600, "completed": BASE + 680}},
    ])
    app = build_preloaded_insight(str(source))
    load = next(entry.fn for entry in app.fns.values() if entry.api_name == "_do_load")
    outputs = load(str(source), False, "")
    markdown = next(value for value in outputs.values() if isinstance(value, str) and "Top context-growth steps" in value)
    assert "| +40 |" in markdown and "| +100 |" not in markdown
    cards = next(value for value in outputs.values() if isinstance(value, str) and 'id="wf-card-0"' in value)
    missing, zero = cards.split('id="wf-card-4"', 1)[1].split('id="wf-card-5"', 1)
    assert "<span>n/a tok</span>" in missing and "<span>0 tok</span>" in zero
    path = tmp_path / "report.html"
    code, payload = run_cli(["dashboard", "--transcript", str(source), "--html", str(path)])
    assert code == 0, payload
    growth = path.read_text().split("Top context-growth steps", 1)[1].split("</section>", 1)[0]
    assert "+40" in growth and "+100" not in growth


def test_repeated_user_messages_exclude_synthetic_messages_and_child_dispatches(native_run):
    from agent_trajviz.insight.session import session_from_run

    phrase = "Please verify the final results"
    messages = [
        {"type": kind, "id": f"u{i}", "text": text, "time": {"created": BASE + i * 10}}
        for i, (kind, text) in enumerate([
            ("user", phrase), ("synthetic", phrase), ("user", "Please verify\n the final results"),
            ("user", phrase), ("user", phrase.lower()), ("user", phrase), ("user", phrase)])
    ]
    child = {"session": {"id": "child"}, "messages": [
        {"type": "user", "id": "task", "text": phrase, "time": {"created": BASE + 200}},
        {**message(tokens=usage()), "time": {"created": BASE + 300, "completed": BASE + 400}},
    ]}
    path, run = native_run([*messages, message(tokens=usage(), content=[child_dispatch()])], [child])
    view = project_view(run, run.view("issues"))
    issue = next(row for row in view.data if row["title"].startswith("User repeated a message"))
    assert issue["title"].endswith("(5×)")
    assert issue["steps"] == [0, 1, 2, 4, 5] and phrase in issue["detail"]
    assert issue["why"]
    assert any(step["role"] == "user" and step["session_id"] == "child" for step in run.steps)
    session = project_dashboard_session(session_from_run(run))
    assert session.user_repeats == [{"message": phrase, "count": 5, "steps": [0, 1, 2, 4, 5]}]
    jsonschema.validate(view.to_dict(), tv.view_schema("issues"))
    assert len(json.loads(path.read_bytes())["messages"]) == 8


@pytest.mark.parametrize("child_tokens", [None, usage(0, 0, 0, 0, 0), usage(8, 4, 0, 2, 0)])
def test_browser_agent_token_chart_loads_missing_and_mixed_measurements(native_run, child_tokens):
    from agent_trajviz.insight.session import session_from_run
    from agent_trajviz.insight.ui.overview_tab import pack_load

    child = {"session": {"id": "child"}, "messages": [message(tokens=child_tokens)]}
    _, run = native_run([message(content=[child_dispatch()])], [child])
    session = project_dashboard_session(session_from_run(run))
    fig = pack_load(session)["agent_token_chart"]
    annotations = " ".join(annotation.text for annotation in fig.layout.annotations)
    if child_tokens is None:
        assert not fig.data and "Agent token totals were not" in annotations
    elif child_tokens["input"] == 0:
        assert list(fig.data[0].y) == [0] and "unavailable" in annotations
    else:
        assert len(fig.data) >= 2 and all(trace.y[0] is None for trace in fig.data)
        assert "unavailable" in annotations
    assert project_view(run, run.view("agent_tokens")).data["agents"][0]["total"] is None


def test_browser_agent_token_chart_retains_observed_cache_without_a_total(native_run):
    from agent_trajviz.insight.session import session_from_run
    from agent_trajviz.insight.ui.overview_tab import pack_load

    child = {"session": {"id": "child"}, "messages": [message()]}
    _, run = native_run([message(tokens={"cache": {"read": 125}}, content=[child_dispatch()])], [child])
    session = project_dashboard_session(session_from_run(run))
    fig = pack_load(session)["agent_token_chart"]
    cache_trace, = fig.data
    assert cache_trace.name == "Cache Read" and list(cache_trace.y) == [125, None]
    assert any("unavailable" in annotation.text for annotation in fig.layout.annotations)
    rows = project_view(run, run.view("agent_tokens")).data["agents"]
    assert [(row["total"], row["cache_read"]) for row in rows] == [(None, 125), (None, None)]


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


def test_measured_zero_elapsed_without_assistant_completion_reaches_every_overview(native_run):
    from agent_trajviz.insight.session import session_from_run

    path, _ = native_run([message(tokens=usage(0, 0, 0, 0, 0), completed=None)])
    document = json.loads(path.read_text())
    document["session"]["time"]["updated"] = BASE
    document["messages"][0]["time"]["created"] = BASE
    path.write_text(json.dumps(document))
    run = load_run(str(path))
    brief = project_brief(run)
    browser = project_dashboard_session(session_from_run(run))
    snapshot = build_snapshot(run, transcript_path=str(path), facts=scan_transcript(str(path)),
                              agents_meta={}, artifacts=None)
    assert browser.metrics["wall_clock"] == snapshot["timing"]["wall_clock_s"] == 0
    assert browser.wall_clock == snapshot["timing"]["wall_clock_fmt"] == "0s"
    assert "wall clock 0s" in snapshot["text"]
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
                      "cache_write_tokens", "total_duration_s", "cache_efficiency_pct",
                      "error_count"):
            assert row[field] == original[row["agent_id"]][field]
        expected = 2 if row["agent_id"] == "" else 1
        for field in ("tokens.total", "duration"):
            assert row["data_quality"]["fields"][field] == {
                "status": "observed", "observed": expected, "partial": 0, "total": expected, "reasons": {}}
    child_row = next(row for row in projected.data if row["agent_id"] == "child")
    assert child_row["total_tokens"] == 2 and child_row["output_tokens"] == 0
    assert child_row["output_tokens_per_sec"] == 0
    jsonschema.validate(projected.to_dict(), tv.view_schema("agents"))
    usage_rows = project_view(run, run.view("token_usage")).data["steps"]
    duration_rows = project_view(run, run.view("step_durations")).data["steps"]
    assert [row["agent"] for row in duration_rows] == [row["agent"] for row in usage_rows]
    assert next(row for row in usage_rows if row["agent"] == "child::general")["total"] == 2
    lanes = project_view(run, run.view("agent_swimlane")).data["lanes"]
    assert next(lane for lane in lanes if lane["id"] == "child::general")["segments"][0]["tokens"] == 2
    per_agent = project_view(run, run.view("agent_tokens")).data["agents"]
    assert next(row for row in per_agent if row["agent_id"] == "child")["total"] == 2
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
