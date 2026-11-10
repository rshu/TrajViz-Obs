"""Saved attribution evidence and isolated offline operations through plugin UI."""
from __future__ import annotations

import inspect
import json
import shutil
from pathlib import Path

import gradio as gr
import pytest
from agent_trajviz.awe import saved
from agent_trajviz.insight.ui import awe_tab

from trajviz_obs.attribution_ui import adapt_attribution_workspace
from trajviz_obs.dashboard_ui import decorate_dashboard


def _workspace():
    with gr.Blocks() as app:
        with gr.Tabs():
            refs = awe_tab.layout()
        awe_tab.bind(refs, app)
    return app, refs


def _fn(app, name):
    return next(entry.fn for entry in app.fns.values() if getattr(entry.fn, "__name__", "") == name)


def _saved(root):
    root.mkdir()
    case = {"case_id": "claude_code__case", "agent": "claude_code", "instance_id": "case",
            "blame_status": "primary", "task": {"problem_statement": "Recorded AWE observation <script>inert()</script>"},
            "faults": [{"capability": "code_editing", "error_type": "incorrect_patch", "blame_weight": 1,
                        "is_primary": True, "evidence_chain": {"strength": "deductive"}}]}
    (root / "faults.json").write_text(json.dumps([case]))
    (root / "INSIGHTS.md").write_text("# Original AWE report\nExact recorded wording.")
    return root


def test_workspace_retains_callback_contracts_and_distinct_tab():
    native, _ = _workspace()
    contracts = {key: (entry.fn, entry.inputs, entry.outputs, entry.targets, entry.api_name)
                 for key, entry in native.fns.items()}
    page = decorate_dashboard(native, "insight")
    assert any(isinstance(block, gr.Tab) and block.label == "Attribution workspace" for block in page.blocks.values())
    for key, (original, inputs, outputs, targets, api) in contracts.items():
        entry = page.fns[key]
        assert entry.inputs == inputs and entry.outputs == outputs and entry.api_name == api
        assert [event for _, event in entry.targets] == [event for _, event in targets]
        assert inspect.unwrap(entry.fn) is inspect.unwrap(original)
    wrapped = {key: entry.fn for key, entry in page.fns.items()}
    adapt_attribution_workspace(page)
    assert wrapped == {key: entry.fn for key, entry in page.fns.items()}
    workspace = next(block for block in page.blocks.values()
                     if isinstance(block, gr.Tab) and block.label == "Attribution workspace")
    notes = [block for block in page.blocks.values() if "obs-attribution-worker-note" in (block.elem_classes or [])]
    assert len(notes) == 1 and notes[0] in workspace.children
    assert "/obs attribution" in notes[0].value and "group child evidence by agent role" in notes[0].value
    assert "--reference-root DIR --agent opencode --instance ID" in notes[0].value
    assert any(getattr(entry.fn, "__name__", "") == "on_awe_operation" for entry in page.fns.values())


def test_import_filter_export_and_failure_preserve_original_evidence(tmp_path):
    source = _saved(tmp_path / "recorded-AWE-run")
    original = {p.name: p.read_bytes() for p in source.iterdir()}
    page = decorate_dashboard(_workspace()[0], "insight")
    loaded = _fn(page, "on_awe_import")(None, str(source))
    run = loaded[0]
    assert len(loaded) == 17 and isinstance(run, saved.SavedRun)
    assert "Imported 1 cases" in loaded[1] and "No model request was sent" in loaded[1]
    assert "Recorded AWE observation" in loaded[6] and "&lt;script&gt;inert()&lt;/script&gt;" in loaded[6]
    assert "Original AWE report" in loaded[13]
    filtered = _fn(page, "on_awe_filter")(run, "claude_code", "All outcomes", "case", "Attribution")
    assert filtered[0].choices == [("claude_code / case · failed", "claude_code__case")]
    exported, directory = _fn(page, "on_awe_export")(run, None)
    try:
        assert saved.load_run(exported).raw_artifacts == run.raw_artifacts
        assert str(Path(directory).parent) == str(Path(exported).parent.parent)
    finally:
        shutil.rmtree(directory)
    failed = _fn(page, "on_awe_import")(None, "")
    assert failed[0] is None and failed[6] == failed[8] == failed[13] == "" and failed[-1] is None
    assert "Import failed" in failed[1]
    assert {p.name: p.read_bytes() for p in source.iterdir()} == original


