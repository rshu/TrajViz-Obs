"""Plugin presentation around the full native TrajViz dashboards.

Reuse every upstream component and callback. Keep navigation before the body,
put session health in Overview's Summary, and bound dense charts and filters.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .compat import attribution_message

if TYPE_CHECKING:
    import gradio as gr

_CSS = """
.obs-dashboard .main { width: 100% !important; min-width: 0 !important; max-width: 1440px !important;
  margin: auto; padding: clamp(12px, 2vw, 24px) !important; }
.obs-dashboard, .obs-dashboard * { box-sizing: border-box; }
.obs-dashboard .main, .obs-dashboard .wrap, .obs-dashboard .contain,
.obs-dashboard .tabs, .obs-dashboard .tabitem, .obs-dashboard .row, .obs-dashboard .column {
  min-width: 0 !important; max-width: 100%;
}
.obs-dashboard .obs-guide { padding: 12px 16px; border: 1px solid #dce3ef;
  border-radius: 10px; background: #f8fafc; color: #0f172a; }
.obs-dashboard .obs-guide-header { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 16px; }
.obs-dashboard .obs-guide h1 { font-size: 19px; margin: 0; line-height: 1.4; }
.obs-dashboard .obs-guide details { margin-top: 6px; }
.obs-dashboard .obs-guide summary { color: #1d4ed8; font-size: 12px; cursor: pointer; }
.obs-dashboard .obs-guide summary:focus-visible { outline: 2px solid #2563eb; outline-offset: 3px; }
.obs-dashboard .obs-guide-block .html-container { padding: 0 !important; }
.obs-dashboard .obs-guide-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px; margin-top: 12px; }
.obs-dashboard .obs-guide p { font-size: 13px; line-height: 1.5; margin: 0; overflow-wrap: anywhere; }
.obs-dashboard .obs-guide strong { display: block; margin-bottom: 3px; }
.obs-dashboard .obs-model-note { font-size: 12px !important; margin-top: 12px !important; color: #475569; }
.obs-dashboard .obs-model-note strong { display: inline; }
.obs-dashboard .obs-source-controls { margin-bottom: 0; }
.obs-dashboard .upload-row { padding-top: 8px; }
.obs-dashboard .overview-kpi-strip { margin: 0; padding: 0 !important; }
.obs-dashboard .overview-kpi-strip .html-container { padding: 0 !important; }
.obs-dashboard .ov-kpi-grid { grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; }
.obs-dashboard .ov-kpi-card { min-width: 0; overflow-wrap: anywhere; }
.obs-dashboard .ov-kpi-value { font-size: 21px; font-variant-numeric: tabular-nums; }
.obs-dashboard .ov-kpi-breakdown { max-height: 3.8em; }
.obs-dashboard .obs-summary-status, .obs-dashboard .obs-summary-status > .block,
.obs-dashboard .obs-summary-status .html-container { padding: 0 !important; }
.obs-dashboard [data-help]::after { left: 0; transform: none; max-width: 100%; }
.obs-dashboard .overview-issues-body { max-height: 400px; overflow: auto; }
.obs-dashboard .agent-cards-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 260px), 1fr)); }
.obs-dashboard .agent-card { min-width: 0; max-width: none; overflow-wrap: anywhere; }
.obs-dashboard .agent-card-header { flex-wrap: wrap; }
.obs-dashboard .agent-card-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.obs-dashboard .overview-section-content { min-width: 0 !important; }
.obs-dashboard .overview-section-nav { top: 56px; }
.obs-dashboard .obs-main-tabs > .tab-wrapper { position: sticky; top: 0; z-index: 20;
  background: var(--background-fill-primary, white); }
