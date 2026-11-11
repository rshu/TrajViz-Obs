"""Use recorded token evidence in the browser's token explanations."""

from __future__ import annotations

_COUNTS = {
    "total": ("Total", "总量"), "input": ("Input", "输入"), "output": ("Output", "输出"),
    "cache_read": ("Cache read", "缓存读取"), "cache_write": ("Cache write", "缓存写入"),
}


def token_presenters(clone):
    """Bind the native presenters to the same coverage as the plugin's charts."""
    from agent_trajviz.i18n import paired_text
    from agent_trajviz.insight import token_explain
    from agent_trajviz.insight.presenters.overview import build_token_summary_html
    from agent_trajviz.insight.telemetry_quality import is_otel_step

    from .native_quality import dashboard_field_observed

    native_render = clone(token_explain._render_token_step, {"field_observed": dashboard_field_observed})

    def render(step, index, previous, users):
        if step.get("role") == "assistant" and not is_otel_step(step):
            observed = {key for key in _COUNTS if dashboard_field_observed(step, f"tokens.{key}")}
            if observed and len(observed) < len(_COUNTS):
                header = paired_text(f"Step {index} · Available token counts", f"第 {index} 步 · 可用的 token 数量")
                rows = [paired_text(f"{en} {step['tokens'][key]:,}", f"{zh} {step['tokens'][key]:,}")
                        for key, (en, zh) in _COUNTS.items() if key in observed]
                missing = paired_text("Some token counts are unavailable.", "部分 token 数量不可用。")
                return f"<div><b>{header}</b><br>{'<br>'.join(rows)}<br>{missing}</div>"
        return native_render(step, index, previous, users)

    return {
        "build_token_summary_html": clone(build_token_summary_html, {"field_observed": dashboard_field_observed}),
        "build_token_step_panel_html": clone(token_explain.build_token_step_panel_html, {
            "_step_contexts": clone(token_explain._step_contexts, {"field_observed": dashboard_field_observed}),
            "_render_token_step": render,
        }),
    }
