"""The snapshot document: TrajViz's numbers for the UI, bounded, with the bridge's own enrichments beside them."""

from __future__ import annotations

import json
from pathlib import Path

import agent_trajviz as tv
import pytest
from conftest import make_session

from trajviz_obs.compat import load_run
from trajviz_obs.sidecars import agent_meta, scan_transcript
from trajviz_obs.snapshot import build_snapshot, downsample, empty_snapshot

CONTRACT_KEYS = {
    "schema_version", "generated_at", "duration_ms", "transcript", "trajviz", "title", "task", "counts", "tokens",
    "timing", "tools", "health", "issues", "bottlenecks", "context", "series", "phases", "agents", "swimlane",
    "patterns", "timeline", "diagnostics", "files", "plan", "skills", "artifacts", "text",
}


def _snapshot(path: str, **kwargs) -> dict:
    return build_snapshot(load_run(path, **kwargs), transcript_path=path, facts=scan_transcript(path),
                          agents_meta=agent_meta(path), artifacts=None)


def test_snapshot_keys_and_bounds(big_session):
    snap = build_snapshot(load_run(big_session, window_limit=200_000), transcript_path=big_session,
                          facts=scan_transcript(big_session), agents_meta={}, artifacts=None)
    assert set(snap) == CONTRACT_KEYS
    assert snap["schema_version"] == 1 and snap["counts"]["assistant_steps"] > 0
    assert len(snap["series"]["tokens"]["steps"]) <= 400 and len(snap["timeline"]) <= 60
    for series in snap["series"].values():
        assert all(len(values) <= 400 for values in series.values())
    assert len(snap["phases"]) <= 400 and len(snap["issues"]) <= 10 and len(snap["bottlenecks"]) <= 5
    assert all(len(lane["segments"]) <= 400 for lane in snap["swimlane"]["lanes"])
    assert snap["timeline"][-1]["step"] == snap["counts"]["records"] - 1
    assert snap["context"]["window"] == 200_000 and snap["context"]["window_source"] == "override"
    assert len(json.dumps(snap)) < 300_000


def test_snapshot_enrichment(tmp_session):  # title, prompts, agent label from meta
    snap = _snapshot(tmp_session.transcript)
    assert snap["title"] == "Build the obs plugin" and snap["counts"]["prompts"] == 1
    # TrajViz counts every user record (caveat, slash command, prompt, the sub-agent's prompt); prompts are the real one.
    assert snap["task"] == "fix the tests" and snap["counts"]["user_records"] == 4
    agent = next(a for a in snap["agents"] if a["id"] == "abc")
    assert agent["label"] == "Builder check" and agent["type"] == "general-purpose"
    assert any(lane["label"] == "Builder check" for lane in snap["swimlane"]["lanes"])
    assert snap["transcript"]["session_id"] == tmp_session.id and snap["text"].startswith("Health: ")


def test_snapshot_issue_evidence(fixture_session):
    snap = _snapshot(str(fixture_session))
    issue = snap["issues"][0]
    assert issue["steps"] == [4]
    assert issue["evidence"] == "Read: /home/user/workdirs/django__django-11477/claude_code/tests/i18n/patterns/urls"
    assert snap["tools"]["by_tool"][0] == {"tool": "Grep", "calls": 6, "errors": 0}
    assert snap["context"]["window_source"] == "model" and snap["context"]["current_pct"] is not None


def test_snapshot_cli_empty_current(run_cli, tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text('{"type":"ai-title","aiTitle":"x"}\n')
    code, out = run_cli(["snapshot", "--transcript", str(path), "--kind", "current"])
    assert code == 0 and out["empty"] is True and out["schema_version"] == 1
    assert out["transcript"]["path"] == str(path) and out["reason"]


def test_snapshot_cli_load_error_verbatim(run_cli, tmp_path):
    path = Path(make_session(tmp_path, steps=6))
    lines = path.read_text().splitlines()
    path.write_text("\n".join([*lines[:3], "{broken", *lines[3:]]) + "\n")
    with pytest.raises(tv.LoadError) as caught:
        tv.load(path)
    code, out = run_cli(["snapshot", "--transcript", str(path), "--kind", "current"])
    assert code == 1
    assert out["error"]["code"] == "load_error" and out["error"]["message"] == str(caught.value)
    assert out["error"]["path"] == str(path)


def test_snapshot_warnings_carry_skipped_children(tmp_session_with_bad_child):
    snap = _snapshot(tmp_session_with_bad_child.transcript)
    warnings = snap["trajviz"]["warnings"]
    assert len(warnings) == 2 and all("could not be read" in w for w in warnings)
    assert any("agent-bad1.jsonl" in w for w in warnings) and any("agent-bad2.jsonl" in w for w in warnings)


def test_snapshot_cli_on_fixture(run_cli, fixture_session, tmp_artifacts):
    (tmp_artifacts / "report.html").write_text("<html></html>")
    code, out = run_cli(["snapshot", "--transcript", str(fixture_session), "--window", "1000000",
                         "--artifacts", str(tmp_artifacts), "--max-points", "10"])
    assert code == 0 and out["counts"]["assistant_steps"] == 23
    assert out["context"]["window"] == 1_000_000 and out["context"]["window_source"] == "override"
    assert out["artifacts"] == {"dir": str(tmp_artifacts), "report": str(tmp_artifacts / "report.html"),
                                "views": None, "labels": None}
    assert len(out["series"]["tokens"]["output"]) <= 10 and out["duration_ms"] >= 0


def test_snapshot_small_max_points_merges_lanes_and_phases(tmp_path):
    path = make_session(tmp_path, steps=300, agents=30)
    run = load_run(path)
    raw_lanes = {lane["id"]: lane["segments"] for lane in run.view("agent_swimlane").data["lanes"]}
    assert len(raw_lanes[""]) > 10 and len(run.view("phases").data) > 10
    snap = build_snapshot(run, transcript_path=path, facts=scan_transcript(path), agents_meta={}, artifacts=None,
                          max_points=10)
    main = next(lane for lane in snap["swimlane"]["lanes"] if lane["id"] == "")
    assert len(main["segments"]) <= 10
    assert (main["segments"][0]["start"], main["segments"][-1]["end"]) == (raw_lanes[""][0]["start"],
                                                                           raw_lanes[""][-1]["end"])
    assert 0 < len(snap["phases"]) <= 10
    assert all(a["end"] < b["start"] for a, b in zip(snap["phases"], snap["phases"][1:], strict=False))
    assert all(len(values) <= 10 for series in snap["series"].values() for values in series.values())


def test_downsample_keeps_bucket_maxima():
    assert downsample([1, 5, 2, None, None, None, 3], 3) == [5, None, 3]
    assert downsample([1, 2], 5) == [1, 2]
    assert len(downsample(list(range(1001)), 400)) <= 400


def test_empty_snapshot_shape(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text("{}\n")
    snap = empty_snapshot("no steps", str(path))
    assert snap["schema_version"] == 1 and snap["empty"] is True and snap["reason"] == "no steps"
    assert snap["transcript"]["size"] == 3 and snap["transcript"]["session_id"] == "s"
