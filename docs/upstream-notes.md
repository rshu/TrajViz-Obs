# TrajViz integration notes

This plugin targets `agent-trajviz>=0.2.0,<0.3`. The bridge calls exported TrajViz APIs and its published CLI entry points. Version-sensitive adaptations live in `bridge/trajviz_obs/compat.py`; the Gradio preload adapter lives in `dashboard_cmd.py`. A version outside the supported range is refused when a trajectory is loaded.

## Compatibility adapters

| Need | Current adaptation | Upstream API that would replace it |
| --- | --- | --- |
| One context window across diagnostics, issues, briefs, reports, and exports | Set `run.raw["metadata"]["context_window_limit"]` immediately after `load()`. Mark the snapshot's window source as `override`. | A public `load(..., context_window_limit=...)` or immutable analysis configuration used by every consumer. |
| Run step labeling through Claude Code's model client | Temporarily replace the labeler's `call_llm` with a capture/replay callable. Replace the labeler's own retry/pacing sleep during replay. | A `chat=` injection point on `label_trajectory()`, matching `llm.ask()` and `llm.suggest_fixes()`. |
| Bound labeling work in env mode | Wrap the real labeler client so only the first requested steps reach the endpoint; later steps receive `unknown` placeholders. | A public `max_steps` or selected-step argument to `label_trajectory()`. |
| Open Insight with the current session loaded | Find its Trajectory file component, serve the original transcript path so sibling subagents remain discoverable, and trigger its Load control. Fall back to manual ZIP loading if the component cannot be found. | A server-side initial trajectory parameter on `insight_app()`. |

Prompt capture and replay for `analyze` and `fixes` already use public `chat=` APIs. Replay checks a hash covering the trajectory and exact prompts, including context window, history, language, and call limits, to refuse replies for changed data. TrajViz plugins are loaded before format or view lookup; their standard output is redirected to standard error so the bridge's stdout stays a JSON protocol.

TrajViz 0.2.0's LLM module reaches both Pygments and Plotly through issue presenters, although its `llm` extra does not declare those dependencies. The managed base environment therefore installs `agent-trajviz[llm,charts]`; Gradio remains optional. Doctor uses the lightweight configuration adapter so it can still diagnose an incomplete environment. Bootstrap metadata records a revision so earlier environments are repaired.

## Native Claude Code data

These are measurement limits of the pinned parser, rather than missing UI fields:

- Transcript intervals cannot establish exact per-tool durations. Workflow spawns are not fully represented as spawn relationships, so workflow-heavy throughput and wait figures are approximate.
- Bookkeeping, slash-command, and `isMeta` rows can appear as user records upstream. The overview separately reports assistant steps, real prompts, and record counts.
- Most subagent steps lack a reliable finish marker. The bridge preserves unknown values instead of inferring an end.
- Offloaded tool results under `<session-id>/tool-results/` are represented by transcript previews. Reading full sidecar contents should be an explicit upstream capability with output bounds.
- Native `.meta.json` sidecars can supply subagent names and types. The bridge reads these locally; a public upstream name resolver would remove that work.

The fixture under `bridge/tests/fixtures/native_claude_code/` comes from TrajViz's public test data. Its README records the source revision, license, hash, and sanitization. It must not be replaced with a private live transcript.

## Scale and attribution

TrajViz currently reloads the complete trajectory for each analysis. An incremental load/analysis API would make post-turn watch cheaper on long sessions. The plugin debounces refreshes and stops automatic work above 32 MiB until such an API exists.

TrajViz's reports and dashboards cap rendered input at 2,000 steps. The overview and views analyze every step while limiting the amount drawn in the terminal. A public rendering cap with a visible truncation notice would make those surfaces easier to compare.

DECAF attribution requires a separate installation and gold references and refuses merged multi-file runs. Live sessions commonly include subagents, so the plugin presents its unavailable reason. Supporting merged runs and native session identities upstream would expand that feature without guessing attribution in the plugin.

## Claude Code host boundary

The function hooks API is early access and build-specific. Claude Code 2.1.304's generated `claude-code.d.ts` is the authority for commands, model requests, panes, and process streams. `claude plugin validate --strict .`, `claude plugin test .`, TypeScript checking, and real-session tests cover different parts of this boundary; none substitutes for the others. Keep generated declarations out of the repository and revalidate after upgrading the host.

The Python bridge uses `-I` and a managed environment. Process streams have no interactive stdin: dashboard lifetime must follow process termination, rather than waiting for stdin EOF. The hooks environment has no Node.js or DOM, imports are static, and environment/state names must remain source-visible literals for the host validator.
