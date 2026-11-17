"""Use recorded token evidence in the browser's token explanations."""

from __future__ import annotations

from html import escape

_COUNTS = {
    "total": ("Total", "总量"), "input": ("Input", "输入"), "output": ("Output", "输出"),
    "cache_read": ("Cache read", "缓存读取"), "cache_write": ("Cache write", "缓存写入"),
    "reasoning": ("Reasoning", "思考"),
}
_REQUIRED_COUNTS = _COUNTS.keys() - {"reasoning"}


def _estimation_method(token_explain, paired_text):
    """Describe the upstream estimates using the constants that drive them."""
    note = paired_text(
        "Values marked ≈ are estimates based on available content. Their sum can differ from the provider’s count.",
        "带 ≈ 的项根据可见内容估算，各项合计可能与提供商记录的用量不同。",
    )
    cjk, other = token_explain.CJK_CHARS_PER_TOKEN, token_explain.OTHER_CHARS_PER_TOKEN
    tool_result, tool_batch = token_explain.TOOL_RESULT_WRAPPER_TOKENS, token_explain.TOOL_BATCH_WRAPPER_TOKENS
    user_message = token_explain.USER_MSG_WRAPPER_TOKENS
    tool_call, assistant = token_explain.TOOL_CALL_SYNTAX_TOKENS, token_explain.ASSISTANT_MSG_WRAPPER_TOKENS
    page = token_explain.RADIX_PAGE_TOKENS
    explanations = (
        paired_text(
            f"Text: divide basic CJK characters (U+4E00–U+9FFF) by {cjk} and other characters by {other}. "
            "Round the sum to the nearest whole number, with a minimum of one token for nonempty text.",
            f"文本：基本中日韩统一表意文字（U+4E00–U+9FFF）数量 ÷ {cjk} + 其他字符数 ÷ {other}。"
            "合计取最接近的整数，非空文本至少计 1 个 token。",
        ),
        paired_text(
            f"Input framing adds about {tool_result} tokens per tool result, {tool_batch} per group of results from "
            f"the previous step, and {user_message} per user message. Output framing adds about {tool_call} per tool "
            f"call and {assistant} once for an assistant message with tool calls.",
            f"输入框架按每条工具结果约 {tool_result} 个 token、上一步的整组结果约 {tool_batch} 个、"
            f"每条用户消息约 {user_message} 个估算。输出框架按每次工具调用约 {tool_call} 个估算，"
            f"有工具调用的助手消息另计一次约 {assistant} 个。",
        ),
        paired_text(
            "A partial SGLang cache page is estimated only when the cache-hit count matches "
            f"the previous call’s full pages of {page} tokens each.",
            f"仅当 SGLang 缓存命中量与上一次调用的整页 token 数一致时，才按 {page} token/页估算尾页。",
        ),
    )
    title = paired_text("How the estimates are calculated", "估算方法（带 ≈ 的数值如何得出）")
    return (f"<details class='tv-token-method'><summary>{title}</summary><p>{note}</p><ol>"
            + "".join(f"<li>{explanation}</li>" for explanation in explanations) + "</ol></details>")


def token_presenters(clone):
    """Bind the native presenters to the same coverage as the plugin's charts."""
    from agent_trajviz.i18n import paired_text
    from agent_trajviz.insight import token_explain
    from agent_trajviz.insight.presenters.overview import build_token_summary_html
    from agent_trajviz.insight.rendering import _diag_jump_onclick
    from agent_trajviz.insight.telemetry_quality import is_otel_step

    from .native_quality import dashboard_field_observed

    def remainder_text(english, chinese):
        prefix = "- System prompt and tool definitions ≈ "
        if english.startswith(prefix):
            estimate = english[len(prefix):].partition(" tok")[0]
            return paired_text(
                f"- Other input ≈ {estimate} tok (remainder after estimated messages and framing). "
                "This may include system prompts and tool definitions.",
                f"- 其他输入 ≈ {estimate} tok（扣除消息及其框架的估算后所得，可能包含系统提示和工具定义）。",
            )
        return paired_text(english, chinese)

    first_call = clone(token_explain._first_call_lines, {"paired_text": remainder_text})
    fresh_input = clone(token_explain._fresh_input_lines, {"_first_call_lines": first_call})
    native_render = clone(token_explain._render_token_step, {
        "field_observed": dashboard_field_observed, "_fresh_input_lines": fresh_input,
    })

    def render(step, index, previous, users):
        if step.get("role") == "assistant" and not is_otel_step(step):
            observed = {key for key in _COUNTS if dashboard_field_observed(step, f"tokens.{key}")}
            if observed and not _REQUIRED_COUNTS <= observed:
                heading = paired_text(f"Step {index}", f"第 {index} 步") + " · assistant"
                if model := step.get("model_id"):
                    heading += f" · {escape(str(model))}"
                title = paired_text("Available token counts", "可用的 token 数量")
                jump = paired_text("Open step in Workflow →", "在 Workflow 中查看该步 →")
                unavailable = paired_text("Unavailable", "不可用")
                # The raw counters may overlap. Show only recorded values and do
                # not infer a fresh-input or completion sum from partial usage.
                rows = [
                    f"<tr><th scope='row'>{paired_text(en, zh)}</th><td>"
                    f"{format(step['tokens'][key], ',') if key in observed else unavailable}</td></tr>"
                    for key, (en, zh) in _COUNTS.items()
                ]
                missing = paired_text("Some token counts are unavailable.", "部分 token 数量不可用。")
                return (
                    "<div style='margin-top:0.4em;'>"
                    f"<div style='font-weight:600;margin-bottom:4px;'>{heading} — {title}"
                    f"<span class='insight-step-link' onclick=\"{_diag_jump_onclick(index)}\" "
                    f"style='margin-left:10px;font-weight:400;'>{jump}</span></div>"
                    f"<table>{''.join(rows)}</table><p>{missing}</p></div>"
                )
        return native_render(step, index, previous, users)

    return {
        "build_token_summary_html": clone(build_token_summary_html, {"field_observed": dashboard_field_observed}),
        "build_token_step_panel_html": clone(token_explain.build_token_step_panel_html, {
            "_METHOD_DETAILS_HTML": _estimation_method(token_explain, paired_text),
            "_step_contexts": clone(token_explain._step_contexts, {"field_observed": dashboard_field_observed}),
            "_render_token_step": render,
        }),
    }
