// The dashboard servers: the bridge's `dashboard` (Insight) and `converge`
// children. Each runs in a detached loop over `$.process.spawn` that the
// command hook never awaits, its stream kept in a module-level map by kind
// so `stop` can `return()` it (which kills the child) and the session's end
// can end them all. The child's first stdout line is `{"url": …}` (with
// `"preloaded": false` when the Insight preload could not be wired); the
// bridge's error envelope on stdout (a busy port, a missing extra) or an exit
// before it is the start's failure, and the child is ended at once. What the
// child writes to stderr before the URL (Gradio's prints, Python warnings)
// is context for the exit row, never a failure by itself. Every helper
// taking `$` is a function declaration (validate follows `$` into one);
// state is reached only through the functions in state.ts.
import type { EngineInterface, ProcessSpawnChunk, ProcessSpawnResult, Timer } from 'claude-code'
import type { Dashboards } from '../../types'
import { publicationGeneration } from './publication'
import { resetDashboards, setDashboard } from './state'
import { argvValue } from './args'

export type Kind = keyof Dashboards

/** A child's URL line: where it serves, and whether the session preload was wired (always true for converge). */
export type UrlLine = { url: string; preloaded: boolean; labels_preloaded?: boolean }

/** What a start answers: the child's URL line, or why it did not start. */
export type Started = UrlLine | { error: string }

/** The spawned child's stream, as `$.process.spawn` answers it. */
export type ServerStream = AsyncGenerator<ProcessSpawnChunk, ProcessSpawnResult>
export const START_TIMEOUT_MS = 120_000

/** Text as it arrives, cut into whole lines (`\n`, a `\r` before it dropped). */
export class LineBuffer {
  private pending = ''

  /** The lines `text` completes, in order; the rest waits for its newline. */
  push(text: string): string[] {
    const lines = (this.pending + text).split('\n')
    this.pending = (lines.pop() ?? '').slice(-65_536)
    return lines.map(l => l.slice(-65_536).replace(/\r$/, ''))
  }

  /** What waits without a newline, if anything, and clears it. */
  flush(): string | null {
    const rest = this.pending
    this.pending = ''
    return rest ? rest.replace(/\r$/, '') : null
  }
}

const parseJson = (line: string): unknown => {
  const t = line.trim()
  if (!t.startsWith('{')) return undefined
  try {
    return JSON.parse(t) as unknown
  } catch {
    return undefined
  }
}

/**
 * The `{"url": …}` line: its URL, and `preloaded` false only when the line
 * says `"preloaded": false` (the bridge's word that the Insight preload could
 * not be wired, the dashboard up all the same). Null for any other line.
 */
export function parseUrlLine(line: string): UrlLine | null {
  const doc = parseJson(line)
  if (typeof doc !== 'object' || doc === null) return null
  const { url, preloaded, labels_preloaded } = doc as { url?: unknown; preloaded?: unknown; labels_preloaded?: unknown }
  if (typeof url !== 'string' || !url) return null
  return { url, preloaded: preloaded !== false, ...(typeof labels_preloaded === 'boolean' ? { labels_preloaded } : {}) }
}

/** The message of the bridge's `{"error": {…}}` envelope on a line; null for any other line. */
function parseErrorLine(line: string): string | null {
  const doc = parseJson(line)
  if (typeof doc !== 'object' || doc === null || !('error' in doc)) return null
  const err = (doc as { error: unknown }).error
  if (typeof err !== 'object' || err === null) return null
  const message = (err as { message?: unknown }).message
  return typeof message === 'string' && message ? message : 'the bridge answered an error'
}

/** The port of `url`; 0 when it names none. */
export function portOf(url: string): number {
  return Number(/^[a-z]+:\/\/[^/]*:(\d+)(?:\/|$)/i.exec(url)?.[1] ?? 0)
}

type Server = { stream: ServerStream; url: string; identity: string; preloaded: boolean; labelsPreloaded?: boolean; cancelled: boolean; cancel: () => void }

/** The running children by kind; an entry exists from the URL line until the stream ends. */
const servers = new Map<Kind, Server>()
const starts = new Map<Kind, Promise<Started>>()
const startIdentities = new Map<Kind, string>()
const startTokens = new Map<Kind, object>()

