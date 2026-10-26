"""Plugin presentation preserves the native dashboard's full feature surface."""

from __future__ import annotations

import gradio as gr
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
