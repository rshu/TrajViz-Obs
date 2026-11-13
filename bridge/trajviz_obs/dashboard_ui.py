"""Plugin presentation around the full native TrajViz dashboards.

Reuse every upstream component and callback. Keep navigation before the body,
put session health in Overview's Summary, and bound dense charts and filters.
"""

from __future__ import annotations

import functools
import inspect
import json
import re
import textwrap
from typing import TYPE_CHECKING, Any, get_type_hints

from .attribution_ui import public_translation_head
from .compat import attribution_message, write_dashboard_report
from .dashboard_timeline import TIMELINE_TRANSLATIONS, timeline_builder
from .errors import BridgeError
from .native_quality import _clone_native_function

if TYPE_CHECKING:
    import gradio as gr

_MAX_EXPANDED_TOOL_CHARS = 200_000
_EMPTY_CHART = {
    "token-chart": ("y", "No step token usage was recorded."),
    "duration-chart": ("y", "No step durations were recorded."),
    "gantt-chart": ("x", "No drawable execution intervals. A recorded start and positive duration are required."),
}
_GANTT_STEP = re.compile(r"^<b>Step (\d+)</b> ")
_GANTT_TOKEN_COUNT = re.compile(r"(?<=<br>)-?[\d,]+ tokens$")

_CSS = """
.obs-dashboard .main { width: 100% !important; min-width: 0 !important; max-width: 1440px !important;
  margin: auto; padding: clamp(12px, 2vw, 24px) !important; }
.obs-dashboard, .obs-dashboard * { box-sizing: border-box; }
.obs-dashboard .main, .obs-dashboard .wrap, .obs-dashboard .contain,
.obs-dashboard .tabs, .obs-dashboard .tabitem, .obs-dashboard .row, .obs-dashboard .column {
  min-width: 0 !important; max-width: 100%;
}
.obs-dashboard .obs-guide { padding: 12px 16px; border: 1px solid var(--ov-border, #dce3ef);
  border-radius: 10px; background: var(--ov-nav-bg, #f8fafc); color: var(--ov-text, #0f172a); }
.obs-dashboard .obs-dashboard-heading { align-items: start; }
.obs-dashboard .obs-dashboard-heading > #tv-app-header { padding: 0; margin: 0; flex: 0 0 auto; }
.obs-dashboard .obs-dashboard-heading > #tv-app-header .form { flex: 0 0 auto; }
.obs-dashboard .obs-guide-header { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 16px; }
.obs-dashboard .obs-guide h1 { font-size: 19px; margin: 0; line-height: 1.4; }
.obs-dashboard .obs-guide details { margin-top: 6px; }
.obs-dashboard .obs-guide summary { color: var(--ov-accent, #1d4ed8); font-size: 12px; cursor: pointer; }
.obs-dashboard .obs-guide summary:focus-visible { outline: 2px solid #2563eb; outline-offset: 3px; }
.obs-dashboard .obs-guide-block .html-container { padding: 0 !important; }
.obs-dashboard .obs-guide-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px; margin-top: 12px; }
.obs-dashboard .obs-guide p { font-size: 13px; line-height: 1.5; margin: 0; overflow-wrap: anywhere; }
.obs-dashboard .obs-guide-charts { font-size: 12px; color: var(--ov-accent, #1d4ed8); text-decoration: underline; }
.obs-dashboard #obs-summary-chart-guide { scroll-margin-top: 76px; }
.obs-dashboard .obs-guide strong { display: block; margin-bottom: 3px; }
.obs-dashboard .obs-model-note { font-size: 12px !important; margin-top: 12px !important;
  color: var(--ov-muted, #475569); }
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
/* Let issue cards and their actions scroll with the page. */
.obs-dashboard .overview-issues-body { max-height: none; overflow: visible; scrollbar-gutter: auto; }
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
.obs-dashboard .obs-chart > .js-plotly-plot { min-width: 0; width: 100%; }
.obs-dashboard .obs-chart:has(> .empty) { display: none !important; }
.obs-dashboard .obs-chart-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 640px), 1fr)); }
.obs-dashboard .obs-summary-charts { grid-template-columns: repeat(auto-fit, minmax(min(100%, 420px), 1fr)); }
.obs-dashboard .token-step-explain { min-width: 0; overflow-wrap: anywhere; }
.obs-dashboard .tv-token-step-detail { scroll-margin-top: 64px; }
.obs-dashboard .obs-markdown { min-width: 0 !important; max-width: 100%; overflow-x: auto; }
.obs-dashboard .obs-markdown table { width: max-content; min-width: 100%; }
.obs-dashboard .obs-markdown th, .obs-dashboard .obs-markdown td { max-width: 360px; overflow-wrap: break-word; }
.obs-dashboard .obs-markdown > div > div > div { max-width: 100%; overflow-wrap: anywhere; }
.obs-dashboard .cvg-report { min-width: 0; max-width: 100%; overflow-x: auto; }
.obs-dashboard .obs-chart:focus-visible { outline: 2px solid #2563eb; outline-offset: 2px; }
.obs-dashboard .insight-step-link:focus-visible { outline: 2px solid #2563eb; outline-offset: 2px; }
.obs-dashboard .obs-workflow-filters { display: grid; align-items: start; gap: 12px;
  grid-template-areas: "toc view filters search";
  grid-template-columns: max-content minmax(210px, 250px) minmax(0, 1fr) minmax(160px, .4fr); }
.obs-dashboard .obs-workflow-filters > button { grid-area: toc;
  align-self: start !important; justify-self: start; height: auto !important; }
.obs-dashboard .obs-workflow-filters > .form:has(> .obs-workflow-view) { grid-area: view;
  min-width: 0 !important; background: transparent !important; border: 0; box-shadow: none; }
.obs-dashboard .obs-workflow-view { flex: 1 1 auto !important; width: 100%; min-width: 0 !important; }
.obs-dashboard .obs-workflow-filters > #wf-filter-chips { grid-area: filters; min-width: 0; }
.obs-dashboard .obs-workflow-filters > .form:has(> .wf-toolbar-search) { grid-area: search; min-width: 0 !important; }
.obs-dashboard .filter-group { align-items: start; }
.obs-dashboard .filter-options { min-width: 0; max-height: 128px; overflow-y: auto; padding: 2px; }
.obs-dashboard .filter-chip { flex-shrink: 0; }
.obs-dashboard .obs-workflow-body { display: grid; align-items: start; gap: 16px;
  grid-template-columns: 140px minmax(0, 1fr) minmax(280px, .9fr); }
.obs-dashboard .wf-toc-sidebar { width: 100%; top: 56px; }
.obs-dashboard .wf-toc-sidebar .toc-entry { gap: 3px; padding-right: 4px; }
.obs-dashboard .wf-toc-sidebar .toc-num { min-width: 22px; }
.obs-dashboard .detail-panel { top: 56px; }
@media (max-width: 1100px) {
  .obs-dashboard .ov-kpi-grid { grid-template-columns: repeat(3, minmax(0, 1fr)) !important; }
  .obs-dashboard .obs-workflow-body { grid-template-columns: 128px minmax(0, 1fr); }
  .obs-dashboard .obs-workflow-body > .column:first-child { grid-row: 1 / span 2; }
  .obs-dashboard .obs-workflow-body > .detail-panel { grid-column: 2; position: relative; top: auto; }
  .obs-dashboard .obs-workflow-body .wf-scroll { max-height: 50vh; }
}
@media (max-width: 768px) {
  .obs-dashboard #analysis-sidebar.right { width: calc(100vw - 32px) !important;
    max-width: calc(100vw - 32px) !important; right: calc(32px - 100vw) !important; }
  .obs-dashboard .obs-file-row { flex-direction: column; }
  .obs-dashboard .obs-file-row > .column,
  .obs-dashboard .obs-file-row > .block,
  .obs-dashboard .obs-file-row > .form { width: 100%; flex-basis: auto !important; }
  .obs-dashboard #analysis-sidebar.right:not(.open) .toggle-button::after { content: "AI analysis"; }
  .obs-dashboard .obs-guide { padding: 10px 12px; }
  .obs-dashboard .obs-guide-grid { grid-template-columns: 1fr; gap: 10px; }
  .obs-dashboard .ov-kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)) !important; }
  .obs-dashboard .overview-section-nav { padding: 4px; margin-bottom: 8px; }
  .obs-dashboard .overview-nav-title { padding: 4px 8px; margin-bottom: 0; }
  .obs-dashboard .overview-section-radio > .wrap { scrollbar-width: thin; }
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
.obs-main-tabs.obs-content-medium .obs-workflow-body { grid-template-columns: 128px minmax(0, 1fr); }
.obs-main-tabs.obs-content-medium .obs-workflow-body > .column:first-child { grid-row: 1 / span 2; }
.obs-main-tabs.obs-content-medium .obs-workflow-body > .detail-panel { grid-column: 2; position: relative; top: auto; }
.obs-main-tabs.obs-content-medium .obs-workflow-body .wf-scroll { max-height: 50vh; }
.obs-main-tabs.obs-content-medium .obs-workflow-filters {
  grid-template-areas: "toc view filters" ". search filters";
  grid-template-columns: max-content minmax(240px, .7fr) minmax(0, 1fr); }
.obs-main-tabs.obs-content-narrow .ov-kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)) !important; }
.obs-main-tabs.obs-content-narrow .obs-workflow-filters {
  grid-template-areas: "toc view" "filters filters" "search search";
  grid-template-columns: max-content minmax(0, 1fr); }
.obs-main-tabs.obs-content-narrow .obs-workflow-body { grid-template-columns: minmax(0, 1fr); }
.obs-main-tabs.obs-content-narrow .obs-workflow-body > .column { grid-column: 1; grid-row: auto !important; }
.obs-main-tabs.obs-content-narrow .wf-toc-sidebar { max-height: 140px; position: static; }
.obs-main-tabs.obs-content-narrow .filter-group { flex-direction: column; gap: 6px; }
.obs-main-tabs.obs-content-narrow .filter-group-label { flex-basis: auto; }
.obs-main-tabs.obs-content-narrow .filter-options { width: 100%; max-height: 100px; }
"""

