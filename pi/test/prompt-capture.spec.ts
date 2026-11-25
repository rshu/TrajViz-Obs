import { expect, test } from 'bun:test'
import { mkdtemp, readFile, readdir, realpath, rm, stat } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import type { ExtensionAPI, ExtensionContext, ContextWithSystemEvent, BeforeProviderRequestEvent, TurnEndEvent } from '@earendil-works/pi-coding-agent'
import type { PromptCapture } from '../../shared/prompt-capture.js'
import { PiPromptCapture, registerPromptCapture } from '../src/prompt-capture.js'

type Recorded = Parameters<PromptCapture['record']>[0]
type Hook = (event: unknown, ctx: ExtensionContext) => unknown
type ObjectData = Record<string, any>

function freeze<T>(value: T): T {
  if (value && typeof value === 'object') { Object.freeze(value); for (const child of Object.values(value)) freeze(child) }
  return value
}
function fixture(enabled = true) {
  const records: Recorded[] = []
  const state = { session: 'pi-session', leaf: 'native-user-entry', closes: 0, flushes: 0 }
  const ctx = {
    sessionManager: { getSessionId: () => state.session, getLeafId: () => state.leaf },
    model: { provider: 'fixture', api: 'anthropic-messages', id: 'fixture-model', contextWindow: 200000 },
  } as unknown as ExtensionContext
  const sink = { enabled, record: (event: Recorded) => records.push(structuredClone(event)),
    flush: async () => { state.flushes++ }, close: async () => { state.closes++ } }
  const capture = new PiPromptCapture({ bridge: '/unused/bridge', telemetryPython: () => { throw new Error('should not probe Python') } }, '1.5.2', sink)
  const handlers = new Map<string, Hook[]>()
  const api = { on: (name: string, handler: Hook) => { handlers.set(name, [...handlers.get(name) ?? [], handler]); return () => {} } } as unknown as ExtensionAPI
  registerPromptCapture(api, () => capture)
  const emit = (name: string, event: object = {}) => {
    for (const handler of handlers.get(name) ?? []) expect(handler({ type: name, ...event }, ctx)).toBeUndefined()
  }
  return { records, state, ctx, sink, capture, emit }
}
const context = () => freeze({ type: 'context_with_system', messages: [
  { role: 'system', content: '', sections: { preamble: 'Instructions', project_context: 'AGENTS.md contents', skills: 'Skill descriptions' },
    toolsAdded: [{ name: 'read', description: 'Read a file', parameters: { type: 'object', properties: {} } }], timestamp: 1 },
  { role: 'user', content: [{ type: 'text', text: 'Private user prompt' }], timestamp: 2 },
] } as unknown as ContextWithSystemEvent)
const provider = () => freeze({ type: 'before_provider_request', payload: {
  system: [{ type: 'text', text: 'Provider instructions', cache_control: { type: 'ephemeral' } }],
  messages: [{ role: 'user', content: 'Private serialized prompt' }], tools: [{ name: 'read', input_schema: { type: 'object' } }],
  max_tokens: 4000, custom_provider_field: { enabled: true },
} } as BeforeProviderRequestEvent)
const response = (turnIndex = 0) => freeze({ type: 'turn_end', turnIndex,
  message: { role: 'assistant', content: [{ type: 'text', text: 'Private assistant result' }], model: 'fixture-model', provider: 'fixture',
    usage: { input: 100, output: 20, cacheRead: 80, cacheWrite: 0, totalTokens: 200 }, timestamp: 3, stopReason: 'stop' },
  toolResults: [], messageEntryId: 'native-assistant-entry', toolResultEntryIds: [], outcome: 'completed', canContinue: false,
} as unknown as TurnEndEvent)
const correlation = (record: Recorded): ObjectData => record.correlation as ObjectData

