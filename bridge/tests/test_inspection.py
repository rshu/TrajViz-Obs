"""Default attribution inspects recorded evidence without asserting correctness."""
from __future__ import annotations

import copy
import json
import socket
import subprocess

import pytest
from agent_trajviz.awe import inspect_trajectory, native
from agent_trajviz.insight import attribution as reference_attribution

from trajviz_obs.compat import load_run
from trajviz_obs.inspection import TEXT_LIMIT, inspection_text
from trajviz_obs.passthrough import attribution

BASE = 1_790_000_000_000


@pytest.fixture
def inspection_run(tmp_path):
    def tool(name, identity, inputs, output=None, *, status="completed", metadata=None):
        state = {"status": status, "input": inputs, "metadata": metadata or {}}
        if output is not None:
            state["content"] = [{"type": "text", "text": output}]
        return {"type": "tool", "id": identity, "name": name, "state": state,
                "time": {"created": BASE + 100, "ran": BASE + 110,
                         **({"completed": BASE + 150} if status == "completed" else {})}}

    def message(identity, content, offset):
        return {"id": identity, "type": "assistant", "agent": "build", "content": content,
                "time": {"created": BASE + offset, "completed": BASE + offset + 200}}

    command = "pytest -q tests/test_probe.py"
    root = {"source_format": "opencode_v2", "schema_version": 1, "directory": str(tmp_path),
            "session": {"id": "root", "location": {"directory": str(tmp_path)}}, "children": [], "messages": [
                message("baseline", [tool("read", "read", {"path": "src/probe.py"}, "pass"),
                    tool("shell", "failed", {"command": command}, "1 failed in 0.01s", metadata={"exit": 1})], 100),
                message("repair", [tool("write", "write", {"path": "src/probe.py"}, "Written"),
                    tool("shell", "passed", {"command": command}, "9 passed in 0.02s", metadata={"exit": 0})], 400),
                message("pending", [tool("shell", "pending", {"command": "pytest -q tests/extra.py"}, status="running")], 700),
            ]}
    source = tmp_path / "capture.json"
    source.write_text(json.dumps(root))
    return source, load_run(str(source))


def test_default_inspection_keeps_the_public_report_and_explicit_evidence(inspection_run):
    source, run = inspection_run
    before = source.read_bytes(), copy.deepcopy(run.to_dict(include_raw=True))
    expected = inspect_trajectory(run)
    report = attribution(run, reference_root=None, agent=None, instance_id=None)
    assert {key: value for key, value in report.items() if key != "text"} == expected
    assert report["mode"] == "trajectory_only" and report["available"]
    assert report["summary"]["failed_calls"] == 1 and report["summary"]["missing_results"] == 1
    assert report["summary"]["later_successes"] == 1
    assert report["coverage"]["tool_outcomes"] == {"observed": 4, "total": 5}
    assert report["coverage"]["capture_complete"] is None
    assert report["outcome"]["status"] == "unknown"
    assert any(row["workflow_index"] == 0 for row in report["failures"])
    assert any(row["counts"] and row["counts"]["passed"] == 9 for row in report["tests"])
    text = report["text"]
    for label in ("Findings:", "Recorded tool failures:", "Files:", "Tests:", "Recovery observations:", "Recording coverage:"):
        assert label in text
    assert "step #0" in text and "failed calls 1" in text and "missing results 1" in text
    assert "9 passed" in text and "later success observed" in text
    assert "counts unknown" in text and "Capture completeness: unknown" in text
    assert "Task outcome: unknown" in text and "not correctness attribution" in text
    assert "scorecard" not in report and "primary" not in report
    json.dumps(report, allow_nan=False)
    assert (source.read_bytes(), run.to_dict(include_raw=True)) == before


def test_inspection_uses_only_the_loaded_snapshot_without_reference_or_execution(inspection_run, monkeypatch):
    source, run = inspection_run
    source.unlink()
    before = copy.deepcopy(run.to_dict(include_raw=True))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Inspection must not execute, connect, reread sources, or diagnose references")

    monkeypatch.setattr(reference_attribution, "diagnose", forbidden)
    monkeypatch.setattr(native, "_fingerprints", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setenv("TRAJVIZ_REFERENCE_ROOT", "/unreadable/reference")
    report = attribution(run, reference_root=None, agent=None, instance_id=None)
    assert report["available"] and report["source"]["identity_basis"] == "loaded_snapshot"
    assert run.to_dict(include_raw=True) == before and not source.exists()


def test_inspection_text_is_bounded_and_keeps_coverage_limits_and_unknown_outcome(inspection_run):
    _, run = inspection_run
    report = inspect_trajectory(run)
    for key in ("findings", "files", "tests", "failures", "recovery"):
        first = report[key][0]
        report[key] = [{**first, **{field: "long source " * 1000 for field in
                       ("title", "observation", "command", "path", "output_excerpt")}} for _ in range(100)]
    before = copy.deepcopy(report)
    text = inspection_text(report)
    assert len(text) <= TEXT_LIMIT and "Text preview clipped" in text
    assert "95 more observations" in text
    assert "Recording coverage:" in text and "Task outcome: unknown" in text
    assert "Capture completeness: unknown" in text and "not correctness attribution" in text
    assert "/obs dashboard" in text and report == before
