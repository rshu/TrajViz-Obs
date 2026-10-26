"""A native session and its subagents remain portable and protected during bundle export."""

from __future__ import annotations

import zipfile
from pathlib import Path

import agent_trajviz as tv
import pytest

from trajviz_obs.bundle_cmd import bundle_session
from trajviz_obs.errors import BridgeError


def test_bundle_layout(tmp_session_with_child, tmp_artifacts):
    out = bundle_session(tmp_session_with_child.transcript, tmp_artifacts / "bundle.zip")
    names = zipfile.ZipFile(out["path"]).namelist()
    assert f"{tmp_session_with_child.id}.jsonl" in names and any(n.startswith(f"{tmp_session_with_child.id}/subagents/") for n in names)


def test_bundle_members_and_trajviz_reads_it(tmp_session_with_child, tmp_artifacts):
    sid = tmp_session_with_child.id
    out = bundle_session(tmp_session_with_child.transcript, tmp_artifacts / "bundle.zip")
    assert out["members"] == [f"{sid}.jsonl", f"{sid}/subagents/workflows/wf_1/agent-abc.jsonl",
                              f"{sid}/subagents/workflows/wf_1/agent-abc.meta.json"]
    assert out["bytes"] == (tmp_artifacts / "bundle.zip").stat().st_size
    assert len(tv.load(out["path"]).view("agent_tokens").data["agents"]) == 2


def test_bundle_skips_symlinks(tmp_session_with_child, tmp_artifacts, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("not part of the session")
    (tmp_session_with_child.dir / tmp_session_with_child.id / "subagents" / "link.jsonl").symlink_to(secret)
    out = bundle_session(tmp_session_with_child.transcript, tmp_artifacts / "bundle.zip")
    assert not any(m.endswith("link.jsonl") for m in out["members"])


@pytest.mark.parametrize("source_kind", ["transcript", "child", "staging"])
def test_bundle_cannot_overwrite_session_input(source_kind, tmp_session):
    transcript = Path(tmp_session.transcript)
    source = (next((tmp_session.dir / tmp_session.id / "subagents").rglob("*.jsonl"))
              if source_kind == "child" else transcript)
    original = source.read_bytes()
    # An output's staging file must also be protected (foo.jsonl -> foo.jsonl.tmp).
    if source_kind == "staging":
        source = transcript.with_name("staging.tmp")
        transcript.rename(source)
        transcript = source
        dest = source.with_name("staging")
    else:
        dest = source
    with pytest.raises(BridgeError) as exc:
        bundle_session(str(transcript), dest)
    assert exc.value.code == "bad_output" and source.read_bytes() == original


def test_bundle_cannot_overwrite_session_through_symlink(tmp_session):
    transcript = Path(tmp_session.transcript)
    alias = transcript.with_name("output-alias")
    alias.symlink_to(transcript)
    original = transcript.read_bytes()
    with pytest.raises(BridgeError) as exc:
        bundle_session(str(transcript), alias)
    assert exc.value.code == "bad_output" and transcript.read_bytes() == original


def test_cli_bundle_default_path(run_cli, tmp_session_with_child, tmp_artifacts):
    code, out = run_cli(["bundle", "--transcript", tmp_session_with_child.transcript, "--artifacts", str(tmp_artifacts)])
    assert code == 0 and out["path"] == str(tmp_artifacts / "bundle.zip") and len(out["members"]) == 3
    assert out["members_total"] == 3


def test_cli_bundle_missing_transcript(run_cli, tmp_path):
    missing = tmp_path / "nope.jsonl"
    code, out = run_cli(["bundle", "--transcript", str(missing), "--out", str(tmp_path / "b.zip")])
    assert code == 1 and out["error"]["code"] == "not_found" and not (tmp_path / "b.zip").exists()


def test_cli_bundle_explicit_nested_output(run_cli, fixture_session, tmp_path):
    destination = tmp_path / "exports" / "session.zip"
    code, out = run_cli(["bundle", "--transcript", str(fixture_session), "--out", str(destination)])
    assert code == 0 and out["path"] == str(destination) and out["bytes"] == destination.stat().st_size
    with zipfile.ZipFile(destination) as archive:
        assert archive.read(fixture_session.name) == fixture_session.read_bytes()
    assert list(destination.parent.iterdir()) == [destination]


def test_cli_bundle_refuses_directory_output(run_cli, fixture_session, tmp_artifacts):
    code, out = run_cli(["bundle", "--transcript", str(fixture_session), "--out", str(tmp_artifacts)])
    assert code == 1 and out["error"]["code"] == "bad_path"
    assert out["error"]["path"] == str(tmp_artifacts) and list(tmp_artifacts.iterdir()) == []


def test_failed_bundle_preserves_previous_export_and_removes_partial_file(fixture_session, tmp_path, monkeypatch):
    destination = tmp_path / "session.zip"
    destination.write_bytes(b"previous complete export")
    original = zipfile.ZipFile.write

    def interrupted(archive, *args, **kwargs):
        original(archive, *args, **kwargs)
        raise OSError("interrupted archive write")

    monkeypatch.setattr(zipfile.ZipFile, "write", interrupted)
    with pytest.raises(OSError, match="interrupted archive write"):
        bundle_session(str(fixture_session), destination)
    assert destination.read_bytes() == b"previous complete export" and list(tmp_path.iterdir()) == [destination]


def test_bundle_lists_at_most_200_members(tmp_session_with_child, tmp_artifacts, run_cli):
    sub = tmp_session_with_child.dir / tmp_session_with_child.id / "subagents"
    for i in range(250):
        (sub / f"agent-x{i:03d}.jsonl").write_text("{}\n")
    out = bundle_session(tmp_session_with_child.transcript, tmp_artifacts / "bundle.zip")
    names = zipfile.ZipFile(out["path"]).namelist()
    assert out["members_total"] == len(names) == 253 and out["members"] == names[:200]
    code, out = run_cli(["bundle", "--transcript", tmp_session_with_child.transcript, "--out", out["path"]])
    assert code == 0 and len(out["members"]) == 200 and out["members_total"] == 253
