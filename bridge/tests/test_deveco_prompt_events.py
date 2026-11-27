"""Prompt event markers retain the evidence boundary of captured requests."""
from __future__ import annotations

import copy
import json

import pytest

from trajviz_obs.deveco_prompt_components import MAX_INVENTORY_COMPONENTS, build_component_inventory
from trajviz_obs.deveco_prompt_events import MAX_EVENT_SOURCE_REFS, build_prompt_events


def request(body, *, rid="after", step=2, **changes):
    result = {"id": rid, "request_id": rid, "purpose": "assistant", "scope": "recorded_request",
              "session_id": "root", "agent_id": "build", "assistant_message_id": "assistant-" + rid,
              "step_index": step, "coverage": {"complete": True}, "request_body": body,
              "native_mapping": {"status": "verified"},
              "evidence_quality": {"outbound_body_observed": True, "body_sha256_verified": True}, **changes}
    return result


def tool(name, **extra):
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object", **extra}}}


def pair(before, after):
    return build_prompt_events(request(after), request(before, rid="before", step=1), baseline_status="available")


def events(result, kind):
    return [row for row in result["events"] if row["kind"] == kind]


def test_first_snapshot_reports_presence_without_inventing_additions_or_compaction():
    result = build_prompt_events(request({"messages": [{"role": "system", "content": "Private instructions"}],
                                          "tools": [tool("read"), tool("write")]}), baseline_status="first_request")
    assert [(row["kind"], row["count"], row["action"]) for row in result["events"]] == [
        ("system_snapshot", 1, "snapshot"), ("tool_snapshot", 2, "snapshot")]
    assert result["coverage"]["instructions"] == result["coverage"]["tools"] == "snapshot_only"
    assert result["coverage"]["context_edits"] == "unavailable"
    assert {row["evidence"] for row in result["events"]} == {"observed_snapshot"}
    assert all(row["native_event"] is False and row["lineage_verified"] is False for row in result["events"])
    assert "Private instructions" not in json.dumps(result)


def test_added_tool_result_has_source_pointer_and_no_fake_schema_or_instruction_change():
    first = {"messages": [{"role": "system", "content": "rules"}, {"role": "user", "content": "Task"}],
             "tools": [tool("read")]}
    second = {**first, "messages": [*first["messages"], {"role": "assistant", "content": "Working"},
               {"role": "tool", "tool_call_id": "call", "content": "PRIVATE FILE CONTENT"}]}
    result = pair(first, second)
    assert {row["kind"] for row in result["events"]} == {"context_message", "tool_results"}
    event = events(result, "tool_results")[0]
    assert event["count"] == 1 and event["action"] == "added"
    assert event["evidence"] == "snapshot_comparison" and event["baseline_request_id"] == "before"
    assert event["source_refs"][0]["json_pointer"] == "/messages/3"
    assert event["source_refs"][0]["step_index"] == 2
    assert event["source_refs"][0]["side"] == "after"
    assert result["coverage"]["instructions"] == result["coverage"]["tools"] == "compared"
    assert result["coverage"]["message_differences"] == "compared"
    assert "PRIVATE FILE CONTENT" not in json.dumps(result)


def test_tool_definition_updates_and_removals_are_not_runtime_tool_executions():
    result = pair({"tools": [tool("read"), tool("obsolete")]},
                  {"tools": [tool("read", required=["path"]), tool("write")]})
    assert {(row["kind"], row["count"]) for row in result["events"]} == {
        ("tool_added", 1), ("tool_updated", 1), ("tool_removed", 1)}
    update = events(result, "tool_updated")[0]
    assert [ref["side"] for ref in update["source_refs"]] == ["before", "after"]
    assert {ref["request_id"] for ref in update["source_refs"]} == {"before", "after"}
    assert not events(result, "tool_results")


def test_instruction_add_update_remove_preserve_exact_difference_kind():
    result = pair({"system": "old", "messages": [{"id": "remove", "role": "system", "content": "retire"}]},
                  {"system": "new", "messages": [{"id": "add", "role": "developer", "content": "new rule"}]})
    assert {(row["kind"], row["count"]) for row in result["events"]} == {
        ("system_section_added", 1), ("system_section_updated", 1), ("system_section_removed", 1)}
    assert not any(row["kind"] == "compaction" for row in result["events"])