_LIGHT = """document.documentElement.style.colorScheme = 'light';
  document.documentElement.classList.remove('dark');
  document.body?.classList.remove('dark');"""
_DARK = """document.documentElement.style.colorScheme = 'dark';
  document.documentElement.classList.add('dark', 'tv-theme-dark');
  document.body?.classList.add('dark');"""

_JS = "() => {\n  " + _LIGHT + """
  const root = document.querySelector('.gradio-container');
  if (!root || root.dataset.obsLayoutBound) return [];
  root.dataset.obsLayoutBound = 'true';
  // Hidden overflow can scroll this root when focusing the mobile tab strip.
  // Clip it without creating a second, invisible horizontal scroll container.
  root.style.overflow = 'clip';
  let queued = false;
  let sourceSettled = false;
  let measuredTabs = null;
  let tokenPanel = null;
  const tokenSelection = new MutationObserver(records => {
    // TrajViz toggles the selected step's display when a token bar is clicked.
    const detail = records.find(({target}) => target.matches('.tv-token-step-detail')
      && target.getClientRects().length)?.target;
    if (!detail) return;
    const {top, bottom} = detail.getBoundingClientRect();
    if (bottom <= 56 || top >= window.innerHeight - 64) detail.scrollIntoView({block: 'start'});
  });
  const resize = new ResizeObserver(entries => {
    for (const {target, contentRect} of entries) {
      target.classList.toggle('obs-content-medium', contentRect.width < 1000);
      target.classList.toggle('obs-content-narrow', contentRect.width < 600);
    }
  });
  root.addEventListener('click', event => {
    const link = event.target.closest?.('.obs-guide-charts');
    if (!link || !window.tvClickMainTab) return;
    event.preventDefault();
    window.tvClickMainTab('Overview');
    const start = performance.now();
    let selectedSummary = false;
    const showGuide = () => {
      const summary = root.querySelector('.overview-section-radio input[value="Summary"]');
      if (summary && !summary.checked && !selectedSummary) {
        selectedSummary = true;
        summary.click();
      }
      const guide = root.querySelector('#obs-summary-chart-guide');
      const tip = guide?.querySelector('.section-guide-tip');
      if (tip?.getClientRects().length) {
        guide.scrollIntoView({block: 'start'});
        tip.focus({preventScroll: true});
      } else if (performance.now() - start < 3000) {
        requestAnimationFrame(showGuide);
      }
    };
    requestAnimationFrame(showGuide);
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
    const panel = root.querySelector('.token-step-explain');
    if (panel !== tokenPanel) {
      tokenSelection.disconnect();
      if (panel) tokenSelection.observe(panel, {subtree: true, attributeFilter: ['style']});
      tokenPanel = panel;
    }
    // Gradio moves narrow-screen tabs into a dropdown without role="tab".
    // Keep upstream step links, chart clicks, and history navigation working.
    if (typeof window.tvClickMainTab === 'function' && !window.tvClickMainTab.obsOverflowAware) {
      const original = window.tvClickMainTab;
      const clickTab = label => {
        const button = Array.from(root.querySelectorAll(
          '.obs-main-tabs [role="tab"], .obs-main-tabs .overflow-dropdown button'
        )).find(el => el.textContent.trim() === label);
        if (button) button.click();
        else original(label);
      };
      clickTab.obsOverflowAware = true;
      window.tvClickMainTab = clickTab;
    }
    if (typeof window.tvGotoWorkflowStep === 'function' && !window.tvGotoWorkflowStep.obsHistoryAware
        && typeof window.tvPushTabReturnPoint === 'function') {
      const originalJump = window.tvGotoWorkflowStep;
      const originalReturn = window.tvPushTabReturnPoint;
      let jumping = false;
      // Save the source before the native jump pushes its step URL. Dropdown
      // tabs miss the native capture handler; visible tabs would add it twice.
      const gotoStep = (index, options) => {
        const source = window.tvActiveMainTab?.();
        if (!options?.restore && source && source !== 'Workflow') {
          history.replaceState({...history.state, tvTab: source}, '', window.location.href);
        }
        jumping = true;
        try { return originalJump(index, options); }
        finally { jumping = false; }
      };
      window.tvPushTabReturnPoint = label => { if (!jumping) return originalReturn(label); };
      gotoStep.obsHistoryAware = true;
      window.tvGotoWorkflowStep = gotoStep;
    }
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
    root.querySelectorAll('.obs-timeline-axis label').forEach(label => {
      const choice = {skip_idle: 'Compress idle gaps (default)', wallclock: 'True wall-clock'};
      const key = choice[label.querySelector('input[type=radio]')?.value];
      const caption = label.querySelector('span');
      if (key && caption && caption.getAttribute('data-tv-i18n') !== key) caption.setAttribute('data-tv-i18n', key);
    });
    root.querySelectorAll('.insight-step-link').forEach(step => {
      step.setAttribute('tabindex', '0');
      step.setAttribute('role', 'button');
      if (!step.closest('.token-step-explain')) {
        step.setAttribute('aria-label', `Inspect step ${step.textContent.trim()} in Workflow`);
      }
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
<div class="obs-guide-header"><h1>TrajViz Obs</h1><p>Session health, evidence, and comparisons.</p>
<a class="obs-guide-charts" href="#obs-summary-chart-guide"><span data-tv-i18n="" data-tv-i18n-en="Chart guide" data-tv-i18n-zh="图表说明">Chart guide</span></a></div>
<details><summary>How to use this dashboard</summary><div class="obs-guide-grid">
<p><strong>1 · Overview</strong>Review health and issues in Summary. Choose <b>Analyze issue with AI</b> on a finding to discuss it in the sidebar. Find context growth in Deep Dive.</p>
<p><strong>2 · Workflow</strong>Follow individual steps and switch to Expanded for full evidence. Click an issue’s step number to jump to its evidence.</p>
<p><strong>3 · Comparison</strong>Compare runs. Patterns, Raw Data, and Labels provide the supporting detail.</p>
</div><p class="obs-model-note">Use <code>/obs analyze</code> or <code>/obs fixes</code> for your host’s configured model.
The browser’s AI controls use the connection in Settings, with environment settings as defaults.</p></details></section>""",
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
    # The plugin chooses an explicit palette. Ignore OS-dark media rules so
    # an explicit light page stays light and a dark page uses tv-theme-dark.
    native_css = (upstream.get("css") or "").replace("@media (prefers-color-scheme: dark)", "@media not all")
    result = {**upstream, "css": f'{native_css}\n{css}'}
    if kind == "insight" and isinstance(result.get("head"), str):
        result["head"] = public_translation_head(result["head"], extra=TIMELINE_TRANSLATIONS)
    return result


