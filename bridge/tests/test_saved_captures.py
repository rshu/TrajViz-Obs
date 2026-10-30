"""Saved captures retain session identity, missing evidence and portable exports."""

import json
import zipfile
from pathlib import Path

import agent_trajviz as tv
import jsonschema
import pytest

from trajviz_obs.bundle_cmd import bundle_session
from trajviz_obs.compat import load_run
from trajviz_obs.errors import BridgeError
from trajviz_obs.views_cmd import export


def test_capture_listing_requires_explicit_selection_and_bounds_results(captured_sessions, run_cli):
    code, result = run_cli(["capture-sessions", "--transcript", str(captured_sessions), "--limit", "1"])
    assert code == 0 and len(result["sessions"]) == 1 and result["cut"] == 1
    assert "--session-id" in result["text"]
    for flags in ([], ["--session-id", "missing"]):
        code, result = run_cli(["snapshot", "--transcript", str(captured_sessions), *flags])
        assert code == 1 and "session" in result["error"]["message"].lower()


def test_capture_listing_bounds_the_combined_json_and_text(captured_sessions, run_cli, monkeypatch):
    monkeypatch.setattr(tv, "list_sessions", lambda _source: [{"id": "x" * 4090 + str(index)} for index in range(500)])
    code, result = run_cli(["capture-sessions", "--transcript", str(captured_sessions), "--limit", "500"])
    assert code == 0 and result["cut"] > 0 and len(json.dumps(result).encode()) < 256 * 1024


def test_selected_snapshot_distinguishes_observed_zero_from_unavailable(captured_sessions, run_cli):
    snapshots = {}
    for sid in ("A", "B"):
        code, snapshots[sid] = run_cli(["snapshot", "--transcript", str(captured_sessions), "--session-id", sid])
        assert code == 0, snapshots[sid]
        assert snapshots[sid]["transcript"]["capture_session_id"] == sid
    a, b = snapshots["A"], snapshots["B"]
    assert a["tokens"]["total"] == 9 and a["tokens"]["cache_read"] == 0
    assert b["tokens"]["total"] is None and b["tokens"]["input"] == 7
    assert b["tools"]["failed"] == 0 and b["tools"]["unknown"] == 1
    assert b["files"]["reads"] is None and b["plan"]["items"] is None
    assert b["data_quality"] and b["availability"]["file_interactions"]["status"] == "unavailable"
    assert a["transcript"]["sha256"] != b["transcript"]["sha256"]
    assert a["counts"]["prompts"] == b["counts"]["prompts"] == 1


def test_operation_capture_alignment_returns_an_explanation(captured_sessions, fixture_session, run_cli, tmp_path):
    code, result = run_cli(["compare", "--transcript", str(captured_sessions), "--session-id", "A",
                            "--other", str(fixture_session), "--artifacts", str(tmp_path / "artifacts")])
    assert code == 1 and result["error"]["code"] == "compare_failed"
    assert "alignment is unavailable" in result["error"]["message"]
    assert not (tmp_path / "artifacts").exists()


@pytest.mark.parametrize("sid", ["A", "B"])
def test_selected_export_has_all_schema2_views_and_reloadable_trajectory(captured_sessions, tmp_path, sid):
    run = load_run(str(captured_sessions), session_id=sid)
    result = export(run, tmp_path / sid, only=None, with_trajectory=True)
    index = json.loads((Path(result["path"]) / "index.json").read_text())
    assert index["schema_version"] == 2
    for view_id in tv.VIEW_CATALOG:
        schema = tv.view_schemas()[view_id]
        document = json.loads((Path(result["path"]) / f"{view_id}.json").read_text())
        jsonschema.validate(document, schema)
    restored = load_run(str(Path(result["path"]) / "trajectory.json"))
    assert restored.sha256 == run.sha256 and restored.steps == run.steps
    assert restored.raw["_otel"]["session_id"] == sid


