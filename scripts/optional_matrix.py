#!/usr/bin/env python3
"""Exercise optional collectors with isolated synthetic stores, never the user's agent data.

The real bridge dispatches the upstream CLIs; exported trajectories are reloaded and all views validated.
Artifacts and a native-command manifest stay in an explicitly empty output folder outside the repository.
DECAF verdict execution additionally requires its real external checkout/reference set; an unavailable
notice is recorded only as fallback coverage, never as a successful attribution diagnosis.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "bridge/trajviz_obs_cli.py"
CURSOR_PARENT = "aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb"
CURSOR_CHILD = "cccccccc-4444-5555-6666-dddddddddddd"

SQL_SCHEMA = """
CREATE TABLE session (
 id TEXT PRIMARY KEY, project_id TEXT, workspace_id TEXT, parent_id TEXT, slug TEXT, directory TEXT,
 title TEXT, version TEXT, summary_additions INTEGER, summary_deletions INTEGER, summary_files INTEGER,
 time_created INTEGER, time_updated INTEGER, time_compacting INTEGER, time_archived INTEGER,
 summary_diffs TEXT, revert TEXT, permission TEXT);
CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
 time_created INTEGER, time_updated INTEGER, data TEXT);
"""


def write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")


def source_hashes(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file()}


def make_database(path: Path) -> None:
    """An OpenCode-derived store with a child, precise clocks, provider usage and one failed tool."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(SQL_SCHEMA)
        for session, parent in (("ses_root", None), ("ses_child", "ses_root")):
            connection.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                session, "project", "workspace", parent, session, "/synthetic/workspace", session, "test",
                1, 0, 1, 1000, 2000, None, None, None, None, None))
        messages = [
            ("user", "ses_root", 1000, {"role": "user"}, [
                {"type": "text", "text": "Inspect the synthetic project and run its tests."}]),
            ("main", "ses_root", 1100, {"role": "assistant", "agent": "build", "tokens": {
                "total": 100, "input": 20, "output": 10, "reasoning": 0, "cache": {"read": 70, "write": 0}},
                "time": {"created": 1100, "completed": 1250}}, [
                {"type": "tool", "tool": "read", "callID": "read-call", "state": {
                    "status": "completed", "input": {"filePath": "/synthetic/workspace/app.py"},
                    "output": "def answer(): return 42", "time": {"start": 1100, "end": 1150}}},
                {"type": "tool", "tool": "task", "callID": "task-call", "state": {
                    "status": "completed", "input": {"subagent_type": "explore", "prompt": "Run tests"},
                    "output": "Child inspected tests", "metadata": {"sessionId": "ses_child"},
                    "time": {"start": 1150, "end": 1250}}},
            ]),
            ("child", "ses_child", 1300, {"role": "assistant", "agent": "explore", "tokens": {
                "total": 80, "input": 10, "output": 5, "reasoning": 0, "cache": {"read": 65, "write": 0}},
                "time": {"created": 1300, "completed": 1700}}, [
                {"type": "tool", "tool": "bash", "callID": "test-call", "state": {
                    "status": "error", "input": {"command": "pytest"}, "output": "1 failed",
                    "error": "exit 1", "time": {"start": 1310, "end": 1600}}},
            ]),
            ("final", "ses_root", 1800, {"role": "assistant", "agent": "build", "finish": "stop", "tokens": {
                "total": 60, "input": 15, "output": 5, "reasoning": 0, "cache": {"read": 40, "write": 0}},
                "time": {"created": 1800, "completed": 1950}}, [
                {"type": "text", "text": "The synthetic test failure is preserved."}]),
        ]
        for message, session, created, info, parts in messages:
            connection.execute("INSERT INTO message VALUES (?,?,?,?,?)",
                               (message, session, created, created + 500, json.dumps(info)))
            for index, part in enumerate(parts):
                connection.execute("INSERT INTO part VALUES (?,?,?,?,?,?)", (
                    f"{message}-part-{index}", message, session, created + index, created + index + 20,
                    json.dumps(part)))


