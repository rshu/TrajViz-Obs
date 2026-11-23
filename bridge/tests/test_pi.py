"""Pi's recorded leaf selects one parent chain; historical siblings stay evidence."""

import copy
import hashlib
import json

import pytest
from agent_trajviz import LoadError

from trajviz_obs.compat import EmptySession, load_run, project_view
from trajviz_obs.pi import CAPTURE_KEY, active_branch
from trajviz_obs.sidecars import scan_transcript
from trajviz_obs.snapshot import build_snapshot


def entries(version=3):
    def message(identity, parent, role, tokens, second):
        content = f"Controlled {identity}"
        return {"type": "message", "id": identity, "parentId": parent,
                "timestamp": f"2026-10-30T02:00:0{second}.000Z", "message": {
                    "role": role, "content": content if role == "user" else [{"type": "text", "text": content}],
                    "api": "openai-completions", "provider": "openai", "model": "controlled",
                    "timestamp": 1793325600000 + second * 1000, "stopReason": "stop",
                    "usage": {"input": tokens, "output": 0, "reasoning": 0, "cacheRead": 0, "cacheWrite": 0,
                              "totalTokens": tokens}}}
    return [{"type": "session", "version": version, "id": "pi-controlled", "cwd": "/controlled",
             "timestamp": "2026-10-30T02:00:00.000Z"}, message("user", None, "user", 0, 1),
            message("abandoned", "user", "assistant", 1000, 2), message("active", "user", "assistant", 22, 3)]


def write(tmp_path, rows):
    path = tmp_path / "pi.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


@pytest.mark.parametrize("version", [2, 3])
def test_pi_active_branch_matches_session_manager_leaf_and_preserves_all_source_entries(tmp_path, version):
    rows = entries(version)
    original = copy.deepcopy(rows)
    path = write(tmp_path, rows)
    before = path.read_bytes()
    run = load_run(str(path))
    assert run.format == "pi" and len(run.steps) == 2 and run.metrics["tokens"]["total"] == 22
    assert [step["id"] for step in run.steps] == ["user", "active"]
    assert [step["raw_index"] for step in run.steps] == [1, 3]
    assert run.raw["_pi_entries"] == rows == original
    assert run.raw["_pi_active_entry_ids"] == ["user", "active"]
    assert run.sha256 == hashlib.sha256(before).hexdigest() and path.read_bytes() == before
    export = tmp_path / "trajectory.json"
    export.write_text(json.dumps(run.to_dict(include_raw=True)))
    restored = load_run(str(export))
    assert restored.steps == run.steps and restored.raw["_pi_entries"] == rows


def test_legacy_linear_pi_without_tree_links_keeps_existing_behavior(tmp_path):
    rows = entries(1)
    for row in rows[1:]:
        row.pop("parentId")
    run = load_run(str(write(tmp_path, rows)))
    assert len(run.steps) == 3 and run.metrics["tokens"]["total"] == 1022
    assert active_branch(rows) is rows


def test_future_pi_tree_version_is_refused(tmp_path):
    with pytest.raises(LoadError) as caught:
        load_run(str(write(tmp_path, entries(4))))
    assert caught.value.code == "unsupported_schema"


def test_pi_active_leaf_can_be_a_nonmessage_entry_with_model_metadata_ancestors(tmp_path):
    rows = entries()
    rows.insert(3, {"type": "model_change", "id": "model", "parentId": "user", "provider": "controlled",
                    "modelId": "selected-model", "timestamp": "2026-10-30T02:00:02.500Z"})
    rows[4]["parentId"] = "model"
    rows[4]["message"].pop("model")
    rows.append({"type": "label", "id": "label", "parentId": "active", "targetId": "active",
                 "label": "Selected", "timestamp": "2026-10-30T02:00:04.000Z"})
    run = load_run(str(write(tmp_path, rows)))
    assert len(run.steps) == 2 and run.steps[-1]["model_id"] == "selected-model"
    assert run.raw["_pi_active_entry_ids"] == ["user", "model", "active", "label"]


