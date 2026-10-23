import json
import os
from pathlib import Path

import pytest
from conftest import AAA1, AAA2, BBB3, _write_session

from trajviz_obs import paths
from trajviz_obs.errors import BridgeError
from trajviz_obs.paths import project_slug
from trajviz_obs.sessions import Resolved, list_sessions, resolve_session

# paths


def test_slug_replaces_every_non_alnum():
    assert project_slug("/Users/me/My Proj.x_y") == "-Users-me-My-Proj-x-y"


def test_cache_root_prefers_xdg_then_home_then_localappdata():
    assert paths.cache_root({"XDG_CACHE_HOME": "/xdg", "HOME": "/h"}) == Path("/xdg")
    assert paths.cache_root({"HOME": "/h"}) == Path("/h/.cache")
    assert paths.cache_root({"OS": "Windows_NT", "LOCALAPPDATA": r"C:\Local"}) == Path(r"C:\Local")
    assert paths.cache_root({}) == Path.home() / ".cache"


def test_artifacts_dir_is_per_session():
    assert paths.artifacts_dir(Path("/c"), AAA1) == Path("/c/trajviz-obs/sessions") / AAA1
    for bad in ("", ".", "..", "a/b", "a\\b"):
        with pytest.raises(ValueError):
            paths.artifacts_dir(Path("/c"), bad)


def test_config_dir_and_projects_dir():
    assert paths.config_dir({"CLAUDE_CONFIG_DIR": "/cfg", "HOME": "/h"}) == Path("/cfg")
    assert paths.config_dir({"HOME": "/h"}) == Path("/h/.claude")
    assert paths.config_dir({}) == Path.home() / ".claude"
    assert paths.projects_dir(Path("/cfg")) == Path("/cfg/projects")


# resolve_session


def test_resolve_unique_prefix(fake_projects):
    r = resolve_session("bbb", cwd=fake_projects.cwd, config_dir=fake_projects.config)
    assert r.session_id.startswith("bbb") and r.kind == "file"
    assert r == Resolved(path=str(fake_projects.project / f"{BBB3}.jsonl"), session_id=BBB3,
                         project_dir=str(fake_projects.project), kind="file")


def test_resolve_full_id(fake_projects):
    r = resolve_session(AAA2, cwd=fake_projects.cwd, config_dir=fake_projects.config)
    assert r.session_id == AAA2 and r.path == str(fake_projects.project / f"{AAA2}.jsonl")


def test_resolve_ambiguous_prefix_lists_candidates(fake_projects):
    with pytest.raises(BridgeError) as e:
        resolve_session("aaa", cwd=fake_projects.cwd, config_dir=fake_projects.config)
    assert e.value.code == "ambiguous" and len(e.value.extra["candidates"]) == 2
    by_id = {c["id"]: c for c in e.value.extra["candidates"]}
    assert set(by_id) == {AAA1, AAA2}
    assert by_id[AAA1]["started"] == "2026-10-01T10:00:00.000Z"
    assert by_id[AAA2]["size"] == (fake_projects.project / f"{AAA2}.jsonl").stat().st_size


def test_resolve_unknown_prefix_is_not_found(fake_projects):
    with pytest.raises(BridgeError) as e:
        resolve_session("ccc", cwd=fake_projects.cwd, config_dir=fake_projects.config)
    assert e.value.code == "not_found" and e.value.path == str(fake_projects.project)


def test_resolve_prefix_without_project_folder_is_not_found(tmp_path):
    with pytest.raises(BridgeError) as e:
        resolve_session("aaa", cwd=str(tmp_path / "elsewhere"), config_dir=tmp_path / "config")
    assert e.value.code == "not_found"


def test_resolve_file_path_absolute_and_relative(fake_projects, tmp_path):
    other = tmp_path / "work" / "My Proj.x_y" / "runs" / "other.jsonl"
    other.parent.mkdir(parents=True)
    other.write_text("{}\n")
    for spec in (str(other), "runs/other.jsonl"):
        r = resolve_session(spec, cwd=fake_projects.cwd, config_dir=fake_projects.config)
        assert r == Resolved(path=str(other), session_id="other", project_dir=str(other.parent), kind="file")


def test_resolve_session_directory(fake_projects):
    folder = fake_projects.project / BBB3
    r = resolve_session(str(folder), cwd=fake_projects.cwd, config_dir=fake_projects.config)
    assert r == Resolved(path=str(folder), session_id=BBB3, project_dir=str(fake_projects.project), kind="dir")


def test_resolve_missing_path_is_not_found(fake_projects):
    with pytest.raises(BridgeError) as e:
        resolve_session("runs/missing.jsonl", cwd=fake_projects.cwd, config_dir=fake_projects.config)
    assert e.value.code == "not_found"
    assert e.value.path == os.path.join(fake_projects.cwd, "runs/missing.jsonl")


