"""The overview text the model reads, and the inline table and sparkline renderers."""

from __future__ import annotations

import copy

from trajviz_obs.text import render_overview, render_series, render_table


def test_overview_text_shape(fixture_snapshot):
    t = render_overview(fixture_snapshot)
    assert t.count("\n") <= 40 and "Health:" in t and "Issues:" in t
    assert fixture_snapshot["issues"][0]["evidence"] in t


def test_overview_lines(fixture_snapshot):
    lines = render_overview(fixture_snapshot).splitlines()
    assert lines[0].startswith("Health: Cache Efficiency good (95.7%) · Tool Success good (95.8%)")
    assert "23 assistant steps" in lines[1] and "602,021 tokens" in lines[1] and "24 tool calls (95.8% ok)" in lines[1]
    assert "wall clock 1m 45s" in lines[1]
    assert "Context: 15.8% of 200,000" in lines[2]
    assert any(line.startswith("1. ") and " — step 4 — " in line for line in lines)
    assert "Bottlenecks: none" in lines
    assert any(line.startswith("- Scaffold/tooling miss") for line in lines)
    assert not any(line.startswith("Warnings:") for line in lines)


def test_overview_unknown_window_and_warnings(fixture_snapshot):
    snap = copy.deepcopy(fixture_snapshot)
    snap["context"].update(window=None, window_source="unknown", current_pct=None, window_note="No window declared.")
    snap["trajviz"]["warnings"] = ["skipped 2 malformed lines"]
    t = render_overview(snap)
    assert "Context: ctx ? (No window declared.)" in t and "Warnings: skipped 2 malformed lines" in t


def test_overview_bounded(fixture_snapshot):
    snap = copy.deepcopy(fixture_snapshot)
    issue = {**snap["issues"][0], "steps": list(range(50)), "why": "w" * 500}
    snap["issues"] = [{**issue, "why": f"{i} {issue['why']}"} for i in range(10)]
    snap["bottlenecks"] = [{"step": i, "duration_s": 9.5, "cause": "tool", "title": "Tool bottleneck: Bash (9.5s)",
                            "detail": ""} for i in range(5)]
    snap["trajviz"]["warnings"] = ["x" * 400] * 30
    t = render_overview(snap)
    assert "- step 0 (9.5s, tool): Tool bottleneck: Bash (9.5s)" in t
    assert t.count("\n") < 40 and max(len(line) for line in t.splitlines()) <= 240
    assert "steps 0, 1, 2, 3, 4 (+45 more)" in t


def test_table_pages(): assert render_table([{"a": i} for i in range(100)], limit=25, offset=25).endswith("rows 26–50 of 100")


def test_table_union_and_nested():
    t = render_table([{"a": 1, "b": {"x": [1, 2]}}, {"c": "p|q"}], limit=10, offset=0)
    lines = t.splitlines()
    assert lines[0] == "| a | b | c |" and lines[1] == "|---|---|---|"
    assert lines[2] == '| 1 | {"x": [1, 2]} |  |' and lines[3] == "|  |  | p\\|q |"
    assert lines[-1] == "rows 1–2 of 2"
    assert render_table([], limit=10, offset=0) == "rows 0 of 0"


def test_series_glyphs(): assert render_series([0, 1, None, 8], width=4) == "▁▂·█"


def test_series_width_and_flat():
    assert len(render_series(list(range(1000)))) <= 60 and render_series(list(range(1000)))[-1] == "█"
    assert render_series([None, None]) == "··" and render_series([]) == "" and render_series([3, 3]) == "▁▁"
