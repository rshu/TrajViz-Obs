"""Bounded, escaped component inventory and exact-value changes for DevEco bodies.

Only host-authorized request detail reaches these renderers. Content is never
translated. The raw request and complete inventory remain in Evidence JSON when
an HTML preview reaches its explicit limit.
"""
from __future__ import annotations

import difflib
import hashlib
import html
import json

MAX_COMPONENT_ROWS = 600
MAX_VALUE_CHARACTERS = 20_000
MAX_RENDER_CHARACTERS = 160_000
MAX_DIFF_LINES = 200
MAX_LABEL_CHARACTERS = 240
MAX_METADATA_CHARACTERS = 1024

_GROUPS = (("system", "System / developer instructions", "系统 / 开发者指令"),
           ("tools", "Tool definitions", "工具定义"),
           ("messages", "Conversation and tool results", "对话与工具结果"),
           ("parameters", "Generation and provider settings", "生成与服务商设置"))
_STATUSES = {"added": ("Added", "新增"), "removed": ("Removed", "移除"),
             "updated": ("Updated", "更新"), "moved": ("Moved", "移动"),
             "unchanged": ("Unchanged", "未变"), "uncompared": ("Not compared", "未比较")}
_CSS = """<style>
.obs-prompt-inspector{color:var(--ov-text,#172d3a);min-width:0;overflow-wrap:anywhere}
.obs-prompt-inspector *{box-sizing:border-box}
.obs-prompt-inspector h3,.obs-prompt-inspector h4{margin:0 0 8px;color:inherit}
.obs-prompt-inspector p{margin:7px 0 12px}
.obs-prompt-inspector .dpi-groups{display:grid;gap:15px;margin-top:15px}
.obs-prompt-inspector .dpi-group>summary{font-weight:600}
.obs-prompt-inspector details{min-width:0;margin:7px 0}
.obs-prompt-inspector summary{cursor:pointer;overflow-wrap:anywhere}
.obs-prompt-inspector summary:focus-visible{outline:2px solid var(--ov-accent,#2563eb);outline-offset:3px}
.obs-prompt-inspector .dpi-component{border-bottom:1px solid var(--ov-border,#dbe6e9);padding:8px 0}
.obs-prompt-inspector .dpi-component>summary{display:list-item;font-weight:500}
.obs-prompt-inspector .dpi-status{display:inline-block;font-size:12px;padding:2px 7px;margin-inline-start:8px;border-radius:4px;background:var(--background-fill-secondary,#eef3f6);color:inherit}
.obs-prompt-inspector .dpi-added{color:var(--ov-success,#15803d)}
.obs-prompt-inspector .dpi-removed{color:var(--ov-bad,#b91c1c)}
.obs-prompt-inspector .dpi-updated{color:var(--ov-accent,#2563eb)}
.obs-prompt-inspector .dpi-moved{color:var(--ov-warn,#92600c)}
.obs-prompt-inspector .dpi-counts{display:flex;flex-wrap:wrap;gap:7px 18px;margin:12px 0}
.obs-prompt-inspector .dpi-counts strong{font-variant-numeric:tabular-nums;margin-inline-end:5px}
.obs-prompt-inspector .dpi-meta{display:grid;grid-template-columns:minmax(100px,max-content) minmax(0,1fr);gap:5px 12px;font-size:12px;margin:12px 0}
.obs-prompt-inspector .dpi-meta dt{color:var(--ov-muted,#526a77)}
.obs-prompt-inspector .dpi-meta dd{margin:0;min-width:0}
.obs-prompt-inspector .dpi-note{font-size:12px;color:var(--ov-muted,#526a77)}
.obs-prompt-inspector .dpi-warning{padding:9px 12px;border-left:3px solid var(--ov-warn,#92600c);background:var(--background-fill-secondary,#f1f5f9)}
.obs-prompt-inspector .dpi-pair{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:14px;margin:12px 0}
.obs-prompt-inspector pre{white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;max-height:480px;overflow:auto;margin:7px 0;padding:12px;background:var(--background-fill-secondary,#f4f7f8);color:inherit;border:1px solid var(--ov-border,#dbe6e9);border-radius:5px;font:12px/1.6 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.obs-prompt-inspector .dpi-diff-line{display:block;min-height:1.6em}
.obs-prompt-inspector .dpi-diff-add{background:color-mix(in srgb,var(--ov-success,#15803d) 12%,transparent)}
.obs-prompt-inspector .dpi-diff-remove{background:color-mix(in srgb,var(--ov-bad,#b91c1c) 12%,transparent)}
.obs-prompt-inspector .dpi-index{width:100%;border-collapse:collapse;font-size:12px;margin:12px 0}
.obs-prompt-inspector .dpi-index th,.obs-prompt-inspector .dpi-index td{text-align:start;padding:7px;border-bottom:1px solid var(--ov-border,#dbe6e9);vertical-align:top}
.obs-prompt-inspector .dpi-index code{white-space:normal;overflow-wrap:anywhere}
.obs-prompt-inspector .dpi-retained{font-size:12px}
@media(max-width:650px){.obs-prompt-inspector .dpi-pair{grid-template-columns:minmax(0,1fr)}.obs-prompt-inspector .dpi-meta{grid-template-columns:minmax(0,1fr);gap:3px}.obs-prompt-inspector .dpi-meta dd{margin-bottom:6px}}
</style>"""


