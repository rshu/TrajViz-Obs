"""Record fixed actions on the live dashboard without exposing telemetry configuration to the browser."""

from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock, Thread

from telemetry_client import flush_usage, queue_usage, usage_settings

_BUTTONS = {
    "dashboard": {
        "load": {"Load Trajectory"},
        "export": {"Export HTML", "Export issues into JSON"},
        "compare": {"Run Comparison", "Build scorecard"},
        "analyze": {"Analyze trajectory",
                    "Suggest fixes with LLM (sends trajectory excerpts to the configured provider)"},
        "attribution": {"Inspect trajectory", "Diagnose failure"},
    },
    "converge": {"compare": {"Compare", "Run Batch", "Compare Before/After"}},
}

_JS = r"""(actions, insight) => {
  const root = document.querySelector('.gradio-container');
  if (!root || root.dataset.obsUsageBound) return [];
  root.dataset.obsUsageBound = 'true';
  const selector = '.obs-main-tabs [role="tab"], .obs-main-tabs .overflow-dropdown button';
  const stepSelector = '.insight-step-link, #wf-toc-container .toc-entry, #wf-detail-panel .wf-card';
  const send = action => {
    const control = root.querySelector(`#obs-usage-${action}`);
    const button = control?.matches('button') ? control : control?.querySelector('button');
    button?.click();
  };
  root.addEventListener('click', event => {
    if (!event.isTrusted || !(event.target instanceof Element)) return;
    const target = event.target;
    if (target.closest(':disabled, [aria-disabled="true"]')) return;
    const action = actions.find(name => target.closest(`.obs-track-${name}`));
    if (action) send(action);
    // Issue cards use a visible HTML button that forwards to a hidden Gradio control.
    else if (insight && target.closest('.overview-issue-analyze')) send('analyze');
    else if (insight && target.closest(stepSelector)) send('inspect');
    else {
      const tab = target.closest(selector);
      if (tab && tab.getAttribute('aria-selected') !== 'true') send('tab');
    }
  }, true);
  // The layout handler converts these keys to a synthetic click on a step link.
  if (insight) root.addEventListener('keydown', event => {
    if (event.isTrusted && !event.repeat && (event.key === 'Enter' || event.key === ' ')
        && event.target instanceof Element && event.target.closest('.insight-step-link')) send('inspect');
  }, true);
  let rendered = false;
  const check = () => {
    if (rendered || !root.querySelector('.obs-main-tabs [role="tab"]')?.getClientRects().length
        || !root.querySelector('#obs-usage-render')) return;
    rendered = true;
    observer.disconnect();
    requestAnimationFrame(() => send('render'));
  };
  const observer = new MutationObserver(check);
  observer.observe(root, {subtree: true, childList: true, attributes: true, attributeFilter: ['class', 'hidden']});
  requestAnimationFrame(check);
  setTimeout(() => observer.disconnect(), 15000);
  return [];
}"""


class DashboardUsage:
    def __init__(self, adapter: str, version: str) -> None:
        self.adapter = adapter
        self.version = version
        self._lock = Lock()
        self._requested = False
        self._running = False

    @classmethod
    def from_environment(cls, adapter: str | None, version: str | None) -> DashboardUsage | None:
        # Only the host supplies these hidden arguments after checking consent.
        if adapter is None or version is None:
            return None
        settings = usage_settings(os.environ, adapter)
        if settings.configuration != "ready" or settings.endpoint is None or settings.directory is None:
            return None
        return cls(adapter, version)

    def _target(self) -> tuple[str, Path, str | None] | None:
        settings = usage_settings(os.environ, self.adapter)
        if settings.configuration == "ready" and settings.endpoint is not None and settings.directory is not None:
            return settings.endpoint, settings.directory, settings.token
        return None

    def _enqueue(self, feature: str) -> None:
        target = self._target()
        if target is None:
            return
        endpoint, directory, _token = target
        if not queue_usage(endpoint, directory, adapter=self.adapter, surface="browser", feature=feature,
                           outcome="observed", duration_ms=0, version=self.version, env=os.environ):
            return
        with self._lock:
            self._requested = True
            if self._running:
                return
            self._running = True
            try:
                Thread(target=self._drain, daemon=True, name="trajviz-dashboard-usage").start()
            except RuntimeError:
                self._running = False

    def _drain(self) -> None:
        while True:
            with self._lock:
                if not self._requested:
                    self._running = False
                    return
                self._requested = False
            if (target := self._target()) is not None:
                endpoint, directory, token = target
                flush_usage(endpoint, directory, self.adapter, token=token, env=os.environ)

    def _callback(self, feature: str):
        def observe() -> None:
            self._enqueue(feature)

        return observe

    def attach(self, app, kind: str) -> None:
        import gradio as gr

        actions = _BUTTONS[kind]
        for block in app.blocks.values():
            if isinstance(block, (gr.Button, gr.DownloadButton)):
                for name, labels in actions.items():
                    if any(isinstance(text, str) and text in labels for text in (block.value, block.label)):
                        block.elem_classes = [*(block.elem_classes or []), f"obs-track-{name}"]
        events = ["render", "tab", *actions]
        if kind == "dashboard":
            events.append("inspect")
        with app:
            for name in events:
                gr.Button(visible="hidden", elem_id=f"obs-usage-{name}").click(
                    self._callback(f"{kind}.{name}"), api_name=f"obs_usage_{name}", api_visibility="private",
                    queue=False, show_progress="hidden", trigger_mode="multiple",
                )
            app.load(fn=None, js=f"() => ({_JS})({json.dumps(list(actions))}, {json.dumps(kind == 'dashboard')})")