def configure_insight(app: gr.Blocks, *, dark: bool = False, inline_plotly: bool = False) -> None:
    """Use the native callbacks with options scoped to this Insight app."""
    from agent_trajviz.insight.session import LoadedSession
    from agent_trajviz.insight.ui import upload

    from .native_quality import project_dashboard_session

    if dark:
        _set_dark_theme(app)
    _bound_expanded_workflow(app)
    _scope_overview_issues(app)
    _scope_gantt_hover(app)
    original = upload.prepare_html_export
    exports = [entry for entry in app.fns.values() if entry.fn is original]
    if not exports:
        raise BridgeError("dashboard_incompatible", "TrajViz's browser HTML export changed.")

    def write_report(source, *, dest, dark):
        if isinstance(source, LoadedSession):
            source = project_dashboard_session(source)
        return write_dashboard_report(upload.write_report_file, source, dest=dest, dark=dark, inline_plotly=inline_plotly)

    callback = _replace_native_binding(original, "write_report_file", write_report)
    for entry in exports:
        entry.fn = callback


def _replace_native_binding(original, name, replacement):
    if name not in original.__globals__ or name not in original.__code__.co_names:
        raise BridgeError("dashboard_incompatible", f"TrajViz's {original.__name__} browser callback changed.")
    return functools.update_wrapper(_clone_native_function(original, {name: replacement}), original)


