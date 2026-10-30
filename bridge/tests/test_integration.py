"""Every subcommand end to end through ``cli.main`` on the vendored fixture (and a fake project folder).

The dashboards are left out (they serve until stdin closes, see test_dashboard.py), as are the env-mode LLM
commands, which call the configured endpoint over HTTP (see test_llm.py).
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import BBB3

from trajviz_obs import cli

#: Subcommands this test does not run, with the reason.
EXCLUDED = {
    "dashboard": "serves until stdin closes",
    "converge": "serves until stdin closes",
    "ask": "calls the ANALYZE_* endpoint",
    "fixes": "calls the ANALYZE_* endpoint",
    "label": "calls the LABEL_* endpoint",
}

LABEL_REPLY = json.dumps({"phase": "understand", "action": "code_reading"})


def _replies(plan: dict, text: str, dest: Path) -> str:
    dest.write_text(json.dumps({"hash": plan["hash"],
                                "replies": [{"index": p["index"], "text": text} for p in plan["prompts"]]}))
    return str(dest)


def test_every_subcommand_runs_on_fixture(run_cli, fixture_session, synthetic_session, fake_projects,
                                          tmp_artifacts, tmp_path, captured_sessions):
    ran: set[str] = set()

    def ok(argv: list[str]) -> dict:
        code, out = run_cli(argv)
        assert code == 0, (argv, out)
        assert "error" not in out, (argv, out)
        ran.add(argv[0])
        return out

    session = ["--transcript", str(fixture_session), "--window", "200000", "--artifacts", str(tmp_artifacts)]
    location = ["--cwd", fake_projects.cwd, "--config-dir", str(fake_projects.config)]

    assert ok(["resolve", "--session", "bbb", *location])["session_id"] == BBB3
    assert ok(["sessions", *location])["sessions"][0]["id"] == BBB3
    assert {row["id"] for row in ok(["capture-sessions", "--transcript", str(captured_sessions)])["sessions"]} == {"A", "B"}
    assert ok(["doctor"])["trajviz"]["version"].startswith("0.3")

    assert ok(["snapshot", *session])["context"]["window"] == 200000
    assert ok(["view", *session])["views"]
    assert Path(ok(["view", *session, "tool_usage"])["path"]).is_file()
    assert (Path(ok(["json", *session, "--view", "tool_usage"])["path"]) / "index.json").is_file()
    assert ok(["schemas", "--out", str(tmp_path / "schemas")])["files"]
    assert Path(ok(["bundle", "--transcript", str(fixture_session), "--artifacts", str(tmp_artifacts)])["path"]).is_file()
    assert ok(["brief", *session])["brief"]
    assert ok(["attribution", *session])["available"] is False

    plan = ok(["llm-prompt", *session, "--feature", "analyze"])
    answer = ok(["llm-apply", *session, "--feature", "analyze",
                 "--replies", _replies(plan, "The session is fine.", tmp_path / "analyze-replies.json")])
    assert "The session is fine." in json.dumps(answer)
    plan = ok(["llm-prompt", *session, "--feature", "label", "--max-steps", "3"])
    labelled = ok(["llm-apply", *session, "--feature", "label", "--max-steps", "3",
                   "--replies", _replies(plan, LABEL_REPLY, tmp_path / "label-replies.json")])
    assert ok(["labels", "--sidecar", labelled["path"]])

    assert ok(["compare", *session, "--other", synthetic_session])
    assert ok(["scorecard", *session, "--others", synthetic_session])
    converge = ok(["converge-cli", "--artifacts", str(tmp_artifacts), "--", str(fixture_session), synthetic_session])
    assert converge["exit"] == 0 and Path(converge["path"]).is_file()
    assert ok(["collect", "--", "--version"])["exit"] == 0

    assert ran | set(EXCLUDED) == set(cli.SUBCOMMANDS), "a subcommand is neither run here nor excluded"
