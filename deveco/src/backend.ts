import { createHash } from 'node:crypto'
import { existsSync } from 'node:fs'
import { readFile, realpath, unlink } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import type { ChildProcessWithoutNullStreams } from 'node:child_process'
import { Runtime, atomicFile, stopChild, type Options } from '../../opencode/src/runtime.js'
import { Revisions } from '../../opencode/src/revisions.js'
import { promptExplorerArgs } from '../../shared/prompt-capture.js'
import { captureSession, record, type NativeSource } from './collect.js'
import type { HostClient } from './host.js'

export type Source = { sessionID: string; transcript: string; liveTranscript: string; artifacts: string; argv: string[] }
type Server = { child: ChildProcessWithoutNullStreams; url: string }
const hash = (text: string) => createHash('sha256').update(text).digest('hex')

/** Host-owned copies only: no direct access to DevEco's database or credentials. */
export class Backend {
  readonly runtime: Runtime
  readonly revisions = new Revisions()
  readonly root: string
  readonly servers = new Map<string, Server>()
  private readonly lifetime = new AbortController()
  private readonly active = new Set<Promise<unknown>>()
  private readonly latest = new Map<string, number>()
  private readonly live = new Map<string, string>()
  private readonly published = new Map<string, number>()
  private readonly writes = new Map<string, Promise<void>>()
  private readonly starts = new Map<string, { controller: AbortController; promise: Promise<{ url: string }> }>()
  private timer?: ReturnType<typeof setTimeout>
  private refreshing?: Promise<void>
  private dirty = false
  private nativeReads = 0
  private closed?: Promise<void>
  constructor(readonly client: HostClient, readonly directory: string, readonly options: Options = {}) {
    const repository = fileURLToPath(new URL('../../bridge/', import.meta.url))
    const packaged = fileURLToPath(new URL('../runtime/bridge/', import.meta.url))
    this.runtime = new Runtime(options, directory, { cacheNamespace: 'deveco',
      bridge: existsSync(path.join(repository, 'trajviz_obs_cli.py')) ? repository : packaged })
    this.root = path.join(this.runtime.cache, 'sessions', hash(directory).slice(0, 24))
  }
  private signal(requested?: AbortSignal): AbortSignal {
    return requested ? AbortSignal.any([requested, this.lifetime.signal]) : this.lifetime.signal
  }
  private track<T>(task: Promise<T>): Promise<T> {
    this.active.add(task)
    void task.then(() => this.active.delete(task), () => this.active.delete(task))
    return task
  }
  async source(sessionID: string, requested?: AbortSignal): Promise<Source> {
    if (!sessionID || sessionID.length > 4096 || sessionID.includes('\0')) throw new Error('A native DevEco session ID is required.')
    const signal = this.signal(requested)
    signal.throwIfAborted()
    if (this.nativeReads >= 4) throw new Error('Four native history reads are already running. Retry after an active inspection finishes.')
    this.nativeReads++
    return this.track((async () => {
      try {
        const sequence = (this.latest.get(sessionID) ?? 0) + 1
        this.latest.set(sessionID, sequence)
        const native = await captureSession(this.client, sessionID, this.directory, signal)
        signal.throwIfAborted()
        return await this.materialize(native, sequence, signal)
      } finally { this.nativeReads-- }
    })())
  }
  private async materialize(native: NativeSource, sequence: number, signal: AbortSignal): Promise<Source> {
    const sessionID = String(native.session.id), artifacts = path.join(this.root, hash(sessionID).slice(0, 32))
    const text = JSON.stringify(native)
    const transcript = await this.revisions.write(artifacts, text)
    const liveTranscript = path.join(artifacts, `live-${this.revisions.instance}.json`)
    const prior = this.writes.get(sessionID) ?? Promise.resolve()
    const publish = prior.catch(() => {}).then(async () => {
      signal.throwIfAborted()
      if ((this.published.get(sessionID) ?? 0) >= sequence) return
      await atomicFile(liveTranscript, text)
      this.live.set(sessionID, liveTranscript)
      this.published.set(sessionID, sequence)
    })
    this.writes.set(sessionID, publish)
    try { await publish } finally { if (this.writes.get(sessionID) === publish) this.writes.delete(sessionID) }
    return { sessionID, transcript, liveTranscript, artifacts, argv: ['--transcript', transcript, '--artifacts', artifacts] }
  }
  async inspect(sessionID: string, command: string, args: string[] = [], signal?: AbortSignal): Promise<Record<string, unknown>> {
    return this.revisions.run(async () => {
      const source = await this.source(sessionID, signal)
      return this.runtime.run([command, ...source.argv, ...args], this.signal(signal))
    })
  }
  async sessions(limit: number, requested?: AbortSignal): Promise<Record<string, unknown>[]> {
    const signal = this.signal(requested)
    signal.throwIfAborted()
    if (!this.client.session.list) throw new Error('This DevEco host does not expose native session listing.')
    const response = await this.client.session.list({ query: { directory: this.directory, limit }, signal, throwOnError: true })
    if (!Array.isArray(response.data)) throw new Error('DevEco returned no session list.')
    const canonical = (directory: string) => realpath(directory).catch(() => path.resolve(directory))
    const expected = await canonical(this.directory)
    const rows = await Promise.all(response.data.map(async value => {
      const row = record(value)
      return typeof row.directory === 'string' && await canonical(row.directory) === expected ? row : undefined
    }))
    return rows.filter((row): row is Record<string, unknown> => row !== undefined).slice(0, limit)
  }
  /** Events trigger bounded/coalesced native reads only while a dashboard is open. */
  changed(): void {
    if (this.lifetime.signal.aborted || !this.servers.size) return
    this.dirty = true
    if (this.timer || this.refreshing) return
    this.timer = setTimeout(() => {
      this.timer = undefined
      this.refreshing = this.refresh().finally(() => { this.refreshing = undefined; if (this.dirty) this.changed() })
    }, 200)
    this.timer.unref()
  }
  private async refresh(): Promise<void> {
    this.dirty = false
    for (const sessionID of this.servers.keys()) {
      const before = this.published.get(sessionID)
      try { await this.revisions.run(() => this.source(sessionID)) }
      catch { await this.markStale(sessionID, before) }
    }
  }
  private async markStale(sessionID: string, version: number | undefined): Promise<void> {
    const file = this.live.get(sessionID)
    if (!file || this.lifetime.signal.aborted) return
    const previous = this.writes.get(sessionID) ?? Promise.resolve()
    const write = previous.catch(() => {}).then(async () => {
      if (this.lifetime.signal.aborted || this.published.get(sessionID) !== version) return
      const source = JSON.parse(await readFile(file, 'utf8')) as Record<string, unknown>
      source.source_health = { status: 'stale', reason: 'native_refresh_failed' }
      source.warnings = ['Live DevEco refresh failed; this is the last successfully captured native session. Child-session discovery may be stale. Retry obs overview or dashboard.']
      await atomicFile(file, JSON.stringify(source))
    }).catch(() => {})
    this.writes.set(sessionID, write)
    try { await write } finally { if (this.writes.get(sessionID) === write) this.writes.delete(sessionID) }
  }
  async dashboard(sessionID: string, port?: number, requested?: AbortSignal): Promise<{ url: string }> {
    const reuse = (result: { url: string }) => {
      if (port !== undefined && Number(new URL(result.url).port) !== port) {
        throw new Error('A dashboard is already running or starting on a different port. Use "dashboard stop" before changing ports.')
      }
      return result
    }
    const existing = this.servers.get(sessionID)
    if (existing && existing.child.exitCode === null && existing.child.signalCode === null) {
      const result = reuse({ url: existing.url })
      await this.revisions.run(() => this.source(sessionID, requested))
      return result
    }
    const pending = this.starts.get(sessionID)
    if (pending) return reuse(await this.wait(pending.promise, this.signal(requested)))
    if (this.servers.size + this.starts.size >= 8) throw new Error('Close an existing dashboard before opening another one (maximum 8).')
    const controller = new AbortController()
    const signal = AbortSignal.any([this.signal(requested), controller.signal])
    const promise = this.track(this.revisions.run(async () => {
      const source = await this.source(sessionID, signal)
      const args = ['dashboard', '--transcript', source.liveTranscript, '--exit-on-stdin', ...promptExplorerArgs(sessionID)]
      if (port !== undefined) args.push('--port', String(port))
      const argv = await this.runtime.argv(args, signal, ['app'])
      signal.throwIfAborted()
      return this.start(sessionID, argv, signal)
    }))
    this.starts.set(sessionID, { controller, promise })
    try { return await promise } finally { if (this.starts.get(sessionID)?.promise === promise) this.starts.delete(sessionID) }
  }
  private wait<T>(promise: Promise<T>, signal: AbortSignal): Promise<T> {
    signal.throwIfAborted()
    return new Promise((resolve, reject) => {
      const abort = () => { signal.removeEventListener('abort', abort); reject(new Error('Dashboard startup wait cancelled.')) }
      signal.addEventListener('abort', abort, { once: true })
      void promise.then(value => { signal.removeEventListener('abort', abort); resolve(value) }, error => { signal.removeEventListener('abort', abort); reject(error) })
    })
  }
  private start(sessionID: string, argv: string[], signal: AbortSignal): Promise<{ url: string }> {
    const child = this.runtime.spawn(argv)
    return new Promise((resolve, reject) => {
      let stdout = '', stderr = '', settled = false
      const finish = (error?: Error, url?: string) => {
        if (settled) return
        settled = true; clearTimeout(timer); signal.removeEventListener('abort', abort)
        if (error) { stopChild(child); reject(error) }
        else { this.servers.set(sessionID, { child, url: url! }); resolve({ url: url! }) }
      }
      const abort = () => finish(new Error('Dashboard startup cancelled.'))
      const timer = setTimeout(() => finish(new Error('Dashboard startup exceeded 120 seconds.')), 120_000)
      signal.addEventListener('abort', abort, { once: true })
      child.stdout.setEncoding('utf8'); child.stderr.setEncoding('utf8')
      child.stdout.on('data', chunk => {
        if (settled) return
        stdout += chunk
        if (stdout.length > 65536) { finish(new Error('Dashboard startup output exceeded its limit.')); return }
        const lines = stdout.split('\n'); stdout = lines.pop() ?? ''
        for (const line of lines) {
          let value: { url?: string; error?: { message?: string } }
          try { value = JSON.parse(line) } catch { continue }
          if (value.error) finish(new Error(value.error.message ?? 'Dashboard startup failed.'))
          else if (value.url && /^http:\/\/127\.0\.0\.1:\d+\/$/.test(value.url)) finish(undefined, value.url)
        }
      })
      child.stderr.on('data', chunk => { stderr = (stderr + chunk).slice(-4096) })
      child.once('error', error => finish(error))
      child.once('close', () => {
        if (this.servers.get(sessionID)?.child === child) this.servers.delete(sessionID)
        finish(new Error(`Dashboard stopped before becoming ready: ${stderr.trim().split('\n').at(-1) ?? ''}`))
      })
      if (signal.aborted) abort()
    })
  }
  async stop(sessionID?: string): Promise<boolean> {
    const pending = [...this.starts].filter(([id]) => sessionID === undefined || sessionID === id)
    for (const [, item] of pending) item.controller.abort()
    const servers = [...this.servers].filter(([id]) => sessionID === undefined || sessionID === id)
    for (const [id, server] of servers) {
      this.servers.delete(id); stopChild(server.child)
      if (server.child.exitCode === null && server.child.signalCode === null) await new Promise<void>(resolve => server.child.once('close', resolve))
    }
    await Promise.allSettled(pending.map(([, item]) => item.promise))
    return Boolean(pending.length || servers.length)
  }
  close(): Promise<void> {
    if (!this.closed) {
      this.lifetime.abort(); clearTimeout(this.timer); this.dirty = false
      this.runtime.close()
      this.closed = (async () => {
        await this.stop()
        await Promise.allSettled([...this.active, ...this.writes.values(), ...(this.refreshing ? [this.refreshing] : [])])
        await this.revisions.close()
        await Promise.all([...this.live.values()].map(file => unlink(file).catch(() => {})))
      })()
    }
    return this.closed
  }
}
