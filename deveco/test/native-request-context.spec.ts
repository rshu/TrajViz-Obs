import { afterEach, expect, test } from 'bun:test'
import { context, trace, TraceFlags, ROOT_CONTEXT, type Context, type ContextManager } from '@opentelemetry/api'
import { AsyncLocalStorage } from 'node:async_hooks'
import { mkdtemp, readFile, rm, stat, realpath, chmod, mkdir, symlink, writeFile, open } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { resolve } from 'node:path'
import { gzipSync } from 'node:zlib'
import http from 'node:http'
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { getNativeTraceContext } from '../src/native-request-context.js'
// @ts-ignore The launcher is a native Node ES module, deliberately independent of Bun.
import { createTraceCollector, captureEnvironment } from '../scripts/capture-run.mjs'

const roots: string[] = []
afterEach(async () => { delete process.env.TRAJVIZ_NATIVE_TRACE_CAPTURE; context.disable(); await Promise.all(roots.splice(0).map(p => rm(p, { recursive: true, force: true }))) })
const tid = '1234567890abcdef1234567890abcdef'
const sid = '1234567890abcdef'
const input = { sessionID: 'ses_probe', agent: 'build', message: { id: 'msg_user', sessionID: 'ses_probe' } }
class Manager implements ContextManager {
  store = new AsyncLocalStorage<Context>()
  active() { return this.store.getStore() ?? ROOT_CONTEXT }
  with<A extends unknown[], F extends (...args: A) => ReturnType<F>>(c: Context, fn: F, thisArg?: ThisParameterType<F>, ...args: A): ReturnType<F> { return this.store.run(c, () => fn.apply(thisArg, args)) }
  bind<T>(_: Context, target: T): T { return target }
  enable() { return this }
  disable() { this.store.disable(); return this }
}

test('native context is explicitly enabled, recording, sampled, and preserves session ownership', async () => {
  expect(getNativeTraceContext(input)).toBeUndefined()
  context.setGlobalContextManager(new Manager())
  process.env.TRAJVIZ_NATIVE_TRACE_CAPTURE = '1'
  const span = { spanContext: () => ({ traceId: tid, spanId: sid, traceFlags: TraceFlags.SAMPLED }), isRecording: () => true }
  const c = trace.setSpan(ROOT_CONTEXT, span as any)
  await context.with(c, async () => {
    expect(getNativeTraceContext(input)).toEqual({ basis: 'native_processor_span', trace_id: tid, span_id: sid,
      sessionID: 'ses_probe', userMessageID: 'msg_user', purpose: 'assistant' })
    expect(getNativeTraceContext({ ...input, agent: 'title' })?.purpose).toBe('title')
    expect(getNativeTraceContext({ ...input, agent: 'compaction' })?.purpose).toBe('compaction')
    expect(getNativeTraceContext({ ...input, message: { id: 'msg_user', sessionID: 'ses_wrong' } })).toBeUndefined()
    expect(getNativeTraceContext({ ...input, sessionID: 'unsafe/session' })).toBeUndefined()
    await Promise.resolve()
    expect(getNativeTraceContext(input)?.span_id).toBe(sid)
  })
  expect(getNativeTraceContext(input)).toBeUndefined()
})


/** Exercise the wrapper's actual Node HTTP implementation. Bun's node:http
 * shim intermittently corrupts binary chunked gzip writes before our handler. */
