// Running the Python bridge: `<venv python> -I <plugin root>/bridge/
// trajviz_obs_cli.py <subcommand> …` in the session's working directory,
// one JSON document back on stdout, the bridge's error envelope raised as a
// BridgeError. `runBridge` is `$.process.run` under a deadline; `spawnBridge`
// is `$.process.spawn` with none, for a subcommand whose length is the
// person's choice (`label`: one request per step). Every helper taking `$`
// is a function declaration (validate follows `$` into one).
import type { EngineInterface, ProcessRunResult } from 'claude-code'
import { isProcessWithheld, readEnvState } from './python'

/** The launcher, relative to the plugin root. */
export const LAUNCHER = 'bridge/trajviz_obs_cli.py'

/** The row when there is no managed environment yet (the `no_env` BridgeError's message). */
export const NO_ENV_TEXT = 'TrajViz is not set up yet: run /obs setup, or /obs, which sets it up first.'
export const MAX_BRIDGE_CHARS = 4 * 1024 * 1024
const STDERR_TAIL_CHARS = 64 * 1024

/**
 * A bridge call that did not answer with a document. `code` is the envelope's
 * (`load_error`, `not_found`, `ambiguous`, `missing_extra`, `bad_option`,
 * `stale_prompts`, `internal`, …) or the runner's own: `no_env` (no managed
 * environment yet), `run_failed` (the child could not start or timed out),
 * `truncated` (stdout cut at the host's 4 MiB), `exit` (a non-zero exit, or
 * a signal, with no envelope, such as argparse's 2), `bad_output` (exit 0
 * without JSON).
 * `message` is the bridge's text verbatim; `extra` carries the envelope's
 * other fields (`path`, `candidates`, `extra`, `options`, …).
 */
export class BridgeError extends Error {
  readonly code: string
  readonly extra: Record<string, unknown>
  constructor(code: string, message: string, extra: Record<string, unknown> = {}) {
    super(message)
    this.name = 'BridgeError'
    this.code = code
    this.extra = extra
  }
}

const lastLine = (text: string): string => {
  const lines = text.slice(-4096).split('\n').map(l => l.trimEnd()).filter(Boolean)
  return lines[lines.length - 1] ?? ''
}

const parseJson = (text: string): unknown => {
  const t = text.trim()
  if (!t) return undefined
  try {
    return JSON.parse(t) as unknown
  } catch {
    return undefined
  }
}

/** The envelope's `error` object when the document is one, else null. */
function envelope(doc: unknown): Record<string, unknown> | null {
  if (typeof doc !== 'object' || doc === null || !('error' in doc)) return null
  const err = (doc as { error: unknown }).error
  return typeof err === 'object' && err !== null ? (err as Record<string, unknown>) : null
}

/**
 * The command line of the bridge subcommand `argv`: the managed environment's
 * interpreter (`state.json`; `BridgeError('no_env')` when there is none),
 * `-I`, the launcher, then `argv`.
 */
export async function launcherArgv($: EngineInterface, argv: readonly string[]): Promise<string[]> {
  const env = await readEnvState($)
  if (!env) throw new BridgeError('no_env', NO_ENV_TEXT)
  return [env.python, '-I', `${$.plugin.root}/${LAUNCHER}`, ...argv]
}

/** How a bridge child ended and what it wrote, whichever way it was started. */
type Ended = { stdout: string; stderr: string; exitCode: number | null; signal?: string | null }

/** The `run_failed` error for a child that could not start (or, run, timed out); a withheld call is rethrown. */
function couldNotRun(name: string, e: unknown): never {
  if (isProcessWithheld(e)) throw e
  throw new BridgeError('run_failed', `obs ${name}: the bridge could not run: ${e instanceof Error ? e.message : String(e)}`)
}

/**
 * The subcommand `name`'s document from what its child wrote: the error
 * envelope as a BridgeError, else `exit` on a non-zero exit (or a signal)
 * and `bad_output` on exit 0 without JSON.
 */
function decode<T>(name: string, r: Ended): T {
  const doc = parseJson(r.stdout)
  const err = envelope(doc)
  if (err) {
    const { code, message, ...extra } = err
    throw new BridgeError(typeof code === 'string' ? code : 'error', typeof message === 'string' ? message : lastLine(r.stderr), extra)
  }
  const detail = lastLine(r.stderr)
  if (r.exitCode !== 0) {
    const how = r.exitCode === null ? `was ended by ${r.signal ?? 'a signal'}` : `exited with ${r.exitCode}`
    throw new BridgeError('exit', `obs ${name}: the bridge ${how}${detail ? `: ${detail}` : ''}`)
  }
  if (typeof doc !== 'object' || doc === null || Array.isArray(doc)) {
    throw new BridgeError('bad_output', `obs ${name}: the bridge printed no JSON document${detail ? `: ${detail}` : ''}`)
  }
  return doc as T
}

/**
 * Runs the bridge subcommand `argv` and answers its document. The
 * interpreter is the managed environment's (`state.json`; `no_env` when there
 * is none), the working directory the session's (so TrajViz's `.env` loading
 * sees the project's file). The caller sets `timeoutMs` per subcommand
 * (120 s for snapshot, resolve, view, sessions, brief, doctor; 600 s for the
 * exports and comparisons).
 */
export async function runBridge<T>($: EngineInterface, argv: string[], opts: { timeoutMs: number }): Promise<T> {
  const command = await launcherArgv($, argv)
  const name = argv[0] ?? 'bridge'
  let r: ProcessRunResult
  try {
    r = await $.process.run(command, {
      cwd: await $.session.cwd(),
      timeoutMs: opts.timeoutMs,
    })
  } catch (e) {
    couldNotRun(name, e)
  }
  if (r.isStdoutTruncated) {
    throw new BridgeError('truncated', `obs ${name}: the bridge's output exceeded 4 MiB and was cut; run it with a smaller --limit or as an export.`)
  }
  return decode<T>(name, r)
}

/**
 * Runs the bridge subcommand `argv` through `$.process.spawn`, with no
 * deadline, and answers its document once the child ends: for a subcommand
 * as long as the person asked for (`label` in env mode makes one request per
 * step, and a confirmed run may take longer than `$.process.run` allows).
 * Stdout is bounded as for process.run, stderr retains only its tail. An
 * aborted or oversized run closes the stream, which ends the child.
 */
export async function spawnBridge<T>($: EngineInterface, argv: string[]): Promise<T> {
  const command = await launcherArgv($, argv)
  const name = argv[0] ?? 'bridge'
  const text = { stdout: '', stderr: '' }
  let ended: { code: number | null; signal: string | null }
  try {
    const child = $.process.spawn({ argv: command, cwd: await $.session.cwd() })
    let done = false
    try {
      for (;;) {
        const r = await child.next()
        if (r.done) {
          ended = r.value
          done = true
          break
        }
        const { stream, text: chunk } = r.value
        if (stream === 'stdout') {
          if (text.stdout.length + chunk.length > MAX_BRIDGE_CHARS) throw new BridgeError('truncated', `obs ${name}: the bridge's output exceeded 4 MiB; run it with fewer --max-steps.`)
          text.stdout += chunk
        } else text.stderr = (text.stderr + chunk).slice(-STDERR_TAIL_CHARS)
      }
    } finally {
      if (!done) {
        try { await child.return({ code: null, signal: null }) } catch { /* Preserve the original failure. */ }
      }
    }
  } catch (e) {
    if (e instanceof BridgeError) throw e
    couldNotRun(name, e)
  }
  return decode<T>(name, { ...text, exitCode: ended.code, signal: ended.signal })
}
