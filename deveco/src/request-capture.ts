import { createHash, randomUUID } from 'node:crypto'
import { constants } from 'node:fs'
import { lstat, mkdir, open, readdir, realpath, rename } from 'node:fs/promises'
import http, { type IncomingMessage, type ServerResponse } from 'node:http'
import https from 'node:https'
import path from 'node:path'
import type { Socket } from 'node:net'

type RecordValue = Record<string, unknown>
export type RequestContext = {
  sessionID?: string; userMessageID?: string; assistantMessageID?: string
  purpose?: string; requestPurpose?: string; invocationID?: string; previousRequestID?: string
  basis?: 'host_processor_context' | 'native_processor_span'
  trace_id?: string; span_id?: string
}
export type RequestCaptureOptions = {
  directory?: string
  env?: Record<string, string | undefined>
  resolveContext?: (input: RecordValue) => RequestContext | undefined | Promise<RequestContext | undefined>
  maxBodyBytes?: number; maxStorageBytes?: number; timeoutMs?: number
}
type Route = { provider: string; original: string; base: URL; prefix: string; local: string }
type Invocation = {
  id: string; session: string; user: string; assistant: string | null; purpose: string
  agent: string | null; provider: string; model: string | null; basis: string
  trace: string | null; span: string | null; previous: string | null; at: number; active: number
}
const object = (value: unknown): RecordValue => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as RecordValue : {}
const identity = (value: unknown): value is string => typeof value === 'string' && /^[A-Za-z0-9_.:/-]{1,256}$/.test(value)
const uuid = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(value)
const digest = (value: string | Buffer) => createHash('sha256').update(value).digest('hex')
const hop = new Set(['host', 'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailer', 'transfer-encoding', 'upgrade', 'content-length'])
const reasonError = (reason: string) => new Error(`TrajViz request capture: ${reason}`)

/** Explicit opt-in, fixed-upstream HTTP body recorder. Never reads provider credentials. */
export class RequestCapture {
  private readonly env: Record<string, string | undefined>
  private readonly requested: boolean
  private readonly configuredRoot: string | undefined
  private canonicalRoot?: string
  private readonly routeSecret = randomUUID().replaceAll('-', '')
  private readonly routes = new Map<string, Route>()
  private readonly invocations = new Map<string, Invocation>()
  private readonly jobs = new Set<Promise<void>>()
  private readonly controllers = new Set<AbortController>()
  private server?: http.Server
  private readonly sockets = new Set<Socket>()
  private starting?: Promise<void>
  private closing?: Promise<void>
  private stopped = false
  private storage = 0
  private initialized = false
  readonly maxBodyBytes: number
  readonly maxStorageBytes: number
  readonly timeoutMs: number
  constructor(private readonly options: RequestCaptureOptions = {}) {
    this.env = options.env ?? process.env
    this.requested = this.env.TRAJVIZ_OBS_REQUEST_CAPTURE === '1'
    this.configuredRoot = this.env.TRAJVIZ_OBS_REQUEST_CAPTURE_DIR
    this.maxBodyBytes = options.maxBodyBytes ?? 16 * 1024 * 1024
    this.maxStorageBytes = options.maxStorageBytes ?? 512 * 1024 * 1024
    this.timeoutMs = options.timeoutMs ?? 120_000
    for (const value of [this.maxBodyBytes, this.maxStorageBytes, this.timeoutMs]) {
      if (!Number.isSafeInteger(value) || value <= 0) throw reasonError('invalid configured limit')
    }
  }
  get enabled(): boolean { return this.requested && !this.stopped }
  get root(): string | undefined { return this.canonicalRoot ?? this.configuredRoot }