@pytest.mark.parametrize("invalid", ["missing-parent", "cycle", "self-cycle", "duplicate", "missing-link"])
def test_invalid_pi_active_tree_is_refused_without_guessing_a_branch(tmp_path, invalid):
    rows = entries()
    if invalid == "missing-parent":
        rows[-1]["parentId"] = "missing"
    elif invalid == "cycle":
        rows[1]["parentId"] = "active"
    elif invalid == "self-cycle":
        rows[-1]["parentId"] = "active"
    elif invalid == "duplicate":
        rows[-1]["id"] = "abandoned"
    else:
        rows[-1].pop("parentId")
    path = write(tmp_path, rows)
    before = path.read_bytes()
    with pytest.raises(LoadError) as caught:
        load_run(str(path))
    assert caught.value.code == "invalid_pi_branch" and path.read_bytes() == before


def capture(rows, **metadata):
    rows[0][CAPTURE_KEY] = {"schema_version": 1, **metadata}
    return rows


def snapshot(path, **kwargs):
    return build_snapshot(load_run(str(path), **kwargs), transcript_path=str(path), facts=scan_transcript(str(path)),
                          agents_meta={}, artifacts=None)


def native_tool_rows(*, result=True, error=False):
    rows = entries()
    rows = [rows[0], rows[1], rows[3]]
    assistant = rows[-1]
    assistant["timestamp"] = "2026-10-30T02:00:06.000Z"
    assistant["message"].update(timestamp=1793325602000, durationMs=750, stopReason="toolUse")
    assistant["message"]["usage"]["cost"] = {"input": .01, "output": .02, "cacheRead": .003,
                                              "cacheWrite": .004, "total": .037}
    assistant["message"]["content"].append({"type": "toolCall", "id": "call", "name": "codemode",
                                            "arguments": {"code": "controlled"}})
    if result:
        rows.append({"type": "message", "id": "result", "parentId": "active",
                     "timestamp": "2026-10-30T02:00:07.000Z", "message": {
                         "role": "toolResult", "toolCallId": "call", "toolName": "codemode", "isError": error,
                         "content": [{"type": "text", "text": "controlled result"}],
                         "timestamp": 1793325607000, "durationMs": 500,
                         "usage": {"input": 3, "output": 2, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 5,
                                   "cost": {"input": .001, "output": .002, "cacheRead": 0, "cacheWrite": 0, "total": .003}},
                         "nestedCalls": {"complete": True, "calls": [{"id": "nested", "name": "read",
                             "arguments": {"path": "controlled.txt"}, "status": "ok", "durationMs": 200}]}}})
    return rows


def test_live_leaf_moves_without_a_new_file_entry_and_survives_portable_reload(tmp_path):
    rows = capture(entries(), leaf_id="abandoned", context_window=200000, context_tokens=1000)
    path = write(tmp_path, rows)
    before = path.read_bytes()
    run = load_run(str(path))
    assert [s["id"] for s in run.steps] == ["user", "abandoned"]
    assert run.metrics["tokens"]["total"] == 1000
    assert run.raw["_pi_entries"] == rows and path.read_bytes() == before
    portable = tmp_path / "portable.json"
    portable.write_text(json.dumps(run.to_dict(include_raw=True)))
    for source in (path, portable):
        snap = snapshot(source)
        assert snap["context"]["window"] == 200000
        assert snap["context"]["current_occupancy"] == 1000
        assert snap["context"]["current_pct"] == .5


def test_explicit_null_leaf_is_empty_instead_of_returning_the_last_branch(tmp_path):
    rows = capture(entries(), leaf_id=None)
    assert active_branch(rows) == [rows[0]]
    with pytest.raises(EmptySession):
        load_run(str(write(tmp_path, rows)), current=True)


