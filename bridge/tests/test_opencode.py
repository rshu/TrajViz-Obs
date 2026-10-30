"""Released OpenCode 2 messages retain their meaning across every bridge load path."""

from __future__ import annotations

import copy
import hashlib
import json
import zipfile

import agent_trajviz as tv
import pytest

from trajviz_obs.bundle_cmd import bundle_session
from trajviz_obs.compat import EmptySession, load_plugins, load_run
from trajviz_obs.opencode import NATIVE_KEY, normalize

BASE = 1_790_000_000_000


def usage(input=40, output=10, reasoning=5, read=20, write=2):
    return {"input": input, "output": output, "reasoning": reasoning,
            "cache": {"read": read, "write": write}}


def assistant(mid="a", *, created=100, completed=1500, **extra):
    time = {"created": BASE + created, "streamed": BASE + created + 100}
    if completed is not None:
        time["completed"] = BASE + completed
    return {"id": mid, "type": "assistant", "agent": "build",
            "model": {"providerID": "example", "id": "test-model", "variant": "high"},
            "time": time, "content": [{"type": "text", "text": "Finished"}],
            "tokens": usage(), "cost": 0.02, **extra}


def native(sid="ses_root", messages=None, children=None):
    return {"source_format": "opencode_v2", "schema_version": 1, "host_version": "2.2.2",
            "directory": "/controlled/project", "session": {"id": sid, "title": "Controlled test",
            "agent": "build", "location": {"directory": "/controlled/project"},
            "time": {"created": BASE, "updated": BASE + 4000}},
            "messages": messages if messages is not None else [assistant()], "children": children or []}


@pytest.fixture
def source(tmp_path):
    child = native("ses_child", [
        {"id": "u", "type": "user", "text": "Check this", "time": {"created": BASE + 800}},
        assistant("a", created=900, completed=1100, tokens=usage(3, 4, 0, 0, 0))])
    call = {"type": "tool", "id": "call", "name": "subagent", "time": {
        "created": BASE + 200, "ran": BASE + 400, "completed": BASE + 1200},
        "state": {"status": "completed", "input": {"prompt": "Check this"},
                  "content": [{"type": "text", "text": "Done"}], "metadata": {"sessionID": "ses_child"}}}
    failed = {"type": "tool", "id": "bad", "name": "bash", "time": {
        "created": BASE + 1250, "ran": BASE + 1300, "completed": BASE + 1400},
        "state": {"status": "error", "input": {"command": "false"},
                  "error": {"type": "tool.error", "message": "Intentional failure"}}}
    raw = native(messages=[
        {"id": "u", "type": "user", "text": "Run controlled checks", "time": {"created": BASE}},
        assistant(content=[call, failed]),
        {"id": "s", "type": "synthetic", "text": "Host update", "time": {"created": BASE + 1600}},
        {"id": "c", "type": "compaction", "status": "completed", "reason": "manual",
         "summary": "Summary", "tokens": usage(1, 2, 0, 0, 0), "time": {"created": BASE + 1700}},
        {"id": "cf", "type": "compaction", "status": "failed", "reason": "auto",
         "error": {"message": "Failed"}, "time": {"created": BASE + 1800}},
        assistant("final", created=2000, completed=2500, tokens=usage(8, 2, 0, 0, 0)),
        {"id": "i", "type": "idle", "outcome": "succeeded", "time": {"created": BASE + 2600}},
    ], children=[child])
    path = tmp_path / "source.json"
    path.write_text(json.dumps(raw))
    return path, raw


