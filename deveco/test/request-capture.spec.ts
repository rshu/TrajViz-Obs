import { describe, expect, test } from 'bun:test'
import { createHash } from 'node:crypto'
import { chmod, lstat, mkdtemp, readFile, readdir, rm, symlink, writeFile } from 'node:fs/promises'
import http, { type IncomingMessage, type ServerResponse } from 'node:http'
import os from 'node:os'
import path from 'node:path'
import { RequestCapture, type RequestCaptureOptions, type RequestContext } from '../src/request-capture.js'

type Seen = { body: Buffer; url: string; headers: http.IncomingHttpHeaders }
type Row = Record<string, any>
const hash = (value: string | Buffer) => createHash('sha256').update(value).digest('hex')
const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))
const context: RequestContext = { basis: 'native_processor_span', trace_id: 'a'.repeat(32), span_id: 'b'.repeat(16), purpose: 'assistant' }
const input = (session = 'session-a', extra: Record<string, unknown> = {}) => ({ sessionID: session, message: { id: 'user-a' }, agent: 'build', model: { providerID: 'test', id: 'model' }, ...extra })
async function rows(root: string, session = 'session-a'): Promise<Row[]> {
  const folder = path.join(root, hash(session))
  try { return await Promise.all((await readdir(folder)).filter(name => name.endsWith('.json')).map(async name => JSON.parse(await readFile(path.join(folder, name), 'utf8')))) } catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return []; throw error }
}
async function until<T>(read: () => Promise<T>, ready: (value: T) => boolean): Promise<T> {
  for (let i = 0; i < 200; i++) { const value = await read(); if (ready(value)) return value; await sleep(10) }
  throw new Error('capture metadata did not settle')
}
async function fixture(options: RequestCaptureOptions = {}, respond?: (request: IncomingMessage, response: ServerResponse, seen: Seen) => void) {
  const root = await mkdtemp(path.join(os.tmpdir(), 'deveco-capture-'))
  const seen: Seen[] = []
  const server = http.createServer(async (request, response) => {
    const chunks: Buffer[] = []
    for await (const chunk of request) chunks.push(Buffer.from(chunk))
    const row = { body: Buffer.concat(chunks), url: request.url!, headers: request.headers }
    seen.push(row)
    if (respond) respond(request, response, row)
    else { response.writeHead(200, { 'content-type': 'application/json' }); response.end('{"ok":true}') }
  })
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  const address = server.address() as { port: number }
  const original = `http://127.0.0.1:${address.port}/v1`
  const config = { model: 'test/model', small_model: 'test/model', provider: { test: { options: { baseURL: original, apiKey: 'DUMMY-NOT-A-REAL-KEY', headers: { 'x-provider-option': 'retained' }, other: { temperature: 0.2 } } } } }
  const capture = new RequestCapture({ ...options, env: { TRAJVIZ_OBS_REQUEST_CAPTURE: '1', TRAJVIZ_OBS_REQUEST_CAPTURE_DIR: root, TRAJVIZ_OBS_REQUEST_CAPTURE_PROVIDERS: 'test', ...options.env } })
  const prepare = async (session = 'session-a', requestContext: RequestContext | undefined = context) => {
    const output = { headers: {} as Record<string, string> }
    await capture.headers(input(session, requestContext === undefined ? {} : { requestContext }), output)
    return output.headers
  }
  const close = async () => {
    await capture.close()
    server.closeAllConnections()
    await new Promise<void>(resolve => server.close(() => resolve()))
    await rm(root, { recursive: true, force: true })
  }
  return { root, seen, original, config, capture, prepare, close, local: () => config.provider.test.options.baseURL }
}

function post(url: string, headers: Record<string, string>, body = '{"messages":[]}') {
  return fetch(url, { method: 'POST', headers: { 'content-type': 'application/json', ...headers }, body })
}

