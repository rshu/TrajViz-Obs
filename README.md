# TrajViz Obs

[![Checks](https://github.com/rshu/TrajViz-Obs/actions/workflows/checks.yml/badge.svg?branch=main&event=push)](https://github.com/rshu/TrajViz-Obs/actions/workflows/checks.yml)
[![Plugin version](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Frshu%2FTrajViz-Obs%2Fmain%2F.claude-plugin%2Fplugin.json&query=%24.version&label=plugin&prefix=v&color=blue)](https://github.com/rshu/TrajViz-Obs/blob/main/.claude-plugin/plugin.json)
[![License](https://img.shields.io/github/license/rshu/TrajViz-Obs)](LICENSE)

Visualize and diagnose Claude Code sessions with [TrajViz](https://github.com/rshu/TrajViz). Use `/obs` for a quick health check and `/obs dashboard` to explore the session, inspect individual steps, and compare runs.

Overview, diagnostic views, and exports run locally without model calls. Optional `/obs analyze`, `/obs fixes`, and `/obs label` use a model and may incur usage charges.

## Install

You need:

- Claude Code with function hooks (mods) and process execution available. Validated on **2.1.308**; other versions need compatibility checks. Hosts limited to shell hooks and Markdown commands cannot run this plugin.
- [uv](https://docs.astral.sh/uv/) or **Python 3.11+**, with network access to install packages on first use. uv can provision Python automatically.

In Claude Code, run:

```text
/plugin install trajviz-obs --marketplace rshu/TrajViz-Obs
```

Confirm adding the marketplace, choose an installation scope, and review the options. Restart Claude Code after installation.

The first `/obs` installs `agent-trajviz>=0.2.0,<0.3` with its `llm` and `charts` extras in a private environment. Dashboards download the larger `app` extra when needed, with confirmation in interactive sessions. `/obs setup --extras all` installs all extras in advance.

<details>
<summary>Shell installation, updates, and Claude Desktop</summary>

```sh
claude plugin marketplace add rshu/TrajViz-Obs
claude plugin install trajviz-obs@trajviz-obs --scope user
```

To update an existing installation:

```sh
claude plugin update trajviz-obs@trajviz-obs
```

Restart Claude Code after installing or updating. In Claude Desktop's Code tab, use the Plugins menu to add `rshu/TrajViz-Obs` and install `trajviz-obs`; the same function-hook requirement applies.

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
| Export data | `/obs json --with-trajectory`, `/obs bundle` |
| Find or compare sessions | `/obs sessions 20`, `/obs compare <session-id>` |
| Compare several runs | `/obs scorecard <session-b> <session-c>` |
| Open the comparison dashboard | `/obs converge` |
| Ask for model analysis or fixes | `/obs analyze [question]`, `/obs fixes` |
| Classify steps | `/obs label --max-steps 20` |
| Collect another agent's session | `/obs collect <cursor\|opencode\|codearts> --help` |
| See all commands and options | `/obs help` |

In the dashboard, start with **Overview → Summary** for issues and health. Use **Contents** for tools, agents, context, diagnostics, and labels. Click an issue's step number to inspect it in **Workflow**. Use the other tabs for patterns, attribution, comparisons, and raw data. The selected session and its cached labels load automatically, along with Claude Code's context window for the current session. Expand **Session source and labels** to reload or choose another file.

`/obs converge` supports pairwise, batch, and before/after analysis. `/obs view` lists registered views; `/obs json` exports their data, and `/obs schemas` exports their schemas. `/obs attribution` and the dashboard's **Attribution** tab use the optional attribution setup described below.

<details>
<summary>Step labeling and label comparisons</summary>

```text
/obs label --max-steps 20
/obs dashboard
```

The labeler classifies assistant steps by phase and action, preserves their source indices, and assigns user steps deterministic labels. Run `/obs dashboard` after labeling to load the saved sidecar into the phase charts. Assistant steps beyond the cap remain `unknown`; user steps keep their deterministic labels.

Use `--assistant-only` to keep only assistant records, `-o /path/to/labels.json` to choose the output, or `--model haiku` to override the model. Only the default session-cache `labels.json` loads automatically; upload sidecars saved elsewhere through **Labels (optional)**.

Custom labeling accepts `--taxonomy /path/to/taxonomy.md`, `--user-phase <tag>`, and `--user-action <tag>`. Use `--max-content-chars` to bound each step's prompt and `--max-tokens` for the response limit. In `env` mode, `--delay` sets pacing (0–3600 seconds), `--temperature` controls sampling, and failed classifications may retry once. Session mode requires zero delay because waits count against Claude Code's hook time limit.

`/obs labels <labels.json> [reference-labels.json]` exports all six label views and, with a reference, both comparison views. In the dashboard's **Comparison** tab, load each run's sidecar to include its phases.

</details>

The `/obs` card shows actions, metrics, and ranked findings. The terminal pane has seven tabs: Overview, Tools, Issues, Patterns, Agents, Context, and Timeline. Use `1`–`7` to switch, `r` to refresh, `w` to toggle watch, and `d` for the dashboard. Close it with `/obs pane close`.

Watch refreshes an open pane after main-session turns. `/obs watch off` disables watch; `/obs statusline on` pins a summary under the prompt. Turn both off to stop automatic refreshes.

Session commands accept `--session <id|unique-prefix|path>`, for example `/obs --session /path/to/session.jsonl`. Run `/obs` without a selector to return to the current session.

## Configure

Change plugin options in Claude Code's `/config` menu. From a shell, `claude plugin configure trajviz-obs@trajviz-obs` shows current options; its `--values-stdin` flag accepts a JSON object of string values for scripted updates.

<details>
<summary>Options and manual configuration</summary>

| Option | Default | Purpose |
| --- | --- | --- |
| `trajvizSource` | `agent-trajviz>=0.2.0,<0.3` | Pip requirement, URL, or local checkout. Changing it rebuilds the environment; local paths install editable. |
| `python` | empty | Override the interpreter; otherwise use uv or Python 3.11+. |
| `llm` | `session` | Use Claude Code's model client, or `env` for TrajViz's `ANALYZE_*` / `LABEL_*` settings. |
| `analysisModel` | `sonnet` | Model for `analyze` and `fixes` when `llm=session`; `session` follows the main model. |
| `labelModel` | `haiku` | Model for `label` when `llm=session`; `session` follows the main model. |
| `labelMaxSteps` | `50` | Maximum assistant steps to classify in a headless session. |
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

<details>
<summary>Environment model providers</summary>

With `llm` set to `env`, configure the provider in the environment or the project's `.env`:

```dotenv
ANALYZE_BASE_URL=https://provider.example/v1
ANALYZE_API_KEY=your-api-key
ANALYZE_MODEL=your-model
```

Labeling reads `LABEL_BASE_URL`, `LABEL_API_KEY`, and `LABEL_MODEL` first, falling back to the analysis settings. Set `ANALYZE_PROVIDER` or `LABEL_PROVIDER` to `openai` (default) or `anthropic` for the endpoint's protocol. In env mode, `analysisModel` and `labelModel` are ignored; `/obs label --model <name>` still overrides the labeling model.

</details>

<details>
<summary>Optional attribution setup</summary>

Start Claude Code with `TRAJVIZ_ATTRIBUTION_PATH=/path/to/attribution-checkout` and `TRAJVIZ_REFERENCE_ROOT=/path/to/reference-data` in its environment. Reference data must contain the task's requirements, patch, canonical trajectory, and a failed evaluation outcome. Passing cases are refused. An uploaded trajectory must match its canonical reference file byte for byte.

Use `/obs attribution` or the dashboard's **Attribution** tab. Incompatible attribution parsers show an explanation instead of issuing a capability verdict.

</details>

## Data and model use

`/obs analyze`, `/obs fixes`, and `/obs label` send redacted prompts to the configured model. They show planned calls, estimated input tokens, and the model before running. Interactive sessions ask before more than four calls; headless labeling is capped by `labelMaxSteps`.

The dashboard's AI sidebar and fix button always use the environment provider settings above, independently of `llm`. Opening the sidebar starts analysis; follow-up questions and fix requests send trajectory excerpts to that provider. Set `llm=session` to run `/obs analyze` and `/obs fixes` through Claude Code's model client.

Exports, bundles, cached prompts, and analysis history contain session data and stay on the host unless you share them. Dashboards bind to `127.0.0.1` and stop on request, plugin reload, or session end. TrajViz's own **Export HTML** button remains available inside the dashboard.

## Troubleshooting and limits

Run `/obs doctor` to check the environment, or `/obs setup` to install it. If `/obs` is missing, check Claude Code's debug log for disabled function hooks or an API mismatch. If a dashboard opens empty, export `/obs bundle` and load the ZIP manually. Over SSH, forward the displayed dashboard port; set `openBrowser` to `false` to disable browser opening.

The cache defaults to `~/.cache/trajviz-obs/`, or `$XDG_CACHE_HOME/trajviz-obs/`. On Windows without `HOME`, it uses `%LOCALAPPDATA%/trajviz-obs/`. Close Claude sessions using the plugin before deleting the cache.

- Timing and tool wait are transcript-based estimates. Workflow-spawned work is not fully recognized, and subagent finish times are often unavailable.
- Each refresh reloads the full trajectory. Transcript files above **32 MiB** need manual refreshes.
- Insight's trajectory analysis uses at most the first **2,000 steps**. `/obs json --with-trajectory` retains the full parsed trajectory; individual diagnostics and previews also have limits.
- Attribution requires a separate installation and reference data; merged subagent sessions are unsupported. Parsers that lose native Claude JSONL tool evidence cannot diagnose that format.
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

`hooks/` contains the TypeScript plugin; `bridge/` calls TrajViz from Python. The full check script runs plugin validation, hook tests, type checking, bridge tests, format and command/schema matrices, and lint:

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

Use an empty output directory outside this repository. The harness checks tool failure/recovery, commands, exports, dashboards and cleanup, a real subagent, the plugin tool, and model features. `--skip-dashboard` skips servers. `--skip-llm` skips plugin model features and requires omitting `--test-llm-all`; coding and subagent turns still use Claude.

`scripts/dashboard_e2e.py` tests live Insight and Converge callbacks with a local model stub. `scripts/provider_e2e.py --cache <cache-root> --output <empty-dir>` tests native `/obs` against local OpenAI and Anthropic stubs; the main harness's default cache root is `<output>/cache`. Both avoid external model charges. `scripts/optional_matrix.py --attribution-root /path/to/attribution-checkout` also checks offline attribution against synthetic reference data; the native harness accepts the same flag. Each script's `--help` lists its inputs.

</details>

MIT licensed. TrajViz is a separate MIT-licensed project; this plugin is not an Anthropic product.