def _scope_overview_issues(app: gr.Blocks) -> None:
    """Use the displayed findings when a native action rebuilds Overview issues."""
    from agent_trajviz.insight.session import session_from_run
    from agent_trajviz.insight.ui import overview_tab, settings

    from .native_quality import project_dashboard_session

    def displayed_session(run, path=None):
        return project_dashboard_session(session_from_run(run, path))

    offline = _replace_native_binding(settings._offline_issues, "session_from_run", displayed_session)
    issue_callbacks = {
        function: _replace_native_binding(function, "session_from_run", displayed_session)
        for function in (overview_tab.prepare_issues_agent_download, overview_tab.on_suggest_fixes)
    }
    settings_callbacks = {"on_apply_settings", "on_reset_settings"}
    found = set()
    for entry in app.fns.values():
        original = entry.fn
        if original in issue_callbacks:
            entry.fn = issue_callbacks[original]
            found.add(original.__name__)
        elif (getattr(original, "__module__", "") == settings.__name__
              and original.__name__ in settings_callbacks):
            entry.fn = _replace_native_binding(original, "_offline_issues", offline)
            found.add(original.__name__)
    if found != settings_callbacks | {function.__name__ for function in issue_callbacks}:
        raise BridgeError("dashboard_incompatible", "TrajViz's Overview issue callbacks changed.")


