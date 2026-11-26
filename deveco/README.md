# TrajViz Obs for DevEco Code

An experimental adapter for inspecting DevEco Code session trajectories and the prompt components exposed by its legacy plugin hooks. It shares TrajViz's Python analysis engine, browser dashboard, and the read-only Captured components panel with the other adapters.

The adapter targets the plugin interface shipped with **DevEco Code 0.2.6**. It is separate from the OpenCode 2 adapter: DevEco Code uses a **`plugin` array (singular)** and loads the built JavaScript entry. Native panes, OpenCode 2 RPC, automatic updates, and model-assisted analysis/labeling are not provided by this adapter.

## Install from a checkout

Requires DevEco Code, Git, [Bun](https://bun.sh) **1.3.0+**, and either [uv](https://docs.astral.sh/uv/) or **Python 3.11+ with `venv` and `pip`**. The default analysis engine is TrajViz **0.7.1**. First-time dashboard setup needs network access to install the private TrajViz runtime.

From a checkout containing the `deveco` directory:

```sh
cd /absolute/path/TrajViz-Obs/deveco
bun install --frozen-lockfile
bun run build
```

Add the built entry to `~/.config/deveco/deveco.jsonc`. Preserve existing configuration and append to an existing `plugin` array:

```jsonc
{
  "plugin": ["/absolute/path/TrajViz-Obs/deveco/dist/index.js"]
}
```

Keep the checkout and its installed dependencies in place. A project-specific `deveco.jsonc` can use the same entry instead. Quit DevEco Code and relaunch it from the project you want to inspect:

```sh
deveco /absolute/path/to/project
```

## Inspect a session

Complete an assistant response, then ask the agent to invoke the plugin's **`obs` tool**, for example:

```text
Call the obs tool with command "help".
```

```text
Call the obs tool with command "dashboard" and show me the returned local URL.
```

The tool input is `{ "command": "dashboard" }`. Open the returned URL in your browser. This is a custom agent tool; this adapter does not register a native `/obs` terminal command or pane. The host may use its configured coding model to select the tool, but the diagnostic tool itself makes no model calls.

| Tool command | Result |
| --- | --- |
| `help` | Supported commands and syntax |
| `overview` / `brief` | Session summary / brief diagnostics |
| `issues`, `health`, `agents`, `context`, `timeline` | Diagnostic views |
| `view` / `view <id> --limit 25 --offset 0 --json` | View catalog / a paged diagnostic view |
| `view prompt_construction --request-id <id>` | Inspect one request candidate from the ordinary trajectory |
| `dashboard` / `dashboard --port 7863` | Start a localhost browser dashboard |
| `dashboard stop` | Stop this session's dashboard |
| `doctor` | Check the private analysis runtime |
| `sessions 20` | List saved sessions in the current project |
| `json` | Export the selected trajectory and views to the adapter's artifact directory |

`--request-id` accepts an exact, nonempty ID of up to 4,096 characters and only applies to `view prompt_construction`; quote IDs containing spaces, and use `--request-id="-leading-dash"` for a leading dash. Omitting it uses the view’s default selection. The built-in **Prompt Evolution** tab and this view use evidence in the ordinary trajectory. DevEco transcript candidates are estimates, not the private component observations below, and do not prove final provider payloads.

Use `dashboard stop` before changing the port of an existing dashboard. The browser dashboard shows steps, tools, timing, usage, and available subagent evidence. Missing measurements remain unknown. The adapter refreshes a private native source while the dashboard is open; use the dashboard's **Load** control to update the displayed trajectory. The Captured components tab has a separate **Refresh captures** action. If native refresh fails, the last successful copy remains available with a stale-source warning; retry `overview` or `dashboard` to refresh it.

The adapter keeps runtime and session artifacts under `~/.cache/trajviz-obs/deveco` by default (`XDG_CACHE_HOME` changes the cache parent). It does not install skills or modify HomeTrans configuration. The initial DevEco adapter does not collect usage telemetry.

## Enable prompt component capture

Capture is off by default. Before starting DevEco Code, create a private destination and set both launch variables:

```sh
private_dir="$(mktemp -d)"
private_dir="$(cd "$private_dir" && pwd -P)"
export TRAJVIZ_OBS_PROMPT_CAPTURE=1
export TRAJVIZ_OBS_PROMPT_CAPTURE_DIR="$private_dir/captures"
deveco /absolute/path/to/project
```

After a response, invoke `obs` with `command: "dashboard"` and open **Captured components**. Inspect composition, captured component content, and capture history; use **Refresh captures** to include newer observations. To stop capture, unset both variables and relaunch DevEco Code. Existing capture files remain local.

The verified legacy hooks expose separate observations of system sections, message history, generation parameters, user messages, and compaction inputs. They do **not** expose the final provider request body. Observations stay in separate lanes without guessed request links; a hook without a trustworthy session identity is skipped. All current DevEco stages are independent `component.*` observations, shown as `comparison_status: unlinked_component`: automatic deltas and request-level comparisons are unavailable. Timeline, composition, and content inspection remain available. Later hooks or provider lowering may change inputs after an observation. Composition sizes are serialized bytes, not token measurements.

Prompt capture does not edit, replay, or analyze prompts with a model. It uses the same private writer and evidence limits described in [PROMPT_CAPTURE.md](../PROMPT_CAPTURE.md). Capture currently requires POSIX filesystem controls; Windows capture fails closed. Private capture sidecars are excluded from trajectory exports and usage telemetry and do not populate the built-in **Prompt Evolution** tab. Ordinary exports can retain native prompt evidence already present in the host trajectory.

## Update, remove, or troubleshoot

Update your checkout and rerun the locked install/build in `deveco`, then fully restart DevEco Code. Remove its entry from the configuration and restart to disable the adapter. There is no adapter-managed update command.

If the `obs` tool is missing, check the singular `plugin` array, the absolute `dist/index.js` path, the build output, and the full process restart. If runtime setup fails, verify Python/uv and network access, then invoke `doctor` again. DevEco account/model setup belongs to the host and is independent of TrajViz capture. If the host requires product agreements before dispatch, review them with `deveco auth consent`.

## Validation status

A real **DevEco Code 0.2.6 + DeepSeek Flash** run using TrajViz **0.5.1** migrated the nine foreground Duration countdown scenarios from Simple Progress v1.5 using HomeTrans. The run exercised the `obs` tool, native history and confirmed child sessions, the browser trajectory dashboard, and the private capture panel (then named Prompt Explorer). Independent checks passed **25 tests** against the exact production countdown model, a **DevEco CLI 1.3.0** build against **API 26**, and the HomeTrans ArkTS/CodeLinter precheck. The resulting HAP is unsigned; device installation, UI rendering, screenshot parity, background behavior, and restart restoration were not validated.

The same saved native trace has also been replayed with TrajViz **0.7.1** across all **37 views**: 351 records from 13 sessions and 333 estimated prompt boundaries. Prompt Evolution computes composition for a bounded 200 of those boundaries and identifies transcript comparisons as estimates with unverified request lineage. The private panel still reads the 1,019 component observations independently. This replay made no new coding-model requests and did not repeat the migration or device validation.

A fresh **TrajViz 0.7.1** live run on **2026-11-26** used DevEco Code **0.2.6** and HomeTrans's planner, coder and reviewer to migrate the fixed Simple Progress v1.5 source into a new EmptyAbility project. It recorded **134 native records**, **4 sessions**, **128 estimated assistant boundaries**, **168 tool calls** and **392 independent component observations**. The application passed **11 project tests**, **28 independent tests of its production model**, the **API 26 SDK build** and the changed-ETS ArkTS/CodeLinter precheck. Review found **0 defects**. The unsigned HAP was not deployed; device acceptance remains unvalidated. See the [case study and light-mode screenshots](docs/case-study.md) for Prompt Evolution and runtime examples.

The Captured components panel labels the available boundaries as **System sections**, **Message history**, **User message**, **Generation parameters**, and **Compaction input**. These remain independent observations with no inferred request-level comparisons. Offline contract tests cover hook boundaries beyond those exercised in the case.

## Development

```sh
bun install --frozen-lockfile
bun run build
bun run typecheck
bun test
```

The build bundles canonical TypeScript helpers and packages the common Python bridge. Offline contract tests do not start a coding session or make model requests. Keep private sessions, captures, and test output outside the repository.
