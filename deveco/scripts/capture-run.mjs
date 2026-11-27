#!/usr/bin/env node
/** Opt-in wrapper for DevEco 0.2.6. No installed binary changes. The loopback
 * collector retains only span ancestry and exact native processor identity. */
import http from 'node:http'
import { randomBytes, timingSafeEqual } from 'node:crypto'
import { constants } from 'node:fs'
import { lstat, mkdir, open, realpath } from 'node:fs/promises'
import { basename, isAbsolute, resolve } from 'node:path'
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { gunzipSync } from 'node:zlib'

const MAX_BODY = 8 * 1024 * 1024
const MAX_ROWS = 65_536
const MAX_JOURNAL = 16 * 1024 * 1024
const SAFE_NAMES = new Set(['Plugin.trigger', 'LLMRequestPrep.prepare', 'LLM.run', 'SessionProcessor.process', 'SessionPrompt.ensureTitle', 'SessionPrompt.run', 'SessionPrompt.loop', 'SessionRunState.ensureRunning', 'SessionPrompt.promptImpl', 'SessionPrompt.prompt', 'SessionHttpApi.prompt'])
const hex = (s, n) => typeof s === 'string' && new RegExp(`^[0-9a-f]{${n}}$`).test(s) && !/^0+$/.test(s)
const nativeID = (s, prefix) => typeof s === 'string' && s.startsWith(prefix) && /^[A-Za-z0-9_-]{1,256}$/.test(s)

function safeRows(payload, kind) {
  const rows = []
  const add = row => { if (rows.length >= 65_536) throw Error('batch_limit'); rows.push(row) }
  if (kind === 'traces') {
    for (const r of payload.resourceSpans ?? []) for (const scope of r.scopeSpans ?? []) for (const s of scope.spans ?? []) {
      if (!hex(s.traceId, 32) || !hex(s.spanId, 16) || (s.parentSpanId && !hex(s.parentSpanId, 16))) continue
      if (typeof s.name !== 'string' || !SAFE_NAMES.has(s.name)) continue
      add({ schema_version: 1, type: 'span', trace_id: s.traceId, span_id: s.spanId,
        parent_span_id: s.parentSpanId || null, name: s.name })
    }
  } else {
    for (const r of payload.resourceLogs ?? []) for (const scope of r.scopeLogs ?? []) for (const s of scope.logRecords ?? []) {
      // DevEco's Effect logger serializes logInfo('process', IDs) as this exact
      // two-element array. Never retain general log bodies or attributes.
      const values = s.body?.arrayValue?.values
      if (!Array.isArray(values) || values.length !== 2 || values[0]?.stringValue !== 'process') continue
      const text = values[1]?.stringValue
      if (typeof text !== 'string' || text.length > 1024 || !hex(s.traceId, 32) || !hex(s.spanId, 16)) continue
      let ids
      try { ids = JSON.parse(text) } catch { continue }
      if (!ids || !nativeID(ids['session.id'], 'ses_') || !nativeID(ids.messageID, 'msg_')) continue
      add({ schema_version: 1, type: 'processor_identity', trace_id: s.traceId, span_id: s.spanId,
        session_id: ids['session.id'], assistant_message_id: ids.messageID })
    }
  }
  return rows
}

