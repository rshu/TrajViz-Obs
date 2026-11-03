"""Label sidecars and charts retain evidence availability without schema changes."""

import copy
import json

import agent_trajviz as tv
import jsonschema
import pytest

from trajviz_obs.compat import labeler_chat, labeler_run
from trajviz_obs.label_quality import install_chart, label_coverage


def _rows():
    return [{"index": 0, "role": "assistant", "phase": "understand", "action": "code_reading",
             "duration_s": None, "tokens_total": 0, "tokens_observed": False, "duration_observed": False},
            {"index": 1, "role": "assistant", "phase": "understand", "action": "code_reading",
             "duration_s": 0, "tokens_total": 0, "tokens_observed": True, "duration_observed": True}]


def test_session_label_serializer_keeps_unknown_and_measured_zero_distinct_without_model_or_global_leaks(tmp_path):
    from agent_trajviz.labeling import labeler, llm

    source, output = tmp_path / "source.json", tmp_path / "labels.json"
    source.write_text("{}")
    steps = [{"role": "assistant", "session_id": "captured-a", "text_preview": "Controlled reply",
              "duration": 0 if observed else None, "tokens": {"total": 0, "input": 0, "output": 0,
                  "reasoning": 0, "cache_read": 0, "cache_write": 0}, "_otel": {"kind": "model_request",
                  "fields": {"tokens.total": "observed" if observed else "unavailable",
                             "duration": "observed" if observed else "unavailable"}}}
             for observed in (False, True)]
    run = tv.Trajectory.from_steps(steps, format="otlp", source=str(source))
    before = copy.deepcopy(run.steps)
    original = (labeler.load_all_steps, labeler._serialize_step, llm.call_llm)
    calls = []
    with labeler_run(run), labeler_chat(lambda system, user: calls.append(user) or
            '{"phase":"understand","action":"code_reading"}'):
        result = labeler.label_trajectory(str(source), str(output), base_url="unused", api_key="unused", model="session")
    assert len(calls) == 2
    assert (labeler.load_all_steps, labeler._serialize_step, llm.call_llm) == original
    assert run.steps == before
    unknown, zero = result["steps"]
    assert unknown["tokens_total"] == zero["tokens_total"] == 0
    assert unknown["tokens_observed"] is False and zero["tokens_observed"] is True
    assert unknown["duration_observed"] is False and zero["duration_observed"] is True
    assert unknown["source_coverage"]["tokens.total"] == "unavailable"
    timeline = tv.label_views(output)["label_timeline"]
    assert timeline.data["steps"][0]["tokens_observed"] is False
    jsonschema.validate(timeline.to_dict(), tv.view_schema("label_timeline"))


def test_label_serializer_is_restored_after_an_interrupted_session(tmp_path):
    from agent_trajviz.labeling import labeler

    run = tv.Trajectory.from_steps([{"role": "assistant", "text_preview": "Controlled"}])
    original = labeler._serialize_step
    with pytest.raises(RuntimeError), labeler_run(run):
        raise RuntimeError("Controlled interruption")
    assert labeler._serialize_step is original


def test_label_timeline_hover_marks_unknown_while_observed_zero_remains_zero():
    from agent_trajviz.insight.charts import label_charts
    from agent_trajviz.insight.presenters import label_ui

    rows = _rows()
    before = copy.deepcopy(rows)
    install_chart()
    original = label_charts.build_label_timeline_chart
    install_chart()
    assert label_charts.build_label_timeline_chart is original and label_ui.build_label_timeline_chart is original
    figure = label_charts.build_label_timeline_chart(rows)
    assert "Tokens: unknown" in figure.data[0].hovertext[0]
    assert "Duration: unknown" in figure.data[0].hovertext[0]
    assert "Tokens: 0" in figure.data[0].hovertext[1] and "Duration: 0.0s" in figure.data[0].hovertext[1]
    assert rows == before