async function nodeCollector(captureRoot: string) {
  const module = new URL('../scripts/capture-run.mjs', import.meta.url).href
  const script = `
    import { createInterface } from 'node:readline';
    import http from 'node:http';
    const { createTraceCollector } = await import(process.argv[2]);
    const collector = await createTraceCollector({ captureRoot: process.argv[1] });
    const send = value => process.stdout.write(JSON.stringify(value) + '\\n');
    send({ ready: { captureRoot: collector.captureRoot, endpoint: collector.endpoint,
      headers: collector.headers, journalPath: collector.journalPath } });
    const lines = createInterface({ input: process.stdin });
    for await (const line of lines) {
      const command = JSON.parse(line);
      try {
        let value;
        if (command.op === 'post') {
          const body = Buffer.from(command.body, 'base64');
          value = await new Promise((ok, fail) => {
            const req = http.request(command.url, { method: 'POST', headers: command.headers }, res => {
              res.resume(); res.on('end', () => ok(res.statusCode));
            });
            req.on('error', fail);
            req.write(body.subarray(0, 9)); req.end(body.subarray(9));
          });
        } else if (command.op === 'diagnostics') value = collector.diagnostics;
        else if (command.op === 'close') { await collector.close(); value = null; }
        else throw Error('unsupported fixture command');
        send({ id: command.id, value });
        if (command.op === 'close') { lines.close(); break; }
      } catch { send({ id: command.id, error: 'Node collector fixture failed' }); }
    }
  `
  const child = spawn('node', ['--input-type=module', '-e', script, captureRoot, module], { stdio: ['pipe', 'pipe', 'pipe'] })
  type Ready = { captureRoot: string; endpoint: string; headers: string; journalPath: string }
  const pending = new Map<number, { resolve(value: unknown): void; reject(error: Error): void }>()
  let readyResolve!: (value: Ready) => void, readyReject!: (error: Error) => void
  const ready = new Promise<Ready>((resolve, reject) => { readyResolve = resolve; readyReject = reject })
  let buffer = '', sequence = 0, closed: Promise<void> | undefined
  const exited = new Promise<void>(resolve => child.once('exit', () => {
    const error = Error('Node collector fixture exited')
    readyReject(error); for (const task of pending.values()) task.reject(error); pending.clear(); resolve()
  }))
  child.once('error', error => { readyReject(error); for (const task of pending.values()) task.reject(error) })
  child.stdout.on('data', data => {
    buffer += data.toString('utf8')
    let end: number
    while ((end = buffer.indexOf('\n')) >= 0) {
      const row = JSON.parse(buffer.slice(0, end)); buffer = buffer.slice(end + 1)
      if (row.ready) readyResolve(row.ready)
      else { const task = pending.get(row.id); pending.delete(row.id); if (row.error) task?.reject(Error(row.error)); else task?.resolve(row.value) }
    }
  })
  child.stderr.resume() // Fixture failures are fixed messages; never print the private token.
  const deadline = setTimeout(() => { readyReject(Error('Node collector fixture readiness timeout')); child.kill('SIGKILL') }, 5000)
  let info: Ready
  try { info = await ready } finally { clearTimeout(deadline) }
  const call = <T>(op: string, fields = {}): Promise<T> => new Promise((resolve, reject) => {
    const id = ++sequence
    pending.set(id, { resolve: value => resolve(value as T), reject })
    child.stdin.write(JSON.stringify({ id, op, ...fields }) + '\n')
  })
  return { ...info,
    post: (url: string, body: Buffer, headers: Record<string, string>) => call<number>('post', { url, body: body.toString('base64'), headers }),
    diagnostics: () => call<Record<string, unknown>>('diagnostics'),
    close: () => closed ??= (async () => { try { await call('close'); child.stdin.end(); await exited } finally { if (child.exitCode === null) child.kill('SIGKILL') } })(),
  }
}

const post = (url: string, body: Buffer, headers: Record<string, string>) => new Promise<number>((ok, fail) => {
  const req = http.request(url, { method: 'POST', headers }, res => { res.resume(); res.on('end', () => ok(res.statusCode!)) })
  req.on('error', fail); req.write(body.subarray(0, 9)); req.end(body.subarray(9))
})

