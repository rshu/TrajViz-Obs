"""Every artifact writer must refuse paths that would replace the analyzed session."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from trajviz_obs.compat import load_run, protect_run_outputs
from trajviz_obs.errors import BridgeError
from trajviz_obs.llm_cmd import apply_replies, plan_prompts
from trajviz_obs.outputs import host_inputs, protect_inputs, staged_path
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


@pytest.mark.parametrize(("command", "name"), [
    ("json", "trajectory.json"), ("schemas", "summary.schema.json"), ("schemas", "workflow.schema.json"),
    ("bundle", "portable.zip"), ("label", "labels.json"),
])
def test_host_protects_actual_source_when_bridge_reads_a_private_capture(command, name, fixture_run,
                                                                       fixture_session, tmp_path):
    capture = tmp_path / "private-capture.json"
    capture.write_text(json.dumps(fixture_run.to_dict(include_raw=True)))
    out = tmp_path / "out"
    out.mkdir()
    native = out / name
    before = fixture_session.read_bytes()
    native.write_bytes(before)
    if command in {"json", "schemas"}:
        argv = [command, "--out", str(out)]
        if command == "json":
            argv += ["--transcript", str(capture), "--view", "summary"]
    else:
        argv = [command, "--transcript", str(capture), "--out", str(native)]
    launcher = Path(__file__).resolve().parents[1] / "trajviz_obs_cli.py"
    result = subprocess.run([sys.executable, "-I", str(launcher), f"--host-input={native}", *argv],
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert json.loads(result.stdout)["error"]["code"] == "bad_output"
    assert native.read_bytes() == before
    assert list(out.iterdir()) == [native]  # bulk writers fail before replacing even an earlier schema/view


@pytest.mark.parametrize("alias", ["hardlink", "symlink", "parent-symlink"])
@pytest.mark.parametrize("command", ["json", "schemas"])
def test_host_output_guard_follows_child_aliases_without_denying_the_directory(alias, command, fixture_run,
                                                                            fixture_session, tmp_path, run_cli):
    capture = tmp_path / "private.json"
    capture.write_text(json.dumps(fixture_run.to_dict(include_raw=True)))
    native = tmp_path / "native.jsonl"
    original = fixture_session.read_bytes()
    native.write_bytes(original)
    out = tmp_path / "export"
    out.mkdir()
    name = "trajectory.json" if command == "json" else "workflow.schema.json"
    if alias == "hardlink":
        os.link(native, out / name)
    else:
        (out / name).symlink_to(native)
        if alias == "parent-symlink":
            linked = tmp_path / "linked-folder"
            linked.symlink_to(out, target_is_directory=True)
            out = linked
    argv = [command, "--out", str(out)]
    if command == "json":
        argv += ["--transcript", str(capture), "--view", "summary"]
    code, doc = run_cli([f"--host-input={native}", *argv])
    assert code == 1 and doc["error"]["code"] == "bad_output", doc
    assert native.read_bytes() == original
    assert [child.name for child in out.iterdir()] == [name]


def test_host_protection_is_shared_by_worker_threads_and_resets_after_failure(tmp_path):
    source = tmp_path / "native.jsonl"
    source.write_text("Keep native source")
    other = tmp_path / "other.jsonl"
    other.write_text("Keep other source")
    with host_inputs([source]):
        with host_inputs([other]), ThreadPoolExecutor(max_workers=1) as pool:
            for destination in (source, other):
                with pytest.raises(BridgeError, match="overwrite input session"):
                    pool.submit(_write_json, destination, {"do": "not replace"}).result()
        with pytest.raises(BridgeError):
            protect_inputs([source], ())
        protect_inputs([other], ())
        with (host_inputs([]), ThreadPoolExecutor(max_workers=1) as pool,
              pytest.raises(BridgeError, match="overwrite input session")):
            pool.submit(_write_json, source, {"still": "protected by outer scope"}).result()
    _write_json(source, {"now": "unprotected temporary fixture"})
    assert json.loads(source.read_text()) == {"now": "unprotected temporary fixture"}


def test_cli_host_scope_does_not_leak_into_a_later_command(tmp_path, run_cli):
    native = tmp_path / "workflow.schema.json"
    native.write_text("Keep native source")
    code, doc = run_cli([f"--host-input={native}", "schemas", "--out", str(tmp_path)])
    assert code == 1 and doc["error"]["code"] == "bad_output"
    assert native.read_text() == "Keep native source" and list(tmp_path.iterdir()) == [native]
    code, doc = run_cli(["schemas", "--out", str(tmp_path)])
    assert code == 0 and doc["files"]
    assert isinstance(json.loads(native.read_text()), dict)


def test_host_guard_allows_adjacent_exports_and_remains_private(tmp_path, fixture_run, fixture_session, run_cli):
    native = tmp_path / "native.jsonl"
    original = fixture_session.read_bytes()
    native.write_bytes(original)
    capture = tmp_path / "private.json"
    capture.write_text(json.dumps(fixture_run.to_dict(include_raw=True)))
    code, doc = run_cli([f"--host-input={native}", f"--host-input={capture}", "json", "--transcript", str(capture),
                         "--out", str(tmp_path), "--view", "summary"])
    assert code == 0 and "trajectory.json" in doc["files"]
    assert native.read_bytes() == original
    code, help_doc = run_cli(["--help"])
    assert code == 0 and "host-input" not in help_doc["help"]
    code, invalid = run_cli(["--host-input=relative.jsonl", "doctor"])
    assert code == 2 and invalid["error"]["code"] == "usage"


def test_staged_writer_rechecks_a_destination_alias_before_installing(tmp_path):
    source, destination = tmp_path / "native.jsonl", tmp_path / "result.json"
    source.write_text("Keep native source")
    with (host_inputs([source]), pytest.raises(BridgeError, match="overwrite input session"),
          staged_path(destination) as stage):
        stage.write_text("Generated result")
        destination.symlink_to(source)
    assert source.read_text() == "Keep native source"
    assert not list(tmp_path.glob(".trajviz-obs-*"))


def test_staged_writer_without_host_inputs_still_replaces_a_symlink_atomically(tmp_path):
    destination = tmp_path / "result.json"
    destination.symlink_to(destination.name)
    _write_json(destination, {"complete": True})
    assert not destination.is_symlink() and json.loads(destination.read_text()) == {"complete": True}
