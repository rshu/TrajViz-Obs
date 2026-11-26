# Captured components and read-only capture

The experimental **Captured components** tab in the browser dashboard shows captured prompt composition and available component comparisons for Pi, Claude Code, and OpenCode. DevEco Code contributes independent component observations for timeline, composition, and content inspection; automatic deltas and request-level comparisons are unavailable. Open it with `/obs dashboard` in the first three hosts, or the `obs` tool with `command: "dashboard"` in DevEco Code. It reads local observations without editing prompts, replaying requests, or making model calls. Capture is **off by default**, independently of usage telemetry. Private capture sidecars stay out of session exports and the usage collector.

## Prompt Evolution and private captures

TrajViz **0.7.1** also provides a built-in **Prompt Evolution** dashboard tab and the `prompt_construction` view. They inspect evidence already in the loaded trajectory: supported native prompt evidence when available, or explicitly estimated candidates reconstructed from transcript history. Estimates do not prove the final model request. Request selection uses `/obs view prompt_construction request_id=<id>` in Pi, Claude Code, and OpenCode; DevEco Code's `obs` tool accepts `view prompt_construction --request-id <id>`.

The **Captured components** tab, documented below, reads separately authorized private capture sidecars. They are not injected into the ordinary trajectory, the built-in Prompt Evolution view, or session exports. Native prompt evidence already contained in ordinary host history can still appear in ordinary `trajectory.json` and view exports. Review those exports before sharing. An engine upgrade does not turn DevEco's unlinked component observations into request-level comparisons.

## Enable

Use a checkout containing the Captured components panel and your host adapter. Build the adapter after updating it (`bun run build` in `pi`, `opencode`, or `deveco`). The writer uses an available Python interpreter and only the standard library; capture never installs a runtime.

In a macOS or Linux shell, create a private directory and resolve temporary-path aliases:

```sh
private_dir="$(mktemp -d)"
private_dir="$(cd "$private_dir" && pwd -P)"
export TRAJVIZ_OBS_PROMPT_CAPTURE=1
export TRAJVIZ_OBS_PROMPT_CAPTURE_DIR="$private_dir/captures"
```

Launch Pi, Claude Code, DevEco Code, or the OpenCode server from that shell. Both variables are required; only the exact value `1` enables capture. An existing capture directory must be owned by the current user and accessible only to that user. Symlink components in its path are rejected.

OpenCode TUI and Desktop share server-side capture. Set these variables on the **server**, not just a client connecting to an existing server. Restart that server from the configured shell to apply launch settings. For Desktop's built-in server, finish active tasks, fully quit Desktop, run the matching Desktop CLI below, then reopen Desktop. Quitting Desktop alone can leave its server running. Preserve any existing configuration/data-directory launch variables.

```sh
"/Applications/OpenCode.app/Contents/Resources/opencode-cli" service restart
```

For a terminal-managed server use `opencode service restart`; for a standalone session, close and relaunch `opencode --standalone`. This does not require changing `opencode.json`.

