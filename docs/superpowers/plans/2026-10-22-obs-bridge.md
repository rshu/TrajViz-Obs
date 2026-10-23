# `/obs` Bridge (Python) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Python command-line bridge that turns a Claude Code session transcript into TrajViz analysis documents (snapshot, views, report, exports, comparisons, LLM prompts and replies, dashboards) as JSON on stdout, for the `/obs` mod to call.

**Architecture:** One package `bridge/trajviz_obs/` behind a `-I`-safe launcher; `cli.py` maps subcommands to handler functions and wraps every result in one JSON envelope; every handler is a plain function over a `Trajectory` loaded through `compat.load_run()`, which is the only place TrajViz internals are touched. Large outputs become files under a per-session artifact folder.

**Tech Stack:** Python 3.11+, `agent-trajviz>=0.2.0,<0.3` (extras `llm` always; `charts`, `app` optional), stdlib argparse/json/zipfile; pytest, ruff for development.

**Spec:** `docs/superpowers/specs/2026-10-22-obs-plugin-design.md`

## Global Constraints

- Python ≥ 3.11; dependency `agent-trajviz>=0.2.0,<0.3`; TrajViz internals only inside `bridge/trajviz_obs/compat.py`, each shim behind `compat.check_supported()`.
- The launcher is run as `python -I bridge/trajviz_obs_cli.py <subcommand> …` with CWD = the session's working directory; the launcher inserts only its own `bridge/` directory into `sys.path`.
- Every subcommand prints exactly one JSON document on stdout and exits 0; failures print `{"error": {"code", "message", "path"?, …}}` and exit 1; argparse usage errors exit 2; an unexpected exception exits 3 with `{"error": {"code": "internal", …}}` and the traceback on stderr.
- Output-size rule: a subcommand whose stdout can grow with the session writes files under the artifact folder and prints `{"path", "bytes", "rows"}`; stdout carries only snapshots and small answers (≤ 256 KB).
- Artifact folder: `<cache>/trajviz-obs/sessions/<session-id>/` with `report.html`, `views/`, `labels.json`, `analyze.json`, `compare/`, `converge/`, `bundle.zip`; `<cache>` = `$XDG_CACHE_HOME`, else `~/.cache`, else `%LOCALAPPDATA%` on Windows. The mod passes it as `--artifacts DIR`; the bridge creates it on demand.
- The bridge never follows paths found inside transcript contents; it reads only the known layout `<session-id>/subagents/**/agent-<id>.meta.json` and the transcript's own records.
- `LoadError` codes `unknown_format` and `no_steps` on the current session (`--kind current`) are the empty state `{"empty": true, "reason": …}` with exit 0, never an error.
- The real context window reaches every consumer: `compat.load_run(path, window_limit=N)` sets `run.raw["metadata"]["context_window_limit"] = N` before anything is computed; `window_source` is reported as `override` when so.
- Nothing is sent anywhere by the bridge itself except in `env` mode (`ask`, `fixes`, `label` direct subcommands), which use TrajViz's own HTTP client with `ANALYZE_*`/`LABEL_*`.

## Review Focus

- A transcript with an interior malformed line: TrajViz fails the whole load with `load_failed`; the bridge must report TrajViz's message verbatim with the path, never a traceback (pinned in Task 5).
- A session with hundreds of sub-agent logs, some over TrajViz's 64 MiB cap: the snapshot's `trajviz.warnings` must carry every skipped log (pinned in Task 5).
- A `view` option the view does not take, or a non-integer `snapshot_step`: an error with the catalog's option help, not a `TypeError` (pinned in Task 7).
- Replies for `llm-apply` that belong to a different transcript (the session grew between prompt and apply): refused by hash, not silently misapplied (pinned in Task 10).
- A `--session` prefix matching two sessions: the ambiguity listed with start times, not the first match taken (pinned in Task 2).

---

### Task 1: Package skeleton, launcher, JSON envelope, `doctor`

**Files:**
- Create: `bridge/trajviz_obs_cli.py`, `bridge/trajviz_obs/__init__.py`, `bridge/trajviz_obs/cli.py`, `bridge/trajviz_obs/errors.py`, `bridge/trajviz_obs/doctor.py`, `bridge/pyproject.toml`, `bridge/tests/conftest.py`, `bridge/tests/fixtures/native_claude_code/README.md`, `bridge/tests/fixtures/native_claude_code/django__django-11477.jsonl` (copied from the TrajViz checkout's `tests/fixtures/native_claude_code/`, MIT; the README states the sanitization note from TrajViz's fixture README)
- Test: `bridge/tests/test_cli.py`

