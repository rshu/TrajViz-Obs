# Release validation

Validation was performed on macOS on 2026-10-25 with the user's configured Claude Code account. The first integration checks ran on Claude Code 2.1.304; the installed host updated to 2.1.308 during the work. The release checks use 2.1.308 and its generated API declarations. `agent-trajviz` is 0.2.0.

The complete real-session run **passed**, including normal stdin-EOF shutdown and release of both dashboard ports. The local checks passed: **180 hook tests**, **315 Python tests**, strict plugin validation, TypeScript against both 2.1.304 and 2.1.308 declarations, and Ruff. See the [sanitized machine-readable results](e2e-results.json).

## Automated checks

Run the complete local suite without model calls:

```sh
uv sync --project bridge --locked --extra dev
bridge/.venv/bin/python scripts/check.py
```

The runner validates plugin and marketplace manifests, executes Claude Code's native hook tests, type-checks against the installed host declarations, runs the Python tests, and lints the bridge and scripts. It continues after a failed check and returns a nonzero status if any check fails. Python tests include real loopback servers; run them in an environment that permits local socket binding.

Hook tests cover terminal and desktop rendering, all pane tabs, historical cards, saved-session actions, session selection, model failures, native MCP output shape, startup deadlines, process cleanup, and output limits. The generated native snapshot fixture supplements the synthetic layout fixture so the renderer is exercised against actual bridge shapes.

Python tests use the real TrajViz library and sanitized fixtures. They cover native parent/subagent sessions, partial writes and corruption, comparison, exports, capture/replay transactions, optional dependencies, source-file protection, immutable environment publication, concurrent setup, lock ownership, and interrupted installer descendants.

The GitHub workflow runs Python 3.11–3.13 on Linux, a minimal runtime without Gradio, and native bootstrap tests on Windows. The workflow has been configured and checked locally; remote GitHub Actions results are not claimed here. Runtime environments are independently verified before publication, and the development dependency set is locked in `bridge/uv.lock`.

## Real-session end to end

Use an empty output directory. This command makes model calls and installs packages if needed:

```sh
python3 scripts/e2e.py \
  --output /tmp/trajviz-obs-e2e-new \
  --model haiku --budget 5 \
  --test-subagent --test-llm-all --test-obs-tool
```

The harness records a private `ledger.json`, raw events, and exports. It verifies actual tool events and artifacts, rather than trusting Claude Code's `is_error` field: local plugin errors can still arrive inside a successful command envelope.

The complete scenario intentionally fails a Bash assertion, creates and tests a small Python program, delegates a real subagent, and inspects the same running session. It checks the overview and health, tool usage, normalized JSON export and subagent merge, offline HTML, analysis, fix judgment, one-step labeling and label views, and a model-initiated deferred `obs` tool call. It then checks Insight and Converge over HTTP, command-to-command survival, stop/restart, and port cleanup when the Claude session ends.

A separate installation check uses a new `CLAUDE_CONFIG_DIR`, adds this checkout as a local marketplace, installs the plugin, and runs overview/doctor from a fresh working directory **without** `--plugin-dir`. It succeeds without model calls and does not modify the user's normal plugin configuration.

[Browser verification](browser-e2e.md) separately records the actual Chrome check: the real transcript loaded and rendered KPIs and plots without clicking Load Trajectory. Raw test transcripts and screenshots containing browser chrome remain outside the repository.

## Scope

The local real-session proof covers macOS. Linux and Windows have dedicated CI coverage configured; their full Claude terminal/browser sessions have not been run locally. The function-hooks API remains an early access host feature. Hosts supporting only shell hooks cannot load this plugin. See the README for upstream measurement limits and optional DECAF requirements.