def _p(english: str, chinese: str) -> str:
    from agent_trajviz.i18n import paired_text
    return paired_text(english, chinese)


def _esc(value) -> str:
    return html.escape(str(value if value is not None else "—"), quote=True)


def _bounded(value, limit: int) -> str:
    value = str(value if value is not None else "—")
    return value if len(value) <= limit else value[:limit] + "…"


def _label(value) -> str:
    return _esc(_bounded(value, MAX_LABEL_CHARACTERS))


def _code(value) -> str:
    return "<code>" + _esc(_bounded(value, MAX_METADATA_CHARACTERS)) + "</code>"


def _mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def _rows(detail: dict) -> list[dict]:
    return [row for row in detail.get("component_inventory", []) if isinstance(row, dict)]


def _delta_rows(detail: dict) -> list[dict]:
    return [row for row in _mapping(detail.get("component_delta")).get("rows", []) if isinstance(row, dict)]


def _text(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)


class _Budget:
    def __init__(self):
        self.remaining = MAX_RENDER_CHARACTERS

    def preview(self, value) -> tuple[str, bool]:
        content = _text(value)
        limit = min(MAX_VALUE_CHARACTERS, self.remaining)
        shown = content[:limit]
        self.remaining -= len(shown)
        return shown, len(shown) < len(content)


def _preview(component: dict | None, budget: _Budget, *, missing: str = "", locator: str | None = None) -> str:
    if component is None:
        return '<p class="dpi-note">' + (missing or _p("Not present in this snapshot", "此快照中不存在")) + '</p>'
    value, clipped = budget.preview(component.get("content"))
    rendered = '<pre>' + _esc(value) + '</pre>' if value else '<pre>""</pre>' if not clipped else ''
    if clipped:
        if locator:
            rendered += ('<p class="dpi-warning">' + _p(
                "Preview limited. The complete baseline value is available in Evidence JSON at:",
                "预览已达到上限。完整基线值可在 Evidence JSON 的以下位置查看：") + ' ' + _code(locator)
                + '. ' + _p("This JSON pointer belongs to the baseline request, not the selected request:",
                             "以下 JSON Pointer 属于基线请求，并非所选请求：")
                + ' ' + _code(component.get("json_pointer")) + ' · '
                + _code(_mapping(component.get("source")).get("request_id")) + '</p>')
        else:
            rendered += ('<p class="dpi-warning">' + _p(
                "Preview limited. The complete component is available in Evidence JSON → request_body at this JSON pointer:",
                "预览已达到上限。完整组件可在 Evidence JSON → request_body 中通过以下 JSON Pointer 查看：")
                + ' ' + _code(component.get("json_pointer")) + '</p>')
    return rendered


