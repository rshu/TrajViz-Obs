"""Every artifact writer must refuse paths that would replace the analyzed session."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from trajviz_obs.compat import load_run, protect_run_outputs
from trajviz_obs.errors import BridgeError
from trajviz_obs.llm_cmd import apply_replies, plan_prompts
from trajviz_obs.outputs import protect_inputs
from trajviz_obs.views_cmd import _write_json


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


@pytest.mark.parametrize("command,relative", [("view", "views/health.json"), ("json", "views/trajectory.json"),
                                               ("llm-prompt", "prompts-analyze.json"),
                                               ("ask", "analyze.json")])
def test_reloaded_export_protects_its_actual_input_not_embedded_provenance(command, relative, fixture_run,
                                                                         tmp_path, run_cli):
    artifacts = tmp_path / "artifacts"
    source = artifacts / relative
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps(fixture_run.to_dict(include_raw=True)))
    original = source.read_bytes()
    argv = [command, "--transcript", str(source), "--artifacts", str(artifacts)]
    if command == "view":
        argv += ["health"]
    elif command == "json":
        argv += ["--with-trajectory"]
    elif command == "llm-prompt":
        argv += ["--feature", "analyze"]
    code, out = run_cli(argv)
    assert code == 1 and out["error"]["code"] == "bad_output", out
    assert source.read_bytes() == original


@pytest.mark.parametrize("readable", [True, False])
def test_scorecard_directory_input_protects_selected_transcript_hardlinks(readable, fixture_session, tmp_path, run_cli):
    artifacts = tmp_path / "artifacts"
    directory = artifacts / "compare"
    directory.mkdir(parents=True)
    source = directory / "session.jsonl"
    original = fixture_session.read_bytes() if readable else b"{broken\n"
    source.write_bytes(original)
    output = directory / "scorecard.json"
    os.link(source, output)
    code, out = run_cli(["scorecard", "--no-self", "--transcript", str(fixture_session), "--artifacts",
                         str(artifacts), "--others", str(directory), str(fixture_session), str(fixture_session)])
    assert code == 1 and out["error"]["code"] == "bad_output", out
    assert source.read_bytes() == output.read_bytes() == original


@pytest.mark.parametrize("layout", ["renamed", "symlink", "legacy"])
def test_protection_covers_every_native_child_discovery_layout(layout, tmp_session, tmp_path):
    source = Path(tmp_session.transcript)
    child = next(source.with_suffix("").rglob("*.jsonl"))
    if layout == "renamed":
        renamed = source.with_name("renamed.jsonl")
        source.rename(renamed)
        source = renamed
    elif layout == "symlink":
        alias = tmp_path / "alias.jsonl"
        alias.symlink_to(source)
        source = alias
    else:
        legacy = source.parent / child.name
        child.rename(legacy)
        child = legacy
    run = load_run(str(source))
    assert any(step.get("agent") == "abc" for step in run.steps)
    with pytest.raises(BridgeError) as exc:
        protect_run_outputs(run, [child])
    assert exc.value.code == "bad_output"


@pytest.mark.parametrize("command,relative", [("view", "views/health.json"), ("json", "views/health.json"),
                                               ("bundle", "bundle.zip")])
def test_artifact_staging_cannot_follow_a_preexisting_symlink(command, relative, fixture_session, tmp_path, run_cli):
    artifacts = tmp_path / "artifacts"
    destination = artifacts / relative
    destination.parent.mkdir(parents=True)
    unrelated = tmp_path / "important.txt"
    unrelated.write_text("keep this data")
    legacy_stage = destination.with_name(destination.name + ".tmp")
    legacy_stage.symlink_to(unrelated)
    argv = [command, "--transcript", str(fixture_session), "--artifacts", str(artifacts)]
    if command == "view":
        argv += ["health"]
    elif command == "json":
        argv += ["--view", "health"]
    code, out = run_cli(argv)
    assert code == 0, out
    assert destination.is_file() and unrelated.read_text() == "keep this data"
    assert legacy_stage.is_symlink()
    assert not list(artifacts.rglob(".trajviz-obs-*"))


def test_overlapping_json_writers_leave_one_complete_document_and_no_shared_staging(tmp_path):
    destination = tmp_path / "result.json"
    payloads = [{"request": index, "text": str(index) * 50_000} for index in range(24)]
    with ThreadPoolExecutor(max_workers=12) as pool:
        sizes = list(pool.map(lambda payload: _write_json(destination, payload), payloads))
    assert len(sizes) == len(payloads) and all(size > 0 for size in sizes)
    final = json.loads(destination.read_text())
    assert final == payloads[final["request"]]
    assert list(tmp_path.iterdir()) == [destination]


def test_failed_json_serialization_retains_previous_artifact_without_staging_files(tmp_path):
    destination = tmp_path / "result.json"
    destination.write_text('{"previous":true}\n')
    with pytest.raises(ValueError):
        _write_json(destination, {"invalid": float("nan")})
    assert destination.read_text() == '{"previous":true}\n' and list(tmp_path.iterdir()) == [destination]
