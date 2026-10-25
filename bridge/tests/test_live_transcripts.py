"""Runtime transcripts can grow or end in an incomplete append without hiding interior corruption."""

from __future__ import annotations

from pathlib import Path

import pytest

from trajviz_obs.compat import load_run
from trajviz_obs.errors import BridgeError
from trajviz_obs.llm_cmd import apply_replies, plan_prompts


def test_current_snapshot_tolerates_only_an_in_progress_tail(fixture_session, tmp_path, run_cli):
    source = tmp_path / "live.jsonl"
    source.write_bytes(fixture_session.read_bytes())
    baseline = load_run(str(source))
    with source.open("ab") as handle:
        handle.write(b'{"type":"assistant","message":{"content":')
    code, out = run_cli(["snapshot", "--transcript", str(source), "--kind", "current"])
    assert code == 0 and not out.get("empty")
    assert out["counts"]["records"] == len(baseline.steps)
    with source.open("ab") as handle:
        handle.write(b"\n")
    code, out = run_cli(["snapshot", "--transcript", str(source), "--kind", "current"])
    assert code == 1 and out["error"]["code"] == "load_error"


@pytest.mark.parametrize("command", ["snapshot", "view", "json", "report", "brief", "attribution", "compare",
                                     "scorecard", "llm-prompt", "ask", "fixes", "label"])
def test_current_session_with_only_a_real_user_prompt_is_empty(command, fixture_session, tmp_path, run_cli):
    import json

    source = tmp_path / "new.jsonl"
    source.write_text(json.dumps({"type": "user", "sessionId": "new", "uuid": "first",
                                 "message": {"role": "user", "content": "build it"}}) + "\n")
    argv = [command, "--transcript", str(source), "--kind", "current", "--artifacts", str(tmp_path / "artifacts")]
    if command == "view":
        argv += ["health"]
    elif command == "compare":
        argv += ["--other", str(fixture_session)]
    elif command == "scorecard":
        argv += ["--others", str(fixture_session)]
    elif command == "llm-prompt":
        argv += ["--feature", "analyze"]
    code, out = run_cli(argv)
    assert code == 0 and out.get("empty") is True, (command, out)
    assert not (tmp_path / "artifacts").exists()
    assert len(load_run(str(source)).steps) == 1  # historical inputs can still describe a user-only run


def test_label_replies_are_stale_when_only_a_subagent_changes(tmp_session, tmp_path):
    source = Path(tmp_session.transcript)
    options = {"transcript_path": str(source), "question": None, "history_path": None, "limit": 3,
               "language": None, "max_steps": 2, "assistant_only": False}
    plan = plan_prompts(load_run(str(source)), "label", **options)
    child = next(source.with_suffix("").rglob("*.jsonl"))
    child.write_text(child.read_text().replace("The build is fine.", "The build failed."))
    replies = {"hash": plan["hash"], "replies": []}
    with pytest.raises(BridgeError) as exc:
        apply_replies(load_run(str(source)), "label", replies, out_path=tmp_path / "labels.json", **options)
    assert exc.value.code == "stale_prompts"
    assert not (tmp_path / "labels.json").exists()


@pytest.mark.parametrize("command", ["snapshot", "view", "json", "report", "brief", "attribution", "compare",
                                     "scorecard", "llm-prompt", "ask", "fixes", "label"])
def test_current_session_corruption_is_reported_by_every_loading_entrypoint(command, fixture_session, tmp_path,
                                                                          run_cli):
    source = tmp_path / "corrupt.jsonl"
    lines = fixture_session.read_bytes().splitlines(keepends=True)
    source.write_bytes(b"".join([*lines[:3], b"{broken\n", *lines[3:]]))
    argv = [command, "--transcript", str(source), "--kind", "current", "--artifacts", str(tmp_path / "artifacts")]
    if command == "view":
        argv += ["health"]
    elif command == "compare":
        argv += ["--other", str(fixture_session)]
    elif command == "scorecard":
        argv += ["--others", str(fixture_session)]
    elif command == "llm-prompt":
        argv += ["--feature", "analyze"]
    code, out = run_cli(argv)
    assert code == 1 and out["error"]["code"] == "load_error", (command, out)
    assert "Invalid JSONL at line 4" in out["error"]["message"]
    assert not (tmp_path / "artifacts").exists()
