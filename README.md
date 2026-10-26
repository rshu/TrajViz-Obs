# TrajViz Obs

[![Checks](https://github.com/rshu/TrajViz-Obs/actions/workflows/checks.yml/badge.svg?branch=main&event=push)](https://github.com/rshu/TrajViz-Obs/actions/workflows/checks.yml)
[![Plugin version](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Frshu%2FTrajViz-Obs%2Fmain%2F.claude-plugin%2Fplugin.json&query=%24.version&label=plugin&prefix=v&color=blue)](https://github.com/rshu/TrajViz-Obs/blob/main/.claude-plugin/plugin.json)
[![License](https://img.shields.io/github/license/rshu/TrajViz-Obs)](LICENSE)

Visualize and diagnose Claude Code sessions with [TrajViz](https://github.com/rshu/TrajViz). Use `/obs` for a quick health check and `/obs dashboard` to explore the session, inspect individual steps, and compare runs.

Diagnostics run locally without model calls. Optional `analyze`, `fixes`, and `label` commands use a model and may incur usage charges.

## Install

You need:

- Claude Code with function hooks (mods) and process execution available. Validated on **2.1.308**; other versions need compatibility checks. Hosts limited to shell hooks and Markdown commands cannot run this plugin.
- [uv](https://docs.astral.sh/uv/) or **Python 3.11+**, with network access to install packages on first use. uv can provision Python automatically.

In Claude Code, run:

```text
/plugin install trajviz-obs --marketplace rshu/TrajViz-Obs
```

Confirm adding the marketplace, choose an installation scope, and review the options.

The first `/obs` installs `agent-trajviz>=0.2.0,<0.3` with its `llm` and `charts` extras in a private environment. Dashboards download the larger `app` extra when needed, with confirmation in interactive sessions. `/obs setup --extras all` installs all extras in advance.

<details>
<summary>Install from a shell or Claude Desktop</summary>

```sh
claude plugin marketplace add rshu/TrajViz-Obs
claude plugin install trajviz-obs@trajviz-obs --scope user
```

Start or reload your Claude Code session afterward. In Claude Desktop's Code tab, use the Plugins menu to add `rshu/TrajViz-Obs` and install `trajviz-obs`.

</details>

## Use

After the session's first assistant response:

```text
/obs
/obs dashboard
```

| Task | Command |
| --- | --- |
| Inspect issues or subagents | `/obs issues`, `/obs agents` |
| Browse diagnostic views | `/obs view`, `/obs view tool_usage --limit 10` |
| Open the full dashboard | `/obs dashboard`; close it with `/obs dashboard stop` |
| Keep a terminal pane open | `/obs pane` |
| Export data | `/obs json`, `/obs bundle` |
| Find or compare sessions | `/obs sessions 20`, `/obs compare <session-id>` |
| Ask for model analysis | `/obs analyze [question]`, `/obs fixes`, `/obs label` |
| See all commands and options | `/obs help` |

In the dashboard, start with **Overview → Summary** for issues and health. Use its **Contents** menu for tools, agents, context, diagnostics, and labels. Click an issue's step number to inspect it in **Workflow**. **Patterns**, **Comparison**, and **Raw Data** provide the rest of TrajViz's single-session features. The session, context-window setting, and saved labels load automatically; expand **Session source and labels** to reload or choose another file.

For larger comparisons, `/obs converge` opens pairwise, batch, and before/after analysis. All registered views are available through `/obs view`, JSON exports through `/obs json`, and schemas through `/obs schemas`. `/obs labels <file> [other-file]` exports all label views and optional label comparisons. DECAF attribution is available through `/obs attribution` and the dashboard when its external checkout and reference data are configured.

The `/obs` card leads with actions, compact metrics, and ranked findings. The terminal pane has seven tabs: Overview, Tools, Issues, Patterns, Agents, Context, and Timeline. Use `1`–`7` to switch, `r` to refresh, `w` to toggle watch, and `d` for the dashboard. Close it with `/obs pane close`.

An open pane refreshes after main-session turns. Use `/obs watch off` to disable this, or `/obs statusline on` for a summary under the prompt.

Session commands accept `--session <id|unique-prefix|path>`, for example `/obs --session /path/to/session.jsonl`. Run `/obs` without a selector to return to the current session.

## Configure

Change options in Claude Code's `/config` menu, or run `claude plugin configure trajviz-obs@trajviz-obs` from your shell.

<details>
<summary>Options and manual configuration</summary>

| Option | Default | Purpose |
| --- | --- | --- |
| `trajvizSource` | `agent-trajviz>=0.2.0,<0.3` | Pip requirement, URL, or local checkout. Changing it rebuilds the environment; local paths install editable. |
| `python` | empty | Override the interpreter; otherwise use uv or Python 3.11+. |
| `llm` | `session` | Use Claude Code's model client, or `env` for TrajViz's `ANALYZE_*` / `LABEL_*` settings. |
| `analysisModel` | `sonnet` | Model for `analyze` and `fixes`; `session` uses the main session's model. |
| `labelModel` | `haiku` | Model for `label`; `session` uses the main session's model. |
| `labelMaxSteps` | `50` | Maximum labeling calls in a headless session. |
| `watch` | `true` | Refresh an open pane after turns. |
| `statusline` | `false` | Show a summary under the prompt. |
| `openBrowser` | `true` | Open dashboard URLs automatically. |

For a local checkout loaded with `--plugin-dir`, settings use the short plugin name:

```json
{
  "pluginConfigs": {
    "trajviz-obs": {
      "options": { "analysisModel": "session", "openBrowser": false }
    }
  }
}
```

Installed plugins use `trajviz-obs@trajviz-obs` as the key. A one-time `/obs setup --source ...` does not update `trajvizSource`; change the option too if you want to keep that source.

</details>

## Data and model use

Overview and exports run locally. `/obs analyze`, `/obs fixes`, and `/obs label` send redacted prompts to the configured model, showing planned calls, estimated input tokens, and model first. Interactive sessions ask before more than four calls; headless labeling is capped by `labelMaxSteps`.

The browser's AI sidebar and fix button use TrajViz's `ANALYZE_*` / `LABEL_*` environment settings or the project's `.env`. They do not use the plugin's `llm=session` client; use `/obs analyze` or `/obs fixes` for that client. Browser model calls send excerpts to that configured provider when requested.

Exports, bundles, cached prompts, and analysis history contain session data and stay on the host unless you share them. Dashboards bind to `127.0.0.1` and stop on request, plugin reload, or session end. TrajViz's own **Export HTML** button remains available inside the dashboard.

## Troubleshooting and limits

Run `/obs doctor` to check the environment, or `/obs setup` to install it. If `/obs` is missing, check Claude Code's debug log for disabled function hooks or an API mismatch. If a dashboard opens empty, export `/obs bundle` and load the ZIP manually. Over SSH, forward the displayed dashboard port; set `openBrowser` to `false` to disable browser opening.

The cache defaults to `~/.cache/trajviz-obs/`, or `$XDG_CACHE_HOME/trajviz-obs/`. On Windows without `HOME`, it uses `%LOCALAPPDATA%/trajviz-obs/`. Close Claude sessions using the plugin before deleting the cache.

- Timing and tool wait are transcript-based estimates. Workflow-spawned work is not fully recognized, and subagent finish times are often unavailable.
- Each refresh reloads the full trajectory. Sessions above **32 MiB** need manual refreshes.
- Dashboards render at most **2,000 steps**. Overview, views, and JSON analysis use the full trajectory, with bounded UI previews.
- DECAF attribution requires a separate installation and reference data; merged subagent sessions are unsupported.
- Real-session testing covers **macOS**. Linux and Windows still need platform-specific end-to-end validation.

## Development

<details>
<summary>Local checkout, checks, and paid end-to-end tests</summary>

```sh
git clone https://github.com/rshu/TrajViz-Obs.git
cd TrajViz-Obs
claude --plugin-dir .
```

Claude Code reloads the module after edits. Use `claude --debug --plugin-dir .` to investigate loading failures.

`hooks/` contains the TypeScript plugin; `bridge/` calls TrajViz from Python. The full check script runs plugin validation, hook tests, type checking, bridge tests, the command/schema matrix, and lint:

```sh
uv sync --project bridge --locked --extra dev
bridge/.venv/bin/python scripts/check.py
```

Full checks require Node.js, TypeScript 5.4+, and this Claude Code build's API declarations. Load the local plugin in an interactive session to generate `.claude-plugin/types/`, or pass `--api-types` to the check script. Use `--tsc` to select a compiler. Generated declarations stay untracked. Deterministic tests make no external model calls.

To test a real session with your configured Claude Code account (incurs usage charges):

```sh
python3 scripts/e2e.py --output /tmp/trajviz-obs-e2e-new --model haiku --budget 5 \
  --test-command-matrix --test-obs-tool --test-subagent --test-llm-all
```

Use an empty output directory outside this repository. The harness checks tool failure/recovery, commands, exports, dashboards and cleanup, a real subagent, the plugin tool, and model features. `--skip-llm` and `--skip-dashboard` narrow coverage.

</details>

MIT licensed. TrajViz is a separate MIT-licensed project; this plugin is not an Anthropic product.
