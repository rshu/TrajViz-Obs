"""Component inspection displays exact values without inventing missing changes."""
from __future__ import annotations

import copy
from html.parser import HTMLParser

from agent_trajviz.insight.ui import prompt_tab
from test_deveco_prompt_components import detail
from test_deveco_request_ui import Run
from test_deveco_request_ui import detail as ui_detail

from trajviz_obs.deveco_prompt_components import compare_component_inventory
from trajviz_obs.deveco_prompt_inspector import render_changes, render_inventory, request_choices
from trajviz_obs.deveco_request_ui import _render_binding


def inspected(before, after):
    previous = detail(before, 'request-1', step_index=1)
    selected = detail(after, 'request-2', step_index=3)
    selected['component_delta'] = compare_component_inventory(selected, previous, basis='previous_verified_native_assistant_request')
    return selected


class Markup(HTMLParser):
    def __init__(self):
        super().__init__(); self.tags = []; self.ids = []; self.text = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        values = dict(attrs)
        if values.get('id'):
            self.ids.append(values['id'])

    def handle_data(self, data):
        self.text.append(data)


def test_inventory_exposes_exact_schema_full_message_role_pointer_and_projection_size():
    body = {'system': 'Follow the HomeTrans migration instructions.',
            'tools': [{'type': 'function', 'function': {'name': 'read_file', 'description': 'Read source',
                       'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path']}}}],
            'messages': [{'role': 'user', 'content': 'Migrate the countdown.'},
                         {'role': 'tool', 'tool_call_id': 'read-1', 'content': {'path': 'CountdownModel.ts', 'text': 'class Countdown {}'}}],
            'model': 'controlled-model', 'temperature': 0.2}
    selected = detail(body)
    before = copy.deepcopy(selected)
    rendered = render_inventory(selected)
    parsed = Markup(); parsed.feed(rendered)
    text = ''.join(parsed.text)
    assert 'Components in this request' in text
    for value in ['Follow the HomeTrans', 'read_file', 'description', 'properties', 'required', 'CountdownModel.ts',
                  'class Countdown {}', 'temperature', 'controlled-model', '/tools/0', '/messages/1']:
        assert value in text
    assert 'Canonical JSON bytes' in text and 'not slices of the original HTTP bytes' in text
    assert 'Component SHA-256' in text and 'Evidence JSON' in text
    assert '系统 / 开发者指令' in rendered and '生成与服务商设置' in rendered
    assert len(parsed.ids) == len(set(parsed.ids)) == len(selected['component_inventory'])
    assert selected == before


def test_changes_show_added_removed_updated_and_literal_before_after_values():
    selected = inspected(
        {'messages': [{'id': 'm1', 'role': 'user', 'content': 'OLD_FEEDBACK'},
                      {'id': 'removed', 'role': 'assistant', 'content': 'REMOVED_DETAIL'}], 'temperature': 0.1},
        {'messages': [{'id': 'm1', 'role': 'user', 'content': 'NEW_FEEDBACK'},
                      {'id': 'added', 'role': 'tool', 'content': 'ADDED_DETAIL'}], 'temperature': 0.4})
    original = copy.deepcopy(selected)
    rendered = render_changes(selected)
    parsed = Markup(); parsed.feed(rendered); text = ''.join(parsed.text)
    for label in ['Added', 'Removed', 'Updated', 'Moved', 'Unchanged', 'OLD_FEEDBACK', 'NEW_FEEDBACK',
                  'REMOVED_DETAIL', 'ADDED_DETAIL', '0.1', '0.4', 'request-1', 'request-2', '/messages/0', '/temperature']:
        assert label in text
    assert 'dpi-diff-add' in rendered and 'dpi-diff-remove' in rendered
    assert 'does not prove a request-parent relationship' in text
    assert 'Baseline native step' in text and 'Exact-value difference' in text
    assert selected == original


def test_no_baseline_has_unknown_status_not_fabricated_added_or_removed_values():
    selected = detail({'messages': [{'role': 'user', 'content': 'PRIVATE_BUT_CURRENT'}]})
    selected['component_delta'] = compare_component_inventory(selected, None)
    rendered = render_changes(selected)
    assert 'No comparable captured baseline' in rendered
    assert 'missing evidence is not an empty prompt' in rendered
    assert 'dpi-counts' not in rendered.split('</style>')[1]
    assert 'PRIVATE_BUT_CURRENT' not in rendered
    inventory = render_inventory(selected)
    assert 'Not compared' in inventory and 'PRIVATE_BUT_CURRENT' in inventory


def test_prompt_text_and_tool_names_cannot_create_html_or_event_handlers():
    payload = '</summary><script>steal()</script><img src=x onerror=steal()>'
    selected = inspected({'messages': []}, {'tools': [{'name': payload, 'parameters': {'description': payload}}],
                                         'messages': [{'id': 'new', 'role': 'user', 'content': payload}]})
    for rendered in (render_inventory(selected), render_changes(selected)):
        parsed = Markup(); parsed.feed(rendered)
        assert 'script' not in parsed.tags and 'img' not in parsed.tags
        assert '&lt;script&gt;' in rendered
        assert payload not in rendered


def test_large_content_is_explicitly_bounded_with_complete_json_pointer_access(monkeypatch):
    import trajviz_obs.deveco_prompt_inspector as inspector
    monkeypatch.setattr(inspector, 'MAX_VALUE_CHARACTERS', 60)
    monkeypatch.setattr(inspector, 'MAX_RENDER_CHARACTERS', 70)
    selected = inspected({'instructions': 'A' * 1000}, {'instructions': 'B' * 1000})
    rendered = render_inventory(selected)
    assert 'Preview limited' in rendered and '/instructions' in rendered
    assert 'complete component is available in Evidence JSON' in rendered
    assert 'B' * 61 not in rendered
    changes = render_changes(selected)
    assert 'component_delta.rows' in changes
    assert 'B' * 61 not in changes and 'A' * 61 not in changes


