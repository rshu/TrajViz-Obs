# TrajViz Obs

[TrajViz](https://github.com/rshu/TrajViz) inside a running Claude Code session. Type `/obs` to inspect the session's health, token and context use, tools, bottlenecks, issues, and subagents. Open a live pane for the same data, export reports, or compare sessions without leaving Claude Code.

The overview and diagnostic views run locally and do not make model calls. `analyze`, `fixes`, and `label` are explicit model features and can incur usage charges.

## Requirements

- Claude Code with **function hooks enabled**. Developed against **2.1.304**, with real-session validation on **2.1.308**. This plugin uses the early access hooks module API, including immediate commands, `$.process`, and rich rendering. An installation that only supports shell hooks and Markdown commands cannot run it. Other Claude Code versions need validation against their own generated API declarations.
- [uv](https://docs.astral.sh/uv/) or Python **3.11+** on the Claude Code host, with access to install Python packages on first use. With uv, a suitable Python can be provisioned automatically.
- The supported analysis engine is **`agent-trajviz>=0.2.0,<0.3`**. The plugin creates a private virtual environment; a global TrajViz installation is unnecessary.

The default install includes TrajViz's `llm` and `charts` extras. TrajViz 0.2 imports chart presenters from its prompt builders, so both are needed for briefs and model features. Dashboards install the larger `app` extra on first use, with confirmation in an interactive session; `/obs setup --extras all` installs it in advance. A host that withholds command execution receives a message explaining that analysis requires a terminal session.

## Install

In Claude Code, run:

```text
/plugin install trajviz-obs --marketplace rshu/TrajViz-Obs
```

Answer `y` to add the marketplace, choose the installation scope, and review the configuration options. In Claude Desktop, use the Plugins menu to add the same GitHub marketplace and install `trajviz-obs`. The hooks module becomes active when the host loads it.

The equivalent shell workflow is:

```sh
claude plugin marketplace add rshu/TrajViz-Obs
claude plugin install trajviz-obs@trajviz-obs --scope user
```

You can also use `claude plugin install trajviz-obs --marketplace rshu/TrajViz-Obs`. After installing from a shell, start or reload the Claude Code session that will use it.

To run a local checkout during development:

```sh
git clone https://github.com/rshu/TrajViz-Obs.git
cd TrajViz-Obs
claude --plugin-dir .
```

The interactive host watches this folder and reloads the module after edits. Use `claude --debug --plugin-dir .` to inspect hooks that fail to load or are skipped. If `/obs` is unavailable, check the debug log for the function hooks feature being disabled or an API mismatch.

## Use

After the session's first assistant response:

```text
/obs
/obs pane
/obs issues
/obs view
/obs view tool_usage --limit 10
/obs report --offline
```

`/obs` adds a snapshot card with a timestamp and a snapshot id. The pane has seven tabs: Overview, Tools, Issues, Patterns, Agents, Context, and Timeline. Use `1`–`7` to switch tabs, `r` to refresh, `w` to toggle watch, `o` for a report, and `d` for the dashboard. Close it with `/obs pane close`. Wide fullscreen terminals can open the pane automatically; ordinary terminals retain a compact card and command hints.

Watch refreshes after main session turns while the pane is open. `/obs watch off` disables it. `/obs statusline on` enables a compact summary under the prompt. Refreshes are serialized, debounced, and skipped when the transcript has not changed. Sessions above 32 MiB require manual refreshes.

Session-bound commands accept `--session <id|unique-prefix|path>`. Use `/obs sessions 20` to find saved sessions, or `/obs --session /path/to/session.jsonl` to inspect one. `/obs` without a selector returns to the current session. A new session with no assistant step reports that there is nothing to analyze yet.

| Task | Commands |
| --- | --- |
| Inspect | `issues`, `health`, `patterns`, `agents`, `context`, `timeline`, `view [id] [k=v]... [--limit N] [--json]` |
| Export | `report [--dark] [--offline] [-o FILE]`, `json [-o DIR] [--view ID]... [--with-trajectory]`, `bundle [-o FILE]`, `schemas [DIR]` |
| Browse | `dashboard [--port N]`, `converge [--data-root DIR] [--port N]`; add `stop` to end either server |
| Compare | `compare <other> [--anchor-patch FILE] [--fuzzy-commands] [--reverse]`, `scorecard <other>... [--labels a,b] [--no-self]`, `converge-cli ...`, `labels <sidecar> [other]` |
| Ask a model | `analyze [question]`, `fixes [--limit N]`, `label [-o FILE] [--assistant-only] [--model M] [--max-steps N]` |
| Maintain | `setup [--extras all\|none\|charts,app] [--source SOURCE]`, `doctor`, `sessions [N]`, `watch [on\|off]`, `statusline [on\|off]`, `help` |
| Advanced | `attribution [--reference-root DIR] [--agent A] [--instance I]`, `collect <cursor\|opencode\|codearts> ...` |

`/obs help` shows the command reference. The model can retrieve a bounded brief, issues, health, or a named view through the deferred `obs` tool (`mcp__trajviz-obs__obs`), discovered using ToolSearch.

## Configuration

Set the plugin's options in Claude Code's `/config` menu. When loading with `--plugin-dir`, settings use `pluginConfigs.trajviz-obs.options`, for example:

```json
{
  "pluginConfigs": {
    "trajviz-obs": {
      "options": { "analysisModel": "session", "openBrowser": false }
    }
  }
}
```

Installed plugins use their full id (`trajviz-obs@trajviz-obs`) as the key. `claude plugin configure trajviz-obs@trajviz-obs` shows or updates those settings.

| Option | Default | Meaning |
| --- | --- | --- |
| `trajvizSource` | `agent-trajviz>=0.2.0,<0.3` | A pip requirement, URL, or local checkout; changing it rebuilds the managed environment. Local paths are installed editable. |
| `python` | empty | Interpreter used to build the environment. Otherwise uv or an available Python 3.11+ is selected. |
| `llm` | `session` | Use Claude Code's model client, or `env` for TrajViz's `ANALYZE_*` / `LABEL_*` configuration. |
| `analysisModel` | `sonnet` | Model for `analyze` and `fixes`; `session` uses the main session's model. |
| `labelModel` | `haiku` | Model for `label`; `session` uses the main session's model. |
| `labelMaxSteps` | `50` | Maximum labeling calls in a headless session. |
| `watch` | `true` | Refresh an open pane after turns. |
| `statusline` | `false` | Keep the summary under the prompt. |
| `openBrowser` | `true` | Open reports and dashboard URLs automatically. |

Model commands show their planned call count, approximate input tokens, and model. Interactive sessions ask before more than four calls; headless labeling is capped by `labelMaxSteps`. Session mode reports token usage after completion. Current-session model requests use a frozen trajectory snapshot, so records appended during the call cannot invalidate the reply; the result retains the original path and hash. Env mode reads the working directory's `.env` through TrajViz and uses its own HTTP client. Analysis and fix prompts use TrajViz's brief and issue windows; labeling sends per-step prompts. TrajViz's redactor runs before these prompts are sent.

The plugin does not upload a transcript as part of an overview, report, or export. A bundle contains session data and is written locally for you to upload. Dashboards bind to `127.0.0.1` and run until stopped, the plugin reloads, or the session ends; closing their process stream terminates the child. Over SSH, reports and URLs are printed without opening a browser; forward the displayed port if needed. Setting `openBrowser` to `false` also disables automatic browser opening.

## Files and troubleshooting

The default cache is `~/.cache/trajviz-obs/`, or `$XDG_CACHE_HOME/trajviz-obs/`. On Windows without `HOME`, it uses `%LOCALAPPDATA%/trajviz-obs/`. The folder holds immutable virtual environments under `envs/<source-key>/<generation>/`, an atomic `state.json` pointer, and `sessions/<session-id>/` artifacts. Setup is locked across processes, and each Claude session pins its own interpreter. Installing another source or extra publishes a new environment without replacing files used by existing sessions. Old generations are retained; to reset the cache, close Claude sessions using the plugin before removing this folder. Reports, JSON views, labels, and analysis history are kept there unless a command specifies an output path. Treat exports and cached prompts as session data.

Run `/obs doctor` for the interpreter, TrajViz version, extras, plugins, and optional DECAF/LLM readiness. It reports missing environment variable names without printing their values. If setup has not run, use `/obs setup`. A one-time `setup --source ...` does not change `trajvizSource`; set the option as well to keep using that source.

## Limits

- Each refresh loads the complete trajectory; TrajViz has no incremental analysis API. The card keeps the most recent five rich snapshots; older rows retain their original text.
- Timing and tool wait are estimates from transcript intervals. Workflow-spawned work is not fully recognized by the pinned TrajViz parser, and most subagent records have no reliable finish time. Offloaded tool results are visible through their transcript previews.
- The plugin distinguishes assistant steps, real prompts, and raw records. Upstream views can still count Claude Code bookkeeping records differently.
- Reports and dashboards show at most the first 2,000 steps under TrajViz's rendering cap. The overview, views, and JSON analysis use the full trajectory, with bounded series and table previews in the UI.
- DECAF needs a separate installation and reference data. It cannot attribute a merged session with subagent files and reports the reason instead.
- Dashboard preload adapts TrajViz's Gradio controls. If those controls change, the dashboard can open empty; the command explains how to load an `/obs bundle` ZIP manually.
- macOS is the real-session test platform. The code includes Linux and Windows interpreter/browser paths, which need platform-specific end-to-end verification.

The adapter boundaries and upstream improvements are described in [docs/upstream-notes.md](docs/upstream-notes.md).

## Development

`hooks/` is the TypeScript hooks module and rendering layer. `bridge/` is a Python CLI that calls TrajViz and emits JSON. The plugin runs the bridge with its managed interpreter and `-I`; it does not parse session JSONL in TypeScript. `types/index.d.ts` declares the snapshot and plugin state contract.

```sh
claude plugin validate --strict .
claude plugin test .
uv sync --project bridge --locked --extra dev
bridge/.venv/bin/python scripts/check.py
```

TypeScript 5.4+ and Node.js are development tools only. For type checking, first load the plugin in an interactive Claude Code session to generate `.claude-plugin/types/`, or pass this build's `plugin-authoring/types` directory with `--api-types`. Install TypeScript locally or pass its CLI path with `--tsc`; the helper also finds an existing npm cache copy and never installs packages. Generated host declarations remain untracked.

The hook tests run through Claude Code's own testing engine with deterministic host and model responses. Python tests use real `agent-trajviz` and the sanitized public fixture whose provenance is recorded in [its README](bridge/tests/fixtures/native_claude_code/README.md). Real-session end-to-end checks are separate from those tests. See [validation details](docs/validation.md) for the scenario, evidence, and platform scope.

To run the paid end-to-end check with your configured Claude Code account:

```sh
python3 scripts/e2e.py --output /tmp/trajviz-obs-e2e-new --model haiku --budget 5
```

The output directory must be empty. The harness creates a scratch coding session, verifies a real failed tool call and its recovery, checks diagnostic and export contents, installs optional extras, and tests reports, session-model analysis, and both dashboards over local HTTP. It checks dashboard stop/restart and session-end port cleanup in the same Claude process. `--skip-llm`, `--skip-report`, and `--skip-dashboard` narrow coverage; `--test-obs-tool` adds a model turn that must actually call the plugin tool; `--test-subagent` verifies a native subagent; `--test-llm-all` also verifies fix judgment and step labeling. The ledger records selected coverage and validation outcomes. Raw events and artifacts remain private session data; do not commit them.

MIT licensed. TrajViz is a separate MIT-licensed project; this plugin is not an Anthropic product.
