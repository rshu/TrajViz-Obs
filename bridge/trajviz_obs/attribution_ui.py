"""Public attribution wording and reference-data guidance, scoped to each app.

Only generated labels, status messages and errors change. Saved evidence,
settings, remembered values, operation results and source paths keep their
native identities and contents.
"""
from __future__ import annotations

import functools
import inspect

from .compat import attribution_defaults, attribution_identity_message, attribution_message

_STATUS = {"on_awe_apply": 3, "on_awe_restore": -2, "on_awe_reset": -2}
_WORKER_NOTE = ("For reference-based OpenCode subagent diagnoses, use "
                "`/obs attribution --reference-root DIR --agent opencode --instance ID` for session-accurate child evidence. "
                "The bundled evaluation workers can group child evidence by agent role.")
_GRAPH_LABEL = ("aria-label='Numbered AWE provenance graph'", "aria-label='Numbered attribution provenance graph'")


def _diagnosis_callback(original):
    from agent_trajviz.insight.loaders import detect_format
    from agent_trajviz.insight.rendering import build_attribution_html

    signature = inspect.signature(original)
    automatic = original.__name__ == "on_diagnose_autoload"
    invalid = "invalid agent id (must be a plain agent name)"
    refused = build_attribution_html({"available": False, "reason": invalid})
    guidance = build_attribution_html({"available": False, "reason": attribution_identity_message(invalid, browser=True)})

    @functools.wraps(original)
    def present(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        override = bound.arguments.get("agent_override", "")
        raw = bound.arguments.get("overview_raw")
        if isinstance(raw, dict) and detect_format(raw) == "opencode":
            agent, instance = attribution_defaults("opencode", raw, str(override or "").strip() or None,
                str(bound.arguments.get("inst_override") or "").strip() or None)
            if automatic:
                # Reference attribution is optional in 0.4.2. Preserve the
                # native autoload's no-diagnosis/reset behavior; only its
                # identity prefills need the OpenCode session defaults.
                result = (*original(*args, **kwargs)[:2], agent or "", instance or "")
            else:
                bound.arguments.update(agent_override=agent or "", inst_override=instance or "")
                result = original(*bound.args, **bound.kwargs)
        else:
            result = original(*args, **kwargs)
        # Only the generated refusal message changes. Explicit invalid
        # overrides, evidence, source identities and verdicts stay native.
        if (automatic or not str(override or "").strip()) and result[0] == refused:
            result = (guidance, *result[1:])
        return result

    present._obs_attribution_identity = True
    return present


def _callback(original):
    import gradio as gr

    name = original.__name__

    @functools.wraps(original)
    def present(*args, **kwargs):
        try:
            result = original(*args, **kwargs)
        except gr.Error as exc:
            raise gr.Error(attribution_message(exc.message), duration=exc.duration, visible=exc.visible,
                           title=exc.title, print_exception=exc.print_exception) from None
        if name in _STATUS:
            values = list(result)
            index = _STATUS[name]
            values[index] = attribution_message(values[index])
            return tuple(values)
        if name in {"on_awe_import", "on_awe_filter", "on_awe_case", "on_awe_node", "on_awe_import_result"}:
            # This literal belongs to generated SVG markup. Imported quotes
            # are escaped, so recorded text and source values are unaffected.
            return tuple(value.replace(*_GRAPH_LABEL) if isinstance(value, str) else value for value in result)
        return result

    present._obs_attribution_workspace = True
    return present


def adapt_attribution_workspace(app) -> None:
    """Preserve native callback contracts and the workspace's independent state."""
    import gradio as gr

    workspaces = [component for component in app.blocks.values()
                  if isinstance(component, gr.Tab) and component.label in {"AWE", "Attribution workspace"}]
    for component in workspaces:
        component.label = "Attribution workspace"
        if not any("obs-attribution-worker-note" in (child.elem_classes or []) for child in component.children):
            with app:
                note = gr.Markdown(_WORKER_NOTE, elem_classes=["obs-attribution-worker-note"])
            app.children.remove(note)
            component.children.insert(1, note)
    for entry in app.fns.values():
        original = entry.fn
        if (callable(original) and getattr(original, "__module__", "") == "agent_trajviz.insight.ui.attribution_tab"
                and getattr(original, "__name__", "") in {"on_diagnose", "on_diagnose_autoload"}
                and not getattr(original, "_obs_attribution_identity", False)):
            entry.fn = _diagnosis_callback(original)
            continue
        if (callable(original) and getattr(original, "__module__", "") == "agent_trajviz.insight.ui.awe_tab"
                and getattr(original, "__name__", "").startswith("on_awe_")
                and not getattr(original, "_obs_attribution_workspace", False)):
            entry.fn = _callback(original)