  private async privateDirectory(directory: string): Promise<void> {
    await mkdir(directory, { recursive: true, mode: 0o700 })
    const info = await lstat(directory)
    if (!info.isDirectory() || info.isSymbolicLink() || (info.mode & 0o077) !== 0
      || (process.getuid && info.uid !== process.getuid())) throw reasonError('capture directory must be owner-only and cannot be a symlink')
  }
  private async initialize(): Promise<void> {
    if (this.initialized) return
    if (process.platform === 'win32') throw reasonError('private request capture requires POSIX filesystem controls')
    const root = this.configuredRoot
    if (!root || !path.isAbsolute(root) || root.includes('\0')) throw reasonError('an absolute TRAJVIZ_OBS_REQUEST_CAPTURE_DIR is required')
    await this.privateDirectory(root)
    this.canonicalRoot = await realpath(root)
    let files = 0
    for (const directory of await readdir(this.canonicalRoot)) {
      if (['trace-spans.jsonl', 'native-trace-status.json'].includes(directory)) {
        const info = await lstat(path.join(this.canonicalRoot, directory))
        if (!info.isFile() || info.isSymbolicLink() || (info.mode & 0o077) !== 0 || (process.getuid && info.uid !== process.getuid())) throw reasonError('trace journal must be a private regular file')
        this.storage += info.size
        continue
      }
      if (!/^[a-f0-9]{64}$/.test(directory)) throw reasonError('capture root contains unrelated entries')
      const parent = path.join(this.canonicalRoot, directory)
      await this.privateDirectory(parent)
      for (const name of await readdir(parent)) {
        if (++files > 20_000 || !/^[a-f0-9-]{36}\.(?:body|json|tmp)$/.test(name)) throw reasonError('capture root exceeds its bounded file layout')
        const info = await lstat(path.join(parent, name))
        if (!info.isFile() || info.isSymbolicLink() || (info.mode & 0o077) !== 0 || (process.getuid && info.uid !== process.getuid())) throw reasonError('capture files must be private regular files')
        this.storage += info.size
      }
    }
    if (this.storage >= this.maxStorageBytes) throw reasonError('capture storage limit reached')
    this.initialized = true
  }
  private async start(): Promise<void> {
    if (this.server) return
    if (this.starting) return this.starting
    this.starting = (async () => {
      await this.initialize()
      if (this.stopped) throw reasonError('recorder is closed')
      const server = http.createServer((request, response) => {
        const task = this.handle(request, response).catch(() => this.error(response, 502, 'capture_failed'))
        this.jobs.add(task)
        void task.finally(() => this.jobs.delete(task))
      })
      server.on('connection', socket => { this.sockets.add(socket); socket.once('close', () => this.sockets.delete(socket)) })
      server.requestTimeout = this.timeoutMs
      server.headersTimeout = Math.min(this.timeoutMs, 60_000)
      server.on('upgrade', (_request, socket) => { socket.end('HTTP/1.1 501 Not Implemented\r\nConnection: close\r\n\r\n') })
      await new Promise<void>((resolve, reject) => {
        const fail = (error: Error) => { server.close(); reject(error) }
        server.once('error', fail)
        server.listen(0, '127.0.0.1', () => { server.removeListener('error', fail); resolve() })
      })
      server.on('error', () => { for (const controller of this.controllers) controller.abort() })
      this.server = server
    })().finally(() => { this.starting = undefined })
    return this.starting
  }

  /** Rewrites only explicitly selected providers with explicit HTTP(S) baseURLs. */
  async configure(config: RecordValue): Promise<void> {
    if (!this.requested) return
    if (this.stopped) throw reasonError('recorder is closed')
    if (this.env.DEVECO_EXPERIMENTAL_NATIVE_LLM && !['0', 'false'].includes(this.env.DEVECO_EXPERIMENTAL_NATIVE_LLM)) throw reasonError('experimental native LLM transport is not covered')
    const selected = (this.env.TRAJVIZ_OBS_REQUEST_CAPTURE_PROVIDERS ?? '').split(',').map(value => value.trim()).filter(Boolean)
    if (!selected.length || selected.length > 16 || selected.some(value => !identity(value))) throw reasonError('set TRAJVIZ_OBS_REQUEST_CAPTURE_PROVIDERS to explicit provider IDs')
    const providers = object(config.provider)
    const prepared: { provider: string; options: RecordValue; original: string; base: URL }[] = []
    for (const provider of new Set(selected)) {
      const entry = object(providers[provider]), options = object(entry.options)
      const original = options.baseURL
      const previous = this.routes.get(provider)
      if (previous && (original === previous.local || original === previous.original)) { options.baseURL = previous.local; continue }
      if (previous) throw reasonError('restart DevEco to change a captured provider endpoint')
      if (typeof original !== 'string') throw reasonError('selected providers need an explicit options.baseURL')
      let base: URL
      try { base = new URL(original) } catch { throw reasonError('invalid provider baseURL') }
      if (!['http:', 'https:'].includes(base.protocol) || base.username || base.password || base.hash) throw reasonError('provider baseURL must be HTTP(S) without userinfo or fragments')
      prepared.push({ provider, options, original, base })
    }
    await this.start()
    const address = this.server!.address()
    if (!address || typeof address === 'string') throw reasonError('capture listener did not become ready')
    for (const item of prepared) {
      const prefix = `/${this.routeSecret}/${randomUUID()}`
      const local = `http://127.0.0.1:${address.port}${prefix}${item.base.pathname}${item.base.search}`
      this.routes.set(item.provider, { provider: item.provider, original: item.original, base: item.base, prefix, local })
      item.options.baseURL = local
    }
  }