export async function createTraceCollector({ captureRoot }) {
  if (!isAbsolute(captureRoot)) throw Error('captureRoot must be absolute')
  await mkdir(captureRoot, { recursive: true, mode: 0o700 })
  const stat = await lstat(captureRoot)
  if (!stat.isDirectory() || stat.isSymbolicLink() || (stat.mode & 0o077) || (process.getuid && stat.uid !== process.getuid())) throw Error('captureRoot must be a private directory (0700)')
  if (await realpath(captureRoot) !== resolve(captureRoot)) throw Error('captureRoot must not traverse symlinks')
  const journalPath = resolve(captureRoot, 'trace-spans.jsonl')
  const file = await open(journalPath, constants.O_RDWR | constants.O_CREAT | constants.O_APPEND | constants.O_NOFOLLOW, 0o600)
  const fs = await file.stat()
  if (!fs.isFile() || (fs.mode & 0o077) || fs.nlink !== 1 || (process.getuid && fs.uid !== process.getuid()) || fs.size > MAX_JOURNAL) { await file.close(); throw Error('unsafe trace journal') }
  const existing = await file.readFile()
  let existingRows = 0
  for (const byte of existing) if (byte === 10) existingRows++
  if (existingRows >= MAX_ROWS) { await file.close(); throw Error('trace journal row limit reached') }
  const nonce = randomBytes(32).toString('hex')
  let bytes = fs.size, rows = existingRows, closed = false, chain = Promise.resolve()
  const diagnostics = { accepted: 0, rejected: 0, spans: 0, identities: 0, storage_failed: false, exhausted: false }
  const server = http.createServer(async (req, res) => {
    const deny = (status) => { diagnostics.rejected++; res.writeHead(status, { 'Content-Length': '0' }); res.end(); req.resume() }
    const provided = req.headers['x-trajviz-trace-token']
    if (typeof provided !== 'string' || !/^[0-9a-f]{64}$/.test(provided) || !timingSafeEqual(Buffer.from(provided), Buffer.from(nonce))) return deny(401)
    if (req.method !== 'POST' || !['/v1/traces', '/v1/logs'].includes(req.url)) return deny(404)
    if (!/^application\/json(?:\s*;|$)/i.test(req.headers['content-type'] ?? '')) return deny(415)
    if (closed || diagnostics.storage_failed || diagnostics.exhausted) return deny(503)
    try {
      let length = 0
      const chunks = []
      for await (const chunk of req) { length += chunk.length; if (length > MAX_BODY) throw Error('body_limit'); chunks.push(chunk) }
      let body = Buffer.concat(chunks)
      const encoding = req.headers['content-encoding']
      if (encoding === 'gzip') body = gunzipSync(body, { maxOutputLength: MAX_BODY })
      else if (encoding && encoding !== 'identity') throw Error('encoding')
      if (body.length > MAX_BODY) throw Error('body_limit')
      const selected = safeRows(JSON.parse(body.toString('utf8')), req.url === '/v1/traces' ? 'traces' : 'logs')
      const encoded = selected.map(row => JSON.stringify(row) + '\n').join('')
      const size = Buffer.byteLength(encoded)
      // Reserve before queueing so concurrent POSTs cannot overrun quota.
      if (bytes + size > MAX_JOURNAL || rows + selected.length > MAX_ROWS) { diagnostics.exhausted = true; return deny(507) }
      bytes += size; rows += selected.length
      const write = chain.then(async () => { if (encoded) await file.writeFile(encoded) })
      chain = write.catch(() => { diagnostics.storage_failed = true })
      await write
      diagnostics.accepted++
      diagnostics.spans += selected.filter(r => r.type === 'span').length
      diagnostics.identities += selected.filter(r => r.type === 'processor_identity').length
      res.writeHead(200, { 'Content-Type': 'application/json', 'Content-Length': '2' }); res.end('{}')
    } catch { deny(diagnostics.storage_failed ? 503 : 400) }
  })
  server.requestTimeout = 10_000
  server.headersTimeout = 10_000
  await new Promise((ok, fail) => { server.once('error', fail); server.listen(0, '127.0.0.1', ok) })
  const endpoint = `http://127.0.0.1:${server.address().port}`
  return { captureRoot, endpoint, headers: `x-trajviz-trace-token=${nonce}`, journalPath, diagnostics,
    async close() {
      if (closed) return
      closed = true
      server.closeIdleConnections()
      await new Promise(ok => server.close(ok))
      await chain; await file.sync(); await file.close()
    } }
}