def test_v2_roles_tokens_tools_and_native_source_are_preserved(source):
    path, raw = source
    before = path.read_bytes()
    run = load_run(str(path))
    assert run.format == "opencode" and run.raw[NATIVE_KEY] == raw
    assert run.sha256 == hashlib.sha256(before).hexdigest() and path.read_bytes() == before
    assert [s["role"] for s in run.steps] == ["user", "assistant", "user", "assistant", "compaction", "assistant"]
    main = run.steps[1]
    assert main["tokens"] == {"total": 77, "input": 40, "output": 10, "reasoning": 5,
                              "cache_read": 20, "cache_write": 2}
    assert main["message_id"] == "ses_root:a" and main["model_id"] == "test-model"
    assert main["provider_id"] == "example" and main["duration"] == 1.4
    call, failed = main["tool_calls"]
    assert call["tool_name"] == "subagent" and call["tool_id"] == "ses_root:call"
    assert call["time_start"] == BASE + 400 and call["time_end"] == BASE + 1200
    assert call["output"] == "Done" and failed["status"] == "error"
    assert "Intentional failure" in failed["error"] and main["error_count"] == 1
    child = next(s for s in run.steps if s["message_id"] == "ses_child:a")
    assert child["is_sub_agent"] and child["session_depth"] == 1 and child["parent_session_id"] == "ses_root"
    assert sum(s["tokens"]["total"] for s in run.steps) == 97
    compaction = next(s for s in run.steps if s["role"] == "compaction")
    assert compaction["compaction_reason"] == "manual" and compaction["tokens"]["total"] == 3


def test_snapshot_counts_real_main_prompts_and_compactions(source, run_cli):
    path, _ = source
    code, snapshot = run_cli(["snapshot", "--transcript", str(path), "--window", "200000"])
    assert code == 0
    assert snapshot["title"] == "Controlled test" and snapshot["task"] == "Run controlled checks"
    assert snapshot["counts"]["prompts"] == 1 and snapshot["counts"]["assistant_steps"] == 3
    assert snapshot["counts"]["compactions"] == 1 and snapshot["counts"]["agents"] == 2
    assert snapshot["tools"]["calls"] == 2 and snapshot["tools"]["failed"] == 1
    assert snapshot["context"]["current_occupancy"] == 8
    assert snapshot["context"]["current_pct"] == 0.0
    assert snapshot["series"]["context"]["occupancy"]
    failed = next(issue for issue in snapshot["issues"] if issue["kind"] == "error")
    assert failed["evidence"] == "bash: false"


def test_normalizer_is_non_mutating_and_deterministic(source):
    _, raw = source
    original = copy.deepcopy(raw)
    result = normalize(raw)
    assert raw == original and normalize(raw) == result
    assert normalize(result) is result
    assert result["_trajviz_obs_normalizer_version"] == 1


def test_bundle_roundtrip_preserves_native_source_and_all_views(source, tmp_path):
    path, raw = source
    expected = load_run(str(path))
    result = bundle_session(str(path), tmp_path / "bundle.zip")
    assert result["members"] == ["source.json"]
    with zipfile.ZipFile(result["path"]) as archive:
        assert archive.read("source.json") == path.read_bytes()
    actual = load_run(result["path"])
    assert actual.raw[NATIVE_KEY] == raw and actual.steps == expected.steps
    def summary(run):
        return {k: v for k, v in run.view("summary").data.items() if k not in {"source", "sha256"}}

    assert summary(actual) == summary(expected)
    assert actual.view("agent_swimlane").data == expected.view("agent_swimlane").data


def test_bridge_export_reload_preserves_source_and_identity(source, tmp_path, run_cli):
    path, raw = source
    code, _ = run_cli(["json", "--transcript", str(path), "--out", str(tmp_path / "export"),
                       "--with-trajectory"])
    assert code == 0
    actual = load_run(str(tmp_path / "export" / "trajectory.json"))
    assert actual.raw[NATIVE_KEY] == raw and actual.steps == load_run(str(path)).steps


def test_native_flat_export_is_supported_without_capture_marker(source, tmp_path):
    _, raw = source
    export = {"info": raw["session"], "messages": raw["messages"]}
    path = tmp_path / "native-export.json"
    path.write_text(json.dumps(export))
    assert load_run(str(path)).steps[1]["role"] == "assistant"


def test_forks_and_unconfirmed_sessions_are_not_counted_as_subagents(source):
    _, raw = source
    fork = native("ses_fork", [assistant()])
    fork["session"]["fork"] = {"sessionID": "ses_root", "boundary": {"type": "message", "messageID": "a"}}
    unrelated = native("ses_unrelated", [assistant()])
    raw["children"] += [fork, unrelated]
    raw["messages"][1]["content"][0]["state"]["metadata"]["sessionID"] = "ses_fork"
    result = normalize(raw)
    assert not any(m["info"]["isSubAgent"] for m in result["messages"])
    assert len(result["_load_warnings"]) >= 2 and result[NATIVE_KEY] == raw


