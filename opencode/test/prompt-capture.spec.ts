import { expect, test } from 'bun:test'
import { registerPromptCapture } from '../src/prompt-capture.js'
import { PromptCapture, promptExplorerArgs } from '../src/shared/prompt-capture.js'

type Host = Parameters<typeof registerPromptCapture>[0]
type Capture = Parameters<typeof registerPromptCapture>[1]
type RecordInput = Parameters<Capture['record']>[0]

function host(enabled = true) {
  const callbacks = new Map<string, (event: any) => unknown>()
  const disposed: string[] = [], records: RecordInput[] = []
  let closed = 0
  const capture = { enabled, record(value: RecordInput) { records.push(JSON.parse(JSON.stringify(value))) },
    async close() { closed++ } } as Capture
  const ctx = { session: { async hook(name: string, callback: (event: unknown) => unknown) {
    callbacks.set(name, callback)
    return { async dispose() { disposed.push(name) } }
  } } } as unknown as Host
  const event = (sessionID = 'session-a', kind = 'primary') => ({ sessionID, kind, agent: 'build', model: { providerID: 'test', id: 'model' } })
  return { ctx, capture, records, callbacks, disposed, event, closed: () => closed }
}

function freeze<T>(value: T): T {
  if (value && typeof value === 'object') { Object.values(value).forEach(freeze); Object.freeze(value) }
  return value
}

test('packaged OpenCode Prompt Explorer helper requires host opt-in, absolute destination, and the native session ID', () => {
  const keys = ['TRAJVIZ_OBS_PROMPT_CAPTURE', 'TRAJVIZ_OBS_PROMPT_CAPTURE_DIR', 'TRAJVIZ_OBS_TELEMETRY', 'OTEL_LOG_RAW_API_BODIES'] as const
  const previous = keys.map(key => [key, process.env[key]] as const)
  try {
    process.env.TRAJVIZ_OBS_TELEMETRY = '1'
    process.env.TRAJVIZ_OBS_PROMPT_CAPTURE_DIR = '/private/capture'
    for (const flag of [undefined, '', '0', 'true', ' 1', '1 ']) {
      if (flag === undefined) delete process.env.TRAJVIZ_OBS_PROMPT_CAPTURE
      else process.env.TRAJVIZ_OBS_PROMPT_CAPTURE = flag
      expect(promptExplorerArgs('ses_native')).toEqual([])
    }
    process.env.TRAJVIZ_OBS_PROMPT_CAPTURE = '1'
    for (const root of [undefined, '', 'relative/prompts', '~/.capture']) {
      if (root === undefined) delete process.env.TRAJVIZ_OBS_PROMPT_CAPTURE_DIR
      else process.env.TRAJVIZ_OBS_PROMPT_CAPTURE_DIR = root
      expect(promptExplorerArgs('ses_native')).toEqual([])
    }
    process.env.TRAJVIZ_OBS_PROMPT_CAPTURE_DIR = '/private/capture'
    for (const id of ['', 'bad\0session', 'x'.repeat(4097)]) expect(promptExplorerArgs(id)).toEqual([])
    process.env.OTEL_LOG_RAW_API_BODIES = 'file:/private/unrelated-claude-api'
    expect(promptExplorerArgs('ses_native')).toEqual(['--prompt-capture-root', '/private/capture', '--prompt-session-id', 'ses_native'])
    expect(promptExplorerArgs('x'.repeat(4096))).toHaveLength(4)
  } finally {
    for (const [key, value] of previous) if (value === undefined) delete process.env[key]; else process.env[key] = value
  }
})

test('disabled prompt capture registers no hooks and closes without recording or worker work', async () => {
  const h = host(false)
  const close = await registerPromptCapture(h.ctx, h.capture)
  expect(h.callbacks.size).toBe(0)
  expect(h.records).toEqual([])
  await Promise.all([close(), close()])
  expect(h.closed()).toBe(1)
  expect(h.disposed).toEqual([])
})

