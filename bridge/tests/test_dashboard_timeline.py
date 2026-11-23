"""Regression coverage for the dashboard's real and compressed timeline axes."""

import json

import pytest

from trajviz_obs.dashboard_cmd import build_preloaded_insight
from trajviz_obs.dashboard_timeline import timeline_builder


def _trace(fig, name):
    return next(trace for trace in fig.data if trace.name == name)


def test_prompt_marker_keeps_previous_gap_compression_on_dashboard_load_and_toggle(tmp_path):
    base = 1_790_000_000_000

    def assistant(message_id, start, end):
        return {
            "id": message_id, "type": "assistant", "agent": "build",
            "model": {"id": "controlled"},
            "time": {"created": base + start, "completed": base + end},
            "content": [{"type": "text", "text": "Recorded assistant reply"}],
        }

    source = tmp_path / "timed-opencode.json"
    source.write_text(json.dumps({
        "source_format": "opencode_v2", "schema_version": 1,
        "session": {"id": "timed", "time": {"created": base, "updated": base + 206_000}},
        "messages": [
            assistant("first", 0, 2_000),
            assistant("second", 102_000, 104_000),
            {"id": "prompt", "type": "user", "time": {"created": base + 202_000},
             "text": "Recorded user prompt"},
            assistant("third", 204_000, 206_000),
        ],
    }))
    app = build_preloaded_insight(str(source))
    loader = next(entry for entry in app.fns.values() if entry.api_name == "_do_load")
    axis = next(entry for entry in app.fns.values() if entry.api_name == "on_gantt_axis_change")
    loaded = loader.fn(str(source), False, "")
    steps = loaded[axis.inputs[1]]
    for fig in (loaded[axis.outputs[0]], axis.fn("skip_idle", steps, False)):
        waits = [trace for trace in fig.data if trace.name == "User wait (idle)"]
        marker = _trace(fig, "User message")
        assert fig.layout.title.text == "Execution Timeline (compressed)"
        assert fig.layout.meta["tv_i18n"]["layout"]["title.text"] == fig.layout.title.text
        assert len(waits) == 2
        assert waits[1].base[0] < marker.x[0] < waits[1].base[0] + waits[1].x[0]
        assert marker.x[0] == pytest.approx(33.7)
        assert "before user message" in waits[1].hovertext
        assert "before user message" not in waits[0].hovertext
        assert "+202.0s" in marker.hovertext[0]
        assert _trace(fig, "Step (thinking)").hovertext[0].endswith("Tokens unavailable")

    wall = axis.fn("wallclock", steps, False)
    assert wall.layout.title.text == "Execution Timeline (wall-clock)"
    assert wall.layout.xaxis.tickvals is None
    assert _trace(wall, "User message").x == (202.0,)
    assert _trace(wall, "User wait (idle)").x == (100.0,)


def test_prompt_pause_caps_at_fifteen_seconds_even_in_a_busy_session():
    steps = [
        {"role": "user", "time_created_ms": 10_000},
        {"role": "assistant", "time_created_ms": 10_007, "time_completed_ms": 871_346},
        {"role": "user", "time_created_ms": 918_033},
        {"role": "assistant", "time_created_ms": 918_043, "time_completed_ms": 1_202_833},
    ]
    build = timeline_builder()
    compressed = build(steps)
    wall = build(steps, axis_mode="wallclock")
    assert _trace(compressed, "User wait (idle)").x == (15.0,)
    assert _trace(wall, "User wait (idle)").x[0] == pytest.approx(46.697)
    assert _trace(wall, "User message").hovertext == _trace(compressed, "User message").hovertext


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("axis_mode", ["wallclock", "skip_idle"])
def test_wrapped_timeline_legend_stays_above_wait_lane_without_shrinking_the_plot(dark, axis_mode):
    from agent_trajviz.insight.charts import gantt

    steps = [
        {"role": "user", "time_created_ms": 10_000},
        {"role": "assistant", "time_created_ms": 11_000, "time_completed_ms": 15_000,
         "tool_calls": [{"tool_name": "Read", "time_start": 12_000, "time_end": 13_000}]},
        {"role": "user", "time_created_ms": 130_000},
        {"role": "assistant", "time_created_ms": 131_000, "time_completed_ms": 135_000},
    ]
    native = gantt.build_execution_gantt_chart(steps, dark=dark, axis_mode=axis_mode)
    figure = timeline_builder()(steps, dark=dark, axis_mode=axis_mode)
    assert {trace.name for trace in figure.data} == {
        "Step (thinking)", "Tool execution", "User message", "User wait (idle)"}
    # A bottom-anchored legend above paper y=1 expands upward when it wraps.
    # Reserve header room while keeping the original plot/lane area intact.
    layout = json.loads(figure.to_json())["layout"]
    assert layout["legend"]["yanchor"] == "bottom" and layout["legend"]["y"] > 1
    assert layout["margin"]["t"] >= 120
    assert figure.layout.height - figure.layout.margin.t == native.layout.height - native.layout.margin.t
    assert _trace(figure, "User wait (idle)").y == ("user wait",)
    assert _trace(figure, "Step (thinking)").y == _trace(native, "Step (thinking)").y
