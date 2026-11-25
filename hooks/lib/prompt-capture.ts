import type { EngineInterface, PluginOptions, TurnStepInput } from 'claude-code'
import { findPython, pythonOverride, readEnvState } from './python'

const MAX_RECORD_BYTES = 2 * 1024 * 1024
const MAX_QUEUE_BYTES = 8 * 1024 * 1024
const MAX_QUEUE_RECORDS = 128
const WRITE_TIMEOUT_MS = 2_000
const SHUTDOWN_GRACE_MS = 1_000
const SETTINGS_KEYS = ['TRAJVIZ_OBS_PROMPT_CAPTURE', 'TRAJVIZ_OBS_PROMPT_CAPTURE_DIR']
type Configuration = { root: string; sessionId: string }
type Observation = { stage: string; data: unknown; correlation?: unknown; model?: unknown; kind?: string }

/** Sensitive prompt data has its own explicit local opt-in, independent of usage telemetry. */
export type PromptCapture = {
  instanceId: string
  sequence: number
  options: PluginOptions
  configuration?: Promise<Configuration | undefined>
  python?: Promise<string | undefined>
  queue: string[]
  queuedBytes: number
  active?: Promise<void>
  dropped: number
  totalDropped: number
  failed: number
  reportedFailed: number
  stopped: boolean
  diagnosed: boolean
}

export function createPromptCapture(options: PluginOptions): PromptCapture {
  return { instanceId: crypto.randomUUID(), sequence: 0, options, queue: [], queuedBytes: 0,
    dropped: 0, totalDropped: 0, failed: 0, reportedFailed: 0, stopped: false, diagnosed: false }
}

async function configuration($: EngineInterface): Promise<Configuration | undefined> {
  try {
    if (await $.env.get('TRAJVIZ_OBS_PROMPT_CAPTURE') !== '1') return
    const root = await $.env.get('TRAJVIZ_OBS_PROMPT_CAPTURE_DIR')
    const windows = /^windows/i.test((await $.env.get('OS')) ?? '')
    // A local absolute path only; the writer independently checks every directory and file.
    if (!root || root.includes('\0') || root.startsWith('//') || root.startsWith('\\\\')
      || !(windows ? /^[A-Za-z]:[\\/]/.test(root) : root.startsWith('/'))) return
    const settings = await $.settings.read({ key: 'env' })
    if (settings && typeof settings === 'object' && Object.keys(settings).some(key => SETTINGS_KEYS.includes(key.toUpperCase()))) return
    return { root, sessionId: await $.session.id() }
  } catch { return }
}

async function captureConfiguration($: EngineInterface, state: PromptCapture): Promise<Configuration | undefined> {
  state.configuration ??= configuration($)
  return state.configuration
}

async function capturePython($: EngineInterface, options: PluginOptions): Promise<string | undefined> {
  try {
    const installed = await readEnvState($, false)
    return installed?.python ?? (await findPython($, pythonOverride(options)))?.path
  } catch { return }
}

async function diagnose($: EngineInterface, state: PromptCapture): Promise<void> {
  if (state.diagnosed) return
  state.diagnosed = true
  try { await $.ui.log('obs prompt capture: some local records could not be saved.', { to: 'debug' }) } catch { /* Observation never interrupts the host. */ }
}

function recordBody(state: PromptCapture, config: Configuration, observation: Observation): string {
  return JSON.stringify({ schema_version: 1, adapter: 'claude-code', session_id: config.sessionId,
    instance_id: state.instanceId, record_id: crypto.randomUUID(), sequence: ++state.sequence, observed_at: new Date().toISOString(),
    capture_health: { dropped: state.totalDropped, failed: state.failed }, ...observation })
}

async function writeBody($: EngineInterface, state: PromptCapture, config: Configuration, python: string, body: string): Promise<boolean> {
  try {
    const result = await $.process.run([python, '-I', `${$.plugin.root}/bridge/prompt_capture.py`, 'write', '--root', config.root], {
      cwd: $.plugin.root, stdin: body, timeoutMs: WRITE_TIMEOUT_MS, env: { TRAJVIZ_OBS_PROMPT_CAPTURE: '1' },
    })
    if (result.exitCode === 0 && !result.isStdoutTruncated) return true
  } catch { /* The helper's diagnostics may contain private data. */ }
  await diagnose($, state)
  return false
}