test('authenticated chunked and gzip OTLP retains exact safe identity, never raw logs or attributes', async () => {
  const root = await realpath(await mkdtemp(resolve(tmpdir(), 'native-trace-'))); roots.push(root)
  const collector = await nodeCollector(root)
  const token = collector.headers.split('=')[1]!
  const headers = { 'Content-Type': 'application/json', 'x-trajviz-trace-token': token }
  try {
    const traces = Buffer.from(JSON.stringify({ resourceSpans: [{ scopeSpans: [{ spans: [
      { traceId: tid, spanId: sid, name: 'SessionProcessor.process', attributes: [{ key: 'prompt', value: { stringValue: 'PRIVATE_PROMPT' } }] },
      { traceId: tid, spanId: 'abcdef1234567890', parentSpanId: sid, name: 'PRIVATE_PROMPT' },
    ] }] }] }))
    expect(await collector.post(collector.endpoint + '/v1/traces', traces, { 'Content-Type': 'application/json' })).toBe(401)
    expect(await collector.post(collector.endpoint + '/v1/traces', gzipSync(traces), { ...headers, 'Content-Encoding': 'gzip' })).toBe(200)
    const logs = Buffer.from(JSON.stringify({ resourceLogs: [{ scopeLogs: [{ logRecords: [
      { traceId: tid, spanId: sid, body: { arrayValue: { values: [{ stringValue: 'process' }, { stringValue: JSON.stringify({ 'session.id': 'ses_probe', messageID: 'msg_owner', apiKey: 'PRIVATE_CREDENTIAL' }) }] } } },
      { traceId: tid, spanId: sid, body: { stringValue: 'PRIVATE_PROMPT' }, attributes: [{ key: 'token', value: { stringValue: 'PRIVATE_CREDENTIAL' } }] },
    ] }] }] }))
    expect(await collector.post(collector.endpoint + '/v1/logs', logs, headers)).toBe(200)
    const encoded = await readFile(collector.journalPath, 'utf8')
    expect(encoded).not.toContain('PRIVATE_')
    expect(encoded.trim().split('\n').map((line: string) => JSON.parse(line))).toEqual([
      { schema_version: 1, type: 'span', trace_id: tid, span_id: sid, parent_span_id: null, name: 'SessionProcessor.process' },
      { schema_version: 1, type: 'processor_identity', trace_id: tid, span_id: sid, session_id: 'ses_probe', assistant_message_id: 'msg_owner' },
    ])
    expect((await stat(collector.journalPath)).mode & 0o777).toBe(0o600)
    expect(() => captureEnvironment({ OTEL_TRACES_SAMPLER: 'always_off' }, collector)).toThrow('conflicts')
    expect(captureEnvironment({}, collector)).toMatchObject({ OTEL_TRACES_SAMPLER: 'always_on', TRAJVIZ_OBS_REQUEST_CAPTURE: '1', TRAJVIZ_OBS_REQUEST_CAPTURE_DIR: root })
    expect(() => captureEnvironment({ TRAJVIZ_OBS_REQUEST_CAPTURE_DIR: '/different-root' }, collector)).toThrow('conflicts')
    expect(() => captureEnvironment({ DEVECO_EXPERIMENTAL_NATIVE_LLM: '1' }, collector)).toThrow('unsupported')
    expect(await collector.diagnostics()).toMatchObject({ spans: 1, identities: 1, rejected: 1, storage_failed: false })
  } finally { await collector.close() }
})


test('collector rejects public roots, symlinks, oversized journals, and bounded gzip expansion', async () => {
  const parent = await realpath(await mkdtemp(resolve(tmpdir(), 'native-trace-security-'))); roots.push(parent)
  const exposed = resolve(parent, 'public'); await mkdir(exposed, { mode: 0o755 })
  await expect(createTraceCollector({ captureRoot: exposed })).rejects.toThrow('private directory')
  const privateRoot = resolve(parent, 'private'); await mkdir(privateRoot, { mode: 0o700 })
  const link = resolve(parent, 'link'); await symlink(privateRoot, link)
  await expect(createTraceCollector({ captureRoot: link })).rejects.toThrow('private directory')
  const journal = await open(resolve(privateRoot, 'trace-spans.jsonl'), 'w', 0o600)
  await journal.truncate(16 * 1024 * 1024 + 1); await journal.close()
  await expect(createTraceCollector({ captureRoot: privateRoot })).rejects.toThrow('unsafe trace journal')
  await rm(resolve(privateRoot, 'trace-spans.jsonl'))
  const collector = await createTraceCollector({ captureRoot: privateRoot })
  try {
    const headers = { 'Content-Type': 'application/json', 'x-trajviz-trace-token': collector.headers.split('=')[1]!, 'Content-Encoding': 'gzip' }
    expect(await post(collector.endpoint + '/v1/logs', gzipSync(Buffer.alloc(8 * 1024 * 1024 + 1, 32)), headers)).toBe(400)
    expect((await stat(collector.journalPath)).size).toBe(0)
  } finally { await collector.close() }
})

test('journal quota is reserved before writes and reports exhaustion', async () => {
  const root = await realpath(await mkdtemp(resolve(tmpdir(), 'native-trace-quota-'))); roots.push(root)
  const journal = await open(resolve(root, 'trace-spans.jsonl'), 'w', 0o600)
  await journal.truncate(16 * 1024 * 1024 - 8); await journal.close()
  const collector = await createTraceCollector({ captureRoot: root })
  try {
    const body = Buffer.from(JSON.stringify({ resourceSpans: [{ scopeSpans: [{ spans: [{ traceId: tid, spanId: sid, name: 'SessionProcessor.process' }] }] }] }))
    expect(await post(collector.endpoint + '/v1/traces', body, { 'Content-Type': 'application/json', 'x-trajviz-trace-token': collector.headers.split('=')[1]! })).toBe(507)
    expect(collector.diagnostics.exhausted).toBe(true)
  } finally { await collector.close() }
})

