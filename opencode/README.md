# TrajViz Obs for OpenCode 2

Inspect session health, tools, subagents, context use, and step labels with `/obs`. The browser dashboard and Python analysis engine are shared with the Claude Code plugin.

## Install

Requires **OpenCode 2.2.2**, [Bun](https://bun.sh), and [uv](https://docs.astral.sh/uv/) or **Python 3.11+**.

```sh
git clone https://github.com/rshu/TrajViz-Obs.git
cd TrajViz-Obs/opencode
bun install --frozen-lockfile
bun run build
```

Add the adapter's absolute directory to `opencode.json`:

```json
{
  "plugins": ["/absolute/path/TrajViz-Obs/opencode"]
}
```

Restart OpenCode. The first `/obs` installs a private TrajViz runtime; opening a dashboard adds its dependencies. Initial setup needs network access. Update the checkout, repeat the install/build commands, and restart to update.

## Use

After an assistant response, run `/obs` for a summary, `/obs pane` for the live panel, or `/obs dashboard` for the browser dashboard. Use the displayed **Open URL** action; remote servers need port forwarding.

The panel has Overview, Tools, Issues, Patterns, Agents, Context, and Timeline tabs. `/obs pane close` closes it. `/obs watch off` disables automatic refresh; `/obs statusline on` adds a compact footer. `/obs cancel` cancels a running command.

Use `/obs help` for all commands. Diagnostics, exports, comparisons, analysis, fixes, step labeling, and optional attribution follow the [shared usage guide](https://github.com/rshu/TrajViz-Obs#use). `/obs sessions` lists this project's saved sessions; session commands accept `--session <id|prefix|path>`.

For a saved OTLP capture, use `/obs sessions --session /path/to/capture`, then `/obs --session /path/to/capture --session-id <id>`. Each selected session has separate artifacts and model history. Refresh imported sessions manually after their inputs change. TrajViz **0.3.1** provides all **36 views**, including `/obs view data_quality`; view JSON uses schema version **2**.

## Options

Set options on the plugin entry:

```json
{
  "plugins": [{
    "package": "/absolute/path/TrajViz-Obs/opencode",
    "options": { "watch": true, "statusline": false, "openBrowser": false }
  }]
}
```

`llm` defaults to `session`, using OpenCode's selected model. `analysisModel` and `labelModel` accept `session` or `provider/model`. Model commands show their call plan and ask before more than four calls; labeling defaults to 50 steps (`labelMaxSteps`). Native generation uses OpenCode's configured output settings and does not return per-call usage. Per-request `--max-tokens` and `--temperature` require `llm: "env"` and the [environment provider settings](https://github.com/rshu/TrajViz-Obs#configure).

`python` overrides interpreter discovery; `trajvizSource` overrides the TrajViz requirement. `cacheRoot` moves the private runtime and session artifacts. Dashboards bind to localhost and stop with `/obs dashboard stop`, `/obs converge stop`, or plugin unload.

The TUI reads full history from its connected server. Headless tool calls discover OpenCode's local service; custom servers can set `serverUrl` to a loopback URL on that server's host and `serverPasswordEnv` (default `OPENCODE_PASSWORD`). History before compaction is retained. Forks remain separate runs; confirmed subagent sessions appear in the parent trajectory.

Live captures are limited to 32 MiB and 64 sessions in one family. For larger inputs, export the session and use `--session /path/to/export.json`.

## Development

```sh
bun run typecheck
bun test
npm pack
```

The package includes the OpenCode adapter and shared Python runtime. Keep real-session fixtures, logs, and screenshots outside the repository. OpenCode 1 and desktop GUI extensions are outside this adapter's scope.