def test_duplicate_messages_do_not_duplicate_usage_or_tool_calls(source):
    _, raw = source
    raw["messages"].append(copy.deepcopy(raw["messages"][1]))
    raw["children"].append(copy.deepcopy(raw["children"][0]))
    result = normalize(raw)
    assert sum(m["info"]["role"] == "assistant" for m in result["messages"]) == 3


def test_incomplete_assistant_keeps_usage_and_timing_unknown(tmp_path):
    message = assistant(completed=None)
    message.pop("tokens")
    path = tmp_path / "running.json"
    path.write_text(json.dumps(native(messages=[message])))
    step = load_run(str(path)).steps[0]
    assert step["duration"] is None and step["time_completed_ms"] is None
    assert "Total Tokens" in step["_metrics_unavailable_fields"]


@pytest.mark.parametrize("status", ["pending", "running", "streaming"])
def test_unfinished_native_tool_outcome_stays_unknown_in_upstream_nullable_views(tmp_path, status):
    message = assistant(completed=None, content=[{
        "type": "tool", "id": "active", "name": "shell", "time": {"created": BASE, "ran": BASE + 10},
        "state": {"status": status, "input": {"command": "echo controlled"}}}])
    message.pop("tokens")
    path = tmp_path / "unfinished-native.json"
    path.write_text(json.dumps(native(messages=[message])))
    run = load_run(str(path))
    step = run.steps[0]
    assert "Total Tokens" in step["_metrics_unavailable_fields"] and step["duration"] is None
    assert run.metrics["tool_success_rate"] is None
    assert run.metrics["tool_unknown_outcomes"] == 1 and run.metrics["tool_fail"] == 0
    assert run.view("tool_outcomes").data["calls"][0]["ok"] is None
    tool_health = next(row for row in run.view("health").data if row["metric"] == "Tool Success")
    assert tool_health["status"] == "unavailable"


def test_completed_native_zero_exit_is_observed_success_not_missing(tmp_path):
    call = {"type": "tool", "id": "zero", "name": "shell", "time": {"created": BASE,
            "ran": BASE + 10, "completed": BASE + 10}, "state": {"status": "completed",
            "input": {"command": "true"}, "metadata": {"exit": 0}}}
    path = tmp_path / "zero-native.json"
    path.write_text(json.dumps(native(messages=[assistant(content=[call])])))
    run = load_run(str(path))
    assert run.view("tool_outcomes").data["calls"][0]["ok"] is True
    assert run.metrics["tool_success_rate"] == 100 and run.metrics["tool_fail"] == 0
    assert run.steps[0]["tool_calls"][0]["metadata"]["exit"] == 0


def test_empty_current_materialization_reports_empty_session(tmp_path):
    path = tmp_path / "empty.json"
    path.write_text(json.dumps(native(messages=[])))
    with pytest.raises(EmptySession):
        load_run(str(path), current=True)


def test_schema_version_and_format_mismatch_are_explicit(source):
    _, raw = source
    raw["schema_version"] = 99
    with pytest.raises(ValueError, match="schema_version"):
        normalize(raw)
    path, _ = source
    load_plugins()
    with pytest.raises(tv.LoadError, match="mismatch"):
        tv.load(str(path), format="claude_code")


def test_legacy_opencode_envelope_is_unchanged():
    old = {"info": {"id": "ses_old"}, "messages": [{"info": {"role": "assistant"}, "parts": []}]}
    assert normalize(old) is old


def test_native_dashboard_loader_uses_the_same_normalization(source):
    from trajviz_obs.dashboard_cmd import build_preloaded_insight

    path, raw = source
    app = build_preloaded_insight(str(path), window=100000)
    loader = next(block for block in app.fns.values() if getattr(block.fn, "__name__", "") == "_do_load")
    values = loader.fn(str(path), False, "")
    actual = next(value for value in values.values() if isinstance(value, tv.Trajectory))
    assert actual.raw[NATIVE_KEY] == raw and actual.steps == load_run(str(path)).steps
    assert actual.context_pressure()["stats"]["window_limit"] == 100000


