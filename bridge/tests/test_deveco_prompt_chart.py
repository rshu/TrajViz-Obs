"""DevEco chart coverage must not turn missing markers into missing content."""
from __future__ import annotations

import copy

import pytest
from agent_trajviz.insight.ui import prompt_evolution as core

from trajviz_obs.deveco_prompt_chart import evolution_chart


def row(index, *, session="main", actor="", events=None, coverage=None):
    return {"id": f"request-{index}", "label": f"request-{index}", "order": index, "step_index": index + 10,
            "session_id": session, "context_id": session, "actor_kind": "main" if session == "main" else "subagent",
            "actor_name": actor, "model": "fixture", "observed_prompt_tokens": None,
            "scope": "recorded_request", "prompt_events": events or [],
            "prompt_event_coverage": coverage or {"instructions": "compared", "tools": "compared",
                                                   "context_edits": "unavailable", "applicable": True},
            "composition": {"partial": False, "basis": "character_estimate", "component_count": 4,
                            "categories": [{"category": "system", "estimated_tokens": 10},
                                           {"category": "tools", "estimated_tokens": 5},
                                           {"category": "conversation", "estimated_tokens": index * 2}]}}


def data(*rows):
    return {"format": "deveco", "evolution": list(rows),
            "request_order": {"basis": "recorded_timestamp", "chronological": True},
            "composition_history": {"complete": True}}


def annotations(figure):
    return [item.text for item in figure.layout.annotations]


def trace(figure, name):
    return next(item for item in figure.data if item.name == name)


@pytest.mark.parametrize("format_name,coverage", [("opencode", True), ("deveco", False)])
@pytest.mark.parametrize("language", ["en", "zh"])
def test_other_formats_and_legacy_deveco_keep_exact_core_result(monkeypatch, format_name, coverage, language):
    marker = object()
    fixture = data(row(1))
    fixture["format"] = format_name
    if not coverage:
        fixture["evolution"][0].pop("prompt_event_coverage")
    calls = []
    monkeypatch.setattr(core, "evolution_chart", lambda *args: calls.append(args) or marker)
    assert evolution_chart(fixture, "request-1", language) is marker
    assert calls == [(fixture, "request-1", language)]


@pytest.mark.parametrize("language,expected", [
    ("en", ("No changes observed in compared requests", "Compaction events unavailable")),
    ("zh", ("已对比请求中未观察到变化", "压缩事件数据不可用")),
])
def test_empty_comparison_lanes_report_coverage_without_asserting_native_compaction_absence(language, expected):
    fixture = data(row(1, coverage={"instructions": "snapshot_only", "tools": "snapshot_only",
                                   "context_edits": "unavailable"}), row(2))
    figure = evolution_chart(fixture, "request-2", language)
    text = annotations(figure)
    assert text.count(expected[0]) == 2
    assert expected[1] in text
    assert core.translate("Not recorded", language) not in text
    assert not any("No compaction" in value for value in text)


def test_unavailable_or_partial_event_coverage_does_not_claim_zero_changes():
    fixture = data(row(1), row(2))
    fixture["evolution"][1]["prompt_event_coverage"] = None
    text = annotations(evolution_chart(fixture, "request-2"))
    assert text.count("Event timeline unavailable") == 2
    assert "No changes observed in compared requests" not in text


def test_auxiliary_requests_do_not_downgrade_applicable_comparison_coverage():
    fixture = data(row(1), row(2, coverage={"applicable": False}), row(3))
    text = annotations(evolution_chart(fixture, "request-3"))
    assert text.count("No changes observed in compared requests") == 2


@pytest.mark.parametrize("language,presence,source", [
    ("en", "Tool schemas present", "Observed in captured snapshot"),
    ("zh", "包含工具 Schema", "在捕获快照中观察到"),
])
def test_initial_presence_markers_are_labeled_snapshot_observations_not_additions(language, presence, source):
    events = [{"kind": "system_snapshot", "count": 1, "evidence": "observed_snapshot", "native_event": False},
              {"kind": "tool_snapshot", "count": 17, "evidence": "observed_snapshot", "native_event": False}]
    figure = evolution_chart(data(row(1, events=events)), "request-1", language)
    hover = trace(figure, core.translate("Tools", "zh-CN" if language == "zh" else language)).customdata[0][1]
    assert presence in hover and source in hover and "×17" in hover
    assert "Tool added" not in hover and "工具已添加" not in hover
    assert "Recorded context event" not in hover