.obs-dashboard .obs-main-tabs [role="tab"] { flex-shrink: 0; white-space: nowrap; }
.obs-dashboard .obs-chart, .obs-dashboard .resizable-chart {
  min-width: 0 !important; max-height: 720px !important; overflow: auto !important;
  border: 1px solid var(--ov-border, #dce3ef); border-radius: 8px; scrollbar-width: thin;
}
.obs-dashboard .obs-chart > .js-plotly-plot { min-width: 640px; }
.obs-dashboard .obs-chart:has(> .empty) { display: none !important; }
.obs-dashboard .obs-chart-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 640px), 1fr)); }
.obs-dashboard .obs-markdown { min-width: 0 !important; max-width: 100%; overflow-x: auto; }
.obs-dashboard .obs-markdown table { width: max-content; min-width: 100%; }
.obs-dashboard .obs-markdown th, .obs-dashboard .obs-markdown td { max-width: 360px; overflow-wrap: break-word; }
.obs-dashboard .obs-markdown > div > div > div { max-width: 100%; overflow-wrap: anywhere; }
.obs-dashboard .cvg-report { min-width: 0; max-width: 100%; overflow-x: auto; }
.obs-dashboard .obs-chart:focus-visible { outline: 2px solid #2563eb; outline-offset: 2px; }
.obs-dashboard .insight-step-link:focus-visible { outline: 2px solid #2563eb; outline-offset: 2px; }
.obs-dashboard .obs-workflow-filters { display: grid; align-items: start;
  grid-template-columns: auto minmax(0, 1fr) minmax(220px, .6fr); }
.obs-dashboard .obs-workflow-filters > button { align-self: start !important; height: auto !important; }
.obs-dashboard .filter-group { align-items: start; }
.obs-dashboard .filter-options { min-width: 0; max-height: 128px; overflow-y: auto; padding: 2px; }
.obs-dashboard .filter-chip { flex-shrink: 0; }
.obs-dashboard .obs-workflow-body { display: grid; align-items: start; gap: 16px;
  grid-template-columns: 120px minmax(0, 1fr) minmax(280px, .9fr); }
.obs-dashboard .wf-toc-sidebar { width: 100%; top: 56px; }
.obs-dashboard .detail-panel { top: 56px; }
@media (max-width: 1100px) {
  .obs-dashboard .ov-kpi-grid { grid-template-columns: repeat(3, minmax(0, 1fr)) !important; }
  .obs-dashboard .obs-workflow-body { grid-template-columns: 100px minmax(0, 1fr); }
  .obs-dashboard .obs-workflow-body > .column:first-child { grid-row: 1 / span 2; }
  .obs-dashboard .obs-workflow-body > .detail-panel { grid-column: 2; position: relative; top: auto; }
  .obs-dashboard .obs-workflow-body .wf-scroll { max-height: 50vh; }
}
@media (max-width: 768px) {
  .obs-dashboard #analysis-sidebar.right:not(.open) .toggle-button::after { content: "AI analysis"; }
  .obs-dashboard .obs-guide { padding: 10px 12px; }
  .obs-dashboard .obs-guide-grid { grid-template-columns: 1fr; gap: 10px; }
  .obs-dashboard .ov-kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)) !important; }
  .obs-dashboard .overview-issues-body { max-height: 340px; }
  .obs-dashboard .overview-section-nav { padding: 4px; margin-bottom: 8px; }
  .obs-dashboard .overview-nav-title { padding: 4px 8px; margin-bottom: 0; }
  .obs-dashboard .overview-section-radio > .wrap { scrollbar-width: thin; }
  .obs-dashboard .obs-workflow-filters { grid-template-columns: auto minmax(0, 1fr); }
  .obs-dashboard .obs-workflow-filters > .form { grid-column: 1 / -1; }
  .obs-dashboard .obs-workflow-body { grid-template-columns: minmax(0, 1fr); }
  .obs-dashboard .obs-workflow-body > .column { grid-column: 1; grid-row: auto !important; }
  .obs-dashboard .wf-toc-sidebar { max-height: 140px; position: static; }
  .obs-dashboard .filter-group { flex-direction: column; gap: 6px; }
  .obs-dashboard .filter-group-label { flex-basis: auto; }
  .obs-dashboard .filter-options { width: 100%; max-height: 100px; }
}
/* Gradio 6 drops @container rules when scoping CSS. ResizeObserver sets these
   classes from the actual tab width, including space taken by the AI sidebar. */