def test_component_list_bound_leaves_complete_inventory_accessible(monkeypatch):
    import trajviz_obs.deveco_prompt_inspector as inspector
    monkeypatch.setattr(inspector, 'MAX_COMPONENT_ROWS', 2)
    selected = detail({'messages': [{'role': 'user', 'content': str(i)} for i in range(5)]})
    rendered = render_inventory(selected)
    assert rendered.count('id="dpi-component-') == 2
    assert 'All components remain in Evidence JSON' in rendered
    assert len(selected['component_inventory']) == 5


def test_retained_components_are_collapsed_without_duplicating_their_content_in_changes():
    selected = inspected({'system': 'UNCHANGED_SYSTEM_CONTENT', 'temperature': 0.1},
                         {'system': 'UNCHANGED_SYSTEM_CONTENT', 'temperature': 0.4})
    rendered = render_changes(selected)
    assert '<details class="dpi-retained"><summary>' in rendered
    assert 'UNCHANGED_SYSTEM_CONTENT' not in rendered
    assert 'Open Prompt snapshot' in rendered


def test_selector_names_native_steps_physical_attempts_and_unknown_child_without_reordering():
    catalog = [{'id': 'request-one', 'session_id': 's', 'assistant_message_id': 'a', 'step_index': 7,
                'native_mapping': {'status': 'verified'}, 'actor_kind': 'main', 'purpose': 'assistant'},
               {'id': 'request-two', 'session_id': 's', 'assistant_message_id': 'a', 'step_index': 7,
                'native_mapping': {'status': 'verified'}, 'actor_kind': 'main', 'purpose': 'assistant'},
               {'id': 'request-three', 'session_id': 'child', 'actor_kind': 'unknown_child',
                'native_mapping': {'status': 'unlinked'}, 'purpose': 'assistant'}]
    choices = [('old1', 'request-one'), ('old2', 'request-two'), ('old3', 'request-three')]
    result = request_choices(catalog, choices, 'en')
    assert [row[1] for row in result] == [row[1] for row in choices]
    assert 'Step 7' in result[0][0] and 'Attempt 1/2' in result[0][0]
    assert 'Attempt 2/2' in result[1][0]
    assert 'Unlinked request' in result[2][0] and 'Unidentified subagent' in result[2][0]
    chinese = request_choices(catalog, choices, 'zh-CN')
    assert '步骤 7' in chinese[0][0] and '尝试 2/2' in chinese[1][0] and '未知子代理' in chinese[2][0]


def test_actual_captured_output_panels_use_inventory_delta_and_keep_original_json():
    selected = inspected({'system': 'KEEP', 'messages': []},
                         {'system': 'KEEP', 'messages': [{'id': 'new', 'role': 'user', 'content': 'EXACT_NEW_FEEDBACK'}]})
    selected = ui_detail(**selected)
    selected['request_catalog'] = [{'id': selected['id'], 'session_id': 'root', 'assistant_message_id': 'a',
                                  'step_index': 3, 'purpose': 'assistant', 'actor_kind': 'main',
                                  'native_mapping': {'status': 'verified'}}]
    original = copy.deepcopy(selected)
    result = _render_binding(prompt_tab)(Run(selected))
    assert 'data-obs-prompt-inventory="true"' in result[3]
    assert 'data-obs-prompt-delta="true"' in result[4]
    assert 'EXACT_NEW_FEEDBACK' in result[3] and 'EXACT_NEW_FEEDBACK' in result[4]
    assert 'Step 3' in result[0]['choices'][0][0]
    assert result[5] is selected and selected == original


def test_delta_index_links_to_its_details_and_large_old_values_reference_baseline_json(monkeypatch):
    import trajviz_obs.deveco_prompt_inspector as inspector
    monkeypatch.setattr(inspector, 'MAX_VALUE_CHARACTERS', 40)
    selected = inspected({'instructions': 'OLD_ONLY_' * 30}, {'instructions': 'NEW_ONLY_' * 30})
    rendered = render_changes(selected)
    parsed = Markup(); parsed.feed(rendered)
    assert 'href="#dpi-change-' in rendered
    assert len(parsed.ids) == 1 and 'id="' + parsed.ids[0] + '"' in rendered
    assert 'component_delta.rows[0].before.content' in rendered
    assert 'belongs to the baseline request, not the selected request' in rendered
    assert 'request-1' in rendered


def test_partial_inventory_is_marked_and_cannot_look_like_complete_prompt_membership():
    selected = detail({'instructions': 'LIMITED'})
    selected['component_inventory_coverage'] = {'complete': False, 'loaded_components': 1, 'total_components': 9000, 'limit': 4096}
    selected['component_delta'] = compare_component_inventory(selected, None)
    for rendered in (render_inventory(selected), render_changes(selected)):
        assert 'component inventory is partial' in rendered
        assert 'complete Evidence JSON' in rendered and 'serialized_body' in rendered
        assert '9000' in rendered


def test_untrusted_long_names_and_paths_cannot_expand_metadata_without_bound():
    oversized = 'NAME_' * 10000
    selected = detail({oversized: 'short'})
    rendered = render_inventory(selected)
    assert len(rendered) < 30000
    assert oversized not in rendered
    assert 'Compact labels and metadata can be shortened' in rendered
    assert selected['component_inventory'][0]['json_pointer'] == '/' + oversized
