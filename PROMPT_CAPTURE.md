# Read-only prompt capture

Experimental capture records how requests change before building a prompt-explorer UI. It observes existing calls; it does not edit prompts, replay requests, or make model calls. Capture is **off by default**, independently of usage telemetry. Captured content never goes to the usage collector.

## Enable

Use the checkout on the `codex/read-only-prompt-capture` branch. Build the adapter after updating it (`bun run build` in `pi` or `opencode`). The writer uses an available Python interpreter and only the standard library; capture never installs a runtime.

In a macOS or Linux shell, create a private directory and resolve temporary-path aliases:

```sh
private_dir="$(mktemp -d)"
private_dir="$(cd "$private_dir" && pwd -P)"
export TRAJVIZ_OBS_PROMPT_CAPTURE=1
export TRAJVIZ_OBS_PROMPT_CAPTURE_DIR="$private_dir/captures"
```

Launch Pi, Claude Code, or the OpenCode server from that shell. Both variables are required; only the exact value `1` enables capture. An existing capture directory must be owned by the current user and accessible only to that user. Symlink components in its path are rejected.

OpenCode TUI and Desktop share server-side capture. Set these variables on the **server**, not just a client connecting to an existing server. Restart that server from the configured shell to apply launch settings. For Desktop's built-in server, finish active tasks, fully quit Desktop, run the matching Desktop CLI below, then reopen Desktop. Quitting Desktop alone can leave its server running. Preserve any existing configuration/data-directory launch variables.

```sh
"/Applications/OpenCode.app/Contents/Resources/opencode-cli" service restart
```

For a terminal-managed server use `opencode service restart`; for a standalone session, close and relaunch `opencode --standalone`. This does not require changing `opencode.json`.

Claude Code rejects capture options supplied through `settings.env`; use its launch environment. To stop capture, unset both variables and restart the host or server. Existing capture files remain local until you remove them.

## Inspect composition and changes

From the repository root:

```sh
python3 bridge/prompt_capture.py inspect --root "$TRAJVIZ_OBS_PROMPT_CAPTURE_DIR"
```

The JSON report contains component identities, hashes, positions, serialized byte sizes, provenance, and added/removed/modified/moved components. It omits component content by default. Add `--include-content` only when you want prompt text in the report. Keep saved reports outside the capture tree so they are not mistaken for observations.

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

The inspector uses Claude's native request/message IDs and thread continuation references. It does not guess links to plugin step IDs. Requests from other sessions are excluded. Unset `OTEL_LOG_RAW_API_BODIES` when finished.

## Evidence and limits

| Host | Recorded boundaries | Limits |
| --- | --- | --- |
| Pi | Full-context hook, provider-payload hook, turn results, compaction entries | Later extensions may change inputs. Native compaction and branch-summary requests bypass the payload hook; their internal prompts are not captured. The compaction entry retains the result and available usage. |
| Claude Code | System sections, initial context, attachments, expanded skills, tool descriptions, normalized per-step messages and completion usage | Component hooks are not complete provider requests. Optional native logs add successful request bodies; extended thinking may be redacted. |
| OpenCode | Main and auxiliary context hooks; native HTTP request bodies and WebSocket frames | Later hooks may change inputs. Semantic and transport observations have independent identities; hook entry order is preserved even when body reads finish out of order. |

Logical context and transmitted payloads are separate. A continuation can send only new messages while the provider retains earlier instructions and tools. Removed fields in a payload diff do **not** mean the model lost that context. WebSocket control frames and omitted captures do not become empty prompt snapshots. A semantic hook also does not prove a request was dispatched.

Component tokens are **not measured** in this prototype. Provider usage, when exposed, remains separate; byte sizes are not token counts. Missing content stays unknown. Capture gaps are explicit, and comparisons stay within the observed session, agent, lineage, and boundary. Capture begun mid-session cannot recover earlier missing observations.

The writer publishes immutable `0600` JSON records under private directories with exclusive instance locking. Queues, record sizes, inspection memory, and shutdown waits are bounded. Pi/OpenCode allow 16 outstanding records and 8 MiB in total; Claude Code allows 128 records and 8 MiB for startup component bursts. Each adapter/session/instance directory stops at 4,096 records or 128 MiB and records a storage-limit marker. Inspection also limits its entire selected input to 4,096 records and 128 MiB; choose a narrower session/instance directory for larger capture trees. Failures never block the agent's model dispatch on disk I/O.

Private storage currently requires POSIX filesystem controls. Validation targets macOS; Windows capture fails closed. Prompt files can contain complete conversations and source code. They are separate from session exports and usage-statistics queues; there is no automatic upload, sharing, or retention cleanup.
