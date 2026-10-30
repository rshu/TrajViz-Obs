"""``converge-cli``, ``collect``, ``attribution`` and ``brief``: TrajViz's CLIs and plain results passed through."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest
from agent_trajviz.collect import __main__ as collect_main

from trajviz_obs.passthrough import OUTPUT_CAP, _BoundedCapture, attribution, brief, run_collect, run_converge_cli


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


@pytest.mark.parametrize("mode", ["pairwise", "batch", "before_after"])
def test_fresh_converge_cli_preserves_native_v2_family(mode, tmp_path):
    """A fresh CLI must normalize typed records before the upstream fallback sees them."""
    base = 1_790_000_000_000

    def assistant(mid, tokens, content, at=100):
        return {"id": mid, "type": "assistant", "agent": "build",
                "tokens": {"reasoning": 0, "cache": {"read": 0, "write": 0}, **tokens},
                "model": {"providerID": "example", "id": "test-model"},
                "time": {"created": base + at, "streamed": base + at + 50, "completed": base + at + 300},
                "content": content}

    def session(sid, messages, children=()):
        return {"source_format": "opencode_v2", "schema_version": 1, "host_version": "2.2.2",
                "directory": "/controlled/project", "session": {"id": sid, "agent": "build",
                "time": {"created": base, "updated": base + 1000}},
                "messages": messages, "children": list(children)}

    def shell(command, *, failed=False):
        return {"type": "tool", "id": "shell", "name": "shell",
                "time": {"created": base + 100, "ran": base + 120, "completed": base + 150},
                "state": {"status": "completed", "input": {"command": command},
                          "metadata": {"exit": 1 if failed else 0}}}

    user = {"id": "u", "type": "user", "text": "Check the test", "time": {"created": base}}
    reference = session("ses_ref", [user, assistant("a", {
        "input": 7, "output": 3, "reasoning": 2, "cache": {"read": 5, "write": 1}}, [shell("python3 test.py")])])
    child = session("ses_child", [user, assistant("a", {"input": 5, "output": 1}, [shell("false", failed=True)])])
    spawn = {"type": "tool", "id": "spawn", "name": "subagent",
             "time": {"created": base + 100, "ran": base + 120, "completed": base + 350},
             "state": {"status": "completed", "input": {"prompt": "Check this"},
                       "metadata": {"sessionID": "ses_child"}}}
    compared = session("ses_main", [user, assistant("a", {"input": 15, "output": 5}, [spawn]),
        {"id": "compact", "type": "compaction", "status": "completed", "reason": "manual",
         "summary": "Continue", "tokens": {"input": 1, "output": 2, "reasoning": 0,
                                           "cache": {"read": 0, "write": 0}},
         "time": {"created": base + 400}},
        assistant("final", {"input": 8, "output": 2}, [{"type": "text", "text": "Done"}], at=500)], [child])
    for name, value in [("reference.json", reference), ("compared.json", compared)]:
        (tmp_path / name).write_text(json.dumps(value))
    for name, compared_name in [("before.json", "compared.json"), ("after.json", "reference.json")]:
        (tmp_path / name).write_text(json.dumps([
            {"task_id": "native-test", "reference": "reference.json", "compared": compared_name}]))
    args = {"pairwise": [str(tmp_path / "reference.json"), str(tmp_path / "compared.json")],
            "batch": ["--batch", str(tmp_path / "before.json")],
            "before_after": ["--before", str(tmp_path / "before.json"), "--after", str(tmp_path / "after.json")]}[mode]
    launcher = Path(__file__).resolve().parents[1] / "trajviz_obs_cli.py"
    source_bytes = {p: p.read_bytes() for p in tmp_path.glob("*.json")}

    def run(output):
        result = subprocess.run([sys.executable, "-I", "-B", str(launcher), "converge-cli",
                                 "--artifacts", str(tmp_path / "artifacts"), "--", *args, "--output", output],
                                capture_output=True, text=True, timeout=30, check=False)
        assert result.returncode == 0, result.stderr
        envelope = json.loads(result.stdout)
        assert envelope["exit"] == 0, envelope
        return envelope

    report = json.loads(Path(run("json")["path"]).read_text())
    if mode == "pairwise":
        outcome = report["outcome"]
        assert (outcome["reference_format"], outcome["compared_format"]) == ("OpenCode", "OpenCode")
        assert (outcome["reference_steps"], outcome["compared_steps"]) == (2, 6)
        assert (outcome["reference_tokens"], outcome["compared_tokens"]) == (18, 39)
        assert (outcome["reference_tool_calls"], outcome["compared_tool_calls"]) == (1, 2)
    elif mode == "batch":
        assert report["summary"]["compared_count"] == 1 and report["summary"]["error_count"] == 0
        outcome = report["per_task"][0]["outcome"]
        assert (outcome["reference_format"], outcome["compared_format"]) == ("OpenCode", "OpenCode")
    else:
        assert report["paired_tasks"] == 1
        f1 = report["intervention_effect"]["alignment_f1"]
        assert f1["before_mean"] < f1["after_mean"] == 1.0
    summary = run("summary")
    assert summary["path"] is None and "Converge" in summary["text"] and "unknown" not in summary["text"]
    assert source_bytes == {p: p.read_bytes() for p in source_bytes}


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


def test_collect_bounds_storage_during_multibyte_stdout_and_stderr_writes(monkeypatch):
    import io
    import sys

    def noisy(argv):
        for _ in range(32):
            sys.stdout.write("界" * OUTPUT_CAP)
            sys.stderr.write("🙂" * OUTPUT_CAP)
        # Inspect retained storage before the command returns, so a post-hoc output cap cannot pass.
        assert isinstance(sys.stdout, _BoundedCapture) and isinstance(sys.stderr, _BoundedCapture)
        assert len(io.StringIO.getvalue(sys.stdout).encode()) <= OUTPUT_CAP
        assert len(io.StringIO.getvalue(sys.stderr).encode()) <= OUTPUT_CAP
        return 0

    monkeypatch.setattr(collect_main, "main", noisy)
    result = run_collect(["cursor"])
    assert result["exit"] == 0
    assert all(len(result[key].encode()) <= OUTPUT_CAP and result[key].endswith("… (truncated)")
               for key in ("stdout", "stderr"))


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
