"""Plugin presentation preserves the native dashboard's full feature surface."""

from __future__ import annotations

import json

import gradio as gr
import plotly.graph_objects as go
import pytest
from agent_trajviz import dashboard

from trajviz_obs.dashboard_ui import decorate_dashboard, presentation


@pytest.mark.parametrize("kind", ["insight", "converge"])
def test_decoration_preserves_every_native_component_and_callback(kind, tmp_path):
    native = dashboard.insight_app() if kind == "insight" else dashboard.converge_app(data_root=tmp_path)
    blocks = dict(native.blocks)
    callbacks = [fn.fn for fn in native.fns.values() if fn.fn is not None]
    app = decorate_dashboard(native, kind)
    assert all(app.blocks[key] is block for key, block in blocks.items())
    assert all(any(fn.fn is callback for fn in app.fns.values()) for callback in callbacks)
    plots = [block for block in app.blocks.values() if isinstance(block, gr.Plot)]
    assert plots and all("obs-chart" in block.elem_classes for block in plots)
    assert any(isinstance(block, gr.HTML) and 'aria-label="' in str(block.value) and "obs-guide" in str(block.value)
               for block in app.blocks.values())


@pytest.mark.parametrize("kind", ["insight", "converge"])
def test_every_visible_component_is_reachable_in_the_served_layout(kind, tmp_path):
    """Gradio resolves frontend readiness only after every visible configured component registers."""
    native = dashboard.insight_app() if kind == "insight" else dashboard.converge_app(data_root=tmp_path)
    config = decorate_dashboard(native, kind).get_config_file()

    def descendants(layout):
        yield layout["id"]
        for child in layout.get("children", []):
            yield from descendants(child)

    reachable = set(descendants(config["layout"]))
    orphaned = [(block["id"], block["type"]) for block in config["components"]
                if block["props"].get("visible") is not False and block["id"] not in reachable]
    assert orphaned == [], f"Visible components outside the layout prevent Gradio initialization: {orphaned}"


def test_source_disclosure_contains_native_uploads_without_hiding_main_tabs():
    native = dashboard.insight_app()
    files = [block for block in native.blocks.values() if isinstance(block, gr.File)]
    app = decorate_dashboard(native, "insight")
    controls = next(block for block in app.blocks.values() if "obs-source-controls" in (block.elem_classes or []))
    # Gradio lazily mounts closed disclosures; initial preload needs the native Load button mounted.
    # The app's browser hook collapses controls after the main tabs appear.
    assert isinstance(controls, gr.Accordion) and controls.open is True
    source = next(child for child in controls.children if "upload-row" in (child.elem_classes or []))
    assert "upload-row" in source.elem_classes
    assert all(app.blocks[block._id] is block for block in files)
    assert any(isinstance(child, gr.Tabs) and "obs-main-tabs" in child.elem_classes for child in app.children)
    assert controls in app.children and source not in app.children


def test_single_chart_rows_do_not_reserve_space_for_empty_columns():
    native = dashboard.insight_app()
    labels = {"Skill Calls by Agent", "Token Breakdown by Agent"}
    rows = [block for block in native.blocks.values() if isinstance(block, gr.Row)
            and any(isinstance(child, gr.Plot) and child.label in labels for child in block.children)]
    assert len(rows) == 2
    spacers = [child for row in rows for child in row.children
               if isinstance(child, gr.Column) and not child.children]
    assert len(spacers) == 2 and all(child.visible for child in spacers)
    app = decorate_dashboard(native, "insight")
    assert all(app.blocks[child._id] is child and child.visible is False for child in spacers)
    assert all(child.visible for row in rows for child in row.children if isinstance(child, gr.Plot))


def test_chart_row_preserves_a_column_containing_controls():
    with gr.Blocks() as native, gr.Row():
        gr.Plot()
        with gr.Column() as controls:
            button = gr.Button("Keep this action")
    app = decorate_dashboard(native, "insight")
    assert app.blocks[controls._id] is controls and controls.visible
    assert app.blocks[button._id] is button and button.visible


def test_attribution_clear_finishes_before_load_and_automatic_diagnosis():
    native = dashboard.insight_app()
    load = next(fn for fn in native.fns.values() if fn.api_name == "_do_load")
    clear = next(fn for fn in native.fns.values() if fn.api_name == "_clear_attribution")
    automatic = next(fn for fn in native.fns.values() if fn.api_name == "on_diagnose_autoload")
    clicks = list(load.targets)
    assert clear.targets == clicks and load.trigger_after is None
    app = decorate_dashboard(native, "insight")
    assert app.fns[load._id] is load and app.fns[clear._id] is clear
    assert clear.targets == clicks and clear.trigger_after is None
    assert load.targets == [(None, "then")] and load.trigger_after == clear._id
    assert automatic.trigger_after == load._id


@pytest.mark.parametrize("label", ["Token Usage", "Tool Call Frequency", "Token Breakdown by Agent", "Skill Calls by Agent"])
def test_native_plot_outputs_fit_container_without_mutating_source_figures(label):
    native = dashboard.insight_app()
    plot = next(block for block in native.blocks.values() if isinstance(block, gr.Plot) and block.label == label)
    figure = go.Figure() if label == "Skill Calls by Agent" else go.Figure(go.Bar(x=["main"], y=[3]))
    figure.update_layout(width=900, height=320, autosize=False, title=label)
    before = figure.to_json()
    decorate_dashboard(native, "insight")
    result = plot.postprocess(figure)
    layout = json.loads(result.plot)["layout"]
    assert "width" not in layout and layout["autosize"] is True
    assert layout["height"] == 320 and layout["title"]["text"] == label
    assert figure.to_json() == before


