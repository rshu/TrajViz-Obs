// Which transcript `/obs` analyzes: the one the classic hooks last saw, else
// the path Claude Code derives from the session id and working directory,
// else the bridge's scan; `--session <id|prefix|path>` goes to the bridge.
// Every helper taking `$` is a function declaration (validate follows `$`
// into one); state is reached only through the functions in state.ts.
import type { EngineInterface } from 'claude-code'
import type { SessionState } from '../../types'
import { BridgeError, runBridge } from './bridge'
import { valueArgv } from './args'
import { configDir, projectSlug } from './paths'
import { invalidatePublication, lockPublication, publicationGeneration } from './publication'
import { readFlags, readSession, stampRefresh, updateSession, writeLatest } from './state'

/** A transcript to analyze: its path and the session id it belongs to. */
export type Transcript = { path: string; sessionId: string; captureSessionId?: string }

/** The bridge's `resolve` document (`sessions.Resolved`). */
type Resolved = { path: string; session_id: string; project_dir?: string; kind?: 'file' | 'dir' }

/** How long the bridge's `resolve` may take. */
export const RESOLVE_TIMEOUT_MS = 120_000

/** The transcript Claude Code writes for `id` in `cwd`: `<configDir>/projects/<slug>/<id>.jsonl`. */
export async function derivedTranscriptPath($: EngineInterface, cwd: string, id: string): Promise<string> {
  return `${await configDir($)}/projects/${projectSlug(cwd)}/${id}.jsonl`
}

/** `resolve --cwd … --config-dir … <selector>` through the bridge. */
async function resolveViaBridge($: EngineInterface, selector: string[]): Promise<Transcript> {
  const argv = ['resolve', '--cwd', await $.session.cwd(), '--config-dir', await configDir($), ...selector]
  const doc = await runBridge<Resolved>($, argv, { timeoutMs: RESOLVE_TIMEOUT_MS })
  return { path: doc.path, sessionId: doc.session_id }
}

/**
 * The transcript to analyze. With `spec` (an id, a unique id prefix, a path
 * to a TrajViz-readable file or a session directory) the bridge resolves it
 * (`not_found` or `ambiguous` with its candidates as a BridgeError).
 * Otherwise the current session's: the latest `transcript_path` a classic
 * hook stored, else the derived path when `$.fs.exists` says so, else the
 * bridge's scan of every project folder for `<id>.jsonl`.
 */
export async function resolveTranscript($: EngineInterface, spec?: string, captureSessionId?: string): Promise<Transcript> {
  if (spec) return { ...await resolveViaBridge($, valueArgv('session', spec)), ...(captureSessionId === undefined ? {} : { captureSessionId }) }
  const known = await readSession($)
  if (known.transcriptPath) return { path: known.transcriptPath, sessionId: known.sessionId ?? (await $.session.id()) }
  const id = await $.session.id()
  const derived = await derivedTranscriptPath($, await $.session.cwd(), id)
  if (await $.fs.exists(derived)) return { path: derived, sessionId: id }
  return resolveViaBridge($, ['--session-id', id])
}

/**
 * Records the session the person named last with `--session` (null: the
 * current one), which the pane, the status line and the `obs` tool read.
 */
export async function selectSession($: EngineInterface, sel: SessionState['selected'], generation?: number): Promise<number> {
  const release = await lockPublication()
  try {
    if (generation !== undefined && generation !== publicationGeneration()) throw new BridgeError('session_changed', 'The selected session changed during analysis; run /obs again.')
    const previous = await readSession($)
    await updateSession($, s => ({ ...s, selected: sel }))
    if (previous.selected?.path !== sel?.path || previous.selected?.captureSessionId !== sel?.captureSessionId) await invalidateSession($)
    return publicationGeneration()
  } finally { release() }
}

/** Clears the previous session's live data and invalidates an in-flight bridge reply. */
async function invalidateSession($: EngineInterface): Promise<void> {
  invalidatePublication()
  await writeLatest($, null)
  await stampRefresh($, { lastRunAt: 0, lastDurationMs: 0, lastSize: 0, lastMtimeMs: 0 })
  if ((await readFlags($)).statusline) $.ui.status(undefined)
}

/**
 * What every classic hook does with its event: keeps the latest transcript
 * path and session id seen (a `/clear` starts a new file). An event with no
 * local transcript (`transcript_path` empty) changes nothing.
 */
export async function rememberTranscript($: EngineInterface, e: { readonly transcript_path: string; readonly session_id: string }): Promise<void> {
  if (!e.transcript_path) return
  const release = await lockPublication()
  try {
    const previous = await readSession($)
    await updateSession($, s => ({ ...s, transcriptPath: e.transcript_path, sessionId: e.session_id }))
    if (previous.transcriptPath !== e.transcript_path && !previous.selected) await invalidateSession($)
  } finally { release() }
}