def make_cursor(root: Path) -> dict[str, str]:
    projects = root / "projects"
    session = projects / "synthetic-workspace" / "agent-transcripts" / CURSOR_PARENT
    parent = session / f"{CURSOR_PARENT}.jsonl"
    child = session / "subagents" / f"{CURSOR_CHILD}.jsonl"
    write_jsonl(parent, [
        {"role": "user", "message": {"content": [{"type": "text", "text": "Inspect the project"}]}},
        {"role": "assistant", "message": {"content": [
            {"type": "text", "text": "Inspecting synthetic source"},
            {"type": "tool_use", "name": "Read", "input": {"path": "/synthetic/app.py"}},
            {"type": "tool_use", "name": "Task", "input": {"prompt": "Explore", "subagent_type": "explore"}},
        ]}}, {"type": "turn_ended", "status": "success"},
    ])
    write_jsonl(child, [
        {"role": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Grep", "input": {"path": "/synthetic", "pattern": "missing"}},
        ]}}, {"type": "turn_ended", "status": "success"},
    ])
    database = root / "state.vscdb"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")

        def put(key: str, value: dict) -> None:
            connection.execute("INSERT INTO cursorDiskKV VALUES (?,?)", (key, json.dumps(value)))

        put(f"composerData:{CURSOR_PARENT}", {
            "composerId": CURSOR_PARENT, "name": "Synthetic collector test", "createdAt": 1000,
            "lastUpdatedAt": 2000, "status": "completed", "subagentComposerIds": [CURSOR_CHILD],
            "modelConfig": {"modelName": "synthetic-model"},
            "promptTokenBreakdown": {"totalUsedTokens": 1200, "maxTokens": 256000},
            "fullConversationHeadersOnly": [{"bubbleId": key} for key in ("user", "assistant", "read", "task")],
        })
        put(f"composerData:{CURSOR_CHILD}", {
            "composerId": CURSOR_CHILD, "name": "Explore child", "createdAt": 1400, "lastUpdatedAt": 1800,
            "promptTokenBreakdown": {"totalUsedTokens": 500, "maxTokens": 256000},
            "fullConversationHeadersOnly": [{"bubbleId": "grep"}],
        })
        put(f"bubbleId:{CURSOR_PARENT}:user", {"type": 1, "text": "Inspect the project"})
        put(f"bubbleId:{CURSOR_PARENT}:assistant", {"type": 2, "startedAtMs": 1000, "completedAtMs": 1800})
        for session_id, key, name, arguments, result, status, start, end in [
            (CURSOR_PARENT, "read", "read_file_v2", {"path": "/synthetic/app.py"}, "synthetic source", "completed", 1100, 1150),
            (CURSOR_PARENT, "task", "task_v2", {"prompt": "Explore"}, "Child started", "completed", 1200, 1500),
            (CURSOR_CHILD, "grep", "ripgrep_raw_search", {"path": "/synthetic", "pattern": "missing"}, "No match", "error", 1500, 1750),
        ]:
            put(f"bubbleId:{session_id}:{key}", {"type": 2, "startedAtMs": start, "completedAtMs": end,
                "toolFormerData": {"toolCallId": f"call-{key}", "name": name, "status": status,
                                   "rawArgs": json.dumps(arguments), "result": result,
                                   **({"error": "exit 1"} if status == "error" else {})}})
    return {"projects": str(projects), "database": str(database), "transcript": str(parent)}