def test_bundle_preserves_all_captured_sessions_and_exact_zip(captured_sessions, tmp_path):
    original = captured_sessions.read_bytes()
    result = bundle_session(str(captured_sessions), tmp_path / "capture.zip", session_id="A")
    assert "complete saved capture" in result["note"]
    assert {row["id"] for row in tv.list_sessions(result["path"])} == {"A", "B"}
    for sid in ("A", "B"):
        expected = load_run(str(captured_sessions), session_id=sid)
        actual = load_run(result["path"], session_id=sid)
        assert actual.metrics["tokens"] == expected.metrics["tokens"]
    copied = bundle_session(result["path"], tmp_path / "copy.zip", session_id="B")
    assert Path(copied["path"]).read_bytes() == Path(result["path"]).read_bytes()
    assert captured_sessions.read_bytes() == original


def test_manifest_bundle_includes_only_consumed_members_and_protects_them(captured_sessions, tmp_path):
    folder = tmp_path / "bundle"
    folder.mkdir()
    traces = folder / "traces.json"
    traces.write_bytes(captured_sessions.read_bytes())
    manifest = folder / "manifest.json"
    manifest.write_text(json.dumps({"type": "agent-trajviz-otlp-bundle", "schema_version": 1, "profile": "claude_code",
                                    "traces": ["traces.json"], "logs": []}))
    (folder / "unrelated.txt").write_text("not session input")
    result = bundle_session(str(folder), tmp_path / "directory.zip", session_id="B")
    assert set(result["members"]) == {"manifest.json", "traces.json"}
    assert len(tv.list_sessions(result["path"])) == 2
    before = traces.read_bytes()
    with pytest.raises(BridgeError) as caught:
        bundle_session(str(manifest), traces, session_id="A")
    assert caught.value.code == "bad_output" and traces.read_bytes() == before


@pytest.mark.parametrize("name", ["manifest.txt", "bom-manifest.json"])
def test_bundle_canonicalizes_arbitrary_manifest_names_without_member_collisions(captured_sessions, tmp_path, name):
    folder = tmp_path / "arbitrary"
    folder.mkdir()
    # This valid trace filename would otherwise collide with the generated manifest.
    trace = folder / "manifest.json"
    trace.write_bytes(captured_sessions.read_bytes())
    manifest = folder / name
    document = {"type": "agent-trajviz-otlp-bundle", "schema_version": 1, "profile": "claude_code",
                "traces": [trace.name], "logs": []}
    manifest.write_text("\ufeff" + json.dumps(document))
    before = manifest.read_bytes(), trace.read_bytes()
    result = bundle_session(str(manifest), tmp_path / f"{name}.zip", session_id="B")
    assert set(result["members"]) == {"manifest.json", "data/000.jsonl"}
    assert {row["id"] for row in tv.list_sessions(result["path"])} == {"A", "B"}
    assert load_run(result["path"], session_id="B").metrics["tokens"]["total"] is None
    assert (manifest.read_bytes(), trace.read_bytes()) == before


def test_selected_models_do_not_accept_other_session_replies(captured_sessions, tmp_path, run_cli):
    common = ["--transcript", str(captured_sessions), "--artifacts", str(tmp_path / "artifacts"),
              "--kind", "current", "--feature", "analyze"]
    code, plan = run_cli(["llm-prompt", *common, "--session-id", "A"])
    assert code == 0, plan
    replies = tmp_path / "replies.json"
    replies.write_text(json.dumps({"hash": plan["hash"], "replies": [{"index": 0, "text": "A answer"}]}))
    code, result = run_cli(["llm-apply", *common, "--session-id", "B", "--replies", str(replies)])
    assert code == 1 and result["error"]["code"] == "stale_prompts"
    assert not (tmp_path / "artifacts" / "analyze.json").exists()


def test_incomplete_capture_model_prompt_preserves_missing_evidence(captured_sessions, tmp_path, run_cli):
    code, plan = run_cli(["llm-prompt", "--transcript", str(captured_sessions), "--session-id", "B",
                          "--artifacts", str(tmp_path / "artifacts"), "--feature", "analyze"])
    assert code == 0 and plan["plan"]["calls"] == 1
    prompt = plan["prompts"][0]["system"] + plan["prompts"][0]["prompt"]
    assert "tokens_total: None" in prompt and "telemetry_evidence" in prompt
    assert any("assistant" in line and "?tok" in line for line in prompt.splitlines())
    assert "file activity was not captured" in prompt