export function captureEnvironment(env, collector) {
  // Existing SDK/exporter settings can disable sampling, redirect data, or send
  // credentials elsewhere. An explicit wrapper must not silently replace them.
  if (env.DEVECO_EXPERIMENTAL_NATIVE_LLM && !['0', 'false'].includes(env.DEVECO_EXPERIMENTAL_NATIVE_LLM))
    throw Error('Native LLM transport is unsupported by complete HTTP request capture; use an HTTP(S) provider endpoint')
  const conflicts = Object.keys(env).filter(k => k.startsWith('OTEL_') && env[k])
  if (conflicts.length) throw Error(`Existing OpenTelemetry configuration conflicts: ${conflicts.join(', ')}`)
  if (env.TRAJVIZ_NATIVE_TRACE_CAPTURE) throw Error('A native capture wrapper is already active')
  if (env.TRAJVIZ_OBS_REQUEST_CAPTURE && env.TRAJVIZ_OBS_REQUEST_CAPTURE !== '1')
    throw Error('Existing request capture opt-out conflicts with this explicit capture wrapper')
  if (env.TRAJVIZ_OBS_REQUEST_CAPTURE_DIR && resolve(env.TRAJVIZ_OBS_REQUEST_CAPTURE_DIR) !== collector.captureRoot)
    throw Error('Existing request capture directory conflicts with --capture-root')
  // Calling this wrapper is the explicit opt-in. Provider selection remains
  // mandatory and explicit in TRAJVIZ_OBS_REQUEST_CAPTURE_PROVIDERS.
  return { ...env, TRAJVIZ_OBS_REQUEST_CAPTURE: '1', TRAJVIZ_OBS_REQUEST_CAPTURE_DIR: collector.captureRoot, OTEL_EXPORTER_OTLP_ENDPOINT: collector.endpoint, OTEL_EXPORTER_OTLP_HEADERS: collector.headers,
    OTEL_TRACES_SAMPLER: 'always_on', OTEL_BSP_SCHEDULE_DELAY: '100',
    OTEL_BSP_MAX_QUEUE_SIZE: '65536', OTEL_BSP_MAX_EXPORT_BATCH_SIZE: '512', TRAJVIZ_NATIVE_TRACE_CAPTURE: '1' }
}

export async function main(args = process.argv.slice(2)) {
  const cut = args.indexOf('--')
  if (cut !== 2 || args[0] !== '--capture-root' || !args[1] || !args[3])
    throw Error('Usage: capture-run.mjs --capture-root /absolute/private/directory -- deveco [arguments]')
  const [command, ...childArgs] = args.slice(3)
  if (!/^deveco(?:\.exe)?$/.test(basename(command))) throw Error('The capture wrapper supports the DevEco executable only')
  for (let i = 0; i < childArgs.length; i++) {
    if (childArgs[i].startsWith('--log-level=') || childArgs[i] === '--log-level')
      throw Error('Remove --log-level: capture requires INFO to export native processor identities')
  }
  const collector = await createTraceCollector({ captureRoot: args[1] })
  try {
    const env = captureEnvironment(process.env, collector)
    const child = spawn(command, ['--log-level', 'INFO', ...childArgs], { env, stdio: 'inherit' })
    let forwarded = false
    const forward = signal => { if (!forwarded) { forwarded = true; child.kill(signal) } }
    const onInt = () => forward('SIGINT'), onTerm = () => forward('SIGTERM')
    process.on('SIGINT', onInt); process.on('SIGTERM', onTerm)
    try {
      const result = await new Promise((ok, fail) => {
        child.once('error', fail); child.once('exit', (code, signal) => ok({ code, signal }))
      })
      // Keep accepting the final HTTP export batch after the child's graceful
      // SDK shutdown. SIGKILL cannot flush SDK buffers and remains incomplete.
      await new Promise(ok => setTimeout(ok, 300))
      if (result.signal) process.exitCode = 128 + ({ SIGINT: 2, SIGTERM: 15, SIGKILL: 9 }[result.signal] ?? 1)
      else process.exitCode = result.code ?? 1
    } finally { process.off('SIGINT', onInt); process.off('SIGTERM', onTerm) }
  } finally {
    await collector.close()
    console.error('[TrajViz native trace capture]', JSON.stringify(collector.diagnostics))
  }
}
// npm bin entries are symlinks; Node resolves import.meta.url to the target.
// Eval imports can also receive argv[1], but must not execute the CLI.
const evaluated = process.execArgv.some(arg => /^(?:--(?:eval|print)(?:=|$)|-[ep]+$)/.test(arg))
const entry = !evaluated && process.argv[1] ? await realpath(process.argv[1]).catch(() => undefined) : undefined
if (entry && entry === await realpath(fileURLToPath(import.meta.url)))
  main().catch(error => { console.error(error.message); process.exitCode = 1 })