describe('DevEco exact serialized HTTP request capture', () => {
  test('retains exact UTF-8 request bytes, fixed routing, private files and authoritative trace identity', async () => {
    const f = await fixture()
    try {
      const options = f.config.provider.test.options
      await f.capture.configure(f.config)
      expect(f.config.model).toBe('test/model'); expect(f.config.small_model).toBe('test/model')
      expect(f.config.provider.test.options).toBe(options)
      expect(options.apiKey).toBe('DUMMY-NOT-A-REAL-KEY'); expect(options.other).toEqual({ temperature: 0.2 })
      const body = ' {"model":"model","system":"系统 instruction","tools":[{"name":"read","parameters":{"type":"object"}}],"messages":[{"role":"user","content":[{"type":"image_url","image_url":{"url":"data:image/png;base64,AA=="}},{"type":"text","text":"你好"}]}],"temperature":0.2}\n'
      const response = await post(f.local() + '/chat/completions?routing=one%20two', { ...await f.prepare(), authorization: 'Bearer DUMMY-SECRET', 'x-trajviz-extra': 'private', 'connection': 'x-connection-private', 'x-connection-private': 'private' }, body)
      expect(response.status).toBe(200); await response.text()
      const [record] = await until(() => rows(f.root), rows => rows.length === 1 && rows[0]!.dispatch_status === 'complete')
      expect(record!.request_id).toMatch(/^[a-f0-9-]{36}$/)
      expect(record!.body_ref).toBe(record!.request_id + '.body')
      const bytes = await readFile(path.join(f.root, hash('session-a'), record!.body_ref))
      expect(bytes.equals(Buffer.from(body))).toBe(true)
      expect(record!.body_sha256).toBe(hash(bytes)); expect(record!.body_bytes).toBe(Buffer.byteLength(body))
      expect(record!.coverage).toEqual({ complete: true, omitted_fields: [], redacted_fields: [] })
      expect(record!.mapping).toEqual({ status: 'pending', basis: 'native_processor_span' })
      expect(record!.trace_id).toBe('a'.repeat(32)); expect(record!.span_id).toBe('b'.repeat(16))
      expect(record!.previous_request_id).toBeNull(); expect(record!.assistant_message_id).toBeNull()
      expect(f.seen[0]!.url).toBe('/v1/chat/completions?routing=one%20two')
      expect(f.seen[0]!.body.equals(bytes)).toBe(true)
      expect(f.seen[0]!.headers.authorization).toBe('Bearer DUMMY-SECRET')
      expect(Object.keys(f.seen[0]!.headers).some(key => key.startsWith('x-trajviz-'))).toBe(false)
      expect(f.seen[0]!.headers['x-connection-private']).toBeUndefined()
      expect(JSON.stringify(record)).not.toContain('DUMMY-SECRET')
      expect(JSON.stringify(record)).not.toContain('DUMMY-NOT-A-REAL-KEY')
      expect((await lstat(f.root)).mode & 0o777).toBe(0o700)
      expect((await lstat(path.join(f.root, hash('session-a')))).mode & 0o777).toBe(0o700)
      for (const name of await readdir(path.join(f.root, hash('session-a')))) expect((await lstat(path.join(f.root, hash('session-a'), name))).mode & 0o777).toBe(0o600)
    } finally { await f.close() }
  })

  test('records separate physical retry bodies and preserves provider error responses', async () => {
    let attempt = 0
    const f = await fixture({}, (_request, response) => { response.writeHead(++attempt === 1 ? 503 : 200); response.end(attempt === 1 ? 'retry later' : 'ok') })
    try {
      await f.capture.configure(f.config)
      const headers = await f.prepare()
      const first = await post(f.local() + '/chat/completions', headers, '{"attempt":1}')
      expect(first.status).toBe(503); expect(await first.text()).toBe('retry later')
      const second = await post(f.local() + '/chat/completions', headers, '{"attempt":2}')
      expect(second.status).toBe(200); await second.text()
      const records = await until(() => rows(f.root), rows => rows.length === 2 && rows.every(row => row.dispatch_status === 'complete'))
      expect(new Set(records.map(row => row.request_id)).size).toBe(2)
      expect(new Set(records.map(row => row.invocation_id)).size).toBe(1)
      expect(records.map(row => row.status).sort()).toEqual([200, 503])
      expect(new Set(records.map(row => row.body_sha256)).size).toBe(2)
    } finally { await f.close() }
  })

  test('streams SSE before upstream completion while exact request is already available', async () => {
    let release!: () => void
    const gate = new Promise<void>(resolve => { release = resolve })
    const f = await fixture({}, (_request, response) => { response.writeHead(200, { 'content-type': 'text/event-stream' }); response.write('data: first\n\n'); void gate.then(() => response.end('data: [DONE]\n\n')) })
    try {
      await f.capture.configure(f.config)
      const response = await post(f.local() + '/chat/completions', await f.prepare())
      const reader = response.body!.getReader()
      const chunk = await Promise.race([reader.read(), sleep(1500).then(() => { throw new Error('SSE was buffered') })])
      expect(new TextDecoder().decode(chunk.value)).toContain('data: first')
      const records = await rows(f.root)
      expect(records).toHaveLength(1); expect(records[0]!.coverage.complete).toBe(true)
      expect(records[0]!.dispatch_status).toBe('sent')
      expect(await readFile(path.join(f.root, hash('session-a'), records[0]!.body_ref), 'utf8')).toBe('{"messages":[]}')
      release()
      while (!(await reader.read()).done) { /* drain live stream */ }
      await until(() => rows(f.root), rows => rows[0]?.dispatch_status === 'complete')
    } finally { release(); await f.close() }
  })

  test('keeps concurrent sessions and their authoritative assistant context separate', async () => {
    const f = await fixture()
    try {
      await f.capture.configure(f.config)
      const headersA = await f.prepare('session-a', { basis: 'host_processor_context', assistantMessageID: 'assistant-a', previousRequestID: 'explicit-parent' })
      const headersB = await f.prepare('session-b', { ...context, trace_id: 'c'.repeat(32), span_id: 'd'.repeat(16) })
      const responses = await Promise.all([post(f.local() + '/chat/completions', headersA, '{"session":"a"}'), post(f.local() + '/chat/completions', headersB, '{"session":"b"}')])
      await Promise.all(responses.map(response => response.text()))
      const [a] = await until(() => rows(f.root), rows => rows[0]?.dispatch_status === 'complete')
      const [b] = await until(() => rows(f.root, 'session-b'), rows => rows[0]?.dispatch_status === 'complete')
      expect(a!.mapping).toEqual({ status: 'provided', basis: 'host_processor_context' })
      expect(a!.assistant_message_id).toBe('assistant-a'); expect(a!.previous_request_id).toBe('explicit-parent')
      expect(b!.trace_id).toBe('c'.repeat(32)); expect(b!.assistant_message_id).toBeNull()
      expect(a!.invocation_id).not.toBe(b!.invocation_id)
      expect(await readFile(path.join(f.root, hash('session-b'), b!.body_ref), 'utf8')).toBe('{"session":"b"}')
    } finally { await f.close() }
  })

  test('accepts injected native resolver, refuses identity conflicts, and marks absent context unlinked', async () => {
    const f = await fixture({ resolveContext: async value => value.sessionID === 'session-b' ? context : undefined })
    try {
      await f.capture.configure(f.config)
      const response = await post(f.local() + '/chat/completions', await f.prepare('session-a'))
      await response.text()
      const [record] = await until(() => rows(f.root), rows => rows[0]?.dispatch_status === 'complete')
      expect(record!.mapping).toEqual({ status: 'unlinked', basis: 'absent' })
      expect(record!.trace_id).toBeNull(); expect(record!.assistant_message_id).toBeNull()
      const output = { headers: {} as Record<string, string> }
      await f.capture.headers(input('session-b'), output)
      expect(output.headers['x-trajviz-capture-id']).toBeDefined()
    } finally { await f.close() }
    const conflicting = await fixture()
    try {
      await conflicting.capture.configure(conflicting.config)
      await expect(conflicting.prepare('session-a', { ...context, sessionID: 'foreign-session' })).rejects.toThrow('disagrees')
      await expect(conflicting.prepare('session-a', { ...context, invocationID: 'opaque-not-a-uuid' })).rejects.toThrow('canonical UUID')
      const explicit = await conflicting.prepare('session-a', { ...context, invocationID: '12345678-1234-1234-1234-123456789abc' })
      expect(explicit['x-trajviz-capture-id']).toBe('12345678-1234-1234-1234-123456789abc')
    } finally { await conflicting.close() }
  })

  test('refuses tampered correlation and URL escape without dispatching upstream', async () => {
    const f = await fixture()
    try {
      await f.capture.configure(f.config)
      const headers = await f.prepare()
      let response = await post(f.local() + '/chat/completions', { ...headers, 'x-trajviz-session-id': 'foreign' })
      expect(response.status).toBe(403); await response.text()
      response = await post(f.local() + '/chat/completions', { ...headers, 'x-trajviz-capture-id': 'forged' })
      expect(response.status).toBe(403); await response.text()
      // WHATWG URL normalizes this into the secret route prefix but outside fixed /v1.
      response = await post(f.local() + '/../outside', headers)
      expect(response.status).toBe(403); await response.text()
      expect(f.seen).toHaveLength(0)
    } finally { await f.close() }
  })

  test('fails closed for oversized, chunked, and unsupported request transports', async () => {
    const f = await fixture({ maxBodyBytes: 8 })
    try {
      await f.capture.configure(f.config)
      const headers = await f.prepare()
      const response = await post(f.local() + '/chat/completions', headers, '{"too":"large"}')
      expect(response.status).toBe(413); await response.text()
      const chunked = await new Promise<number>((resolve, reject) => {
        const request = http.request(f.local() + '/chat/completions', { method: 'POST', headers: { ...headers, 'transfer-encoding': 'chunked' } }, response => { response.resume(); resolve(response.statusCode!) })
        request.on('error', reject); request.write('a'); request.end('b')
      })
      expect(chunked).toBe(501); expect(f.seen).toHaveLength(0)
      const records = await rows(f.root)
      expect(records).toHaveLength(2)
      for (const record of records) { expect(record.coverage.complete).toBe(false); expect(record.body_ref).toBeNull(); expect(record.dispatch_status).toBe('rejected') }
      expect((await readdir(path.join(f.root, hash('session-a')))).some(name => name.endsWith('.body'))).toBe(false)
    } finally { await f.close() }
  })

  test('blocks redirects instead of forwarding credentials to another origin', async () => {
    const f = await fixture({}, (_request, response) => { response.writeHead(307, { location: 'https://another.invalid/never-follow' }); response.end('redirect') })
    try {
      await f.capture.configure(f.config)
      const response = await post(f.local() + '/chat/completions', { ...await f.prepare(), authorization: 'Bearer DUMMY' })
      expect(response.status).toBe(502); expect(response.headers.get('location')).toBeNull(); await response.text()
      const [record] = await until(() => rows(f.root), rows => rows[0]?.error === 'upstream_redirect_unsupported')
      expect(record!.coverage.complete).toBe(true); expect(record!.status).toBe(307)
      expect(f.seen).toHaveLength(1)
    } finally { await f.close() }
  })

  test('times out silent upstreams and closes active streaming requests idempotently', async () => {
    const timed = await fixture({ timeoutMs: 80 }, () => {})
    try {
      await timed.capture.configure(timed.config)
      const response = await post(timed.local() + '/chat/completions', await timed.prepare())
      expect(response.status).toBe(504); await response.text()
      await until(() => rows(timed.root), rows => rows[0]?.error === 'upstream_timeout')
    } finally { await timed.close() }
    const f = await fixture({}, (_request, response) => { response.writeHead(200, { 'content-type': 'text/event-stream' }); response.write('data: open\n\n') })
    try {
      await f.capture.configure(f.config)
      const response = await post(f.local() + '/chat/completions', await f.prepare())
      await response.body!.getReader().read()
      await Promise.race([Promise.all([f.capture.close(), f.capture.close()]), sleep(1500).then(() => { throw new Error('close left active requests hanging') })])
      expect(f.capture.enabled).toBe(false)
      await expect(f.capture.configure(f.config)).rejects.toThrow('closed')
    } finally { await f.close() }
  })

  test('disabled capture is inert and enabled capture requires explicit provider and storage scope', async () => {
    const config = { provider: { test: { options: { baseURL: 'https://example.invalid/v1' } } } }
    const disabled = new RequestCapture({ env: {} })
    await disabled.configure(config); await disabled.headers({}, { headers: {} }); await disabled.close()
    expect(config.provider.test.options.baseURL).toBe('https://example.invalid/v1')
    for (const env of [ { TRAJVIZ_OBS_REQUEST_CAPTURE: '1' }, { TRAJVIZ_OBS_REQUEST_CAPTURE: '1', TRAJVIZ_OBS_REQUEST_CAPTURE_PROVIDERS: 'test' } ]) {
      const capture = new RequestCapture({ env })
      await expect(capture.configure(config)).rejects.toThrow('TrajViz request capture')
      await capture.close()
    }
    const f = await fixture({ env: { DEVECO_EXPERIMENTAL_NATIVE_LLM: '1' } })
    try { await expect(f.capture.configure(f.config)).rejects.toThrow('native LLM transport') } finally { await f.close() }
  })

  test('preserves explicit baseURL query, reconfiguration identity, and small model options', async () => {
    const f = await fixture()
    try {
      f.config.provider.test.options.baseURL += '?tenant=synthetic%20one'
      await f.capture.configure(f.config)
      const local = new URL(f.local())
      expect(local.search).toBe('?tenant=synthetic%20one')
      await f.capture.configure(f.config)
      expect(f.local()).toBe(local.href)
      local.pathname += '/chat/completions'
      const response = await post(local.href, await f.prepare())
      await response.text()
      expect(f.seen[0]!.url).toBe('/v1/chat/completions?tenant=synthetic%20one')
      f.config.provider.test.options.baseURL = 'https://elsewhere.invalid/v1'
      await expect(f.capture.configure(f.config)).rejects.toThrow('restart DevEco')
    } finally { await f.close() }
  })

  test('rejects public or symlink capture roots and permits only private native trace journals', async () => {
    const f = await fixture()
    try {
      await chmod(f.root, 0o755)
      await expect(f.capture.configure(f.config)).rejects.toThrow('owner-only')
    } finally { await f.close() }
    const original = await mkdtemp(path.join(os.tmpdir(), 'deveco-capture-target-'))
    const parent = await mkdtemp(path.join(os.tmpdir(), 'deveco-capture-link-'))
    const link = path.join(parent, 'capture')
    await symlink(original, link)
    const c = new RequestCapture({ env: { TRAJVIZ_OBS_REQUEST_CAPTURE: '1', TRAJVIZ_OBS_REQUEST_CAPTURE_DIR: link, TRAJVIZ_OBS_REQUEST_CAPTURE_PROVIDERS: 'test' } })
    try { await expect(c.configure({ provider: { test: { options: { baseURL: 'http://127.0.0.1:1/v1' } } } })).rejects.toThrow('symlink') } finally { await c.close(); await rm(parent, { recursive: true, force: true }); await rm(original, { recursive: true, force: true }) }
    const journal = await fixture()
    try {
      await writeFile(path.join(journal.root, 'trace-spans.jsonl'), '{"version":1}\n', { mode: 0o600 })
      await journal.capture.configure(journal.config)
    } finally { await journal.close() }
  })

  test('reserves bounded storage across concurrent physical sends', async () => {
    const f = await fixture({ maxStorageBytes: 17_000 })
    try {
      await f.capture.configure(f.config)
      const headers = await f.prepare()
      const responses = await Promise.all([post(f.local() + '/chat/completions', headers, 'a'.repeat(100)), post(f.local() + '/chat/completions', headers, 'b'.repeat(100))])
      expect(responses.map(response => response.status).sort()).toEqual([200, 507])
      await Promise.all(responses.map(response => response.text()))
      expect(f.seen).toHaveLength(1)
    } finally { await f.close() }
  })
})
