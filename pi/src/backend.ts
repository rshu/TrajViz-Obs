import { createHash } from 'node:crypto'
import { existsSync } from 'node:fs'
import { unlink, realpath } from 'node:fs/promises'
import path from 'node:path'
import { homedir } from 'node:os'
import { fileURLToPath } from 'node:url'
import { SessionManager, type ExtensionContext, type SessionInfo } from '@earendil-works/pi-coding-agent'
import { atomicFile, Runtime, stopChild, type Options } from '../../opencode/src/runtime.js'
import { Revisions } from '../../opencode/src/revisions.js'
import { valueArgv } from '../../hooks/lib/args.js'
import type { EmptySnapshot, Snapshot } from './types.js'
import type { ChildProcessWithoutNullStreams } from 'node:child_process'

export { Runtime, type Options }
export type Source = { sessionID: string; transcript: string; artifacts: string; argv: string[]; liveTranscript?: string; captureSessionID?: string }
type Server = { child: ChildProcessWithoutNullStreams; url: string; signature: string; release(): void }
export const digest = (value: string) => createHash('sha256').update(value).digest('hex')

/** Resolve existing symlink ancestors even when the final artifact directory is new. */
async function canonicalPath(file: string): Promise<string> {
  try { return await realpath(file) }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error
    const parent = path.dirname(file)
    return parent === file ? file : path.join(await canonicalPath(parent), path.basename(file))
  }
}

