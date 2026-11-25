"""Prompt exploration renders escaped evidence and does not invent comparisons."""

from __future__ import annotations

import hashlib
import json
from html.parser import HTMLParser

import pytest

from trajviz_obs.prompt_explorer_render import (
    EXPLORER_CSS,
    MAX_TIMELINE_ITEMS,
    MAX_VISIBLE_COMPONENTS,
    component_token,
    format_bytes,
    render_explorer,
)


class Document(HTMLParser):
    def __init__(self, value):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.content = []
        self.feed(value)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.content.append(data)

    @property
    def text(self):
        return "".join(self.content)


def component(identity, content, *, kind="conversation", position=0):
    encoded = json.dumps(content, ensure_ascii=False).encode()
    return {"id": identity, "content": content, "kind": kind, "position": position,
            "sha256": hashlib.sha256(encoded).hexdigest(), "utf8_bytes": len(encoded)}


def record(identity, rows=None, **fields):
    return {"selection_id": identity, "record_id": identity, "lane_id": "one", "adapter": "pi",
            "session_id": "session", "instance_id": "instance", "stage": "context", "kind": "primary",
            "observed_at": "2026-11-25T01:02:03Z", "correlation": {"lineage_id": "lineage"},
            "components": rows if rows is not None else [component("message:user:1", "prompt")],
            "changes": {"added": [], "removed": [], "modified": [], "moved": [], "unchanged": []},
            "comparison_status": "initial_baseline", "baseline_record_id": None, **fields}


def dataset(*records, **fields):
    return {"status": "ready", "records": list(records), "capture_gaps": [], **fields}


@pytest.mark.parametrize("status,title", [("disabled", "Prompt capture is off"), ("empty", "No prompt snapshots yet"),
                                          ("unavailable", "Prompt capture is unavailable"), ("limit", "Capture limit reached")])
def test_empty_states_have_clear_read_only_guidance(status, title):
    doc = Document(render_explorer({"status": status, "message": "A useful reason", "records": []}))
    assert title in doc.text and "A useful reason" in doc.text
    assert "Separate from usage telemetry" in doc.text
    assert not any(tag in {"input", "textarea", "form", "script", "iframe"} for tag, _ in doc.elements)


def test_initial_snapshot_has_composition_without_fake_added_changes():
    rows = [component("system:section:rules", "Rule", kind="system_section"),
            component("tool:read", {"name": "read", "description": "Reads"}, kind="tool_definition", position=1)]
    doc = Document(render_explorer(dataset(record("r1", rows, changes={"added": [row["id"] for row in rows]})), "r1"))
    assert "System sections" in doc.text and "Tool definitions" in doc.text
    assert "Serialized UTF-8 bytes" in doc.text
    assert "not token counts" in doc.text
    assert "No comparable baseline" in doc.text
    assert "2 added" not in doc.text
    assert any(tag == "button" and attrs.get("data-obs-prompt-component") == component_token("system:section:rules") for tag, attrs in doc.elements)


def test_verified_changes_include_added_removed_modified_and_moved_components():
    before = record("before", [component("a", "old"), component("gone", "removed", position=1)])
    after = record("after", [component("a", "new", position=1), component("new", "added")],
                   baseline_record_id="before", baseline_selection_id="before", comparison_status="compared",
                   changes={"added": ["new"], "removed": ["gone"], "modified": ["a"], "moved": ["a"], "unchanged": []})
    doc = Document(render_explorer(dataset(before, after), "after", component_token("a")))
    assert all(value in doc.text for value in ["1 added", "1 changed", "1 removed", "1 moved", "Before", "After", "-old", "+new"])
    assert "Changes from baseline" in doc.text
    removed = Document(render_explorer(dataset(before, after), "after", component_token("gone")))
    assert "-removed" in removed.text and "Not present in this snapshot" in removed.text
    assert any(attrs.get("data-obs-prompt-component") == component_token("gone") for _, attrs in removed.elements)


def test_missing_or_cross_lane_baseline_does_not_invent_a_diff():
    before = record("before", lane_id="other")
    after = record("after", baseline_record_id="before", baseline_selection_id="before", comparison_status="compared",
                   changes={"modified": ["message:user:1"]})
    for records in [(after,), (before, after)]:
        doc = Document(render_explorer(dataset(*records), "after"))
        assert "No comparable baseline" in doc.text
        assert "Changes from baseline" not in doc.text
        assert "Content unchanged" not in doc.text


def test_provider_delta_and_incomplete_payload_are_not_empty_context_claims():
    current = record("delta", stage="provider.request", remote_context="retained_not_reconstructed",
                     components=[], omitted_reason="size_limit", comparison_status="capture_incomplete", changes=None)
    doc = Document(render_explorer(dataset(current), "delta"))
    assert "Provider request parameters" in doc.text
    assert "Earlier context is retained" in doc.text
    assert "Component removals and an empty prompt cannot be inferred" in doc.text
    assert "No prompt components were exposed" in doc.text


