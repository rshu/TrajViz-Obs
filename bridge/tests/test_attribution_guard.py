"""Bundled native attribution keeps certified evidence and strict JSON results."""
from __future__ import annotations

import inspect
import json
import os
from dataclasses import asdict
from pathlib import Path

import agent_trajviz as tv
import pytest
from agent_trajviz.insight import attribution as native_attribution

from trajviz_obs import compat, dashboard_cmd, passthrough


def request(**overrides):
    return {"agent": "claude_code", "instance_id": "case", "source_path": "/synthetic/case.jsonl",
            "fmt": "claude_code", "expected_sha": "captured", "merged_sources": 3,
            "reference_root": "/synthetic/reference", **overrides}


@pytest.fixture
def seam(monkeypatch, tmp_path):
    """Isolate mutable engine caches and the per-app wrapper across tests."""
    from agent_trajviz.awe._engine import config

    monkeypatch.setattr(native_attribution, "diagnose", inspect.unwrap(native_attribution.diagnose))
    monkeypatch.setattr(config, "PROJECT_DATA", tmp_path / "cache")
    monkeypatch.setattr(config, "OUTPUT_DATA", tmp_path / "output")
    monkeypatch.setattr(config, "JUDGE_CACHE_DIR", tmp_path / "judge")
    native_attribution.configure(native_attribution._DEFAULT_ROOT)
    yield
    native_attribution.configure(native_attribution._DEFAULT_ROOT)


def test_guard_is_idempotent_and_keeps_every_native_identity_gate(seam, monkeypatch):
    calls = []
    expected = native_attribution.AttributionResult(True, mode="reference")

    def native(**kwargs):
        calls.append(kwargs)
        return expected

    monkeypatch.setattr(native_attribution, "diagnose", native)
    compat.install_attribution_guard()
    wrapped = native_attribution.diagnose
    compat.install_attribution_guard()
    assert native_attribution.diagnose is wrapped
    args = request()
    assert native_attribution.diagnose(**args) is expected
    assert calls == [args]


@pytest.mark.parametrize("message, expected", [
    (None, None),
    ("DECAF diagnosis unavailable (ValueError)", "Attribution diagnosis unavailable (ValueError)"),
    ("This DeCaF parser failed. DECAF is unavailable.", "This attribution parser failed. Attribution is unavailable."),
    ("Failed while loading DECAF.", "Failed while loading attribution."),
    ("AWE workspace", "Attribution workspace"),
    ("Use AWE operations for verified cached judgments.", "Use attribution operations for verified cached judgments."),
    ("Reference path /tmp/DECAF/data; DECAF.json; DECAF_AVAILABLE",
     "Reference path /tmp/DECAF/data; DECAF.json; DECAF_AVAILABLE"),
    ("AWE_JUDGE_MODEL; agent_trajviz.awe; /tmp/AWE/data", "AWE_JUDGE_MODEL; agent_trajviz.awe; /tmp/AWE/data"),
])
def test_public_terminology_does_not_rewrite_paths_or_api_identifiers(message, expected):
    assert compat.attribution_message(message) == expected


