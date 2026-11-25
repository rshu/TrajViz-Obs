import type { Plugin } from '@opencode/plugin'
import { randomUUID } from 'node:crypto'
import type { PromptCapture } from './shared/prompt-capture.js'

type Capture = Pick<PromptCapture, 'enabled' | 'record' | 'close'>
type Input = Parameters<Capture['record']>[0]
type Host = Pick<Plugin.Context, 'session'>
type Identity = { sessionID: string; model: unknown; agent?: string }

const MAX_BODY_BYTES = 2 * 1024 * 1024
const READ_TIMEOUT_MS = 500
const MAX_PENDING = 4
const REQUEST_KINDS = ['context', 'compaction', 'generate', 'title'] as const
const OPTION_KEYS = ['maxTokens', 'temperature', 'topP', 'topK', 'frequencyPenalty', 'presencePenalty', 'seed', 'stop'] as const

type Body = { payload: unknown; omitted_reason?: 'size_limit' | 'read_timeout' | 'invalid_json' | 'read_failed' }

/** Inspect only a clone. Cancelling a tee branch must not wait for the provider to read its branch. */
async function readBody(request: Request): Promise<Body> {
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined
  let timer: ReturnType<typeof setTimeout> | undefined
  const cancel = () => { try { void reader?.cancel().catch(() => {}) } catch { /* Capture cannot break dispatch. */ } }
  try {
    if (request.bodyUsed) return { payload: null, omitted_reason: 'read_failed' }
    const declared = Number(request.headers.get('content-length'))
    if (Number.isFinite(declared) && declared > MAX_BODY_BYTES) return { payload: null, omitted_reason: 'size_limit' }
    reader = request.clone().body?.getReader()
    if (!reader) return { payload: null, omitted_reason: 'invalid_json' }
    const read = async (): Promise<Body> => {
      const chunks: Uint8Array[] = []
      let bytes = 0
      while (true) {
        const part = await reader!.read()
        if (part.done) break
        bytes += part.value.byteLength
        if (bytes > MAX_BODY_BYTES) { cancel(); return { payload: null, omitted_reason: 'size_limit' } }
        chunks.push(part.value)
      }
      try { return { payload: JSON.parse(Buffer.concat(chunks).toString('utf8')) } }
      catch { return { payload: null, omitted_reason: 'invalid_json' } }
    }
    const timeout = new Promise<Body>(resolve => {
      timer = setTimeout(() => { cancel(); resolve({ payload: null, omitted_reason: 'read_timeout' }) }, READ_TIMEOUT_MS)
      timer.unref()
    })
    return await Promise.race([read(), timeout])
  } catch { return { payload: null, omitted_reason: 'read_failed' } }
  finally { if (timer) clearTimeout(timer) }
}

/** Local observation only. Semantic and transport captures intentionally have no guessed step pairing. */
export async function registerPromptCapture(ctx: Host, capture: Capture): Promise<() => Promise<void>> {
  const registrations: { dispose(): Promise<void> }[] = []
  const pending = new Set<Promise<void>>()
  let stopped = false, sequence = 0
  let closing: Promise<void> | undefined
  const active = () => !stopped && capture.enabled
  const observation = () => ({ capture_id: randomUUID(), observation_sequence: ++sequence,
    observed_at: new Date().toISOString(), pairing: 'independent' })
  const record = (event: Identity, stage: Input['stage'], kind: string, correlation: Record<string, unknown>, data: unknown) => {
    if (!capture.enabled) return
    try {
      // The shared writer snapshots synchronously; later hooks cannot change this observation.
      capture.record({ session_id: event.sessionID, stage, kind, model: event.model,
        correlation: { ...correlation, ...(typeof event.agent === 'string' ? { agent_id: event.agent } : {}) }, data })
    } catch { /* An observation must never affect the model request. */ }
  }
  if (capture.enabled) {
    for (const kind of REQUEST_KINDS) {
      try {
        registrations.push(await ctx.session.hook(kind, event => {
          try {
            if (!active()) return
            const options = Object.fromEntries(OPTION_KEYS.filter(key => event.options[key] !== undefined).map(key => [key, event.options[key]]))
            record(event, 'context', kind === 'context' ? 'primary' : kind, observation(), {
              system: event.system, messages: event.messages,
              tools: 'tools' in event ? event.tools : {}, options,
              ...('agent' in event ? { agent: event.agent } : {}),
              provenance: { point: `${kind} hook`, provider_lowering: 'not_applied', later_hooks: 'not_observed' },
            })
          } catch { /* Capture errors cannot stop a model request. */ }
        }))
      } catch { /* Hosts may not expose every auxiliary hook. Other captures remain useful. */ }
    }
    try {
      registrations.push(await ctx.session.hook('http.request', event => {
        try {
          if (!active()) return
          const identity: Identity = { sessionID: event.sessionID, agent: event.agent,
            model: JSON.parse(JSON.stringify(event.model)) }
          const kind = event.kind, correlation = observation()
          if (pending.size >= MAX_PENDING) {
            record(identity, 'provider.request', kind, correlation, { transport: 'http', agent: identity.agent, payload: null, omitted_reason: 'capture_busy' })
            return
          }
          // No headers or URLs are recorded. Reading a bounded clone never consumes the original body.
          const job = readBody(event.request).then(body => {
            record(identity, 'provider.request', kind, correlation, {
              transport: 'http', agent: identity.agent, ...body,
              provenance: { point: 'http.request hook', later_hooks: 'not_observed' },
            })
          }).catch(() => {})
          pending.add(job)
          void job.finally(() => pending.delete(job))
        } catch { /* Capture errors cannot stop dispatch. */ }
      }))
    } catch { /* Semantic snapshots still work without transport hooks. */ }
    try {
      registrations.push(await ctx.session.hook('experimental.ws.send', event => {
        if (!active()) return
        let body: Body
        if (Buffer.byteLength(event.frame, 'utf8') > MAX_BODY_BYTES) body = { payload: null, omitted_reason: 'size_limit' }
        else {
          try { body = { payload: JSON.parse(event.frame) } }
          catch { body = { payload: null, omitted_reason: 'invalid_json' } }
        }
        record(event, 'provider.request', event.kind, observation(), {
          transport: 'websocket', agent: event.agent, ...body,
          provenance: { point: 'experimental.ws.send hook', later_hooks: 'not_observed' },
        })
      }))
    } catch { /* HTTP-only hosts do not need WebSocket capture. */ }
  }
  return () => closing ??= (async () => {
    stopped = true
    await Promise.allSettled(registrations.map(async item => item.dispose()))
    await Promise.allSettled(pending)
    await capture.close()
  })().catch(() => {})
}
