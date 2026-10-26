"""Standalone report layout without changing chart payloads or trajectory text."""

from __future__ import annotations

from html.parser import HTMLParser

import pytest

from trajviz_obs.report_html import adapt_report_html


class _Markup(HTMLParser):
    def __init__(self, source: str):
        super().__init__(convert_charrefs=False)
        self.nodes: list[tuple[str, dict[str, str | None]]] = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.nodes.append((tag, dict(attrs)))

    def with_class(self, value: str):
        return [(tag, attrs) for tag, attrs in self.nodes if value in (attrs.get("class") or "").split()]


def _document(body: str) -> str:
    return (
        "<!DOCTYPE html><html class='tv-theme-light'><head><meta charset='utf-8'>"
        "<style>.original{color:red}</style></head><body><article class='tv-report'>"
        + body
        + "</article></body></html>"
    )


@pytest.mark.parametrize("source", ["", "plain text", "<html><head></head><body>Other report</body></html>"])
def test_non_trajviz_document_is_unchanged(source):
    assert adapt_report_html(source) == source


def test_missing_head_is_unchanged():
    source = "<article class='tv-report'><section class='tv-chart'><h2>Chart</h2></section></article>"
    assert adapt_report_html(source) == source


def test_adds_one_scoped_stylesheet_inside_head():
    source = _document("<section class='tv-section'><h2>Summary</h2></section>")
    result = adapt_report_html(source)
    styles = [(tag, attrs) for tag, attrs in _Markup(result).nodes if attrs.get("id") == "trajviz-obs-report-style"]
    assert styles == [("style", {"id": "trajviz-obs-report-style"})]
    assert result.index("trajviz-obs-report-style") < result.index("</head>")
    assert "<style>.original{color:red}</style>" in result


def test_chart_scroll_region_wraps_body_without_wrapping_heading():
    chart = "<div style='height:9528px; width:100%;'><div class='plotly-graph-div' id='chart'></div></div>"
    source = _document("<section class='tv-section tv-chart' id='files'><h2>Files &amp; tools</h2>" + chart + "</section>")
    result = adapt_report_html(source)
    regions = _Markup(result).with_class("tv-chart-scroll")
    assert len(regions) == 1
    tag, attrs = regions[0]
    assert tag == "div"
    assert attrs["tabindex"] == "0" and attrs["role"] == "region"
    assert attrs["aria-label"] == "Files & tools chart"
    opening = result.index('<div class="tv-chart-scroll"')
    assert result.index("</h2>") < opening
    assert chart in result
    assert opening < result.index(chart) < result.rindex("</section>")


def test_wraps_each_table_preserving_table_markup():
    first = "<table><thead><tr><th>Tool</th></tr></thead><tbody><tr><td><code>a &amp; b</code></td></tr></tbody></table>"
    second = '<table class="metrics"><tr><td>123.4</td><td>Workflow</td></tr></table>'
    source = _document("<section class='tv-section tv-md'><h2>Metrics</h2>" + first + second + "</section>")
    result = adapt_report_html(source)
    regions = _Markup(result).with_class("tv-table-scroll")
    assert len(regions) == 2
    for tag, attrs in regions:
        assert tag == "div" and attrs["tabindex"] == "0" and attrs["role"] == "region"
        assert attrs["aria-label"] == "Data table"
    assert first in result and second in result


def test_preserves_plotly_and_json_script_bytes():
    plotly = '''<script type="text/javascript">if (document.getElementById("chart")) {
Plotly.newPlot("chart", [{"text":"<table>Workflow</table>","customdata":["wf-card-9","tvGotoWorkflowStep"]}],
{"height":9528,"meta":"</section><h2>Not markup</h2>"}, {"responsive":true});
}</script>'''
    data = '''<script type="application/json">{"html":"<table><tr><td>value</td></tr></table>"}</script>'''
    source = _document("<section class='tv-chart'><h2>Chart</h2><div id='chart'></div>" + plotly + data + "</section>")
    result = adapt_report_html(source)
    assert plotly in result and data in result
    assert len(_Markup(result).with_class("tv-chart-scroll")) == 1
    assert _Markup(result).with_class("tv-table-scroll") == []