def test_incomplete_observations_remain_visible_in_the_evolution_lane():
    before = record("before", stage="provider.request")
    omitted = record("omitted", stage="provider.request", components=[],
                     omitted_reason="size_limit", comparison_status="capture_incomplete")
    after = record("after", stage="provider.request")
    doc = Document(render_explorer(dataset(before, omitted, after), "after"))
    requests = [attrs["data-obs-prompt-request"] for _, attrs in doc.elements if "pe-timeline-item" in attrs.get("class", "")]
    assert requests == ["before", "omitted", "after"]
    assert "Capture incomplete" in doc.text
    assert "3 snapshots in this lane" in doc.text


def test_normalized_claude_context_does_not_claim_to_be_the_complete_model_input():
    doc = Document(render_explorer(dataset(record("r1", adapter="claude-code"))))
    assert "System instructions and tool declarations may be exposed separately" in doc.text
    assert "not a reconstructed full API request" in doc.text


def test_pi_patch_view_points_to_the_retained_system_and_tool_state():
    doc = Document(render_explorer(dataset(record("r1", effective_state=[]))))
    assert "Pi can record system and tool patches" in doc.text
    assert "inspect the retained instructions and tool declarations" in doc.text
    assert "0 B" not in doc.text


def test_unlinked_component_observations_remain_uncompared():
    current = record("component", [component("component.section", "observed", kind="component_observation")],
                     stage="component.section", comparison_status="unlinked_component", changes=None)
    doc = Document(render_explorer(dataset(current), "component"))
    assert "Unlinked component observation" in doc.text
    assert "request association is not proven" in doc.text
    assert "no request-to-request comparison" in doc.text


def test_pi_effective_state_uses_active_sections_and_tools_without_double_counting_conversation():
    current = record("pi", [component("message:user", "conversation")],
                     effective_state=[component("system:section:rules", "Effective rule", kind="system_section")],
                     effective_state_changes={"added": ["system:section:rules"]})
    doc = Document(render_explorer(dataset(current), "pi", mode="effective"))
    assert "Active Pi system sections and tool declarations" in doc.text
    assert "Effective rule" in doc.text
    assert "conversation" not in doc.text
    assert "System sections" in doc.text
    assert "1 added" not in doc.text


def test_captured_null_is_distinct_from_unavailable_content():
    current = record("null", [component("value", None)])
    doc = Document(render_explorer(dataset(current), "null"))
    assert "null" in doc.text
    assert "Content was not retained" not in doc.text
    current["components"][0].pop("content")
    doc = Document(render_explorer(dataset(current), "null"))
    assert "Content was not retained" in doc.text


def test_prompt_source_diff_and_labels_are_escaped_without_active_html():
    attack = '</pre></details><img src=x onerror="alert(1)"><script>alert(2)</script>'
    old = component('tool:"<bad>', "old " + attack, kind="tool_definition")
    new = component('tool:"<bad>', {"name": attack, "description": "new " + attack}, kind="tool_definition")
    new["source"] = {"path": attack}
    before = record("before", [old])
    after = record("after", [new], baseline_selection_id="before", baseline_record_id="before", comparison_status="compared",
                   changes={"modified": [new["id"]]})
    rendered = render_explorer(dataset(before, after), "after")
    doc = Document(rendered)
    assert not any(tag in {"script", "img", "iframe", "a", "form", "input", "textarea"} for tag, _ in doc.elements)
    assert not any(key.startswith("on") for _, attrs in doc.elements for key in attrs)
    assert attack in doc.text
    assert 'data-obs-prompt-component="' + component_token(new["id"]) + '"' in rendered
    assert "&lt;script&gt;" in rendered and "<script>" not in rendered
    assert "<style>" not in rendered


def test_large_content_and_many_components_are_bounded_but_selected_component_stays_reachable():
    rows = [component(f"item:{index}", "x" * 100, position=index) for index in range(400)]
    rows[-1]["content"] = "\n".join(str(index) + "x" * 200 for index in range(1000))
    current = record("large", rows)
    rendered = render_explorer(dataset(current), "large", component_token("item:399"))
    doc = Document(rendered)
    buttons = [attrs for tag, attrs in doc.elements if tag == "button" and "data-obs-prompt-component" in attrs]
    assert len(buttons) <= MAX_VISIBLE_COMPONENTS
    assert any(attrs["data-obs-prompt-component"] == component_token("item:399") and attrs["aria-pressed"] == "true" for attrs in buttons)
    assert "Preview limited to 24,000 characters" in doc.text
    assert len(rendered) < 160_000