@pytest.mark.parametrize("metadata,code", [
    ({"leaf_id": "absent"}, "invalid_pi_branch"), ({"leaf_id": True}, "invalid_pi_branch"),
    ({"context_window": 0}, "invalid_pi_capture"), ({"context_window": True}, "invalid_pi_capture"),
    ({"context_tokens": -1}, "invalid_pi_capture"), ({"context_tokens": 1.5}, "invalid_pi_capture"),
    ({"context_window": 10 ** 1000}, "invalid_pi_capture"),
    ({"schema_version": 2}, "unsupported_schema"), ({"schema_version": True}, "unsupported_schema"),
])
def test_invalid_live_capture_is_refused(tmp_path, metadata, code):
    with pytest.raises(LoadError) as caught:
        load_run(str(write(tmp_path, capture(entries(), **metadata))))
    assert caught.value.code == code


def test_native_model_duration_and_tool_details_preserve_separate_accounting(tmp_path):
    rows = native_tool_rows()
    path = write(tmp_path, rows)
    run = load_run(str(path))
    assistant = run.steps[-1]
    assert assistant["duration"] == .75
    assert run.metrics["avg_duration"] == .75 and run.metrics["wall_clock"] == 7
    call, = assistant["tool_calls"]
    assert call["duration_ms"] == 500 and call["status"] == "success"
    assert call["metadata"]["pi_nested_calls"] == rows[-1]["message"]["nestedCalls"]
    assert call["metadata"]["pi_usage"]["totalTokens"] == 5
    assert run.metrics["tool_call_count"] == 1  # no fabricated nested output or extra full call
    assert run.metrics["tokens"]["total"] == 22  # the tool's model is separate from the agent's model
    assert run.raw["_pi_auxiliary_usage"][0]["included_in_model_totals"] is False
    assert snapshot(path)["engine_usage"]["cost"]["usd"] == pytest.approx(.04)
    duration = project_view(run, run.view("step_durations")).data["steps"][-1]
    assert duration["duration_s"] == .75


def test_pending_tools_are_unknown_and_real_failure_results_remain_failures(tmp_path):
    pending = write(tmp_path, native_tool_rows(result=False))
    run = load_run(str(pending))
    assert run.steps[-1]["tool_calls"][0]["status"] == "pending"
    snap = snapshot(pending)
    assert snap["tools"]["failed"] == 0 and snap["tools"]["unknown"] == 1
    assert snap["tools"]["success_rate"] is None
    assert not any(issue["kind"] == "error" for issue in snap["issues"])
    failed = snapshot(write(tmp_path, native_tool_rows(error=True)))
    assert failed["tools"]["failed"] == 1 and failed["tools"]["unknown"] == 0