def test_anonymous_instruction_replacement_stays_remove_and_add():
    result = pair({"messages": [{"role": "system", "content": "old"}]},
                  {"messages": [{"role": "system", "content": "new"}]})
    assert {row["kind"] for row in result["events"]} == {"system_section_added", "system_section_removed"}


@pytest.mark.parametrize("field", ["system", "instructions"])
@pytest.mark.parametrize("empty", ["", [], None])
def test_clearing_an_instruction_emits_removal_without_an_empty_replacement(field, empty):
    result = pair({field: "rules"}, {field: empty})
    assert [(row["kind"], row["action"], row["count"]) for row in result["events"]] == [
        ("system_section_removed", "removed", 1)]
    assert result["events"][0]["source_refs"][0]["side"] == "before"
    assert result["coverage"]["instructions"] == "compared"


@pytest.mark.parametrize("field", ["system", "instructions"])
@pytest.mark.parametrize("empty", ["", [], None])
def test_instruction_after_an_empty_field_is_an_addition(field, empty):
    result = pair({field: empty}, {field: "rules"})
    assert [(row["kind"], row["action"], row["count"]) for row in result["events"]] == [
        ("system_section_added", "added", 1)]
    assert result["events"][0]["source_refs"][0]["side"] == "after"


@pytest.mark.parametrize("field", ["messages", "input"])
def test_clearing_messages_does_not_add_an_empty_aggregate_message(field):
    message = {"role": "user", "content": "Task"}
    result = pair({field: [message]}, {field: []})
    assert [(row["kind"], row["action"], row["count"]) for row in result["events"]] == [
        ("context_edit", "removed", 1)]
    assert result["events"][0]["source_refs"][0]["json_pointer"] == "/" + field + "/0"
    assert result["coverage"]["message_differences"] == "compared"


def test_same_assistant_retry_keeps_snapshot_coverage_despite_explicit_predecessor():
    current = request({"system": "new"}, assistant_message_id="same-assistant")
    previous = request({"system": "old"}, rid="before", step=2, assistant_message_id="same-assistant")
    current["previous_request_id"] = "before"
    result = build_prompt_events(current, previous, baseline_status="available")
    assert result["coverage"]["instructions"] == "snapshot_only"
    assert result["coverage"]["reason"] == "not_a_previous_assistant_boundary"
    assert [(row["kind"], row["evidence"]) for row in result["events"]] == [
        ("system_snapshot", "observed_snapshot")]


def test_smaller_context_is_only_a_verified_difference_and_never_compaction():
    result = pair({"messages": [{"id": "a", "role": "user", "content": "A"},
                                 {"id": "b", "role": "tool", "content": "Very large"}]},
                  {"messages": [{"id": "a", "role": "user", "content": "A"}]})
    assert len(result["events"]) == 1
    event = result["events"][0]
    assert event["kind"] == "context_edit" and event["action"] == "removed"
    assert event["evidence"] == "snapshot_comparison" and event["native_event"] is False
    assert result["coverage"]["context_edits"] == "unavailable"


@pytest.mark.parametrize("change, reason", [
    ({"session_id": "child"}, "different_session"),
    ({"agent_id": "review"}, "different_agent"),
    ({"step_index": 3}, "not_a_previous_assistant_boundary"),
    ({"assistant_message_id": "assistant-after"}, "not_a_previous_assistant_boundary"),
    ({"coverage": {"complete": False}}, "complete_body_unavailable"),
    ({"native_mapping": {"status": "pending"}}, "native_owner_unverified"),
])
def test_invalid_baseline_never_creates_removal_or_change_events(change, reason):
    current = request({"messages": [{"role": "user", "content": "Now"}]})
    previous = request({"system": "Absent now", "tools": [tool("read")]}, rid="before", step=1, **change)
    result = build_prompt_events(current, previous, baseline_status="available")
    assert result["events"] == []
    assert result["coverage"]["reason"] == reason
    assert result["coverage"]["instructions"] == "snapshot_only"