def test_timeline_is_scoped_and_windowed_without_leaking_other_lanes():
    records = [record(f"r{index}") for index in range(160)]
    records.append(record("other", lane_id="other", adapter="claude-code"))
    doc = Document(render_explorer(dataset(*records), "r159"))
    buttons = [attrs for tag, attrs in doc.elements if tag == "button" and "pe-timeline-item" in attrs.get("class", "")]
    assert len(buttons) <= MAX_TIMELINE_ITEMS
    assert any(attrs["data-obs-prompt-request"] == "r159" and attrs["aria-current"] == "step" for attrs in buttons)
    assert not any(attrs["data-obs-prompt-request"] == "other" for attrs in buttons)
    assert "of 160 snapshots" in doc.text


def test_snapshot_navigation_moves_beyond_the_visible_window_and_has_native_disabled_edges():
    records = [record(f"r{index}") for index in range(160)]
    for selected, previous, following in [("r0", None, "r1"), ("r100", "r99", "r101"), ("r159", "r158", None)]:
        doc = Document(render_explorer(dataset(*records), selected))
        buttons = [attrs for tag, attrs in doc.elements if tag == "button" and "pe-nav-button" in attrs.get("class", "")]
        assert len(buttons) == 2
        for button, expected in zip(buttons, [previous, following], strict=True):
            if expected is None:
                assert "disabled" in button and "data-obs-prompt-request" not in button
            else:
                assert button["data-obs-prompt-request"] == expected and "disabled" not in button
        assert "Previous snapshot" in doc.text and "Next snapshot" in doc.text


def test_changed_only_filters_content_and_position_changes_and_keeps_removed_evidence():
    before = record("before", [component("same", "SAME_ONLY"), component("modified", "old", position=1),
                               component("removed", "deleted", position=2), component("shifted", "stays", position=3)])
    after = record("after", [component("same", "SAME_ONLY"), component("modified", "new", position=1),
                             component("added", "added", position=2), component("shifted", "stays", position=4)],
                   baseline_selection_id="before", baseline_record_id="before", comparison_status="compared",
                   changes={"added": ["added"], "modified": ["modified"], "removed": ["removed"], "moved": ["shifted"], "unchanged": ["same", "shifted"]})
    doc = Document(render_explorer(dataset(before, after), "after", component_token("same"), changes_only=True))
    ids = [attrs["data-obs-prompt-component"] for _, attrs in doc.elements if "data-obs-prompt-component" in attrs]
    assert set(ids) == {component_token(value) for value in ["modified", "removed", "added", "shifted"]}
    assert "SAME_ONLY" not in doc.text
    assert "Changed components" in doc.text
    moved = Document(render_explorer(dataset(before, after), "after", component_token("shifted"), changes_only=True))
    assert "Position shifted from 4 to 5" in moved.text
    assert "Content same" in moved.text
    assert "Moved counts position shifts separately" in moved.text


def test_changed_only_empty_result_is_not_a_missing_capture_or_false_removal():
    before = record("before")
    after = record("after", baseline_selection_id="before", baseline_record_id="before", comparison_status="compared",
                   changes={"unchanged": ["message:user:1"]})
    doc = Document(render_explorer(dataset(before, after), "after", changes_only=True))
    assert "No changed components in this snapshot" in doc.text
    assert not any("data-obs-prompt-component" in attrs for _, attrs in doc.elements)
    assert "Composition" in doc.text


@pytest.mark.parametrize("status", ["initial_baseline", "unlinked_component", "capture_incomplete", "page_boundary"])
def test_changed_only_keeps_uncomparable_evidence_visible(status):
    current = record("current", comparison_status=status)
    doc = Document(render_explorer(dataset(current), "current", changes_only=True))
    assert "Available captured components remain visible" in doc.text
    assert any("data-obs-prompt-component" in attrs for _, attrs in doc.elements)
    assert "No changed components" not in doc.text


@pytest.mark.parametrize("status", ["unlinked_component", "capture_incomplete", "page_boundary"])
def test_incomplete_or_unlinked_comparisons_are_rejected_even_with_a_resolvable_baseline(status):
    before = record("before")
    current = record("current", comparison_status=status, baseline_selection_id="before",
                     changes={"removed": ["message:user:1"]})
    doc = Document(render_explorer(dataset(before, current), "current", changes_only=True))
    assert "Available captured components remain visible" in doc.text
    assert "Changes from verified baseline" not in doc.text
    assert "Changes from baseline" not in doc.text