.obs-main-tabs.obs-content-medium .ov-kpi-grid { grid-template-columns: repeat(3, minmax(0, 1fr)) !important; }
.obs-main-tabs.obs-content-medium .obs-workflow-body { grid-template-columns: 100px minmax(0, 1fr); }
.obs-main-tabs.obs-content-medium .obs-workflow-body > .column:first-child { grid-row: 1 / span 2; }
.obs-main-tabs.obs-content-medium .obs-workflow-body > .detail-panel { grid-column: 2; position: relative; top: auto; }
.obs-main-tabs.obs-content-medium .obs-workflow-body .wf-scroll { max-height: 50vh; }
.obs-main-tabs.obs-content-medium .obs-workflow-filters { grid-template-columns: auto minmax(0, 1fr); }
.obs-main-tabs.obs-content-medium .obs-workflow-filters > .form { grid-column: 1 / -1; }
.obs-main-tabs.obs-content-narrow .ov-kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)) !important; }
.obs-main-tabs.obs-content-narrow .obs-workflow-body { grid-template-columns: minmax(0, 1fr); }
.obs-main-tabs.obs-content-narrow .obs-workflow-body > .column { grid-column: 1; grid-row: auto !important; }
.obs-main-tabs.obs-content-narrow .wf-toc-sidebar { max-height: 140px; position: static; }
.obs-main-tabs.obs-content-narrow .filter-group { flex-direction: column; gap: 6px; }
.obs-main-tabs.obs-content-narrow .filter-group-label { flex-basis: auto; }
.obs-main-tabs.obs-content-narrow .filter-options { width: 100%; max-height: 100px; }
"""

_JS = """() => {
  // Native charts and callback state are light-only in TrajViz 0.2.
  document.documentElement.style.colorScheme = 'light';
  document.documentElement.classList.remove('dark');
  document.body?.classList.remove('dark');
  const root = document.querySelector('.gradio-container');
  if (!root || root.dataset.obsLayoutBound) return [];
  root.dataset.obsLayoutBound = 'true';
  let queued = false;
  let sourceSettled = false;
  let measuredTabs = null;
  const resize = new ResizeObserver(entries => {
    for (const {target, contentRect} of entries) {
      target.classList.toggle('obs-content-medium', contentRect.width < 1000);
      target.classList.toggle('obs-content-narrow', contentRect.width < 600);
    }
  });
  root.addEventListener('keydown', event => {
    const step = event.target.closest?.('.insight-step-link');
    if (step && (event.key === 'Enter' || event.key === ' ')) {
      event.preventDefault();
      step.click();
    }
  });
  function polish() {
    queued = false;
    const tabs = root.querySelector('.obs-main-tabs');
    if (tabs && tabs !== measuredTabs) {
      resize.disconnect();
      resize.observe(tabs);
      measuredTabs = tabs;
    }
    // Keep upload controls mounted for the initial preload, then tuck them away.
    if (!sourceSettled && root.querySelector('.obs-main-tabs [role="tab"]')) {
      sourceSettled = true;
      const button = root.querySelector('.obs-source-controls > button');
      if (button?.classList.contains('open')) button.click();
    }
    root.querySelectorAll('.insight-step-link').forEach(step => {
      step.setAttribute('tabindex', '0');
      step.setAttribute('role', 'button');
      step.setAttribute('aria-label', `Inspect step ${step.textContent.trim()} in Workflow`);
    });
    root.querySelectorAll('.obs-chart').forEach(el => {
      el.setAttribute('tabindex', '0');
      el.setAttribute('role', 'region');
      el.setAttribute('aria-label', el.querySelector('label')?.textContent.trim() || 'Interactive chart');
      const plot = el.querySelector('.js-plotly-plot');
      if (window.Plotly && plot?.data?.filter(trace => trace.showlegend !== false).length > 8
          && plot.layout?.legend?.maxheight !== 96) {
        window.Plotly.relayout(plot, {'legend.maxheight': 96}).catch(() => {});
      }
    });
    const chart = root.querySelector('#diag-file-chart .js-plotly-plot');
    const axis = chart?.layout?.yaxis;
    const count = axis?.categoryarray?.length;
    if (window.Plotly && count > 20) {
      const update = {};
      if (chart.layout.legend?.maxheight !== 96) update['legend.maxheight'] = 96;
      if (axis.autorange !== false) {
        update['yaxis.range'] = axis.autorange === 'reversed' ? [count - 0.5, -0.5] : [-0.5, count - 0.5];
        update['yaxis.autorange'] = false;
      }
      if (Object.keys(update).length) window.Plotly.relayout(chart, update).catch(() => {});
    }
  }
  const observer = new MutationObserver(() => {
    if (!queued) { queued = true; requestAnimationFrame(polish); }
  });
  observer.observe(root, {childList: true, subtree: true});
  polish();
  return [];
} """

_GUIDE = {
    "insight": """<section class="obs-guide" aria-label="Dashboard guide">