# list_sessions


def test_list_sessions_has_started_and_records(fake_projects):
    rows = list_sessions(cwd=fake_projects.cwd, config_dir=fake_projects.config, limit=10)
    assert rows[0]["records"] >= 1 and rows[0]["started"] is not None
    assert [row["id"] for row in rows] == [BBB3, AAA2, AAA1]
    newest = fake_projects.project / f"{BBB3}.jsonl"
    assert rows[0] == {"id": BBB3, "path": str(newest), "mtime_ms": 1_700_000_300_000,
                       "size": newest.stat().st_size, "records": 6, "started": "2026-10-03T10:00:00.000Z"}


def test_list_sessions_limit(fake_projects):
    rows = list_sessions(cwd=fake_projects.cwd, config_dir=fake_projects.config, limit=1)
    assert [row["id"] for row in rows] == [BBB3]


def test_list_sessions_started_only_from_first_50_lines(fake_projects):
    late = fake_projects.project / "late.jsonl"
    body = "".join(json.dumps({"type": "progress"}) + "\n" for _ in range(50))
    late.write_text(body + json.dumps({"timestamp": "2026-10-09T00:00:00Z"}) + "\n")
    os.utime(late, (1_800_000_000, 1_800_000_000))
    row = list_sessions(cwd=fake_projects.cwd, config_dir=fake_projects.config, limit=1)[0]
    assert row["id"] == "late" and row["started"] is None and row["records"] == 51


def test_list_sessions_tolerates_partial_and_malformed_lines(fake_projects):
    live = fake_projects.project / "live.jsonl"
    live.write_text('not json\n{"timestamp": "2026-10-09T00:00:00Z"}\n{"type": "assi')
    os.utime(live, (1_800_000_000, 1_800_000_000))
    row = list_sessions(cwd=fake_projects.cwd, config_dir=fake_projects.config, limit=1)[0]
    assert row["started"] == "2026-10-09T00:00:00Z" and row["records"] == 3


def test_list_sessions_missing_project_is_empty(tmp_path):
    assert list_sessions(cwd=str(tmp_path / "nowhere"), config_dir=tmp_path / "config", limit=10) == []


# subcommands


def test_resolve_subcommand(run_cli, fake_projects):
    code, out = run_cli(["resolve", "--cwd", fake_projects.cwd, "--config-dir", str(fake_projects.config),
                         "--session", "bbb"])
    assert code == 0
    assert out == {"path": str(fake_projects.project / f"{BBB3}.jsonl"), "session_id": BBB3,
                   "project_dir": str(fake_projects.project), "kind": "file"}


def test_resolve_subcommand_ambiguous_envelope(run_cli, fake_projects):
    code, out = run_cli(["resolve", "--cwd", fake_projects.cwd, "--config-dir", str(fake_projects.config),
                         "--session", "aaa"])
    assert code == 1 and out["error"]["code"] == "ambiguous"
    assert {c["id"] for c in out["error"]["candidates"]} == {AAA1, AAA2}
    assert all(c["started"] for c in out["error"]["candidates"])


def test_resolve_session_id_scans_every_project(run_cli, fake_projects):
    elsewhere = fake_projects.config / "projects" / "-other-project"
    elsewhere.mkdir()
    target = _write_session(elsewhere, "ddd44444", "2026-10-04T00:00:00Z", 1_700_000_400)
    code, out = run_cli(["resolve", "--cwd", fake_projects.cwd, "--config-dir", str(fake_projects.config),
                         "--session-id", "ddd44444"])
    assert code == 0 and out["path"] == str(target) and out["project_dir"] == str(elsewhere)


def test_resolve_session_id_is_exact(run_cli, fake_projects):
    for spec in ("bbb", "../projects"):
        code, out = run_cli(["resolve", "--cwd", fake_projects.cwd, "--config-dir", str(fake_projects.config),
                             "--session-id", spec])
        assert code == 1 and out["error"]["code"] == "not_found"


def test_resolve_subcommand_needs_one_spec(run_cli, fake_projects):
    base = ["resolve", "--cwd", fake_projects.cwd, "--config-dir", str(fake_projects.config)]
    assert run_cli(base)[0] == 2
    assert run_cli([*base, "--session", "a", "--session-id", "b"])[0] == 2


def test_sessions_subcommand(run_cli, fake_projects):
    code, out = run_cli(["sessions", "--cwd", fake_projects.cwd, "--config-dir", str(fake_projects.config),
                         "--limit", "2"])
    assert code == 0
    assert out["project_dir"] == str(fake_projects.project)
    assert [row["id"] for row in out["sessions"]] == [BBB3, AAA2]
