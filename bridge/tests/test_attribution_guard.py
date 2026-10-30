"""An optional parser that loses native tool evidence must never attribute missing tool execution."""
from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import agent_trajviz as tv
import pytest
from agent_trajviz.insight import attribution as native_attribution

from trajviz_obs import compat, dashboard_cmd, passthrough


def _parser_fixture(agent, instance_id):
    config, calls, mode = (globals()[key] for key in ("config", "calls", "mode"))
    path = config.TRAJECTORY_DIR / agent / (instance_id + ".jsonl")
    calls.append(path)
    if mode == "raises":
        raise OSError("unreadable parser")
    records = [json.loads(line) for line in path.read_text().splitlines()]
    steps = []
    if mode == "supported":
        for record in records:
            for part in record.get("message", {}).get("content", []):
                if isinstance(part, dict) and part.get("type") == "tool_use":
                    steps.append(SimpleNamespace(type="tool_call", native_tool=part["name"],
                        canonical_action={"Read": "read", "Bash": "run_test"}[part["name"]]))
    return SimpleNamespace(steps=steps)


@pytest.fixture
def seam(monkeypatch, tmp_path):
    calls, native_calls = [], []
    real_root = tmp_path / "real-reference-trajectories"
    real_root.mkdir()
    config = SimpleNamespace(TRAJECTORY_DIR=real_root, unrelated="retain")
    adapters = types.ModuleType("awe.adapters")
    awe = types.ModuleType("awe")
    awe.adapters = adapters
    monkeypatch.setitem(sys.modules, "awe", awe)
    monkeypatch.setitem(sys.modules, "awe.adapters", adapters)
    monkeypatch.setattr(native_attribution, "DECAF_AVAILABLE", True)
    compat._attribution_reads_native_claude.cache_clear()

    def native(**kwargs):
        native_calls.append(kwargs)
        return native_attribution.AttributionResult(True, mode="reference", agent=kwargs["agent"],
                                                    instance_id=kwargs["instance_id"])

    monkeypatch.setattr(native_attribution, "diagnose", native)

    def parser(mode="supported"):
        namespace = {"config": config, "Path": Path, "json": json, "SimpleNamespace": SimpleNamespace,
                     "calls": calls, "mode": mode}
        adapters._parse_normalized = types.FunctionType(_parser_fixture.__code__, namespace, "parse")
        return adapters._parse_normalized

    return SimpleNamespace(calls=calls, native_calls=native_calls, config=config, real_root=real_root,
                           parser=parser, adapters=adapters, native=native)


def request(**overrides):
    return {"agent": "claude_code", "instance_id": "case", "source_path": "/synthetic/case.jsonl",
            "fmt": "claude_code", "expected_sha": "recorded-sha", "merged_sources": 0,
            "reference_root": "/synthetic/reference", **overrides}


def test_parser_that_loses_native_tools_is_unavailable_without_blame_and_probed_once(seam):
    seam.parser("unsupported")
    compat.install_attribution_guard()
    result = native_attribution.diagnose(**request())
    again = native_attribution.diagnose(**request())
    assert not result.available and not again.available and result.mode == "reference"
    assert "This attribution parser does not preserve native Claude Code JSONL" in result.reason
    assert "decaf" not in result.reason.lower()
    assert result.faults == result.scorecard == [] and result.primary is None and not result.used_judge
    assert len(seam.calls) == 1 and seam.native_calls == []
    assert seam.config.TRAJECTORY_DIR == seam.real_root and list(seam.real_root.iterdir()) == []
    assert not seam.calls[0].exists()  # the isolated probe was deleted, with no reference cache created


def test_supported_parser_retains_all_native_identity_and_reference_gates(seam):
    parser = seam.parser()
    compat.install_attribution_guard()
    wrapped = native_attribution.diagnose
    compat.install_attribution_guard()
    assert native_attribution.diagnose is wrapped
    args = request(merged_sources=3)
    result = native_attribution.diagnose(**args)
    assert result.available and seam.native_calls == [args]
    assert len(seam.calls) == 1 and parser.__globals__["config"] is seam.config
    assert seam.config.TRAJECTORY_DIR == seam.real_root and seam.config.unrelated == "retain"


def test_replacing_the_parser_rechecks_capability_instead_of_reusing_an_old_refusal(seam):
    seam.parser("unsupported")
    compat.install_attribution_guard()
    assert not native_attribution.diagnose(**request()).available
    seam.parser()
    assert native_attribution.diagnose(**request()).available
    assert len(seam.calls) == 2 and len(seam.native_calls) == 1


@pytest.mark.parametrize("overrides", [{"fmt": "codex"}, {"fmt": None}, {"source_path": "/synthetic/case.json"},
                                      {"source_path": None}])
