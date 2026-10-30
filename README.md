# TrajViz Obs

[![Checks](https://github.com/rshu/TrajViz-Obs/actions/workflows/checks.yml/badge.svg?branch=main&event=push)](https://github.com/rshu/TrajViz-Obs/actions/workflows/checks.yml)
[![Claude Code supported](https://img.shields.io/badge/Claude_Code-supported-D97757)](#install)
[![OpenCode 2 supported](https://img.shields.io/badge/OpenCode_2-supported-3B82F6)](opencode/README.md#install)
[![Claude Code plugin version](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Frshu%2FTrajViz-Obs%2Fmain%2F.claude-plugin%2Fplugin.json&query=%24.version&label=Claude%20plugin&prefix=v&color=blue)](https://github.com/rshu/TrajViz-Obs/blob/main/.claude-plugin/plugin.json)
[![License](https://img.shields.io/github/license/rshu/TrajViz-Obs)](LICENSE)

Visualize and diagnose Claude Code and OpenCode 2 sessions with [TrajViz](https://github.com/rshu/TrajViz). Use `/obs` for a quick health check and `/obs dashboard` to inspect steps, tools, subagents, and context use.

## Install

For **OpenCode 2.2.2**, follow the [OpenCode installation guide](opencode/README.md). The instructions below install the Claude Code plugin.

Requires Claude Code with function hooks (mods) and process execution, plus [uv](https://docs.astral.sh/uv/) or **Python 3.11+**. Validated on Claude Code **2.1.308**; hosts limited to shell hooks and Markdown commands cannot run this plugin.

In Claude Code:

```text
/plugin install trajviz-obs --marketplace rshu/TrajViz-Obs
```

Confirm the marketplace and installation scope, then restart Claude Code. The first `/obs` installs a private TrajViz runtime; dashboards add their dependencies when first opened. Initial setup requires network access.

To update:

```sh
claude plugin update trajviz-obs@trajviz-obs
```

Restart Claude Code after updating.

## Use

After the session's first assistant response:

```text
/obs
/obs dashboard
```

| Task | Command |
| --- | --- |
| Inspect diagnostics | `/obs issues`, `/obs agents`, `/obs view` |
| Open a live terminal pane | `/obs pane`; close with `/obs pane close` |
| Export data | `/obs json --with-trajectory`, `/obs bundle` |
| Compare runs | `/obs compare <session-id>`, `/obs converge` |
| Ask for analysis or fixes | `/obs analyze [question]`, `/obs fixes` |
| Label steps by phase and action | `/obs label --max-steps 20` |
| List all commands and options | `/obs help` |

In the dashboard, start with **Overview → Summary** for health and issues. Click an issue's step number to inspect it in **Workflow**. The selected session and its cached labels load automatically. Close the server with `/obs dashboard stop`.

Find saved sessions with `/obs sessions 20`, then add `--session <id|prefix|path>` to a session command. Run `/obs` without a selector to return to the current session.

Saved OTLP captures can contain several sessions. List them with `/obs sessions --session /path/to/capture`, then select one with `/obs --session /path/to/capture --session-id <id>`. JSON, JSONL, bundle manifests, directories, and ZIP captures are supported. This imports saved telemetry; it does not enable live telemetry collection.

Both plugins use TrajViz **0.3.1**, including all **36 views**, data-quality reporting, and Codex, Pi, and compressed DeepSeek session imports. `/obs view data_quality` explains missing or partial evidence; unavailable measurements appear as unknown. JSON view exports use schema version **2**. Bundles preserve a complete OTLP capture; use `/obs json --with-trajectory` to export only the selected session. Other imported formats may bundle a portable `trajectory.json` that can also be loaded separately.

Watch refreshes an open pane after turns. `/obs statusline on` pins a summary under the prompt. Use `/obs watch off` and `/obs statusline off` to stop automatic refreshes.

## Demo

Claude Code overview and live pane in light mode, rendered from recorded output of a controlled test session:

![Claude Code overview and live pane in light mode](assets/demo-terminal-light.png)

Dashboard summary from the same session:

![Dashboard summary with session health, tool success, and issues](assets/demo-dashboard.jpg)

## Configure

Use Claude Code's `/config` menu for plugin options. Model features default to `llm=session`, using Claude Code's client: `analysisModel=sonnet` for analysis and fixes, and `labelModel=haiku` for labeling. Set either model option to `session` to follow the main conversation's model.

OpenCode uses [plugin options in `opencode.json`](opencode/README.md#options) and defaults to its selected model.

<details>
<summary>Use an external model provider</summary>

Set `llm=env` and add these values to the launch environment or the project's `.env`:

```dotenv
ANALYZE_BASE_URL=https://provider.example/v1
ANALYZE_API_KEY=your-api-key
ANALYZE_MODEL=your-model
```

Labeling uses `LABEL_BASE_URL`, `LABEL_API_KEY`, and `LABEL_MODEL`, falling back to the analysis settings. Set `ANALYZE_PROVIDER` or `LABEL_PROVIDER` to `openai` (default) or `anthropic` to match the endpoint. In this mode, the environment model settings replace `analysisModel` and `labelModel`.

</details>

<details>
<summary>Optional attribution setup</summary>

Start your host with `TRAJVIZ_ATTRIBUTION_PATH=/path/to/attribution-checkout` and `TRAJVIZ_REFERENCE_ROOT=/path/to/reference-data`. Use `/obs attribution` or the dashboard's **Attribution** tab.

Reference data must include requirements, patch, canonical trajectory, and a failed evaluation outcome. The uploaded trajectory must match its reference byte for byte. Unsupported parsers or merged subagent sessions show an explanation instead of a capability verdict.

</details>

## Data and model use

Overview, diagnostic views, and exports run locally without model calls. `/obs analyze`, `/obs fixes`, and `/obs label` send redacted prompts to a model and may incur usage charges. They show the planned calls and model first; interactive sessions ask before more than four calls. Labeling defaults to at most 50 steps.

The dashboard's AI sidebar and fix button use the connection selected in **Settings**, independently of `llm`. Environment values provide defaults. API keys stay in the current dashboard session; remembered browser settings exclude keys. Opening the sidebar starts model analysis; follow-up questions and fixes also send trajectory excerpts.

Exports and cached analysis contain session data and stay on the host unless shared. Dashboards bind to `127.0.0.1` and stop on request or plugin unload.

## Troubleshooting and limits

Run `/obs doctor` to check setup or `/obs setup` to install dependencies. If `/obs` is missing, check the Claude Code debug log for disabled function hooks or an API mismatch. If the dashboard is empty, export `/obs bundle` and load its ZIP manually. Over SSH, forward the displayed port; set `openBrowser=false` to disable browser opening.

- Timing is estimated from transcripts; workflow-spawned work and subagent finish times may be incomplete.
- Operation-based OTLP captures support observed metrics and diagnostics; transcript alignment and capability attribution require supported native transcripts.
- Imported sessions require manual refresh after their inputs change. Claude Code files above **32 MiB** also require manual refresh; OpenCode has [live-capture limits](opencode/README.md#options). Insight analyzes at most the first **2,000 steps**; `/obs json --with-trajectory` retains the full parsed trajectory.
- Real-session validation covers **macOS**. Linux and Windows still need platform-specific end-to-end testing.

## Development

```sh
git clone https://github.com/rshu/TrajViz-Obs.git
cd TrajViz-Obs
claude --plugin-dir .
```

Load the local plugin once to generate Claude Code's API declarations. Checks also require Node.js and TypeScript 5.4+:

```sh
uv sync --project bridge --locked --extra dev
bridge/.venv/bin/python scripts/check.py
```

These checks make no external model calls. For real-session testing, see `python3 scripts/e2e.py --help`; it uses your Claude Code account and incurs model usage. Keep test output outside the repository.

MIT licensed. TrajViz is a separate MIT-licensed project; this plugin is not an Anthropic product.