@pytest.mark.parametrize("baseline_status", ["unavailable", "ambiguous_retries", "first_request"])
def test_a_supplied_but_unapproved_baseline_is_not_selected_implicitly(baseline_status):
    result = build_prompt_events(request({}), request({"system": "old"}, rid="before", step=1),
                                 baseline_status=baseline_status)
    assert result["coverage"]["reason"] == baseline_status
    assert all(event["baseline_request_id"] is None and event["action"] == "snapshot" for event in result["events"])


@pytest.mark.parametrize("changes", [
    {"coverage": {"complete": False}}, {"native_mapping": {"status": "pending"}},
    {"evidence_quality": {"outbound_body_observed": True, "body_sha256_verified": False}},
    {"step_index": None}, {"assistant_message_id": None}, {"scope": "trajectory_estimate"},
])
def test_unverified_current_source_yields_no_event_markers(changes):
    result = build_prompt_events(request({"system": "secret"}, **changes))
    assert result["events"] == [] and result["coverage"]["instructions"] == "unavailable"


def test_auxiliary_title_requests_are_not_applicable_to_assistant_event_coverage():
    result = build_prompt_events(request({"messages": [{"role": "user", "content": "title"}]}, purpose="title"))
    assert result["events"] == [] and result["coverage"]["applicable"] is False


def test_partial_inventory_fails_closed_before_content_materialization(monkeypatch):
    import trajviz_obs.deveco_prompt_events as module
    def fail(*_):
        raise AssertionError("Partial inventory should not be materialized")
    monkeypatch.setattr(module, "build_component_inventory", fail)
    body = {"messages": [{"role": "user", "content": "repeat"}] * (MAX_INVENTORY_COMPONENTS + 1)}
    result = build_prompt_events(request(body))
    assert result["events"] == [] and result["coverage"]["reason"] == "component_inventory_partial"


def test_event_source_refs_are_bounded_while_complete_component_count_is_retained():
    body = {"tools": [tool("tool_" + str(i)) for i in range(MAX_EVENT_SOURCE_REFS + 10)]}
    result = pair({"tools": []}, body)
    event = events(result, "tool_added")[0]
    assert event["count"] == MAX_EVENT_SOURCE_REFS + 10
    assert len(event["source_refs"]) == MAX_EVENT_SOURCE_REFS
    assert event["source_ref_count"] == event["count"] and event["source_refs_complete"] is False
    assert not events(result, "tool_removed")


def test_structured_tool_result_blocks_and_function_outputs_are_classified():
    result = pair({"messages": []}, {"messages": [
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "done"}]},
        {"type": "function_call_output", "call_id": "y", "output": "result"},
    ]})
    assert events(result, "tool_results")[0]["count"] == 2


def test_cached_inventories_are_reused_and_inputs_and_outputs_are_detached(monkeypatch):
    import trajviz_obs.deveco_prompt_events as module
    previous = request({"tools": [tool("read")]}, rid="before", step=1)
    current = request({"tools": [tool("read"), tool("write")]})
    for value in (previous, current):
        value["component_inventory"] = build_component_inventory(value)
    original = copy.deepcopy([previous, current])
    def fail(*_):
        raise AssertionError("Complete cached inventories should be reused")
    monkeypatch.setattr(module, "build_component_inventory", fail)
    result = build_prompt_events(current, previous, baseline_status="available")
    result["events"][0]["source_refs"][0]["json_pointer"] = "/mutated"
    assert [previous, current] == original


def test_event_metadata_validates_against_the_installed_prompt_schema():
    from pathlib import Path

    import agent_trajviz
    import jsonschema
    schema = json.loads((Path(agent_trajviz.__file__).parent / "schemas" / "prompt_construction.schema.json").read_text())
    event_schema = schema["properties"]["data"]["properties"]["requests"]["items"]["properties"]["prompt_events"]
    snapshots = [request({}), request({"system": "rules", "tools": [tool("read")]}),
                 request({"system": "rules", "tools": [], "messages": []})]
    for current in snapshots:
        jsonschema.validate(build_prompt_events(current)["events"], event_schema)
        jsonschema.validate(build_prompt_events(current, request({}, rid="before", step=1),
                                                 baseline_status="available")["events"], event_schema)
