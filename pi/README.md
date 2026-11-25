# TrajViz Obs for Pi

Inspect a running Pi session with local diagnostics, colored terminal charts, and the [TrajViz](https://github.com/rshu/TrajViz) browser dashboard. Tested with **Pi 1.5.2 and 1.6.0** and TrajViz **0.5.1**.

## Install

Requires **Node.js 22.19+** with npm, Git, and either [uv](https://docs.astral.sh/uv/) or **Python 3.11+ with `venv` and `pip`**. Check Node with `node --version`.

Install or update [Pi](https://pi.dev/):

```sh
npm install -g --ignore-scripts @earendil-works/pi-coding-agent@1.6.0
```

Install the extension for your user account:

```sh
pi install git:github.com/rshu/TrajViz-Obs
```

For one project only, run `pi install --local git:github.com/rshu/TrajViz-Obs` from that project instead.

Start Pi in the project you want to observe:

```sh
cd /path/to/project
pi
```

If Pi is already running, restart it or run `/reload`. For a project installation, approve Pi's project-settings trust prompt when it appears.

For a first-time Pi setup, use `/login` to connect your provider and `/model` to choose a model, or use your existing provider credentials. Complete a task, then run these commands one at a time:

```text
/obs
/obs pane
/obs dashboard
```

`/obs help` lists commands and `/obs doctor` checks setup. The first diagnostic command installs a private Python runtime. Opening a dashboard adds its dependencies automatically. First-time setup requires network access. Diagnostics run locally without a model call.

For a local checkout, use `pi install /absolute/path/to/TrajViz-Obs`. Keep the checkout in place; source installs need no build.

### Update or remove

For project installations, run update and removal commands from that project after approving its settings in Pi.

Run `/obs update --check` to inspect this plugin’s installation and available revision. Local source checkouts also show the installed and available versions. Wait for Pi to finish its task, then run `/obs update`. Pi-managed Git installations use Pi's package manager; local source checkouts show a confirmation and fast-forward the reviewed revision automatically. Local updates require a clean official checkout on `main` tracking `origin/main`; no Bun build is needed for Pi source installations.

Pi reloads its extensions when the update finishes and Pi is still idle. If new work starts, run `/reload` afterward. Other sessions using the checkout also need `/reload`. Explicit refs, local changes, custom branches, linked worktrees, built local packages, and ambiguous registrations require manual attention. Failed remote checks show the available revision or version as unknown.

If an older plugin does not recognize `/obs update`, run `pi update git:github.com/rshu/TrajViz-Obs` for its managed Git installation, then restart Pi or run `/reload`. Use that native command to repair an incomplete managed update. For a local checkout, run `git pull --ff-only` in the checkout instead; a failed local update reports its repair state under `.git/trajviz-obs-update`.

To remove the Git installation, run `pi remove git:github.com/rshu/TrajViz-Obs`; for a local checkout, pass its path instead. Add `--local` when removing a project installation, then restart Pi or run `/reload`.

## Use

`/obs` shows a compact summary above the editor. `/obs pane` opens a scrollable overlay with **Overview, Tools, Issues, Patterns, Agents, Context, and Timeline**. It closes with **Esc**, leaving your conversation in place.

| Task | Command |
| --- | --- |
| Inspect diagnostics | `/obs issues`, `/obs agents`, `/obs context`, `/obs view` |
| Inspect recorded workflow evidence | `/obs attribution` |
| Open the browser dashboard | `/obs dashboard`; stop with `/obs dashboard stop` |
| Export data | `/obs json`, `/obs bundle`, `/obs collect pi session.jsonl` |
| Save offline HTML | `/obs dashboard --html session.html` |
| Compare sessions | `/obs compare <id-or-path>`, `/obs converge` |
| Ask for analysis or fixes | `/obs analyze [question]`, `/obs fixes` |
| Label steps | `/obs label --max-steps 20` |
| List this project's saved sessions | `/obs sessions 20` |
| Check setup | `/obs doctor`, `/obs setup` |
| Check or install plugin updates | `/obs update --check`, `/obs update` |
| List every command and option | `/obs help` |

The overlay supports **1–7** or **← →** to switch tabs, **↑ ↓ / Page Up / Page Down** to scroll, **r** to refresh, **d** to open the dashboard, and **w** to toggle watch. Escape cancels work while its progress panel is open; `/obs cancel` does the same through an RPC client. Once a plugin update starts applying, let it finish.

Watch refreshes the widget and overlay after turns and tree navigation. `/obs watch off` stops automatic refreshes. `/obs statusline off` removes the footer summary. The browser dashboard loads the latest private capture when you press **Load** or reload its page; rerunning `/obs dashboard` also loads the latest capture.

Add `--session <id|prefix|path>` to inspect another session, for example `/obs pane --session "/path/to/session.jsonl"`. Imported captures also support `--session-id <id>`. Running `/obs` without a selector returns to the current session. Active-tree navigation and unsaved sessions use Pi's in-memory state, so abandoned branches do not inflate the selected run's metrics. A Pi collection retains the complete tree and active selection. Forks include their inherited branch history; elapsed time covers the displayed steps.

AI analysis, fixes, and labeling use Pi's selected model and authentication by default. They preview their call count and estimated input size; more than four calls require confirmation. Requests can incur model charges. The dashboard's AI settings are separate. See [data and model use](https://github.com/rshu/TrajViz-Obs#data-and-model-use).

![Pi diagnostics in light mode, rendered from a real terminal recording](https://raw.githubusercontent.com/rshu/TrajViz-Obs/main/assets/demo-pi-overview-light.jpg)

## Configure

Optional settings go in `~/.pi/agent/trajviz-obs.json`, or `$PI_CODING_AGENT_DIR/trajviz-obs.json` when using a custom agent directory. Create the file if it is missing. Project settings in `.pi/trajviz-obs.json` override matching global options. Run `/reload` after editing these files.

```json
{
  "watch": true,
  "statusline": true,
  "openBrowser": false,
  "dashboardPort": 0,
  "llm": "session",
  "labelMaxSteps": 50
}
```

| Option | Default |
| --- | --- |
| `watch`, `statusline` | `true` |
| `openBrowser` | `false`; open dashboard URLs automatically when enabled |
| `dashboardPort` | `0` for automatic selection |
| `llm` | `session`; use `env` for the shared `ANALYZE_*` / `LABEL_*` provider settings |
| `analysisModel`, `labelModel` | Selected Pi model; optionally set `provider/model` or `session` |
| `labelMaxSteps` | `50` |
| `python`, `trajvizSource`, `cacheRoot` | Automatic Python discovery, `agent-trajviz==0.5.1`, and `~/.cache/trajviz-obs/pi` |

Change the dashboard port during a session with `/obs dashboard port 7863`. Use `auto` for automatic selection or `default` to restore the file setting. `/obs dashboard --port N` overrides it for one launch.

Usage telemetry is on by default; saved opt-outs are preserved. `/obs disable_telemetry` stops recording and pauses uploads, `/obs enable_telemetry` turns it back on, and `/obs telemetry` checks delivery. The collector defaults to `127.0.0.1:8765`. Change it during a session with `/obs telemetry_destination https://telemetry.example.com`. The choice is separate from Claude Code and OpenCode. See [telemetry configuration and reports](https://github.com/rshu/TrajViz-Obs/blob/main/telemetry/README.md).

## Limits

For experimental local request snapshots and composition diffs, see [read-only prompt capture](../PROMPT_CAPTURE.md). Capture is off by default and separate from usage telemetry.

- Pi's terminal overlay uses Pi's native UI APIs. RPC clients receive command results as notifications. Print and JSON modes do not have a slash-command result UI; use interactive Pi or an RPC client for `/obs`.
- Pi subagents are implemented by extensions. Nested tool records are preserved, but child processes are not presented as confirmed subagents without explicit relationships. A fork is session ancestry.
- Timing uses Pi's native durations when available and recorded timestamps otherwise. Missing evidence appears as unknown. Cost estimates supplied by Pi depend on its configured model pricing.
- Automatic captures are capped at 64 MiB. Inspect larger saved sessions with `--session <path-to-session.jsonl>`. The browser dashboard shows at most 2,000 steps. JSON and bundle exports retain the complete parsed trajectory. Keep exports private when they contain project data.
- Real-session validation covers macOS. Linux and Windows need platform-specific runtime testing.

## Development

```sh
cd pi
bun install --frozen-lockfile
bun run build
bun run typecheck
bun test
```

The package build bundles the adapter and shared Node helpers, copies the Python bridge, and keeps Pi's host libraries external. `npm pack` produces a standalone package without development files. Tests use the installed bridge environment when available; set `TRAJVIZ_OBS_TEST_PYTHON` to choose its interpreter. Test artifacts stay outside the repository.