def test_workspace_settings_stay_session_only_and_preflight_runs_offline(tmp_path):
    reference = tmp_path / "reference"
    requirements = reference / "data/requirements"
    requirements.mkdir(parents=True)
    patch = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 0\n+value = 1\n"
    (requirements / "case.json").write_text(json.dumps({"instance_id": "case", "repo": "controlled/project",
        "base_commit": "baseline", "problem_statement": "Fix the source behavior.", "gold_patch": patch,
        "FAIL_TO_PASS": ["test_fix"], "PASS_TO_PASS": []}))
    patches = reference / "data/patch/claude_code"
    patches.mkdir(parents=True)
    (patches / "case.diff").write_text(patch)
    (reference / "data/eval_claude_code.json").write_text(json.dumps({"case": False}))
    app, _ = _workspace()
    page = decorate_dashboard(app, "insight")
    values = {**awe_tab._DEFAULTS, "reference_root": str(reference), "project_data": str(tmp_path / "cache"),
              "output_root": str(tmp_path / "operations"), "agents": "claude_code", "base_url": "https://example.invalid/v1"}
    args = [values[name] for name in awe_tab._PREF_FIELDS]
    applied = _fn(page, "on_awe_apply")(*args, "controlled-session-key", False, True, None)
    settings, cleared, clear_toggle, status, remembered, capabilities = applied
    assert settings.api_key == "controlled-session-key" and cleared == "" and clear_toggle is False
    assert "Attribution settings applied" in status and "No operation was started" in status
    assert "controlled-session-key" not in json.dumps(remembered) + capabilities
    assert not (tmp_path / "operations").exists()
    restored = _fn(page, "on_awe_restore")(remembered)
    assert restored[0] is None and ">Attribution connection preferences restored" in restored[-2]
    assert "controlled-session-key" not in json.dumps(restored, default=str)
    result, logs, path, active, network, *_ = _fn(page, "on_awe_operation")(
        settings, "preflight", "", "", "", "{}", False)
    assert result["success"], (result["reason"], logs)
    assert result["operation"] == "preflight" and network is False
    assert active.api_key == "controlled-session-key" and active.allow_network is False
    assert "controlled-session-key" not in json.dumps(result) + logs
    manifest = json.loads((Path(path) / "operation-manifest.json").read_text())
    assert manifest["network_enabled"] is False and manifest["api_key_stored"] is False
    reset = _fn(page, "on_awe_reset")()
    assert reset[0] is None and ">Attribution settings cleared.</span>" in reset[-2]


def test_workspace_errors_use_public_wording_without_mutating_settings():
    page = decorate_dashboard(_workspace()[0], "insight")
    with pytest.raises(gr.Error, match="Apply attribution settings"):
        _fn(page, "on_awe_operation")(None, "preflight", "", "", "", "{}", False)
    with pytest.raises(gr.Error, match="saved attribution run"):
        _fn(page, "on_awe_export")(None, None)


def test_saved_workspace_stays_available_after_native_load_failure(tmp_path, fixture_session):
    from trajviz_obs.dashboard_cmd import build_preloaded_insight

    page = build_preloaded_insight(str(fixture_session))
    loaded = _fn(page, "on_awe_import")(None, str(_saved(tmp_path / "saved")))
    run = loaded[0]
    entry = next(entry for entry in page.fns.values() if getattr(entry.fn, "__name__", "") == "_do_load")
    failed = entry.fn(None, False, "")
    assert any(isinstance(component, gr.Tabs) and value.get("visible") is True
               for component, value in failed.items() if isinstance(value, dict))
    workspace_state = next(entry for entry in page.fns.values()
                           if getattr(entry.fn, "__name__", "") == "on_awe_import").outputs[0]
    assert workspace_state not in entry.outputs
    case = _fn(page, "on_awe_case")(run, "claude_code__case", "Attribution")
    assert "Recorded AWE observation" in case[0]
