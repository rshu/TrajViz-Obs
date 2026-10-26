import type { CommandRunResult, EngineInterface } from 'claude-code'
import { BridgeError, launcherArgv } from '../lib/bridge'
import { openTarget, type OpenResult } from '../lib/open'
import { configDir, projectSlug } from '../lib/paths'
import { publicationGeneration } from '../lib/publication'
import { type Kind, hasServer, portOf, running, startServer, stopServer } from '../lib/servers'
import { ensureReady, sessionArgv } from '../lib/snapshot'
import type { CommandContext } from './index'
import { errorRow, sessionSpec } from './overview'

const MAX_PORT = 65_535
let unverifiedSidecar = 0

/** The subcommand of each dashboard kind, and the name its rows use. */
const KINDS: Record<'dashboard' | 'converge', { kind: Kind; name: string }> = {
  dashboard: { kind: 'insight', name: 'Insight dashboard' },
  converge: { kind: 'converge', name: 'Converge dashboard' },
}

/** A delayed dashboard request must not replace a newer session selection. */
function checkGeneration(generation?: number): void {
  if (generation !== undefined && generation !== publicationGeneration()) throw new BridgeError('session_changed', 'The selected session changed during analysis; run /obs again.')
}

/** The usage row of `dashboard` or `converge`. */
function usage(sub: 'dashboard' | 'converge'): string {
  const flags = sub === 'dashboard' ? '[--port N]' : '[--data-root DIR] [--port N]'
  return `Usage: /obs ${sub} ${flags} | ${sub} stop  (N from 1 to ${MAX_PORT})`
}

/** `--port N` as a number when given and valid; undefined when absent; null when bad. */
function portFlag(ctx: CommandContext): number | undefined | null {
  const p = ctx.parsed.flags['port']
  if (p === undefined) return undefined
  if (typeof p !== 'string' || !/^\d+$/.test(p)) return null
  const n = Number(p)
  return n >= 1 && n <= MAX_PORT ? n : null
}

/** The rest of a dashboard's row after its URL: whether the browser has it, then how to stop it. */
function openedTail(sub: 'dashboard' | 'converge', url: string, opened: OpenResult): string {
  const stop = `/obs ${sub} stop ends it.`
  switch (opened) {
    case 'opened':
      return `; opened in the browser. ${stop}`
    case 'skipped-ssh': {
      const port = portOf(url) || 'PORT'
      return `; not opened over SSH: forward the port (ssh -L ${port}:127.0.0.1:${port} <host>) and open it on your machine. ${stop}`
    }
    case 'disabled':
      return `; not opened (openBrowser is off in /config). ${stop}`
    case 'failed':
      return `; no browser could be opened here, so open it yourself. ${stop}`
  }
}

/**
 * Starts the dashboard of `sub` as the bridge child `argv` (or names the one
 * already running), opens its URL, and answers the row: `<name> at <url>
 * <what(preloaded)>; <opened>. /obs <sub> stop ends it.`, or `obs <sub>:
 * <error>` when the child did not start (the bridge's envelope message, say
 * a busy port's, or its exit).
 */
async function serve($: EngineInterface, ctx: CommandContext, sub: 'dashboard' | 'converge', argv: string[], what: (preloaded: boolean) => string, revision = '', generation?: number): Promise<string> {
  checkGeneration(generation)
  const { kind, name } = KINDS[sub]
  const launch = await launcherArgv($, argv)
  checkGeneration(generation)
  const sourceRevision = generation === undefined ? revision : JSON.stringify([revision, generation])
  const live = running(kind, launch, sourceRevision)
  const started = await startServer($, kind, launch, sourceRevision, generation)
  checkGeneration(generation)
  if ('error' in started) return `obs ${sub}: ${started.error}`
  const labelsAt = argv.indexOf('--labels')
  const labels = labelsAt >= 0 ? argv[labelsAt + 1] : undefined
  const labelNote = started.labels_preloaded === false && labels ? `; labels were not preloaded: upload ${labels} in the Labels tab` : ''
  const title = live !== null ? `${name} already running at ${started.url}` : `${name} at ${started.url} ${what(started.preloaded)}`
  return `${title}${labelNote}${openedTail(sub, started.url, await openTarget($, ctx.options, started.url))}`
}