test('Pi observes frozen prompt, provider and response data without replacements or mutations', () => {
  const h = fixture()
  const input = context(), payload = provider(), result = response()
  const originals = structuredClone([input, payload, result])
  h.emit('agent_start'); h.emit('turn_start', { turnIndex: 0, timestamp: 1 })
  expect(h.capture.context(input, h.ctx)).toBeUndefined()
  expect(h.capture.provider(payload, h.ctx)).toBeUndefined()
  expect(h.capture.response(result, h.ctx)).toBeUndefined()
  expect([input, payload, result]).toEqual(originals)
  expect(h.records.map(record => record.stage)).toEqual(['context', 'provider.request', 'response'])
  expect((h.records[0]!.data as ObjectData).messages).toEqual(input.messages)
  expect((h.records[1]!.data as ObjectData).payload).toEqual(payload.payload)
  expect((h.records[2]!.data as ObjectData).message.usage).toEqual((result.message as ObjectData).usage)
  expect((h.records[2]!.data as ObjectData).messageEntryId).toBe('native-assistant-entry')
  expect((h.records[2]!.data as ObjectData).canContinue).toBe(false)
  const run = correlation(h.records[0]!).run_id
  const lineage = correlation(h.records[0]!).lineage_id
  for (const record of h.records) {
    expect(record.session_id).toBe('pi-session')
    expect(record.kind).toBe(record.stage === 'provider.request' ? undefined : 'primary')
    expect(record.model).toEqual({ provider: 'fixture', id: 'fixture-model', api: 'anthropic-messages', context_window: 200000 })
    expect(correlation(record)).toMatchObject({ run_id: run, run_source: 'agent_start', request_seq: 1, turn_index: 0,
      lineage_id: lineage, lineage_source: 'local_epoch', lineage_origin_entry_id: 'native-user-entry',
      run_origin_entry_id: 'native-user-entry', provider_request_id_available: false })
    expect(correlation(record)).not.toHaveProperty('provider_request_id')
  }
  expect((h.records[0]!.data as ObjectData).provenance.later_handlers_may_modify).toBe(true)
  expect((h.records[1]!.data as ObjectData).provenance.transport_may_send_delta).toBe(true)
  expect(correlation(h.records[1]!).confidence).toBe('inferred_active_context')
})

test('Pi keeps composition lineage across submitted runs while request sequences and turn indexes restart', () => {
  const h = fixture()
  h.emit('agent_start'); h.emit('turn_start', { turnIndex: 0 }); h.emit('context_with_system', context()); h.emit('turn_end', response())
  h.state.leaf = 'tool-results-entry'
  h.emit('turn_start', { turnIndex: 1 }); h.emit('context_with_system', context()); h.emit('turn_end', response(1))
  const firstRun = correlation(h.records[0]!).run_id
  const lineage = correlation(h.records[0]!).lineage_id
  expect(correlation(h.records[2]!)).toMatchObject({ run_id: firstRun, request_seq: 2, turn_index: 1, request_origin_entry_id: 'tool-results-entry' })
  h.emit('agent_settled'); h.state.leaf = 'next-user-prompt-entry'
  h.emit('agent_start'); h.emit('turn_start', { turnIndex: 0 }); h.emit('context_with_system', context())
  expect(correlation(h.records[4]!).run_id).not.toBe(firstRun)
  expect(correlation(h.records[4]!)).toMatchObject({ request_seq: 1, turn_index: 0, lineage_id: lineage,
    lineage_origin_entry_id: 'native-user-entry', run_origin_entry_id: 'next-user-prompt-entry' })
  expect(correlation(h.records[4]!)).not.toHaveProperty('branch_id')
})

test('Pi resets composition lineage across tree navigation and resumed sessions', () => {
  const h = fixture()
  h.emit('agent_start'); h.emit('context_with_system', context())
  const runs = [correlation(h.records.at(-1)!).run_id]
  const lineages = [correlation(h.records.at(-1)!).lineage_id]
  h.emit('session_tree', { oldLeafId: 'native-user-entry', newLeafId: 'native-user-entry' }); h.emit('context_with_system', context())
  expect(correlation(h.records.at(-1)!).lineage_id).toBe(lineages[0])
  for (const [event, payload] of [['session_tree', { oldLeafId: 'native-user-entry', newLeafId: 'fork-entry' }], ['session_start', {}]] as const) {
    h.emit(event, payload); h.emit('context_with_system', context())
    const current = correlation(h.records.at(-1)!)
    expect(current.request_seq).toBe(1); expect(current.run_source).toBe('observed')
    expect(runs).not.toContain(current.run_id); runs.push(current.run_id)
    expect(lineages).not.toContain(current.lineage_id); lineages.push(current.lineage_id)
  }
  h.state.session = 'resumed-pi-session'; h.state.leaf = 'resumed-native-entry'
  h.emit('context_with_system', context())
  expect(h.records.at(-1)!.session_id).toBe('resumed-pi-session')
  expect(correlation(h.records.at(-1)!)).toMatchObject({ request_seq: 1, request_origin_entry_id: 'resumed-native-entry' })
  expect(runs).not.toContain(correlation(h.records.at(-1)!).run_id)
  expect(lineages).not.toContain(correlation(h.records.at(-1)!).lineage_id)
})

