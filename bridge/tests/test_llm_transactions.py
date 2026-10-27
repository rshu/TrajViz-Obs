"""Current-session model requests use an immutable input while the host appends UI/model events."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from trajviz_obs import llm_cmd
from trajviz_obs.errors import BridgeError

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


def test_current_apply_refuses_cached_steps_changed_outside_the_planned_label_cap(fixture_session, tmp_path, run_cli):
    artifacts = tmp_path / "artifacts"
    base = ["--transcript", str(fixture_session), "--artifacts", str(artifacts), "--kind", "current",
            "--feature", "label", "--max-steps", "1"]
    code, plan = run_cli(["llm-prompt", *base])
    assert code == 0
    frozen = artifacts / "plans" / plan["hash"] / "trajectory.json"
    captured = json.loads(frozen.read_text())
    # The source identity and the single captured model prompt do not change. A checksum must still detect
    # corruption in the unplanned tail rather than accepting it into the generated labels sidecar.
    captured["steps"][-1]["text_preview"] = "unexpected cached mutation"
    frozen.write_text(json.dumps(captured))
    code, result = run_cli(["llm-apply", *base, "--replies", _reply_file(plan, "label", tmp_path)])
    assert code == 1 and result["error"]["code"] == "stale_prompts"
    assert "contents changed" in result["error"]["message"]
    assert not (artifacts / "labels.json").exists()


def test_historical_export_tail_changes_invalidate_capped_label_replies(fixture_run, tmp_path, run_cli):
    source = tmp_path / "trajectory.json"
    captured = fixture_run.to_dict(include_raw=True)
    source.write_text(json.dumps(captured))
    base = ["--transcript", str(source), "--artifacts", str(tmp_path / "artifacts"), "--feature", "label",
            "--max-steps", "1"]
    code, plan = run_cli(["llm-prompt", *base])
    assert code == 0
    captured["steps"][-1]["text_preview"] = "changed historical export tail"
    source.write_text(json.dumps(captured))
    code, result = run_cli(["llm-apply", *base, "--replies", _reply_file(plan, "label", tmp_path)])
    assert code == 1 and result["error"]["code"] == "stale_prompts"
    assert not (tmp_path / "artifacts" / "labels.json").exists()


def test_analysis_history_changed_during_replay_is_preserved_instead_of_accepting_an_old_context_reply(
        fixture_run, fixture_session, tmp_path, monkeypatch):
    history = tmp_path / "analyze.json"
    previous = [{"role": "user", "content": "Original question"}, {"role": "assistant", "content": "Original answer"}]
    concurrent = [*previous, {"role": "user", "content": "Concurrent question"},
                  {"role": "assistant", "content": "Concurrent answer"}]
    history.write_text(json.dumps({"history": previous}))
    options = {"transcript_path": str(fixture_session), "question": "Why?", "history_path": history,
               "limit": 1, "language": None, "max_steps": 1, "assistant_only": False}
    plan = llm_cmd.plan_prompts(fixture_run, "analyze", **options)
    original_read, reads = llm_cmd._read_history, []
    def changed(path):
        captured = original_read(path)
        reads.append(captured)
        if len(reads) == 1:
            history.write_text(json.dumps({"history": concurrent}))
        return captured
    monkeypatch.setattr(llm_cmd, "_read_history", changed)
    with pytest.raises(BridgeError) as caught:
        llm_cmd.apply_replies(fixture_run, "analyze", {"hash": plan["hash"], "replies": [
            {"index": 0, "text": "An answer based only on the original context"}]},
            **options, out_path=tmp_path / "unused.json")
    assert caught.value.code == "stale_prompts"
    assert json.loads(history.read_text())["history"] == concurrent


def test_env_analysis_does_not_overwrite_history_changed_during_provider_latency(fixture_run, fixture_session,
                                                                                tmp_path, monkeypatch, no_llm_env):
    from agent_trajviz.insight import assistant

    monkeypatch.chdir(tmp_path)
    for key, value in (("BASE_URL", "http://127.0.0.1:9"), ("API_KEY", "synthetic"), ("MODEL", "test")):
        monkeypatch.setenv(f"ANALYZE_{key}", value)
    history = tmp_path / "analyze.json"
    concurrent = [{"role": "user", "content": "A different exchange completed first"}]
    def complete(*_args, **_kwargs):
        history.write_text(json.dumps({"history": concurrent}))
        return "Answer to the old exchange"
    monkeypatch.setattr(assistant, "complete_chat", complete)
    with pytest.raises(BridgeError) as caught:
        llm_cmd.direct(fixture_run, "analyze", transcript_path=str(fixture_session), question="Why?",
                       history_path=history, limit=1, language=None, max_steps=1, assistant_only=False,
                       out_path=tmp_path / "unused.json")
    assert caught.value.code == "stale_prompts" and json.loads(history.read_text())["history"] == concurrent


def test_label_replay_cannot_replace_the_replies_input_file(fixture_session, tmp_path, run_cli):
    base = ["--transcript", str(fixture_session), "--artifacts", str(tmp_path / "artifacts"),
            "--feature", "label", "--max-steps", "1"]
    code, plan = run_cli(["llm-prompt", *base])
    assert code == 0
    replies = Path(_reply_file(plan, "label", tmp_path))
    original = replies.read_bytes()
    code, result = run_cli(["llm-apply", *base, "--replies", str(replies), "--out", str(replies)])
    assert code == 1 and result["error"]["code"] == "bad_output" and replies.read_bytes() == original


def test_simultaneous_analysis_commits_cannot_both_replace_the_same_exchange(tmp_path):
    worker = """