def test_unknown_usage_and_content_do_not_become_plausible_empty_steps():
    malformed = assistant()
    malformed.pop("content")
    with pytest.raises(ValueError, match="content array"):
        normalize(native(messages=[malformed]))


def test_explicit_skill_activation_is_visible_without_an_invented_tool_call(tmp_path):
    raw = native(messages=[{"id": "skill", "type": "skill", "skill": "skill_id", "name": "review",
                            "text": "Review instructions", "time": {"created": BASE}}, assistant()])
    path = tmp_path / "skill.json"
    path.write_text(json.dumps(raw))
    run = load_run(str(path))
    assert len(run.view("skill_calls").data["links"]) == 1
    assert run.view("skill_calls").data["links"][0]["skill"] == "review"
    assert run.view("skill_calls").data["links"][0]["calls"] == 1
    assert sum(len(step["tool_calls"]) for step in run.steps) == 0
    assert sum(step["role"] == "assistant" for step in run.steps) == 1


def test_prompt_selected_skill_is_observed_without_fabricating_tool_or_assistant_steps(tmp_path):
    raw = native(messages=[{"id": "msg_prompt", "type": "user", "text": "Review this code",
        "skills": [{"id": "review_id", "name": "review", "text": "Controlled skill instructions"}],
        "time": {"created": BASE}}, assistant()])
    path = tmp_path / "prompt-skill.json"
    path.write_text(json.dumps(raw))
    run = load_run(str(path))
    links = run.view("skill_calls").data["links"]
    assert len(links) == 1 and links[0]["skill"] == "review" and links[0]["calls"] == 1
    assert len(run.steps) == 2 and sum(step["role"] == "assistant" for step in run.steps) == 1
    assert not any(step["tool_calls"] for step in run.steps)
    assert run.raw[NATIVE_KEY] == raw


def test_manual_compaction_is_not_reported_as_an_agent_compaction(source):
    path, _ = source
    run = load_run(str(path), window_limit=100000)
    assert run.view("premature_compactions").data == []
    assert run.context_pressure()["stats"]["compaction_count"] == 1


def test_completed_shell_with_nonzero_exit_is_failed_in_every_view(tmp_path):
    tool = {"type": "tool", "id": "command", "name": "shell", "time": {"created": BASE,
            "ran": BASE + 10, "completed": BASE + 100}, "state": {"status": "completed",
            "input": {"command": "false"}, "content": [{"type": "text", "text": "Exited with code 1"}],
            "metadata": {"exit": 1}}}
    path = tmp_path / "failed-shell.json"
    raw = native(messages=[assistant(content=[tool])])
    path.write_text(json.dumps(raw))
    run = load_run(str(path))
    assert run.steps[0]["error_count"] == 1 and run.steps[0]["tool_calls"][0]["status"] == "error"
    assert run.metrics["tool_fail"] == 1
    assert run.raw[NATIVE_KEY]["messages"][0]["content"][0]["state"]["status"] == "completed"


def test_structured_native_tool_error_has_a_readable_message_and_retains_full_evidence(tmp_path, run_cli):
    error = {"type": "tool.execution", "message": "Subagent depth limit reached (1). "
             'Increase "experimental.subagent_depth" to allow nested subagents.'}
    tool = {"type": "tool", "id": "depth-limit", "name": "subagent", "time": {"created": BASE,
            "ran": BASE + 10, "completed": BASE + 100}, "state": {"status": "error", "input": {},
            "error": error, "metadata": {"existing": "preserved"}}}
    raw = native(messages=[assistant(content=[tool])])
    path = tmp_path / "structured-error.json"
    path.write_text(json.dumps(raw))
    before = path.read_bytes()
    run = load_run(str(path))
    call = run.steps[0]["tool_calls"][0]
    assert call["error"] == error["message"] and call["error_type"] == "tool.execution"
    assert call["metadata"] == {"existing": "preserved", "_trajviz_obs_native_error": error}
    code, snapshot = run_cli(["snapshot", "--transcript", str(path)])
    assert code == 0
    issue = next(row for row in snapshot["issues"] if row["kind"] == "error")
    assert "Subagent depth limit reached" in issue["title"] and "tool.execution" not in issue["title"]
    assert run.raw[NATIVE_KEY] == raw and path.read_bytes() == before