<div class="obs-guide-header"><h1>TrajViz Obs</h1><p>Session health, evidence, and comparisons.</p></div>
<details><summary>How to use this dashboard</summary><div class="obs-guide-grid">
<p><strong>1 · Overview</strong>Start with Summary for health and issues. Contents opens tools, agents, context, diagnostics, and labels.</p>
<p><strong>2 · Workflow</strong>Follow individual steps. Click an issue’s step number to jump to its evidence.</p>
<p><strong>3 · Comparison</strong>Compare runs. Patterns, Raw Data, and Labels provide the supporting detail.</p>
</div><p class="obs-model-note">For Claude Code’s model, use <code>/obs analyze</code> or <code>/obs fixes</code>.
The browser’s AI controls use your <code>ANALYZE_*</code> environment settings.</p></details></section>""",
    "converge": """<section class="obs-guide" aria-label="Comparison guide">
<div class="obs-guide-header"><h1>Compare trajectories</h1><p>Choose a comparison mode below.</p></div>
<details><summary>How to compare runs</summary><div class="obs-guide-grid">
<p><strong>1 · Pairwise</strong>Load a reference and a compared run. Add a patch to check write recall.</p>
<p><strong>2 · Batch</strong>Compare multiple pairs with a manifest. Paths resolve within the selected data root.</p>
<p><strong>3 · Before/After</strong>Load two manifests to see how an intervention changes results.</p>
</div></details></section>""",
}


def presentation(kind: str, upstream: dict[str, Any]) -> dict[str, Any]:
    """Append plugin styles without dropping upstream theme, head, or scripts."""
    # Layout JS is attached to the app below, so an upstream launch script is
    # retained exactly and runs independently.
    # Gradio scopes media rules under .gradio-container .contain. A repeated
    # root selector there never matches; let Gradio scope component selectors.
    css = _CSS.replace(".obs-dashboard ", "").replace(".obs-dashboard", ".gradio-container")
    # Both native dashboards render light charts. Their OS-dark media rules
    # otherwise mix dark cards with light plots and Gradio controls.
    native_css = (upstream.get("css") or "").replace("@media (prefers-color-scheme: dark)", "@media not all")
    return {**upstream, "css": f'{native_css}\n{css}'}


def decorate_dashboard(app: gr.Blocks, kind: str) -> gr.Blocks:
    """Wrap a native dashboard while preserving all its components and events."""
    import gradio as gr

    for block in app.blocks.values():
        for attribute in ("value", "label", "info", "placeholder"):
            value = getattr(block, attribute, None)
            if isinstance(value, str):
                if isinstance(block, gr.HTML) and attribute == "value":
                    value = value.replace("DECAF capability failure attribution", "Capability failure attribution")
                setattr(block, attribute, attribution_message(value))
        if kind == "converge" and isinstance(block, gr.File):
            block.height = 140
        extra = "obs-chart" if isinstance(block, gr.Plot) else "obs-markdown" if isinstance(block, gr.Markdown) else None
        if extra:
            block.elem_classes = [*(block.elem_classes or []), extra]
        if isinstance(block, gr.Row) and sum(isinstance(child, gr.Plot) for child in block.children) > 1:
            block.elem_classes = [*(block.elem_classes or []), "obs-chart-row"]
        if isinstance(block, gr.Row) and any(child.elem_id == "wf-filter-chips" for child in block.children):
            block.elem_classes = [*(block.elem_classes or []), "obs-workflow-filters"]
            for child in block.children:
                if isinstance(child, gr.Button) and child.value == "TOC":
                    child.value = "Step index"
        if isinstance(block, gr.Row) and any(
            isinstance(child, gr.Column) and any(part.elem_id == "wf-toc-container" for part in child.children)
            for child in block.children
        ):
            block.elem_classes = [*(block.elem_classes or []), "obs-workflow-body"]
    for child in app.children:
        if isinstance(child, gr.Tabs):
            child.elem_classes = [*(child.elem_classes or []), "obs-main-tabs"]
    with gr.Blocks(title="TrajViz Obs", analytics_enabled=False,
                   elem_classes=["trajectory-viz", "obs-dashboard"]) as page:
        gr.HTML(_GUIDE[kind], elem_classes=["obs-guide-block"])
        app.render()
        page.load(fn=None, js=_JS)
    # Keep the original upload component and every callback referring to it.
    # Only move the native source row under a disclosure in this app's tree.
    if kind == "insight":
        _focus_overview(page)
        source = next((child for child in page.children if "upload-row" in (child.elem_classes or [])), None)
        if source is not None:
            position = page.children.index(source)
            with page:
                controls = gr.Accordion("Session source and labels", open=True,
                                        elem_classes=["obs-source-controls"])
            page.children.remove(controls)
            page.children[position] = controls
            branding = next((child for child in page.children if isinstance(child, gr.HTML)
                             and "Coding Agent Trajectory Analysis Dashboard" in str(child.value)), None)
            if branding is not None:
                page.children.remove(branding)
                controls.children.append(branding)
            controls.children.append(source)
    elif kind == "converge":
        # The native comparison app is one long page. Put its unchanged sections
        # in tabs so empty pairwise plots do not bury batch and before/after.
        body = page.children[1:]
        for child in body:
            if isinstance(child, gr.Markdown) and str(child.value).startswith("# Converge\n"):
                child.value = "Upload a reference and a compared run, then select **Compare**. The anchor patch is optional."
        batch = next((i for i, child in enumerate(body) if isinstance(child, gr.Markdown)
                      and str(child.value).startswith("## Batch Comparison")), None)
        before_after = next((i for i, child in enumerate(body) if isinstance(child, gr.Markdown)
                             and str(child.value).startswith("## Before/After Comparison")), None)
        if batch is not None and before_after is not None and 0 < batch < before_after:
            sections = [body[:batch], body[batch:before_after], body[before_after:]]
            with page, gr.Tabs(elem_classes=["obs-main-tabs"]) as tabs:
                panels = [gr.Tab(label) for label in ("Pairwise", "Batch", "Before/After")]
            page.children = [page.children[0], tabs]
            for panel, children in zip(panels, sections, strict=True):
                panel.children.extend(children)
    return page


def _focus_overview(page: gr.Blocks) -> None:
    """Keep navigation above health cards; show session diagnostics in Summary.

    Move the original components rather than rebuilding HTML or changing any
    callback outputs. If the upstream structure changes, retain its layout.
    """
    import gradio as gr

    tabs = next((child for child in page.children if isinstance(child, gr.Tabs)), None)
    overview = next((child for child in tabs.children if child.label == "Overview"), None) if tabs else None
    layout = next((child for child in overview.children
                   if "overview-content-layout" in (child.elem_classes or [])), None) if overview else None
    content = next((child for child in layout.children
                    if "overview-section-content" in (child.elem_classes or [])), None) if layout else None
    summary = next((child for child in content.children if isinstance(child, gr.Column) and child.visible is True),
                   None) if content else None
    if summary is None:
        return
    cards = [child for child in page.children if "overview-kpi-strip" in (child.elem_classes or [])]
    warnings = [child for child in page.children if isinstance(child, gr.Column) and any(
        "summary-banner" in (part.elem_classes or []) for part in child.children)]
    findings = [child for child in overview.children if child is not layout]
    for child in [*cards, *warnings]:
        page.children.remove(child)
    for child in findings:
        overview.children.remove(child)
    for column in warnings:
        column.elem_classes = [*(column.elem_classes or []), "obs-summary-status"]
    summary.children[:0] = [*cards, *warnings, *findings]