def test_hint_in_script_data_is_not_rewritten():
    script = '<script type="application/json">Ranked problems — click a step to open Workflow; expand Why for context</script>'
    assert script in adapt_report_html(_document(script))


def test_hint_in_trajectory_text_is_not_rewritten():
    paragraph = "<p>Ranked problems — click a step to open Workflow; expand Why for context</p>"
    assert paragraph in adapt_report_html(_document(paragraph))


@pytest.mark.parametrize(
    "handler",
    [
        "window.tvGotoWorkflowStep(12)",
        "window.tvFocusWorkflowCard(document.getElementById('wf-card-12'))",
        "window.tvClickMainTab('Workflow')",
        "document.getElementById('wf-card-12').click()",
    ],
)
def test_dashboard_only_controls_become_static_references(handler):
    source = _document(
        '<span class="insight-step-link" role="button" tabindex="0" onclick="'
        + handler
        + '">Step 12</span>'
    )
    result = adapt_report_html(source)
    references = _Markup(result).with_class("obs-static-reference")
    assert len(references) == 1
    tag, attrs = references[0]
    assert tag == "span" and "insight-step-link" in attrs["class"].split()
    assert not ({"onclick", "role", "tabindex"} & attrs.keys())
    assert ">Step 12</span>" in result


def test_root_cause_dashboard_control_becomes_static():
    source = _document(
        '<div class="diag-rc-item diag-rc-secondary" style="cursor:pointer;" '
        'onclick="window.tvGotoWorkflowStep(12)"><span>Root cause</span></div>'
    )
    result = adapt_report_html(source)
    references = _Markup(result).with_class("obs-static-reference")
    assert len(references) == 1 and references[0][0] == "div"
    assert "onclick" not in references[0][1]
    assert "<span>Root cause</span>" in result


def test_working_local_issue_control_and_unrelated_text_are_preserved():
    control = '''<div class="ov-kpi-card ov-kpi-card--issues" role="link" tabindex="0"
onclick="if(window.tvClickMainTab){window.tvClickMainTab('Overview');}var el=document.getElementById('overview-issues');if(el){el.scrollIntoView({behavior:'smooth',block:'start'});if(!el.open){el.open=true;}}">Issues</div>'''
    content = "<p>Workflow, wf-card-12, and tvGotoWorkflowStep are literal trajectory text.</p>"
    result = adapt_report_html(_document(control + content))
    assert control in result and content in result
    assert _Markup(result).with_class("obs-static-reference") == []


def test_removes_instruction_for_unavailable_workflow_tab():
    old = "Ranked problems — click a step to open Workflow; expand Why for context"
    new = "Ranked problems — expand Why for context"
    source = _document("<div class='overview-issues-body'><p>" + old + "</p></div>")
    result = adapt_report_html(source)
    assert old not in result and new in result


def test_rewrites_entity_encoded_hint_without_corrupting_following_text():
    source = _document("<div class='overview-issues-body'><p>"
                       "Ranked problems &#8212; click a step to open Workflow; expand Why for context"
                       "</p><p>Following text</p></div>")
    result = adapt_report_html(source)
    assert "Ranked problems — expand Why for context</p><p>Following text</p>" in result


def test_empty_chart_wrapper_is_balanced():
    result = adapt_report_html(_document("<section class='tv-chart'><h2>Empty</h2></section>"))
    assert 'aria-label="Empty chart"></div></section>' in result


def test_table_immediately_after_chart_heading_keeps_wrapper_nesting():
    result = adapt_report_html(_document("<section class='tv-chart'><h2>Chart</h2><table></table></section>"))
    assert 'aria-label="Chart chart"><div class="tv-table-scroll"' in result
    assert "<table></table></div></div></section>" in result


def test_adapter_is_idempotent():
    source = _document(
        "<section class='tv-chart'><h2>Chart</h2><div id='chart'></div></section>"
        '<span class="insight-step-link" onclick="window.tvGotoWorkflowStep(1)" role="button" tabindex="0">Step 1</span>'
        "<table><tr><td>value</td></tr></table>"
    )
    once = adapt_report_html(source)
    assert adapt_report_html(once) == once


def test_unclosed_head_is_left_unchanged():
    source = "<html><head><article class='tv-report'><table><tr><td>x</td></tr></table></article>"
    assert adapt_report_html(source) == source