test('wrapper forwards SIGINT and drains the final authenticated native export', async () => {
  const parent = await realpath(await mkdtemp(resolve(tmpdir(), 'native-trace-signal-'))); roots.push(parent)
  const root = resolve(parent, 'capture'), executable = resolve(parent, 'deveco')
  await writeFile(executable, `#!/usr/bin/env node
const timer=setInterval(()=>{},1000);
process.stdout.write('READY\\n');
process.on('SIGINT',async()=>{
 clearInterval(timer);
 const token=process.env.OTEL_EXPORTER_OTLP_HEADERS.split('=')[1];
 const payload={resourceSpans:[{scopeSpans:[{spans:[{traceId:'${tid}',spanId:'${sid}',name:'SessionProcessor.process'}]}]}]};
 await fetch(process.env.OTEL_EXPORTER_OTLP_ENDPOINT+'/v1/traces',{method:'POST',headers:{'content-type':'application/json','x-trajviz-trace-token':token},body:JSON.stringify(payload)});
 process.exit(0);
});
`, { mode: 0o700 })
  const env = { ...process.env }
  for (const name of Object.keys(env)) if (name.startsWith('OTEL_') || name.startsWith('TRAJVIZ_NATIVE_') || name === 'TRAJVIZ_OBS_REQUEST_CAPTURE_DIR') delete env[name]
  const launcher = fileURLToPath(new URL('../scripts/capture-run.mjs', import.meta.url))
  const child = spawn(process.execPath, [launcher, '--capture-root', root, '--', executable], { env, stdio: ['ignore', 'pipe', 'pipe'] })
  let output = ''
  try {
    await new Promise<void>((ok, fail) => {
      const timeout = setTimeout(() => fail(Error('wrapper readiness timeout')), 5000)
      child.stdout.on('data', data => { output += data; if (output.includes('READY')) { clearTimeout(timeout); ok() } })
      child.once('error', fail); child.once('exit', () => { if (!output.includes('READY')) { clearTimeout(timeout); fail(Error('wrapper exited early')) } })
    })
    const exited = new Promise<number | null>(ok => child.once('exit', code => ok(code)))
    child.kill('SIGINT')
    expect(await exited).toBe(0)
    const rows = (await readFile(resolve(root, 'trace-spans.jsonl'), 'utf8')).trim().split('\n').map(line => JSON.parse(line))
    expect(rows).toEqual([{ schema_version: 1, type: 'span', trace_id: tid, span_id: sid, parent_span_id: null, name: 'SessionProcessor.process' }])
  } finally { if (child.exitCode === null) child.kill('SIGKILL') }
}, 10_000)

test('published bin symlink executes main while eval imports stay inert', async () => {
  const parent = await realpath(await mkdtemp(resolve(tmpdir(), 'native-trace-bin-entry-'))); roots.push(parent)
  const launcher = fileURLToPath(new URL('../scripts/capture-run.mjs', import.meta.url))
  const bin = resolve(parent, 'trajviz-deveco-capture')
  await symlink(launcher, bin)
  const run = (args: string[]) => new Promise<{ code: number | null; stdout: string; stderr: string }>((ok, fail) => {
    const child = spawn('node', args, { stdio: ['ignore', 'pipe', 'pipe'] })
    let stdout = '', stderr = ''
    const timeout = setTimeout(() => { child.kill('SIGKILL'); fail(Error('Node entry fixture timed out')) }, 3000)
    child.stdout.on('data', value => { stdout += value.toString('utf8') })
    child.stderr.on('data', value => { stderr += value.toString('utf8') })
    child.once('error', error => { clearTimeout(timeout); fail(error) })
    child.once('exit', code => { clearTimeout(timeout); ok({ code, stdout, stderr }) })
  })
  for (const entry of [launcher, bin]) {
    const result = await run([entry])
    expect(result.code).toBe(1)
    expect(result.stderr).toContain('Usage: capture-run.mjs')
    expect(result.stdout).toBe('')
  }
  // An eval script may pass the launcher as argv[1] without invoking its CLI.
  const imported = await run(['--input-type=module', '-e', 'await import(process.argv[1])', launcher])
  expect(imported).toEqual({ code: 0, stdout: '', stderr: '' })
})