def prepare_inputs(output: Path) -> dict:
    inputs = output / "inputs"
    database = inputs / "agent stores" / "opencode.db"
    make_database(database)
    cursor = make_cursor(inputs / "cursor")
    legacy = inputs / "codearts legacy"
    write_json(legacy / "chat_baseInfo.json", {"sessionId": "legacy", "title": "Synthetic archive"})
    write_json(legacy / "messages_0.json", [{"role": "user", "content": "First shard"}])
    write_json(legacy / "messages_1.json", [{"role": "assistant", "content": "Second shard"}])
    exports = output / "exports"
    exports.mkdir(parents=True, exist_ok=True)
    commands = [
        f'/obs collect cursor {CURSOR_PARENT} "{exports / "cursor.json"}" --db "{cursor["database"]}" --projects-dir "{cursor["projects"]}"',
        f'/obs collect opencode ses_root "{exports / "opencode.json"}"',
        f'/obs collect codearts "{database}" --session-id ses_root -o "{exports / "codearts.json"}"',
        f'/obs collect codearts "{legacy}" -o "{exports / "codearts-legacy.json"}"',
    ]
    manifest = {"database": str(database), "cursor": cursor, "legacy": str(legacy),
                "environment": {"OPENCODE_DATABASE": str(database)}, "commands": commands}
    write_json(output / "native-commands.json", manifest)
    return manifest


def prepare_decaf_reference(output: Path) -> dict[str, str]:
    """A tiny gold-grounded failure, using the collector's actual OpenCode export as canonical input."""
    root = output / "decaf-reference"
    instance = "optional-case"
    source = root / "data/trajectory/opencode_glm" / f"{instance}.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes((output / "exports/opencode.json").read_bytes())
    write_json(root / "data/requirements" / f"{instance}.json", {
        "repo": "synthetic/optional-e2e", "base_commit": "0000000",
        "problem_statement": "Make answer return 42 and preserve the failing test as evidence.",
        "gold_patch": "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-def answer(): return 0\n+def answer(): return 42\n",
        "FAIL_TO_PASS": ["tests/test_app.py::test_answer"], "PASS_TO_PASS": [],
    })
    patch = root / "data/patch/opencode_glm" / f"{instance}.diff"
    patch.parent.mkdir(parents=True, exist_ok=True)
    patch.write_text("diff --git a/other.py b/other.py\n--- a/other.py\n+++ b/other.py\n@@ -1 +1 @@\n-value = 0\n+value = 1\n",
                     encoding="utf-8")
    write_json(root / "data/eval_opencode_glm.json", {instance: False})
    return {"transcript": str(source), "reference_root": str(root),
            "agent": "opencode_glm", "instance": instance}