def test_old_checkout_configuration_never_selects_executable_code(seam, monkeypatch, tmp_path):
    hostile = tmp_path / "external"
    hostile.mkdir()
    (hostile / "awe.py").write_text('raise RuntimeError("external code must not be loaded")')
    monkeypatch.setenv("TRAJVIZ_ATTRIBUTION_PATH", str(hostile))
    monkeypatch.setenv("AWE_DECAF_PATH", str(hostile))
    environment = dict(os.environ)
    compat.install_attribution_guard()
    result = native_attribution.diagnose(**request(source_path=None, expected_sha=None,
                                                   reference_root=str(tmp_path / "missing")))
    assert result.available is False and "no gold reference" in result.reason
    assert dict(os.environ) == environment


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("location", ["scorecard", "fault", "arbiter", "dossier"])
def test_nonfinite_evidence_never_becomes_a_capability_verdict(seam, monkeypatch, number, location):
    def diagnose(**kwargs):
        result = native_attribution.AttributionResult(True, agent=kwargs["agent"],
            instance_id=kwargs["instance_id"], primary={"capability": "tool_use"}, used_judge=True)
        if location == "scorecard":
            result.scorecard = [native_attribution.CapabilityScore("tool_use", True, True, number, None, None)]
        elif location == "fault":
            result.faults = [{"capability": "tool_use", "blame_weight": number}]
        elif location == "dossier":
            result.dossier = {"evidence": [{"confidence": number}]}
        else:
            result.arbiter = {"evidence": [{"confidence": number}]}
        return result

    monkeypatch.setattr(native_attribution, "diagnose", diagnose)
    compat.install_attribution_guard()
    result = native_attribution.diagnose(**request())
    assert not result.available and "non-finite" in result.reason
    assert result.primary is None and result.faults == result.scorecard == [] and not result.used_judge
    assert result.agent == "claude_code" and result.instance_id == "case"
    json.dumps(asdict(result), allow_nan=False)


def test_observed_zero_attribution_weight_remains_valid(seam, monkeypatch):
    expected = native_attribution.AttributionResult(True, scorecard=[
        native_attribution.CapabilityScore("tool_use", True, False, 0.0, None, None)])
    monkeypatch.setattr(native_attribution, "diagnose", lambda **_: expected)
    compat.install_attribution_guard()
    assert native_attribution.diagnose(**request()) is expected


def test_unavailable_result_is_not_rewritten_in_place(seam, monkeypatch):
    expected = native_attribution.AttributionResult(False,
        reason="DECAF unavailable (DECAF import failed (ImportError))",
        notes=["Use AWE operations for verified cached judgments."])
    monkeypatch.setattr(native_attribution, "diagnose", lambda **_: expected)
    compat.install_attribution_guard()
    result = native_attribution.diagnose(**request())
    assert result.reason.startswith("Bundled attribution engine unavailable (ImportError)")
    assert "Reinstall TrajViz" in result.reason and "TRAJVIZ_ATTRIBUTION_PATH" not in result.reason
    assert result.notes == ["Use attribution operations for verified cached judgments."]
    assert expected.reason.startswith("DECAF unavailable")
    assert expected.notes[0].startswith("Use AWE")


def _native_claude(path: Path, *, subagent=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"type": "user", "sessionId": "case", "uuid": "user", "parentUuid": None,
         "message": {"role": "user", "content": "Read the source and run its tests."}},
        {"type": "assistant", "sessionId": "case", "uuid": "read", "parentUuid": "user",
         "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "read-call",
            "name": "Read", "input": {"file_path": "src/probe.py"}}]}},
        {"type": "user", "sessionId": "case", "uuid": "read-result", "parentUuid": "read",
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "read-call",
            "content": "def probe(): pass", "is_error": False}]}},
        {"type": "assistant", "sessionId": "case", "uuid": "test", "parentUuid": "read-result",
         "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "test-call",
            "name": "Bash", "input": {"command": "pytest -q tests/test_probe.py"}}]}},
        {"type": "user", "sessionId": "case", "uuid": "test-result", "parentUuid": "test",
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "test-call",
            "content": "1 failed\nExit code 1", "is_error": True}]}},
    ]
    if subagent:
        for row in rows:
            row.update(isSidechain=True, agentId="reviewer")
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def _benchmark(root: Path, agent: str, instance="case"):
    patch = "diff --git a/src/probe.py b/src/probe.py\n--- a/src/probe.py\n+++ b/src/probe.py\n@@ -1 +1 @@\n-def probe(): pass\n+def probe(): return 42\n"
    requirements = root / "data/requirements" / f"{instance}.json"
    requirements.parent.mkdir(parents=True, exist_ok=True)
    requirements.write_text(json.dumps({"instance_id": instance, "repo": "controlled/project", "base_commit": "baseline",
        "problem_statement": "Fix the source behavior.", "gold_patch": patch,
        "FAIL_TO_PASS": ["test_fix"], "PASS_TO_PASS": []}))
    submitted = root / "data/patch" / agent / f"{instance}.diff"
    submitted.parent.mkdir(parents=True, exist_ok=True)
    submitted.write_text(patch.replace("return 42", "return 0"))
    (root / "data" / f"eval_{agent}.json").write_text(json.dumps({instance: False}))