def test_frozen_model_replay_protects_original_manifest_companions(captured_sessions, tmp_path, run_cli):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"type": "agent-trajviz-otlp-bundle", "schema_version": 1,
                                    "profile": "claude_code", "traces": [captured_sessions.name], "logs": []}))
    common = ["--transcript", str(manifest), "--session-id", "A", "--artifacts", str(tmp_path / "artifacts"),
              "--kind", "current", "--feature", "label", "--max-steps", "1"]
    code, plan = run_cli(["llm-prompt", *common])
    assert code == 0, plan
    replies = tmp_path / "replies.json"
    replies.write_text(json.dumps({"hash": plan["hash"], "replies": [
        {"index": p["index"], "text": '{"phase":"understand","action":"code_reading"}'}
        for p in plan["prompts"]]}))
    before = captured_sessions.read_bytes()
    code, result = run_cli(["llm-apply", *common, "--out", str(captured_sessions), "--replies", str(replies)])
    assert code == 1 and result["error"]["code"] == "bad_output"
    assert captured_sessions.read_bytes() == before


def test_partial_capture_labels_keep_source_coverage_in_sidecar_and_views(captured_sessions, tmp_path, run_cli):
    common = ["--transcript", str(captured_sessions), "--session-id", "B",
              "--artifacts", str(tmp_path / "artifacts"), "--feature", "label", "--max-steps", "1"]
    code, plan = run_cli(["llm-prompt", *common])
    assert code == 0 and plan["plan"]["calls"] == 1
    replies = tmp_path / "replies.json"
    replies.write_text(json.dumps({"hash": plan["hash"], "replies": [
        {"index": 0, "text": '{"phase":"understand","action":"code_reading"}'}]}))
    code, result = run_cli(["llm-apply", *common, "--replies", str(replies)])
    assert code == 0 and "evidence is unavailable" in result["text"]
    document = json.loads(Path(result["path"]).read_text())
    assistant = next(step for step in document["steps"] if step["role"] == "assistant")
    assert assistant["tokens_observed"] is False and assistant["duration_observed"] is False
    code, labels = run_cli(["labels", "--sidecar", result["path"], "--artifacts", str(tmp_path / "label-views")])
    assert code == 0, labels
    views = json.loads(Path(labels["path"]).read_text())["views"]
    assert views["label_timeline"]["availability"]["status"] == "unavailable"
    assert views["label_phase_durations"]["availability"]["fields"]["duration"]["observed"] == 0
    for vid, view in views.items():
        jsonschema.validate(view, tv.view_schemas()[vid])


def test_zip_snapshot_counts_root_human_prompts(tmp_session, tmp_path, run_cli):
    result = bundle_session(tmp_session.transcript, tmp_path / "native.zip")
    code, snapshot = run_cli(["snapshot", "--transcript", result["path"]])
    assert code == 0 and snapshot["counts"]["prompts"] == 1
    assert snapshot["task"] == "fix the tests"


def test_non_native_jsonl_bundle_reloads_active_pi_branch(tmp_path):
    rows = [{"type": "session", "version": 3, "id": "pi-session", "cwd": str(tmp_path)},
            {"type": "message", "id": "user", "parentId": None,
             "message": {"role": "user", "content": "Controlled prompt"}},
            {"type": "message", "id": "old", "parentId": "user", "message": {"role": "assistant",
             "content": [{"type": "text", "text": "Abandoned"}], "usage": {"input": 1000, "output": 0}}},
            {"type": "message", "id": "active", "parentId": "user", "message": {"role": "assistant",
             "content": [{"type": "text", "text": "Active"}], "usage": {"input": 7, "output": 2}}}]
    path = tmp_path / "session.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    expected = load_run(str(path))
    result = bundle_session(str(path), tmp_path / "pi.zip")
    assert result["members"] == ["trajectory.json"]
    actual = load_run(result["path"])
    assert actual.steps == expected.steps and actual.metrics["tokens"]["total"] == 9
    with zipfile.ZipFile(result["path"]) as archive:
        assert len(json.loads(archive.read("trajectory.json"))["raw"]["_pi_entries"]) == 4
