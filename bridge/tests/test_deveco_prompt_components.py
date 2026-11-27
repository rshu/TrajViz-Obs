"""Exact component content, conservative identity, and per-request differences."""
from __future__ import annotations

import copy
import hashlib
import json

from trajviz_obs.deveco_prompt_components import build_component_inventory, compare_component_inventory


def detail(body, rid="current", **changes):
    value = {"id": rid, "request_id": rid, "request_body": body, "session_id": "root",
             "coverage": {"complete": True}, **changes}
    value["component_inventory"] = build_component_inventory(value)
    return value


def compare(old, new):
    return compare_component_inventory(detail(new), detail(old, "baseline"), basis="test_body_pair")


def resolve(body, pointer):
    value = body
    for field in pointer.split("/")[1:]:
        field = field.replace("~1", "/").replace("~0", "~")
        value = value[int(field)] if isinstance(value, list) else value[field]
    return value


def test_inventory_retains_exact_values_paths_roles_schemas_and_unknown_parameters():
    body = {"system": [{"type": "text", "text": "技能：迁移"}], "instructions": "No truncation",
            "messages": [{"role": "developer", "content": "Dev rule"},
                         {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:test"}}]},
                         {"role": "tool", "tool_call_id": "call1", "content": "Full result"}],
            "tools": [{"type": "function", "function": {"name": "read", "description": "Read a file",
                       "parameters": {"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}}}}],
            "model": "x", "vendor/~field": {"nested": [True, None, 1.5]}}
    original = copy.deepcopy(body)
    inventory = detail(body)["component_inventory"]
    assert [row["group"] for row in inventory] == ["system", "system", "system", "messages", "messages", "tools", "parameters", "parameters"]
    assert len({row["id"] for row in inventory}) == len(inventory)
    for row in inventory:
        assert row["content"] == resolve(body, row["json_pointer"])
        canonical = json.dumps(row["content"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        assert row["utf8_bytes"] == len(canonical)
        assert row["content_sha256"] == hashlib.sha256(canonical).hexdigest()
        assert row["size_basis"] == "canonical_json_utf8"
    assert inventory[-1]["json_pointer"] == "/vendor~1~0field"
    assert inventory[2]["role"] == "developer"
    assert inventory[5]["content"]["function"]["parameters"]["required"] == ["path"]
    inventory[5]["content"]["function"]["parameters"]["required"].append("untrusted")
    assert body == original


def test_input_string_and_empty_collections_are_not_lost():
    body = {"input": "Actual user input", "tools": [], "system": [], "messages": [], "stream": False}
    rows = detail(body)["component_inventory"]
    assert {row["json_pointer"]: row["content"] for row in rows} == {"/" + key: value for key, value in body.items()}


def test_parameter_changes_are_updates_and_unknown_fields_remain_inspectable():
    delta = compare({"temperature": 0, "model": "x", "extra": {"depth": 1}},
                    {"temperature": 0.2, "model": "x", "extra": {"depth": 2}})
    assert delta["counts"] == {"added": 0, "removed": 0, "updated": 2, "moved": 0, "unchanged": 1}
    row = next(row for row in delta["rows"] if row["after"]["label"] == "extra")
    assert row["before"]["content"] == {"depth": 1} and row["after"]["content"] == {"depth": 2}


def test_named_tool_schema_update_is_not_a_replaced_tool():
    tool = lambda schema: {"type": "function", "function": {"name": "read", "parameters": schema}}
    delta = compare({"tools": [tool({"type": "object"})]},
                    {"tools": [tool({"type": "object", "required": ["path"]})]})
    assert delta["counts"]["updated"] == 1
    assert delta["counts"]["added"] == delta["counts"]["removed"] == 0
    assert delta["rows"][0]["match_basis"] == "stable_identity"


def test_insertion_shifts_json_paths_without_inventing_updates_or_moves():
    message = lambda text: {"role": "user", "content": text}
    old = {"messages": [message("First"), message("Second")]}
    new = {"messages": [message("Inserted"), *old["messages"]]}
    delta = compare(old, new)
    assert delta["counts"] == {"added": 1, "removed": 0, "updated": 0, "moved": 0, "unchanged": 2}
    same = next(row for row in delta["rows"] if row["after"]["content"]["content"] == "Second")
    assert same["before"]["json_pointer"] == "/messages/1"
    assert same["after"]["json_pointer"] == "/messages/2"


def test_stable_message_id_allows_exact_updated_message_diff():
    old = {"messages": [{"id": "same", "role": "user", "content": "old"}]}
    new = {"messages": [{"id": "same", "role": "user", "content": "new"}]}
    delta = compare(old, new)
    assert delta["counts"]["updated"] == 1
    assert delta["rows"][0]["before"]["content"]["content"] == "old"
    assert delta["rows"][0]["after"]["content"]["content"] == "new"


def test_anonymous_changed_message_is_add_and_remove_not_guessed_update():
    old = {"messages": [{"role": "user", "content": "old"}]}
    new = {"messages": [{"role": "user", "content": "new"}]}
    delta = compare(old, new)
    assert delta["counts"]["updated"] == 0
    assert delta["counts"]["removed"] == delta["counts"]["added"] == 1
    assert next(row for row in delta["rows"] if row["status"] == "removed")["before"]["content"]["content"] == "old"


def test_duplicate_message_occurrences_are_preserved_without_false_updates():
    a = {"role": "assistant", "content": "repeat"}
    b = {"role": "user", "content": "new"}
    delta = compare({"messages": [a, a]}, {"messages": [a, b, a]})
    assert delta["counts"] == {"added": 1, "removed": 0, "updated": 0, "moved": 0, "unchanged": 2}
    assert len(delta["rows"]) == 3
    assert len({row["id"] for row in delta["rows"]}) == 3


def test_duplicate_native_ids_do_not_guess_an_update():
    old = {"messages": [{"id": "repeat", "content": "A"}, {"id": "repeat", "content": "B"}]}
    new = {"messages": [{"id": "repeat", "content": "A"}, {"id": "repeat", "content": "C"}]}
    delta = compare(old, new)
    assert delta["counts"]["updated"] == 0 and delta["counts"]["unchanged"] == 1
    assert delta["counts"]["added"] == delta["counts"]["removed"] == 1


def test_relative_reordering_marks_move_without_treating_content_as_updated():
    messages = [{"id": value, "role": "user", "content": value} for value in ["a", "b", "c"]]
    delta = compare({"messages": messages}, {"messages": [messages[2], *messages[:2]]})
    assert delta["counts"]["moved"] == 1
    assert delta["counts"]["updated"] == 0
    assert delta["counts"]["unchanged"] == 3
    moved = next(row for row in delta["rows"] if row["status"] == "moved")
    assert moved["after"]["content"]["id"] == "c"


def test_missing_or_incomplete_baseline_is_unknown_not_removed():
    current = detail({"messages": [{"content": "still present"}]})
    for previous in [None, detail({}, "old", coverage={"complete": False}), detail({}, "old", session_id="foreign")]:
        delta = compare_component_inventory(current, previous)
        assert delta["available"] is False and set(delta["counts"].values()) == {0}
        assert [row["status"] for row in delta["rows"]] == ["uncompared"]
        assert delta["baseline_request_id"] is None


def test_request_integration_exposes_catalog_without_private_content_or_fake_lineage(tmp_path):
    from test_deveco_requests import captured, two_step_run, view
    run = two_step_run(tmp_path)
    first = {"system": "Rules", "model": "x", "messages": [{"role": "user", "content": "Base"}]}
    second = {**first, "messages": [*first["messages"], {"role": "user", "content": "Feedback"}]}
    root, before, *_ = captured(tmp_path, aid="a", body=first)
    _, after, *_ = captured(tmp_path, aid="b", body=second)
    result = view(run, root, after["request_id"])
    selected = result["selected_request"]
    delta = selected["component_delta"]
    assert delta["available"] is True and delta["counts"]["added"] == 1
    assert delta["baseline_request_id"] == before["request_id"]
    assert delta["baseline_assistant_message_id"] == "a" and delta["baseline_session_id"] == "root"
    assert delta["basis"] == "previous_verified_native_assistant_request"
    assert delta["lineage_verified"] is False
    assert {row["id"] for row in selected["request_catalog"]} == {before["request_id"], after["request_id"]}
    assert all("content" not in row and "request_body" not in row for row in selected["request_catalog"])
    assert "component_inventory" not in result["requests"][0]
    assert len(selected["component_inventory"]) == 4
    assert all("actor_kind" in row and "native_mapping" in row for row in selected["request_catalog"])


def test_equal_parameter_values_do_not_match_different_fields():
    delta = compare({"temperature": 0.5}, {"top_p": 0.5})
    assert delta["counts"] == {"added": 1, "removed": 1, "updated": 0, "moved": 0, "unchanged": 0}
    assert {row["status"] for row in delta["rows"]} == {"added", "removed"}


def test_large_duplicate_histories_use_bounded_alignment_without_collapsing_occurrences():
    message = {"role": "assistant", "content": "repeat"}
    old = {"messages": [message] * 1100}
    new = {"messages": [message] * 1101}
    delta = compare(old, new)
    assert delta["counts"]["unchanged"] == 1100
    assert delta["counts"]["added"] == 1
    assert delta["counts"]["removed"] == delta["counts"]["updated"] == delta["counts"]["moved"] == 0


def test_large_flat_inventory_is_bounded_and_never_compared_as_a_complete_prompt():
    from trajviz_obs.deveco_prompt_components import MAX_INVENTORY_COMPONENTS, component_inventory_coverage
    body = {f"setting_{index}": 0 for index in range(100_000)}
    current = detail(body)
    coverage = component_inventory_coverage(current)
    assert len(current["component_inventory"]) == MAX_INVENTORY_COMPONENTS
    assert coverage == {"complete": False, "limit": MAX_INVENTORY_COMPONENTS,
                        "total_components": 100_000, "loaded_components": MAX_INVENTORY_COMPONENTS,
                        "raw_request_available": True, "size_basis": "canonical_json_utf8"}
    assert current["request_body"]["setting_99999"] == 0
    delta = compare_component_inventory(current, detail({"removed_setting": 1}, "old"))
    assert delta["available"] is False and set(delta["counts"].values()) == {0}
    assert len(delta["rows"]) == MAX_INVENTORY_COMPONENTS
    assert {row["status"] for row in delta["rows"]} == {"uncompared"}
    assert "structural limit" in delta["reason"]
    # A complete current inventory must not infer massive removals from a
    # structurally partial previous one either.
    reverse = compare_component_inventory(detail({"model": "x"}), current)
    assert reverse["available"] is False and reverse["counts"]["removed"] == 0


def test_large_message_array_stops_materializing_before_remaining_fields():
    from trajviz_obs.deveco_prompt_components import MAX_INVENTORY_COMPONENTS, component_inventory_coverage
    body = {"messages": [{"role": "user", "content": "repeat"}] * (MAX_INVENTORY_COMPONENTS + 10), "model": "x"}
    current = detail(body)
    assert len(current["component_inventory"]) == MAX_INVENTORY_COMPONENTS
    assert current["component_inventory"][-1]["json_pointer"] == f"/messages/{MAX_INVENTORY_COMPONENTS - 1}"
    assert component_inventory_coverage(current)["total_components"] == MAX_INVENTORY_COMPONENTS + 11
    assert component_inventory_coverage(current)["complete"] is False
    assert current["request_body"]["model"] == "x"