test('Pi compaction preserves composition lineage, clears in-flight correlation, and records the native marker', () => {
  const h = fixture()
  h.emit('agent_start'); h.emit('turn_start', { turnIndex: 0 }); h.emit('context_with_system', context())
  const before = correlation(h.records.at(-1)!)
  const compact = freeze({ reason: 'threshold', willRetry: false, fromExtension: false,
    compactionEntry: { type: 'compaction', id: 'native-compact-entry', parentId: 'native-old-entry', timestamp: '2026-11-25T04:00:00Z',
      summary: 'Private compacted summary', firstKeptEntryId: 'native-kept-entry', tokensBefore: 12000,
      usage: { input: 10000, output: 300, cacheRead: 1000, cacheWrite: 0, totalTokens: 11300 },
      systemMessage: { role: 'system', sections: { preamble: 'Retained instructions' }, content: '', timestamp: 4 } } })
  const original = structuredClone(compact)
  h.state.leaf = 'native-compact-entry'; h.emit('session_compact', compact)
  const marker = h.records.at(-1)!
  expect(marker.stage).toBe('lifecycle.compaction'); expect(marker.kind).toBe('compaction')
  expect(marker.data).toMatchObject({ ...compact, type: 'session_compact', provenance: { hook: 'session_compact', affects_next_context: true,
    request_payload_exposed: false, model_source: 'active_session_not_verified_summarizer' } })
  expect(compact).toEqual(original)
  expect(correlation(marker)).toMatchObject({ lineage_id: before.lineage_id, run_id: before.run_id, confidence: 'native_compaction' })
  expect(correlation(marker)).not.toHaveProperty('request_seq')
  h.emit('before_provider_request', provider())
  expect(correlation(h.records.at(-1)!)).toMatchObject({ confidence: 'unlinked_provider_request', lineage_id: before.lineage_id })
  h.emit('turn_start', { turnIndex: 1 }); h.emit('context_with_system', context())
  expect(correlation(h.records.at(-1)!)).toMatchObject({ lineage_id: before.lineage_id, run_id: before.run_id, request_seq: 2,
    request_origin_entry_id: 'native-compact-entry' })
})

test('Pi counts provider callbacks without inventing network attempts or linking unknown calls', () => {
  const h = fixture()
  h.emit('before_provider_request', provider())
  expect(h.records[0]!.kind).toBeUndefined()
  expect(correlation(h.records[0]!)).toMatchObject({ confidence: 'unlinked_provider_request', provider_request_id_available: false })
  expect(correlation(h.records[0]!)).not.toHaveProperty('request_seq')
  h.emit('turn_end', response())
  expect(correlation(h.records[1]!)).toMatchObject({ confidence: 'unlinked_turn_response' })
  expect(correlation(h.records[1]!)).not.toHaveProperty('request_seq')
  h.emit('turn_start', { turnIndex: 1 }); h.emit('context_with_system', context())
  h.emit('before_provider_request', provider()); h.emit('before_provider_request', provider()); h.emit('turn_end', response(1))
  expect(correlation(h.records[3]!)).toMatchObject({ request_seq: 1, provider_event_seq: 1 })
  expect(correlation(h.records[4]!)).toMatchObject({ request_seq: 1, provider_event_seq: 2 })
  expect(correlation(h.records[5]!)).toMatchObject({ request_seq: 1, provider_event_seq: 2, confidence: 'native_turn_response' })
})

