"""Read-only, host-pinned Prompt Explorer in the live session dashboard."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import gradio as gr
from agent_trajviz import Trajectory
from agent_trajviz.insight.ui.shared import drop_latest_run, is_latest_run

from .compat import input_path
from .prompt_explorer_data import load_dataset, select_record
from .prompt_explorer_render import component_token, render_explorer

EXPLORER_JS = """() => {
  if (window.__obsPromptExplorerBound) return;
  window.__obsPromptExplorerBound = true;
  let pendingFocus = null;
  document.addEventListener('keydown', event => {
    if (event.key !== 'Enter' && event.key !== ' ') pendingFocus = null;
  }, true);
  new MutationObserver(() => {
    if (!pendingFocus || pendingFocus.target.isConnected) return;
    if (document.activeElement !== document.body) { pendingFocus = null; return; }
    const attribute = 'data-obs-prompt-' + pendingFocus.kind;
    const target = Array.from(document.querySelectorAll('#obs-prompt-explorer [' + attribute + ']'))
      .find(element => element.getAttribute(attribute) === pendingFocus.value);
    const selected = pendingFocus.kind === 'request' ? target?.getAttribute('aria-current') === 'step'
      : target?.getAttribute('aria-pressed') === 'true';
    if (selected) { target.focus({preventScroll:true}); pendingFocus = null; }
  }).observe(document.body, {childList:true, subtree:true});
  document.addEventListener('click', event => {
    const target = event.target.closest?.('[data-obs-prompt-request], [data-obs-prompt-component]');
    if (!target?.closest('#obs-prompt-explorer') || event.defaultPrevented) return;
    const kind = target.hasAttribute('data-obs-prompt-request') ? 'request' : 'component';
    const value = target.getAttribute('data-obs-prompt-' + kind);
    const field = document.querySelector('#obs-prompt-action textarea, #obs-prompt-action input');
    const submit = document.querySelector('#obs-prompt-apply button, button#obs-prompt-apply');
    if (!field || !submit || !value) return;
    event.preventDefault();
    pendingFocus = event.detail === 0 ? {kind, value, target} : null;
    const text = JSON.stringify({kind, value});
    const prototype = field instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, 'value').set.call(field, text);
    field.dispatchEvent(new Event('input', {bubbles:true}));
    submit.click();
  });
}"""


def _empty(message: str, status: str = "disabled") -> dict:
    return {"status": status, "message": message, "records": [], "lanes": [], "markers": [], "capture_gaps": []}


def _session(run: Trajectory) -> str | None:
    metadata = run.raw.get("metadata")
    native = metadata.get("session_id") if isinstance(metadata, dict) else None
    if isinstance(native, str) and native:
        return native
    telemetry = run.raw.get("_otel")
    if isinstance(telemetry, dict) and isinstance(telemetry.get("session_id"), str):
        return telemetry["session_id"]
    return next((row.get("session_id") for row in run.steps if not row.get("is_sub_agent") and row.get("session_id")), None)


def scoped_dataset(run, pinned_source: Path, capture_root: Path | None, *, session_id: str | None = None,
                   claude_raw: Path | None = None) -> dict:
    """Only a host-pinned source may authorize IDs; uploaded metadata cannot grant access."""
    if capture_root is None:
        return _empty("Prompt capture is off. Enable local capture in the host's launch environment and reopen /obs dashboard.")
    if not isinstance(run, Trajectory):
        return _empty("Load the host-selected session to inspect its captured prompts.", "empty")
    try:
        source = input_path(run)
        if source is None or source.resolve() != pinned_source.resolve():
            return _empty("Prompt captures are available only for the host-selected session. Imported trajectories do not grant capture access.", "empty")
        identity = _session(run)
        if not identity or session_id is not None and identity != session_id:
            return _empty("This trajectory does not match the host-selected capture session.", "empty")
        adapter = "pi" if run.raw.get("_pi_format") else "opencode" if run.format == "opencode" else "claude-code" if run.format == "claude_code" else None
        if adapter is None:
            return _empty("No supported native prompt capture is associated with this trajectory.", "empty")
        sessions = {identity}
        # Child IDs come from the same host-pinned native capture, never a browser selector.
        sessions.update(str(row["session_id"]) for row in run.steps if row.get("session_id"))
        return load_dataset(capture_root, session_ids=sessions, adapter=adapter,
                            claude_raw=claude_raw if adapter == "claude-code" else None)
    except (OSError, ValueError, TypeError, KeyError):
        return _empty("The local prompt captures could not be read safely. Check the private capture directory.", "unavailable")


def _lane_choices(dataset: dict) -> list[tuple[str, str]]:
    lanes = dataset.get("lanes", [])
    # Main request boundaries first; auxiliary and unlinked observations remain inspectable.
    order = {"context": 0, "request.context": 0, "provider.request": 1}
    lanes = sorted(lanes, key=lambda lane: order.get(lane.get("stage"), 2))
    counts = {}
    output = []
    for lane in lanes:
        label = lane["label"]
        counts[label] = counts.get(label, 0) + 1
        duplicate = sum(other["label"] == label for other in lanes) > 1
        output.append((f"{label} · Segment {counts[label]}" if duplicate else label, lane["id"]))
    return output


def _render_state(dataset: dict, selection: dict | None, mode: str):
    selection = selection if isinstance(selection, dict) else {}
    if not selection.get("request"):
        contexts = [row for row in dataset.get("records", []) if row.get("stage") in {"context", "request.context"}]
        if contexts:
            selection = {"request": max(contexts, key=lambda row: row.get("observed_at", ""))["selection_id"]}
    chosen = select_record(dataset, selection.get("request"), effective=mode == "effective")
    record = chosen.get("record")
    if record is None:
        chosen = select_record(dataset, effective=mode == "effective")
        record = chosen.get("record")
    has_effective = bool(record is not None and "effective_state" in record)
    if not has_effective:
        mode = "physical"
    selected = record.get("selection_id") if record else None
    components = chosen.get("components", [])
    component_ids = [component_token(row["id"]) for row in components]
    component = selection.get("component")
    if component not in component_ids:
        changed = next((row for row in components
                        if set(row.get("changes", [])) & {"added", "modified", "removed"}), None)
        component = component_token(changed["id"]) if changed else component_ids[0] if component_ids else None
    choices = _lane_choices(dataset)
    lane = record.get("lane_id") if record else None
    requests = [row for row in dataset.get("records", []) if row.get("lane_id") == lane]
    request_choices = [(f"{row.get('lane_index', index + 1)} · {row.get('observed_at', '')}", row["selection_id"]) for index, row in enumerate(requests)]
    component_choices = [(f"{index + 1} · {(row.get('after') or row.get('before') or {}).get('kind', 'component').replace('_', ' ')} · {row['id'][:90]}", component_token(row["id"])) for index, row in enumerate(components)]
    selection = {"request": selected, "component": component}
    return (dataset, gr.update(choices=choices, value=lane), gr.update(choices=request_choices, value=selected),
            gr.update(choices=component_choices, value=component), selection,
            render_explorer(dataset, selected, component, mode=mode),
            gr.update(value=mode, visible=has_effective))


def mount_prompt_explorer(app: gr.Blocks, *, transcript_path: str, session_id: str | None = None,
                          capture_root: Path | None = None, claude_raw: Path | None = None) -> bool:
    if getattr(app, "_obs_prompt_explorer", False) or any(getattr(block, "elem_id", None) == "obs-prompt-tab" for block in app.blocks.values()):
        return True
    tabs = next((block for block in app.children if isinstance(block, gr.Tabs)), None)
    loaders = [block for block in app.fns.values() if getattr(block.fn, "__name__", "") == "_do_load"]
    if tabs is None or len(loaders) != 1:
        return False
    slots = inspect.getclosurevars(loaders[0].fn).nonlocals.get("slots")
    state_run = slots.get("state_run") if isinstance(slots, dict) else None
    if not isinstance(state_run, gr.State):
        return False
    pinned = Path(transcript_path).resolve()
    # All capture authority stays in Python closures. Config and browser inputs contain no paths.
    root = capture_root if session_id else None
    initial = _empty("Load the host-selected session to inspect its captured prompts.", "empty") if root else scoped_dataset(None, pinned, None)
    with app:
        state = gr.State(initial)
        selected = gr.State({})
        with tabs, gr.Tab("Prompt Explorer", id="obs-prompt-explorer-tab", elem_id="obs-prompt-tab"):
            with gr.Row(elem_classes=["obs-prompt-toolbar"]):
                lane = gr.Dropdown(label="Capture lane", choices=[], interactive=True, scale=4, min_width=220, elem_id="obs-prompt-lane")
                mode = gr.Radio([("Request components", "physical"), ("Pi active system / tools", "effective")],
                                value="physical", label="Composition view", scale=3, visible=False, elem_id="obs-prompt-mode")
                refresh = gr.Button("Refresh captures", scale=1, elem_id="obs-prompt-refresh")
            with gr.Row(elem_classes=["obs-prompt-toolbar"]):
                request_picker = gr.Dropdown(label="Request / observation", choices=[], interactive=True, scale=1, elem_id="obs-prompt-request-picker")
                component_picker = gr.Dropdown(label="Find component", choices=[], interactive=True, scale=1, elem_id="obs-prompt-component-picker")
            body = gr.HTML(render_explorer(initial), elem_id="obs-prompt-content")
            action = gr.Textbox(value="", label="Prompt selection", elem_id="obs-prompt-action", elem_classes=["obs-prompt-internal"])
            apply = gr.Button("Select prompt component", elem_id="obs-prompt-apply", elem_classes=["obs-prompt-internal"])
        outputs = [state, lane, request_picker, component_picker, selected, body, mode]

        def current(run, request):
            return isinstance(run, Trajectory) and is_latest_run(getattr(request, "session_hash", None), run)

        def refresh_data(run, selection, selected_mode, request: gr.Request | None = None):
            if root is None:
                return _render_state(scoped_dataset(None, pinned, None), {}, selected_mode)
            dataset = scoped_dataset(run, pinned, root, session_id=session_id, claude_raw=claude_raw) if current(run, request) else _empty("Load the host-selected session to inspect its captured prompts.", "empty")
            if isinstance(run, Trajectory) and not current(run, request):
                return (gr.skip(),) * len(outputs)
            result = _render_state(dataset, selection, selected_mode)
            return result if not isinstance(run, Trajectory) or current(run, request) else (gr.skip(),) * len(outputs)

        def choose(run, dataset, selection, selected_mode, value, *, kind: str, request=None):
            if not current(run, request) or scoped_dataset_authorized(run, pinned, session_id) is False:
                return _render_state(_empty("The selected source changed. Reload the host-selected session.", "empty"), {}, selected_mode)
            selection = dict(selection) if isinstance(selection, dict) else {}
            records = dataset.get("records", []) if isinstance(dataset, dict) else []
            if kind == "lane":
                row = next((row for row in records if row.get("lane_id") == value), None)
                if row:
                    selection = {"request": row["selection_id"]}
            elif kind == "request" and any(row.get("selection_id") == value for row in records):
                selection = {"request": value}
            elif kind == "component" and isinstance(value, str) and len(value) == 24:
                selection["component"] = value
            result = _render_state(dataset, selection, selected_mode)
            return result if current(run, request) else (gr.skip(),) * len(outputs)

        def choose_lane(run, dataset, selection, selected_mode, value, request: gr.Request | None = None):
            return choose(run, dataset, selection, selected_mode, value, kind="lane", request=request)

        def choose_request(run, dataset, selection, selected_mode, value, request: gr.Request | None = None):
            return choose(run, dataset, selection, selected_mode, value, kind="request", request=request)

        def choose_component(run, dataset, selection, selected_mode, value, request: gr.Request | None = None):
            return choose(run, dataset, selection, selected_mode, value, kind="component", request=request)

        def choose_mode(run, dataset, selection, selected_mode, request: gr.Request | None = None):
            return choose(run, dataset, selection, selected_mode, None, kind="mode", request=request)

        def choose_action(run, dataset, selection, selected_mode, value, request: gr.Request | None = None):
            try:
                parsed = json.loads(value) if isinstance(value, str) and len(value) <= 512 else {}
                kind = parsed.get("kind") if isinstance(parsed, dict) else None
                identity = parsed.get("value") if isinstance(parsed, dict) else None
                if kind not in {"request", "component"}:
                    kind, identity = "invalid", None
            except ValueError:
                kind, identity = "invalid", None
            return choose(run, dataset, selection, selected_mode, identity, kind=kind, request=request)

        common = [state_run, state, selected, mode]
        refresh.click(refresh_data, inputs=[state_run, selected, mode], outputs=outputs, api_name="obs_prompt_refresh", api_visibility="private")
        state_run.change(refresh_data, inputs=[state_run, selected, mode], outputs=outputs, api_name=False, api_visibility="private")
        lane.input(choose_lane, inputs=[*common, lane], outputs=outputs, api_name="obs_prompt_lane", api_visibility="private")
        request_picker.input(choose_request, inputs=[*common, request_picker], outputs=outputs, api_name="obs_prompt_request", api_visibility="private")
        component_picker.input(choose_component, inputs=[*common, component_picker], outputs=outputs, api_name="obs_prompt_component", api_visibility="private")
        mode.input(choose_mode, inputs=common, outputs=outputs, api_name="obs_prompt_mode", api_visibility="private")
        apply.click(choose_action, inputs=[*common, action], outputs=outputs, api_name="obs_prompt_action", api_visibility="private")
        source = next((block for block in loaders[0].inputs if isinstance(block, gr.File)), None)
        if source:
            def clear_source(request: gr.Request | None = None):
                if getattr(request, "session_hash", None):
                    drop_latest_run(request.session_hash)
                return _render_state(_empty("Load the host-selected session to inspect its captured prompts.", "empty"), {}, "physical")
            source.change(clear_source, inputs=[], outputs=outputs, api_name=False, api_visibility="private")
        app.load(fn=None, js=EXPLORER_JS)
    app._obs_prompt_explorer = True
    return True


def scoped_dataset_authorized(run, pinned_source: Path, session_id: str | None) -> bool:
    if not isinstance(run, Trajectory):
        return False
    source = input_path(run)
    return source is not None and source.resolve() == pinned_source and bool(session_id) and _session(run) == session_id