test('real capture writer never probes Python without both explicit consent and an absolute destination', async () => {
  const keys = ['TRAJVIZ_OBS_PROMPT_CAPTURE', 'TRAJVIZ_OBS_PROMPT_CAPTURE_DIR'] as const
  const previous = keys.map(key => [key, process.env[key]] as const)
  let probes = 0
  try {
    for (const [flag, directory] of [[undefined, '/private/tmp/unused-capture'], ['0', '/private/tmp/unused-capture'], ['yes', '/private/tmp/unused-capture'], ['1', 'relative/capture'], ['1', undefined]]) {
      if (flag === undefined) delete process.env.TRAJVIZ_OBS_PROMPT_CAPTURE
      else process.env.TRAJVIZ_OBS_PROMPT_CAPTURE = flag
      if (directory === undefined) delete process.env.TRAJVIZ_OBS_PROMPT_CAPTURE_DIR
      else process.env.TRAJVIZ_OBS_PROMPT_CAPTURE_DIR = directory
      const capture = new PromptCapture({ adapter: 'opencode', bridge: '/unused', python: () => { probes++; throw new Error('Must not run') } })
      const h = host()
      const close = await registerPromptCapture(h.ctx, capture)
      expect(capture.enabled).toBe(false)
      capture.record({ session_id: 'unused', stage: 'context', data: { messages: ['private data'] } })
      await capture.flush()
      await close()
      expect(h.callbacks.size).toBe(0)
    }
    expect(probes).toBe(0)
  } finally {
    for (const [key, value] of previous) if (value === undefined) delete process.env[key]; else process.env[key] = value
  }
})

test('semantic captures preserve frozen prompt objects and keep request kinds distinct', async () => {
  const h = host()
  const close = await registerPromptCapture(h.ctx, h.capture)
  const original = freeze({ ...h.event(), system: [{ type: 'text', text: 'System instructions', metadata: { experimental: { instructions: [{ key: 'core/date', text: 'Today' }] } } }],
    messages: [{ role: 'user', content: [{ type: 'text', text: 'Task' }] }],
    tools: { read: { description: 'Read a file', input: { type: 'object' } } },
    options: { temperature: 0.2, maxTokens: 1000, headers: { authorization: 'private-token' }, apiKey: 'private-token' } })
  const before = JSON.stringify(original)
  for (const kind of ['context', 'compaction', 'generate', 'title']) {
    expect(() => h.callbacks.get(kind)!(original)).not.toThrow()
  }
  await close()
  expect(JSON.stringify(original)).toBe(before)
  expect(h.records.map(record => record.kind)).toEqual(['primary', 'compaction', 'generate', 'title'])
  for (const record of h.records) {
    expect(record.stage).toBe('context')
    expect(record.model).toEqual(original.model)
    const data = record.data as any
    expect(data.system).toEqual(original.system)
    expect(data.messages).toEqual(original.messages)
    expect(data.tools).toEqual(original.tools)
    expect(data.options).toEqual({ maxTokens: 1000, temperature: 0.2 })
    expect(data.provenance.later_hooks).toBe('not_observed')
    expect(record.correlation).toMatchObject({ pairing: 'independent', agent_id: 'build' })
  }
  expect(JSON.stringify(h.records)).not.toContain('private-token')
  expect(h.disposed).toHaveLength(6)
  expect(h.closed()).toBe(1)
})

test('HTTP capture reads a clone and excludes the URL and authentication headers', async () => {
  const h = host()
  const close = await registerPromptCapture(h.ctx, h.capture)
  const payload = { model: 'test-model', messages: [{ role: 'user', content: 'Request content' }] }
  const text = JSON.stringify(payload)
  const request = new Request('https://provider.example/messages?api_key=private-url-token', {
    method: 'POST', headers: { 'content-type': 'application/json', authorization: 'Bearer private-header-token' }, body: text,
  })
  const original = Object.freeze({ ...h.event(), request })
  const identity = original.request
  h.callbacks.get('http.request')!(original)
  await close()
  expect(original.request).toBe(identity)
  expect(request.bodyUsed).toBe(false)
  expect(request.headers.get('authorization')).toBe('Bearer private-header-token')
  expect(await request.text()).toBe(text)
  expect(h.records).toHaveLength(1)
  expect(h.records[0]).toMatchObject({ session_id: 'session-a', stage: 'provider.request', kind: 'primary',
    data: { transport: 'http', payload } })
  const recorded = JSON.stringify(h.records)
  expect(recorded).not.toContain('private-url-token')
  expect(recorded).not.toContain('private-header-token')
  expect(recorded).not.toContain('provider.example')
})