def _statuses(row: dict) -> str:
    changes = row.get("changes")
    changes = changes if isinstance(changes, list) and changes else [row.get("status", "uncompared")]
    return ''.join('<span class="dpi-status dpi-' + status + '">' + _p(*_STATUSES[status]) + '</span>'
                   for status in _STATUSES if status in changes)


def _token(detail: dict, row: dict) -> str:
    value = str(detail.get("id")) + '\0' + str(row.get("id"))
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def _meta(component: dict) -> str:
    source = _mapping(component.get("source"))
    pairs = [(_p("Request JSON pointer", "请求 JSON Pointer"), _code(component.get("json_pointer"))),
             (_p("Role", "角色"), _code(component.get("role"))),
             (_p("Inventory position (zero-based)", "组件列表位置（从零开始）"), _code(component.get("position"))),
             (_p("Canonical JSON bytes", "规范 JSON 字节数"), _code(component.get("utf8_bytes"))),
             (_p("Component SHA-256", "组件 SHA-256"), _code(component.get("content_sha256"))),
             (_p("Capture source", "捕获来源"), _code(source.get("kind", "outbound_http_body"))),
             (_p("Source request", "来源请求"), _code(source.get("request_id")))]
    return '<dl class="dpi-meta">' + ''.join('<dt>' + key + '</dt><dd>' + value + '</dd>' for key, value in pairs) + '</dl>'


def _coverage_note(detail: dict) -> str:
    coverage = _mapping(detail.get("component_inventory_coverage"))
    if coverage.get("complete") is not False:
        return ""
    return ('<p class="dpi-warning">' + _p(
        "The component inventory is partial because its limit was reached. Unlisted fields are still present in the complete Evidence JSON → request_body and serialized_body. Step-to-step changes are unavailable for a partial inventory.",
        "组件列表已达到上限，因此仅展示部分组件。未列出的字段仍完整保留在 Evidence JSON → request_body 与 serialized_body 中。组件列表不完整时，不提供步骤间变化推断。")
        + ' ' + _code(coverage.get("loaded_components")) + ' / ' + _code(coverage.get("total_components")) + '</p>')


def _size_note() -> str:
    return '<p class="dpi-note">' + _p(
        "Component sizes and hashes use canonical JSON UTF-8 values, not slices of the original HTTP bytes. String values are displayed decoded; structured values are formatted for reading. Compact labels and metadata can be shortened; complete values remain in Evidence JSON, including the original request_body and serialized_body.",
        "组件大小与哈希基于规范 JSON 的 UTF-8 值，并非原始 HTTP 字节切片。字符串按解码后的内容显示，结构化值为便于阅读而排版。紧凑标签与元数据可能缩短；完整值保留在 Evidence JSON 中，包括原始 request_body 与 serialized_body。") + '</p>'


def _baseline(detail: dict, delta: dict) -> str:
    selected = _code(detail.get("request_id") or detail.get("id"))
    rows = [(_p("Selected request", "所选请求"), selected)]
    if type(detail.get("step_index")) is int:
        rows.append((_p("Native assistant step", "原生助手步骤"), _code(detail["step_index"])))
    baseline = delta.get("baseline_request_id")
    if baseline:
        rows.append((_p("Comparison baseline", "对比基线"), _code(baseline)))
    if type(delta.get("baseline_step_index")) is int:
        rows.append((_p("Baseline native step", "基线原生步骤"), _code(delta["baseline_step_index"])))
    output = '<dl class="dpi-meta">' + ''.join('<dt>' + key + '</dt><dd>' + value + '</dd>' for key, value in rows) + '</dl>'
    if delta.get("available") is not True:
        output += '<p class="dpi-warning">' + _p(
            "No comparable captured baseline is available. These components are not classified as added or removed; missing evidence is not an empty prompt.",
            "没有可比较的捕获基线。这些组件不会被标记为新增或移除；证据缺失不等于提示词为空。") + '</p>'
    elif delta.get("lineage_verified") is not True:
        output += '<p class="dpi-note">' + _p(
            "The comparison uses the recorded native step order. It shows body differences; it does not prove a request-parent relationship or model influence.",
            "对比基于已记录的原生步骤顺序，展示请求体差异，不证明请求之间的父子关系或模型受到何种影响。") + '</p>'
    else:
        output += '<p class="dpi-note">' + _p(
            "The baseline follows the recorded request-predecessor link. Content differences do not establish what influenced the model.",
            "基线来自已记录的请求前驱关联。内容差异不代表已证明哪些信息影响了模型。") + '</p>'
    return output


