# `/obs` — TrajViz observability for the current Claude Code session

**Status:** design v2, 2026-10-22. Written from the request plus recon of
[TrajViz](https://github.com/rshu/TrajViz) (agent-trajviz 0.2.0), the Claude
Code 2.1.304 plugin ("mod") API, a throwaway spike mod (validated, tested,
type-checked and run headless), a browser check of the dashboard preload, a
builder check and a three-lens review with adversarial verification.
Decisions marked *(my call)* were made without the requester's input; the
requester can overturn any of them.

## Intent

A Claude Code plugin. When the person types `/obs` in a session, it shows a
visualization and a diagnosis of **that session's trajectory**: tokens and
cache behaviour, timing, tool use, health verdicts, detected anti-patterns,
issues, context-window pressure, agents and sub-agents, phases, and (when asked)
an LLM analysis with suggested fixes. Everything is computed by TrajViz from the
session's own transcript, which Claude Code writes to
`~/.claude/projects/<project-slug>/<session-id>.jsonl` (sub-agents under
`<session-id>/subagents/**/agent-*.jsonl`, which TrajViz already discovers).

The plugin is TrajViz brought to where the trajectory is being produced: no
export, no upload, no leaving the terminal unless the person wants the full
dashboard or HTML report in a browser.

## Why now

TrajViz 0.2.0 reads native Claude Code sessions, and this Claude Code build has
a plugin API that can register slash commands, draw panes and rich command
output in the terminal, run host processes, and call the session's own model.
Together they make an in-session observability tool possible without a
separate workflow.

## Goals

1. `/obs` works on the current session with one keystroke: an overview card in
   the transcript, and a live pane beside it where the layout allows.
2. **Every TrajViz feature is reachable** from `/obs`: all 35 single-run views,
   health checks, issues triage, anti-pattern and diagnostics tables, the HTML
   report, JSON export with schemas, the Insight and Converge dashboards, the AI
   analysis and the Issues fix judge, step labeling and label views (single and
   two-run), pairwise comparison (anchor patch, fuzzy commands, token rate),
   N-run scorecards, Converge batch and before/after modes, DECAF attribution
   (where configured), the collect tools, and the extension points (TrajViz
   plugins registering formats and views are honoured because the bridge goes
   through `tv.load()` / `tv.load_plugins()`).
3. The diagnosis is **readable by Claude too**: the command's output is in the
   transcript, and a tool lets Claude fetch more of the same data on request,
   so the person can say "fix the top issue" next.
4. Zero-config first run on a machine with Python 3.11+ or `uv`: the plugin
   creates and maintains its own environment, in seconds.
5. Honest numbers: context pressure is measured against the session's real
   context window (the engine reports it), the overview separates assistant
   steps from Claude Code's bookkeeping rows, and nothing is shown as current
   that is not.

## Non-goals

- Re-implementing TrajViz's analysis in TypeScript. The hooks module draws and
  orchestrates; Python (TrajViz) computes.
- A general trajectory browser for other agents' files. `/obs` is about Claude
  Code sessions; other formats still open through `--session <path>` because
  TrajViz detects them, but no UI is designed for them.
- Hosting or sharing dashboards beyond localhost.
- Changing TrajViz itself in this work. Small upstream additions that would
  simplify the plugin are recorded in `docs/upstream-notes.md`.

## Anti-goals (what would make this a failure even if it "works")

- Sending the transcript anywhere. LLM features send only TrajViz's statistics
  brief and clipped issue windows, redacted by TrajViz's own redactor.
- A `/obs` that takes the model's turn or costs tokens by default. The overview
  is computed locally and deterministically; the model is involved only for
  `analyze`, `fixes` and `label`, only on request, with the cost stated first.
- Breaking the session: a failed analysis must never block the prompt, kill the
  turn, or leave child processes behind.
- Stale or wrong numbers presented as truth: a card from thirty turns ago must
  keep showing what it showed; a percentage must name its window; a count must
  say what it counts.

## Constraints

- **Plugin form:** a Claude Code plugin with a function-hooks module
  (`hooks/hooks.json` → `hooks/register.tsx`), the form this build's
  plugin-authoring skill prescribes for a slash command, a pane and rich
  command output. The repository root is the plugin root and carries a
  `.claude-plugin/marketplace.json`, so it installs with
  `/plugin install trajviz-obs --marketplace rshu/TrajViz-Obs` (desktop app:
  the Plugins menu, steps in the README).
- **Mod environment:** the hooks module has no Node and no DOM; it reaches the
  host through `$` only. Its own code has a 10 s budget per hook dispatch, but
  time spent in `$` calls (process runs, model calls) does not count; a
  `$.clock.sleep` does count, so waits use `$.clock.after` or timestamps.
  `$.env.get` names and `$.state` references must be literals in the source,
  so no helper takes an env name or a state key as a parameter; every plugin
  file is reached by a static `import`.
- **Host process limits:** `$.process.run` returns at most 4 MiB per stream,
  times out at 30 s unless `timeoutMs` is set (ten minutes at most); longer
  work goes through `$.process.spawn`. `$.fs.read` reads at most 4 MiB.
  `$.process` is a CLI noun; a host that withholds it gets a one-line answer
  (below), not a crash.
- **TrajViz:** Python 3.11+. Core is stdlib-only; `llm` (requests) is needed
  even to build prompts because `agent_trajviz.llm` imports it; `charts`
  (plotly, pygments) for figures and the HTML report; `app` (gradio) for the
  dashboards; DECAF is a separate checkout and optional. Reports and
  dashboards draw the first 2,000 steps (TrajViz's cap); views use every step.
  TrajViz has no incremental mode: every refresh is a full load.
- **Transcript freshness:** the session file is appended while the session
  runs; TrajViz drops an unterminated last line and loads the rest. A fresh
  session with no assistant step yet raises `LoadError` (`unknown_format` or
  `no_steps`), which the bridge maps to "nothing to analyze yet".
- **TrajViz's public surface** is the names exported from `agent_trajviz` plus
  `agent_trajviz.figures`, `.dashboard`, `.llm`, `.attribution`; the console
  entry points named in its `pyproject.toml` (`agent_trajviz.insight.__main__`,
  `converge.cli`, `collect.__main__`, `labeling.labeler`) are stable by
  contract too. `insight.*`, `converge.*` (beyond the CLI), `labeling.*`
  (beyond the CLI) and `collect.*` (beyond the CLI) are internal. The plugin
  ships against the **published 0.2.0** (`agent-trajviz>=0.2.0,<0.3`), so the
  places with no public path live in one adapter module
  (`trajviz_obs/compat.py`), each shim guarded by the TrajViz version and
  covered by a test that fails loudly when upstream changes:
  1. **Session-mode labeling:** `label_trajectory()` has no `chat=` injection
     point, so the adapter patches `agent_trajviz.labeling.llm.call_llm` for
     the duration of a capture or replay run (and the labeler's retry sleep).
  2. **Real context window everywhere:** `context_pressure(window_limit=)` and
     the two `context_*` views take an override, but the analysis behind the
     issues list, the premature-compaction detector, the brief, the report and
     the export does not. The adapter sets
     `run.raw["metadata"]["context_window_limit"]` to the engine's window
     right after `tv.load()` (TrajViz then reports `window_source:
     "declared"`, which the snapshot relabels `override`), so every consumer,
     `write_report(run)` and `export_json(run)` included, measures against it.
  3. The analysis and judge prompts are captured and replayed through the
     public `chat=` callable (no internals; listed because it is the mechanism).

## Decided: how it gets made

### Shape *(my call)*

```
TrajViz-Obs/                         ← plugin root (this repository)
  .claude-plugin/plugin.json         name "trajviz-obs", version, author, userConfig, types
  .claude-plugin/marketplace.json    one entry, source "./"
  hooks/hooks.json                   { "modules": ["./register.tsx"] }
  hooks/register.tsx                 register(on, options): wiring only
  hooks/lib/*.ts                     args, paths, python env, bridge calls, state refs, formatting, rasters
  hooks/render/*.tsx                 overview card (CommandOutput), pane tabs, shared widgets
  hooks/*.test.tsx                   `claude plugin test .`
  types/index.d.ts                   the PluginState contract for the mod's $.state values
  bridge/trajviz_obs_cli.py          launcher: python -I <this file> <subcommand> …
  bridge/trajviz_obs/                the bridge package (argparse CLI, JSON on stdout)
  bridge/tests/                      pytest against real agent-trajviz and fixtures
  bridge/pyproject.toml              dev-only: pytest, ruff, mypy
  README.md, LICENSE (MIT), CHANGELOG.md, tsconfig.json, .gitignore
  docs/superpowers/{specs,plans}/, docs/upstream-notes.md
```

`types/index.d.ts` declares `interface PluginState { 'trajviz-obs': … }` and is
named in `plugin.json` as `"types"`; `claude plugin validate` reads it and
checks every `$.state` key the module names against it (verified on the spike).

Two layers with one contract between them:

- **Bridge (Python):** `trajviz_obs_cli.py <subcommand> [args]`, run with
  `-I` and with the session's working directory as CWD (so TrajViz's own
  `.env` loading sees the project's file in `env` mode). It prints one JSON
  document on stdout and exits 0; on failure, non-zero with
  `{"error": {"code", "message", "path"?}}`. Any subcommand whose output can
  exceed 256 KB writes files under the session's artifact folder and prints
  `{"path", "bytes", "rows"}` instead; stdout JSON is for snapshots and small
  answers only. Every subcommand is independently testable with pytest.
- **Mod (TypeScript):** registers `/obs`, parses arguments, resolves the
  session, runs the bridge through `$.process.run` (with an explicit
  `timeoutMs`: 120 s for snapshot, resolve, view and sessions; 600 s for
  json, report, bundle, compare, scorecard and converge CLI runs) or
  `$.process.spawn` (setup, label, dashboards), keeps results in `$.state`,
  draws the overview card and the pane, and opens browsers. It never parses
  transcripts itself.

### Analysis engine: PyPI `agent-trajviz`, not a vendored copy *(my call)*

The bridge depends on `agent-trajviz>=0.2.0,<0.3` from PyPI, installed into an
environment the plugin manages. Rationale: TrajViz is a maintained package by
the same author; vendoring 38k lines would fork it; the stdlib-only core makes
the base install fast. For development, the userConfig field `trajvizSource`
accepts a local checkout path (installed editable) or any pip requirement.

### Python environment *(my call)*

- Location: `<cache>/trajviz-obs/venv`, where `<cache>` is `$XDG_CACHE_HOME`,
  else `$HOME/.cache`, else `%LOCALAPPDATA%` (Windows without `HOME`). A
  `state.json` beside it records what was installed (TrajViz version, extras,
  source, interpreter) so the mod decides whether a setup is needed without
  running pip.
- Creation: `uv` when on PATH (`uv venv --python '>=3.11'`, which picks the
  newest installed interpreter or downloads one, then `uv pip install`);
  otherwise the first of `python3.13`, `python3.12`, `python3.11`, `python3`,
  `python` whose `--version` is ≥ 3.11, with `-m venv` and `pip`. The
  userConfig field `python` overrides the interpreter.
- Extras in stages: first run installs `agent-trajviz[llm]` only (pure Python,
  seconds), enough for every view, the overview, JSON export and the LLM
  prompts. `charts` is installed on the first `report`, `app` on the first
  `dashboard` or `converge`; each of those two installs is announced in the
  command's row before it starts and confirmed with `$.ui.ask` when the
  session is interactive (a dismissed ask counts as no); a headless session
  installs without asking. `/obs setup --extras all` installs everything.
- Setup runs through `$.process.spawn`, streaming progress to `$.ui.status`,
  with a toast at the start ("obs: first run, setting up TrajViz (about
  10 s)"); the same `/obs` that triggered it continues into its overview.
- When neither `uv` nor a Python ≥ 3.11 exists, the row says exactly which
  were looked for, and that installing `uv` or Python 3.11+ (or setting
  `python` in `/config`) fixes it.
- The bridge is run as `<venv python> -I <plugin.root>/bridge/trajviz_obs_cli.py`.
  `-I` keeps the working directory and user site-packages off `sys.path`; the
  launcher adds only its own `bridge/` directory before importing the package.

### Finding the session transcript *(my call)*

1. Every `classic.SessionStart` and `classic.Stop` event carries
   `transcript_path`; the mod keeps the **latest** one seen in `$.state`
   (a `/clear` starts a new transcript file). Both hooks are observe-only
   pass-throughs.
2. Otherwise: `<configDir>/projects/<slug>/<id>.jsonl`, with `id` from
   `$.session.id()`, `configDir` from `$CLAUDE_CONFIG_DIR` else `~/.claude`,
   and `slug` = the session's working directory with every character outside
   `[A-Za-z0-9]` replaced by `-`; accepted only if `$.fs.exists` says so.
3. Otherwise the bridge's `resolve` subcommand scans `<configDir>/projects/*/`
   for `<id>.jsonl`.

`--session <id|prefix|path>` on any subcommand analyzes another session instead
(an id or unique id prefix from this project, a path to any TrajViz-readable
file, or a session directory); an ambiguous prefix lists the matches with their
start times. The session named last by the person's own `--session` (or the
current one) is the **selected session**, which the pane, the status line and
the model's tool read. `/obs sessions` lists this project's sessions by id,
start time, size and record count without loading them.

### Command grammar

`/obs [subcommand] [options]`, every subcommand accepting `--session <id|path>`.
Registered with `argumentHint: "[overview|pane|report|dashboard|issues|health|analyze|fixes|json|help] [--session id]"`
and `immediate: true` *(my call)*, so `/obs` typed while Claude is working runs
at once against the transcript as written so far; its hooks assume nothing
about the turn.

**Look**

| Subcommand | What the person gets |
|---|---|
| *(none)* / `overview` | The overview card in the transcript (health, KPIs, sparklines, top issues, bottlenecks, hints) and the pane refreshed if it is open. |
| `pane` / `pane close` | Open (or focus) the live pane; close it. |
| `issues`, `health`, `patterns`, `agents`, `context`, `timeline` | The pane's tabs drawn inline, each at most `--limit` rows (default 25). |
| `view [<id>] [k=v]… [--limit N] [--json]` | With no id, the view catalog (id, kind, title, options). With an id, that view drawn inline (tables paged to `--limit`, chart data as text sparklines and bars), the full data written to `views/<id>.json` and named in the hidden context. Options are coerced to the view's types (`snapshot_step` to int, `window_limit` as given, `agent` validated against the view's agent list; `main` and `__main__` both name the main agent). |

**Export**

| Subcommand | What the person gets |
|---|---|
| `report [--dark] [--offline] [-o PATH]` | TrajViz's HTML report written to `report.html` in the session's artifact folder (overwritten each time), opened in the browser; `--offline` embeds plotly.js, otherwise the report loads it from Plotly's CDN when opened. |
| `json [-o DIR] [--view ID]… [--with-trajectory]` | Every view (or the named ones) as `<id>.json` plus `index.json`; `--with-trajectory` adds TrajViz's reloadable `trajectory.json` (tens of MB for a long session). |
| `bundle [-o PATH]` | A ZIP of the parent transcript and its `<session-id>/` folder in the layout TrajViz's dashboards accept, for a hosted dashboard. |
| `schemas [DIR]` | The JSON Schema of every view, written as files. |

**Browser**

| Subcommand | What the person gets |
|---|---|
| `dashboard [--port N]` / `dashboard stop` | The Insight dashboard on localhost, opened in the browser with the session already loaded (preload below); the URL is kept for the pane and status line. The dashboard's own AI sidebar and judge need `env` configuration (TrajViz's HTTP client). |
| `converge [--data-root DIR] [--port N]` / `converge stop` | The Converge comparison dashboard; manifests must name files inside the data root (default: this project's sessions directory). |

**Compare**

| Subcommand | What the person gets |
|---|---|
| `compare <session|path> [--anchor-patch P] [--fuzzy-commands] [--reverse]` | `tv.compare(reference=<other>, compared=<current>)`, the Insight dashboard's orientation (`--reverse` flips it), rendered through `comparison_views()`: outcome, alignment, divergence points, milestones, summary. |
| `scorecard <b> [<c>…] [--fuzzy-commands] [--labels a,b,…] [--no-self]` | `compare_runs()` with the current session as the first source and baseline unless `--no-self`; `run_group_views()` drawn. |
| `converge-cli …` | Pass-through to `agent-trajviz-converge` for `--batch`, `--before/--after`, `--token-rate`, `--task-id`, `--output summary`; the JSON report is written to the artifact folder and the summary printed. |
| `labels <sidecar> [<other sidecar>]` | Label views for a sidecar; with two, `label_comparison_views()` as well. |

**LLM** (only on request; each prints its plan first: calls, approximate input tokens, model)

| Subcommand | What the person gets |
|---|---|
| `analyze [question]` | TrajViz's AI trajectory analysis (first-pass question when none given); follow-ups replay the prior exchange. |
| `fixes [--limit N]` | TrajViz's Issues fix judge over the top issues (N ≤ 8): where to change what, with confidence. |
| `label [-o PATH] [--assistant-only] [--model M] [--max-steps N]` | Step labeling to a sidecar (`labels.json` in the artifact folder), then the label views. |

**Setup and other**

| Subcommand | What the person gets |
|---|---|
| `attribution [--reference-root DIR] [--agent A] [--instance I]` | DECAF attribution; meaningful only for `--session <path in a DECAF reference set>`, otherwise TrajViz's reason verbatim. |
| `collect <cursor|opencode|codearts> …` | Pass-through to `agent-trajviz-collect`. |
| `sessions [N]` | Recent sessions of this project. |
| `watch [on|off]`, `statusline [on|off]` | Live refresh of the pane; a one-line summary pinned under the prompt. |
| `setup [--extras …] [--source …]`, `doctor` | Create/refresh the environment; report Python, TrajViz, extras, DECAF, LLM configuration (`config.ready`/`missing`, names only). |
| `help` | The grouped table above, under 30 lines. |

Unknown input answers one line ("Unknown subcommand 'isues'. Did you mean
'issues'? `/obs help` lists everything."). Arguments are parsed by the mod
(shell-like quoting honoured); the bridge receives an argv, never a shell string.

### What the bridge adds on top of TrajViz *(my call)*

TrajViz reads only the conversation records of a transcript, and it counts
every user-role record as a human turn: Claude Code's own slash-command rows
(`/obs` included), `isMeta` rows, skill injections and messages from other
agents. The bridge reads three things itself, in one streaming pass over the
session folder's known layout (never a path found inside transcript contents):

- the session **title** from the transcript's `ai-title` record;
- the **real human prompts**: user records that are not `isMeta` and whose
  text does not open with `<`; their count is the card's "prompts" number and
  the first is the task line. The card leads with assistant steps, and the
  snapshot reports TrajViz's total separately as "records".
- **sub-agent names**: `<session-id>/subagents/**/agent-<id>.meta.json` holds
  `description` and `agentType`, used as the agent's label where TrajViz has
  only the id.

These enrichments live beside TrajViz's numbers in the snapshot, clearly
separated, and never alter a TrajViz view.

### Where artifacts go *(my call)*

Everything a subcommand writes goes under
`<cache>/trajviz-obs/sessions/<session-id>/`: `report.html`, `views/`,
`labels.json`, `analyze.json`, `compare/`, `converge/`, `bundle.zip`; never
into the project (no `git status` noise) and never into Claude Code's own
session store (the labeler's default would). `-o PATH` writes elsewhere.
Because the folder is outside the working directory, Claude's Read of a file
there prompts the person, which is the intended check; the tool below gives
the model top-N data without a file read.

### Insight dashboard preload *(my call)*

The stock dashboard has no server-side load. The bridge's `dashboard`
subcommand builds `agent_trajviz.dashboard.insight_app()`, re-enters the Gradio
`Blocks`, adds a page-load event that sets the trajectory file control to the
session file (served through `allowed_paths`) and clicks the dashboard's own
Load button from the page's script, then launches with
`prevent_thread_lock=True`, prints `{"url": …}` as its first stdout line and
blocks until its stdin closes. Because the file is read at its real path, the
sub-agent tree is discovered as on the command line. Verified in a browser on
2026-10-22 against agent-trajviz 0.2.0 and gradio 6.30 (the page opened with
the session loaded, no click needed). If a TrajViz release breaks the preload,
the dashboard still launches and the row names the `bundle` ZIP to drop in.

The mod starts the child in a detached `for await` loop that the command hook
does not await, keeps the stream handle in a module-level map keyed by kind
(insight, converge) so `stop` calls `return()`, records the URL in `$.state`,
and answers the command as soon as the URL line arrives (or with the child's
first error line, for example a busy port).

### What the model reads *(my call)*

`command.run` answers `{ text, context }`. `text` is the compact markdown
diagnosis (about 40 lines at most): a provenance line (`obs snapshot 12 ·
16:40:12 · 311 assistant steps · window 1,000,000`), health verdicts, headline
numbers, top issues with step numbers **and the evidence TrajViz has** (tool
name, command or path excerpt), bottlenecks, hints, and TrajViz's load
warnings. `context` carries one hidden note naming the artifact folder and the
`obs` tool with its arguments, so the model can dig in on request.

### Model-callable tool *(my call)*

The mod registers one tool, `obs` (listed as `mcp__trajviz-obs__obs`, behind
ToolSearch), served by the same bridge: input `{ what: "brief" | "issues" |
"health" | "view", view?, options?, limit? }`, output top-N JSON (at most
64 KB) plus the path of the full file. It takes no session argument: it reads
the selected session, so the model cannot pull previews out of other
sessions' transcripts on its own. Same data as `/obs`, never a new computation
path.

### Terminal visualization

Drawn by `ui.render` hooks:

- **Overview card** (the `CommandOutput` site for `obs`): health chips coloured
  by verdict; KPI row (assistant steps, prompts, agents, tokens with
  fresh/cache/output split, tool calls and success, wall clock, cost and live
  context fill from `$.session.usage()`); sparklines as one `Raster` each
  (tokens per step by segment, step durations with outliers marked, context
  occupancy per turn with compactions marked); tool-usage bars; phase ribbon;
  agent swimlane; issues with evidence. Each card reads **its own snapshot**:
  snapshots live in a `StateFamily` keyed by snapshot id, the provenance line
  of `text` carries the id, the hook parses it from `e.props.text` and reads
  that member, never the latest; the last five are kept. The button row
  `[Pane] [Report] [Dashboard] [JSON] [Fixes] [Analyze]` is drawn only where it
  can be pressed (`e.viewport.isFullscreen === true` or a non-terminal
  surface); elsewhere the card ends with a dim line of the equivalent `/obs`
  forms.
- **Breakpoints** by `e.viewport.columns`: ≥ 100 the full layout; 72–99 KPIs on
  two rows, sparklines stacked full width, no swimlane; < 72 chips, a KPI list,
  the top three issues, no Raster.
- **Pane** (`$.ui.open({ id: "obs" })`): tabs by hotkey — `1` Overview,
  `2` Tools, `3` Issues, `4` Patterns (sequences, failures, clusters, chains,
  anti-patterns, regressions, compactions), `5` Agents, `6` Context (TrajViz's
  history and compactions beside the engine's own `/context` breakdown from
  `$.session.usage({ breakdown: "summary" })`), `7` Timeline (workflow steps
  with role, category, errors); `r` refresh, `w` watch, `o` report,
  `d` dashboard. Sized to `e.props.bodyColumns`; below 48 columns tab 1 is a
  KPI list plus a line saying to drag the dock wider for tables. The pane
  opens unasked only when `presentation.isFullscreen && columns >= 140`,
  requesting `columns: 56`; `/obs pane` opens it anywhere (`rows: 14` inline).
  The pane and the status line read the latest snapshot.
- **Surfaces:** `Raster` exists only on the terminal; on desktop, VS Code and
  mobile the same data is drawn with block-glyph text sparklines and
  `Markdown` tables. Computation needs the CLI's `$.process`; where a host
  withholds it, `/obs` answers one line saying the session's host runs no
  commands and the data is available from a terminal session.
- **Status line** (optional): `obs · ctx 24% · 49 tools 100% · 2 issues`, the
  context figure from `$.session.usage()`.
- **Context numbers:** the card's context fill is the main agent's latest
  occupancy over the window (peak shown separately); when TrajViz reports the
  window as unknown the card shows `ctx ?` with TrajViz's note.

### LLM routing *(my call)*

Two modes, userConfig `llm`:

- `session` (default): the bridge captures exactly the prompts TrajViz would
  send and later replays the replies, through the public `chat=` callable:
  `llm-prompt analyze|fixes` calls `llm.ask(run, question, history, chat=capture)`
  or `llm.suggest_fixes(run, chat=capture, limit=N)` where `capture(system,
  messages)` records each already-redacted pair and returns a syntactically
  valid placeholder (fixes: `{"where":"","fix":"","also":"","confidence":"low"}`),
  so every prompt comes out of one pass; `llm-apply` re-runs the same call
  with `chat=replay` returning reply *i* on call *i* (issue order is rank
  order, deterministic for the same transcript, which `llm-apply` checks by
  hash) and prints TrajViz's result. `llm-prompt label` / `llm-apply label`
  do the same through the compat adapter. The mod runs the prompts through
  `$.model.complete({ model, system, prompt, maxTokens, effort, timeoutMs })`
  with `maxTokens` as TrajViz's own cap for that feature (analysis 2048, judge
  2048, label 1024), `model` = `options.analysisModel` (default `sonnet`;
  `labelModel` default `haiku`; `session` names the session's model), and an
  `isAnswered: false` result reported in TrajViz's own words for a failed
  request. A follow-up `analyze` renders the prior exchange (kept in
  `analyze.json`) as leading prompt blocks, the stable brief first with
  `cache: true` on its last block. No API key is needed.
- `env`: TrajViz's own HTTP client with `ANALYZE_*` / `LABEL_*`
  (`llm.config_from_env(load_dotenv=True)` in the session's working directory).

**Cost discipline:** every LLM subcommand first prints its plan in the row
(`analyze: 1 call · ~7k input tokens · model sonnet`; `fixes: up to 8 calls`;
`label: 312 calls · model haiku`), asks through `$.ui.ask` when the plan
exceeds 4 calls in an interactive session (a dismissed ask is no), proceeds
only up to `labelMaxSteps` without asking in a headless one, and prints the
summed usage (input, cache, output tokens) when done.

### Live updates *(my call)*

`turn.complete` on the main loop (`e.agentId === undefined`) re-runs the
snapshot while the pane is open or `watch`/`statusline` is on, never
concurrently, no sooner than `max(3 s, 10 × the last snapshot's wall time)`
after the previous run, and only when the transcript's size or mtime changed.
The snapshot subcommand computes only the views the UI draws (`only=`), and
records its own `durationMs`, shown dimly on the card. Above 32 MB of
transcript, watch degrades to manual (`statusline` shows `obs · large session ·
/obs to refresh`) and says so once. Debouncing uses `$.clock.after` and
timestamps in `$.state`, never `$.clock.sleep`.

### Error and degraded states

| State | Row text |
|---|---|
| no transcript yet | `No transcript for this session yet. Send one prompt, then run /obs.` |
| no assistant steps (`LoadError` `unknown_format`/`no_steps` on the current session) | `Nothing to analyze yet: this session has no assistant steps. Run /obs after the first turn.` (exit 0, no card) |
| ambiguous `--session` prefix | the matching ids with their start times |
| TrajViz load error | TrajViz's message verbatim, with the path |
| environment missing | the setup line (above) |
| host withholds `$.process` | one line, as above |
| partial load (skipped or unreadable sub-agent logs) | TrajViz's `warnings` on the provenance line |

### Privacy and safety

All computation is local. Dashboards bind 127.0.0.1 and TrajViz's telemetry is
off (TrajViz's own default). LLM prompts are TrajViz's redacted brief and
issue windows only. The default HTML report loads plotly.js from Plotly's CDN
when opened in a browser; `--offline` embeds it. The bridge never follows
paths found inside a transcript. Child processes are tied to the session:
spawned dashboards end with the module (unload, `/reload-plugins`, a config
change, session end) or `stop`. Under SSH (`SSH_CONNECTION` or `SSH_TTY` set)
the mod does not open a browser; it prints the path or URL with a one-line
port-forward hint, and `report` always prints its path.

### userConfig

| Field | Type | Default | Meaning |
|---|---|---|---|
| `trajvizSource` | string | `agent-trajviz>=0.2.0,<0.3` | pip requirement or local checkout path |
| `python` | string | `""` | interpreter to build the environment with |
| `llm` | string, options `session`/`env` | `session` | how LLM features run |
| `analysisModel` | string | `sonnet` | model for `analyze`/`fixes` (`session` = the session's model) |
| `labelModel` | string | `haiku` | model for `label` |
| `labelMaxSteps` | number | `50` | labeling cap without a confirmation |
| `watch` | boolean | `true` | live refresh while the pane is open |
| `statusline` | boolean | `false` | the pinned summary line |
| `openBrowser` | boolean | `true` | open reports and dashboards in the browser |

None is required or sensitive. A change reloads the module (and so ends a
running dashboard).

### Tests and checks

- `claude plugin validate .` clean (`--strict` too, hence `author` filled).
- `tsc -p .` clean after one interactive `claude --plugin-dir .` load lays
  `.claude-plugin/types/` (engine-written, untracked); CI copies the skill's
  `types/claude-code.d.ts` there before type-checking.
- `claude plugin test .`: argument parsing; session resolution (state, derived
  path, scan); the overview tree on terminal and desktop from a fixture
  snapshot with `process.run` answered by the test, including the breakpoints
  and the no-buttons form; a card keeping its own snapshot after a newer one;
  pane tabs; watch gating; the `obs` tool; LLM plan lines and `isAnswered:
  false` handling with `model.complete` answered by the test.
- `pytest bridge/tests`: every subcommand against real `agent-trajviz`, using
  TrajViz's sanitized native fixture vendored into `bridge/tests/fixtures/`
  (`django__django-11477.jsonl`, MIT, with its sanitization note) plus
  synthetic session trees the tests write (nested `<id>/subagents/<dir>/agent-*.jsonl`,
  `.meta.json` sidecars, `ai-title` and `isMeta` rows, a truncated last line,
  a session with no assistant step), capture/replay for all three LLM features
  with canned replies, the window override reaching the issues and the
  report, comparison and scorecard, output-size rules, and error envelopes.
- Smoke: `claude -p "/obs doctor" --plugin-dir .` and `claude -p "/obs
  --session <fixture path>" --plugin-dir .` run headless and print the
  command's text (a fresh headless session has no assistant steps of its own).

## Known limitations to state in the README

- TrajViz's tool timings come from transcript intervals (this build's
  transcripts carry no per-call durations), and a `Workflow` spawn is not
  recognised as a spawn tool, so throughput and tool-wait figures are skewed
  in workflow-heavy sessions.
- TrajViz's own step total counts Claude Code's bookkeeping rows as user
  turns; the card shows assistant steps and real prompts, and names the total
  "records".
- `finish` is empty for most sub-agent steps; the plugin never infers an end
  from it.
- The HTML report and the dashboards analyze the first 2,000 steps; the
  overview, views and JSON export use every step.
- DECAF attribution needs gold reference data and refuses merged multi-file
  runs (a session with sub-agents), so for a live session it reports why it
  cannot run rather than a verdict.
- Offloaded tool results (`<session-id>/tool-results/*.txt`) are seen through
  their transcript previews only.
- `docs/upstream-notes.md` lists the TrajViz additions that would retire the
  compat shims and these caveats: a `chat=` for the labeler, a public window
  override on `load()`, skipping command and `isMeta` rows, a spawn-tool
  entry for `Workflow`, reading the session's own tool-result sidecars and
  `.meta.json` names, and a server-side dashboard preload.

## Left to the builder

- Exact visual layout, colours (theme keys so dark and light themes both
  read), glyph choices, and the downsampling rule for sparklines wider than
  the available columns.
- The snapshot document's exact schema (hand-picked from the views the UI
  draws, series capped, tables top-N; under ~300 KB for a 2,000-step session,
  and bounded on any session).
- Argument parser details, help text wording.
- Windows specifics beyond the venv path and the browser-open command.