@pytest.mark.parametrize("second_result", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
def test_reused_native_tool_ids_keep_each_turns_result_and_pending_state(tmp_path, second_result, legacy):
    rows = native_tool_rows()
    later = copy.deepcopy(rows[2])
    later.update(id="later", parentId="result", timestamp="2026-10-30T02:00:08.000Z")
    later["message"]["timestamp"] = 1793325608000
    rows.append(later)
    if second_result:
        result = copy.deepcopy(rows[3])
        result.update(id="later-result", parentId="later", timestamp="2026-10-30T02:00:09.000Z")
        result["message"].update(timestamp=1793325609000, durationMs=250)
        result["message"]["usage"]["input"] = 7
        result["message"]["nestedCalls"]["calls"][0]["id"] = "later-nested"
        rows.append(result)
    if legacy:
        rows[0]["version"] = 1
        for row in rows[1:]:
            row.pop("id")
            row.pop("parentId")
    path = write(tmp_path, rows)
    before = path.read_bytes()
    run = load_run(str(path))
    first, second = [tool for step in run.steps for tool in step["tool_calls"]]
    assert first["status"] == "success" and first["duration_ms"] == 500
    assert first["metadata"]["pi_usage"]["input"] == 3
    assert first["metadata"]["pi_nested_calls"]["calls"][0]["id"] == "nested"
    if second_result:
        assert second["status"] == "success" and second["duration_ms"] == 250
        assert second["metadata"]["pi_usage"]["input"] == 7
        assert second["metadata"]["pi_nested_calls"]["calls"][0]["id"] == "later-nested"
    else:
        assert second["status"] == "pending" and second["duration_ms"] is None
        assert "pi_usage" not in second.get("metadata", {})
        assert "pi_nested_calls" not in second.get("metadata", {})
        assert snapshot(path)["tools"]["unknown"] == 1 and snapshot(path)["tools"]["failed"] == 0
    portable = tmp_path / "portable.json"
    portable.write_text(json.dumps(run.to_dict(include_raw=True)))
    assert load_run(str(portable)).steps == run.steps
    assert path.read_bytes() == before


def test_orphan_results_with_reused_ids_preserve_their_own_runtime_details(tmp_path):
    rows = native_tool_rows()
    rows[2]["message"]["content"] = [{"type": "text", "text": "Controlled result-only import"}]
    later = copy.deepcopy(rows[3])
    later.update(id="later-result", parentId="result", timestamp="2026-10-30T02:00:08.000Z")
    later["message"].update(timestamp=1793325608000, isError=True)
    later["message"]["usage"]["input"] = 7
    later["message"]["nestedCalls"]["calls"][0]["id"] = "later-nested"
    rows.append(later)
    run = load_run(str(write(tmp_path, rows)))
    first, second = [tool for step in run.steps for tool in step["tool_calls"]]
    assert first["status"] == "success" and second["status"] == "error"
    assert first["metadata"]["pi_usage"]["input"] == 3
    assert second["metadata"]["pi_usage"]["input"] == 7
    assert first["metadata"]["pi_nested_calls"]["calls"][0]["id"] == "nested"
    assert second["metadata"]["pi_nested_calls"]["calls"][0]["id"] == "later-nested"


@pytest.mark.parametrize("value", [None, True, -.1, "0.04", float("nan"), float("inf"), 10 ** 1000])
def test_incomplete_or_invalid_native_cost_is_unknown(tmp_path, value):
    rows = native_tool_rows()
    rows[-1]["message"]["usage"]["cost"]["total"] = value
    assert "engine_usage" not in snapshot(write(tmp_path, rows))


def test_context_unknown_after_compaction_and_explicit_window_override(tmp_path):
    path = write(tmp_path, capture(native_tool_rows(), leaf_id="result", context_window=100000, context_tokens=None))
    snap = snapshot(path)
    assert snap["context"]["current_occupancy"] is None and snap["context"]["current_pct"] is None
    assert snapshot(path, window_limit=300000)["context"]["window"] == 300000


def test_incomplete_native_counters_do_not_become_measured_zero(tmp_path):
    rows = native_tool_rows()
    rows[2]["message"]["usage"].pop("input")
    snap = snapshot(write(tmp_path, rows))
    assert snap["tokens"]["input"] is None
    assert snap["tokens"]["output"] == 0
    assert snap["data_quality"]["fields"]["tokens.input"]["status"] == "unknown"


def test_measured_zero_counters_and_cost_stay_zero(tmp_path):
    rows = native_tool_rows(result=False)
    usage = rows[-1]["message"]["usage"]
    for key in ("input", "output", "reasoning", "cacheRead", "cacheWrite", "totalTokens"):
        usage[key] = 0
    usage["cost"]["total"] = 0
    snap = snapshot(write(tmp_path, rows))
    assert all(value == 0 for value in snap["tokens"].values())
    assert snap["data_quality"]["fields"]["tokens.total"]["status"] == "observed"
    assert snap["engine_usage"]["cost"]["usd"] == 0


@pytest.mark.parametrize("value", [True, -1, 1.5, float("nan"), float("inf"), 10 ** 1000])
def test_invalid_native_tokens_remain_unknown_without_breaking_other_measurements(tmp_path, value):
    rows = native_tool_rows()
    rows[2]["message"]["usage"]["input"] = value
    snap = snapshot(write(tmp_path, rows))
    assert snap["tokens"]["input"] is None
    assert snap["timing"]["avg_step_s"] == .75


def test_reported_cost_requires_all_selected_model_calls_and_excludes_siblings(tmp_path):
    rows = native_tool_rows()
    rows.insert(2, {**copy.deepcopy(rows[2]), "id": "sibling", "parentId": "user"})
    rows[2]["message"].pop("usage")
    assert snapshot(write(tmp_path, rows))["engine_usage"]["cost"]["usd"] == pytest.approx(.04)
    rows[3]["message"].pop("usage")
    assert "engine_usage" not in snapshot(write(tmp_path, rows))


def test_streaming_response_does_not_claim_a_completed_model_duration(tmp_path):
    rows = native_tool_rows(result=False)
    rows[-1]["message"]["stopReason"] = "pending"
    path = write(tmp_path, rows)
    run = load_run(str(path))
    assert run.steps[-1]["duration"] is None and run.steps[-1]["time_completed_ms"] is None
    assert snapshot(path)["timing"]["avg_step_s"] is None


def test_native_runtime_metadata_survives_export(tmp_path):
    source = write(tmp_path, capture(native_tool_rows(), leaf_id="result", context_window=100000, context_tokens=42))
    portable = tmp_path / "portable.json"
    run = load_run(str(source))
    portable.write_text(json.dumps(run.to_dict(include_raw=True)))
    restored = load_run(str(portable))
    assert restored.steps == run.steps
    assert restored.raw["_pi_entries"] == run.raw["_pi_entries"]
    assert restored.raw["_pi_auxiliary_usage"] == run.raw["_pi_auxiliary_usage"]
    snap = snapshot(portable)
    assert snap["engine_usage"]["cost"]["usd"] == pytest.approx(.04)
    assert snap["context"]["current_occupancy"] == 42 and snap["timing"]["avg_step_s"] == .75
    assert snap["context"]["peak_occupancy"] >= snap["context"]["current_occupancy"]


@pytest.mark.parametrize(("model", "tool", "expected"), [
    (True, True, "provider"), (True, False, "estimate"), (False, True, "estimate"), (None, None, "estimate"),
])
def test_native_cost_distinguishes_provider_billing_from_catalog_estimates(tmp_path, model, tool, expected):
    rows = native_tool_rows()
    for row, provenance in ((rows[2], model), (rows[3], tool)):
        if provenance is not None:
            row["message"]["usage"]["cost"]["totalFromProvider"] = provenance
    assert snapshot(write(tmp_path, rows))["engine_usage"]["cost"] == {"usd": .04, "source": expected}


@pytest.mark.parametrize("custom_entry", [True, False])
def test_user_shell_and_extension_messages_remain_workflow_evidence_not_agent_work(tmp_path, custom_entry):
    rows = native_tool_rows(result=False)
    rows[-1]["message"]["content"] = [{"type": "text", "text": "Controlled final answer"}]
    shell = {"type": "message", "id": "shell", "parentId": "user", "timestamp": "2026-10-30T02:00:01.100Z",
             "message": {"role": "bashExecution", "command": "controlled --check", "output": "Controlled shell output",
                         "exitCode": 7, "cancelled": False, "truncated": True, "fullOutputPath": "/controlled/output",
                         "excludeFromContext": True, "timestamp": 1793325601100}}
    custom = {"type": "custom_message", "id": "injected", "parentId": "shell",
              "timestamp": "2026-10-30T02:00:01.200Z", "customType": "controlled-extension",
              "content": [{"type": "text", "text": "Controlled injected context"}], "display": False,
              "details": {"controlled": True}}
    if not custom_entry:
        custom = {**{key: custom[key] for key in ("id", "parentId", "timestamp")}, "type": "message",
                  "message": {"role": "custom", **{key: custom[key] for key in ("customType", "content", "display", "details")},
                              "timestamp": 1793325601200}}
    state = {"type": "custom", "id": "state", "parentId": "injected",
             "timestamp": "2026-10-30T02:00:01.300Z", "customType": "plugin-state", "data": {"state": "unchanged"}}
    rows[-1]["parentId"] = "state"
    rows[2:2] = [shell, custom, state]
    path = write(tmp_path, rows)
    before = path.read_bytes()
    run = load_run(str(path))
    assert [step["id"] for step in run.steps] == ["user", "shell", "injected", "active"]
    assert [step["raw_index"] for step in run.steps] == [1, 2, 3, 5]
    assert run.metrics["assistant_steps"] == 1 and run.metrics["tool_call_count"] == 0 and run.metrics["tool_fail"] == 0
    user_shell = run.steps[1]
    assert user_shell["role"] == "user" and user_shell["duration"] is None
    assert "controlled --check" in user_shell["parts"][0]["text"]
    assert "Controlled shell output" in user_shell["parts"][0]["text"]
    assert user_shell["parts"][0]["metadata"]["native_role"] == "bashExecution"
    assert user_shell["parts"][0]["metadata"]["exitCode"] == 7
    assert user_shell["parts"][0]["metadata"]["excludeFromContext"] is True
    assert run.steps[2]["parts"][0]["metadata"]["native_role"] == "custom"
    assert "Controlled injected context" in run.steps[2]["parts"][0]["text"]
    assert len(run.view("workflow").data["steps"]) == 4
    snap = snapshot(path)
    assert snap["counts"]["prompts"] == snap["counts"]["user_records"] == 1
    assert snap["counts"]["assistant_steps"] == 1 and snap["counts"]["tool_calls"] == 0
    assert snap["task"] == "Controlled user"
    portable = tmp_path / "portable.json"
    portable.write_text(json.dumps(run.to_dict(include_raw=True)))
    restored = load_run(str(portable))
    assert restored.steps == run.steps and restored.raw["_pi_entries"] == rows
    assert snapshot(portable)["counts"]["prompts"] == 1
    assert path.read_bytes() == before


@pytest.mark.parametrize("human_repeats", [False, True])
def test_repeated_shell_and_extension_evidence_does_not_claim_repeated_human_prompts(tmp_path, human_repeats):
    from trajviz_obs.compat import project_analysis

    rows = native_tool_rows(result=False)
    extra, parent = [], "user"
    for kind in ["bashExecution", "custom", *(["user"] if human_repeats else [])]:
        for index in range(5):
            identity = f"{kind}-{index}"
            message = {"role": kind, "timestamp": 1793325601100}
            if kind == "bashExecution":
                message.update(command="controlled --status", output="Same controlled output", exitCode=0,
                               cancelled=False, truncated=False)
            elif kind == "custom":
                message.update(customType="controlled-extension", content="Same injected context", display=False)
            else:
                message["content"] = "Please verify the actual human instruction"
            extra.append({"type": "message", "id": identity, "parentId": parent,
                          "timestamp": "2026-10-30T02:00:01.100Z", "message": message})
            parent = identity
    rows[-1]["parentId"] = parent
    rows[2:2] = extra
    run = load_run(str(write(tmp_path, rows)))
    repeated = project_analysis(run).user_repeats
    if human_repeats:
        assert repeated == [{"message": "Please verify the actual human instruction", "count": 5,
                             "steps": [11, 12, 13, 14, 15]}]
    else:
        assert repeated == []
    issues = project_view(run, run.view("issues")).data
    repeats = [issue for issue in issues if issue["title"].startswith("User repeated a message")]
    assert len(repeats) == int(human_repeats)
    assert all("Same injected context" not in issue["detail"] and "controlled --status" not in issue["detail"]
               for issue in repeats)


def test_legacy_idless_source_is_preserved_before_conversion_and_through_export(tmp_path):
    rows = native_tool_rows(result=False)
    rows[0]["version"] = 1
    rows[-1]["message"]["usage"]["input"] = True  # invalid measurement stays untouched in source evidence
    rows[2:2] = [
        {"type": "message", "timestamp": "2026-10-30T02:00:01.100Z", "message": {
            "role": "bashExecution", "command": "controlled --status", "output": "Controlled output",
            "exitCode": 0, "cancelled": False, "truncated": False, "timestamp": 1793325601100}},
        {"type": "custom_message", "timestamp": "2026-10-30T02:00:01.200Z", "customType": "controlled",
         "content": "Controlled extension context", "display": False},
    ]
    for row in rows[1:]:
        row.pop("id", None)
        row.pop("parentId", None)
    path = write(tmp_path, rows)
    original = copy.deepcopy(rows), path.read_bytes()
    run = load_run(str(path))
    assert run.raw["_pi_entries"] == rows
    assert [step["raw_index"] for step in run.steps] == [1, 2, 3, 4]
    assert run.steps[1]["parts"][0]["metadata"]["native_role"] == "bashExecution"
    assert run.steps[2]["parts"][0]["metadata"]["native_role"] == "custom"
    assert snapshot(path)["counts"]["prompts"] == 1
    assert snapshot(path)["tokens"]["input"] is None
    portable = tmp_path / "portable.json"
    portable.write_text(json.dumps(run.to_dict(include_raw=True)))
    restored = load_run(str(portable))
    assert restored.raw["_pi_entries"] == rows and restored.steps == run.steps
    assert rows == original[0] and path.read_bytes() == original[1]


def test_fork_elapsed_covers_retained_history_and_matches_the_true_wall_clock_chart(tmp_path):
    from agent_trajviz.insight.charts.gantt import build_execution_gantt_chart

    rows = entries()
    rows = [rows[0], rows[1], rows[3]]
    rows[0].update(parentSession="/controlled/parent.jsonl", timestamp="2026-10-30T03:00:00.000Z")
    rows[-1]["message"].update(timestamp=1793325602000, durationMs=1000)
    rows.append({"type": "thinking_level_change", "id": "thinking", "parentId": "active", "thinkingLevel": "off",
                 "timestamp": "2026-10-30T03:00:00.006Z"})
    path = write(tmp_path, rows)
    before = path.read_bytes()
    run = load_run(str(path))
    assert run.metrics["wall_clock"] == 2 and run.metrics["total_duration"] == 1
    session = run.view("session").data
    assert session["started_at"] == "2026-10-30T02:00:01.000Z"
    assert session["finished_at"] == "2026-10-30T02:00:03.000Z"
    assert run.raw["_pi_inherited_timing"] == {
        "basis": "selected_branch_steps", "fork_created_at": "2026-10-30T03:00:00.000Z"}
    assert any("inherited history" in warning for warning in run.warnings)
    figure = build_execution_gantt_chart(run.steps, axis_mode="wallclock")
    ends = [float(base) + float(duration) for trace in figure.data if trace.type == "bar"
            for base, duration in zip(trace.base or [], trace.x or []) if base is not None and duration is not None]
    assert max(ends) == run.metrics["wall_clock"]
    assert snapshot(path)["timing"]["wall_clock_s"] == 2
    portable = tmp_path / "portable.json"
    portable.write_text(json.dumps(run.to_dict(include_raw=True)))
    assert load_run(str(portable)).metrics["wall_clock"] == 2
    assert run.raw["_pi_entries"] == rows and path.read_bytes() == before


def test_a_fork_without_older_retained_history_keeps_native_timing(tmp_path):
    rows = entries()
    rows[0]["parentSession"] = "/controlled/parent.jsonl"
    run = load_run(str(write(tmp_path, rows)))
    assert "_pi_inherited_timing" not in run.raw
    assert run.metrics["wall_clock"] == 3
