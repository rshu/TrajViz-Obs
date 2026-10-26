"""Standalone layout fixes for TrajViz 0.2's dashboard-derived HTML export.

Keep the public report renderer and its chart payloads intact. Source-position
edits add accessible scroll regions and remove controls that require a live
Workflow tab; unlike reserializing the document, this preserves Plotly scripts
and escaped trajectory content byte for byte.
"""

from __future__ import annotations

import html
from html.parser import HTMLParser

_STYLE_ID = "trajviz-obs-report-style"
_CSS = """
body {
  font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI',
    'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', sans-serif;
  line-height: 1.5;
}
.tv-report, .tv-report * { box-sizing: border-box; }
.tv-report { max-width: 1200px; padding: 28px 24px 64px; }
.tv-report h1, .tv-report p, .tv-report summary,
.tv-report .agent-card, .tv-report .ov-kpi-card { overflow-wrap: anywhere; }
.tv-report > header { margin-bottom: 28px; }
.tv-report .tv-meta { line-height: 1.6; }
.tv-report .tv-section { min-width: 0; margin: 28px 0; }
.tv-report .ov-kpi-grid {
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 210px), 1fr));
}
.tv-report .ov-kpi-card { min-width: 0; }
.tv-report .ov-kpi-value { font-variant-numeric: tabular-nums; }
.tv-report [data-help]::after { left: 0; transform: none; max-width: 100%; }
.tv-report .agent-cards-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 280px), 1fr));
}
.tv-report .agent-card { min-width: 0; max-width: none; }
.tv-report .agent-card-header { flex-wrap: wrap; }
.tv-report .agent-card-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.tv-report .tv-md > div > div, .tv-report .session-detail > div > div {
  min-width: 0 !important; max-width: 100%; overflow-wrap: anywhere;
}
.tv-report .tv-chart-scroll, .tv-report .tv-table-scroll {
  max-width: 100%; overflow: auto; border: 1px solid var(--ov-border);
  border-radius: 8px; background: var(--ov-card); scrollbar-width: thin;
}
.tv-report .tv-chart-scroll { max-height: 720px; }
.tv-report .tv-chart-scroll > div { min-width: 640px; }
.tv-report .tv-table-scroll { max-height: 560px; margin: 8px 0 16px; }
.tv-report .tv-table-scroll table { margin: 0; width: 100%; }
.tv-report .tv-table-scroll th { position: sticky; top: 0; z-index: 1; }
.tv-report .tv-table-scroll th, .tv-report .tv-table-scroll td {
  min-width: 88px; max-width: 360px; overflow-wrap: break-word; vertical-align: top;
}
.tv-report .tv-chart-scroll:focus-visible, .tv-report .tv-table-scroll:focus-visible {
  outline: 2px solid var(--ov-accent); outline-offset: 3px;
}
.tv-report .obs-static-reference, .tv-report .obs-static-reference:hover {
  cursor: default !important; text-decoration: none; transform: none; box-shadow: none;
}
.tv-report .insight-step-link.obs-static-reference:hover {
  background: transparent; padding: 0;
}
@media (max-width: 768px) {
  .tv-report { padding: 20px 16px 48px; }
  .tv-report h1 { font-size: 20px; }
  .tv-report .ov-kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)) !important; }
}
@media (max-width: 420px) {
  .tv-report .ov-kpi-grid { grid-template-columns: minmax(0, 1fr) !important; }
}
@media print {
  .tv-report { max-width: none; padding: 0; }
  .tv-report .tv-chart-scroll, .tv-report .tv-table-scroll { max-height: none; overflow: visible; }
  .tv-report .tv-chart-scroll > div { min-width: 0; }
  .tv-report .tv-table-scroll th { position: static; }
  .tv-report .tv-chart { break-inside: auto; }
}
"""
_WORKFLOW_HANDLERS = ("Workflow", "wf-card", "tvGotoWorkflowStep", "tvFocusWorkflowCard")
_LIVE_HINT = "Ranked problems — click a step to open Workflow; expand Why for context"
_SCRIPT = """
(() => {
  function fitFileChart() {
    const chart = document.querySelector('#file-interactions .plotly-graph-div');
    const axis = chart && chart.layout && chart.layout.yaxis;
    const count = axis && axis.categoryarray && axis.categoryarray.length;
    if (!window.Plotly || !count || count <= 20) return;
    // Plotly's proportional category padding becomes hundreds of empty pixels
    // on a tall file chart. Keep half a row of padding at each end instead.
    const update = {'legend.maxheight': 120};
    if (axis.autorange !== false) {
      update['yaxis.range'] = axis.autorange === 'reversed' ? [count - 0.5, -0.5] : [-0.5, count - 0.5];
      update['yaxis.autorange'] = false;
    }
    window.Plotly.relayout(chart, update).catch(() => {});
  }
  if (document.readyState === 'complete') fitFileChart();
  else window.addEventListener('load', fitFileChart, {once: true});
})();
"""