def render_inventory(detail: dict) -> str:
    """All current components, grouped by actual request membership and path."""
    components, delta = _rows(detail), _mapping(detail.get("component_delta"))
    lookup = {str(_mapping(row.get("after")).get("id")): row for row in _delta_rows(detail) if row.get("after") is not None}
    budget = _Budget()
    parts = [_CSS, '<div class="obs-prompt-inspector" data-obs-prompt-inventory="true"><h3>',
             _p("Components in this request", "此请求中的组件"), '</h3><p>',
             _p("Open a component to inspect its captured value, role, location, and content hash.",
                "展开组件可查看其捕获值、角色、位置与内容哈希。"), '</p>', _coverage_note(detail), _size_note(), '<div class="dpi-groups">']
    rendered = 0
    for group, english, chinese in _GROUPS:
        rows = [row for row in components if row.get("group") == group]
        parts.append('<details class="dpi-group" open><summary>' + _p(english, chinese) + ' · ' + str(len(rows)) + '</summary>')
        if not rows:
            parts.append('<p class="dpi-note">' + _p("No components in this group", "此分组没有组件") + '</p>')
        for component in rows[:max(0, MAX_COMPONENT_ROWS - rendered)]:
            row = lookup.get(str(component.get("id")), {"status": "uncompared"})
            identifier = _token(detail, component)
            parts += ['<details class="dpi-component" id="dpi-component-' + identifier + '"><summary>',
                      _label(component.get("label") or component.get("json_pointer")), _statuses(row), '</summary>',
                      _meta(component), '<h4>', _p("Captured value", "捕获值"), '</h4>', _preview(component, budget), '</details>']
            rendered += 1
        parts.append('</details>')
    parts.append('</div>')
    if len(components) > rendered:
        parts.append('<p class="dpi-warning">' + _p(
            "The HTML component list is limited. All components remain in Evidence JSON → component_inventory.",
            "HTML 组件列表已达到上限。所有组件仍保留在 Evidence JSON → component_inventory 中。") + '</p>')
    if delta.get("available") is not True:
        parts.append('<p class="dpi-note">' + _p("Change status is unavailable until a comparable baseline is captured.", "捕获可比较基线后才能显示变化状态。") + '</p>')
    return ''.join(parts) + '</div>'


