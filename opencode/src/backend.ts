import type { Plugin } from '@opencode/plugin'
import { OpenCode, type OpenCodeClient } from '@opencode/client'
import { Service } from '@opencode/client/service'
import { createHash } from 'node:crypto'
import { AsyncLocalStorage } from 'node:async_hooks'
import { readFile } from 'node:fs/promises'
import path from 'node:path'
import { captureSession, record, validateSource, type NativeSource } from './collect.js'
import { Runtime, exists, stopChild, type Options } from './runtime.js'
import { Revisions } from './revisions.js'
import type { EmptySnapshot, Snapshot } from './types.js'
import type { ChildProcessWithoutNullStreams } from 'node:child_process'

export type SourceRef = { sessionID: string; transcript: string; artifacts: string; argv: string[]; native?: NativeSource }
type Server = { child: ChildProcessWithoutNullStreams; url: string; signature: string; preloaded?: boolean }
const hash = (text: string) => createHash('sha256').update(text).digest('hex')

export class Backend {
  readonly runtime: Runtime
  readonly revisions = new Revisions()
  readonly sources = new Map<string, NativeSource>()
  private readonly captures = new Map<string, Promise<NativeSource>>()
  private readonly capturedAt = new Map<string, number>()
  private readonly dirty = new Set<string>()
  private readonly parents = new Map<string, Set<string>>()
  private readonly families = new Map<string, Set<string>>()
  private readonly sourceBytes = new Map<string, number>()
  private readonly pinned = new AsyncLocalStorage<ReadonlyMap<string, NativeSource>>()
  private readonly targets = new Map<string, string>()
  private readonly servers = new Map<string, Server>()
  private readonly starting = new Map<string, { promise: Promise<{ url: string; preloaded?: boolean }>; controller: AbortController }>()
  private client?: OpenCodeClient
  progress: (runID: string, text: string) => Promise<void> = async () => {}
  readonly root: string
  constructor(readonly ctx: Plugin.Context, readonly options: Options) {
    this.runtime = new Runtime(options, ctx.location.directory)
    this.root = path.join(this.runtime.cache, 'sessions', hash(ctx.location.directory).slice(0, 24))
  }
  async sync(value: unknown): Promise<{ sessionID: string }> {
    const source = validateSource(value), sessionID = String(source.session.id)
    const current = record(await this.ctx.session.get({ sessionID } as Parameters<Plugin.Context['session']['get']>[0]))
    if (current.id !== sessionID || record(current.location).directory !== source.directory) throw new Error('Session capture does not match this OpenCode server.')
    this.remember(source)
    await this.materialize(source)
    return { sessionID }
  }
  async sessions(limit = 20): Promise<Record<string, unknown>[]> {
    const page = await this.ctx.session.list({ directory: this.ctx.location.directory, limit } as Parameters<Plugin.Context['session']['list']>[0])
    return page.data.map(record)
  }
  async resolve(currentID: string, selector?: string): Promise<{ sessionID: string; path?: string }> {
    if (!selector) {
      if (!currentID || currentID === 'home') throw new Error('Open a session first, then run /obs.')
      return { sessionID: currentID }
    }
    const file = path.resolve(this.ctx.location.directory, selector.replace(/^~(?=[/\\])/, process.env.HOME ?? ''))
    if (await exists(file)) return { sessionID: hash(file).slice(0, 24), path: file }
    const found: string[] = []
    let cursor: string | undefined
    const cursors = new Set<string>()
    do {
      const page = await this.ctx.session.list((cursor ? { cursor, limit: 200 } : { directory: this.ctx.location.directory, limit: 200 }) as Parameters<Plugin.Context['session']['list']>[0])
      for (const item of page.data) if (item.id === selector) return { sessionID: item.id }; else if (item.id.startsWith(selector)) found.push(item.id)
      cursor = page.cursor.next ?? undefined
      if (cursor && cursors.has(cursor)) throw new Error('OpenCode repeated a session cursor.')
      if (cursor) cursors.add(cursor)
    } while (cursor)
    if (found.length !== 1) throw new Error(found.length ? `Ambiguous session prefix: ${found.join(', ')}` : `No session matching '${selector}'. Run /obs sessions.`)
    return { sessionID: found[0]! }
  }
  private async connection(sessionID: string): Promise<OpenCodeClient | undefined> {
    if (this.client) return this.client
    const discovered = await Service.discover({ version: this.ctx.app.version })
    const password = process.env[this.options.serverPasswordEnv ?? 'OPENCODE_PASSWORD'] ?? process.env.OPENCODE_SERVER_PASSWORD
    const endpoint = this.options.serverUrl
      ? discovered?.url === this.options.serverUrl ? discovered : { url: this.options.serverUrl, ...(password ? {auth:{type:'basic' as const,username:'opencode',password}} : {}) }
      : discovered
    if (!endpoint) return undefined
    const client = OpenCode.make({ baseUrl: endpoint.url, headers: Service.headers(endpoint) })
    // A standalone/remote plugin must never borrow an unrelated registered service.
    try {
      const session = await client.session.get({ sessionID }, { signal: AbortSignal.timeout(5000) })
      const local = record(await this.ctx.session.get({ sessionID } as Parameters<Plugin.Context['session']['get']>[0]))
      if (session.id !== local.id || session.location.directory !== record(local.location).directory) return undefined
    } catch { return undefined }
    this.client = client
    return client
  }
  private async capture(sessionID: string, signal?: AbortSignal): Promise<NativeSource> {
    const fixed = this.pinned.getStore()?.get(sessionID)
    if (fixed) return fixed
    const cached = this.sources.get(sessionID)
    if (cached && !this.dirty.has(sessionID) && Date.now() - (this.capturedAt.get(sessionID) ?? 0) < 2000) return cached
    const pending = this.captures.get(sessionID)
    if (pending) return pending
    const next = (async () => {
      const client = await this.connection(sessionID)
      if (!client) throw new Error('Full session history is not synchronized. Run /obs from the OpenCode terminal UI, or configure serverUrl for this server.')
      const source = validateSource(await captureSession(client, sessionID, signal))
      this.remember(source)
      return source
    })()
    this.captures.set(sessionID, next)
    try { return await next } finally { this.captures.delete(sessionID) }
  }
  invalidate(sessionID: string): void {
    if (this.sources.has(sessionID)) this.dirty.add(sessionID)
    // Child activity also changes every materialized parent family.
    for (const id of this.parents.get(sessionID) ?? []) this.dirty.add(id)
  }
  private remember(source: NativeSource): void {
    const id = String(source.session.id)
    for (const child of this.families.get(id) ?? []) this.parents.get(child)?.delete(id)
    const children = new Set<string>()
    const visit = (item: NativeSource) => { for (const child of item.children) { const key = String(child.session.id); if (!children.has(key)) { children.add(key); visit(child) } } }
    visit(source)
    this.families.set(id, children)
    for (const child of children) { const roots = this.parents.get(child) ?? new Set<string>(); roots.add(id); this.parents.set(child, roots) }
    this.sources.delete(id); this.sources.set(id, source)
    this.capturedAt.set(id, Date.now()); this.dirty.delete(id)
    this.sourceBytes.set(id, Buffer.byteLength(JSON.stringify(source)))
    while (this.sources.size > 64 || [...this.sourceBytes.values()].reduce((a,b) => a+b, 0) > 128 * 1024 * 1024) {
      const oldest = this.sources.keys().next().value!
      this.sources.delete(oldest); this.capturedAt.delete(oldest); this.dirty.delete(oldest); this.sourceBytes.delete(oldest)
      for (const child of this.families.get(oldest) ?? []) { const roots = this.parents.get(child); roots?.delete(oldest); if (!roots?.size) this.parents.delete(child) }
      this.families.delete(oldest)
    }
  }
  withSources<T>(enabled: boolean | undefined, task: () => Promise<T>): Promise<T> { return this.revisions.run(() => enabled ? this.pinned.run(new Map(this.sources), task) : task()) }
  async materialize(source: NativeSource): Promise<SourceRef> {
    const sessionID = String(source.session.id)
    const artifacts = path.join(this.root, hash(sessionID).slice(0, 32))
    let captured = source
    const model = record(source.session.model)
    if (typeof model.id === 'string') {
      const models = await this.ctx.model.list()
      const info = models.data.find(m => m.id === model.id && m.providerID === model.providerID)
      if (info && Number.isSafeInteger(info.limit.context) && info.limit.context > 0) captured = { ...source, window: info.limit.context }
    }
    const json = JSON.stringify(captured)
    const transcript = await this.revisions.write(artifacts, json)
    this.targets.set(sessionID, transcript)
    const argv = ['--transcript', transcript, '--artifacts', artifacts]
    if (captured.window) argv.push('--window', String(captured.window))
    return { sessionID, transcript, artifacts, argv, native: captured }
  }
  async session(currentID: string, selector?: string, signal?: AbortSignal): Promise<SourceRef> {
    const target = await this.resolve(currentID, selector)
    if (target.path) {
      this.targets.set(target.sessionID, target.path)
      const artifacts = path.join(this.root, hash(target.path).slice(0, 32))
      return { sessionID: target.sessionID, transcript: target.path, artifacts, argv: ['--transcript', target.path, '--artifacts', artifacts] }
    }
    const source = await this.materialize(await this.capture(target.sessionID, signal))
    if (!selector) source.argv.push('--kind', 'current')
    return source
  }
  async snapshot(sessionID: string, signal?: AbortSignal, selector?: string): Promise<Snapshot | EmptySnapshot> {
    const source = await this.session(sessionID, selector, signal)
    const snapshot = await this.runtime.run<Snapshot | EmptySnapshot>(['snapshot', ...source.argv], signal)
    if (!snapshot.empty) {
      const native = source.native
      const costs: number[] = []
      const cost = (item: NativeSource): boolean => {
        const value = item.session.cost
        if (typeof value !== 'number' || !Number.isFinite(value)) return false
        costs.push(value)
        return item.children.every(cost)
      }
      if (native && cost(native)) snapshot.engine_usage = { context: { window: snapshot.context.window ?? 0 }, cost: { usd: costs.reduce((a,b) => a+b, 0) } }
    }
    return snapshot
  }
  async serve(kind: 'dashboard' | 'converge', argv: string[], signal?: AbortSignal): Promise<{ url: string; preloaded?: boolean }> {
    const at = argv.indexOf('--transcript'), labelsAt = argv.indexOf('--labels')
    const identity = at >= 0 ? argv[at + 1]! : argv[argv.indexOf('--data-root') + 1]!
    const canonical = /^source-[a-f0-9]{64}\.json$/.test(path.basename(identity)) ? path.dirname(identity) : identity
    const key = `${kind}:${canonical}`
    const revision = at >= 0 ? hash(await readFile(identity, 'utf8')) : ''
    const labelRevision = labelsAt >= 0 ? hash(await readFile(argv[labelsAt + 1]!, 'utf8')) : ''
    const signature = JSON.stringify([argv, revision, labelRevision])
    const live = this.servers.get(key)
    if (live?.signature === signature && live.child.exitCode === null && live.child.signalCode === null) return live
    const pending = this.starting.get(key)
    if (pending) { await pending.promise; return this.serve(kind, argv, signal) }
    const controller = new AbortController()
    const startupSignal = AbortSignal.any([controller.signal, ...(signal ? [signal] : [])])
    const next = (async () => {
      await this.stopKey(key)
      const launch = await this.runtime.argv([...argv, '--exit-on-stdin'], startupSignal, ['app'])
      startupSignal.throwIfAborted()
      const child = this.runtime.spawn(launch)
      return new Promise<{ url: string; preloaded?: boolean }>((resolve, reject) => {
        let buffer = '', stderr = '', settled = false
        let release = () => {}
        child.stdout.setEncoding('utf8'); child.stderr.setEncoding('utf8')
        const finish = (error?: Error, result?: {url:string;preloaded?:boolean}) => {
          if (settled) return
          settled = true; clearTimeout(timer); startupSignal.removeEventListener('abort', abort)
          if (error) { stopChild(child); reject(error) }
          else { release = this.revisions.retain(identity); this.servers.set(key, { child, signature, ...result! }); resolve(result!) }
        }
        const abort = () => finish(new Error('Cancelled dashboard startup.'))
        const timer = setTimeout(() => finish(new Error('Dashboard startup exceeded 120 seconds.')), 120_000)
        startupSignal.addEventListener('abort', abort, { once: true })
        child.stdout.on('data', chunk => {
          if (settled) return
          buffer += chunk
          if (buffer.length > 65536) { finish(new Error('Dashboard returned too much startup output.')); return }
          const lines = buffer.split('\n'); buffer = lines.pop() ?? ''
          for (const line of lines) { try { const doc = JSON.parse(line); if (doc.error) finish(new Error(doc.error.message)); else if (typeof doc.url === 'string' && /^http:\/\/127\.0\.0\.1:\d+\/$/.test(doc.url)) finish(undefined, doc) } catch { /* Ignore non-JSON dependency notices. */ } }
        })
        child.stderr.on('data', chunk => { stderr = (stderr + chunk).slice(-4096) })
        child.once('error', error => finish(error))
        child.once('close', () => { release(); if (this.servers.get(key)?.child === child) this.servers.delete(key); finish(new Error(`Dashboard stopped before becoming ready: ${stderr.trim().split('\n').slice(-1).join('')}`)) })
      })
    })()
    this.starting.set(key, {promise:next,controller})
    try { return await next } finally { this.starting.delete(key) }
  }
  async stop(kind: string, sessionID?: string): Promise<boolean> {
    const target = sessionID ? this.targets.get(sessionID) ?? path.join(this.root, hash(sessionID).slice(0, 32), 'source.json') : undefined
    const canonical = target && /^source-[a-f0-9]{64}\.json$/.test(path.basename(target)) ? path.dirname(target) : target
    const keys = [...new Set([...this.servers.keys(), ...this.starting.keys()])].filter(key => key.startsWith(`${kind}:`) && (!canonical || key === `${kind}:${canonical}`))
    for (const key of keys) {
      const starting = this.starting.get(key)
      starting?.controller.abort()
      await starting?.promise.catch(() => {})
      await this.stopKey(key)
    }
    return keys.length > 0
  }
  private async stopKey(key: string): Promise<boolean> {
    const server = this.servers.get(key)
    if (!server) return false
    this.servers.delete(key)
    stopChild(server.child)
    await new Promise<void>(resolve => { if (server.child.exitCode !== null || server.child.signalCode !== null) resolve(); else server.child.once('close', () => resolve()) })
    return true
  }
  async close(): Promise<void> { for (const start of this.starting.values()) start.controller.abort(); this.runtime.close(); this.servers.clear(); this.sources.clear(); await this.revisions.close() }
}
