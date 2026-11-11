"""Report distribution counters and native installation figures by source."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import urllib.error
import urllib.request
from datetime import date, timedelta
from urllib.parse import quote

_REPO = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*/[a-zA-Z0-9][a-zA-Z0-9_.-]*")
_PACKAGE = re.compile(r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*")
_MAX_RESPONSE = 4 * 1024 * 1024
_RELEASE_PAGE_SIZE = 100
_MAX_RELEASE_PAGES = 10


def _read_json(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": "TrajViz-Obs distribution report",
                                                   "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        data = response.read(_MAX_RESPONSE + 1)
    if len(data) > _MAX_RESPONSE:
        raise ValueError("response too large")
    return json.loads(data)


def _count(value) -> bool:
    return type(value) is int and value >= 0


def _day(value: str) -> date:
    day = date.fromisoformat(value)
    if value != day.isoformat():
        raise ValueError("expected a UTC date in YYYY-MM-DD format")
    return day


def github_downloads(repo: str) -> dict:
    """GitHub counts downloads of uploaded release assets."""
    if not _REPO.fullmatch(repo):
        raise ValueError("expected a GitHub repository in owner/name form")
    assets = []
    try:
        for page in range(1, _MAX_RELEASE_PAGES + 1):
            releases = _read_json(f"https://api.github.com/repos/{repo}/releases?per_page={_RELEASE_PAGE_SIZE}&page={page}")
            if not isinstance(releases, list):
                raise TypeError("invalid release response")
            for release in releases:
                for asset in release["assets"]:
                    if asset.get("state") != "uploaded":
                        continue
                    count = asset["download_count"]
                    if not _count(count):
                        raise ValueError("invalid release asset count")
                    assets.append({"release": release["tag_name"], "asset": asset["name"], "downloads": count})
            if len(releases) < _RELEASE_PAGE_SIZE:
                break
        else:
            return {"status": "unavailable", "reason": "release_listing_exceeds_limit"}
    except urllib.error.HTTPError as exc:
        return {"status": "unavailable", "reason": "repository_unavailable" if exc.code == 404 else "source_error"}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {"status": "unavailable", "reason": "source_error"}
    if not assets:
        return {"status": "unavailable", "reason": "no_uploaded_release_assets"}
    return {"status": "available", "scope": "lifetime_downloads_of_current_release_assets",
            "downloads": sum(asset["downloads"] for asset in assets), "assets": assets}


def github_clones(repo: str) -> dict:
    """Read GitHub's 14-day repository traffic using the operator's GitHub CLI."""
    if not _REPO.fullmatch(repo):
        raise ValueError("expected a GitHub repository in owner/name form")
    try:
        result = subprocess.run(["gh", "api", "--hostname", "github.com", f"repos/{repo}/traffic/clones?per=day"],
                                check=True, capture_output=True, timeout=15)
        data = json.loads(result.stdout)
        clones, unique, rows = data["count"], data["uniques"], data["clones"]
        if not _count(clones) or not _count(unique) or unique > clones or not isinstance(rows, list) or len(rows) > 14:
            raise ValueError("invalid repository traffic response")
        daily = []
        for row in rows:
            timestamp = row["timestamp"]
            day, suffix = timestamp.split("T", 1)
            count, distinct = row["count"], row["uniques"]
            if (suffix != "00:00:00Z" or not _count(count) or not _count(distinct)
                    or distinct > count or count > clones):
                raise ValueError("invalid daily repository traffic response")
            daily.append({"day": _day(day).isoformat(), "clones": count, "unique_cloners": distinct})
        daily.sort(key=lambda row: row["day"])
        if len({row["day"] for row in daily}) != len(daily):
            raise ValueError("duplicate repository traffic day")
    except FileNotFoundError:
        return {"status": "unavailable", "reason": "github_cli_unavailable"}
    except subprocess.CalledProcessError:
        return {"status": "unavailable", "reason": "traffic_unavailable_check_repository_access"}
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, KeyError, AttributeError):
        return {"status": "unavailable", "reason": "source_error"}
    return {"status": "available", "scope": "repository_full_clones", "window_days": 14,
            "clones": clones, "unique_cloners": unique, "daily": daily}


def npm_downloads(package: str, period: str) -> dict:
    """Read daily package downloads for one standard registry reporting period."""
    if not _PACKAGE.fullmatch(package) or len(package) > 214:
        raise ValueError("expected an npm package name")
    if period not in {"last-day", "last-week", "last-month"}:
        raise ValueError("invalid npm reporting period")
    try:
        data = _read_json(f"https://api.npmjs.org/downloads/range/{period}/{quote(package, safe='')}")
        start, end = _day(data["start"]), _day(data["end"])
        rows = data["downloads"]
        if start > end or not isinstance(rows, list) or len(rows) != (end - start).days + 1:
            raise ValueError("invalid npm download range")
        counts = {}
        for row in rows:
            day, count = _day(row["day"]), row["downloads"]
            if not start <= day <= end or day in counts or not _count(count):
                raise ValueError("invalid daily npm download count")
            counts[day] = count
        days = [{"day": day.isoformat(), "downloads": counts[day]} for day in sorted(counts)]
        return {"status": "available", "scope": "npm_registry_downloads", "period": period,
                "start": start.isoformat(), "end": end.isoformat(),
                "downloads": sum(counts.values()), "daily": days}
    except urllib.error.HTTPError as exc:
        return {"status": "unavailable", "reason": "package_unavailable" if exc.code == 404 else "source_error"}
    except (OSError, ValueError, TypeError, KeyError):
        return {"status": "unavailable", "reason": "source_error"}


def claude_directory_installs(aggregate: list[str] | None) -> dict:
    """Label a date-bounded aggregate entered from a published listing's Usage tab."""
    scope = "published_claude_directory_install_accounts"
    if aggregate is None:
        return {"status": "unavailable", "scope": scope, "reason": "directory_portal_total_not_supplied"}
    start, end, count = aggregate
    first, last = _day(start), _day(end)
    if not timedelta(0) <= last - first < timedelta(days=90):
        raise ValueError("Claude directory report requires an inclusive period of 1 to 90 days")
    if not count.isascii() or not count.isdecimal():
        raise ValueError("Claude directory installs must be a nonnegative whole number")
    return {"status": "available", "scope": scope, "source": "operator_supplied_claude_directory_usage",
            "start": start, "end": end, "installed_accounts": int(count)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="rshu/TrajViz-Obs")
    parser.add_argument("--package", default="trajviz-obs-opencode")
    parser.add_argument("--period", default="last-month", choices=("last-day", "last-week", "last-month"))
    parser.add_argument("--github-traffic", action="store_true", help="Read 14-day repository clones via authenticated gh")
    parser.add_argument("--claude-directory-installs", nargs=3, metavar=("START", "END", "COUNT"),
                        help="Attach the install count from a live listing's Usage tab for an inclusive UTC date range")
    args = parser.parse_args()
    try:
        directory = claude_directory_installs(args.claude_directory_installs)
        github = github_downloads(args.repo)
        npm = npm_downloads(args.package, args.period)
        traffic = github_clones(args.repo) if args.github_traffic else {"status": "unavailable", "reason": "not_requested"}
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps({
        "github_release_assets": {"repository": args.repo, **github},
        "github_repository_traffic": {"repository": args.repo, **traffic},
        "npm": {"package": args.package, **npm},
        "native_installs": {
            "claude_directory": directory,
            "claude_git_marketplace": {"status": "unavailable", "reason": "no_publisher_install_counter"},
            "opencode": {"status": "unavailable", "reason": "no_publisher_install_counter"},
        },
    }, indent=2))


if __name__ == "__main__":
    main()