def _diff(before: dict | None, after: dict | None, budget: _Budget) -> str:
    old, old_clip = budget.preview(before.get("content")) if before is not None else ("", False)
    new, new_clip = budget.preview(after.get("content")) if after is not None else ("", False)
    old_lines, new_lines = old.splitlines(), new.splitlines()
    lines = difflib.unified_diff(old_lines[:MAX_DIFF_LINES], new_lines[:MAX_DIFF_LINES],
                                 fromfile='Before', tofile='After', lineterm='', n=3)
    rendered = []
    for index, line in enumerate(lines):
        if index >= MAX_DIFF_LINES:
            old_clip = True
            break
        kind = ' dpi-diff-add' if line.startswith('+') and not line.startswith('+++') else ' dpi-diff-remove' if line.startswith('-') and not line.startswith('---') else ''
        rendered.append('<span class="dpi-diff-line' + kind + '">' + _esc(line) + '</span>')
    output = '<pre class="dpi-diff">' + ''.join(rendered) + '</pre>' if rendered else '<p class="dpi-note">' + (_p("Diff preview unavailable within the remaining HTML budget", "剩余 HTML 配额不足以展示差异预览") if old_clip or new_clip else _p("No differences in the displayed text", "显示的文本中没有差异")) + '</p>'
    if old_clip or new_clip or len(old_lines) > MAX_DIFF_LINES or len(new_lines) > MAX_DIFF_LINES:
        output += '<p class="dpi-warning">' + _p(
            "Diff preview limited to 200 lines and 20,000 characters per value, within the shared HTML budget. Inspect the full before/after values in Evidence JSON → component_delta.rows.",
            "差异预览受共享 HTML 配额限制，每个值最多 20,000 字符、200 行。完整前后值可在 Evidence JSON → component_delta.rows 中查看。") + '</p>'
    return output


def render_changes(detail: dict) -> str:
    """A delta index with exact-value diffs; unchanged content stays collapsed."""
    delta = _mapping(detail.get("component_delta"))
    rows, counts = _delta_rows(detail), _mapping(delta.get("counts"))
    parts = [_CSS, '<div class="obs-prompt-inspector" data-obs-prompt-delta="true"><h3>',
             _p("What changed in this prompt?", "这次提示词改变了什么？"), '</h3>', _coverage_note(detail), _baseline(detail, delta)]
    if delta.get("available") is not True:
        parts += ['<p>', _p("Inspect the captured components in Prompt snapshot; no step-to-step difference is inferred.",
                           "可在 Prompt snapshot 中检查已捕获组件，此处不推断步骤之间的差异。"), '</p></div>']
        return ''.join(parts)
    parts.append('<div class="dpi-counts">')
    for status in ('added', 'removed', 'updated', 'moved', 'unchanged'):
        count = counts.get(status)
        count = count if type(count) is int and count >= 0 else sum(status in row.get('changes', [row.get('status')]) for row in rows)
        parts.append('<span class="dpi-' + status + '"><strong>' + str(count) + '</strong>' + _p(*_STATUSES[status]) + '</span>')
    parts += ['</div><p class="dpi-note">', _p("Moved is tracked separately and may overlap updated or unchanged content.",
                                             "移动单独统计，可与更新或内容未变重叠。"), '</p>', _size_note()]
    changed = [row for row in rows if row.get('status') not in {'unchanged', 'uncompared'}]
    budget = _Budget()
    row_positions = {id(row): index for index, row in enumerate(rows)}
    if not changed:
        parts.append('<p>' + _p("All captured component values match the comparison baseline.", "所有捕获组件值均与对比基线一致。") + '</p>')
    else:
        parts.append('<table class="dpi-index"><thead><tr><th>' + _p("Component", "组件") + '</th><th>'
                     + _p("Change", "变化") + '</th><th>' + _p("Before → after location", "之前 → 之后的位置") + '</th></tr></thead><tbody>')
        for row in changed[:MAX_COMPONENT_ROWS]:
            component = _mapping(row.get('after') or row.get('before'))
            parts.append('<tr><td><a href="#dpi-change-' + _token(detail, row) + '">' + _label(component.get('label')) + '</a></td><td>' + _statuses(row) + '</td><td>'
                         + _code(_mapping(row.get('before')).get('json_pointer')) + ' → '
                         + _code(_mapping(row.get('after')).get('json_pointer')) + '</td></tr>')
        parts.append('</tbody></table>')
    for index, row in enumerate(changed[:MAX_COMPONENT_ROWS]):
        before = row.get('before') if isinstance(row.get('before'), dict) else None
        after = row.get('after') if isinstance(row.get('after'), dict) else None
        component = after or before or {}
        parts += ['<details class="dpi-component" id="dpi-change-' + _token(detail, row) + '"' + (' open' if index == 0 else '') + '><summary>',
                  _label(component.get('label')), _statuses(row), '</summary>', _meta(component)]
        if row.get('status') == 'moved':
            parts.append('<p class="dpi-note">' + _p("Captured content is unchanged; only its position changed.", "捕获内容未变，仅位置发生变化。") + '</p>')
        else:
            parts += ['<h4>', _p("Exact-value difference", "捕获值差异"), '</h4>', _diff(before, after, budget)]
        parts += ['<details><summary>', _p("Before and after captured values", "之前与之后的捕获值"), '</summary><div class="dpi-pair"><section><h4>',
                  _p("Before", "之前"), '</h4>', _meta(before) if before else '', _preview(before, budget, locator=f'component_delta.rows[{row_positions[id(row)]}].before.content'), '</section><section><h4>', _p("After", "之后"),
                  '</h4>', _meta(after) if after else '', _preview(after, budget), '</section></div></details></details>']
    if len(changed) > MAX_COMPONENT_ROWS:
        parts.append('<p class="dpi-warning">' + _p("The HTML change list is limited. All changes remain in Evidence JSON → component_delta.rows.", "HTML 变化列表已达到上限。所有变化仍保留在 Evidence JSON → component_delta.rows 中。") + '</p>')
    retained = [row for row in rows if row.get('status') == 'unchanged']
    if retained:
        parts.append('<details class="dpi-retained"><summary>' + _p("Unchanged components", "未变组件") + ' · ' + str(len(retained)) + '</summary><ul>')
        for row in retained[:MAX_COMPONENT_ROWS]:
            component = _mapping(row.get('after') or row.get('before'))
            parts.append('<li>' + _label(component.get('label')) + ' ' + _code(component.get('json_pointer')) + '</li>')
        parts += ['</ul><p class="dpi-note">', _p("Open Prompt snapshot to inspect retained component content.", "打开 Prompt snapshot 可检查保留组件的内容。"), '</p></details>']
    return ''.join(parts) + '</div>'