def _scope_gantt_hover(app: gr.Blocks) -> None:
    """Use prompt-aware timing and disclose missing tokens in Gantt hover text."""
    from agent_trajviz.insight.ui.shared import safe_callback

    from .native_quality import dashboard_token_total_observed

    loaders = [entry for entry in app.fns.values() if getattr(entry.fn, "__name__", "") == "_do_load"]
    axes = [entry for entry in app.fns.values() if getattr(entry.fn, "__name__", "") == "on_gantt_axis_change"]
    if len(loaders) != 1 or len(axes) != 1:
        raise BridgeError("dashboard_incompatible", "TrajViz's Execution Timeline callbacks changed.")
    loader, axis = loaders[0], axes[0]
    original_load, original_axis = loader.fn, axis.fn
    slots = inspect.getclosurevars(inspect.unwrap(original_load)).nonlocals.get("slots")
    if (not isinstance(slots, dict) or slots.get("gantt_chart") not in loader.outputs
            or slots.get("state_steps") not in loader.outputs or axis.outputs != [slots["gantt_chart"]]):
        raise BridgeError("dashboard_incompatible", "TrajViz's Execution Timeline outputs changed.")
    chart, steps_state = slots["gantt_chart"], slots["state_steps"]
    radio = slots["gantt_axis_mode"]
    radio.elem_classes = [*(radio.elem_classes or []), "obs-timeline-axis"]
    radio.choices = [("Compress idle gaps (default)" if value == "skip_idle" else label, value)
                     for label, value in radio.choices]
    build = safe_callback("Execution Timeline")(_replace_native_binding(
        inspect.unwrap(original_axis), "build_execution_gantt_chart", timeline_builder(),
    ))

    def mark_missing(fig, steps):
        missing = {step["index"] for step in (steps or []) if not dashboard_token_total_observed(step)}
        if not missing:
            return fig

        def hover_label(label):
            match = _GANTT_STEP.match(label)
            if match and int(match[1]) in missing:
                return _GANTT_TOKEN_COUNT.sub("Tokens unavailable", label)
            return label

        for trace in fig.data:
            if trace.type == "bar" and isinstance(trace.hovertext, (list, tuple)):
                trace.hovertext = tuple(hover_label(label) for label in trace.hovertext)
        return fig

    @functools.wraps(original_load)
    def load(*args, **kwargs):
        outputs = original_load(*args, **kwargs)
        mark_missing(outputs[chart], outputs[steps_state])
        return outputs

    @functools.wraps(original_axis)
    def change_axis(axis_mode, steps, dark):
        return mark_missing(build(axis_mode, steps, dark), steps)

    load.__annotations__ = get_type_hints(original_load)  # Retain Gradio's typed request injection.
    loader.fn = load
    axis.fn = change_axis