test('Pi disabled capture never reads session data, probes Python, or records prompts', async () => {
  const h = fixture(false)
  h.ctx.sessionManager.getSessionId = () => { throw new Error('disabled capture read session') }
  h.emit('agent_start'); h.emit('turn_start', { turnIndex: 0 }); h.emit('context_with_system', context())
  h.emit('before_provider_request', provider()); h.emit('turn_end', response())
  expect(h.records).toEqual([])
  await h.capture.close()
  expect(h.state.closes).toBe(1)
})

test('Pi capture errors and shutdown cannot replace prompts or disrupt the host', async () => {
  const h = fixture()
  h.sink.record = () => { throw new Error('private writer failure') }
  h.sink.flush = async () => { throw new Error('private flush failure') }
  h.sink.close = async () => { h.state.closes++; throw new Error('private close failure') }
  h.emit('agent_start'); h.emit('turn_start', { turnIndex: 0 }); h.emit('context_with_system', context())
  h.emit('before_provider_request', provider()); h.emit('turn_end', response())
  h.sink.record = event => h.records.push(event)
  h.emit('before_provider_request', provider())
  expect(correlation(h.records.at(-1)!)).toMatchObject({ confidence: 'unlinked_provider_request' })
  h.records.length = 0
  await h.capture.flush()
  await Promise.all([h.capture.close(), h.capture.close()])
  expect(h.state.closes).toBe(1)
  h.sink.record = event => h.records.push(event)
  h.emit('context_with_system', context())
  expect(h.records).toEqual([])
})

test('Pi callbacks do nothing before the controller is available', () => {
  const callbacks: Hook[] = []
  registerPromptCapture({ on: (_name: string, handler: Hook) => { callbacks.push(handler); return () => {} } } as unknown as ExtensionAPI, () => undefined)
  for (const callback of callbacks) expect(callback({}, {} as ExtensionContext)).toBeUndefined()
})

const python = Bun.which('python3') ?? Bun.which('python')
test.skipIf(!python)('Pi writes complete local snapshots through the private shared writer', async () => {
  const temporary = await realpath(await mkdtemp(path.join(tmpdir(), 'trajviz-pi-prompt-capture-')))
  const root = path.join(temporary, 'capture')
  const previous = [process.env.TRAJVIZ_OBS_PROMPT_CAPTURE, process.env.TRAJVIZ_OBS_PROMPT_CAPTURE_DIR]
  let capture: PiPromptCapture | undefined
  try {
    process.env.TRAJVIZ_OBS_PROMPT_CAPTURE = '1'; process.env.TRAJVIZ_OBS_PROMPT_CAPTURE_DIR = root
    capture = new PiPromptCapture({ bridge: path.resolve(import.meta.dir, '../../bridge'), telemetryPython: () => python ?? undefined }, '1.5.2')
    const h = fixture(), original = context(), payload = provider(), result = response()
    capture.start(h.ctx); capture.turn({ type: 'turn_start', turnIndex: 0, timestamp: 1 }, h.ctx)
    capture.context(original, h.ctx); capture.provider(payload, h.ctx); capture.response(result, h.ctx)
    await capture.close()
    const files = (await readdir(root, { recursive: true })).filter(file => file.endsWith('.json'))
    expect(files).toHaveLength(3)
    const records: ObjectData[] = await Promise.all(files.map(file => readFile(path.join(root, file), 'utf8').then(JSON.parse)))
    records.sort((a, b) => a.sequence - b.sequence)
    expect(records.map(record => record.stage)).toEqual(['context', 'provider.request', 'response'])
    expect(records[0]!.data.messages).toEqual(original.messages)
    expect(records[1]!.data.payload).toEqual(payload.payload)
    expect(records[2]!.data.messageEntryId).toBe('native-assistant-entry')
    expect(records.every(record => record.adapter === 'pi' && record.host_version === '1.5.2')).toBe(true)
    if (process.platform !== 'win32') for (const file of files) expect((await stat(path.join(root, file))).mode & 0o777).toBe(0o600)
  } finally {
    await capture?.close()
    for (const [index, key] of ['TRAJVIZ_OBS_PROMPT_CAPTURE', 'TRAJVIZ_OBS_PROMPT_CAPTURE_DIR'].entries()) {
      const value = previous[index]
      if (value === undefined) delete process.env[key]; else process.env[key] = value
    }
    await rm(temporary, { recursive: true, force: true })
  }
}, 10000)