def test_changed_only_uses_the_selected_effective_pi_state_instead_of_physical_patch_changes():
    before = record("before", effective_state=[component("section:retained", "old", kind="system_section")])
    current = record("current", effective_state=[component("section:retained", "new", kind="system_section")],
                     baseline_selection_id="before", comparison_status="compared",
                     changes={"unchanged": ["message:user:1"]}, effective_state_changes={"modified": ["section:retained"]})
    doc = Document(render_explorer(dataset(before, current), "current", mode="effective", changes_only=True))
    ids = [attrs["data-obs-prompt-component"] for _, attrs in doc.elements if "data-obs-prompt-component" in attrs]
    assert ids == [component_token("section:retained")]
    assert "Replayed native state" in doc.text and "Changes from baseline" in doc.text


def test_page_summaries_explain_loaded_boundaries_without_rendering_cursors_or_paths():
    page = {"observations": 80, "known_observations": 1500, "has_older": True, "has_newer": True,
            "omitted_oversized": 2, "comparison_boundary": "visible_page_only", "current_cursor": "PRIVATE_CURSOR",
            "older_cursor": "/private/capture/path", "newer_cursor": '<img src=x onerror="bad">'}
    doc = Document(render_explorer(dataset(record("boundary", comparison_status="page_boundary"), page=page), "boundary"))
    assert "80 observations loaded" in doc.text
    assert "1,500 known" in doc.text
    assert "Earlier captures available" in doc.text and "Later captures available" in doc.text
    assert "do not bridge an unloaded page boundary" in doc.text
    assert "2 oversized observation bodies" in doc.text
    assert "Page boundary" in doc.text
    assert "PRIVATE_CURSOR" not in doc.text and "/private/capture/path" not in doc.text
    assert not any(tag == "img" for tag, _ in doc.elements)


def test_component_evidence_does_not_invent_file_or_rule_attribution():
    current = record("current", [component("system:section:custom", "instructions", kind="system_section")])
    doc = Document(render_explorer(dataset(current), "current"))
    assert "Captured component" in doc.text and "Source not attributed" in doc.text
    assert "Tokens unmeasured" in doc.text
    current["effective_state"] = current["components"]
    effective = Document(render_explorer(dataset(current), "current", mode="effective"))
    assert "Replayed native state" in effective.text
    assert "Project rule" not in effective.text


@pytest.mark.parametrize("role", ["toolResult", "tool_result"])
def test_tool_result_roles_have_readable_titles(role):
    result = {**component("result", "ok"), "role": role}
    doc = Document(render_explorer(dataset(record("current", [result])), "current"))
    assert "Tool result message" in doc.text


def test_expired_window_notice_is_fixed_text_and_cannot_expose_history_values():
    doc = Document(render_explorer(dataset(record("current"), history_reset=True), "current"))
    assert "Session history changed or the saved window expired. Showing newest captures." in doc.text
    unchanged = Document(render_explorer(dataset(record("current"), history_reset="PRIVATE_CURSOR"), "current"))
    assert "Showing newest captures" not in unchanged.text and "PRIVATE_CURSOR" not in unchanged.text


def test_page_budget_omission_and_control_frame_labels_do_not_claim_capture_or_context_loss():
    current = record("large", components=[], omitted_reason="record_exceeds_page_budget", comparison_status="capture_incomplete")
    doc = Document(render_explorer(dataset(current), "large"))
    assert "Body not loaded" in doc.text and "retained capture" in doc.text
    assert "This capture is incomplete" not in doc.text
    current["omitted_reason"] = "no_prompt_components_exposed"
    control = Document(render_explorer(dataset(current), "large"))
    assert "may be a control frame" in control.text
    assert "does not establish an empty prompt" in control.text


def test_unknown_sizes_do_not_become_measured_zero_or_token_estimates():
    current = record("unknown", [component("known", "x"), {"id": "unknown", "kind": "opaque", "content": "unknown", "position": 1}])
    doc = Document(render_explorer(dataset(current), "unknown", component_token("unknown")))
    assert "1 component size unknown" in doc.text
    assert "Size unknown" in doc.text
    assert "Not measured per component" in doc.text


def test_long_source_previews_are_explicitly_limited():
    current = record("source")
    current["components"][0]["source"] = "source" * 1000
    doc = Document(render_explorer(dataset(current), "source"))
    assert "source preview limited" in doc.text
    assert "source" * 1000 not in doc.text


@pytest.mark.parametrize("value,expected", [(None, "Size unknown"), (0, "0 B"), (1024, "1.0 KiB"), (1048576, "1.00 MiB")])
def test_byte_units_are_explicit(value, expected):
    assert format_bytes(value) == expected


def test_css_uses_dashboard_theme_tokens_and_reflows_narrow_layouts():
    assert "var(--ov-text" in EXPLORER_CSS and "var(--ov-bad" in EXPLORER_CSS
    assert "@media (max-width:560px)" in EXPLORER_CSS
    assert "grid-template-columns:minmax(0,1fr)" in EXPLORER_CSS
    assert "url(" not in EXPLORER_CSS and "@import" not in EXPLORER_CSS
