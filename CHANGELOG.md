# Changelog

## 0.1.2 — 2026-10-25

- Handle a new Claude session before its transcript file exists.
- Synchronize process cleanup tests on descendant exit instead of a fixed delay.

## 0.1.1 — 2026-10-25

- Fixed stale and concurrent snapshot publication, saved-session actions, dashboard cancellation, and concurrent model reply files.
- Protected loaded inputs and sidecars from export overwrites, and isolated temporary output files between writers.
- Hardened environment metadata, Windows publication, installation deadlines, and installer process cleanup.
- Corrected environment-provider settings, model request integrity, bounded output, and issue totals.
- Added regression, local HTTP, subprocess/schema, cold-install, and expanded real-session tests.
- Removed internal plans and validation reports; kept plugin instructions in the README.

## 0.1.0 — 2026-10-25

- Added the immediate `/obs` command, snapshot cards, seven-tab pane, post-turn watch, and optional status line for Claude Code's function hooks API.
- Connected TrajViz's diagnostics, view catalog, reports, JSON exports, session bundles, comparisons, and local dashboards through a Python bridge.
- Added explicit model analysis, fix suggestions, and step labeling through the session client or TrajViz's environment-configured client.
- Added managed Python setup, optional extras, session discovery, the deferred `obs` tool, and doctor output.
- Hardened live model requests with immutable inputs, setup with locked environment generations, and dashboard lifetime and cancellation.
- Added output bounds, source-file protection, saved-session action targeting, and native MCP result validation.
- Added hook and Python tests, locked dependencies, CI, a real-session E2E harness, packaging metadata, and developer documentation.

Developed against Claude Code 2.1.304; validated on 2.1.308. Supports `agent-trajviz>=0.2.0,<0.3`.