def test_compaction_request_usage_is_not_reported_as_the_resulting_window(tmp_path, run_cli):
    raw = native(messages=[assistant(), {"id": "compact", "type": "compaction", "status": "completed",
        "reason": "manual", "summary": "Summary", "tokens": usage(10000, 100, 0, 0, 0),
        "time": {"created": BASE + 1700}}])
    path = tmp_path / "compact.json"
    path.write_text(json.dumps(raw))
    code, snapshot = run_cli(["snapshot", "--transcript", str(path), "--window", "200000"])
    assert code == 0
    assert snapshot["context"]["current_occupancy"] is None
    assert snapshot["context"]["current_pct"] is None
    assert snapshot["context"]["peak_occupancy"] == 62


def test_native_model_and_terminal_failure_are_reported_without_control_steps(tmp_path):
    error = {"type": "model.error", "message": "Controlled provider failure"}
    message = assistant(error=error)
    raw = native(messages=[message, {"id": "idle", "type": "idle", "outcome": "failed", "error": error,
                                    "time": {"created": BASE + 1600}}])
    path = tmp_path / "native-failure.json"
    path.write_text(json.dumps(raw))
    before = path.read_bytes()
    run = load_run(str(path))
    assert len(run.steps) == 1 and run.steps[0]["finish"] == "error"
    assert run.raw["output"]["error"] == "Controlled provider failure"
    assert run.raw["_opencode_v2_history_errors"] == ["Controlled provider failure"]
    assert run.warnings.count("OpenCode model/session error: Controlled provider failure") == 1
    assert run.raw[NATIVE_KEY] == raw and path.read_bytes() == before


def test_native_recovery_clears_terminal_failure_but_preserves_history(tmp_path):
    error = {"message": "Controlled interrupted turn"}
    raw = native(messages=[assistant(error=error), {"id": "idle-a", "type": "idle", "outcome": "interrupted",
        "error": error}, {"id": "resumed", "type": "user", "text": "Try again", "time": {"created": BASE + 1700}},
        assistant("recovered", created=1800, completed=2200), {"id": "idle-b", "type": "idle",
        "outcome": "succeeded", "time": {"created": BASE + 2300}}])
    path = tmp_path / "native-recovery.json"
    path.write_text(json.dumps(raw))
    run = load_run(str(path))
    assert [step["role"] for step in run.steps] == ["assistant", "user", "assistant"]
    assert run.raw["output"]["error"] is None and run.raw["_opencode_v2_errors"] == []
    assert run.raw["_opencode_v2_history_errors"] == ["Controlled interrupted turn"]
    assert "OpenCode model/session error: Controlled interrupted turn" in run.warnings


def test_native_child_model_failure_cannot_replace_the_parent_outcome(source):
    path, raw = source
    raw["children"][0]["messages"].append({"id": "child-idle", "type": "idle", "outcome": "failed",
                                         "error": {"message": "Controlled child failure"}})
    path.write_text(json.dumps(raw))
    run = load_run(str(path))
    assert run.raw["output"]["error"] is None
    assert "OpenCode model/session error: Controlled child failure" in run.warnings
    assert sum(step["role"] == "assistant" for step in run.steps) == 3


def test_native_zero_usage_and_successful_outcome_remain_observed(tmp_path):
    raw = native(messages=[assistant(tokens=usage(0, 0, 0, 0, 0)),
                           {"id": "idle", "type": "idle", "outcome": "succeeded"}])
    path = tmp_path / "native-zero.json"
    path.write_text(json.dumps(raw))
    run = load_run(str(path))
    assert not run.steps[0]["_metrics_unavailable_fields"]
    assert run.metrics["tokens"]["total"] == 0
    assert run.raw["output"]["error"] is None and not run.raw["_opencode_v2_history_errors"]
    assert len(run.steps) == 1