def test_initial_plot_value_is_responsive_and_non_plotly_outputs_are_unchanged():
    from gradio.components.plot import PlotData

    figure = go.Figure().update_layout(width=900, autosize=False)
    with gr.Blocks() as native:
        plot = gr.Plot(value=figure)
    original = plot.value
    decorate_dashboard(native, "insight")
    assert "width" not in json.loads(plot.value["plot"])["layout"]
    assert json.loads(plot.value["plot"])["layout"]["autosize"] is True
    assert json.loads(original["plot"])["layout"]["width"] == 900
    assert plot.postprocess(None) is None
    other = PlotData(type="altair", plot='{"width": 900}')
    assert plot.postprocess(other) is other
    serialized = PlotData(type="plotly", plot=figure.to_json())
    responsive = plot.postprocess(serialized)
    assert responsive is not serialized and json.loads(serialized.plot)["layout"]["width"] == 900


def test_mobile_plot_text_wraps_plain_empty_messages_and_axis_titles_only():
    plain = "No Skill-tool invocations recorded in this trajectory."
    rich = "<b>Keep this deliberately long rich annotation intact.</b>"
    figure = go.Figure().update_layout(
        width=900, margin={"b": 20},
        xaxis_title="Summed call time (s) — parallel calls each count",
        yaxis_title="<b>Keep this deliberately long rich axis title intact.</b>",
        annotations=[{"text": plain}, {"text": rich}],
    )
    before = figure.to_json()
    with gr.Blocks() as native:
        plot = gr.Plot()
    decorate_dashboard(native, "insight")
    layout = json.loads(plot.postprocess(figure).plot)["layout"]
    assert all(len(line) <= 28 for line in layout["annotations"][0]["text"].split("<br>"))
    assert layout["annotations"][1]["text"] == rich
    assert "<br>" in layout["xaxis"]["title"]["text"] and layout["margin"]["b"] == 70
    assert layout["yaxis"]["title"]["text"] == figure.layout.yaxis.title.text
    assert figure.to_json() == before
    figure.add_bar(x=["main"], y=[1])
    assert json.loads(plot.postprocess(figure).plot)["layout"]["annotations"][0]["text"] == plain


def test_presentation_keeps_upstream_assets_without_mutating_original():
    original = {"css": "native-css", "js": "native-js", "head": "native-head", "theme": object()}
    result = presentation("insight", original)
    assert result is not original and original["css"] == "native-css"
    assert result["css"].startswith("native-css\n")
    assert all(result[key] is original[key] for key in ("js", "head", "theme"))


def test_health_and_findings_live_in_summary_without_burying_other_tabs():
    native = dashboard.insight_app()
    cards = next(block for block in native.blocks.values()
                 if "overview-kpi-strip" in (block.elem_classes or []))
    app = decorate_dashboard(native, "insight")
    tabs = next(child for child in app.children if isinstance(child, gr.Tabs))
    overview = next(child for child in tabs.children if child.label == "Overview")
    layout = next(child for child in overview.children if "overview-content-layout" in child.elem_classes)
    content = next(child for child in layout.children if "overview-section-content" in child.elem_classes)
    summary = content.children[0]
    assert cards not in app.children and cards in summary.children
    assert overview.children == [layout]
    assert any(isinstance(child, gr.Button) and "Suggest fixes" in child.value for child in summary.children)
    assert any(isinstance(child, gr.HTML) and "summary-banner" in child.elem_classes
               for column in summary.children if isinstance(column, gr.Column) for child in column.children)


@pytest.mark.parametrize("kind", ["insight", "converge"])
def test_native_dark_media_rules_do_not_conflict_with_light_dashboard_charts(kind):
    native = dashboard.insight_presentation() if kind == "insight" else dashboard.converge_presentation()
    original = native["css"]
    assert "@media (prefers-color-scheme: dark)" in original
    result = presentation(kind, native)
    assert "@media (prefers-color-scheme: dark)" not in result["css"]
    assert "@media not all" in result["css"]
    assert native["css"] == original


def test_converge_sections_keep_all_native_controls_in_separate_tabs(tmp_path):
    app = decorate_dashboard(dashboard.converge_app(data_root=tmp_path), "converge")
    tabs = next(child for child in app.children if isinstance(child, gr.Tabs))
    assert [tab.label for tab in tabs.children] == ["Pairwise", "Batch", "Before/After"]
    for panel, action in zip(tabs.children, ("Compare", "Run Batch", "Compare Before/After"), strict=True):
        assert any(isinstance(child, gr.Button) and child.value == action for child in panel.children)


def test_converge_upload_rows_stack_on_mobile_without_stacking_chart_rows(tmp_path):
    app = decorate_dashboard(dashboard.converge_app(data_root=tmp_path), "converge")

    def descendants(block):
        for child in getattr(block, "children", []):
            yield child
            yield from descendants(child)

    rows = [block for block in app.blocks.values() if "obs-file-row" in (block.elem_classes or [])]
    files = [block for block in app.blocks.values() if isinstance(block, gr.File)]
    covered_files = [child for row in rows for child in descendants(row) if isinstance(child, gr.File)]
    assert len(rows) == 4 and len(covered_files) == len(files) == 6
    assert set(covered_files) == set(files)
    assert not any(isinstance(child, gr.Plot) for row in rows for child in descendants(row))
    css = presentation("converge", dashboard.converge_presentation())["css"]
    desktop, mobile = css.split("@media (max-width: 768px) {", 1)
    assert "obs-file-row" not in desktop
    mobile = mobile.split("\n}\n", 1)[0]
    assert ".obs-file-row { flex-direction: column; }" in mobile
