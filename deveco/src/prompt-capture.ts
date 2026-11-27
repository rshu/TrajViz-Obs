import { randomUUID } from 'node:crypto'
import type { PromptCapture } from '../../shared/prompt-capture.js'
import type { HostHooks, HookInput } from './host.js'
import { record } from './collect.js'

type Capture = Pick<PromptCapture, 'enabled' | 'record' | 'close'>
type Observation = Parameters<Capture['record']>[0]
const validID = (value: unknown): value is string => typeof value === 'string' && value.length > 0 && value.length <= 4096 && !value.includes('\0')

function modelIdentity(value: unknown): Record<string, string> | undefined {
  const input = record(value), result: Record<string, string> = {}
  for (const key of ['providerID', 'id', 'modelID']) if (typeof input[key] === 'string' && input[key].length <= 512) result[key] = input[key]
  return Object.keys(result).length ? result : undefined
}

/** Legacy hooks expose independent components, not the assembled engine request
 * or provider payload. Never mutate output, and never guess a session from a
 * previous hook, current UI selection, or wall-clock proximity.
 */
export function registerPromptCapture(capture: Capture): { hooks: HostHooks; close(): Promise<void> } {
  let stopped = false, sequence = 0
  let closing: Promise<void> | undefined
  const hooks: HostHooks = {}
  const observe = (sessionID: unknown, stage: string, point: string, data: Record<string, unknown>, input: HookInput = {}) => {
    if (stopped || !capture.enabled || !validID(sessionID)) return
    const correlation = { capture_id: randomUUID(), observation_sequence: ++sequence,
      observed_at: new Date().toISOString(), pairing: 'independent',
      ...(typeof input.agent === 'string' ? { agent_id: input.agent } : {}),
      ...(validID(input.messageID) ? { message_id: input.messageID } : {}) }
    const observation: Observation = { session_id: sessionID, stage, kind: point,
      correlation, model: modelIdentity(input.model), data: { ...data,
        provenance: { point, assembled_request: 'not_observed', provider_lowering: 'not_observed', later_hooks: 'not_observed' } } }
    // Snapshot while the callback still owns the observed values. Circular or
    // unserializable host objects are ignored without affecting model dispatch.
    capture.record(JSON.parse(JSON.stringify(observation)) as Observation)
  }
  const safe = <I, O>(callback: (input: I, output: O) => void) => async (input: I, output: O): Promise<void> => {
    try { if (!stopped && capture.enabled) callback(input, output) } catch { /* Observation cannot interrupt the host. */ }
  }
  if (capture.enabled) {
    hooks['experimental.chat.system.transform'] = safe((input, output) => {
      observe(input.sessionID, 'component.system', 'experimental.chat.system.transform', { system: output.system }, input)
    })
    hooks['experimental.chat.messages.transform'] = safe((input, output) => {
      if (!Array.isArray(output.messages) || !output.messages.length) return
      const ids = output.messages.map(row => record(record(row).info).sessionID)
      const id = ids[0]
      if (!validID(id) || ids.some(value => value !== id) || validID(input.sessionID) && input.sessionID !== id) return
      const rows: Record<string, unknown>[] = []
      for (const raw of output.messages) {
        const row = record(raw), info = record(row.info)
        if (!Array.isArray(row.parts) || row.parts.some(part => {
          const value = record(part)
          return value.sessionID !== undefined && value.sessionID !== id
        })) return
        rows.push({ id: info.id, role: info.role, content: row.parts })
      }
      observe(id, 'component.messages', 'experimental.chat.messages.transform', { messages: rows }, input)
    })
    hooks['chat.message'] = safe((input, output) => {
      if (output.message.sessionID !== undefined && output.message.sessionID !== input.sessionID) return
      if (!Array.isArray(output.parts) || output.parts.some(part => part.sessionID !== undefined && part.sessionID !== input.sessionID)) return
      observe(input.sessionID, 'component.user_message', 'chat.message', {
        messages: [{ id: output.message.id, role: output.message.role, content: output.parts }],
      }, { ...input, messageID: output.message.id })
    })
    hooks['chat.params'] = safe((input, output) => {
      const parameters: Record<string, unknown> = {}
      for (const key of ['temperature', 'topP', 'topK', 'maxOutputTokens']) {
        if (typeof output[key] === 'number' && Number.isFinite(output[key])) parameters[key] = output[key]
      }
      // Provider options may contain authentication values. Never copy them or headers.
      observe(input.sessionID, 'component.parameters', 'chat.params', { parameters }, input)
    })
    hooks['experimental.session.compacting'] = safe((input, output) => {
      observe(input.sessionID, 'component.compaction', 'experimental.session.compacting', {
        context: output.context, ...(typeof output.prompt === 'string' ? { prompt: output.prompt } : {}),
        default_prompt: output.prompt === undefined ? 'not_exposed' : 'overridden_by_host_or_plugin',
      }, input)
    })
  }
  return { hooks, close: () => closing ??= (async () => {
    stopped = true
    try { await capture.close() } catch { /* Capture shutdown cannot break host disposal. */ }
  })() }
}
