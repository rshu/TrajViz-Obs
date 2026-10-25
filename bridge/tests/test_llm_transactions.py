"""Current-session model requests use an immutable input while the host appends UI/model events."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPLIES = {"analyze": "The failed Bash call was recovered.",
           "fixes": '{"where":"CLAUDE.md","fix":"check x","also":"","confidence":"high"}',
           "label": '{"phase":"understand","action":"code_reading"}'}


def _reply_file(plan: dict, feature: str, folder: Path) -> str:
    path = folder / "replies.json"
    path.write_text(json.dumps({"hash": plan["hash"], "replies": [
        {"index": prompt["index"], "text": REPLIES[feature]} for prompt in plan["prompts"]]}))
    return str(path)


@pytest.mark.parametrize("feature", ["analyze", "fixes", "label"])
@pytest.mark.parametrize("change", ["append", "remove"])
def test_current_apply_uses_captured_input_and_original_provenance(feature, change, fixture_session, tmp_path, run_cli):
    source = tmp_path / "current.jsonl"
    source.write_bytes(fixture_session.read_bytes())
    artifacts = tmp_path / "artifacts"
    base = ["--transcript", str(source), "--artifacts", str(artifacts), "--kind", "current", "--feature", feature,
            "--window", "1000000", "--max-steps", "2"]
    code, plan = run_cli(["llm-prompt", *base])
    assert code == 0 and plan["source"]["frozen"] is True
    frozen = artifacts / "plans" / plan["hash"] / "trajectory.json"
    assert frozen.is_file()
    if change == "append":
        with source.open("a") as handle:
            handle.write(json.dumps({"type": "system", "subtype": "ui_log", "text": "analysis cost preview"}) + "\n")
    else:
        source.unlink()
    code, result = run_cli(["llm-apply", *base, "--replies", _reply_file(plan, feature, tmp_path)])
    assert code == 0, result
    assert result["source"] == plan["source"]
    assert result["source"]["path"] == str(source)
    if feature == "analyze":
        assert result["reply"] == REPLIES[feature]
    elif feature == "fixes":
        assert result["issues"][0]["judgment"]["fix"] == "check x" and result["errors"] == []
    else:
        sidecar = json.loads(Path(result["path"]).read_text())
        assert sidecar["trajectory_file"] == str(source)
        assert result["summary"]["classified"] == 2 and result["errors"] == []


def test_current_label_captures_subagents_without_reopening_them(tmp_session, tmp_path, run_cli):
    source = Path(tmp_session.transcript)
    base = ["--transcript", str(source), "--artifacts", str(tmp_path / "artifacts"), "--kind", "current",
            "--feature", "label"]
    code, plan = run_cli(["llm-prompt", *base])
    assert code == 0
    child = next(source.with_suffix("").rglob("*.jsonl"))
    child.write_text("{corrupt\n")
    code, result = run_cli(["llm-apply", *base, "--replies", _reply_file(plan, "label", tmp_path)])
    assert code == 0 and result["summary"]["classified"] == plan["plan"]["calls"]
    sidecar = json.loads(Path(result["path"]).read_text())
    assert any(step["agent"] == "abc" for step in sidecar["steps"])


def test_current_apply_refuses_changed_analysis_history(fixture_session, tmp_path, run_cli):
    artifacts = tmp_path / "artifacts"
    base = ["--transcript", str(fixture_session), "--artifacts", str(artifacts), "--kind", "current",
            "--feature", "analyze"]
    code, plan = run_cli(["llm-prompt", *base])
    assert code == 0
    (artifacts / "analyze.json").write_text(json.dumps({"history": [{"role": "user", "content": "Other question"}]}))
    code, result = run_cli(["llm-apply", *base, "--replies", _reply_file(plan, "analyze", tmp_path)])
    assert code == 1 and result["error"]["code"] == "stale_prompts"


def test_current_apply_refuses_a_plan_for_another_transcript(fixture_session, tmp_path, run_cli):
    artifacts = tmp_path / "artifacts"
    base = ["--artifacts", str(artifacts), "--kind", "current", "--feature", "analyze"]
    code, plan = run_cli(["llm-prompt", "--transcript", str(fixture_session), *base])
    assert code == 0
    other = tmp_path / "other.jsonl"
    other.write_bytes(fixture_session.read_bytes())
    code, result = run_cli(["llm-apply", "--transcript", str(other), *base,
                            "--replies", _reply_file(plan, "analyze", tmp_path)])
    assert code == 1 and result["error"]["code"] == "stale_prompts"


def test_reply_hash_cannot_select_an_arbitrary_saved_path(fixture_session, tmp_path, run_cli):
    replies = tmp_path / "replies.json"
    replies.write_text(json.dumps({"hash": "../../other", "replies": []}))
    code, result = run_cli(["llm-apply", "--transcript", str(fixture_session), "--artifacts", str(tmp_path),
                            "--kind", "current", "--feature", "analyze", "--replies", str(replies)])
    assert code == 1 and result["error"]["code"] == "stale_prompts"
