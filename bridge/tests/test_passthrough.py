"""``converge-cli``, ``collect``, ``attribution`` and ``brief``: TrajViz's CLIs and plain results passed through."""

from __future__ import annotations

import json
import logging

from agent_trajviz.collect import __main__ as collect_main

from trajviz_obs.passthrough import OUTPUT_CAP, attribution, brief, run_collect, run_converge_cli


def test_converge_cli_summary(fixture_session, synthetic_session, tmp_artifacts):
    out = run_converge_cli([str(fixture_session), synthetic_session, "--output", "summary"], tmp_artifacts)
    assert out["exit"] == 0 and "reference" in out["text"].lower()
    assert out["path"] is None and not (tmp_artifacts / "converge").exists()


def test_converge_cli_json_goes_to_numbered_files(fixture_session, synthetic_session, tmp_artifacts):
    first = run_converge_cli([str(fixture_session), synthetic_session], tmp_artifacts)
    second = run_converge_cli([str(fixture_session), synthetic_session, "--task-id", "t2"], tmp_artifacts)
    assert (first["exit"], second["exit"]) == (0, 0)
    assert first["path"] == str(tmp_artifacts / "converge" / "1.json")
    assert second["path"] == str(tmp_artifacts / "converge" / "2.json")
    assert first["text"] is None
    report = json.loads((tmp_artifacts / "converge" / "2.json").read_text())
    assert report["task_id"] == "t2" and "alignment" in report


def test_converge_cli_usage_error_is_exit_2(tmp_artifacts):
    out = run_converge_cli([], tmp_artifacts)
    assert out["exit"] == 2 and out["path"] is None and "provide ref_file" in out["text"].lower()


def test_converge_cli_input_error_is_exit_1(tmp_artifacts):
    out = run_converge_cli([str(tmp_artifacts / "missing.jsonl"), str(tmp_artifacts / "gone.jsonl")], tmp_artifacts)
    assert out["exit"] == 1 and out["path"] is None and out["text"].startswith("Error:")


def test_converge_cli_leaves_no_log_handler(tmp_artifacts):
    logger = logging.getLogger("agent_trajviz")
    before = list(logger.handlers)
    run_converge_cli([], tmp_artifacts)
    assert logger.handlers == before


def test_collect_version():
    out = run_collect(["--version"])
    assert out["exit"] == 0 and out["stdout"].startswith("agent-trajviz-collect ") and out["stderr"] == ""


def test_collect_unknown_tool():
    out = run_collect(["nope"])
    assert out["exit"] == 2 and "unknown tool 'nope'" in out["stderr"]


def test_collect_output_is_capped(monkeypatch):
    def noisy(argv):
        print("x" * (OUTPUT_CAP * 2))
        return 0

    monkeypatch.setattr(collect_main, "main", noisy)
    out = run_collect(["cursor"])
    assert out["exit"] == 0 and len(out["stdout"].encode()) <= OUTPUT_CAP


def test_attribution_unavailable_is_honest(fixture_run):
    out = attribution(fixture_run, reference_root=None, agent=None, instance_id=None)
    assert out["available"] is False and out["reason"]


def test_brief_is_trajviz_text(fixture_run):
    out = brief(fixture_run)
    assert list(out) == ["brief"] and isinstance(out["brief"], str) and out["brief"].strip()


def test_cli_converge_cli_and_collect(run_cli, fixture_session, synthetic_session, tmp_artifacts):
    code, out = run_cli(["converge-cli", "--artifacts", str(tmp_artifacts), "--",
                         str(fixture_session), synthetic_session])
    assert code == 0 and out["exit"] == 0 and out["path"] == str(tmp_artifacts / "converge" / "1.json")
    code, out = run_cli(["collect", "--", "--version"])
    assert code == 0 and out["exit"] == 0 and "agent-trajviz-collect" in out["stdout"]


def test_cli_converge_cli_needs_artifacts(run_cli, fixture_session):
    code, out = run_cli(["converge-cli", "--", str(fixture_session), str(fixture_session)])
    assert code == 2 and out["error"]["code"] == "usage"


def test_cli_attribution_and_brief(run_cli, fixture_session):
    code, out = run_cli(["attribution", "--transcript", str(fixture_session), "--agent", "a", "--instance", "i"])
    assert code == 0 and out["available"] is False
    code, out = run_cli(["brief", "--transcript", str(fixture_session), "--window", "200000"])
    assert code == 0 and out["brief"]


def test_cli_brief_of_empty_current_session(run_cli, tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text('{"type":"ai-title","aiTitle":"x"}\n')
    code, out = run_cli(["brief", "--transcript", str(path), "--kind", "current"])
    assert code == 0 and out["empty"] is True and out["reason"]
