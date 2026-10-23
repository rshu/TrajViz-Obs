"""``report`` and ``bundle``: TrajViz's HTML report written to an explicit file, and the session as a ZIP."""

from __future__ import annotations

import zipfile

import agent_trajviz as tv
import pytest
from agent_trajviz import MissingExtraError

from trajviz_obs import report_cmd
from trajviz_obs.errors import BridgeError
from trajviz_obs.report_cmd import bundle_session, write_report_file


# Deviation from the brief: it counted "<html", but TrajViz's report also has "<html>" in a CSS comment ("The
# report declares its theme on <html> ..."), so the brief's count is 2 for one document. One "<!DOCTYPE html>" and
# the dark class of the second write show the file was replaced, not appended to.
def test_report_written_and_overwritten(fixture_run, tmp_artifacts):
    a = write_report_file(fixture_run, tmp_artifacts / "report.html", dark=False, offline=False)
    b = write_report_file(fixture_run, tmp_artifacts / "report.html", dark=True, offline=False)
    text = (tmp_artifacts / "report.html").read_text()
    assert a["path"] == b["path"] and text.count("<!DOCTYPE html>") == 1 and "tv-theme-dark" in text


def test_bundle_layout(tmp_session_with_child, tmp_artifacts):
    out = bundle_session(tmp_session_with_child.transcript, tmp_artifacts / "bundle.zip")
    names = zipfile.ZipFile(out["path"]).namelist()
    assert f"{tmp_session_with_child.id}.jsonl" in names and any(n.startswith(f"{tmp_session_with_child.id}/subagents/") for n in names)


def test_report_bytes_and_offline_embeds_plotly(fixture_run, tmp_artifacts):
    online = write_report_file(fixture_run, tmp_artifacts / "a.html", dark=False, offline=False)
    offline = write_report_file(fixture_run, tmp_artifacts / "sub" / "b.html", dark=False, offline=True)
    assert online["bytes"] == (tmp_artifacts / "a.html").stat().st_size
    assert offline["path"] == str(tmp_artifacts / "sub" / "b.html") and offline["bytes"] > online["bytes"] + 1_000_000
    assert [p.name for p in tmp_artifacts.iterdir()] and not list(tmp_artifacts.rglob("*.tmp"))


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


def test_cli_report_default_path(run_cli, fixture_session, tmp_artifacts):
    code, out = run_cli(["report", "--transcript", str(fixture_session), "--artifacts", str(tmp_artifacts), "--dark"])
    assert code == 0 and out == {"path": str(tmp_artifacts / "report.html"),
                                 "bytes": (tmp_artifacts / "report.html").stat().st_size}


def test_cli_report_out_and_window(run_cli, fixture_session, tmp_path, monkeypatch):
    seen = {}
    real = tv.write_report

    def spy(run, dest, **kwargs):
        seen["window"] = run.raw["metadata"]["context_window_limit"]
        return real(run, dest, **kwargs)

    monkeypatch.setattr(report_cmd.tv, "write_report", spy)
    dest = tmp_path / "elsewhere" / "r.html"
    code, out = run_cli(["report", "--transcript", str(fixture_session), "--window", "1000000", "--out", str(dest)])
    assert code == 0 and out["path"] == str(dest) and dest.is_file() and seen["window"] == 1_000_000


def test_cli_report_missing_extra(run_cli, fixture_session, tmp_artifacts, monkeypatch):
    def missing(*args, **kwargs):
        raise MissingExtraError("charts", ["plotly"], "write_report()")

    monkeypatch.setattr(report_cmd.tv, "write_report", missing)
    code, out = run_cli(["report", "--transcript", str(fixture_session), "--artifacts", str(tmp_artifacts)])
    assert code == 1 and out["error"]["code"] == "missing_extra" and out["error"]["extra"] == "charts"
    assert not (tmp_artifacts / "report.html").exists()


def test_cli_report_out_is_directory(run_cli, fixture_session, tmp_artifacts):
    code, out = run_cli(["report", "--transcript", str(fixture_session), "--out", str(tmp_artifacts)])
    assert code == 1 and out["error"]["code"] == "bad_path" and out["error"]["path"] == str(tmp_artifacts)


def test_cli_report_empty_current(run_cli, tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text('{"type":"ai-title","aiTitle":"x"}\n')
    code, out = run_cli(["report", "--transcript", str(path), "--kind", "current", "--artifacts", str(tmp_path / "a")])
    assert code == 0 and out["empty"] is True and out["reason"] and not (tmp_path / "a").exists()


def test_cli_bundle_default_path(run_cli, tmp_session_with_child, tmp_artifacts):
    code, out = run_cli(["bundle", "--transcript", tmp_session_with_child.transcript, "--artifacts", str(tmp_artifacts)])
    assert code == 0 and out["path"] == str(tmp_artifacts / "bundle.zip") and len(out["members"]) == 3


def test_cli_bundle_missing_transcript(run_cli, tmp_path):
    missing = tmp_path / "nope.jsonl"
    code, out = run_cli(["bundle", "--transcript", str(missing), "--out", str(tmp_path / "b.zip")])
    assert code == 1 and out["error"]["code"] == "not_found" and not (tmp_path / "b.zip").exists()


def test_report_error_is_bridge_error(fixture_run, tmp_artifacts, monkeypatch):
    def fail(run, dest, **kwargs):
        dest.write_text("partial")
        raise tv.ReportError("the trajectory has no steps")

    monkeypatch.setattr(report_cmd.tv, "write_report", fail)
    with pytest.raises(BridgeError) as e:
        write_report_file(fixture_run, tmp_artifacts / "report.html", dark=False, offline=False)
    assert e.value.code == "report_failed" and list(tmp_artifacts.iterdir()) == []