class _ReportLayout(HTMLParser):
    def __init__(self, document: str) -> None:
        super().__init__(convert_charrefs=True)
        self.document = document
        self.lines = [0]
        self.lines.extend(i + 1 for i, char in enumerate(document) if char == "\n")
        self.edits: list[tuple[int, int, str]] = []
        self.sections: list[dict] = []
        self.tables: list[int] = []
        self.heading: list[str] | None = None
        self.head_end: int | None = None
        self.body_end: int | None = None
        self.is_report = False
        self.adapted = False
        self.raw_tag: str | None = None
        self.issue_hint_pending = False

    def position(self) -> int:
        line, column = self.getpos()
        return self.lines[line - 1] + column

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        classes = (attr.get("class") or "").split()
        if tag == "article" and "tv-report" in classes:
            self.is_report = True
        if "overview-issues-body" in classes:
            self.issue_hint_pending = True
        if tag == "style" and attr.get("id") == _STYLE_ID:
            self.adapted = True
        if tag in {"script", "style"}:
            self.raw_tag = tag
        if tag == "section":
            self.sections.append({"chart": "tv-chart" in classes, "start": None, "title": "Chart"})
        if tag == "h2" and self.sections and self.sections[-1]["chart"]:
            self.heading = []
        if tag == "table":
            self.tables.append(self.position())
        onclick = attr.get("onclick") or ""
        if any(marker in onclick for marker in _WORKFLOW_HANDLERS):
            # These presenters refer to helpers supplied only by the live UI.
            kept = [(key, value) for key, value in attrs
                    if not key.startswith("on") and key not in {"role", "tabindex", "class", "title"}]
            kept.append(("class", " ".join([*classes, "obs-static-reference"])))
            attributes = "".join(f' {key}="{html.escape(value, quote=True)}"' if value is not None else f" {key}"
                                 for key, value in kept)
            start = self.position()
            self.edits.append((start, start + len(self.get_starttag_text()), f"<{tag}{attributes}>"))

    def handle_data(self, data: str) -> None:
        if self.raw_tag is not None:
            return
        if self.heading is not None:
            self.heading.append(data)
        if self.issue_hint_pending and data.strip():
            self.issue_hint_pending = False
            if data == _LIVE_HINT:
                start = self.position()
                end = self.document.find("<", start)
                if end < 0:
                    end = len(self.document)
                if html.unescape(self.document[start:end]) == data:
                    self.edits.append((start, end, "Ranked problems — expand Why for context"))

    def handle_endtag(self, tag: str) -> None:
        if tag == self.raw_tag:
            self.raw_tag = None
            return
        start = self.position()
        end = self.document.find(">", start) + 1
        if tag == "head":
            self.head_end = start
        elif tag == "body":
            self.body_end = start
        elif tag == "h2" and self.heading is not None:
            self.sections[-1].update(start=end, title="".join(self.heading).strip())
            self.heading = None
        elif tag == "section" and self.sections:
            section = self.sections.pop()
            if section["chart"] and section["start"] is not None:
                label = html.escape(f'{section["title"]} chart', quote=True)
                opening = f'<div class="tv-chart-scroll" tabindex="0" role="region" aria-label="{label}">'
                if section["start"] == start:
                    self.edits.append((start, start, opening + "</div>"))
                else:
                    self.edits.extend([(section["start"], section["start"], opening), (start, start, "</div>")])
        elif tag == "table" and self.tables:
            opening = self.tables.pop()
            self.edits.extend([
                (opening, opening, '<div class="tv-table-scroll" tabindex="0" role="region" aria-label="Data table">'),
                (end, end, "</div>"),
            ])


def adapt_report_html(document: str) -> str:
    """Add standalone layout without changing chart data or unrelated documents.

    TrajViz is an external dependency; leave an unrecognized document alone.
    A marker makes this idempotent for reports that are processed again.
    """
    parser = _ReportLayout(document)
    parser.feed(document)
    parser.close()
    if not parser.is_report or parser.head_end is None or parser.adapted:
        return document
    parser.edits.append((parser.head_end, parser.head_end, f'<style id="{_STYLE_ID}">{_CSS}</style>\n'))
    if parser.body_end is not None:
        parser.edits.append((parser.body_end, parser.body_end, f"<script>{_SCRIPT}</script>\n"))
    for start, end, replacement in sorted(parser.edits, key=lambda edit: edit[:2], reverse=True):
        document = document[:start] + replacement + document[end:]
    return document