def test_request_and_tool_hover_markers_distinguish_derived_differences_from_native_events():
    events = [{"kind": "tool_results", "count": 2, "evidence": "snapshot_comparison", "native_event": False,
               "source_refs": [{"step_index": 11}, {"step_index": 12}]},
              {"kind": "context_message", "count": 1, "evidence": "snapshot_comparison", "native_event": False}]
    fixture = data(row(1), row(2, events=events))
    figure = evolution_chart(fixture, "request-2")
    hover = trace(figure, "Tools").customdata[0][1]
    assert "Derived from request comparison" in hover and "#11, #12" in hover
    request_hover = trace(figure, "Request sequence").customdata[1][1]
    assert "Derived from request comparison" in request_hover
    assert "Native recorded event" not in hover + request_hover
    assert trace(figure, "Request sequence").customdata[1][0] == "request-2"


def test_native_compaction_retains_native_event_provenance():
    event = {"kind": "compaction", "evidence": "native_event", "native_event": True}
    figure = evolution_chart(data(row(1, events=[event], coverage={"instructions": "compared", "tools": "compared",
                                                                "context_edits": "native"})), "request-1")
    hover = trace(figure, "Context edits / compaction").customdata[0][1]
    assert "Native recorded event" in hover
    assert "Derived from request comparison" not in hover
    assert "Compaction events unavailable" not in annotations(figure)


def test_context_switches_have_distinct_markers_names_and_separators_without_changing_prompt_sizes():
    fixture = data(row(1), row(2, session="planner", actor="logic-context-builder"), row(3),
                   row(4, session="coder", actor="logic-coder"))
    untouched = copy.deepcopy(fixture)
    original = core.evolution_chart(fixture, "request-3")
    figure = evolution_chart(fixture, "request-3")
    assert fixture == untouched
    original_bars = [(item.name, tuple(item.x), tuple(item.y)) for item in original.data if item.type == "bar"]
    bars = [(item.name, tuple(item.x), tuple(item.y)) for item in figure.data if item.type == "bar"]
    assert bars == original_bars
    sequence = trace(figure, "Request sequence")
    assert sequence.marker.color[0] == sequence.marker.color[2]
    assert len(set(sequence.marker.color)) == 3
    switches = [shape for shape in figure.layout.shapes if shape.name == "deveco-context-switch"]
    assert [shape.x0 for shape in switches] == [1.5, 2.5, 3.5]
    assert len(figure.layout.meta["deveco_context_switches"]) == 3
    text = " ".join(annotations(figure))
    assert "logic-context-builder" in text and "logic-coder" in text
    assert "dotted separators mark switches" in text
    assert all("logic-co<br>" not in value for value in figure.layout.xaxis2.ticktext)
    assert figure.layout.uirevision == original.layout.uirevision
    assert figure.layout.xaxis2.title.text == "Captured request order"


def test_actor_names_are_escaped_and_duplicate_context_names_remain_distinct():
    fixture = data(row(1), row(2, session="one", actor="<script>same</script>"),
                   row(3, session="two", actor="<script>same</script>"))
    figure = evolution_chart(fixture, "request-3", "zh")
    text = " ".join(annotations(figure))
    assert "<script>" not in text and "&lt;script&gt;same&lt;/script&gt;" in text
    assert " · 2" in text
    assert "上下文切换" in text


@pytest.mark.parametrize("mapping_location", ["native_mapping", "evidence_quality"])
def test_later_verified_actor_identifies_a_context_whose_first_request_is_auxiliary(mapping_location):
    auxiliary = row(2, session="coder", coverage={"applicable": False})
    auxiliary["actor_kind"] = "unknown_child"
    identified = row(3, session="coder", actor="logic-coder")
    identified[mapping_location] = ({"status": "verified"} if mapping_location == "native_mapping"
                                    else {"native_mapping": {"status": "verified"}})
    fixture = data(row(1), auxiliary, identified)
    untouched = copy.deepcopy(fixture)
    figure = evolution_chart(fixture, "request-3")
    sequence = trace(figure, "Request sequence")
    assert "logic-coder" in sequence.customdata[1][1]
    assert "logic-coder" in sequence.customdata[2][1]
    assert sequence.marker.color[1] == sequence.marker.color[2]
    assert "Unidentified subagent" not in " ".join(annotations(figure))
    assert fixture == untouched


def test_chinese_alias_localizes_both_core_labels_and_deveco_enhancements():
    fixture = data(row(1), row(2, session="coder", actor="logic-coder"))
    alias = evolution_chart(fixture, "request-2", "zh")
    canonical = evolution_chart(fixture, "request-2", "zh-CN")
    assert alias.to_plotly_json() == canonical.to_plotly_json()
    assert trace(alias, core.translate("Request sequence", "zh-CN")) is not None
    assert alias.layout.xaxis2.title.text == "捕获请求顺序"
    assert "压缩事件数据不可用" in annotations(alias)
    assert "上下文切换" in " ".join(annotations(alias))