def test_other_formats_and_non_native_exports_keep_native_diagnosis(seam, overrides):
    seam.parser("unsupported")
    compat.install_attribution_guard()
    args = request(**overrides)
    assert native_attribution.diagnose(**args).available
    assert seam.calls == [] and seam.native_calls == [args]


def test_missing_parser_uses_public_setup_guidance_without_attempting_a_probe(seam, monkeypatch):
    seam.parser("raises")
    monkeypatch.setattr(native_attribution, "DECAF_AVAILABLE", False)
    expected = native_attribution.AttributionResult(
        False, reason="DECAF unavailable (DECAF import failed (ImportError))",
        notes=["DECAF's dossier API lacks per-call judge/arbiter controls"],
    )
    monkeypatch.setattr(native_attribution, "diagnose", lambda **_kwargs: expected)
    compat.install_attribution_guard()
    result = native_attribution.diagnose(**request())
    assert not result.available and result.reason.startswith("Attribution parser unavailable (ImportError)")
    assert "TRAJVIZ_ATTRIBUTION_PATH" in result.reason and "TRAJVIZ_REFERENCE_ROOT" in result.reason
    assert result.notes == ["Attribution's dossier API lacks per-call judge/arbiter controls"]
    assert expected.reason.startswith("DECAF unavailable")  # the upstream result itself is not mutated
    assert seam.calls == []


@pytest.mark.parametrize("message, expected", [
    (None, None),
    ("DECAF diagnosis unavailable (ValueError)", "Attribution diagnosis unavailable (ValueError)"),
    ("This DeCaF parser failed. DECAF is unavailable.", "This attribution parser failed. Attribution is unavailable."),
    ("Failed while loading DECAF.", "Failed while loading attribution."),
    ("Set AWE_DECAF_PATH to a DECAF checkout", "Set TRAJVIZ_ATTRIBUTION_PATH to an attribution checkout"),
    ("Reference path /tmp/DECAF/data; DECAF.json; DECAF_AVAILABLE",
     "Reference path /tmp/DECAF/data; DECAF.json; DECAF_AVAILABLE"),
    ("Check AWE_DECAF_PATH; /tmp/AWE_DECAF_PATH/parser; AWE_DECAF_PATH.json",
     "Check TRAJVIZ_ATTRIBUTION_PATH; /tmp/AWE_DECAF_PATH/parser; AWE_DECAF_PATH.json"),
])
def test_attribution_messages_normalize_prose_and_public_configuration_only(message, expected):
    assert compat.attribution_message(message) == expected


def test_public_parser_path_reaches_upstream_discovery(monkeypatch):
    monkeypatch.setenv("TRAJVIZ_ATTRIBUTION_PATH", " /synthetic/attribution-parser ")
    monkeypatch.setenv("AWE_DECAF_PATH", "/synthetic/old-parser")
    compat.configure_attribution_environment()
    assert os.environ["AWE_DECAF_PATH"] == "/synthetic/attribution-parser"
    monkeypatch.setenv("TRAJVIZ_ATTRIBUTION_PATH", " ")
    compat.configure_attribution_environment()
    assert os.environ["AWE_DECAF_PATH"] == "/synthetic/attribution-parser"


def test_parser_probe_errors_are_unavailable_rather_than_a_verdict(seam):
    seam.parser("raises")
    compat.install_attribution_guard()
    result = native_attribution.diagnose(**request())
    assert not result.available and result.faults == [] and seam.native_calls == []
    assert seam.config.TRAJECTORY_DIR == seam.real_root


def test_cli_attribution_installs_the_same_guard_before_public_api_dispatch(seam, fixture_run):
    seam.parser("unsupported")
    result = passthrough.attribution(fixture_run, reference_root="/synthetic/reference", agent="claude_code",
                                     instance_id="case")
    assert result["available"] is False and "does not preserve" in result["reason"]
    assert result["faults"] == [] and result["primary"] is None and seam.native_calls == []


def test_native_browser_attribution_callback_uses_the_guard(seam, fixture_session):
    seam.parser("unsupported")
    app = dashboard_cmd.build_preloaded_insight(str(fixture_session))
    callback = next(block.fn for block in app.fns.values() if getattr(block.fn, "__name__", "") == "on_diagnose")
    run = tv.load(fixture_session)
    raw = {**run.raw, "_source_path": str(fixture_session), "_source_sha256": run.sha256}
    html, status = callback(raw, "claude_code", "case", "/synthetic/reference")
    assert "does not preserve" in html and "No gold-grounded attribution" in status
    assert "decaf" not in html.lower()
    assert seam.native_calls == [] and len(seam.calls) == 1