test('WebSocket capture observes native frames without rewriting a frozen event', async () => {
  const h = host()
  const close = await registerPromptCapture(h.ctx, h.capture)
  const payload = { type: 'response.create', response: { input: [{ role: 'user', content: 'Continue' }], previous_response_id: 'provider-state' } }
  const original = freeze({ ...h.event('child', 'generate'), frame: JSON.stringify(payload) })
  h.callbacks.get('experimental.ws.send')!(original)
  await close()
  expect(original.frame).toBe(JSON.stringify(payload))
  expect(h.records[0]).toMatchObject({ session_id: 'child', stage: 'provider.request', kind: 'generate',
    data: { transport: 'websocket', payload } })
})

test('asynchronous body reads retain the model and identity observed at the hook boundary', async () => {
  const h = host()
  const close = await registerPromptCapture(h.ctx, h.capture)
  const event = { ...h.event(), request: new Request('https://provider.example', { method: 'POST', body: '{}' }) }
  h.callbacks.get('http.request')!(event)
  event.sessionID = 'later-session'
  event.agent = 'later-agent'
  event.kind = 'title'
  event.model.id = 'later-model'
  await close()
  expect(h.records[0]).toMatchObject({ session_id: 'session-a', kind: 'primary', model: { id: 'model' },
    correlation: { agent_id: 'build' }, data: { agent: 'build' } })
})

test('an already consumed provider request yields read failure evidence without throwing', async () => {
  const h = host()
  const close = await registerPromptCapture(h.ctx, h.capture)
  const request = new Request('https://provider.example', { method: 'POST', body: '{}' })
  await request.text()
  expect(() => h.callbacks.get('http.request')!({ ...h.event(), request })).not.toThrow()
  await close()
  expect(h.records[0]?.data).toMatchObject({ transport: 'http', payload: null, omitted_reason: 'read_failed' })
  expect(request.bodyUsed).toBe(true)
})

test('concurrent sessions and auxiliary calls remain independently identified', async () => {
  const h = host()
  const close = await registerPromptCapture(h.ctx, h.capture)
  for (const [session, kind] of [['session-a', 'primary'], ['session-a', 'title'], ['child-session', 'primary'], ['session-a', 'generate']] as const) {
    const event = h.event(session, kind)
    h.callbacks.get('http.request')!({ ...event, request: new Request('https://provider.example', { method: 'POST', body: JSON.stringify({ session, kind }) }) })
  }
  await close()
  expect(h.records).toHaveLength(4)
  expect(new Set(h.records.map(record => (record.correlation as any).capture_id)).size).toBe(4)
  expect(new Set(h.records.map(record => (record.correlation as any).observation_sequence)).size).toBe(4)
  for (const record of h.records) {
    expect(record.correlation).toMatchObject({ pairing: 'independent', agent_id: 'build' })
    expect(record.correlation).not.toHaveProperty('step_id')
    expect(record.data).toMatchObject({ payload: { session: record.session_id, kind: record.kind } })
  }
})