def test_context_view_main_alias_selects_the_native_parent_window(source, tmp_path, run_cli):
    path, _ = source
    code, result = run_cli(["view", "--transcript", str(path), "--artifacts", str(tmp_path / "views"),
                            "context_pressure", "--option", "agent=main", "--json"])
    assert code == 0
    agents = result["inline"]["data"]["series"]["agents"]
    assert len(agents) == 1 and agents[0]["agent_id"] == "ses_root"


@pytest.mark.parametrize("alias", ["source", "hardlink", "symlink"])
def test_native_label_output_alias_is_refused_before_prompt_capture(source, tmp_path, run_cli, monkeypatch, alias):
    from trajviz_obs import llm_cmd

    path, _ = source
    destination = path
    if alias != "source":
        destination = tmp_path / "output-alias.json"
        if alias == "hardlink":
            destination.hardlink_to(path)
        else:
            try:
                destination.symlink_to(path)
            except OSError:
                pytest.skip("This platform does not permit test symlinks")
    before = path.read_bytes()

    def no_capture(*_args, **_kwargs):
        pytest.fail("An output alias must be refused before capturing model prompts")

    monkeypatch.setattr(llm_cmd, "_capture", no_capture)
    code, result = run_cli(["llm-prompt", "--feature", "label", "--transcript", str(path),
                            "--artifacts", str(tmp_path / "artifacts"), "--out", str(destination)])
    assert code == 1 and result["error"]["code"] == "bad_output"
    assert path.read_bytes() == before and destination.read_bytes() == before


def test_native_window_remains_consistent_in_exports_and_bundles(source, tmp_path, run_cli):
    path, raw = source
    raw["window"] = 200000
    path.write_text(json.dumps(raw))
    live = load_run(str(path))
    assert live.context_pressure()["stats"]["window_limit"] == 200000
    assert live.raw["metadata"]["context_window_limit"] == 200000
    assert load_run(str(path), window_limit=100000).context_pressure()["stats"]["window_limit"] == 100000
    bundle = bundle_session(str(path), tmp_path / "portable.zip")
    assert load_run(bundle["path"]).context_pressure()["stats"]["window_limit"] == 200000
    code, _ = run_cli(["json", "--transcript", str(path), "--out", str(tmp_path / "export"), "--with-trajectory"])
    assert code == 0
    restored = load_run(str(tmp_path / "export" / "trajectory.json"))
    assert restored.context_pressure()["stats"]["window_limit"] == 200000
    assert restored.raw[NATIVE_KEY] == raw


@pytest.mark.parametrize("window", [True, -1, 0, 12.5, "200000", None])
def test_unusable_native_window_does_not_override_model_resolution(source, window):
    path, raw = source
    raw["window"] = window
    path.write_text(json.dumps(raw))
    assert "context_window_limit" not in load_run(str(path)).raw["metadata"]


def timed_tool(name, start, end, tid):
    return {"type": "tool", "id": tid, "name": name,
            "time": {"created": BASE + start, "ran": BASE + start, "completed": BASE + end},
            "state": {"status": "completed", "input": {}, "content": []}}


