"""Source downloads have independent counters and availability."""

from __future__ import annotations

import json
import subprocess
import urllib.error

import pytest

from telemetry import downloads


def test_public_sources_report_downloads_separately(monkeypatch):
    def read(url):
        if url.startswith("https://api.github.com/"):
            return [{"tag_name": "v1", "assets": [
                {"name": "plugin.zip", "state": "uploaded", "download_count": 10},
                {"name": "draft.zip", "state": "open", "download_count": 20},
            ]}]
        assert url.endswith("/last-week/%40scope%2Fplugin")
        return {"start": "2026-11-09", "end": "2026-11-10", "downloads": [
            {"day": "2026-11-10", "downloads": 3}, {"day": "2026-11-09", "downloads": 0},
        ]}

    monkeypatch.setattr(downloads, "_read_json", read)
    assert downloads.github_downloads("rshu/TrajViz-Obs")["downloads"] == 10
    npm = downloads.npm_downloads("@scope/plugin", "last-week")
    assert npm["downloads"] == 3 and npm["daily"][0]["downloads"] == 0


@pytest.mark.parametrize("days", [
    ["2026-11-09", "2026-11-09"],  # Duplicate
    ["2026-11-09"],  # Incomplete
    ["2026-11-09", "2026-11-11"],  # Outside the returned period
    ["2026-11-09", "2026-11-31"],  # Impossible date
])
def test_inconsistent_npm_range_is_unavailable(monkeypatch, days):
    monkeypatch.setattr(downloads, "_read_json", lambda _url: {
        "start": "2026-11-09", "end": "2026-11-10",
        "downloads": [{"day": day, "downloads": 3} for day in days],
    })
    assert downloads.npm_downloads("trajviz-obs-opencode", "last-week") == {
        "status": "unavailable", "reason": "source_error",
    }


def test_unpublished_sources_do_not_report_zero(monkeypatch):
    monkeypatch.setattr(downloads, "_read_json", lambda _url: [])
    assert downloads.github_downloads("rshu/TrajViz-Obs") == {
        "status": "unavailable", "reason": "no_uploaded_release_assets",
    }

    def missing(url):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(downloads, "_read_json", missing)
    assert downloads.npm_downloads("trajviz-obs-opencode", "last-month") == {
        "status": "unavailable", "reason": "package_unavailable",
    }


@pytest.mark.parametrize("repo", ["../x", "x/..", "x/y?token=a"])
def test_bad_repo_name_is_rejected(repo):
    with pytest.raises(ValueError):
        downloads.github_downloads(repo)
    with pytest.raises(ValueError):
        downloads.github_clones(repo)


def test_github_traffic_uses_its_own_unique_cloner_count(monkeypatch):
    def run(command, **kwargs):
        assert command == ["gh", "api", "--hostname", "github.com", "repos/rshu/TrajViz-Obs/traffic/clones?per=day"]
        assert kwargs == {"check": True, "capture_output": True, "timeout": 15}
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({
            "count": 8, "uniques": 2, "clones": [
                {"timestamp": "2026-11-10T00:00:00Z", "count": 3, "uniques": 2},
                {"timestamp": "2026-11-09T00:00:00Z", "count": 5, "uniques": 2},
            ],
        }).encode())

    monkeypatch.setattr(downloads.subprocess, "run", run)
    assert downloads.github_clones("rshu/TrajViz-Obs") == {
        "status": "available", "scope": "repository_full_clones", "window_days": 14,
        "clones": 8, "unique_cloners": 2, "daily": [
            {"day": "2026-11-09", "clones": 5, "unique_cloners": 2},
            {"day": "2026-11-10", "clones": 3, "unique_cloners": 2},
        ],
    }


@pytest.mark.parametrize(("error", "reason"), [
    (FileNotFoundError(), "github_cli_unavailable"),
    (subprocess.CalledProcessError(1, "gh"), "traffic_unavailable_check_repository_access"),
    (subprocess.TimeoutExpired("gh", 15), "source_error"),
])
def test_unavailable_traffic_has_no_synthetic_counts(monkeypatch, error, reason):
    def run(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(downloads.subprocess, "run", run)
    assert downloads.github_clones("rshu/TrajViz-Obs") == {"status": "unavailable", "reason": reason}


def test_empty_traffic_is_a_measured_zero(monkeypatch):
    monkeypatch.setattr(downloads.subprocess, "run", lambda *_a, **_kw: subprocess.CompletedProcess(
        [], 0, stdout=b'{"count": 0, "uniques": 0, "clones": []}',
    ))
    traffic = downloads.github_clones("rshu/TrajViz-Obs")
    assert traffic["status"] == "available"
    assert traffic["clones"] == traffic["unique_cloners"] == 0


def test_inconsistent_traffic_is_unavailable(monkeypatch):
    monkeypatch.setattr(downloads.subprocess, "run", lambda *_a, **_kw: subprocess.CompletedProcess(
        [], 0, stdout=b'{"count": 1, "uniques": 2, "clones": []}',
    ))
    assert downloads.github_clones("rshu/TrajViz-Obs") == {"status": "unavailable", "reason": "source_error"}


@pytest.mark.parametrize("values", [
    ["2026-02-29", "2026-03-03", "12"], ["2026-11-10", "2026-11-09", "5"],
    ["2026-08-01", "2026-11-11", "5"], ["2026-11-09", "2026-11-10", "-1"],
])
def test_invalid_directory_aggregate(values):
    with pytest.raises(ValueError):
        downloads.claude_directory_installs(values)


def test_cli_keeps_install_accounts_and_distribution_counters_separate(monkeypatch, capsys):
    monkeypatch.setattr(downloads, "github_downloads", lambda _repo: {"status": "available", "downloads": 4})
    monkeypatch.setattr(downloads, "github_clones", lambda _repo: {"status": "available", "clones": 15})
    monkeypatch.setattr(downloads, "npm_downloads", lambda _package, _period: {"status": "available", "downloads": 7})
    monkeypatch.setattr("sys.argv", [
        "downloads", "--github-traffic", "--claude-directory-installs", "2026-11-09", "2026-11-10", "0",
    ])
    downloads.main()
    report = json.loads(capsys.readouterr().out)
    assert report["github_release_assets"]["downloads"] == 4
    assert report["npm"]["downloads"] == 7
    assert report["github_repository_traffic"]["clones"] == 15
    assert report["native_installs"]["claude_directory"] == {
        "status": "available", "scope": "published_claude_directory_install_accounts",
        "source": "operator_supplied_claude_directory_usage",
        "start": "2026-11-09", "end": "2026-11-10", "installed_accounts": 0,
    }
    assert report["native_installs"]["claude_git_marketplace"]["status"] == "unavailable"
    assert report["native_installs"]["opencode"]["status"] == "unavailable"