def _bound_expanded_workflow(app: gr.Blocks) -> None:
    """Cap tool I/O at the native detail pane's limit without touching search or source records."""
    from agent_trajviz.insight.presenters import transcript, workflow
    from agent_trajviz.insight.ui.shared import safe_callback

    from .native_quality import dashboard_field_observed

    entries = [entry for entry in app.fns.values()
               if getattr(entry.fn, "__module__", "") == "agent_trajviz.insight.ui.workflow_tab"
               and getattr(entry.fn, "__name__", "") == "do_filter_workflow"]
    if not entries or len({entry.fn for entry in entries}) != 1:
        raise BridgeError("dashboard_incompatible", "TrajViz's Expanded Workflow callback changed.")

    native_render = _replace_native_binding(workflow.render_workflow_html, "field_observed", dashboard_field_observed)

    def render(steps, *, expanded=False):
        if not expanded:
            return native_render(steps)
        shown = []
        for step in steps:
            parts = []
            for part in step.get("parts", []):
                clipped = {}
                if part.get("type") == "tool_call":
                    for key in ("input", "output", "error"):
                        if key in part:
                            text = transcript._plain(part[key])
                            if len(text) > _MAX_EXPANDED_TOOL_CHARS:
                                clipped[key] = text[:_MAX_EXPANDED_TOOL_CHARS] + "\n... (truncated at 200,000 chars)"
                parts.append({**part, **clipped} if clipped else part)
            shown.append({**step, "parts": parts})
        return native_render(shown, expanded=True)

    presenter = _replace_native_binding(workflow.build_filtered_workflow_outputs, "render_workflow_html", render)
    original = inspect.unwrap(entries[0].fn)
    callback = safe_callback("Workflow filter")(_replace_native_binding(original, "build_filtered_workflow_outputs", presenter))
    for entry in entries:
        entry.fn = callback