For DevEco Code, fully quit and relaunch `deveco` from the configured shell. This is the legacy adapter, with a singular `plugin` configuration and semantic component observations; it does not use OpenCode 2 service restart commands. See the [DevEco guide](deveco/README.md#enable-prompt-component-capture).

Claude Code rejects capture options supplied through `settings.env`; use its launch environment. To stop capture, unset both variables and restart the host or server. Existing capture files remain local until you remove them.

## Use Captured components

1. Enable capture above, then complete an assistant response in the new agent session.
2. Run `/obs dashboard` (DevEco Code: invoke the `obs` tool with `command: "dashboard"`) and open the **Captured components** tab.
3. Choose a **Capture lane** and a request or observation. Click a timeline snapshot, or use **Previous snapshot** and **Next snapshot**, to inspect it.
4. Select a component to read its captured content and, when a comparable baseline exists, its changes. **Changed components only** hides unchanged components; position changes can overlap with unchanged content.
5. Use **Refresh captures** during a task to include newer observations and native child sessions. **Follow newest on refresh** selects the newest snapshot in the chosen lane. Selecting an older snapshot pauses following; **Latest captures** resumes it.

Lanes keep agents, session branches, request purposes, and capture boundaries separate. Engine context and provider request parameters are different views; the explorer does not invent a link between them. A first snapshot or unlinked component observation has no inferred comparison. All current DevEco `component.*` observations have `comparison_status: unlinked_component`; they do not receive automatic deltas or request-level comparisons. Gaps remain marked as unavailable evidence rather than empty prompts.

Composition bars show **serialized UTF-8 bytes**, not token counts or context-window occupancy. In Pi, **Pi active system / tools** shows the effective instructions and tool declarations after applying native patches; conversation messages remain in **Request components**.

The explorer is bound to the host session that opened the dashboard and native child sessions confirmed in that capture. Loading another trajectory clears its prompt view; an uploaded file cannot authorize access to unrelated captures. It has no capture-directory browser, prompt-editing actions, or model-analysis buttons. Captured prompt content is not included in the dashboard's HTML report or session export commands.

**Older captures** and **Newer captures** browse long sessions in bounded windows of up to 1,024 observations and 32 MiB of displayed evidence. History windows follow capture publication order; snapshots within a lane retain their recorded order. An unavailable predecessor at a page boundary is shown explicitly, without inferring additions or removals. An expired window returns to the newest captures with a notice. The metadata catalog is also bounded; exceptionally large capture trees can still require the JSON inspector below.

Refresh rereads the host-selected native session, checks its identity, and discovers newly recorded child sessions. OpenCode and DevEco Code maintain a private live native snapshot for this purpose; their diagnostic and export snapshots stay immutable. If a DevEco native refresh fails, the live source retains the last successful copy with a stale-source warning; retry the `overview` or `dashboard` tool command. Capture omissions, writer losses, and missing provenance remain visible during the session. Loss warnings do not identify which requests were affected. Highly fragmented observations can appear as gap markers when they exceed the component preview budget. The explorer does not read current project files to reconstruct past prompts.

## Inspect JSON

From the repository root:

```sh
python3 bridge/prompt_capture.py inspect --root "$TRAJVIZ_OBS_PROMPT_CAPTURE_DIR"
```

The JSON report contains component identities, hashes, positions, serialized byte sizes, provenance, and added/removed/modified/moved components. It omits component content by default. Add `--include-content` only when you want prompt text in this explicit local report. Keep saved reports outside the capture tree so they are not mistaken for observations.

Pi reports both the physical transcript and the effective named system sections/tool declarations after applying native patches. Its local lineage ID allows comparisons across user turns and compaction; a native branch switch starts a new baseline. Request sequences remain scoped to each run.

Claude Code's component observations are retained without inventing request links. Its per-step message snapshots are normalized logical inputs, not provider payloads. For local provider-request verification, enable Claude Code's own file logging **before launch**:

```sh
mkdir -m 700 "$private_dir/claude-api"
export OTEL_LOG_RAW_API_BODIES="file:$private_dir/claude-api"
export CLAUDE_CODE_ENABLE_TELEMETRY=0
```

The file logger works without enabling remote native telemetry. After the session, include matching successful native requests in the inspection:

```sh
python3 bridge/prompt_capture.py inspect \
  --root "$TRAJVIZ_OBS_PROMPT_CAPTURE_DIR" \
  --claude-raw "$private_dir/claude-api"
```

The inspector uses Claude's native request/message IDs and thread continuation references. It does not guess links to plugin step IDs. Requests from other sessions are excluded. With local file logging enabled before launch, the live explorer can also show matching native API requests in their own lanes. Unset `OTEL_LOG_RAW_API_BODIES` when finished.

## Evidence and limits

| Host | Recorded boundaries | Limits |
| --- | --- | --- |
| Pi | Full-context hook, provider-payload hook, turn results, compaction entries | Later extensions may change inputs. Native compaction and branch-summary requests bypass the payload hook; their internal prompts are not captured. The compaction entry retains the result and available usage. |
| Claude Code | System sections, initial context, attachments, expanded skills, tool descriptions, normalized per-step messages and completion usage | Component hooks are not complete provider requests. Optional native logs add successful request bodies; extended thinking may be redacted. |
| OpenCode | Main and auxiliary context hooks; native HTTP request bodies and WebSocket frames | Later hooks may change inputs. Semantic and transport observations have independent identities; hook entry order is preserved even when body reads finish out of order. |
| DevEco Code | Legacy system/message transforms, user messages, generation parameters, and compaction inputs | Separate component observations, not full engine prompts or provider payloads. Unscoped hooks are skipped; later hooks or provider lowering may change inputs. No inferred request pairing or automatic component deltas (`unlinked_component`). |

Logical context and transmitted payloads are separate. A continuation can send only new messages while the provider retains earlier instructions and tools. Removed fields in a payload diff do **not** mean the model lost that context. WebSocket control frames and omitted captures do not become empty prompt snapshots. A semantic hook also does not prove a request was dispatched.

Component tokens are **not measured**. Provider usage, when exposed, remains separate; byte sizes are not token counts. Missing content stays unknown. Capture gaps are explicit, and comparisons stay within the observed session, agent, lineage, and boundary. Capture begun mid-session cannot recover earlier missing observations. Long component content and diffs have labeled preview limits; the local capture files remain unchanged.

The writer publishes immutable `0600` JSON records under private directories with exclusive instance locking. Queues, record sizes, inspection memory, and shutdown waits are bounded. Pi/OpenCode/DevEco Code allow 16 outstanding records and 8 MiB in total; Claude Code allows 128 records and 8 MiB for startup component bursts. Each adapter/session/instance directory stops at 4,096 records or 128 MiB and records a storage-limit marker. Inspection also limits its entire selected input to 4,096 records and 128 MiB; choose a narrower session/instance directory for larger capture trees. Failures never block the agent's model dispatch on disk I/O.

Private storage currently requires POSIX filesystem controls. Validation targets macOS; Windows capture fails closed. Prompt files can contain complete conversations and source code. They are separate from session exports and usage-statistics queues; there is no automatic upload, sharing, or retention cleanup.