def test_native_browser_missing_parser_diagnostic_uses_public_terminology(seam, fixture_session, monkeypatch):
    monkeypatch.setattr(native_attribution, "DECAF_AVAILABLE", False)
    monkeypatch.setattr(native_attribution, "diagnose", lambda **_kwargs: native_attribution.AttributionResult(
        False, reason="DECAF unavailable (DECAF import failed (ImportError))"))
    app = dashboard_cmd.build_preloaded_insight(str(fixture_session))
    callback = next(block.fn for block in app.fns.values() if getattr(block.fn, "__name__", "") == "on_diagnose")
    html, status = callback({"_source_path": str(fixture_session)}, "claude_code", "case", "")
    assert "Attribution parser unavailable" in html and "TRAJVIZ_ATTRIBUTION_PATH" in html
    assert "decaf" not in (html + status).lower() and seam.calls == []


def _v2_parser_fixture(agent, instance_id):
    config, calls, mode = (globals()[key] for key in ("config", "calls", "mode"))
    path = config.TRAJECTORY_DIR / agent / (instance_id + ".json")
    calls.append(path)
    payload = json.loads(path.read_text())
    steps = []
    if mode == "supported":
        for message in payload["messages"]:
            for part in message.get("content", []):
                if part.get("type") == "tool":
                    steps.append(SimpleNamespace(type="tool_call", native_tool=part["name"],
                        canonical_action={"read": "read", "shell": "run_test"}[part["name"]]))
    return SimpleNamespace(steps=steps)


def _v2_parser(seam, mode="supported"):
    namespace = {"config": seam.config, "json": json, "SimpleNamespace": SimpleNamespace,
                 "calls": seam.calls, "mode": mode}
    seam.adapters._parse_normalized = types.FunctionType(_v2_parser_fixture.__code__, namespace, "parse_v2")


@pytest.fixture
def opencode_source(tmp_path):
    raw = {"source_format": "opencode_v2", "schema_version": 1, "session": {"id": "ses_test"},
           "messages": [{"type": "assistant", "id": "a", "agent": "build", "model": {"id": "test"},
                         "content": [], "time": {"created": 1790000000000}}], "children": []}
    path = tmp_path / "opencode.json"
    path.write_text(json.dumps(raw))
    return path, raw


def test_v2_parser_that_loses_tools_cannot_issue_a_capability_verdict(seam, opencode_source):
    _v2_parser(seam, "unsupported")
    path, _ = opencode_source
    compat.install_attribution_guard()
    result = native_attribution.diagnose(**request(fmt="opencode", source_path=str(path)))
    assert not result.available and "OpenCode 2 read/shell" in result.reason
    assert result.faults == result.scorecard == [] and result.primary is None
    assert seam.native_calls == [] and len(seam.calls) == 1
    assert not seam.calls[0].exists() and list(seam.real_root.iterdir()) == []


def test_supported_v2_parser_keeps_every_original_identity_and_reference_gate(seam, opencode_source):
    _v2_parser(seam)
    path, _ = opencode_source
    compat.install_attribution_guard()
    args = request(agent="opencode", fmt="opencode", source_path=str(path), merged_sources=3)
    assert native_attribution.diagnose(**args).available
    assert native_attribution.diagnose(**args).available
    assert seam.native_calls == [args, args] and len(seam.calls) == 1
    assert seam.config.TRAJECTORY_DIR == seam.real_root


def test_v2_subagent_families_are_not_certified_by_a_single_session_probe(seam, opencode_source):
    _v2_parser(seam)
    path, raw = opencode_source
    raw["children"] = [{"session": {"id": "ses_child"}, "messages": []}]
    path.write_text(json.dumps(raw))
    compat.install_attribution_guard()
    result = native_attribution.diagnose(**request(fmt="opencode", source_path=str(path)))
    assert not result.available and "subagent family" in result.reason
    assert result.faults == [] and seam.native_calls == seam.calls == []


def test_v2_zip_container_requires_its_own_parser_support(seam, opencode_source, tmp_path):
    import zipfile

    _v2_parser(seam)
    path, _ = opencode_source
    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.write(path, "source.json")
    compat.install_attribution_guard()
    result = native_attribution.diagnose(**request(fmt="opencode", source_path=str(archive)))
    assert not result.available and "ZIP bundle" in result.reason
    assert seam.native_calls == seam.calls == []


def test_legacy_opencode_exports_keep_their_existing_attribution_contract(seam, tmp_path):
    _v2_parser(seam, "unsupported")
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps({"info": {"id": "ses_old"}, "messages": [
        {"info": {"role": "assistant"}, "parts": []}]}))
    compat.install_attribution_guard()
    args = request(fmt="opencode", source_path=str(path))
    assert native_attribution.diagnose(**args).available
    assert seam.native_calls == [args] and seam.calls == []
