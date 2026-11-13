"""Plugin presentation preserves the native dashboard's full feature surface."""

from __future__ import annotations

import inspect
import json
import shutil
import subprocess

import gradio as gr
import plotly.graph_objects as go
import pytest
from agent_trajviz import dashboard

from trajviz_obs.dashboard_ui import _JS, decorate_dashboard, presentation


@pytest.mark.parametrize("kind", ["insight", "converge"])
def test_decoration_preserves_every_native_component_and_callback(kind, tmp_path):
    native = dashboard.insight_app() if kind == "insight" else dashboard.converge_app(data_root=tmp_path)
    blocks = dict(native.blocks)
    callbacks = [fn.fn for fn in native.fns.values() if fn.fn is not None]
    app = decorate_dashboard(native, kind)
    assert all(app.blocks[key] is block for key, block in blocks.items())
    assert all(any(inspect.unwrap(fn.fn) is inspect.unwrap(callback) for fn in app.fns.values()
                   if fn.fn is not None) for callback in callbacks)
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


def test_native_attribution_load_clear_and_diagnosis_order_is_preserved():
    native = dashboard.insight_app()
    load = next(fn for fn in native.fns.values() if fn.api_name == "_do_load")
    clear = next(fn for fn in native.fns.values() if fn.api_name == "_clear_attribution")
    automatic = next(fn for fn in native.fns.values() if fn.api_name == "on_diagnose_autoload")
    clicks = list(load.targets)
    assert load.trigger_after is None
    assert clear.targets == [(None, "then")] and clear.trigger_after == load._id
    assert automatic.trigger_after == clear._id
    app = decorate_dashboard(native, "insight")
    assert app.fns[load._id] is load and app.fns[clear._id] is clear
    assert load.targets == clicks and load.trigger_after is None
    assert clear.targets == [(None, "then")] and clear.trigger_after == load._id
    assert automatic.trigger_after == clear._id


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


@pytest.mark.parametrize("chart, axis, note", [
    ("token-chart", "y", "No step token usage"),
    ("duration-chart", "y", "No step durations"),
    ("gantt-chart", "x", "No drawable execution"),
])
def test_missing_chart_evidence_gets_explanation_but_keeps_measured_zero(chart, axis, note):
    with gr.Blocks() as native:
        plot = gr.Plot(elem_id=chart)
    decorate_dashboard(native, "insight")
    missing = json.loads(plot.postprocess(go.Figure(go.Bar(**{axis: [None]}))).plot)
    assert missing["data"] == [] and note in missing["layout"]["annotations"][0]["text"]
    assert missing["layout"]["xaxis"]["visible"] is False
    measured = json.loads(plot.postprocess(go.Figure(go.Bar(**{axis: [0]}))).plot)
    assert measured["data"][0][axis] == [0]


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
    status = next(block for block in native.blocks.values() if isinstance(block, gr.Column) and any(
        "label-status-badge" in (child.elem_classes or []) for child in block.children))
    status_children = list(status.children)
    app = decorate_dashboard(native, "insight")
    tabs = next(child for child in app.children if isinstance(child, gr.Tabs))
    overview = next(child for child in tabs.children if child.label == "Overview")
    layout = next(child for child in overview.children if "overview-content-layout" in child.elem_classes)
    content = next(child for child in layout.children if "overview-section-content" in child.elem_classes)
    summary = content.children[0]
    assert cards not in app.children and cards in summary.children
    assert overview.children == [layout]
    actions = next(child for child in summary.children if isinstance(child, gr.Row) and any(
        isinstance(button, gr.Button) and "Suggest fixes" in button.value for button in child.children))
    assert any(isinstance(button, gr.DownloadButton) and button.label == "Export issues into JSON"
               for button in actions.children)
    assert status in summary.children and status not in app.children
    assert status.children == status_children
    assert "obs-summary-status" in status.elem_classes
    token_row = next(child for child in summary.children if isinstance(child, gr.Row)
                     and any(plot.elem_id == "token-chart" for plot in child.children))
    guide = next(child for child in summary.children if child.elem_id == "obs-summary-chart-guide")
    assert summary.children.index(guide) < summary.children.index(token_row)
    assert "obs-summary-charts" in token_row.elem_classes
    assert [chart.elem_id for chart in token_row.children] == ["token-chart", "duration-chart"]
    assert "token-step-explain" in summary.children[summary.children.index(token_row) + 1].elem_classes
    gantt_row = next(child for child in summary.children if isinstance(child, gr.Row)
                     and any(plot.elem_id == "gantt-chart" for plot in child.children))
    axis_form = summary.children[summary.children.index(gantt_row) + 1]
    assert any(isinstance(control, gr.Radio) and control.label == "Timeline axis" for control in axis_form.children)


