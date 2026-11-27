"""App-scoped provenance labels for captured DevEco request bodies on TrajViz 0.7.1.

The installed package and its module globals stay untouched. Only this app's
native Prompt Evolution callbacks receive a cloned render binding; ordinary
formats and transcript estimates use the original renderer unchanged.
"""
from __future__ import annotations

import functools
import inspect
import types
from html import escape

from .errors import BridgeError
from .native_quality import _clone_native_function

_CALLBACKS = {"on_prompt_load": 1, "on_prompt_select": 3,
              "on_prompt_previous": 1, "on_prompt_next": 1}
_OUTPUTS = ("prompt-request-selector", "prompt-evolution-chart", "prompt-summary",
            "prompt-components", "prompt-changes", "prompt-request-json")
_LINEAGE = "Compared with the previous request in the same recorded lineage."
_NO_PREVIOUS = {"No previous request with verified lineage is available for comparison.",
                "No previous request on a recorded matching lineage is available."}


def _body(detail: object) -> bool:
    return (isinstance(detail, dict) and detail.get("scope") == "recorded_request"
            and isinstance(detail.get("evidence_quality"), dict)
            and detail["evidence_quality"].get("outbound_body_observed") is True)


def _literal(value: object) -> str:
    return "<code>" + escape(str(value if value is not None else "—")[:512]) + "</code>"


def _provenance(detail: dict) -> str:
    from agent_trajviz.i18n import paired_text as p

    mapping = detail.get("native_mapping") or {}
    quality = detail.get("evidence_quality") or {}
    mapping = mapping if isinstance(mapping, dict) else {}
    reason = str(mapping.get("reason") or "")
    status = mapping.get("status")
    if detail.get("purpose") == "title":
        state = p("Auxiliary title request", "辅助标题请求")
    elif status == "verified":
        state = p("Verified native assistant step", "已验证原生助手步骤")
    elif status == "pending" or "pending" in reason:
        state = p("Pending native ownership", "等待验证原生归属")
    else:
        state = p("Unlinked request", "未关联的请求")
    complete = (detail.get("coverage") or {}).get("complete") is True
    heading = p("Complete serialized HTTP body captured", "已捕获完整序列化 HTTP 请求体") if complete else p(
        "Serialized HTTP body observed", "已观察到序列化 HTTP 请求体")
    note = p("Captured after provider serialization. Component groups below describe the saved body; token sizes remain estimates.",
             "在服务商适配层完成序列化后捕获。下方组件分组对应保存的请求体；token 大小仍为估算值。")
    rows = [(p("Request ID", "请求 ID"), _literal(detail.get("request_id") or detail.get("id"))),
            (p("Body bytes", "请求体字节数"), _literal(detail.get("body_bytes"))),
            (p("SHA-256 verified", "SHA-256 已验证") if quality.get("body_sha256_verified") is True else p("SHA-256", "SHA-256"),
             _literal(detail.get("body_sha256"))),
            (p("Native mapping", "原生归属"), state)]
    if detail.get("assistant_message_id"):
        rows.append((p("Native assistant ID", "原生助手 ID"), _literal(detail["assistant_message_id"])))
    if detail.get("dispatch_status"):
        rows.append((p("Dispatch status", "发送状态"), _literal(detail["dispatch_status"])))
    if status != "verified" and detail.get("purpose") != "title":
        note += " " + p("The body remains available while native ownership is unresolved. A later refresh can link it after the required spans are exported.",
                         "原生归属尚未确定时，仍可查看请求体。所需 span 导出后，可在后续刷新时完成关联。")
    return ("<div class='pc-quality obs-deveco-request-provenance'><p><strong>" + heading + "</strong></p><p>" + note
            + "</p><dl style='display:grid;grid-template-columns:minmax(130px,auto) 1fr;gap:6px 14px;overflow-wrap:anywhere'>"
            + "".join("<dt>" + label + "</dt><dd style='margin:0'>" + value + "</dd>" for label, value in rows) + "</dl></div>")


def _presenters(core):
    from agent_trajviz.i18n import paired_text as p

    class Composition:
        def __getattr__(self, key):
            return getattr(core.prompt_composition, key)

        def quality_note(self, detail):
            return _provenance(detail) if _body(detail) else core.prompt_composition.quality_note(detail)

    summary = _clone_native_function(core._summary, {"prompt_composition": Composition()})

    def changes(detail):
        if not _body(detail):
            return core._changes(detail)
        delta = detail.get("changes") or {}
        native_comparison = (delta.get("basis") == "previous_verified_native_assistant_request"
                             and delta.get("lineage_verified") is False)

        def text(template, **params):
            if template == _LINEAGE and native_comparison:
                return p("Compared with the captured request at the previous native assistant step. Native step order supplies the comparison baseline; no request-parent relationship was recorded.",
                         "与前一个原生助手步骤的已捕获请求比较。比较基线来自原生步骤顺序；未记录请求之间的父子关系。")
            if template in _NO_PREVIOUS:
                return p("No comparable captured request at the previous native assistant step is available.",
                         "前一个原生助手步骤没有可用于比较的已捕获请求。")
            return core.text(template, **params)

        def events(selected):
            if selected.get("prompt_events"):
                return core._recorded_events(selected)
            return ("<div class='pc-note'>" + p("This boundary is backed by a saved outbound HTTP body.",
                    "此边界的依据是已保存的出站 HTTP 请求体。") + "</div>")

        render = _clone_native_function(core._changes, {"text": text, "_recorded_events": events})
        return render(detail)

    return summary, changes