def request_choices(catalog: list[dict], choices: list, language: str) -> list:
    """Improve labels without changing IDs, request order, or selection authority."""
    chinese = language in {'zh', 'zh-CN'}
    lookup = {row.get('id'): row for row in catalog if isinstance(row, dict)}
    groups: dict[tuple, list] = {}
    for row in catalog:
        if not isinstance(row, dict):
            continue
        owner = row.get('assistant_message_id') if _mapping(row.get('native_mapping')).get('status') == 'verified' else None
        key = (row.get('session_id'), owner or row.get('invocation_id') or row.get('id'))
        groups.setdefault(key, []).append(row.get('id'))
    output = []
    for label, identifier in choices:
        row = lookup.get(identifier)
        if row is None:
            output.append((label, identifier))
            continue
        verified = _mapping(row.get('native_mapping')).get('status') == 'verified'
        step = row.get('step_index')
        owner = row.get('assistant_message_id') if verified else None
        key = (row.get('session_id'), owner or row.get('invocation_id') or row.get('id'))
        attempts = groups[key]
        attempt = attempts.index(identifier) + 1
        boundary = (('步骤 ' if chinese else 'Step ') + str(step) if verified and type(step) is int else
                    '标题请求' if chinese and row.get('purpose') == 'title' else 'Title request' if row.get('purpose') == 'title' else
                    '未关联请求' if chinese else 'Unlinked request')
        kind = row.get('actor_kind')
        actor = ('主代理' if chinese else 'Main agent') if kind == 'main' else ('未知子代理' if chinese else 'Unidentified subagent') if kind == 'unknown_child' else str(row.get('actor_name') or row.get('agent_id') or row.get('session_id') or '')
        attempt_label = ('尝试 ' if chinese else 'Attempt ') + str(attempt) + '/' + str(len(attempts))
        output.append((' · '.join((boundary, actor, attempt_label, str(identifier)[:8])), identifier))
    return output