/** Reads Pi through its SDK; private captures never mutate the user's session. */
export class Backend {
  readonly runtime: Runtime
  readonly revisions = new Revisions()
  readonly root: string
  readonly servers = new Map<string, Server>()
  private readonly sequences = new Map<string, number>()
  private readonly liveWrites = new Map<string, Promise<void>>()
  private readonly liveDigests = new Map<string, string>()
  private readonly starting = new Map<string, Promise<void>>()
  private readonly pendingStarts = new Set<{ key: string; controller: AbortController }>()
  private readonly activeSources = new Set<Promise<Source>>()
  private closing = false
  private closed?: Promise<void>
  private nativeInput?: string
  constructor(readonly cwd: string, readonly options: Options = {}) {
    const packaged = fileURLToPath(new URL('../runtime/bridge/', import.meta.url))
    const repository = fileURLToPath(new URL('../../bridge/', import.meta.url))
    const bridge = existsSync(path.join(repository, 'trajviz_obs_cli.py')) ? repository : packaged
    this.runtime = new Runtime(options, cwd, { cacheNamespace: 'pi', bridge, protectedInputs: () => this.nativeInput ? [this.nativeInput] : [] })
    this.root = path.join(this.runtime.cache, 'sessions', digest(cwd).slice(0, 24))
  }
  async sessions(ctx: ExtensionContext, signal?: AbortSignal): Promise<SessionInfo[]> {
    return SessionManager.list(ctx.cwd, ctx.sessionManager.getSessionDir(), undefined, signal)
  }
  /** Actual native provenance travels as a host argument, never imported metadata. */
  async protect(ctx: ExtensionContext, artifacts?: string): Promise<void> {
    const original = ctx.sessionManager.getSessionFile()
    this.nativeInput = original ? path.resolve(ctx.cwd, original) : undefined
    if (!this.nativeInput) return
    const source = await canonicalPath(this.nativeInput)
    for (const folder of [this.runtime.cache, ...(artifacts ? [artifacts] : [])]) {
      const relative = path.relative(await canonicalPath(folder), source)
      if (!relative || (!path.isAbsolute(relative) && relative !== '..' && !relative.startsWith(`..${path.sep}`))) {
        throw new Error('The Obs cache or artifact directory contains the live Pi session. Choose a different cacheRoot.')
      }
    }
  }
  async source(ctx: ExtensionContext, selector?: string, signal?: AbortSignal, captureSessionID?: string): Promise<Source> {
    const operation = this.capture(ctx, selector, signal, captureSessionID)
    this.activeSources.add(operation)
    try { return await operation } finally { this.activeSources.delete(operation) }
  }
  private async capture(ctx: ExtensionContext, selector?: string, signal?: AbortSignal, captureSessionID?: string): Promise<Source> {
    signal?.throwIfAborted()
    if (this.closing) throw new Error('The obs plugin is unloading.')
    if (captureSessionID !== undefined && (!selector || !captureSessionID.trim() || captureSessionID.length > 4096 || captureSessionID.includes('\u0000'))) throw new Error('--session-id requires --session <capture path> and an exact nonempty captured session id up to 4096 characters, without NUL.')
    let transcript: string, sessionID: string, liveTranscript: string | undefined, current = false
    if (!selector || selector === ctx.sessionManager.getSessionId()) {
      if (captureSessionID !== undefined) throw new Error('--session-id requires an imported capture path.')
      sessionID = ctx.sessionManager.getSessionId()
      const sequence = (this.sequences.get(sessionID) ?? 0) + 1
      this.sequences.set(sessionID, sequence)
      const manager = ctx.sessionManager
      const header = manager.getHeader() ?? { type: 'session', version: 3, id: sessionID, timestamp: new Date().toISOString(), cwd: ctx.cwd }
      const context = ctx.getContextUsage()
      const window = context?.contextWindow ?? ctx.model?.contextWindow
      const metadata = { schema_version: 1, leaf_id: manager.getLeafId(),
        ...(window && Number.isSafeInteger(window) && window > 0 ? { context_window: window } : {}),
        context_tokens: context?.tokens ?? null }
      const rows = [{ ...header, _trajviz_obs_pi: metadata }, ...manager.getEntries()]
      const artifacts = path.join(this.root, digest(sessionID).slice(0, 32))
      const serialized = JSON.stringify(rows)
      if (Buffer.byteLength(serialized) > 64 * 1024 * 1024) throw new Error('Pi session capture exceeds 64 MiB. Choose a smaller saved session or export it for analysis.')
      await this.protect(ctx, artifacts)
      transcript = await this.revisions.write(artifacts, serialized)
      liveTranscript = path.join(artifacts, `live-${this.revisions.instance}.json`)
      const live = liveTranscript
      // The dashboard's Load action rereads this private file. Requests keep their
      // immutable revision so an automatic refresh cannot change their input.
      const previous = this.liveWrites.get(sessionID) ?? Promise.resolve()
      const publish = previous.catch(() => {}).then(async () => {
        if (this.sequences.get(sessionID) === sequence) {
          await atomicFile(live, serialized)
          this.liveDigests.set(live, digest(serialized))
        }
      })
      this.liveWrites.set(sessionID, publish)
      try { await publish } finally { if (this.liveWrites.get(sessionID) === publish) this.liveWrites.delete(sessionID) }
      current = true
    } else {
      await this.protect(ctx)
      const target = path.resolve(ctx.cwd, selector.replace(/^~(?=[/\\])/, homedir()))
      if (existsSync(target)) {
        transcript = target
        sessionID = digest(JSON.stringify([target, captureSessionID])).slice(0, 32)
      } else {
        if (captureSessionID !== undefined) throw new Error('--session-id requires an existing capture path.')
        const sessions = await this.sessions(ctx, signal)
        const exact = sessions.find(session => session.id === selector)
        const matches = exact ? [exact] : sessions.filter(session => session.id.startsWith(selector))
        if (matches.length !== 1) throw new Error(matches.length ? 'Ambiguous Pi session prefix. Run /obs sessions.' : 'No matching Pi session. Run /obs sessions or pass a session path.')
        transcript = matches[0]!.path; sessionID = matches[0]!.id
      }
    }
    const artifacts = path.join(this.root, digest(sessionID).slice(0, 32))
    if (!current) await this.protect(ctx, artifacts)
    const argv = ['--transcript', transcript, '--artifacts', artifacts]
    if (current) argv.push('--kind', 'current')
    if (captureSessionID !== undefined) argv.push(...valueArgv('session-id', captureSessionID))
    return { sessionID, transcript, artifacts, argv, ...(liveTranscript ? { liveTranscript } : {}), ...(captureSessionID === undefined ? {} : { captureSessionID }) }
  }
  async snapshot(ctx: ExtensionContext, signal?: AbortSignal, selector?: string, captureSessionID?: string): Promise<Snapshot | EmptySnapshot> {
    return this.revisions.run(async () => {
      const source = await this.source(ctx, selector, signal, captureSessionID)
      return this.runtime.run(['snapshot', ...source.argv], signal)
    })
  }
  async serve(key: string, argv: string[], usageArgs: string[], signal: AbortSignal): Promise<{ url: string }> {
    signal.throwIfAborted()
    if (this.closing) throw new Error('The obs plugin is unloading.')
    const previous = this.starting.get(key) ?? Promise.resolve()
    let release!: () => void
    const gate = new Promise<void>(resolve => { release = resolve })
    this.starting.set(key, gate)
    const pending = { key, controller: new AbortController() }
    this.pendingStarts.add(pending)
    const combined = AbortSignal.any([signal, pending.controller.signal])
    const index = argv.indexOf('--transcript')
    const retain = index >= 0 && argv[index + 1] ? this.revisions.retain(argv[index + 1]!) : () => {}
    try {
      await previous
      combined.throwIfAborted()
      if (this.closing) throw new Error('The obs plugin is unloading.')
      return await this.revisions.run(() => this.start(key, argv, usageArgs, combined))
    } finally {
      retain()
      release()
      this.pendingStarts.delete(pending)
      if (this.starting.get(key) === gate) this.starting.delete(key)
    }
  }
  private async start(key: string, argv: string[], usageArgs: string[], signal: AbortSignal): Promise<{ url: string }> {
    const index = argv.indexOf('--transcript')
    const source = index < 0 ? undefined : argv[index + 1]
    const signature = JSON.stringify([argv, usageArgs, source ? this.liveDigests.get(source) : undefined])
    const prior = this.servers.get(key)
    if (prior?.signature === signature && prior.child.exitCode === null && prior.child.signalCode === null) return { url: prior.url }
    // Install before stopping a functioning dashboard in case setup fails.
    const launch = await this.runtime.argv([...argv, '--exit-on-stdin', ...usageArgs], signal, ['app'])
    signal.throwIfAborted()
    await this.stopServers(key)
    signal.throwIfAborted()
    const child = this.runtime.spawn(launch)
    return new Promise((resolve, reject) => {
      let buffer = '', stderr = '', settled = false
      let release = () => {}
      const finish = (error?: Error, url?: string) => {
        if (settled) return
        settled = true; clearTimeout(timer); signal.removeEventListener('abort', abort)
        if (error) { stopChild(child); reject(error) }
        else {
          release = source ? this.revisions.retain(source) : () => {}
          this.servers.set(key, { child, url: url!, signature, release }); resolve({ url: url! })
        }
      }
      const abort = () => finish(new Error('Cancelled dashboard startup.'))
      const timer = setTimeout(() => finish(new Error('Dashboard startup exceeded 120 seconds.')), 120_000)
      signal.addEventListener('abort', abort, { once: true })
      child.stdout.setEncoding('utf8'); child.stderr.setEncoding('utf8')
      child.stdout.on('data', chunk => {
        if (settled) return
        buffer += chunk
        if (buffer.length > 65536) { finish(new Error('Dashboard startup output exceeded its limit.')); return }
        const lines = buffer.split('\n'); buffer = lines.pop() ?? ''
        for (const line of lines) {
          let doc: { url?: string; error?: { message: string } }
          try { doc = JSON.parse(line) } catch { continue }
          if (doc.error) finish(new Error(doc.error.message))
          else if (doc.url && /^http:\/\/127\.0\.0\.1:\d+\/$/.test(doc.url)) finish(undefined, doc.url)
        }
      })
      child.stderr.on('data', chunk => { stderr = (stderr + chunk).slice(-4096) })
      child.once('error', error => finish(error))
      child.once('close', () => {
        release()
        if (this.servers.get(key)?.child === child) this.servers.delete(key)
        finish(new Error(`Dashboard stopped before becoming ready: ${stderr.trim().split('\n').at(-1) ?? ''}`))
      })
    })
  }
  async stop(key?: string): Promise<boolean> {
    const starts = [...this.pendingStarts].filter(pending => key === undefined || pending.key === key)
    for (const pending of starts) pending.controller.abort(new Error('Cancelled dashboard startup.'))
    const stopped = await this.stopServers(key)
    return starts.length > 0 || stopped
  }
  private async stopServers(key?: string): Promise<boolean> {
    const matches = [...this.servers].filter(([name]) => key === undefined || name === key)
    for (const [name, server] of matches) {
      this.servers.delete(name); stopChild(server.child)
      if (server.child.exitCode === null && server.child.signalCode === null) await new Promise<void>(resolve => server.child.once('close', () => resolve()))
      server.release()
    }
    return matches.length > 0
  }
  close(): Promise<void> {
    if (!this.closed) {
      this.closing = true
      for (const pending of this.pendingStarts) pending.controller.abort(new Error('The obs plugin is unloading.'))
      this.runtime.close()
      this.closed = (async () => {
        await Promise.all(this.starting.values())
        await this.stopServers()
        await Promise.allSettled(this.activeSources)
        await Promise.allSettled(this.liveWrites.values())
        await this.revisions.close()
        await Promise.all([...this.liveDigests.keys()].map(file => unlink(file).catch(() => {})))
      })()
    }
    return this.closed
  }
}