def _render_binding(core):
    from .deveco_prompt_chart import evolution_chart

    summary, changes = _presenters(core)
    native_outputs = _clone_native_function(core.outputs, {"evolution_chart": evolution_chart})

    @functools.wraps(core.outputs)
    def outputs(run, request_id=None, language="en"):
        result = native_outputs(run, request_id, language)
        detail = result[5]
        if getattr(run, "format", None) != "deveco" or not _body(detail):
            return result
        if isinstance(detail.get("component_inventory"), list) and isinstance(detail.get("component_delta"), dict):
            from .deveco_prompt_inspector import render_changes, render_inventory, request_choices

            selector = result[0]
            catalog = detail.get("request_catalog")
            if isinstance(selector, dict) and isinstance(catalog, list) and isinstance(selector.get("choices"), list):
                selector = {**selector, "choices": request_choices(catalog, selector["choices"], language)}
            # The header's component count reflects the exact inventory, including settings.
            summary_detail = {**detail, "components": detail["component_inventory"]}
            return (selector, result[1], summary(summary_detail, run), render_inventory(detail), render_changes(detail), detail)
        return (*result[:2], summary(detail, run), result[3], changes(detail), detail)

    return outputs


def _clone_callback(function, outputs, module, memo):
    if function in memo:
        return memo[function]
    values = []
    for cell in function.__closure__ or ():
        value = cell.cell_contents
        if inspect.isfunction(value) and value.__module__ == module:
            value = _clone_callback(value, outputs, module, memo)
        values.append(types.CellType(value))
    copied = types.FunctionType(function.__code__, {**function.__globals__, "outputs": outputs},
                                function.__name__, function.__defaults__, tuple(values) or None)
    copied.__kwdefaults__ = function.__kwdefaults__
    copied.__annotations__ = function.__annotations__
    memo[function] = functools.update_wrapper(copied, function)
    return memo[function]


def install(app) -> None:
    """Bind a narrowly checked renderer to this app; fail closed on core drift."""
    import agent_trajviz
    from agent_trajviz.insight.ui import prompt_tab as core

    if getattr(app, "_trajviz_deveco_request_ui", False):
        return
    signatures = ((core.outputs, ("run", "request_id", "language")),
                  (core._summary, ("detail", "run")), (core._changes, ("detail",)),
                  (core._recorded_events, ("detail",)),
                  (core.evolution_chart, ("data", "selected_id", "language")))
    if (agent_trajviz.__version__ != "0.7.1"
            or any(tuple(inspect.signature(fn).parameters) != expected for fn, expected in signatures)
            or "_summary" not in core.outputs.__code__.co_names
            or "_changes" not in core.outputs.__code__.co_names
            or "evolution_chart" not in core.outputs.__code__.co_names
            or "_recorded_events" not in core._changes.__code__.co_names
            or "quality_note" not in core._summary.__code__.co_names):
        raise BridgeError("dashboard_incompatible", "TrajViz's captured-request render seam changed.")
    entries = [entry for entry in app.fns.values()
               if getattr(entry.fn, "__module__", "") == core.__name__
               and getattr(entry.fn, "__name__", "") in _CALLBACKS]
    counts = {name: sum(entry.fn.__name__ == name for entry in entries) for name in _CALLBACKS}
    if (counts != _CALLBACKS or any(tuple(getattr(out, "elem_id", None) for out in entry.outputs) != _OUTPUTS
                                  for entry in entries)):
        raise BridgeError("dashboard_incompatible", "TrajViz's Prompt Evolution callbacks changed.")
    for entry in entries:
        expected = ("move",) if entry.fn.__name__ in {"on_prompt_previous", "on_prompt_next"} else ("show",)
        if entry.fn.__code__.co_freevars != expected:
            raise BridgeError("dashboard_incompatible", "TrajViz's Prompt Evolution closure changed.")
        helper = entry.fn.__closure__[0].cell_contents
        helper_vars = inspect.getclosurevars(helper).nonlocals
        show = helper_vars.get("show") if expected == ("move",) else helper
        if not inspect.isfunction(show) or "outputs" not in show.__code__.co_names:
            raise BridgeError("dashboard_incompatible", "TrajViz's Prompt Evolution output binding changed.")
    output = _render_binding(core)
    memo = {}
    replacements = [(entry, _clone_callback(entry.fn, output, core.__name__, memo)) for entry in entries]
    for entry, callback in replacements:
        entry.fn = callback
    app._trajviz_deveco_request_ui = True