def _set_dark_theme(app: gr.Blocks) -> None:
    import gradio as gr

    native = [entry for entry in app.fns.values()
              if getattr(entry.fn, "__module__", "") == "agent_trajviz.insight.insight"
              and "window.tvSetGradioText = " in (entry.js or "")]
    if len(native) != 1 or len(native[0].outputs) != 1 or not isinstance(native[0].outputs[0], gr.State):
        raise BridgeError("dashboard_incompatible", "TrajViz's browser theme initializer changed.")
    callback = native[0]
    replacements = {
        "document.documentElement.style.colorScheme = 'light';":
            "document.documentElement.style.colorScheme = 'dark';",
        "document.documentElement.classList.remove('dark');":
            "document.documentElement.classList.add('dark', 'tv-theme-dark');",
        "if (document.body) document.body.classList.remove('dark');":
            "document.body?.classList.add('dark');",
    }
    if any(callback.js.count(before) != 1 for before in replacements):
        raise BridgeError("dashboard_incompatible", "TrajViz's browser theme script changed.")
    for before, after in replacements.items():
        callback.js = callback.js.replace(before, after)
    callback.outputs[0].value = True
    callback.fn = lambda _dark=False: True


def decorate_dashboard(app: gr.Blocks, kind: str, *, dark: bool = False) -> gr.Blocks:
    """Wrap a native dashboard while preserving all its components and events."""
    import gradio as gr

    from .attribution_ui import adapt_attribution_workspace
    from .comparison_ui import adapt_comparisons

    adapt_comparisons(app, kind)
    if kind == "insight":
        adapt_attribution_workspace(app)

    for block in app.blocks.values():
        for attribute in ("value", "label", "info", "placeholder"):
            value = getattr(block, attribute, None)
            if isinstance(value, str):
                if isinstance(block, gr.HTML) and attribute == "value":
                    value = value.replace("DECAF capability failure attribution", "Capability failure attribution")
                setattr(block, attribute, attribution_message(value))
        if kind == "converge" and isinstance(block, gr.File):
            block.height = 140
        if kind == "converge" and isinstance(block, gr.Row) and any(
            isinstance(child, gr.File) or isinstance(child, gr.Column) and any(
                isinstance(part, gr.File) for part in child.children
            ) for child in block.children
        ):
            block.elem_classes = [*(block.elem_classes or []), "obs-file-row"]
        extra = "obs-chart" if isinstance(block, gr.Plot) else "obs-markdown" if isinstance(block, gr.Markdown) else None
        if extra:
            block.elem_classes = [*(block.elem_classes or []), extra]
        if isinstance(block, gr.Plot):
            _responsive_plot(block)
        if isinstance(block, gr.Row) and sum(isinstance(child, gr.Plot) for child in block.children) > 1:
            block.elem_classes = [*(block.elem_classes or []), "obs-chart-row"]
            if {child.elem_id for child in block.children} >= {"token-chart", "duration-chart"}:
                block.elem_classes.append("obs-summary-charts")
        if isinstance(block, gr.Row) and any(isinstance(child, gr.Plot) for child in block.children):
            for child in block.children:
                if isinstance(child, gr.Column) and not child.children:
                    child.visible = False
        if isinstance(block, gr.Row) and any(child.elem_id == "wf-filter-chips" for child in block.children):
            block.elem_classes = [*(block.elem_classes or []), "obs-workflow-filters"]
            # Gradio serializes the radio's classes but drops them from its generated Form.
            for child in block.children:
                for control in getattr(child, "children", (child,)):
                    if isinstance(control, gr.Radio) and control.label == "View":
                        control.elem_classes = [*(control.elem_classes or []), "obs-workflow-view"]
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
        with gr.Row(elem_classes=["obs-dashboard-heading"]) as heading:
            gr.HTML(_GUIDE[kind], elem_classes=["obs-guide-block"], scale=1)
        app.render()
        page.load(fn=None, js=_JS.replace(_LIGHT, _DARK) if dark else _JS)
    # Keep the original upload component and every callback referring to it.
    # Only move the native source row under a disclosure in this app's tree.
    if kind == "insight":
        _focus_overview(page)
        header = next((child for child in page.children if child.elem_id == "tv-app-header"), None)
        if header is not None:
            page.children.remove(header)
            heading.children.append(header)
            for child in header.children:
                if "app-title" in (child.elem_classes or []):
                    child.visible = False
        source = next((child for child in page.children if "upload-row" in (child.elem_classes or [])), None)
        if source is not None:
            position = page.children.index(source)
            with page:
                controls = gr.Accordion("Session source and labels", open=True,
                                        elem_classes=["obs-source-controls"])
            page.children.remove(controls)
            page.children[position] = controls
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



