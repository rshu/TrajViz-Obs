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
    requests = [attrs["data-obs-prompt-request"] for _, attrs in doc.elements if "data-obs-prompt-request" in attrs]
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
    buttons = [attrs for tag, attrs in doc.elements if tag == "button" and "data-obs-prompt-request" in attrs]
    assert len(buttons) <= MAX_TIMELINE_ITEMS
    assert any(attrs["data-obs-prompt-request"] == "r159" and attrs["aria-current"] == "step" for attrs in buttons)
    assert not any(attrs["data-obs-prompt-request"] == "other" for attrs in buttons)
    assert "of 160 snapshots" in doc.text


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