/** Launch arguments plus an optional revision of a file loaded at startup. */
const identityOf = (argv: readonly string[], revision = ''): string => JSON.stringify([argv, revision])

/** Its URL, optionally restricted to the exact launch arguments; null when the source differs. */
export function running(kind: Kind, argv?: readonly string[], revision = ''): string | null {
  const server = servers.get(kind)
  return server && (argv === undefined || server.identity === identityOf(argv, revision)) ? server.url || null : null
}

/** Whether a dashboard process is running or still announcing its URL. */
export function hasServer(kind: Kind): boolean {
  return servers.has(kind) || starts.has(kind)
}

/** Explicit stop selections cannot close a different captured session's dashboard. */
export function serverSourceMatches(kind: Kind, path: string, captureSessionId?: string): boolean {
  const identity = startIdentities.get(kind) ?? servers.get(kind)?.identity
  if (!identity) return false
  try {
    const [argv] = JSON.parse(identity) as [string[]]
    return argvValue(argv, 'transcript') === path && argvValue(argv, 'session-id') === captureSessionId
  } catch { return false }
}

/** Ends `stream` (the engine kills the child); a stream already over or refusing is no error. */
async function end(stream: ServerStream): Promise<void> {
  try {
    await stream.return({ code: null, signal: null })
  } catch {
    // The child is gone either way.
  }
}

/**
 * Reads `stream` as the child of dashboard `kind`, in a loop that outlives
 * this call: resolves the URL line at the first `{"url": …}` stdout line,
 * the entry recorded in the map and in `dashboards` state; `{ error }` at
 * an error envelope, an exit or a failure before it, the child ended. A
 * stderr line before the URL is kept as the exit row's last line and
 * nothing more. Once started, the loop reads on until the stream ends (the
 * child exited, or `stopServer` returned the stream) and then clears the
 * entry, unless a later start already holds the kind.
 */
export async function startServerFrom($: EngineInterface, kind: Kind, stream: ServerStream, identity = ''): Promise<Started> {
  let settle: (s: Started) => void = () => {}
  const first = new Promise<Started>(resolve => {
    settle = resolve
  })
  let timer: Timer | null = null
  const finish = (result: Started): void => {
    timer?.cancel()
    if ('error' in result && servers.get(kind) === server) servers.delete(kind)
    settle(result)
  }
  const server: Server = { stream, url: '', identity, preloaded: true, cancelled: false, cancel: () => finish({ error: 'the dashboard start was cancelled' }) }
  servers.set(kind, server)
  const buffers: Record<ProcessSpawnChunk['stream'], LineBuffer> = { stdout: new LineBuffer(), stderr: new LineBuffer() }
  let last = ''
  timer = $.clock.after(START_TIMEOUT_MS, () => {
    server.cancelled = true
    finish({ error: `the dashboard did not print its URL within 120 seconds${last ? `: ${last}` : ''}` })
    if (server.url) void setDashboard($, kind, null)
    void end(stream)
  })

  /** A line before the URL: the URL starts it; an envelope fails it; anything else (stderr included) is context. */
  const before = async (name: ProcessSpawnChunk['stream'], line: string): Promise<'started' | 'failed' | null> => {
    if (server.cancelled) return 'failed'
    if (line.trim()) last = line
    if (name === 'stderr') return null
    const url = parseUrlLine(line)
    if (url !== null) {
      server.url = url.url
      server.preloaded = url.preloaded
      server.labelsPreloaded = url.labels_preloaded
      servers.set(kind, server)
      await setDashboard($, kind, { url: url.url, port: portOf(url.url) })
      if (server.cancelled) return 'failed'
      finish(url)
      return 'started'
    }
    const error = parseErrorLine(line)
    if (error !== null) {
      if (servers.get(kind) === server) servers.delete(kind)
      await end(stream)
      finish({ error })
      return 'failed'
    }
    return null
  }

  const loop = async (): Promise<void> => {
    let started = false
    try {
      for (;;) {
        let r: IteratorResult<ProcessSpawnChunk, ProcessSpawnResult>
        try {
          r = await stream.next()
        } catch (e) {
          finish({ error: `the dashboard could not start: ${e instanceof Error ? e.message : String(e)}` })
          await end(stream)
          return
        }
        if (r.done) {
          if (!started) {
            for (const name of ['stdout', 'stderr'] as const) {
              const rest = buffers[name].flush()
              if (rest !== null && (await before(name, rest)) !== null) return
            }
            const code = r.value.code ?? `signal ${r.value.signal ?? '?'}`
            finish({ error: `the dashboard exited with ${code} before printing its URL${last ? `: ${last}` : ''}` })
          }
          return
        }
        const { stream: name, text } = r.value
        if (started) continue
        const lines = buffers[name].push(text)
        for (const line of lines) {
          const outcome = await before(name, line)
          if (outcome === 'started') {
            started = true
            break
          }
          if (outcome === 'failed') {
            await end(stream)
            return
          }
        }
      }
    } finally {
      timer?.cancel()
      if (servers.get(kind) === server) {
        servers.delete(kind)
        if (server.url) await setDashboard($, kind, null)
      }
    }
  }
  void loop().catch(e => {
    finish({ error: `the dashboard could not start: ${e instanceof Error ? e.message : String(e)}` })
    void end(stream)
  })
  return first
}

