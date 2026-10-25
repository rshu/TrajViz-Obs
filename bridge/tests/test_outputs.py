"""Every artifact writer must refuse paths that would replace the analyzed session."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from trajviz_obs.compat import load_run, protect_run_outputs
from trajviz_obs.errors import BridgeError
from trajviz_obs.llm_cmd import apply_replies, plan_prompts
from trajviz_obs.outputs import protect_inputs


@pytest.mark.parametrize("command", ["view", "json-view", "json-trajectory", "compare", "scorecard", "llm-prompt",
                                     "ask"])
def test_cli_artifact_writers_cannot_replace_their_transcript(command, fixture_session, tmp_path, run_cli):
    artifacts = tmp_path / "artifacts"
    relative = {"view": "views/health.json", "json-view": "views/health.json",
                "json-trajectory": "views/trajectory.json", "compare": "compare/other.json",
                "scorecard": "compare/scorecard.json", "llm-prompt": "prompts-fixes.json",
                "ask": "analyze.json"}[command]
    source = artifacts / relative
    source.parent.mkdir(parents=True)
    original = fixture_session.read_bytes()
    source.write_bytes(original)
    base = ["--transcript", str(source), "--artifacts", str(artifacts)]
    if command == "view":
        argv = ["view", *base, "health"]
    elif command.startswith("json-"):
        argv = ["json", *base, *(["--with-trajectory"] if command == "json-trajectory" else ["--view", "health"])]
    elif command == "compare":
        other = tmp_path / "other.jsonl"
        other.write_bytes(original)
        argv = ["compare", *base, "--other", str(other)]
    elif command == "scorecard":
        argv = ["scorecard", *base, "--no-self", "--others", str(source), str(fixture_session)]
    elif command == "llm-prompt":
        argv = ["llm-prompt", *base, "--feature", "fixes"]
    else:
        argv = ["ask", *base, "--question", "Why?"]
    code, out = run_cli(argv)
    assert code == 1 and out["error"]["code"] == "bad_output", out
    assert source.read_bytes() == original


def test_label_apply_cannot_replace_child_sidecar(tmp_session):
    source = Path(tmp_session.transcript)
    run = load_run(str(source))
    options = {"transcript_path": str(source), "question": None, "history_path": None, "limit": 3,
               "language": None, "max_steps": 2, "assistant_only": False}
    plan = plan_prompts(run, "label", **options)
    child = next(source.with_suffix("").rglob("*.meta.json"))
    original = child.read_bytes()
    replies = {"hash": plan["hash"], "replies": []}
    with pytest.raises(BridgeError) as exc:
        apply_replies(run, "label", replies, out_path=child, **options)
    assert exc.value.code == "bad_output" and child.read_bytes() == original


def test_directory_input_protects_the_actual_transcript(tmp_session):
    run = load_run(str(tmp_session.dir / tmp_session.id))
    with pytest.raises(BridgeError) as exc:
        protect_run_outputs(run, [Path(tmp_session.transcript)])
    assert exc.value.code == "bad_output"


def test_output_protection_also_covers_hardlinks(tmp_path):
    source, output = tmp_path / "source.jsonl", tmp_path / "export.json"
    source.write_text(json.dumps({"source": True}))
    os.link(source, output)
    with pytest.raises(BridgeError) as exc:
        protect_inputs([output], [source])
    assert exc.value.code == "bad_output"
