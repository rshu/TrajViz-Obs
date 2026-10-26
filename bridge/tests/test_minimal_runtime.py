"""Smoke every entrypoint with the managed runtime's required llm/charts packages, without Gradio."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[1]


def test_every_entrypoint_in_minimal_runtime(fixture_session, tmp_path):
    script = f"""
import importlib.abc
import sys
sys.path.insert(0, {str(BRIDGE)!r})
class MinimalRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] == 'gradio':
            raise ModuleNotFoundError('Optional package disabled: ' + fullname, name=fullname)
sys.meta_path.insert(0, MinimalRuntime())
from trajviz_obs.cli import main
sys.exit(main(sys.argv[1:]))
"""
    env = {key: value for key, value in os.environ.items() if not key.startswith(("ANALYZE_", "LABEL_"))}

    def run(argv: list[str], *, error: str | None = None) -> dict:
        proc = subprocess.run([sys.executable, "-I", "-c", script, *argv], cwd=tmp_path, env=env,
                              text=True, capture_output=True, timeout=30, check=False)
        assert proc.returncode == (1 if error else 0), (argv, proc.stdout, proc.stderr)
        result = json.loads(proc.stdout)
        if error:
            assert result["error"]["code"] == error, (argv, result)
        return result

    session = ["--transcript", str(fixture_session), "--artifacts", str(tmp_path / "artifacts")]
    run(["doctor", "--cwd", str(tmp_path)])
    run(["resolve", "--session", str(fixture_session)])
    run(["sessions", "--config-dir", str(tmp_path / "config")])
    run(["snapshot", *session])
    run(["view", *session])
    run(["view", *session, "health"])
    run(["json", *session, "--view", "health", "--with-trajectory"])
    run(["schemas", "--out", str(tmp_path / "schemas")])
    run(["bundle", *session])
    run(["brief", *session])
    run(["attribution", *session])
    run(["compare", *session, "--other", str(fixture_session)])
    run(["scorecard", *session, "--others", str(fixture_session)])
    run(["collect", "--", "--version"])
    run(["converge-cli", "--artifacts", str(tmp_path / "artifacts"), "--", str(fixture_session),
         str(fixture_session), "--output", "summary"])
    for feature, text in (("analyze", "A short analysis."),
                          ("fixes", '{"where":"CLAUDE.md","fix":"x","also":"","confidence":"high"}'),
                          ("label", '{"phase":"understand","action":"code_reading"}')):
        options = [*session, "--feature", feature, "--max-steps", "2"]
        plan = run(["llm-prompt", *options])
        replies = tmp_path / f"replies-{feature}.json"
        replies.write_text(json.dumps({"hash": plan["hash"], "replies": [
            {"index": prompt["index"], "text": text} for prompt in plan["prompts"]]}))
        result = run(["llm-apply", *options, "--replies", str(replies)])
        if feature == "label":
            run(["labels", "--sidecar", result["path"]])
    for feature in ("ask", "fixes", "label"):
        run([feature, *session], error="llm_not_configured")
    run(["dashboard", "--transcript", str(fixture_session)], error="missing_extra")
    run(["converge", "--data-root", str(tmp_path)], error="missing_extra")