**Interfaces:**
- Produces: `cli.main(argv: list[str] | None = None) -> int`; `cli.register(name: str)` decorator adding a handler `(args: argparse.Namespace) -> dict` to `cli.SUBCOMMANDS`, each handler module exposing `add_parser(sub: argparse._SubParsersAction) -> None`; `errors.BridgeError(code: str, message: str, *, path: str | None = None, **extra)`; `doctor.doctor_info(cwd: str | None) -> dict` with keys `python` (version string), `trajviz` (`{"version", "path"}`), `extras` (`{"charts", "llm", "app"}` booleans from `agent_trajviz._extras`'s import names), `decaf` (`{"available", "reason"}` from `agent_trajviz.attribution.available()`), `llm_env` (`{"ready", "missing": [names]}` from `agent_trajviz.llm.config_from_env(load_dotenv=True)`), `reference_root` (env `TRAJVIZ_REFERENCE_ROOT` or null).
- `conftest.py` fixtures: `fixture_session() -> Path` (the vendored jsonl), `tmp_artifacts(tmp_path) -> Path`, `run_cli(argv) -> tuple[int, dict]` (calls `main` with stdout captured and parses the JSON).

- [ ] **Step 1: Write the failing tests**

```python
def test_doctor_envelope(run_cli):
    code, out = run_cli(["doctor"])
    assert code == 0
    assert out["trajviz"]["version"].startswith("0.2")
    assert set(out["extras"]) == {"charts", "llm", "app"}
    assert "available" in out["decaf"] and "ready" in out["llm_env"]

def test_unknown_subcommand_is_usage_error(run_cli):
    code, out = run_cli(["nonsense"])
    assert code == 2

def test_bridge_error_envelope(run_cli, monkeypatch):
    from trajviz_obs import cli, errors
    @cli.register("boom")
    def _boom(args): raise errors.BridgeError("not_found", "no such thing", path="/x")
    code, out = run_cli(["boom"])
    assert code == 1 and out["error"] == {"code": "not_found", "message": "no such thing", "path": "/x"}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd bridge && python -m pytest tests/test_cli.py -v` (in a venv with `agent-trajviz[all]` installed; `pyproject.toml` declares it)
Expected: FAIL with `ModuleNotFoundError: trajviz_obs`

- [ ] **Step 3: Implement the launcher, `cli.py`, `errors.py`, `doctor.py`, `pyproject.toml`, `conftest.py`**

`cli.main` builds the parser from every registered module's `add_parser`, dispatches, and writes the envelope; `MissingExtraError` from TrajViz maps to code `missing_extra` with `extra` = the extra's name; `LoadError` maps to `load_error` with `trajviz_code` and `path`. `pyproject.toml`: project `trajviz-obs-bridge`, `requires-python >= 3.11`, dependencies `agent-trajviz[all]>=0.2.0,<0.3`, optional `dev = ["pytest>=8", "ruff>=0.5"]`, `[tool.pytest.ini_options] pythonpath = ["."]`, `[tool.ruff] line-length = 120`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd bridge && python -m pytest tests/test_cli.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add bridge
git commit -m "feat(bridge): package skeleton, JSON envelope and doctor"
```

### Task 2: Paths and session resolution

**Files:**
- Create: `bridge/trajviz_obs/paths.py`, `bridge/trajviz_obs/sessions.py`
- Test: `bridge/tests/test_sessions.py`

**Interfaces:**
- Produces: `paths.cache_root(env: Mapping[str, str]) -> Path`; `paths.artifacts_dir(cache_root: Path, session_id: str) -> Path`; `paths.config_dir(env) -> Path` (`CLAUDE_CONFIG_DIR` else `~/.claude`); `paths.projects_dir(config_dir: Path) -> Path`; `paths.project_slug(cwd: str) -> str` (every character outside `[A-Za-z0-9]` → `-`); `sessions.Resolved(path: str, session_id: str, project_dir: str, kind: Literal["file", "dir"])`; `sessions.resolve_session(spec: str, *, cwd: str, config_dir: Path) -> Resolved` (a path to a file or session directory, else an id or unique prefix under this project's folder; raises `BridgeError("ambiguous", …, candidates=[{id, started, size}])` or `BridgeError("not_found", …)`); `sessions.list_sessions(*, cwd: str, config_dir: Path, limit: int) -> list[dict]` with `id, path, mtime_ms, size, records, started` (started = the first `timestamp` found in the first 50 lines, else null; never loads the transcript); subcommands `resolve --cwd --config-dir (--session SPEC | --session-id ID)` and `sessions --cwd --config-dir [--limit N]`.

- [ ] **Step 1: Write the failing tests** (build a fake `<config>/projects/<slug>/` with three sessions `aaa111…`, `aaa222…`, `bbb333…` as tiny jsonl files carrying `timestamp`)

```python
def test_slug_replaces_every_non_alnum():
    assert project_slug("/Users/me/My Proj.x_y") == "-Users-me-My-Proj-x-y"

def test_resolve_unique_prefix(fake_projects):
    r = resolve_session("bbb", cwd=fake_projects.cwd, config_dir=fake_projects.config)
    assert r.session_id.startswith("bbb") and r.kind == "file"

def test_resolve_ambiguous_prefix_lists_candidates(fake_projects):
    with pytest.raises(BridgeError) as e: resolve_session("aaa", cwd=..., config_dir=...)
    assert e.value.code == "ambiguous" and len(e.value.extra["candidates"]) == 2

def test_list_sessions_has_started_and_records(fake_projects):
    rows = list_sessions(cwd=..., config_dir=..., limit=10)
    assert rows[0]["records"] >= 1 and rows[0]["started"] is not None
```

- [ ] **Step 2: Run to verify they fail** — Run: `python -m pytest tests/test_sessions.py -v` — Expected: FAIL (import error)
- [ ] **Step 3: Implement `paths.py`, `sessions.py` and the two subcommands**
- [ ] **Step 4: Run to verify they pass** — Expected: 4 passed
- [ ] **Step 5: Commit** — `git commit -m "feat(bridge): session resolution and listing"`

### Task 3: Sidecar scan (title, real prompts, agent names)

**Files:**
- Create: `bridge/trajviz_obs/sidecars.py`
- Test: `bridge/tests/test_sidecars.py`

**Interfaces:**
- Produces: `sidecars.TranscriptFacts(title: str | None, prompts: list[str], prompt_count: int, meta_count: int)`; `sidecars.scan_transcript(path: str, *, max_prompts: int = 20) -> TranscriptFacts` (streams the file line by line; a `type: "ai-title"` record's title wins, last one kept; a `type: "user"` record counts as a prompt when it has no `isMeta: true` and its text — a string `message.content`, or the joined `text` blocks — does not start with `<`; prompt texts clipped to 200 characters; malformed lines skipped); `sidecars.AgentMeta(agent_id: str, description: str, agent_type: str)`; `sidecars.agent_meta(transcript_path: str) -> dict[str, AgentMeta]` (walks `<stem>/subagents/**` for `agent-*.meta.json`, depth ≤ 16, no symlinks, malformed files skipped).

- [ ] **Step 1: Write the failing tests** (synthetic transcript with an `ai-title` record, a real prompt, an `isMeta` row, a `<command-name>` row; a `subagents/workflows/wf_1/agent-abc.meta.json`)

```python
def test_scan_counts_only_human_prompts(tmp_session):
    f = scan_transcript(tmp_session.transcript)
    assert f.title == "Build the obs plugin" and f.prompt_count == 1 and f.prompts == ["fix the tests"]

def test_agent_meta_from_nested_sidecar(tmp_session):
    meta = agent_meta(tmp_session.transcript)
    assert meta["abc"].description == "Builder check" and meta["abc"].agent_type == "general-purpose"
```

- [ ] **Step 2: Run to verify they fail** — Expected: FAIL (import error)
- [ ] **Step 3: Implement `sidecars.py`**
- [ ] **Step 4: Run to verify they pass** — Expected: 2 passed
- [ ] **Step 5: Commit** — `git commit -m "feat(bridge): transcript sidecar scan"`

### Task 4: `compat` — loading with the real window, labeler chat injection

**Files:**
- Create: `bridge/trajviz_obs/compat.py`
- Test: `bridge/tests/test_compat.py`

**Interfaces:**
- Produces: `compat.TRAJVIZ_VERSION: str`; `compat.check_supported() -> None` (raises `BridgeError("unsupported_trajviz", …)` outside `>=0.2.0,<0.3`); `compat.load_run(path: str, *, window_limit: int | None = None, current: bool = False) -> Trajectory` (calls `agent_trajviz.load`; sets `run.raw["metadata"]["context_window_limit"] = window_limit` when given; when `current` and the `LoadError` code is `unknown_format` or `no_steps`, raises `compat.EmptySession(reason)`); `compat.window_info(run: Trajectory) -> dict` with `window`, `window_source` (`override` when the bridge set it, else TrajViz's), `window_note`; `compat.labeler_chat(chat: Callable[[str, str], str]) -> ContextManager` patching `agent_trajviz.labeling.llm.call_llm` to `chat(system_prompt, user_message)` and `agent_trajviz.labeling.labeler.time.sleep` to a no-op.

- [ ] **Step 1: Write the failing tests**

```python
def test_window_override_reaches_pressure_stats(fixture_session):
    run = load_run(str(fixture_session), window_limit=1_000_000)
    stats = run.view("context_pressure").data["stats"]
    assert stats["window_limit"] == 1_000_000 and window_info(run)["window_source"] == "override"

def test_window_override_reaches_brief(fixture_session):
    from agent_trajviz import llm
    assert "1000000" in llm.brief(load_run(str(fixture_session), window_limit=1_000_000)).replace(",", "")

def test_empty_session_is_signalled(tmp_path):
    p = tmp_path / "s.jsonl"; p.write_text('{"type":"ai-title","title":"x"}\n')
    with pytest.raises(EmptySession): load_run(str(p), current=True)

def test_labeler_chat_injection(fixture_session, tmp_path):
    from agent_trajviz.labeling.labeler import label_trajectory
    seen = []
    with labeler_chat(lambda system, user: seen.append(user) or '{"phase":"understand","action":"code_reading"}'):
        out = label_trajectory(str(fixture_session), str(tmp_path / "l.json"), base_url="x", api_key="x", model="m")
    assert seen and out["counts"]["assistant"] == len(seen)
```

- [ ] **Step 2: Run to verify they fail** — Expected: FAIL (import error)
- [ ] **Step 3: Implement `compat.py`** (the brief test may need the window to appear via `context_pressure` wording; if TrajViz's brief does not print the window, assert on `run.context_pressure()["stats"]["window_limit"]` instead and note it in the test)
- [ ] **Step 4: Run to verify they pass** — Expected: 4 passed
- [ ] **Step 5: Commit** — `git commit -m "feat(bridge): compat layer for window override and labeler injection"`

### Task 5: Snapshot document

**Files:**
- Create: `bridge/trajviz_obs/snapshot.py`
- Modify: `bridge/trajviz_obs/cli.py` (register)
- Test: `bridge/tests/test_snapshot.py`

**Interfaces:**
- Consumes: `compat.load_run`, `compat.window_info`, `sidecars.scan_transcript`, `sidecars.agent_meta`.
- Produces: `snapshot.build_snapshot(run: Trajectory, *, transcript_path: str, facts: TranscriptFacts, agents_meta: dict[str, AgentMeta], artifacts: Path | None, max_points: int = 400, duration_ms: int = 0) -> dict`; `snapshot.downsample(values: list[float | None], n: int) -> list[float | None]` (bucket max for counts, keeps length ≤ n, index list alongside); `snapshot.empty_snapshot(reason: str, transcript_path: str) -> dict` (`{"schema_version": 1, "empty": true, "reason": …, "transcript": {…}}`); subcommand `snapshot --transcript PATH [--window N] [--artifacts DIR] [--max-points N] [--kind current|other]`.
- **The snapshot contract** (the mod's `Snapshot` type mirrors it exactly):

```
schema_version: 1
generated_at: ISO-8601 UTC;  duration_ms: int
transcript: { path, session_id, size, mtime_ms, sha256 }
trajviz: { version, format, format_label, warnings: string[] }
title: string | null;  task: string | null
counts: { records, assistant_steps, user_records, prompts, compactions, agents, tool_calls }
tokens: { total, input, output, cache_read, cache_write, reasoning }
timing: { wall_clock_s, started_at, finished_at, avg_step_s, p95_step_s, output_tokens_per_sec, tool_wait_share }
tools: { success_rate: number | null, calls, failed, by_tool: [{ tool, calls, errors }] (top 12 by calls) }
health: [{ metric, status, label, detail }]
issues: [{ kind, title, detail, why, steps: number[], evidence: string }] (top 10; evidence = the first step's first tool call as "<tool>: <command or path excerpt ≤ 80 chars>", else "")
bottlenecks: [{ step, duration_s, cause, title, detail }] (top 5)
context: { window, window_source, window_note, current_occupancy, current_pct, peak_occupancy, peak_pct, compaction_count, largest_drop }
   (current_* = the main agent's last point of context_pressure series; pct null when window null)
series: { tokens: { steps, fresh, cache_read, cache_write, output }, durations: { steps, seconds, outliers }, context: { turns, occupancy, compactions } }  (each list ≤ max_points)
phases: [{ name, start, end }]
agents: [{ id, label, type, description, steps, tokens, tool_calls, errors, duration_s, cache_pct, gen_tps }]
   (label = meta description clipped to 40 chars when TrajViz's label is the raw id, else TrajViz's)
swimlane: { total_steps, lanes: [{ id, label, segments: [{ start, end }] }] }
patterns: { tool_sequences, failure_patterns, error_clusters, failure_chains, fruitless_streaks, tool_selection, edit_thrash, repeated_searches, phase_regressions, premature_compactions, hotspots }  (each list top 8; hotspots lists top 5)
timeline: [{ step, role, category, agent, errors, tool_calls, duration_s, tokens, preview }] (the last 60 steps)
diagnostics: object (the `diagnostics` view as is)
files: { unique, reads, writes, searches, top: [{ file, reads, writes }] (top 8) }
plan: { items, done, stalled }
skills: [{ skill, calls }]
artifacts: { dir, report, views, labels } (paths or null, by existence)
text: string  (filled by Task 6; "" until then)
```

- [ ] **Step 1: Write the failing tests** (a synthetic 5,000-step session written by a conftest helper `make_session(tmp_path, steps=…, agents=…)` producing native records with usage and tool calls)

```python
def test_snapshot_keys_and_bounds(big_session):
    snap = build_snapshot(load_run(big_session, window_limit=200_000), transcript_path=big_session, facts=scan_transcript(big_session), agents_meta={}, artifacts=None)
    assert snap["schema_version"] == 1 and snap["counts"]["assistant_steps"] > 0
    assert len(snap["series"]["tokens"]["steps"]) <= 400 and len(snap["timeline"]) <= 60
    assert len(json.dumps(snap)) < 300_000

def test_snapshot_enrichment(tmp_session):  # title, prompts, agent label from meta
    snap = build_snapshot(...)
    assert snap["title"] == "Build the obs plugin" and snap["counts"]["prompts"] == 1
    assert any(a["label"] == "Builder check" for a in snap["agents"])

def test_snapshot_cli_empty_current(run_cli, tmp_path): ...  # exit 0, {"empty": true}
def test_snapshot_cli_load_error_verbatim(run_cli, tmp_path): ...  # interior bad line → code load_error, message from TrajViz, path
def test_snapshot_warnings_carry_skipped_children(tmp_session_with_bad_child): ...  # trajviz.warnings non-empty
```

- [ ] **Step 2: Run to verify they fail** — Expected: FAIL
- [ ] **Step 3: Implement `snapshot.py` and the subcommand** (compute `run.views(only=[…])` for exactly the views the contract draws; the `evidence` helper reads `run.steps[i]["tool_calls"][0]`)
- [ ] **Step 4: Run to verify they pass** — Expected: 5 passed
- [ ] **Step 5: Commit** — `git commit -m "feat(bridge): snapshot document"`

### Task 6: Overview text and inline renderers

**Files:**
- Create: `bridge/trajviz_obs/text.py`
- Modify: `bridge/trajviz_obs/snapshot.py` (fill `text`)
- Test: `bridge/tests/test_text.py`

**Interfaces:**
- Produces: `text.render_overview(snap: dict) -> str` (markdown, ≤ 40 lines: a `Health:` line of `metric status (label)` pairs; a numbers line with assistant steps, prompts, agents, tokens split, tool calls and success, wall clock; `Context:` line with `current_pct` and window or `ctx ?` plus the note; `Issues:` numbered with steps and evidence; `Bottlenecks:`; `Hints:` from each issue's `why` (top 3); a `Warnings:` line when any); `text.render_table(rows: list[dict], *, limit: int, offset: int) -> str` (markdown table of the union of keys, nested values stringified, footer `rows N–M of T`); `text.render_series(values: list[float | None], *, width: int = 60) -> str` (block glyphs `▁▂▃▄▅▆▇█`, `·` for null).

- [ ] **Step 1: Write the failing tests**

```python
def test_overview_text_shape(fixture_snapshot):
    t = render_overview(fixture_snapshot)
    assert t.count("\n") <= 40 and "Health:" in t and "Issues:" in t
    assert fixture_snapshot["issues"][0]["evidence"] in t

def test_table_pages(): assert render_table([{"a": i} for i in range(100)], limit=25, offset=25).endswith("rows 26–50 of 100")
def test_series_glyphs(): assert render_series([0, 1, None, 8], width=4) == "▁▂·█"
```

- [ ] **Step 2: Run to verify they fail** — Expected: FAIL
- [ ] **Step 3: Implement `text.py`; `build_snapshot` sets `text = render_overview(snap)`**
- [ ] **Step 4: Run to verify they pass** — Expected: 3 passed
- [ ] **Step 5: Commit** — `git commit -m "feat(bridge): overview text and inline renderers"`

### Task 7: Views, JSON export, schemas

**Files:**
- Create: `bridge/trajviz_obs/views_cmd.py`
- Test: `bridge/tests/test_views.py`

**Interfaces:**
- Produces: `views_cmd.catalog() -> list[dict]` (`id, kind, title, description, options`); `views_cmd.coerce_options(view_id: str, raw: dict[str, str], run: Trajectory) -> dict` (`snapshot_step` → int, `window_limit` as given, `agent`: `main` → `__main__`, validated against the view's `agents` keys; an unknown option → `BridgeError("bad_option", …, options=<catalog options>)`); `views_cmd.view_payload(run, view_id: str, options: dict, *, limit: int, offset: int, out_dir: Path, inline_json: bool) -> dict` (`{"view": {id, title, kind, description}, "path", "bytes", "rows_total", "inline": {"table": str} | {"series": str} | {"data": …}}`; the full view JSON is always written to `out_dir/<id>.json`; `inline.data` only when `inline_json` and the file is ≤ 256 KB); `views_cmd.export(run, out_dir: Path, *, only: list[str] | None, with_trajectory: bool) -> dict` (`{"path", "files", "bytes"}`; without `with_trajectory` it exports `run.views(only=…)` so no `trajectory.json` is written); `views_cmd.write_schemas(out_dir: Path) -> dict`; subcommands `view --transcript … [ID] [--option k=v]… [--limit N] [--offset M] [--json]`, `json --transcript … [--out DIR] [--view ID]… [--with-trajectory]`, `schemas [--out DIR]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_catalog_has_35_views(): assert len(catalog()) == 35
def test_coerce_snapshot_step_and_agent(fixture_run):
    assert coerce_options("context_usage", {"snapshot_step": "12", "agent": "main"}, fixture_run) == {"snapshot_step": 12, "agent": "__main__"}
def test_unknown_option_is_bridge_error(fixture_run):
    with pytest.raises(BridgeError) as e: coerce_options("tool_usage", {"nope": "1"}, fixture_run)
    assert e.value.code == "bad_option"
def test_view_payload_pages_and_writes_file(fixture_run, tmp_artifacts):
    p = view_payload(fixture_run, "steps", {}, limit=25, offset=0, out_dir=tmp_artifacts / "views", inline_json=False)
    assert (tmp_artifacts / "views" / "steps.json").exists() and p["rows_total"] > 25 and "rows 1–25" in p["inline"]["table"]
def test_export_without_trajectory_by_default(fixture_run, tmp_artifacts):
    export(fixture_run, tmp_artifacts / "views", only=None, with_trajectory=False)
    assert not (tmp_artifacts / "views" / "trajectory.json").exists() and (tmp_artifacts / "views" / "index.json").exists()
```

- [ ] **Step 2: Run to verify they fail** — Expected: FAIL
- [ ] **Step 3: Implement `views_cmd.py` and the three subcommands**
- [ ] **Step 4: Run to verify they pass** — Expected: 5 passed
- [ ] **Step 5: Commit** — `git commit -m "feat(bridge): views, json export and schemas"`

### Task 8: HTML report and ZIP bundle

**Files:**
- Create: `bridge/trajviz_obs/report_cmd.py`
- Test: `bridge/tests/test_report.py`

**Interfaces:**
- Produces: `report_cmd.write_report_file(run: Trajectory, dest: Path, *, dark: bool, offline: bool) -> dict` (`{"path", "bytes"}`; `dest` is an explicit file path, overwritten; `MissingExtraError` propagates as `missing_extra`); `report_cmd.bundle_session(transcript_path: str, dest: Path) -> dict` (`{"path", "bytes", "members"}`; members are `<id>.jsonl` and every file under `<id>/subagents/**`, stored with those relative names); subcommands `report --transcript … [--out PATH] [--dark] [--offline]` (default `--out` = `<artifacts>/report.html`), `bundle --transcript … [--out PATH]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_report_written_and_overwritten(fixture_run, tmp_artifacts):
    a = write_report_file(fixture_run, tmp_artifacts / "report.html", dark=False, offline=False)
    b = write_report_file(fixture_run, tmp_artifacts / "report.html", dark=True, offline=False)
    assert a["path"] == b["path"] and (tmp_artifacts / "report.html").read_text().count("<html") == 1
def test_bundle_layout(tmp_session_with_child, tmp_artifacts):
    out = bundle_session(tmp_session_with_child.transcript, tmp_artifacts / "bundle.zip")
    names = zipfile.ZipFile(out["path"]).namelist()
    assert f"{tmp_session_with_child.id}.jsonl" in names and any(n.startswith(f"{tmp_session_with_child.id}/subagents/") for n in names)
```

- [ ] **Step 2: Run to verify they fail** — Expected: FAIL
- [ ] **Step 3: Implement `report_cmd.py` and the subcommands**
- [ ] **Step 4: Run to verify they pass** — Expected: 2 passed
- [ ] **Step 5: Commit** — `git commit -m "feat(bridge): HTML report and session bundle"`

### Task 9: Comparison, scorecard, label views

**Files:**
- Create: `bridge/trajviz_obs/compare_cmd.py`
- Test: `bridge/tests/test_compare.py`

**Interfaces:**
- Produces: `compare_cmd.compare_sessions(current: Trajectory, other_path: str, *, anchor_patch: str | None, fuzzy: bool, reverse: bool, out_dir: Path) -> dict` (`tv.compare(reference=other, compared=current)`, swapped when `reverse`; writes `out_dir/compare/<other-stem>.json` with every `comparison_views()` view and returns `{"path", "orientation": {"reference", "compared"}, "summary": <comparison_summary view data>, "text": str}`); `compare_cmd.scorecard(current: Trajectory, others: list[str], *, fuzzy: bool, labels: list[str] | None, no_self: bool, out_dir: Path) -> dict` (`compare_runs()` with the current run first unless `no_self`; returns `{"path", "rows": <run_scorecard data>, "text"}`); `compare_cmd.label_views_payload(sidecar: str, other: str | None) -> dict` (`label_views` and, with `other`, `label_comparison_views(other, sidecar)`; `{"summary", "phases": …, "comparison"?: …, "text"}`); subcommands `compare --transcript … --other PATH [--anchor-patch P] [--fuzzy-commands] [--reverse]`, `scorecard --transcript … --others P… [--fuzzy-commands] [--labels a,b] [--no-self]`, `labels --sidecar P [--other P]`.

- [ ] **Step 1: Write the failing tests** (fixture vs a synthetic session; a sidecar written by Task 4's labeler test helper)

```python
def test_compare_orientation(fixture_run, synthetic_session, tmp_artifacts):
    out = compare_sessions(fixture_run, synthetic_session, anchor_patch=None, fuzzy=False, reverse=False, out_dir=tmp_artifacts)
    assert out["orientation"]["compared"].endswith("django__django-11477.jsonl") and Path(out["path"]).exists()
def test_scorecard_current_is_first_row(fixture_run, synthetic_session, tmp_artifacts):
    assert scorecard(fixture_run, [synthetic_session], fuzzy=False, labels=None, no_self=False, out_dir=tmp_artifacts)["rows"][0]["path"].endswith("11477.jsonl")
def test_label_views_payload(sidecar_path): assert "classification_rate" in label_views_payload(sidecar_path, None)["summary"]
```

- [ ] **Step 2: Run to verify they fail** — Expected: FAIL
- [ ] **Step 3: Implement `compare_cmd.py` and the subcommands**
- [ ] **Step 4: Run to verify they pass** — Expected: 3 passed
- [ ] **Step 5: Commit** — `git commit -m "feat(bridge): comparison, scorecard and label views"`

### Task 10: LLM prompts — capture and replay

**Files:**
- Create: `bridge/trajviz_obs/llm_cmd.py`
- Test: `bridge/tests/test_llm.py`

**Interfaces:**
- Consumes: `compat.labeler_chat`.
- Produces: `llm_cmd.Feature = Literal["analyze", "fixes", "label"]`; `llm_cmd.plan_prompts(run, feature, *, transcript_path: str, question: str | None, history_path: Path | None, limit: int, language: str | None, max_steps: int, assistant_only: bool) -> dict` → `{"feature", "hash", "plan": {"calls", "approx_input_tokens"}, "prompts": [{"index", "system", "prompt", "max_tokens", "timeout_s"}]}` where `hash` = sha256 of (run.sha256, feature, question, limit); analyze renders prior turns from `history_path` as `User: …\n\nAssistant: …` blocks before the question in `prompt`, `system` as TrajViz's; `max_tokens`/`timeout_s` from `agent_trajviz.llm.config_from_env()` (2048/120 defaults) for analyze and fixes, 1024/120 for label; capture uses `llm.ask(..., chat=capture)` / `llm.suggest_fixes(run, chat=capture, limit=limit)` / `labeler_chat(capture)` with placeholders (`""`, `{"where":"","fix":"","also":"","confidence":"low"}`, `{"phase":"unknown","action":"unknown"}`); `llm_cmd.apply_replies(run, feature, replies: dict, *, transcript_path, question, history_path, limit, language, out_path: Path, max_steps, assistant_only) -> dict` (refuses a `replies["hash"]` ≠ the recomputed hash with `BridgeError("stale_prompts", …)`; each reply is `{"index", "text"}` or `{"index", "error"}`; analyze → `{"reply", "history_path"}` and appends to `history_path`; fixes → `{"issues", "errors", "text"}`; label → `{"path", "summary", "text"}` writing the sidecar to `out_path`); `llm_cmd.direct(run, feature, *, …) -> dict` (env mode; same results via TrajViz's own client); subcommands `llm-prompt`, `llm-apply --replies PATH`, `ask`, `fixes`, `label`.

- [ ] **Step 1: Write the failing tests**

```python
def test_plan_fixes_captures_one_prompt_per_issue(fixture_run):
    p = plan_prompts(fixture_run, "fixes", transcript_path=..., question=None, history_path=None, limit=3, language=None, max_steps=50, assistant_only=False)
    assert p["plan"]["calls"] == min(3, len(fixture_run.view("issues").data)) and p["prompts"][0]["max_tokens"] == 2048
def test_apply_fixes_parses_judgments(fixture_run, tmp_artifacts):
    p = plan_prompts(...); replies = {"hash": p["hash"], "replies": [{"index": i, "text": '{"where":"CLAUDE.md","fix":"do x","also":"","confidence":"high"}'} for i in range(p["plan"]["calls"])]}
    out = apply_replies(fixture_run, "fixes", replies, ...)
    assert out["issues"][0]["judgment"]["where"] == "CLAUDE.md" and out["errors"] == []
def test_apply_refuses_stale_hash(fixture_run): ...  # BridgeError stale_prompts
def test_analyze_history_round_trip(fixture_run, tmp_artifacts): ...  # second plan_prompts prompt contains the first reply
def test_label_capture_and_apply(fixture_run, tmp_artifacts): ...  # sidecar written, summary classification_rate > 0
def test_reply_error_is_reported(fixture_run): ...  # {"index": 0, "error": "api-error 529"} → errors[0] mentions it
```

- [ ] **Step 2: Run to verify they fail** — Expected: FAIL
- [ ] **Step 3: Implement `llm_cmd.py` and the subcommands**
- [ ] **Step 4: Run to verify they pass** — Expected: 6 passed
- [ ] **Step 5: Commit** — `git commit -m "feat(bridge): LLM prompt capture and reply replay"`

### Task 11: Dashboards with preload

**Files:**
- Create: `bridge/trajviz_obs/dashboard_cmd.py`
- Test: `bridge/tests/test_dashboard.py`

**Interfaces:**
- Produces: `dashboard_cmd.build_preloaded_insight(transcript_path: str) -> gr.Blocks` (builds `agent_trajviz.dashboard.insight_app()`, finds the trajectory `gr.File` (label starting `Trajectory`) and re-enters the Blocks to add `app.load(fn=lambda: transcript_path, outputs=[file], js=<setTimeout click on #tv-load-btn after 1500 ms>)`); `dashboard_cmd.serve_insight(transcript_path: str, port: int | None) -> None` (launches with `server_name="127.0.0.1"`, `prevent_thread_lock=True`, `quiet=True`, `allowed_paths=[dirname(transcript)]`, `**insight_presentation()`; prints `{"url": local_url}` + newline and flushes as the first stdout line; blocks reading stdin until EOF, then closes the server); `dashboard_cmd.serve_converge(data_root: str, port: int | None) -> None` (same shape over `launch_converge`); subcommands `dashboard --transcript … [--port N]`, `converge --data-root DIR [--port N]`. A missing `app` extra is `missing_extra` before anything else is printed.

- [ ] **Step 1: Write the failing test**

```python
def test_preloaded_app_has_load_event(fixture_session):
    app = build_preloaded_insight(str(fixture_session))
    assert any("tv-load-btn" in (getattr(d, "js", "") or "") for d in app.fns.values()) or any("tv-load-btn" in str(c) for c in app.config["dependencies"])
```

- [ ] **Step 2: Run to verify it fails** — Expected: FAIL
- [ ] **Step 3: Implement `dashboard_cmd.py`** (the browser-level check is the mod plan's final task)
- [ ] **Step 4: Run to verify it passes** — Expected: 1 passed
- [ ] **Step 5: Commit** — `git commit -m "feat(bridge): dashboards with session preload"`

### Task 12: Pass-throughs and attribution, brief, final integration

**Files:**
- Create: `bridge/trajviz_obs/passthrough.py`
- Test: `bridge/tests/test_passthrough.py`, `bridge/tests/test_integration.py`

**Interfaces:**
- Produces: `passthrough.run_converge_cli(argv: list[str], out_dir: Path) -> dict` (`agent_trajviz.converge.cli.main(argv)` with stdout captured; when the output is JSON it is written to `out_dir/converge/<n>.json`; returns `{"exit", "path", "text"}`); `passthrough.run_collect(argv: list[str]) -> dict` (`{"exit", "stdout", "stderr"}`, each capped at 64 KB); `passthrough.attribution(run, *, reference_root: str | None, agent: str | None, instance_id: str | None) -> dict` (`agent_trajviz.attribution.diagnose` result as is); `passthrough.brief(run) -> dict` (`{"brief": llm.brief(run)}`); subcommands `converge-cli -- ARGS…`, `collect -- ARGS…`, `attribution --transcript … [--reference-root DIR] [--agent A] [--instance I]`, `brief --transcript … [--window N]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_converge_cli_summary(fixture_session, synthetic_session, tmp_artifacts):
    out = run_converge_cli([str(fixture_session), synthetic_session, "--output", "summary"], tmp_artifacts)
    assert out["exit"] == 0 and "reference" in out["text"].lower()
def test_collect_version(): assert run_collect(["--version"])["exit"] == 0
def test_attribution_unavailable_is_honest(fixture_run):
    assert attribution(fixture_run, reference_root=None, agent=None, instance_id=None)["available"] is False
def test_every_subcommand_runs_on_fixture(run_cli, fixture_session, tmp_artifacts):  # resolve/sessions use a fake project dir; dashboard/converge excluded
    for argv in [["snapshot", "--transcript", str(fixture_session), "--window", "200000", "--artifacts", str(tmp_artifacts)], ["view", ...], ["json", ...], ["report", ...], ["bundle", ...], ["brief", ...], ["llm-prompt", ..., "--feature", "analyze"], ["doctor"]]:
        code, out = run_cli(argv); assert code == 0, argv
```

- [ ] **Step 2: Run to verify they fail** — Expected: FAIL
- [ ] **Step 3: Implement `passthrough.py` and the subcommands; run `ruff check bridge` and fix**
- [ ] **Step 4: Run the whole suite** — Run: `cd bridge && python -m pytest -q && ruff check .` — Expected: all passed, no lint errors
- [ ] **Step 5: Commit** — `git commit -m "feat(bridge): pass-throughs, attribution, brief and integration test"`
