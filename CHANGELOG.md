# Changelog

## 0.1.15 — 2026-10-30

- Add the independently packaged OpenCode 2 adapter (0.1.0), with native commands, a live panel, status footer, and the shared dashboard and analysis features.
- Normalize OpenCode 2 history, confirmed subagents, tool outcomes, context usage, skills, and manual compactions while retaining native source data.
- Keep concurrent captures and model plans immutable, scope dashboard processes by session, and validate attribution parser compatibility.

## 0.1.14 — 2026-10-28

- Give the pane overview aligned metrics, compact labeled trends, and a readable latest phase.
- Keep inline panes and freshness information compact, and retain unavailable health metrics as unavailable.
- Exclude confirmed local-command display records from native work steps without changing source files or identities.
- Show a refresh notice when cached labels use an incompatible step layout; retain valid partial labels.
- Let dashboard charts use the available width and finish automatic attribution after clearing its previous result.

## 0.1.13 — 2026-10-28

- Strip terminal control sequences from display text so trajectory previews cannot blank the pane or overview.
- Keep the pane usable when live usage is delayed, and offer retry and close controls if drawing fails.

## 0.1.12 — 2026-10-27

- Restore an enabled status line after dependency setup finishes.
- Stack comparison uploads on narrow screens so filenames remain readable.
- Recommend restarting Claude Code to recover a stopped function handler.
- Make the real-session subagent check work with isolated Python.

## 0.1.11 — 2026-10-27

- Align option descriptions and command help with model routing, labeling caps, and view pagination.
- Keep doctor model names consistent with execution and preserve useful attribution diagnostics containing file paths.
- Resolve relative test inputs before subprocesses change directories.

## 0.1.10 — 2026-10-27

- Use attribution consistently in commands, dashboards, diagnostics, configuration, and documentation.

## 0.1.9 — 2026-10-27

- Validate label destinations before model calls and publish sidecars atomically.
- Preserve analysis history when requests overlap, and protect model reply files from output overwrites.
- Bound label settings and require env mode for pacing that would consume Claude Code's hook time budget.
- Clear a disabled status line on plugin reload.
- Accept session directories in dashboards and bundles, preserving subagent logs for renamed transcripts.

## 0.1.8 — 2026-10-26

- Dispatch card and pane actions directly so Claude Code's command re-entry guard cannot swallow them.
- Refresh an enabled status line after manual overview and pane refreshes, including empty sessions.
- Keep slash commands available when the host disables model tool registration.
- Expose custom label taxonomies, deterministic user tags, content limits, pacing, and model sampling options.
- Refuse capability attribution when its parser loses native Claude JSONL tool evidence, in both CLI and dashboard.
- Expand reproducible coverage across all eight trajectory formats, collectors, comparisons, label views, dashboard callbacks, and real Claude Code sessions.

## 0.1.7 — 2026-10-26

- Recognize Claude Code's plugin output prefix so `/obs` renders its custom card.
- Put dashboard navigation first, with health cards and findings in Overview’s Summary section.
- Compact Claude Code cards and narrow panes; lead with actions, metrics, and ranked issue titles.
- Improve diagnostic labels, units, and wrapping records in the native pane.
- Bound dense agent filters and chart legends, and adapt Workflow to the space available beside the AI panel.
- Keep dashboards consistently light to match native charts; remove empty chart placeholders.

## 0.1.6 — 2026-10-26

- Make the full TrajViz dashboard the browser entry point; remove `/obs report` and its export adapter.
- Improve pane navigation and narrow layouts, and group dashboard source controls, charts, and guidance.
- Restart dashboards when the selected session, data root, or launch settings change.
- Preserve Claude Code's context window and saved labels in the dashboard, and export every label view.
- Clarify session-model analysis versus the dashboard's environment-configured AI controls.

## 0.1.5 — 2026-10-26

- Fix standalone report fonts, responsive cards, and tooltip overflow.
- Keep large charts and tables in keyboard-accessible scroll panels, preserving chart detail.
- Remove report controls and hints that require the live dashboard's Workflow tab.

## 0.1.4 — 2026-10-26

- Prevent status-line usage lookups from blocking reports, dashboards, and session changes.
- Discard superseded status updates and honor off toggles during concurrent commands.
- Report hook timeouts and failures instead of falling through to the missing-handler message.

## 0.1.3 — 2026-10-25

- Keep installer cancellation safe under repeated termination signals.
- Reject orphaned supervisors before starting an installer.

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