def test_bundled_claude_adapter_retains_native_tool_results_and_threads(seam, tmp_path):
    from agent_trajviz.awe.native import project_native

    path = _native_claude(tmp_path / "case.jsonl")
    _native_claude(tmp_path / "case/subagents/agent-reviewer.jsonl", subagent=True)
    run = compat.load_run(str(path))
    projected = project_native(run, "claude_code", "case")
    calls = {(step.native_tool, step.canonical_action) for step in projected.steps if step.type == "tool_call"}
    assert {("Read", "read"), ("Bash", "run_test")} <= calls
    results = [step for step in projected.steps if step.type == "tool_result"]
    assert any(step.tool_error and "1 failed" in step.output_excerpt for step in results)
    assert any(step.subagent_id for step in projected.steps)
    assert projected.source["sha256"] == run.sha256


def test_bundled_claude_tree_is_certified_and_changed_child_is_refused(seam, tmp_path):
    source = _native_claude(tmp_path / "data/trajectory/claude_code/case.jsonl")
    child = _native_claude(source.with_suffix("") / "subagents/agent-reviewer.jsonl", subagent=True)
    _benchmark(tmp_path, "claude_code")
    run = compat.load_run(str(source))
    result = passthrough.attribution(run, reference_root=str(tmp_path), agent="claude_code", instance_id="case")
    assert result["available"], result["reason"]
    assert len(result["scorecard"]) == 7 and result["used_judge"] is False
    assert result["dossier"]["source_certification"]["trajectory_sha256"] == run.sha256
    child.write_text(child.read_text() + "\n")
    stale = passthrough.attribution(run, reference_root=str(tmp_path), agent="claude_code", instance_id="case")
    assert not stale["available"] and "does not match the canonical" in stale["reason"]
    assert stale["faults"] == [] and stale["primary"] is None


def test_cli_and_browser_use_the_bundled_guard(seam, fixture_session, monkeypatch):
    calls = []

    def refused(**kwargs):
        calls.append(kwargs)
        return native_attribution.AttributionResult(False, reason="AWE evidence is unavailable")

    monkeypatch.setattr(native_attribution, "diagnose", refused)
    result = passthrough.attribution(compat.load_run(str(fixture_session)), reference_root="/synthetic/reference",
                                     agent="claude_code", instance_id="case")
    assert result["available"] is False and result["reason"] == "Attribution evidence is unavailable"
    app = dashboard_cmd.build_preloaded_insight(str(fixture_session))
    callback = next(block.fn for block in app.fns.values() if getattr(block.fn, "__name__", "") == "on_diagnose")
    run = tv.load(fixture_session)
    raw = {**run.raw, "_source_path": str(fixture_session), "_source_sha256": run.sha256}
    rendered, status = callback(raw, "claude_code", "case", "/synthetic/reference")
    assert "Attribution evidence is unavailable" in rendered and "No gold-grounded attribution" in status
    assert len(calls) == 2 and calls[-1]["expected_sha"] == run.sha256