def test_parallel_native_subagents_use_wall_wait_union_across_brief_snapshot_views_and_charts(tmp_path, run_cli):
    from agent_trajviz import llm
    from agent_trajviz.insight import diagnostics, metrics

    # Measured in a real 2.2.2 session: the 10.576s and 18.042s child waits
    # overlap. Native streamed=35.880s differs from the exclusive remainder
    # by only 4ms of scheduling; adding the waits loses 10.574s of model time.
    message = assistant(created=0, completed=53920,
        time={"created": BASE, "streamed": BASE + 35880, "completed": BASE + 53920},
        tokens=usage(11247, 664, 3031, 0, 0), content=[
            timed_tool("subagent", 35875, 46451, "parallel-a"),
            timed_tool("subagent", 35877, 53919, "parallel-b")])
    raw = native(messages=[message, *[assistant(f"short{i}", created=55000 + i * 1500,
                                              completed=56000 + i * 1500) for i in range(10)]])
    path = tmp_path / "parallel-native.json"
    path.write_text(json.dumps(raw))
    before = path.read_bytes()
    run = load_run(str(path))
    step = run.steps[0]
    assert [metrics.tool_call_duration_ms(call) for call in step["tool_calls"]] == [10576, 18042]
    assert metrics.spawn_wait_seconds(step) == pytest.approx(18.044)
    assert metrics.generation_seconds(step) == pytest.approx((35.876, 0))
    decomposition = diagnostics.decompose_hotspot_duration(step)
    assert decomposition["tool_s"] == 18.04 and decomposition["inference_s"] == 35.88
    chart = run.view("step_durations").data["steps"][0]
    assert chart["duration_s"] == pytest.approx(35.876) and chart["spawn_adjusted"]
    assert run.view("step_analytics").data[0]["tool_time_ms"] == 0  # child work is already counted there
    bottleneck = next(row for row in run.view("bottlenecks").data if row["step_idx"] == 0)
    assert bottleneck["decomposition"] == decomposition
    assert "35.88s LLM inference" in bottleneck["detail"] and "18.04s tool execution" in bottleneck["detail"]
    brief = llm.brief(run)
    assert "35.88s LLM inference" in brief and "18.04s tool execution" in brief
    code, snapshot = run_cli(["snapshot", "--transcript", str(path)])
    assert code == 0 and snapshot["series"]["durations"]["seconds"][0] == pytest.approx(35.876)
    assert "35.88s LLM inference" in snapshot["bottlenecks"][0]["detail"]
    assert run.sha256 == hashlib.sha256(before).hexdigest() and path.read_bytes() == before
    assert run.raw[NATIVE_KEY] == raw
    # The V2 correction must not alter timing semantics for another host.
    legacy = {k: v for k, v in step.items() if k != NATIVE_KEY}
    assert diagnostics.decompose_hotspot_duration(legacy)["tool_s"] == 28.62
    assert metrics.spawn_wait_seconds(legacy) == pytest.approx(18.042)


def test_parallel_native_regular_tools_share_the_same_union_in_analytics_and_generation(tmp_path):
    from agent_trajviz.insight import diagnostics, metrics

    raw = native(messages=[assistant(created=0, completed=10000, content=[
        timed_tool("read", 2000, 7000, "read"), timed_tool("write", 4000, 8000, "write")])])
    path = tmp_path / "parallel-tools.json"
    path.write_text(json.dumps(raw))
    run = load_run(str(path))
    step = run.steps[0]
    assert metrics.generation_seconds(step) == (4, 6)
    assert diagnostics.decompose_hotspot_duration(step)["tool_s"] == 6
    analytics = run.view("step_analytics").data[0]
    assert analytics["tool_time_ms"] == 6000 and analytics["tool_time_share"] == 0.6
    assert run.view("hotspots").data["slowest"][0]["tool_time_sum"] == 6
    assert run.metrics["tool_time_total"] == 6
    assert [metrics.tool_call_duration_ms(call) for call in step["tool_calls"]] == [5000, 4000]


def test_native_mixed_and_disjoint_spawn_waits_do_not_double_count_model_time(tmp_path):
    from agent_trajviz.insight import diagnostics, metrics

    raw = native(messages=[assistant(created=0, completed=10000, content=[
        timed_tool("subagent", 2000, 5000, "child-a"),
        timed_tool("subagent", 7000, 9000, "child-b"),
        timed_tool("read", 4000, 8000, "read")])])
    path = tmp_path / "mixed-tools.json"
    path.write_text(json.dumps(raw))
    run = load_run(str(path))
    step = run.steps[0]
    assert metrics.spawn_wait_seconds(step) == 5  # disjoint waits are added
    assert metrics.generation_seconds(step) == (3, 2)  # regular tool adds only its uncovered interval
    assert diagnostics.decompose_hotspot_duration(step)["tool_s"] == 7
    assert run.view("step_durations").data["steps"][0]["duration_s"] == 5
    assert run.metrics["delegation_time_total"] == 5 and run.metrics["tool_time_total"] == 4