def test_existing_label_payload_adds_timing_coverage_notice_without_changing_label_counts(tmp_path):
    from agent_trajviz.insight.presenters import label_ui

    sidecar = tmp_path / "labels.json"
    document = {"taxonomy_version": "2", "steps": _rows()}
    sidecar.write_text(json.dumps(document))
    before = sidecar.read_bytes()
    install_chart()
    payload = label_ui.build_label_ui_payload(str(sidecar))
    assert "Partial timing" in payload["status_html"]
    for key in ("phase_duration_fig", "action_duration_fig"):
        assert any("observed durations only" in annotation.text for annotation in payload[key].layout.annotations)
    assert "Tokens: unknown" in payload["timeline_fig"].data[0].hovertext[0]
    assert tv.label_views(sidecar)["labels_summary"].data["classified"] == 2
    coverage = label_coverage(document)
    assert coverage["tokens.total"]["status"] == coverage["duration"]["status"] == "partial"
    assert sidecar.read_bytes() == before


@pytest.fixture
def labels_for_run(tmp_path):
    rows = [{**row, "session_id": "controlled-session", "raw_index": index + 5}
            for index, row in enumerate(_rows())]
    run = tv.Trajectory.from_steps([
        {"role": "assistant", "session_id": "controlled-session", "raw_index": index + 5,
         "text_preview": "Controlled labeled work"} for index in range(2)])
    document = {"model": "controlled<model>", "taxonomy_version": "2", "source_note": "Controlled label provenance",
                "steps": rows}
    sidecar = tmp_path / "labels.json"
    sidecar.write_text(json.dumps(document))
    return sidecar, document, run


def test_native_label_load_preserves_run_validation_provenance_and_all_charts(labels_for_run):
    from agent_trajviz import dashboard
    from agent_trajviz.insight.presenters import label_ui

    sidecar, _, run = labels_for_run
    before = sidecar.read_bytes(), copy.deepcopy(run.to_dict(include_raw=True))
    install_chart()
    payload = label_ui.build_label_ui_payload(str(sidecar), run=run)
    assert "controlled&lt;model&gt;" in payload["status_html"]
    assert "Controlled label provenance" in payload["status_html"]
    assert "Partial timing" in payload["status_html"]
    assert all(payload[key].data for key in ("phase_count_fig", "action_count_fig", "phase_duration_fig",
                                            "action_duration_fig", "timeline_fig"))
    app = dashboard.insight_app()
    load = next(fn.fn for fn in app.fns.values() if fn.api_name == "do_load_labels")
    result = load(str(sidecar), False, run, "")
    assert "Labels loaded" in result[0] and "Label provenance" in result[1]
    assert "could not be loaded" not in str(result[:2])
    assert all(result[index].data for index in (3, 4, 6, 7, 9))
    assert all(result[index]["visible"] is True for index in (2, 5, 8, 10))
    assert "Tokens: unknown" in result[9].data[0].hovertext[0]
    assert "Tokens: 0" in result[9].data[0].hovertext[1]
    assert (sidecar.read_bytes(), run.to_dict(include_raw=True)) == before


@pytest.mark.parametrize(("change", "message"), [
    ({"session_id": "other-session"}, "different captured session"),
    ({"index": 9}, "index absent"),
    ({"raw_index": 99}, "source indices"),
    ({"role": "user"}, "roles do not match"),
])
def test_native_label_load_refuses_conflicting_run_identity(labels_for_run, change, message):
    from agent_trajviz import dashboard
    from agent_trajviz.insight.presenters import label_ui

    sidecar, document, run = labels_for_run
    document["steps"][0].update(change)
    sidecar.write_text(json.dumps(document))
    before = sidecar.read_bytes()
    install_chart()
    with pytest.raises(ValueError, match=message):
        label_ui.build_label_ui_payload(str(sidecar), run=run)
    app = dashboard.insight_app()
    load = next(fn.fn for fn in app.fns.values() if fn.api_name == "do_load_labels")
    result = load(str(sidecar), False, run, "")
    assert "Labels could not be loaded" in result[0] and message in result[0]
    assert not any(result[index].data for index in (3, 4, 6, 7, 9))
    assert all(result[index]["visible"] is False for index in (2, 5, 8))
    assert sidecar.read_bytes() == before


def test_changed_label_payload_contract_is_refused(monkeypatch):
    from agent_trajviz.insight.presenters import label_ui

    from trajviz_obs.label_quality import _install_payload

    monkeypatch.setattr(label_ui, "build_label_ui_payload", lambda file_path, dark=False: {})
    with pytest.raises(RuntimeError, match="label payload changed"):
        _install_payload()


def test_unannotated_legacy_labels_keep_their_original_behavior():
    assert label_coverage({"steps": [{"role": "assistant", "phase": "understand", "action": "code_reading"}]}) is None
