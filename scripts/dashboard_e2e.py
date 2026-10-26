#!/usr/bin/env python3
"""Exercise the real localhost dashboards through their Gradio HTTP APIs.

Uses only synthetic trajectories, reference data and a controlled localhost LLM. The Python client
uploads files and invokes native callbacks; the public REST API additionally preserves layout updates
and the exact server-side preload path. Gradio's browser executes event chains, so this harness calls
their native endpoints explicitly. Browser rendering, clicks and responsive layout remain CUA checks.

Run with the bridge's app/llm environment; optional --decaf-root enables real, offline AWE diagnosis.
Artifacts and server caches must stay outside the repository. No external model requests are needed.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import html
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from e2e_fixtures import native_run, prepare_comparison_inputs
from optional_matrix import prepare_decaf_reference, prepare_inputs, source_hashes

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "bridge/trajviz_obs_cli.py"
JUDGMENT = {"where": "CLAUDE.md", "fix": "Verify calculator assumptions before editing", "also": "", "confidence": "high"}
LABEL = {"phase": "understand", "action": "code_reading"}
SKIPPED = {"state", "row", "column", "tabs", "tab", "tabitem", "box", "form", "accordion", "group", "sidebar"}


def require(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def value_of(value):
    while isinstance(value, dict) and value.get("__type__") == "update" and "value" in value:
        value = value["value"]
    return value


def figure(value) -> dict:
    value = value_of(value)
    require(isinstance(value, dict) and value.get("type") == "plotly", "Native chart is not a Plotly output")
    chart = json.loads(value["plot"])
    require(isinstance(chart.get("data"), list) and isinstance(chart.get("layout"), dict), "Malformed native figure")
    return chart


def text_of(value) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", str(value_of(value))))


def table_rows(markup: str) -> list[list[str]]:
    return [[" ".join(text_of(cell).split()) for cell in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row, re.DOTALL)]
            for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", markup, re.DOTALL)]


def cards(markup: str) -> list[str]:
    return re.findall(r"id=['\"]wf-card-(\d+)['\"]", markup)


def add_context_drop(path: Path) -> None:
    """Give each agent one real usage drop, so the native snapshot selector has history."""
    records = [json.loads(line) for line in path.read_text().splitlines()]
    first = next(row for row in records if row.get("type") == "assistant")
    first["message"]["usage"].update(input_tokens=5_000, cache_read_input_tokens=145_000)
    path.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")


class Provider:
    """A deterministic HTTP provider; an unplanned request fails rather than going off-box."""

    def __init__(self) -> None:
        self.mode = "forbidden"
        self.calls: list[dict] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                pass

            def do_POST(self) -> None:
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.calls.append({"mode": owner.mode, "path": self.path, "body": request})
                if owner.mode == "label":
                    reply = json.dumps(LABEL)
                elif owner.mode == "fixes":
                    reply = json.dumps(JUDGMENT)
                elif owner.mode in ("analysis", "follow-up"):
                    reply = f"Synthetic {owner.mode}: inspect the failed pytest call before editing calculator.py."
                else:
                    self.send_error(500, "Unexpected model request")
                    return
                payload = json.dumps({"choices": [{"message": {"content": reply}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}/v1"

    @contextlib.contextmanager
    def answering(self, mode: str):
        before = len(self.calls)
        self.mode = mode
        try:
            yield
        finally:
            self.mode = "forbidden"
        require(len(self.calls) > before, f"{mode} did not reach the controlled HTTP provider")

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        require(not self.thread.is_alive(), "Provider did not stop")


class Dashboard:
    def __init__(self, python: str, kind: str, args: list[str], output: Path, env: dict, ledger: dict) -> None:
        from gradio_client import Client

        self.kind, self.output, self.ledger = kind, output, ledger
        self.log = (output / f"{kind}-stderr.log").open("w")
        self.process = subprocess.Popen([python, "-I", str(BRIDGE), kind, *args], cwd=output, env=env,
                                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=self.log,
                                        text=True, bufsize=1)
        ready: queue.Queue[str] = queue.Queue()
        self.reader = threading.Thread(target=lambda: ready.put(self.process.stdout.readline()), daemon=True)
        self.reader.start()
        try:
            line = ready.get(timeout=90)
            self.ready = json.loads(line)
            self.url = self.ready["url"]
            require(urlparse(self.url).hostname == "127.0.0.1", "Dashboard is not restricted to localhost")
            self.client = Client(self.url, verbose=False, download_files=False, analytics_enabled=False,
                                 httpx_kwargs={"timeout": 120, "trust_env": False})
            self.config = self.client.config
            self.components = {block["id"]: block for block in self.config["components"]}
            self.dependencies = {item["api_name"]: item for item in self.config["dependencies"] if item.get("backend_fn")}
            write_json(output / f"{kind}-config.json", self.config)
        except BaseException:
            self.close()
            raise

    def record(self, name: str, result: object) -> None:
        entry = {"dashboard": self.kind, "endpoint": name, "sha256": hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()}
        self.ledger["callbacks"].append(entry)
        write_json(self.output / "responses" / f"{len(self.ledger['callbacks']):02d}-{self.kind}-{name}.json", result)

    def call(self, name: str, *args):
        result = self.client.predict(*args, api_name="/" + name)
        self.record(name, result)
        return result

    def raw(self, name: str, *args) -> list:
        """Public REST queue protocol, retaining layout/state placeholders the Python client omits."""
        import httpx

        endpoint = self.url + "gradio_api/call/" + name
        with httpx.Client(timeout=120, trust_env=False) as http:
            response = http.post(endpoint, json={"data": list(args), "session_hash": self.client.session_hash})
            response.raise_for_status()
            event_id = response.json()["event_id"]
            with http.stream("GET", endpoint + "/" + event_id) as stream:
                stream.raise_for_status()
                event = ""
                for line in stream.iter_lines():
                    if line.startswith("event: "):
                        event = line[7:]
                    elif line.startswith("data: ") and event in ("complete", "error"):
                        result = json.loads(line[6:])
                        require(event == "complete", f"{name} failed through Gradio: {result}")
                        require(isinstance(result, list), f"{name} returned a malformed API result")
                        self.record(name, result)
                        return result
        raise AssertionError(f"{name} finished without a Gradio completion event")

    def mapped(self, name: str, *args, raw: bool = False) -> dict[int, object]:
        if raw:
            result, outputs = self.raw(name, *args), self.dependencies[name]["outputs"]
        else:
            result = self.call(name, *args)
            outputs = [key for key in self.dependencies[name]["outputs"]
                       if not self.components[key].get("skip_api", self.components[key]["type"] in SKIPPED)]
            result = [result] if len(outputs) == 1 else list(result)
        require(len(outputs) == len(result), f"{name} output count changed")
        return dict(zip(outputs, result, strict=True))

    def get_output(self, values: dict, *, label: str | None = None, elem_id: str | None = None):
        keys = [key for key in values if (label is None or self.components[key]["props"].get("label") == label)
                and (elem_id is None or self.components[key]["props"].get("elem_id") == elem_id)]
        require(len(keys) == 1, f"Cannot identify output {label or elem_id}: {keys}")
        return values[keys[0]]

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        self.reader.join(timeout=5)
        self.log.close()
        require(not self.reader.is_alive(), "Dashboard readiness reader did not stop")
        if hasattr(self, "client"):
            self.client.close()
        if hasattr(self, "url"):
            import httpx

            try:
                httpx.get(self.url, timeout=2, trust_env=False)
            except httpx.ConnectError:
                return
            raise AssertionError(f"{self.kind} server remained reachable after shutdown")


def bridge_call(python: str, output: Path, env: dict, *args: str) -> dict:
    result = subprocess.run([python, "-I", str(BRIDGE), *args], env=env, cwd=output,
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, check=False)
    require(result.returncode == 0, f"Bridge {args[0]} failed: {result.stdout}\n{result.stderr}")
    return json.loads(result.stdout)


def exercise_insight(app: Dashboard, fixtures: dict, labels: dict, provider: Provider, ledger: dict, decaf: dict | None) -> None:
    from gradio_client import handle_file

    empty_compare = app.call("on_run_comparison", None, None, "", None, None)
    require("Load a trajectory" in empty_compare[0] and not figure(empty_compare[1])["data"],
            "Pairwise comparison did not guard an empty Overview")
    # Exercise the real page-load endpoint then use its FileData unchanged, as the frontend does. Uploading
    # it again would turn it into a cache copy, lose sibling subagents and intentionally skip --window.
    preload = [item["api_name"] for item in app.dependencies.values() if not item["inputs"] and
               len(item["outputs"]) == 1 and app.components[item["outputs"][0]]["type"] == "file"]
    require(len(preload) == 1 and app.ready.get("labels_preloaded") is True, "Native source/labels preload is not registered")
    source = app.raw(preload[0])[0]
    require(Path(source["path"]).resolve() == fixtures["agent_run"].resolve(), "Page load returned the wrong trajectory")
    loaded = app.mapped("_do_load", source, "", raw=True)
    require(app.get_output(loaded, label="Window limit (tokens)") == 1_000_000, "Preloaded 1M context override was lost")
    details_markup = app.get_output(loaded, elem_id="wf-detail-store")
    encoded = re.search(r'data-b64="([^"]+)"', details_markup)
    require(encoded, "Workflow detail store is missing")
    details = json.loads(base64.b64decode(encoded[1]))
    require(len(details) == 11 and any("return a + b" in text_of(detail) for detail in details.values()), "Merged subagent steps or full edit detail were lost")
    workflow = next(value for value in loaded.values() if isinstance(value, str) and cards(value))
    require(len(cards(workflow)) == 11 and set(cards(workflow)) == set(details), "Workflow cards/detail indices do not match the 11 known steps")
    require(any("9 assistant, 2 user" in str(value) for value in loaded.values()), "Overview did not include the actual sibling subagent")
    plot_values = {app.components[key]["props"].get("label"): figure(value) for key, value in loaded.items()
                   if app.components[key]["type"] == "plot"}
    for name in ("Token Usage", "Tool Call Frequency", "Tool Outcome Timeline", "Agent Swimlane", "Token Breakdown by Agent", "Context Window Pressure", "File Interaction Timeline"):
        require(plot_values[name]["data"], f"Overview {name} has no data for a fixture that should populate it")
    require(len(plot_values) == 11, "Overview stopped returning its full native chart set")
    full_json = json.loads(app.get_output(loaded, label="Full trajectory JSON"))
    require(full_json.get("metadata", {}).get("session_id") == fixtures["session_id"], "Raw Data belongs to another source")
    ledger["checks"].extend(["exact-path page preload", "1M context override", "sibling subagent merge", "Overview native chart set", "Raw Data", "Workflow detail payload"])

    # The browser's native change/click handlers expose five label plots through the same API.
    reset = app.raw("_reset_labels", source)
    sidecar = value_of(reset[-1])
    require(Path(sidecar["path"]).resolve() == labels["agent"].resolve(), "Native label reset lost the preloaded sidecar")
    drawn = app.mapped("do_load_labels", sidecar, raw=True)
    charts = [figure(value) for key, value in drawn.items() if app.components[key]["type"] == "plot"]
    require(len(charts) == 5 and all(chart["data"] for chart in charts), "Not all five native label charts loaded")
    require(charts[0]["data"][0]["y"] == [9], "Phase counts do not cover all nine assistant labels")
    no_labels = app.call("do_load_labels", None)
    require("Upload a" in no_labels[1] and all(not figure(chart)["data"] for chart in no_labels[2:]),
            "Removing a sidecar retained old label charts")
    invalid_labels = app.output / "invalid-labels.json"
    write_json(invalid_labels, {})
    rejected_labels = app.call("do_load_labels_1", handle_file(str(invalid_labels)))
    require("Error:" in rejected_labels[1] and all(not figure(chart)["data"] for chart in rejected_labels[2:]),
            "Invalid sidecar did not visibly clear old label charts")
    restored_labels = app.call("do_load_labels", handle_file(str(labels["agent"])))
    require(all(figure(chart)["data"] for chart in restored_labels[2:]), "Labels did not recover after an invalid upload")
    for section in ("Summary", "Tools", "Agents", "Context Utilization", "Diagnostics", "Deep Dive", "Labels"):
        updates = app.raw("show_overview_section", section)
        require(len(updates) == 7 and sum(update.get("visible") is True for update in updates) == 1,
                f"Overview {section} does not show exactly one section")
    ledger["checks"].extend(["labels preload and five charts", "missing/invalid label sidecar and recovery", "all seven Overview sections"])

    agents = app.get_output(loaded, label="Agent")["choices"]
    agent_keys = [key for _label, key in agents if key != "__all__"]
    require(len(agent_keys) == 2, "Context agent selector omitted the subagent")
    for key in agent_keys:
        pressure, strip, snapshots = app.call("on_agent_change", key, 1_000_000)
        require(figure(pressure)["data"] and "1M" in text_of(strip), "Agent-specific context ignored its window")
        choices = snapshots["choices"]
        historic = [choice[1] for choice in choices if choice[1] != "current"]
        require(historic, "Agent context has no historical snapshots")
        older, old_strip = app.call("_rebuild_utilization", key, 2_000_000, historic[0])
        require(figure(older)["data"] and "2M" in text_of(old_strip), "Context snapshot/window controls did not rebuild")
    ledger["checks"].append("context agents, snapshots and changed 2M window")

    toc = app.get_output(loaded, elem_id="wf-toc-container")
    for filters, query, count in (("Assistant,All", "", 10), ("User,All", "", 1),
                                  ("Assistant,Tool Calls", "", 7), ("Assistant,Errors", "", 1),
                                  ("Assistant,All", "pytest", 3), ("Assistant,All", "no-such-needle", 0),
                                  ("All", "", 0)):
        rendered, counter, updated_toc = app.call("do_filter_workflow", filters, query, toc)
        require(len(cards(rendered)) == count and f"Showing {count} of 11 steps" in counter,
                f"Workflow filtering failed: {filters}/{query}")
        require(set(re.findall(r"wf-card-(\d+)", updated_toc)) == set(cards(rendered)), "Filtered step index does not match cards")
    chips = app.get_output(loaded, elem_id="wf-filter-chips")
    agent_tokens = re.findall(r"data-filter=['\"]([^'\"]+)['\"]\s+data-filter-group=['\"]agent['\"]", chips)
    specific = [token for token in agent_tokens if token != "agent:All"]
    require(len(specific) == 2, "Workflow agent chips were not generated")
    filtered_agents = []
    for token in specific:
        filtered, _count, _toc = app.call("do_filter_workflow_1", "Assistant,User,All," + token, "", toc)
        filtered_agents.append(set(cards(filtered)))
    # The worker task prompt belongs to its dispatching parent in the native timeline.
    require(sorted(map(len, filtered_agents)) == [4, 7] and not filtered_agents[0].intersection(filtered_agents[1])
            and filtered_agents[0] | filtered_agents[1] == set(details), "Agent chips did not partition the known parent/worker steps")
    hidden = app.call("on_toc_toggle", toc)
    require("toc-hidden" in hidden and "toc-hidden" not in app.call("on_toc_toggle", hidden), "Step index toggle did not round trip")
    ledger["checks"].extend(["Workflow role/feature/agent filters", "Workflow search and empty results", "Workflow step index toggle"])

    initial_calls = len(provider.calls)
    history, status = app.call("on_trajectory_loaded")
    require(history == [] and "ANALYZE" in status and len(provider.calls) == initial_calls, "Closed AI sidebar triggered a model request")
    autoload = app.call("on_diagnose_autoload", decaf["reference_root"] if decaf else str(app.output / "missing-reference"))
    require("No gold-grounded attribution" in autoload[1] and autoload[2:] == ("", ""),
            "Native attribution autoload did not clear overrides or explain its unavailable verdict")
    if decaf:
        ledger["native_claude_attribution"] = text_of(autoload[0])
    with provider.answering("analysis"):
        history, status = app.call("on_sidebar_expand", [])
    require("Synthetic analysis" in json.dumps(history) and "ANALYZE" in status, "AI sidebar did not show provider analysis")
    count = len(provider.calls)
    repeated, _ = app.call("on_sidebar_expand", history)
    require(repeated == history and len(provider.calls) == count, "Reopening AI sidebar repeated analysis")
    with provider.answering("follow-up"):
        history, cleared_question = app.call("on_analysis_ask", "What failed first?", history)
    require("Synthetic follow-up" in json.dumps(history) and cleared_question == "", "AI follow-up did not update the chat")
    require("Synthetic analysis" in json.dumps(provider.calls[-1]["body"]), "AI follow-up forgot prior analysis")
    app.call("on_sidebar_collapse")
    clear = [item["api_name"] for item in app.dependencies.values() if not item["inputs"] and
             len(item["outputs"]) == 1 and app.components[item["outputs"][0]]["type"] == "chatbot"]
    require(len(clear) == 1 and app.call(clear[0]) == [], "Clear chat did not clear analysis history")
    with provider.answering("fixes"):
        job = app.client.submit(api_name="/on_suggest_fixes")
        judged = job.result(timeout=120)
    app.record("on_suggest_fixes", judged)
    require(JUDGMENT["fix"] in text_of(judged) and len(job.outputs()) >= 2, "Fix suggestion generator did not stream and apply a judgment")
    ledger["checks"].extend(["closed AI sidebar privacy", "AI first analysis", "AI follow-up history", "AI reopen and clear chat", "streamed AI issue fixes"])

    # Additional real uploads must retain upstream defaults and not inherit the selected run's labels.
    switched = app.mapped("_do_load", handle_file(str(fixtures["before"])), "")
    require(app.get_output(switched, label="Window limit (tokens)") == 200_000, "An unrelated upload inherited the selected 1M window")
    cleared = app.call("_reset_labels", handle_file(str(fixtures["before"])))
    require(value_of(cleared[-1]) is None, "An unrelated upload inherited another session's labels")
    reset_comparison = app.call("_reset_comparison")
    require("Comparison complete" not in str(reset_comparison) and value_of(reset_comparison[2]).get("type") == "plotly",
            "Changing source did not clear native comparison results")
    group, timeline, behavior = app.call("on_run_group_scorecard", [handle_file(str(fixtures["after"])), handle_file(str(fixtures["reference"]))], "")
    rows = table_rows(group)
    require(any(row[:4] == ["before (baseline)", "claude_code", "✓", "6"] and row[6:8] == ["4", "75%"] for row in rows),
            "Run group baseline metrics do not match the independently known failing run")
    require(sum(row[3:4] == ["5"] and row[6:8] == ["3", "100%"] for row in rows) == 2, "Run group omitted or miscomputed successful runs")
    require(figure(timeline)["data"] and all(part in text_of(behavior) for part in ("Tool coverage", "Skill coverage", "Waste patterns", "calculator.py")),
            "Run group timeline or behavior matrices are missing")
    compared = app.call("on_run_comparison", handle_file(str(fixtures["reference"])), handle_file(str(fixtures["anchor"])), "",
                        handle_file(str(labels["reference"])), handle_file(str(labels["before"])))
    report, phase_count, phase_duration, status = compared
    require("Comparison complete" in status and ["Steps", "5", "6"] in table_rows(report), "Insight pairwise report has the wrong sides or counts")
    phase = figure(phase_count)["data"]
    require(len(phase) == 2 and sorted(trace["y"][0] for trace in phase) == [4, 5] and figure(phase_duration)["data"],
            "Pairwise phase charts did not use labels from both runs")
    ledger["checks"].extend(["upload state isolation", "N-run scorecard exact metrics", "N-run behavioral matrices", "pairwise report and both label sidecars"])

    exported = app.call("prepare_html_export")
    file = value_of(exported)
    require(exported.get("interactive") is True and file.get("url"), "Native HTML export did not produce a download")
    import httpx

    response = httpx.get(file["url"], timeout=30, trust_env=False)
    response.raise_for_status()
    require("<!doctype html" in response.text.lower() and "calculator.py" in response.text and "plotly" in response.text.lower(), "Downloaded HTML export is not a populated report")
    (app.output / "before-export.html").write_bytes(response.content)
    ledger["checks"].append("downloadable populated HTML export")

    if decaf:
        app.call("_do_load", handle_file(decaf["transcript"]), "")
        result, status = app.call("on_diagnose", decaf["agent"], decaf["instance"], decaf["reference_root"])
        require("Diagnosis complete" in status and "missed_relevant_file" in result and "deductive" in result,
                "Dashboard did not render real objective DECAF attribution")
        ledger["checks"].append("real objective DECAF dashboard diagnosis (canonical OpenCode)")
    else:
        result, status = app.call("on_diagnose", "claude_code", "synthetic", str(app.output / "missing-reference"))
        require("No gold-grounded attribution" in status and "unavailable" in text_of(result).lower(), "Attribution did not visibly explain missing optional setup")
        ledger["checks"].append("honest missing optional DECAF setup")

    # Failed loads must disarm the previous report and not leave a stale comparison/Workflow behind.
    failed = app.mapped("_do_load", None, "")
    require(value_of(app.get_output(failed, label="Export HTML")) is None and app.get_output(failed, elem_id="wf-detail-store") == "",
            "Clearing trajectory retained an export or stale Workflow detail")
    ledger["checks"].append("clearing source removes previous result state")


def exercise_converge(app: Dashboard, fixtures: dict, ledger: dict) -> None:
    from gradio_client import handle_file

    tabs = [block["props"].get("label") for block in app.components.values() if block["type"] == "tabitem"]
    require(tabs == ["Pairwise", "Batch", "Before/After"], "Converge navigation lost a native mode")
    missing = app.call("compare_uploads", None, None, None, 50, False)
    require("Please upload both" in missing[0] and all(not figure(chart)["data"] for chart in missing[1:]), "Missing Converge inputs do not clear native charts")
    report, milestones, segments, divergences, anchor = app.call("compare_uploads", handle_file(str(fixtures["reference"])),
                                                               handle_file(str(fixtures["before"])), handle_file(str(fixtures["anchor"])), 50, False)
    rows = table_rows(report)
    require(["Steps", "5", "6"] in rows and ["Total Tokens", "60", "75"] in rows and ["Total Tool Calls", "3", "4"] in rows,
            "Converge pairwise outcomes do not match the fixture")
    require(all(figure(chart)["data"] for chart in (milestones, segments, divergences, anchor)) and anchor.get("visible") is True,
            "Converge milestones, costs, divergences or anchor chart are missing")
    batch = app.call("do_batch", handle_file(str(fixtures["before_manifest"])), 50, False)
    require("2 of 2 tasks compared" in batch and "Aggregate Metrics" in batch and "Pattern Frequency" in batch, "Converge batch did not aggregate both tasks")
    intervention = app.call("do_before_after", handle_file(str(fixtures["before_manifest"])), handle_file(str(fixtures["after_manifest"])), 50, True)
    rows = table_rows(intervention)
    require("Paired tasks: 2" in intervention and any(row[0] == "alignment_f1" and row[1:3] == ["0.8571", "1.0000"] and row[-1] == "improved" for row in rows), "Before/after did not recognize the known fuzzy-aligned improvement")
    require("Recommendation" in intervention and "Metric Deltas" in intervention, "Intervention recommendation or metrics are missing")
    outside = app.output / "outside-manifest.json"
    write_json(outside, [{"task_id": "escape", "reference": "../outside.json", "compared": "after.jsonl"}])
    refused = app.call("do_batch", handle_file(str(outside)), 50, False)
    require("outside" in text_of(refused).lower() and "Batch failed" in text_of(refused), "Batch manifest escaped its configured data root")
    require(str(app.output) not in refused, "Converge error exposed the server cache path")
    ledger["checks"].extend(["Converge all three modes", "Converge missing inputs", "Converge exact pairwise outcome metrics", "Converge milestone/cost/divergence/anchor plots", "Converge batch aggregation", "Converge fuzzy before/after improvement", "Converge data-root traversal refusal"])


def run(python: str, output: Path, *, decaf_root: Path | None = None) -> dict:
    fixtures = prepare_comparison_inputs(output / "fixtures")
    sid = "ddd44444-0000-4000-8000-000000000004"
    fixtures.update(session_id=sid, agent_run=native_run(output / "fixtures" / f"{sid}.jsonl", sid, regression=True))
    child = native_run(output / "fixtures" / sid / "subagents/agent-worker-a.jsonl", sid, agent="worker-a")
    add_context_drop(fixtures["agent_run"])
    add_context_drop(child)
    hashes = source_hashes(output / "fixtures")
    ledger: dict = {"status": "running", "callbacks": [], "checks": [], "attribution": "real objective AWE" if decaf_root else "missing setup only"}
    provider = Provider()
    env = {key: val for key, val in os.environ.items() if not key.startswith(("ANALYZE_", "LABEL_", "AWE_", "TRAJVIZ_REFERENCE_ROOT"))}
    env.update(GRADIO_ANALYTICS_ENABLED="False", HF_HUB_DISABLE_TELEMETRY="1", GRADIO_TEMP_DIR=str(output / "gradio-cache"),
               XDG_CACHE_HOME=str(output / "cache"), CLAUDE_CONFIG_DIR=str(output / "config"),
               NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost", OPENROUTER_API_KEY="")
    for prefix in ("ANALYZE", "LABEL"):
        env.update({f"{prefix}_BASE_URL": provider.base, f"{prefix}_API_KEY": "synthetic-key",
                    f"{prefix}_MODEL": "local-" + prefix.lower(), f"{prefix}_PROVIDER": "openai"})
    decaf = None
    labels = {}
    try:
        if decaf_root:
            optional = output / "optional"
            inputs = prepare_inputs(optional)
            collected = bridge_call(python, output, {**env, **inputs["environment"]}, "collect", "--", "opencode", "ses_root", str(optional / "exports/opencode.json"))
            require(collected["exit"] == 0, "Could not prepare actual canonical OpenCode attribution input")
            decaf = prepare_decaf_reference(optional)
            env.update(AWE_DECAF_PATH=str(decaf_root), TRAJVIZ_REFERENCE_ROOT=decaf["reference_root"],
                       AWE_ARGUS_ROOT=decaf["reference_root"], AWE_PROJECT_DATA=str(output / "awe-cache"), AWE_OUTPUT_DATA=str(output / "awe-output"))
        for name, source in (("before", fixtures["before"]), ("reference", fixtures["reference"]), ("agent", fixtures["agent_run"])):
            labels[name] = output / "labels" / f"{name}-labels.json"
            labels[name].parent.mkdir(exist_ok=True)
            with provider.answering("label"):
                bridge_call(python, output, env, "label", "--transcript", str(source), "--max-steps", "30", "-o", str(labels[name]))
            sidecar = json.loads(labels[name].read_text())
            expected = {"before": 5, "reference": 4, "agent": 9}[name]
            require(sum(step.get("label_source") == "llm" for step in sidecar["steps"]) == expected, "Fixture labeling did not classify every assistant step")
        insight = Dashboard(python, "dashboard", ["--transcript", str(fixtures["agent_run"]), "--labels", str(labels["agent"]), "--window", "1000000"], output, env, ledger)
        try:
            exercise_insight(insight, fixtures, labels, provider, ledger, decaf)
        finally:
            insight.close()
        converge = Dashboard(python, "converge", ["--data-root", str(output / "fixtures")], output, env, ledger)
        try:
            exercise_converge(converge, fixtures, ledger)
        finally:
            converge.close()
        require(source_hashes(output / "fixtures") == hashes, "Dashboard changed its source fixtures")
        require(all(call["mode"] in {"label", "analysis", "follow-up", "fixes"} and
                    call["path"] == "/v1/chat/completions" and call["body"]["model"].startswith("local-")
                    for call in provider.calls), "An unplanned model request reached the provider")
        ledger.update(status="passed", http_model_requests=len(provider.calls), source_files_unchanged=True)
        return ledger
    except BaseException as exc:
        ledger.update(status="failed", error=f"{type(exc).__name__}: {exc}"[:2000])
        raise
    finally:
        provider.close()
        write_json(output / "provider-requests.json", provider.calls)
        write_json(output / "dashboard-e2e.json", ledger)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--decaf-root", type=Path)
    args = parser.parse_args()
    if args.decaf_root and not (args.decaf_root / "awe/__init__.py").is_file():
        parser.error("--decaf-root must contain the real awe package")
    output = args.output or Path(tempfile.mkdtemp(prefix="trajviz-obs-dashboard-e2e-"))
    if output.resolve().is_relative_to(ROOT):
        parser.error("--output must be outside the repository")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        parser.error("--output must be an empty directory")
    output.mkdir(parents=True, exist_ok=True)
    result = run(args.python, output, decaf_root=args.decaf_root)
    print(json.dumps({"output": str(output), "status": result["status"],
                      "callbacks": len(result["callbacks"]), "http_model_requests": result["http_model_requests"],
                      "attribution": result["attribution"], "passed_checks": len(result["checks"]),
                      "checks": result["checks"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