  /** Stamp one preparation identity; each physical send receives another ID. */
  async headers(input: RecordValue, output: { headers: Record<string, string> }): Promise<void> {
    if (!this.requested) return
    if (this.stopped) throw reasonError('recorder is closed')
    const session = input.sessionID, user = object(input.message).id
    const model = object(input.model), provider = model.providerID ?? object(input.provider).id
    if (!identity(session) || !identity(user) || !identity(provider)) throw reasonError('native session, user and provider identity are required')
    if (!this.routes.has(provider)) throw reasonError('this provider has no configured HTTP capture route')
    const supplied = this.options.resolveContext ? await this.options.resolveContext(input) : object(input.requestContext) as RequestContext
    const context = supplied ?? {}
    if ((context.sessionID !== undefined && context.sessionID !== session) || (context.userMessageID !== undefined && context.userMessageID !== user)) throw reasonError('request context disagrees with native hook identity')
    const basis = ['host_processor_context', 'native_processor_span'].includes(context.basis ?? '') ? context.basis! : 'absent'
    const trace = typeof context.trace_id === 'string' && /^[a-f0-9]{32}$/.test(context.trace_id) ? context.trace_id : null
    const span = typeof context.span_id === 'string' && /^[a-f0-9]{16}$/.test(context.span_id) ? context.span_id : null
    const assistant = basis !== 'absent' && identity(context.assistantMessageID) ? context.assistantMessageID : null
    const now = Date.now()
    for (const [key, entry] of this.invocations) if (entry.active === 0 && now - entry.at > 30 * 60_000) this.invocations.delete(key)
    if (this.invocations.size >= 4096) throw reasonError('too many prepared request identities')
    if (context.invocationID !== undefined && !uuid(context.invocationID)) throw reasonError('request invocation identity must be a canonical UUID')
    const id = context.invocationID ?? randomUUID()
    if (this.invocations.has(id)) throw reasonError('request invocation identity was reused by preparation')
    const agent = identity(input.agent) ? input.agent : null
    const purpose = context.purpose ?? context.requestPurpose ?? (agent === 'title' ? 'title' : agent === 'compaction' ? 'compaction' : 'assistant')
    if (!['assistant', 'title', 'compaction', 'auxiliary'].includes(purpose)) throw reasonError('invalid request purpose')
    this.invocations.set(id, { id, session, user, assistant, purpose, agent, provider,
      model: identity(model.id) ? model.id : null, basis: assistant || (trace && span) ? basis : 'absent',
      trace, span, previous: identity(context.previousRequestID) ? context.previousRequestID : null, at: now, active: 0 })
    output.headers['x-trajviz-capture-id'] = id
    output.headers['x-trajviz-session-id'] = session
    output.headers['x-trajviz-user-message-id'] = user
    if (agent) output.headers['x-trajviz-agent'] = agent
  }