/**
 * `dashboard [--port N]`: the `app` extra, the bridge's `dashboard
 * --transcript … [--port N]` with the session (or `--session`'s) loaded.
 * When the bridge could not wire the preload (`"preloaded": false`: a
 * TrajViz release changed the upload row) the dashboard is up but empty,
 * and the row names the `bundle` ZIP to drop into it.
 */
async function dashboard($: EngineInterface, ctx: CommandContext, port: number | undefined): Promise<string> {
  const spec = sessionSpec(ctx)
  try {
    await ensureReady($, ctx.options, 'app')
    const { transcript, argv: session, generation } = await sessionArgv($, spec)
    const argv = ['dashboard', '--transcript', transcript.path]
    const windowAt = session.indexOf('--window')
    if (windowAt >= 0) argv.push('--window', session[windowAt + 1]!)
    const artifactsAt = session.indexOf('--artifacts')
    const labels = artifactsAt >= 0 ? `${session[artifactsAt + 1]}/labels.json` : undefined
    let revision = ''
    if (labels && await $.fs.exists(labels)) {
      argv.push('--labels', labels)
      try {
        const stat = await $.fs.stat(labels)
        revision = `${stat.size}:${stat.mtimeMs}`
      } catch {
        // Without file metadata, reopen instead of retaining stale labels.
        revision = `unverified:${++unverifiedSidecar}`
      }
    }
    if (port !== undefined) argv.push('--port', String(port))
    const what = (preloaded: boolean): string =>
      preloaded
        ? `with session ${transcript.sessionId} loaded`
        : `without session ${transcript.sessionId} loaded (the preload could not be wired): drop the ZIP /obs bundle writes into it`
    return await serve($, ctx, 'dashboard', argv, what, revision, generation)
  } catch (e) {
    return errorRow(e, spec)
  }
}

/** `converge [--data-root DIR] [--port N]`: the `app` extra, the bridge's `converge --data-root DIR [--port N]`; DIR defaults to this project's sessions folder. */
async function converge($: EngineInterface, ctx: CommandContext, port: number | undefined): Promise<string> {
  const flag = ctx.parsed.flags['data-root']
  try {
    await ensureReady($, ctx.options, 'app')
    const root = typeof flag === 'string' && flag ? flag : `${await configDir($)}/projects/${projectSlug(await $.session.cwd())}`
    const argv = ['converge', '--data-root', root]
    if (port !== undefined) argv.push('--port', String(port))
    return await serve($, ctx, 'converge', argv, () => `over ${root}`)
  } catch (e) {
    return errorRow(e)
  }
}

/** `/obs dashboard [--port N] | dashboard stop` and `/obs converge [--data-root DIR] [--port N] | converge stop`. */
export async function browser($: EngineInterface, ctx: CommandContext): Promise<CommandRunResult> {
  const sub = ctx.parsed.sub === 'converge' ? 'converge' : 'dashboard'
  const { kind, name } = KINDS[sub]
  const [action, ...rest] = ctx.parsed.positional
  if (action === 'stop' && rest.length === 0) {
    if (!hasServer(kind)) return { text: `No ${name} is running.` }
    await stopServer($, kind)
    return { text: `${name} stopped.` }
  }
  const port = portFlag(ctx)
  const root = ctx.parsed.flags['data-root']
  if (action !== undefined || port === null || root === true || (typeof root === 'string' && !root.trim())) return { text: usage(sub) }
  return { text: sub === 'dashboard' ? await dashboard($, ctx, port) : await converge($, ctx, port) }
}