async function drain($: EngineInterface, state: PromptCapture, config: Configuration): Promise<void> {
  try {
    state.python ??= capturePython($, state.options)
    const python = await state.python
    if (!python) {
      state.failed += state.queue.length
      // A later explicit /obs setup can make an interpreter available.
      state.python = undefined
      await diagnose($, state)
      return
    }
    let healthAttemptedFor = state.reportedFailed
    while (state.queue.length || state.dropped || state.failed > healthAttemptedFor) {
      if (!state.queue.length && !state.dropped) {
        const failed = state.failed
        healthAttemptedFor = failed
        const status = recordBody(state, config, { stage: 'capture.status', data: { dropped: state.totalDropped, failed } })
        const bytes = new TextEncoder().encode(status).length
        state.queuedBytes += bytes
        try {
          // Try once per failure count. A broken writer cannot recursively produce health records.
          if (await writeBody($, state, config, python, status)) state.reportedFailed = failed
        } finally { state.queuedBytes -= bytes }
        continue
      }
      const queued = state.queue.length > 0
      const body = queued ? state.queue.shift()! : recordBody(state, config, { stage: 'capture.gap', data: { reason: 'queue_full', omitted_records: state.dropped } })
      if (!queued) state.dropped = 0
      try {
        if (!(await writeBody($, state, config, python, body))) state.failed++
      }
      finally { if (queued) state.queuedBytes -= new TextEncoder().encode(body).length }
    }
  } finally {
    // A missing interpreter cannot leave sensitive records retained in memory indefinitely.
    state.queue.length = 0
    state.queuedBytes = 0
    state.dropped = 0
  }
}

function startDrain($: EngineInterface, state: PromptCapture, config: Configuration): void {
  if (state.active) return
  const work = drain($, state, config)
  state.active = work
  void work.finally(() => {
    if (state.active !== work) return
    state.active = undefined
    if (state.queue.length && !state.stopped) startDrain($, state, config)
  })
}

/** Clone into a bounded JSON string before the event can be changed by another hook. */
export async function capturePromptObservation($: EngineInterface, state: PromptCapture, observation: Observation): Promise<void> {
  try {
    if (state.stopped) return
    const config = await captureConfiguration($, state)
    if (!config || state.stopped) return
    let body = recordBody(state, config, observation)
    let bytes = new TextEncoder().encode(body).length
    if (bytes > MAX_RECORD_BYTES) {
      body = recordBody(state, config, { ...observation, stage: 'capture.gap', data: { reason: 'record_too_large', omitted_stage: observation.stage, limit_bytes: MAX_RECORD_BYTES } })
      bytes = new TextEncoder().encode(body).length
    }
    if (state.queue.length + (state.active ? 1 : 0) >= MAX_QUEUE_RECORDS || state.queuedBytes + bytes > MAX_QUEUE_BYTES) {
      state.dropped++
      state.totalDropped++
      await diagnose($, state)
      return
    }
    state.queue.push(body)
    state.queuedBytes += bytes
    startDrain($, state, config)
  } catch { await diagnose($, state) }
}

/** Capture normalized engine input; these blocks are not the final provider wire payload. */
export async function capturePromptRequest($: EngineInterface, state: PromptCapture, step: TurnStepInput): Promise<void> {
  try {
    if (state.stopped || !(await captureConfiguration($, state))) return
    const messages = await $.session.messages({ as: 'api', ...(step.agentId ? { agentId: step.agentId } : {}) })
    await capturePromptObservation($, state, { stage: 'request.context', kind: 'primary',
      correlation: promptRequestCorrelation(step), model: { id: step.model, effort: step.effort ?? null },
      data: { messages: Array.isArray(messages) ? messages : null, available: Array.isArray(messages),
        source: 'session.messages.api', message_count: step.messageCount, normalized: true,
        provenance: { hook: 'turn.step', boundary: 'engine_normalized_before_provider_serialization', later_handlers_may_modify: true,
          observed_message_count: Array.isArray(messages) ? messages.length : null, message_limit: 4096,
          message_limit_reached: Array.isArray(messages) && messages.length >= 4096 },
        omissions: ['system_prompt', 'tool_schemas', 'provider_transformations'],
        ...(Array.isArray(messages) ? {} : { omitted_reason: 'agent_context_unavailable' }) } })
  } catch {
    await capturePromptObservation($, state, { stage: 'capture.gap', kind: 'primary', correlation: promptRequestCorrelation(step),
      data: { omitted_stage: 'request.context', reason: 'context_unavailable' } })
  }
}

export function promptRequestCorrelation(step: TurnStepInput): Record<string, unknown> {
  return { request_id: `${step.turnId}:${step.index}`, turn_id: step.turnId, step_index: step.index,
    agent_id: step.agentId ?? null, scope: step.agentId ? 'agent' : 'main' }
}

/** Give queued local writes a bounded chance to finish; no shutdown waits on a missing runtime. */
export async function finishPromptCapture($: EngineInterface, state: PromptCapture): Promise<void> {
  state.stopped = true
  if (!state.active) return
  let expire = () => {}
  const deadline = new Promise<void>(resolve => { expire = resolve })
  try {
    const timer = $.clock.after(SHUTDOWN_GRACE_MS, expire)
    try { await Promise.race([state.active, deadline]) } finally { timer.cancel() }
  } catch { /* Shutdown is best effort. */ }
  finally { state.queue.length = 0 }
}