def run_matrix(python: str, output: Path, *, decaf_root: Path | None = None) -> dict:
    from jsonschema import Draft202012Validator

    fixtures = prepare_inputs(output)
    ledger: dict = {"status": "running", "commands": [], "schema_validations": 0,
                    "attribution_verdict": "not_tested: external DECAF checkout/reference required"}
    env = {**os.environ, **fixtures["environment"], "XDG_CACHE_HOME": str(output / "cache"),
           "CLAUDE_CONFIG_DIR": str(output / "config")}
    before = source_hashes(output / "inputs")

    def call(command: str, *args: str) -> dict:
        result = subprocess.run([python, "-I", str(LAUNCHER), command, *args], env=env, cwd=output,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, check=False)
        if result.returncode:
            raise AssertionError(f"{command}: {result.stdout}\n{result.stderr}")
        value = json.loads(result.stdout)
        ledger["commands"].append({"command": command, "args": list(args), "exit": result.returncode,
                                   **({"upstream_exit": value["exit"]} if command == "collect" else {})})
        return value

    def collect(name: str, args: list[str], destination: Path) -> dict:
        result = call("collect", "--", name, *args)
        if result["exit"] != 0 or not destination.is_file():
            raise AssertionError(f"{name}: {result}")
        return json.loads(destination.read_text(encoding="utf-8"))

    exports = output / "exports"
    cursor = fixtures["cursor"]
    specifications = {
        "cursor": [CURSOR_PARENT, str(exports / "cursor.json"), "--db", cursor["database"], "--projects-dir", cursor["projects"]],
        "opencode": ["ses_root", str(exports / "opencode.json")],
        "codearts": [fixtures["database"], "--session-id", "ses_root", "-o", str(exports / "codearts.json")],
    }
    try:
        for name, args in specifications.items():
            destination = exports / f"{name}.json"
            collected = collect(name, args, destination)
            if name == "cursor":
                assert collected["statistics"]["subagent_sessions"] == 1
                assert collected["statistics"]["tool_parts_with_output"] == 3
            else:
                assert len(collected["messages"]) == 4
                assert {m["info"]["sessionID"] for m in collected["messages"]} == {"ses_root", "ses_child"}
                assert sum(m["info"].get("tokens", {}).get("total", 0) for m in collected["messages"]) == 240
            folder = exports / f"{name}-views"
            call("json", "--transcript", str(destination), "--out", str(folder), "--with-trajectory")
            call("schemas", "--out", str(output / "schemas"))
            index = json.loads((folder / "index.json").read_text(encoding="utf-8"))
            assert len(index["views"]) >= 35
            reloaded = folder / "reloaded"
            call("json", "--transcript", str(folder / "trajectory.json"), "--out", str(reloaded))
            for entry in index["views"]:
                vid = entry["id"]
                schema = json.loads((output / "schemas" / f"{vid}.schema.json").read_text(encoding="utf-8"))
                view = json.loads((folder / f"{vid}.json").read_text(encoding="utf-8"))
                Draft202012Validator(schema).validate(view)
                ledger["schema_validations"] += 1
                roundtrip = json.loads((reloaded / f"{vid}.json").read_text(encoding="utf-8"))
                assert roundtrip == view, f"{name}: {vid} changed after export/reload"
                Draft202012Validator(schema).validate(roundtrip)
                ledger["schema_validations"] += 1
            run = json.loads((folder / "trajectory.json").read_text(encoding="utf-8"))
            assert run["format"] == name
            calls = [tool for step in run["steps"] for tool in step.get("tool_calls", [])]
            assert len(calls) == 3 and sum(tool.get("status") == "error" for tool in calls) == 1
            assert all(tool.get("output") for tool in calls)
            assert all(tool.get("time_end") > tool.get("time_start") for tool in calls)
            if name == "cursor":
                assert run["tokens_estimated"] is True
                pressure = json.loads((folder / "context_pressure.json").read_text(encoding="utf-8"))["data"]
                assert pressure["stats"]["window_limit"] == 256000
                assert pressure["stats"]["peak_occupancy"] == 1200
            assert call("snapshot", "--transcript", str(folder / "trajectory.json"))["counts"]["agents"] >= 2
        listed = call("collect", "--", "cursor", "--list", "--db", cursor["database"], "--projects-dir", cursor["projects"])
        assert listed["exit"] == 0 and CURSOR_PARENT in listed["stdout"]
        root_only = collect("codearts", [fixtures["database"], "--session-id", "ses_root", "--no-children",
                                        "-o", str(exports / "root-only.json")], exports / "root-only.json")
        assert len(root_only["messages"]) == 3
        legacy = collect("codearts", [fixtures["legacy"], "-o", str(exports / "legacy.json")], exports / "legacy.json")
        assert legacy["export_metadata"]["source_format"] == "codearts_legacy_json"
        assert len(legacy["messages"]) == 2
        for name, args in [
            ("cursor", [CURSOR_PARENT, cursor["database"], "--db", cursor["database"], "--projects-dir", cursor["projects"]]),
            ("opencode", ["ses_root", fixtures["database"]]),
            ("codearts", [fixtures["database"], "--session-id", "ses_root", "-o", fixtures["database"]]),
        ]:
            refused = call("collect", "--", name, *args)
            assert refused["exit"] != 0, f"{name} accepted overwriting its source"
        assert source_hashes(output / "inputs") == before, "A collector changed its source files"
        # Exercise missing-dependency behavior separately from the optional real-checkout diagnosis.
        # A fallback message cannot stand in for a tested attribution verdict.
        env["AWE_DECAF_PATH"] = str(output / "missing-decaf")
        if call("doctor")["decaf"]["available"] is False:
            fallback = call("attribution", "--transcript", str(exports / "codearts.json"),
                            "--reference-root", str(output / "missing-reference"),
                            "--agent", "codearts", "--instance", "synthetic")
            assert fallback["available"] is False and "DECAF unavailable" in fallback["reason"]
            assert not fallback["faults"] and not fallback["scorecard"] and fallback["primary"] is None
            ledger["attribution_fallback"] = "passed: missing dependency, no fabricated verdict"
        else:
            ledger["attribution_fallback"] = "not_tested: a real default DECAF checkout was discovered"
        if decaf_root is not None:
            reference_data = prepare_decaf_reference(output)
            reference, canonical = Path(reference_data["reference_root"]), Path(reference_data["transcript"])
            original_reference = source_hashes(reference)
            env.update({"AWE_DECAF_PATH": str(decaf_root), "AWE_ARGUS_ROOT": str(reference),
                        "TRAJVIZ_REFERENCE_ROOT": str(reference), "AWE_PROJECT_DATA": str(output / "awe-cache"),
                        "AWE_OUTPUT_DATA": str(output / "awe-output"), "OPENROUTER_API_KEY": ""})
            assert call("doctor")["decaf"]["available"] is True
            common = ["--reference-root", str(reference), "--agent", "opencode_glm", "--instance", "optional-case"]
            verdict = call("attribution", "--transcript", str(canonical), *common)
            assert verdict["available"] is True, verdict.get("reason")
            assert verdict["mode"] == "reference" and len(verdict["scorecard"]) == 7
            assert verdict["primary"] == {"capability": "code_localization", "error_type": "missed_relevant_file"}
            assert verdict["faults"] and all(fault["evidence_chain"]["links"] for fault in verdict["faults"])
            assert verdict["used_judge"] is False and verdict["arbiter"] is None
            write_json(output / "decaf-attribution.json", verdict)
            different = output / "mismatched-trajectory.json"
            different.write_bytes(canonical.read_bytes() + b"\n ")
            rejected = call("attribution", "--transcript", str(different), *common)
            assert rejected["available"] is False and "does not match the canonical" in rejected["reason"]
            assert not rejected["faults"] and not rejected["scorecard"] and rejected["primary"] is None
            outcome = reference / "data/eval_opencode_glm.json"
            write_json(outcome, {"optional-case": True})
            resolved = call("attribution", "--transcript", str(canonical), *common)
            assert resolved["available"] is False and "resolved" in resolved["reason"]
            write_json(outcome, {"optional-case": False})
            assert call("attribution", "--transcript", str(canonical), *common)["available"] is True
            assert source_hashes(reference) == original_reference
            ledger["attribution_verdict"] = "passed: real DECAF objective diagnosis, identity refusal, resolved-case refusal, recovery"
            fixtures["environment"].update({name: env[name] for name in (
                "AWE_DECAF_PATH", "AWE_ARGUS_ROOT", "TRAJVIZ_REFERENCE_ROOT", "AWE_PROJECT_DATA", "AWE_OUTPUT_DATA",
                "OPENROUTER_API_KEY")})
            fixtures["commands"].append(f'/obs attribution --session "{canonical}" {shlex.join(common)}')
            write_json(output / "native-commands.json", fixtures)
        ledger["status"] = "passed"
        return ledger
    finally:
        write_json(output / "optional-matrix.json", ledger)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--decaf-root", type=Path, help="An existing real awe checkout; enables offline objective verdict checks.")
    parser.add_argument("--prepare-only", action="store_true", help="Write fixtures and native /obs commands only.")
    args = parser.parse_args()
    if args.decaf_root is not None and not (args.decaf_root / "awe/__init__.py").is_file():
        parser.error("--decaf-root must contain awe/__init__.py")
    output = args.output or Path(tempfile.mkdtemp(prefix="trajviz-obs-optional-"))
    if output.resolve().is_relative_to(ROOT):
        parser.error("--output must be outside the repository")
    if output.exists() and any(output.iterdir()):
        parser.error("--output must be empty")
    output.mkdir(parents=True, exist_ok=True)
    result = prepare_inputs(output) if args.prepare_only else run_matrix(args.python, output, decaf_root=args.decaf_root)
    print(json.dumps({"output": str(output), "result": result}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