def _responsive_plot_data(value: Any, empty: tuple[str, str] | None = None) -> Any:
    """Size serialized Plotly figures to their container without changing cached figures."""
    data = value if isinstance(value, dict) else {"type": getattr(value, "type", None), "plot": getattr(value, "plot", None)}
    if data.get("type") != "plotly" or not isinstance(data.get("plot"), str):
        return value
    figure = json.loads(data["plot"])
    layout = figure.get("layout")
    if layout is None:
        layout = figure["layout"] = {}
    layout.pop("width", None)
    layout["autosize"] = True
    if empty is not None:
        axis, note = empty
        values = [trace.get(axis) for trace in figure.get("data", [])]
        if not any(any(cell is not None for cell in series) if isinstance(series, list) else bool(series)
                   for series in values):
            figure["data"] = []
            layout.update(height=160, showlegend=False, xaxis={"visible": False}, yaxis={"visible": False}, annotations=[{
                "text": note, "showarrow": False, "xref": "paper", "yref": "paper", "x": 0.5, "y": 0.5,
                "font": {"color": "#64748b"},
            }])
            for name in tuple(layout):
                if name.startswith(("xaxis", "yaxis")):
                    layout[name] = {"visible": False}
    if not figure.get("data"):
        for annotation in layout.get("annotations", []):
            annotation["text"] = _wrap_plot_text(annotation.get("text", ""))
    for name, axis in list(layout.items()):
        if not name.startswith(("xaxis", "yaxis")) or not isinstance(axis, dict):
            continue
        title = axis.get("title")
        text = title.get("text") if isinstance(title, dict) else title
        wrapped = _wrap_plot_text(text)
        if wrapped != text:
            if isinstance(title, dict):
                title["text"] = wrapped
            else:
                axis["title"] = wrapped
            if name.startswith("xaxis"):
                margin = layout.setdefault("margin", {})
                margin["b"] = max(70, margin.get("b") or 0)
    update = {"plot": json.dumps(figure)}
    return {**value, **update} if isinstance(value, dict) else value.model_copy(update=update)


def _wrap_plot_text(text: Any) -> Any:
    if not isinstance(text, str) or len(text) <= 28 or "<" in text or "\n" in text:
        return text
    return "<br>".join(textwrap.wrap(text, width=28, break_long_words=False, break_on_hyphens=False))


def _responsive_plot(block: gr.Plot) -> None:
    """Keep each native Plot's postprocessor, normalizing only its Plotly output."""
    if getattr(block, "_obs_responsive", False):
        return
    original = block.postprocess
    empty = _EMPTY_CHART.get(block.elem_id)

    def postprocess(value: Any) -> Any:
        return _responsive_plot_data(original(value), empty)

    block.postprocess = postprocess
    block.value = _responsive_plot_data(block.value, empty)
    block._obs_responsive = True


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
    chart_guide = next((child for child in summary.children if isinstance(child, gr.HTML)
                        and "section-guide-tip" in str(child.value)), None)
    if chart_guide is not None:
        chart_guide.elem_id = "obs-summary-chart-guide"
    cards = [child for child in page.children if "overview-kpi-strip" in (child.elem_classes or [])]
    warnings = [child for child in page.children if isinstance(child, gr.Column) and any(
        "label-status-badge" in (part.elem_classes or []) for part in child.children)]
    findings = [child for child in overview.children if child is not layout]
    for child in [*cards, *warnings]:
        page.children.remove(child)
    for child in findings:
        overview.children.remove(child)
    for column in warnings:
        column.elem_classes = [*(column.elem_classes or []), "obs-summary-status"]
    summary.children[:0] = [*cards, *warnings, *findings]
