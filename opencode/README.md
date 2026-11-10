# TrajViz Obs for OpenCode 2

Inspect session health, tools, subagents, context use, and step labels with `/obs` in OpenCode's terminal or Desktop app. The browser dashboard and Python analysis engine are shared with the Claude Code plugin.

## Install

Requires **OpenCode 2**, [Bun](https://bun.sh) **1.3.0+**, and either [uv](https://docs.astral.sh/uv/) or **Python 3.11+ with `venv` and `pip`**. Tested on terminal **2.2.5** and Desktop **2.2.6**.

### 1. Build the adapter and find its path

```sh
git clone https://github.com/rshu/TrajViz-Obs.git
cd TrajViz-Obs/opencode
bun install --frozen-lockfile
bun run build
pwd
```

If you already have a checkout, update it with `git pull --ff-only`, then run the install/build commands in its `opencode` directory. Copy the absolute directory printed by `pwd`, pointing to this checkout's `opencode` subdirectory.

### 2. Configure Desktop or the terminal

OpenCode Desktop's built-in local server and the terminal use the same global configuration. For **every project** on macOS/Linux, use `~/.config/opencode/opencode.json`. If `opencode.jsonc` already exists in that directory, edit it instead. If neither file exists, create the directory and `opencode.json`:

```sh
mkdir -p ~/.config/opencode
nano ~/.config/opencode/opencode.json
```

If OpenCode is launched with `XDG_CONFIG_HOME`, use `$XDG_CONFIG_HOME/opencode` in place of `~/.config/opencode`.

For a new file, paste the following and replace the example path with your `pwd` output:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "plugins": ["/absolute/path/TrajViz-Obs/opencode"]
}
```

Use **`plugins` (plural)** for OpenCode 2. In an existing file, preserve other settings and add this top-level field, or append the adapter path to its existing array. If TrajViz Obs is already listed, update that entry in place. Save in nano with **Ctrl+O**, **Enter**, then exit with **Ctrl+X**.

Alternatively, enable it for **one project only** by putting the entry in `opencode.json` or `opencode.jsonc` in that project's root directory. See OpenCode's [configuration](https://opencode.ai/v2/docs/config) and [plugin](https://opencode.ai/v2/docs/plugins) guides.

### 3. Restart and verify

#### OpenCode Desktop

1. Quit OpenCode Desktop completely (**Cmd+Q** on macOS), then reopen it.
2. With Desktop's built-in local server selected, open **Settings → Extensions → Plugins**. Find **trajviz-obs** and make sure its switch is on; this enables the Desktop commands and pane.
3. Open a project and session, complete an assistant response, then enter these commands one at a time in the chat composer:

```text
/obs
/obs dashboard
```

`/obs` opens the summary. `/obs dashboard` starts the browser dashboard; click **Open dashboard** in its result to view it. Stop the server with `/obs dashboard stop`.

For an optional live panel beside the conversation, run `/obs pane`; use **Expand** for a larger view.

If the plugin is missing, check the global configuration and the absolute adapter path from step 1, then restart the app. If its Desktop UI fails to load, use **Reload** on the plugin's settings row. See OpenCode's [Desktop plugin guidance](https://opencode.ai/v2/docs/plugins#desktop).

#### OpenCode terminal

Close the TUI. From the project you want to observe, restart OpenCode's background server and reopen OpenCode:

```sh
opencode service restart
opencode
```

For standalone sessions, close and rerun `opencode --standalone`. With `--server`, restart the connected server and reconnect the TUI instead. Complete an assistant response, then run `/obs` and `/obs dashboard`.

In either interface, the first `/obs` installs a private TrajViz runtime and needs network access. Opening a dashboard adds its dependencies. Use `/obs doctor` to check setup or `/obs help` to list commands.

### Update or remove

To update, pull the checkout, repeat `bun install --frozen-lockfile` and `bun run build` in its `opencode` directory, then repeat the restart step above.

To disable the adapter, remove its entry from the configuration you edited and repeat the restart step.

## Use

After an assistant response, run `/obs` for a summary, `/obs pane` for the live panel, or `/obs dashboard` for the browser dashboard. Use **Open dashboard** in Desktop or **Open URL** in the terminal; remote servers need port forwarding.

Use `/obs dashboard --html session.html` to save an offline copy. Add `--dark` for dark charts, or `--inline-plotly` to a live dashboard command to make its browser HTML exports work offline.

The panel has Overview, Tools, Issues, Patterns, Agents, Context, and Timeline tabs. See the [demo screenshots](../README.md#demo). `/obs pane close` closes it. `/obs watch off` disables automatic refresh; `/obs statusline on` adds a compact status summary. `/obs cancel` cancels a running command.

Overview charts show tokens, context, and step time excluding recorded subagent waits. Tools ranks calls and outcomes; Agents shows activity by work step. Charts follow OpenCode's theme and distinguish missing measurements from zero. Set `NO_COLOR=1` for monochrome terminal charts.

Focus the panel to switch tabs with **1–7**. In Desktop, use **Expand** for a larger view; the panel's command field runs additional `/obs` commands. In the terminal, scroll with **↑↓**, **PgUp/PgDn**, or **Home/End**, and toggle fullscreen with **f**.

Use `/obs help` for all commands. Diagnostics, exports, comparisons, analysis, fixes, step labeling, and attribution follow the [shared usage guide](https://github.com/rshu/TrajViz-Obs#use). `/obs sessions` lists this project's saved sessions; session commands accept `--session <id|prefix|path>`.

For a saved OTLP capture, use `/obs sessions --session /path/to/capture`, then `/obs --session /path/to/capture --session-id <id>`. Each selected session has separate artifacts and model history. Refresh imported sessions manually after their inputs change. TrajViz **0.5.0** provides all **36 views**, including `/obs view data_quality`.

## Options

To set options, replace the adapter's existing string entry with an object:

```json
{
  "plugins": [{
    "package": "/absolute/path/TrajViz-Obs/opencode",
    "options": { "watch": true, "statusline": false, "openBrowser": false }
  }]
}
```

`llm` defaults to `session`, using OpenCode's selected model. `analysisModel` and `labelModel` accept `session` or `provider/model`. Model commands show their call plan and ask before more than four calls; labeling defaults to 50 steps (`labelMaxSteps`). Native generation uses OpenCode's configured output settings and does not return per-call usage. Per-request `--max-tokens` and `--temperature` require `llm: "env"` and the [environment provider settings](https://github.com/rshu/TrajViz-Obs#configure).

Desktop remembers watch and statusline toggles in its own preferences, overriding these configuration defaults.

`python` overrides interpreter discovery; `trajvizSource` overrides the TrajViz requirement. `cacheRoot` moves the private runtime and session artifacts. Dashboards bind to localhost and stop with `/obs dashboard stop`, `/obs converge stop`, or plugin unload.

Desktop and the TUI read full history from their connected server. Headless tool calls discover OpenCode's local service; custom servers can set `serverUrl` to a loopback URL on that server's host and `serverPasswordEnv` (default `OPENCODE_PASSWORD`). Set the named password variable in that server's launch environment. History before compaction is retained. Forks remain separate runs; confirmed subagent sessions appear in the parent trajectory.

Live captures are limited to 32 MiB and 64 sessions in one family. For larger inputs, export the session and use `--session /path/to/export.json`.

## Development

From the checkout's `opencode` directory (packaging also requires Node.js/npm):

```sh
bun run typecheck
bun run test
npm pack
```

The package includes terminal and Desktop components plus the shared Python runtime. The Desktop extension uses OpenCode's experimental GUI API. Keep private session fixtures, logs, and test output outside the repository.