test('HTTP observations retain dispatch order and timestamps when cloned bodies finish in reverse', async () => {
  const h = host()
  const close = await registerPromptCapture(h.ctx, h.capture)
  const body = (marker: string) => {
    let send!: () => void
    const stream = new ReadableStream<Uint8Array>({ start(controller) {
      let completed = false
      send = () => {
        if (completed) return
        completed = true
        controller.enqueue(new TextEncoder().encode(JSON.stringify({ marker })))
        controller.close()
      }
    } })
    return { request: new Request('https://provider.example', { method: 'POST', body: stream as any, duplex: 'half' } as any), send }
  }
  const slow = body('slow'), fast = body('fast')
  let first!: () => void
  const firstRecord = new Promise<void>(resolve => { first = resolve })
  const record = h.capture.record
  h.capture.record = value => { record(value); first() }
  try {
    h.callbacks.get('http.request')!({ ...h.event(), request: slow.request })
    await Bun.sleep(2)
    h.callbacks.get('http.request')!({ ...h.event(), request: fast.request })
    fast.send()
    await firstRecord
    expect(h.records).toHaveLength(1)
    expect(h.records[0]?.data).toMatchObject({ payload: { marker: 'fast' } })
    slow.send()
    await close()
    expect(h.records.map(item => (item.correlation as any).observation_sequence)).toEqual([2, 1])
    const [fastObserved, slowObserved] = h.records.map(item => (item.correlation as any).observed_at)
    expect(typeof fastObserved).toBe('string')
    expect(typeof slowObserved).toBe('string')
    expect(Date.parse(slowObserved)).toBeLessThanOrEqual(Date.parse(fastObserved))
    expect(h.records.every(item => item.session_id === 'session-a' && item.kind === 'primary')).toBe(true)
  } finally {
    slow.send(); fast.send()
    await close()
  }
})

test('oversized and invalid native payloads produce omission evidence without raw partial content', async () => {
  const h = host()
  const close = await registerPromptCapture(h.ctx, h.capture)
  const invalid = 'PRIVATE INVALID JSON'
  const oversized = 'PRIVATE'.repeat(400_000)
  h.callbacks.get('http.request')!({ ...h.event(), request: new Request('https://provider.example', { method: 'POST', body: invalid }) })
  h.callbacks.get('http.request')!({ ...h.event(), request: new Request('https://provider.example', { method: 'POST', headers: { 'content-length': String(oversized.length) }, body: oversized }) })
  h.callbacks.get('experimental.ws.send')!({ ...h.event(), frame: invalid })
  h.callbacks.get('experimental.ws.send')!({ ...h.event(), frame: oversized })
  await close()
  expect(h.records).toHaveLength(4)
  expect(h.records.map(record => (record.data as any).omitted_reason).sort()).toEqual(['invalid_json', 'invalid_json', 'size_limit', 'size_limit'])
  expect(JSON.stringify(h.records)).not.toContain('PRIVATE')
})

test('slow clone reads are bounded and cleanup never waits for the provider body', async () => {
  const h = host()
  const close = await registerPromptCapture(h.ctx, h.capture)
  const requests = Array.from({ length: 5 }, () => new Request('https://provider.example', { method: 'POST', body: new ReadableStream() as any, duplex: 'half' } as any))
  const started = performance.now()
  for (const request of requests) h.callbacks.get('http.request')!({ ...h.event(), request })
  await close()
  expect(performance.now() - started).toBeLessThan(2000)
  expect(h.records.map(record => (record.data as any).omitted_reason).sort()).toEqual(['capture_busy', 'read_timeout', 'read_timeout', 'read_timeout', 'read_timeout'])
  expect(requests.every(request => !request.bodyUsed)).toBe(true)
  for (const request of requests) await request.body!.cancel()
})

test('recording errors, unavailable hooks and late events cannot disrupt host requests', async () => {
  const h = host()
  const originalHook = h.ctx.session.hook
  const ctx = { ...h.ctx, session: { ...h.ctx.session, hook: (async (name: string, callback: any) => {
    if (name === 'compaction') throw new Error('Unavailable')
    return (originalHook as any)(name, callback)
  }) as typeof h.ctx.session.hook } }
  h.capture.record = () => { throw new Error('Storage unavailable') }
  const close = await registerPromptCapture(ctx, h.capture)
  expect(h.callbacks.has('context')).toBe(true)
  expect(h.callbacks.has('experimental.ws.send')).toBe(true)
  const context = freeze({ ...h.event(), system: [], messages: [], tools: {}, options: {} })
  expect(() => h.callbacks.get('context')!(context)).not.toThrow()
  expect(() => h.callbacks.get('experimental.ws.send')!({ ...h.event(), frame: '{}' })).not.toThrow()
  await close()
  h.capture.record = value => h.records.push(value)
  h.callbacks.get('context')!(context)
  expect(h.records).toEqual([])
})