import json
import sys
import time
from pathlib import Path
from trajviz_obs import llm_cmd
from trajviz_obs.errors import BridgeError
folder, name = Path(sys.argv[1]), sys.argv[2]
original_write = llm_cmd._write_json
def slow_write(*args, **kwargs):
    time.sleep(0.15)  # expose compare/write races in two independently running bridge processes
    return original_write(*args, **kwargs)
llm_cmd._write_json = slow_write
(folder / ('ready-' + name)).touch()
deadline = time.monotonic() + 5
while not (folder / 'start').exists():
    if time.monotonic() > deadline:
        raise RuntimeError('coordinator did not start both workers')
    time.sleep(0.005)
try:
    llm_cmd._result('analyze', [{'role': 'user', 'content': name}, {'role': 'assistant', 'content': name}],
                    history_path=folder / 'analyze.json', limit=1, out_path=folder / 'unused', expected_history=[])
    print(json.dumps({'name': name, 'result': 'saved'}))
except BridgeError as exc:
    print(json.dumps({'name': name, 'result': exc.code}))
"""
    processes = [subprocess.Popen([sys.executable, "-c", worker, str(tmp_path), name],
                                  cwd=Path(llm_cmd.__file__).resolve().parent.parent,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for name in ("one", "two")]
    try:
        deadline = time.monotonic() + 10
        while not all((tmp_path / f"ready-{name}").exists() for name in ("one", "two")):
            for process in processes:
                if process.poll() is not None:
                    pytest.fail(f"Analysis worker exited before committing: {process.communicate()}")
            assert time.monotonic() < deadline, "Analysis workers did not become ready"
            time.sleep(0.005)
        (tmp_path / "start").touch()
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            assert process.returncode == 0, stderr
            results.append(json.loads(stdout))
        assert sorted(result["result"] for result in results) == ["saved", "stale_prompts"]
        saved = next(result["name"] for result in results if result["result"] == "saved")
        assert all(message["content"] == saved for message in json.loads((tmp_path / "analyze.json").read_text())["history"])
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait()


def test_history_lock_cannot_alias_the_input_session(fixture_session, tmp_path, run_cli):
    lock = llm_cmd._history_lock_path(tmp_path / "analyze.json")
    lock.symlink_to(fixture_session)
    code, plan = run_cli(["llm-prompt", "--feature", "analyze", "--transcript", str(fixture_session),
                          "--artifacts", str(tmp_path)])
    assert code == 0
    code, result = run_cli(["llm-apply", "--feature", "analyze", "--transcript", str(fixture_session),
                            "--artifacts", str(tmp_path), "--replies", _reply_file(plan, "analyze", tmp_path)])
    assert code == 1 and result["error"]["code"] == "bad_output"