def test_opencode_browser_manual_and_autoload_share_native_default_identities(seam, tmp_path, monkeypatch):
    calls = []

    def unavailable(**kwargs):
        calls.append(kwargs)
        return native_attribution.AttributionResult(False, agent=kwargs["agent"], instance_id=kwargs["instance_id"],
                                                    reason="Reference fixture not configured")

    source = tmp_path / "opaque-cache-generation" / "source-hash.json"
    source.parent.mkdir()
    source.write_text(json.dumps({"source_format": "opencode_v2", "schema_version": 1,
        "session": {"id": "ses_native"}, "messages": [{"id": "a", "type": "assistant",
        "content": [{"type": "text", "text": "Done"}]}], "children": []}))
    monkeypatch.setattr(native_attribution, "diagnose", unavailable)
    page = dashboard_cmd.build_preloaded_insight(str(source))
    callbacks = {getattr(entry.fn, "__name__", ""): entry.fn for entry in page.fns.values()}
    run = compat.load_run(str(source))
    raw = {**run.raw, "_source_path": str(source), "_source_sha256": run.sha256}
    original = json.dumps(raw, sort_keys=True)
    callbacks["on_diagnose"](raw, "", "", str(tmp_path))
    automatic = callbacks["on_diagnose_autoload"](raw, str(tmp_path))
    assert automatic[-2:] == ("", "")
    assert all((call["agent"], call["instance_id"]) == ("opencode", "ses_native") for call in calls)
    callbacks["on_diagnose"](raw, "explicit-agent", "explicit-case", str(tmp_path))
    assert (calls[-1]["agent"], calls[-1]["instance_id"]) == ("explicit-agent", "explicit-case")
    assert all(call["expected_sha"] == run.sha256 and call["source_path"] == str(source) for call in calls)
    assert json.dumps(raw, sort_keys=True) == original


def test_bundled_opencode2_family_retains_read_shell_and_child_evidence(seam, tmp_path):
    compat.install_attribution_guard()
    from agent_trajviz.awe.native import project_native

    def tool(name, identity, params, metadata=None):
        return {"type": "tool", "id": identity, "name": name,
                "time": {"created": 1790000000100, "ran": 1790000000200, "completed": 1790000000300},
                "state": {"status": "completed", "input": params, "metadata": metadata or {},
                          "content": [{"type": "text", "text": "1 passed"}]}}

    def session(sid, content):
        return {"source_format": "opencode_v2", "schema_version": 1, "directory": "/controlled",
                "session": {"id": sid, "location": {"directory": "/controlled"},
                            "time": {"created": 1790000000000, "updated": 1790000001000}},
                "messages": [{"id": "assistant", "type": "assistant", "agent": "build", "content": content,
                              "time": {"created": 1790000000100, "completed": 1790000000400}}], "children": []}

    raw = session("ses_root", [tool("read", "read", {"path": "src/probe.py"}),
                              tool("subagent", "spawn", {"prompt": "Check tests"}, {"sessionID": "ses_child"}),
                              tool("subagent", "spawn-second", {"prompt": "Check again"}, {"sessionID": "ses_second"})])
    raw["children"] = [session(sid, [tool("shell", "test", {"command": "pytest -q tests/test_probe.py"})])
                       for sid in ("ses_child", "ses_second")]
    source = tmp_path / "data/trajectory/opencode/case.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps(raw))
    _benchmark(tmp_path, "opencode")
    run = compat.load_run(str(source))
    before = source.read_bytes(), json.dumps(run.steps, sort_keys=True)
    projected = project_native(run, "opencode", "case")
    calls = {(step.native_tool, step.canonical_action) for step in projected.steps if step.type == "tool_call"}
    assert {("read", "read"), ("Bash", "run_test")} <= calls
    assert {step.subagent_id for step in projected.steps if step.subagent_id} == {"ses_child", "ses_second"}
    assert all(step["agent"] == "build" for step in run.steps)
    result = passthrough.attribution(run, reference_root=str(tmp_path), agent="opencode", instance_id="case")
    assert result["available"], result["reason"]
    assert result["dossier"]["source_certification"]["trajectory_sha256"] == run.sha256
    assert result["used_judge"] is False
    threads = {row.get("source_ref", {}).get("thread") for row in result["dossier"]["thinking_process"]}
    assert {"ses_child", "ses_second"} <= threads
    assert (source.read_bytes(), json.dumps(run.steps, sort_keys=True)) == before
