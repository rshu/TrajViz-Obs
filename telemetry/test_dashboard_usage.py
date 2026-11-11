"""Run the real dashboard bridge and its private UI callbacks against the collector."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from queue import Queue
from threading import Thread
from time import monotonic, sleep
from urllib.request import ProxyHandler, Request, build_opener
from wsgiref.simple_server import make_server

import pytest

from scripts.e2e_fixtures import native_run

from .__main__ import _QuietHandler
from .collector import create_production_app, utc_day
from .credentials import issue_credential
from .report import report


@pytest.mark.parametrize(("kind", "adapter", "enabled"), [
    ("dashboard", "claude-code", True),
    ("converge", "opencode", True),
    ("dashboard", "opencode", False),
])
def test_dashboard_usage_callbacks(tmp_path, client_bridge, kind, adapter, enabled):
    source = native_run(tmp_path / "private-test-path.jsonl", "aaaaaaaa-0000-4000-8000-000000000001")
    database = tmp_path / "usage.sqlite3"
    queue_path = tmp_path / "sender"
    opener = build_opener(ProxyHandler({}))
    registry = tmp_path / "credentials.json"
    token = issue_credential(registry, "dashboard-test")
    app = create_production_app(database, credentials=registry)
    with make_server("127.0.0.1", 0, app, handler_class=_QuietHandler) as collector:
        Thread(target=collector.serve_forever, daemon=True).start()
        try:
            env = {**os.environ, "GRADIO_ANALYTICS_ENABLED": "false",
                   "TRAJVIZ_OBS_TELEMETRY": "1" if enabled else "0",
                   "TRAJVIZ_OBS_TELEMETRY_URL": f"http://127.0.0.1:{collector.server_port}/v1/events",
                   "TRAJVIZ_OBS_TELEMETRY_TOKEN": token,
                   "TRAJVIZ_OBS_TELEMETRY_STATE_DIR": str(queue_path), "GRADIO_TEMP_DIR": str(tmp_path / "gradio")}
            args = ["--transcript", str(source)] if kind == "dashboard" else ["--data-root", str(tmp_path)]
            command = [sys.executable, "-I", str(client_bridge / "trajviz_obs_cli.py"), kind, *args,
                       "--exit-on-stdin", "--usage-adapter", adapter, "--usage-version", "0.5.4"]
            with (tmp_path / "dashboard.log").open("w+") as log, subprocess.Popen(
                command, cwd=tmp_path, env=env, text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
            ) as child:
                ready: Queue[str] = Queue()
                Thread(target=lambda: ready.put(child.stdout.readline()), daemon=True).start()
                try:
                    line = ready.get(timeout=30)
                    log.seek(0)
                    assert line, log.read()
                    url = json.loads(line)["url"]
                    with opener.open(f"{url}config", timeout=10) as response:
                        configuration = response.read().decode()
                    assert token not in configuration
                    config = json.loads(configuration)
                    callbacks = {item["api_name"]: item for item in config["dependencies"]
                                 if str(item["api_name"]).startswith("obs_usage_")}
                    assert not report(database, today=utc_day(), days=1)["features"]
                    if enabled:
                        for action in ("render", "tab", "compare"):
                            name = f"obs_usage_{action}"
                            callback = callbacks[name]
                            assert callback["inputs"] == [] and callback["api_visibility"] == "private"
                            data = json.dumps({"fn_index": callback["id"], "data": []}).encode()
                            request = Request(f"{url}gradio_api/run/{name}", data=data,
                                              headers={"Content-Type": "application/json"})
                            with opener.open(request, timeout=10) as response:
                                assert json.load(response)["data"] == []
                        deadline = monotonic() + 6
                        while True:
                            stats = report(database, today=utc_day(), days=1)
                            if len(stats["features"]) >= 3 or monotonic() >= deadline:
                                break
                            sleep(0.05)
                        assert {entry["feature"] for entry in stats["features"]} == {
                            f"{kind}.render", f"{kind}.tab", f"{kind}.compare",
                        }
                        for entry in stats["features"]:
                            assert entry["adapter"] == adapter
                            assert entry["outcomes"] == {"observed": 1}
                            assert entry["executed"] == 0 and entry["p95_duration_bucket"] is None
                        assert stats["surfaces"] == [{"adapter": adapter, "surface": "browser",
                                                     "invocations": 3, "installations": 1}]
                    else:
                        assert not callbacks
                        assert not queue_path.exists()
                finally:
                    child.stdin.close()
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=10)
        finally:
            collector.shutdown()