def test_language_selector_stays_visible_with_the_plugin_heading():
    app = decorate_dashboard(dashboard.insight_app(), "insight")
    heading = app.children[0]
    header = next(child for child in heading.children if child.elem_id == "tv-app-header")
    selector = next(child for child in app.blocks.values() if child.elem_id == "tv-language-selector")
    assert selector.visible and all(not child.visible for child in header.children if isinstance(child, gr.HTML))
    assert header not in app.children


@pytest.mark.parametrize("locale, caption", [("en", "Attribution API base URL"), ("zh-CN", "归因 API 基础 URL")])
def test_dashboard_language_updates_keep_public_attribution_terminology(locale, caption):
    app = decorate_dashboard(dashboard.insight_app(), "insight")
    updates = next(entry.fn for entry in app.fns.values()
                   if getattr(entry.fn, "__module__", "") == "agent_trajviz.i18n.ui"
                   and getattr(entry.fn, "__name__", "") == "restore")(locale)
    displayed = json.dumps(updates, ensure_ascii=False)
    assert caption in displayed and "AWE API base URL" not in displayed and "AWE API 基础 URL" not in displayed
    head = presentation("insight", dashboard.insight_presentation())["head"]
    translations, _ = json.JSONDecoder().raw_decode(head.split("window.__tvTranslations=", 1)[1])
    assert translations["attribution connection preferences restored. Enter a key and apply settings before running an operation."].startswith("已恢复 归因")


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


def test_step_navigation_reaches_tabs_moved_into_gradio_overflow():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to execute the dashboard browser script")
    script = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const calls = [];
let controls = [{textContent: 'Workflow', click: () => calls.push('overflow')}];
const tabs = {};
const root = {
  dataset: {}, style: {}, addEventListener() {},
  querySelector: selector => selector === '.obs-main-tabs' ? tabs : null,
  querySelectorAll: selector => selector.includes('overflow-dropdown') ? controls : [],
};
const historyCalls = [];
let nativeReturnPoints = 0;
const history = {
  state: null,
  replaceState: state => historyCalls.push(['source', state.tvTab]),
  pushState: state => historyCalls.push(['target', state.tvTab]),
};
const window = {
  tvClickMainTab: label => calls.push('native:' + label),
  tvActiveMainTab: () => 'Overview', location: {href: 'http://localhost/'},
  tvPushTabReturnPoint: () => nativeReturnPoints++,
  tvGotoWorkflowStep: (index, options) => {
    if (!options?.restore) history.pushState({tvTab: 'Workflow'});
    window.tvPushTabReturnPoint('Overview');
  },
};
const document = {
  documentElement: {style: {}, classList: {remove() {}}},
  body: {classList: {remove() {}}}, querySelector: () => root,
};
class Observer { observe() {} disconnect() {} }
vm.runInNewContext('(' + JSON.parse(process.argv[1]) + ')()', {
  document, window, history, ResizeObserver: Observer, MutationObserver: Observer,
  requestAnimationFrame: callback => callback(),
});
window.tvClickMainTab('Workflow');
controls = [{textContent: 'Workflow', click: () => calls.push('visible-tab')}];
window.tvClickMainTab('Workflow');
controls = [];
window.tvClickMainTab('Comparison');
assert.deepEqual(calls, ['overflow', 'visible-tab', 'native:Comparison']);
window.tvGotoWorkflowStep(3);
assert.deepEqual(historyCalls, [['source', 'Overview'], ['target', 'Workflow']]);
assert.equal(nativeReturnPoints, 0);
window.tvGotoWorkflowStep(3, {restore: true});
assert.equal(historyCalls.length, 2);
window.tvPushTabReturnPoint('Overview');
assert.equal(nativeReturnPoints, 1);
"""
    subprocess.run([node, "-e", script, json.dumps(_JS)], check=True, capture_output=True, text=True, timeout=10)