  private error(response: ServerResponse, status: number, reason: string): void {
    if (response.destroyed || response.writableEnded) return
    if (response.headersSent) { response.destroy(); return }
    response.writeHead(status, { 'content-type': 'application/json', 'connection': 'close' })
    response.end(JSON.stringify({ error: { type: 'trajviz_capture_error', message: `Request capture failed: ${reason}` } }))
  }
  private async writeMetadata(directory: string, id: string, metadata: RecordValue): Promise<void> {
    const temporary = path.join(directory, `${id}.tmp`)
    const file = await open(temporary, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600)
    try { await file.writeFile(JSON.stringify(metadata)); await file.sync() } finally { await file.close() }
    await rename(temporary, path.join(directory, `${id}.json`))
  }
  private async handle(request: IncomingMessage, response: ServerResponse): Promise<void> {
    const route = [...this.routes.values()].find(value => request.url?.startsWith(value.prefix + '/'))
    const nonce = request.headers['x-trajviz-capture-id']
    const context = typeof nonce === 'string' ? this.invocations.get(nonce) : undefined
    if (this.stopped || !route || !context || context.provider !== route.provider || this.controllers.size >= 16) {
      request.resume(); this.error(response, 403, 'unrecognized_or_inactive_request'); return
    }
    if (request.headers['x-trajviz-session-id'] !== context.session || request.headers['x-trajviz-user-message-id'] !== context.user) {
      request.resume(); this.error(response, 403, 'correlation_headers_changed'); return
    }
    context.active++
    const controller = new AbortController(); this.controllers.add(controller)
    const id = randomUUID(), directory = path.join(this.canonicalRoot!, digest(context.session))
    const metadata: RecordValue = { schema_version: 1, type: 'deveco_request', adapter: 'deveco', request_id: id,
      invocation_id: context.id, session_id: context.session, user_message_id: context.user,
      assistant_message_id: context.assistant, purpose: context.purpose, agent_id: context.agent,
      provider_id: context.provider, model_id: context.model, trace_id: context.trace, span_id: context.span,
      previous_request_id: context.previous, observed_at: new Date().toISOString(),
      body_ref: null, body_sha256: null, body_bytes: 0, status: null, response_bytes: 0,
      capture_boundary: 'serialized_http_request_body', transport: 'http', dispatch_status: 'received',
      mapping: { status: context.assistant ? 'provided' : context.trace && context.span ? 'pending' : 'unlinked', basis: context.basis },
      coverage: { complete: false, omitted_fields: ['body'], redacted_fields: [] } }
    let persisted = false
    const reject = async (status: number, reason: string) => {
      metadata.status = status; metadata.dispatch_status = 'rejected'; metadata.incomplete_reason = reason
      await this.privateDirectory(directory)
      if (this.storage + 16_384 <= this.maxStorageBytes) { this.storage += 16_384; await this.writeMetadata(directory, id, metadata) }
      request.resume(); this.error(response, status, reason)
    }
    try {
      const declared = request.headers['content-length']
      if (request.method !== 'POST' || request.headers.upgrade || request.headers['transfer-encoding'] || request.headers.expect) { await reject(501, 'unsupported_request_transport'); return }
      if (typeof declared !== 'string' || !/^\d+$/.test(declared)) { await reject(411, 'content_length_required'); return }
      const length = Number(declared); metadata.declared_body_bytes = length
      if (!Number.isSafeInteger(length) || length > this.maxBodyBytes) { await reject(413, 'body_limit_exceeded'); return }
      if (this.storage + length + 16_384 > this.maxStorageBytes) { await reject(507, 'storage_limit_reached'); return }
      this.storage += length + 16_384 // Reserve before awaiting body bytes, including concurrent sends.
      const chunks: Buffer[] = []; let received = 0
      for await (const value of request) {
        const chunk = Buffer.isBuffer(value) ? value : Buffer.from(value)
        received += chunk.length
        if (received > length || received > this.maxBodyBytes) { await reject(413, 'body_limit_exceeded'); return }
        if (controller.signal.aborted) throw reasonError('capture_stopped')
        chunks.push(chunk)
      }
      if (received !== length) { await reject(400, 'incomplete_request_body'); return }
      const body = Buffer.concat(chunks)
      await this.privateDirectory(directory)
      const raw = await open(path.join(directory, `${id}.body`), constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600)
      try { await raw.writeFile(body); await raw.sync() } finally { await raw.close() }
      metadata.body_ref = `${id}.body`; metadata.body_sha256 = digest(body); metadata.body_bytes = body.length
      metadata.coverage = { complete: true, omitted_fields: [], redacted_fields: [] }
      metadata.dispatch_status = 'prepared'
      const contentType = request.headers['content-type']; metadata.body_content_type = typeof contentType === 'string' ? contentType.split(';', 1)[0]!.toLowerCase() : null
      await this.writeMetadata(directory, id, metadata); persisted = true
      const suffix = request.url!.slice(route.prefix.length)
      const upstream = new URL(suffix, route.base.origin)
      const basePath = route.base.pathname.replace(/\/$/, '')
      if (upstream.origin !== route.base.origin || (basePath && upstream.pathname !== basePath && !upstream.pathname.startsWith(basePath + '/'))) { this.error(response, 403, 'upstream_route_changed'); metadata.status = 403; metadata.dispatch_status = 'rejected'; metadata.error = 'upstream_route_changed'; return }
      const connection = String(request.headers.connection ?? '').split(',').map(value => value.trim().toLowerCase())
      const headers = Object.fromEntries(Object.entries(request.headers).filter(([name]) => !hop.has(name) && !connection.includes(name) && !name.startsWith('x-trajviz-')))
      headers['content-length'] = String(body.length)
      metadata.dispatch_status = 'sent'
      await this.writeMetadata(directory, id, metadata)
      if (controller.signal.aborted || request.aborted || response.destroyed) {
        metadata.dispatch_status = 'rejected'
        metadata.error = controller.signal.aborted ? 'capture_stopped' : 'client_disconnected'
        return
      }
      await new Promise<void>(resolve => {
        let settled = false
        const finish = (error?: string) => { if (settled) return; settled = true; if (error) metadata.error = error; response.removeListener('close', disconnected); resolve() }
        const client = (upstream.protocol === 'https:' ? https : http).request(upstream, { method: 'POST', headers, signal: controller.signal }, incoming => {
          metadata.status = incoming.statusCode ?? null
          if (incoming.statusCode && incoming.statusCode >= 300 && incoming.statusCode < 400) { incoming.resume(); this.error(response, 502, 'upstream_redirect_unsupported'); finish('upstream_redirect_unsupported'); return }
          const exclusions = new Set([...hop, ...String(incoming.headers.connection ?? '').split(',').map(value => value.trim().toLowerCase())])
          const responseHeaders = Object.fromEntries(Object.entries(incoming.headers).filter(([name]) => !exclusions.has(name)))
          response.writeHead(incoming.statusCode ?? 502, responseHeaders)
          incoming.on('data', chunk => { metadata.response_bytes = Number(metadata.response_bytes) + Buffer.byteLength(chunk) })
          incoming.once('error', () => { this.error(response, 502, 'upstream_stream_error'); finish('upstream_stream_error') })
          incoming.once('end', () => { metadata.dispatch_status = 'complete'; finish() })
          incoming.pipe(response)
        })
        const disconnected = () => { if (!response.writableEnded) { client.destroy(); finish('client_disconnected') } }
        response.once('close', disconnected)
        client.setTimeout(this.timeoutMs, () => { client.destroy(); this.error(response, 504, 'upstream_timeout'); finish('upstream_timeout') })
        client.once('error', () => { this.error(response, 502, controller.signal.aborted ? 'capture_stopped' : 'upstream_error'); finish(controller.signal.aborted ? 'capture_stopped' : 'upstream_error') })
        client.end(body)
      })
    } catch {
      metadata.error = controller.signal.aborted ? 'capture_stopped' : 'capture_or_upstream_failed'
      this.error(response, 502, String(metadata.error))
    } finally {
      if (persisted) await this.writeMetadata(directory, id, metadata).catch(() => {})
      this.controllers.delete(controller); context.active--
    }
  }

  close(): Promise<void> {
    if (this.closing) return this.closing
    this.stopped = true
    return this.closing = (async () => {
      await this.starting?.catch(() => {})
      for (const controller of this.controllers) controller.abort()
      const server = this.server; this.server = undefined
      if (server) {
        await new Promise<void>(resolve => {
          let settled = false, poll: ReturnType<typeof setInterval> | undefined
          const done = () => { if (settled) return; settled = true; if (poll) clearInterval(poll); resolve() }
          server.close(done)
          server.closeAllConnections()
          for (const socket of this.sockets) socket.destroy()
          // Bun may omit Node's close callback after an early rejected request.
          // A stopped listener with no remaining tracked sockets is equally closed.
          if (!server.listening && this.sockets.size === 0) done()
          if (!settled) poll = setInterval(() => { if (!server.listening && this.sockets.size === 0) done() }, 10)
        })
      }
      await Promise.allSettled([...this.jobs])
      this.invocations.clear(); this.routes.clear()
    })()
  }
}
