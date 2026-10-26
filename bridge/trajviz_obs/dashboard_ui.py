"""Plugin presentation around the full native TrajViz dashboards.

Reuse every upstream component and callback. Only the app-local layout tree is
rearranged: source controls go in a disclosure, plots get scroll regions, and
the native tabs remain the way into all TrajViz features.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import gradio as gr

_CSS = """
.obs-dashboard { width: 100% !important; min-width: 0 !important; max-width: 1440px !important;
  margin: auto; padding: clamp(12px, 2vw, 24px) !important; }
.obs-dashboard, .obs-dashboard * { box-sizing: border-box; }
.obs-dashboard .main, .obs-dashboard .wrap, .obs-dashboard .contain,
.obs-dashboard .tabs, .obs-dashboard .tabitem, .obs-dashboard .row, .obs-dashboard .column {
  min-width: 0 !important; max-width: 100%;
}
.obs-dashboard .obs-guide { padding: 16px 20px; border: 1px solid #dce3ef;
  border-radius: 12px; background: #f8fafc; color: #0f172a; margin-bottom: 8px; }
.obs-dashboard .obs-guide h1 { font-size: 20px; margin: 0 0 12px; line-height: 1.3; }
.obs-dashboard .obs-guide-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; }
.obs-dashboard .obs-guide p { font-size: 13px; line-height: 1.5; margin: 0; overflow-wrap: anywhere; }
.obs-dashboard .obs-guide strong { display: block; margin-bottom: 3px; }
.obs-dashboard .obs-model-note { font-size: 12px !important; margin-top: 12px !important; color: #475569; }
.obs-dashboard .obs-model-note strong { display: inline; }
.obs-dashboard .obs-source-controls { margin-bottom: 12px; }
.obs-dashboard .upload-row { padding-top: 8px; }
.obs-dashboard .ov-kpi-grid { grid-template-columns: repeat(auto-fit, minmax(min(100%, 200px), 1fr)); }
.obs-dashboard .ov-kpi-card { min-width: 0; overflow-wrap: anywhere; }
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
.obs-dashboard .obs-chart-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 640px), 1fr)); }
.obs-dashboard .obs-markdown { min-width: 0 !important; max-width: 100%; overflow-x: auto; }
.obs-dashboard .obs-markdown table { width: max-content; min-width: 100%; }
.obs-dashboard .obs-markdown th, .obs-dashboard .obs-markdown td { max-width: 360px; overflow-wrap: break-word; }
.obs-dashboard .obs-markdown > div > div > div { max-width: 100%; overflow-wrap: anywhere; }
.obs-dashboard .cvg-report { min-width: 0; max-width: 100%; overflow-x: auto; }
.obs-dashboard .obs-chart:focus-visible { outline: 2px solid #2563eb; outline-offset: 2px; }
@media (max-width: 1100px) {
  .obs-dashboard .ov-kpi-grid { grid-template-columns: repeat(3, minmax(0, 1fr)) !important; }
}
@media (max-width: 768px) {
  .obs-dashboard .obs-guide { padding: 14px; }
  .obs-dashboard .obs-guide-grid { grid-template-columns: 1fr; gap: 10px; }
  .obs-dashboard .ov-kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)) !important; }
  .obs-dashboard .overview-issues-body { max-height: 340px; }
}
@media (max-width: 420px) {
  .obs-dashboard .ov-kpi-grid { grid-template-columns: minmax(0, 1fr) !important; }
}
"""

_JS = """() => {
  const root = document.querySelector('.gradio-container');
  if (!root || root.dataset.obsLayoutBound) return [];
  root.dataset.obsLayoutBound = 'true';
  let queued = false;
  let sourceSettled = false;
  function polish() {
    queued = false;
    // Keep upload controls mounted for the initial preload, then tuck them away.
    if (!sourceSettled && root.querySelector('.obs-main-tabs [role="tab"]')) {
      sourceSettled = true;
      const button = root.querySelector('.obs-source-controls > button');
      if (button?.classList.contains('open')) button.click();
    }
    root.querySelectorAll('.obs-chart').forEach(el => {
      el.setAttribute('tabindex', '0');
      el.setAttribute('role', 'region');
      el.setAttribute('aria-label', el.querySelector('label')?.textContent.trim() || 'Interactive chart');
    });
    const chart = root.querySelector('#diag-file-chart .js-plotly-plot');
    const axis = chart?.layout?.yaxis;
    const count = axis?.categoryarray?.length;
    if (window.Plotly && count > 20) {
      const update = {};
      if (chart.layout.legend?.maxheight !== 120) update['legend.maxheight'] = 120;
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
<h1>Explore your session</h1><div class="obs-guide-grid">
<p><strong>1 · Overview</strong>Review health and issues, then use Contents for tools, agents, context, and diagnostics.</p>
<p><strong>2 · Workflow</strong>Follow individual steps. Click an issue’s step number to jump to its evidence.</p>
<p><strong>3 · Comparison</strong>Compare runs. Patterns, Raw Data, and Labels provide the supporting detail.</p>
</div><p class="obs-model-note">For Claude Code’s model, use <code>/obs analyze</code> or <code>/obs fixes</code>.
The browser’s AI controls use your <code>ANALYZE_*</code> environment settings.</p></section>""",
    "converge": """<section class="obs-guide" aria-label="Comparison guide">
<h1>Compare trajectories</h1><div class="obs-guide-grid">
<p><strong>1 · Pairwise</strong>Load a reference and a compared run. Add a patch to check write recall.</p>
<p><strong>2 · Batch</strong>Compare multiple pairs with a manifest. Paths resolve within the selected data root.</p>
<p><strong>3 · Before/After</strong>Load two manifests to see how an intervention changes results.</p>
</div></section>""",
}


def presentation(kind: str, upstream: dict[str, Any]) -> dict[str, Any]:
    """Append plugin styles without dropping upstream theme, head, or scripts."""
    # Layout JS is attached to the app below, so an upstream launch script is
    # retained exactly and runs independently.
    # Gradio scopes media rules under .gradio-container .contain. A repeated
    # root selector there never matches; let Gradio scope component selectors.
    css = _CSS.replace(".obs-dashboard ", "").replace(".obs-dashboard", ".gradio-container")
    return {**upstream, "css": f'{upstream.get("css") or ""}\n{css}'}


def decorate_dashboard(app: gr.Blocks, kind: str) -> gr.Blocks:
    """Wrap a native dashboard while preserving all its components and events."""
    import gradio as gr

    for block in app.blocks.values():
        extra = "obs-chart" if isinstance(block, gr.Plot) else "obs-markdown" if isinstance(block, gr.Markdown) else None
        if extra:
            block.elem_classes = [*(block.elem_classes or []), extra]
        if isinstance(block, gr.Row) and sum(isinstance(child, gr.Plot) for child in block.children) > 1:
            block.elem_classes = [*(block.elem_classes or []), "obs-chart-row"]
    for child in app.children:
        if isinstance(child, gr.Tabs):
            child.elem_classes = [*(child.elem_classes or []), "obs-main-tabs"]
    with gr.Blocks(title="TrajViz Obs", analytics_enabled=False,
                   elem_classes=["trajectory-viz", "obs-dashboard"]) as page:
        gr.HTML(_GUIDE[kind])
        app.render()
        page.load(fn=None, js=_JS)
    # Keep the original upload component and every callback referring to it.
    # Only move the native source row under a disclosure in this app's tree.
    if kind == "insight":
        source = next((child for child in page.children if "upload-row" in (child.elem_classes or [])), None)
        if source is not None:
            position = page.children.index(source)
            with page:
                controls = gr.Accordion("Session source and labels · reload or open another trajectory", open=True,
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