/**
 * Starts dashboard `kind` as the child `argv` in the session's directory
 * (TrajViz's `.env` loading then sees the project's file) and answers its
 * URL line, or why it did not start. Matching launch arguments reuse the
 * process; changing the transcript, data root, labels, window or port
 * cancels the old process before starting its replacement.
 */
export async function startServer($: EngineInterface, kind: Kind, argv: string[], revision = '', generation?: number): Promise<Started> {
  if (generation !== undefined && generation !== publicationGeneration()) return { error: 'The selected session changed during analysis; run /obs again.' }
  const identity = identityOf(argv, revision)
  const live = servers.get(kind)
  if (live?.url && live.identity === identity) return { url: live.url, preloaded: live.preloaded, ...(live.labelsPreloaded !== undefined ? { labels_preloaded: live.labelsPreloaded } : {}) }
  const existing = starts.get(kind)
  if (existing && startIdentities.get(kind) === identity) return existing
  // Cancel a different source before reserving this start. Do not await yet:
  // the reservation must be visible to another request during state cleanup.
  const stopped = hasServer(kind) ? stopServer($, kind) : Promise.resolve()
  const token = {}
  startTokens.set(kind, token)
  const pending = (async () => {
    await stopped
    const cwd = await $.session.cwd()
    if (startTokens.get(kind) !== token) return { error: 'the dashboard start was cancelled' }
    if (generation !== undefined && generation !== publicationGeneration()) return { error: 'The selected session changed during analysis; run /obs again.' }
    return startServerFrom($, kind, $.process.spawn({ argv, cwd }), identity)
  })()
  starts.set(kind, pending)
  startIdentities.set(kind, identity)
  try {
    return await pending
  } finally {
    if (starts.get(kind) === pending) {
      starts.delete(kind)
      startIdentities.delete(kind)
    }
    if (startTokens.get(kind) === token) startTokens.delete(kind)
  }
}

/**
 * Ends dashboard `kind`: its entry and state cleared, then `return()` on its
 * stream, which kills the child. The `return()` is not awaited: it settles
 * only once the loop's pending pull does, which a silent child could hold
 * for a while, and the dashboard is gone for the pane and the next start as
 * soon as this resolves. Nothing when none runs.
 */
export async function stopServer($: EngineInterface, kind: Kind): Promise<void> {
  startTokens.delete(kind)
  starts.delete(kind)
  startIdentities.delete(kind)
  const server = servers.get(kind)
  if (!server) return
  servers.delete(kind)
  server.cancelled = true
  server.cancel()
  if (server.url) await setDashboard($, kind, null)
  void end(server.stream)
}

/** Ends every running dashboard (the session's end). */
export async function stopAll($: EngineInterface): Promise<void> {
  for (const kind of new Set([...servers.keys(), ...starts.keys()])) await stopServer($, kind)
}

/**
 * The module's load (`session.start`, which fires again at a reload: a
 * config change, source reload, a worker respawn). The engine killed the
 * previous environment's children with it and this map starts empty, but
 * `$.state` outlives the module, so `dashboards` would still name them:
 * whatever this map holds is ended and the state is made to say none runs.
 */
export async function resetServers($: EngineInterface): Promise<void> {
  await stopAll($)
  await resetDashboards($)
}
