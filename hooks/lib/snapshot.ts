// Snapshots: taking one through the bridge and keeping the last five in
// `$.state` (a family keyed by snapshot id, `latest` for the pane and the
// status line). The provenance line of a card's text carries the id, so a
// card reads its own member, never the latest. Every helper taking `$` is a
// function declaration (validate follows `$` into one); state is reached
// only through the functions in state.ts.
import type { EngineInterface, PluginOptions } from 'claude-code'
import type { EmptySnapshot, Snapshot } from '../../types'
import { BridgeError, runBridge } from './bridge'
import { clip, fmtInt } from './format'
import { artifactsDir } from './paths'
import { ensureEnv, ensureExtra, needsSetup, readEnvState, type Extra } from './python'
import { RESOLVE_TIMEOUT_MS, resolveTranscript, selectSession, type Transcript } from './session'
import { claimRefresh, readFlags, readSnapshotIds, releaseRefresh, writeLatest, writeSnapshotIds, writeSnapshotMember } from './state'

/** How many snapshots are kept. */
export const KEEP = 5

/** The row for a second `/obs` while one is computing (the `busy` BridgeError's message). */
export const BUSY_TEXT = 'An analysis is already running; try again in a moment.'

const SNAPSHOT_TIMEOUT_MS = 120_000

/** The first-run setup's allowance in the lock: about 10 s, minutes on a slow network; `$.process.spawn` bounds it no further. */
const SETUP_HOLD_MS = 120_000

/**
 * How long a `refresh.busy` may be held before it counts as abandoned: the
 * longest `takeSnapshot` can legitimately take, setup then the bridge's
 * `resolve` and `snapshot` at their timeouts. A hold past it was left by a
 * dispatch cut before its `finally` (Esc, a reload) and is reclaimed.
 */
export const BUSY_STALE_MS = SETUP_HOLD_MS + RESOLVE_TIMEOUT_MS + SNAPSHOT_TIMEOUT_MS

const PREFIX = 'obs snapshot '
const ID_LINE = /^obs snapshot (\d+) · /

/** Whether the document is the empty state. */
export function isEmpty(snap: Snapshot | EmptySnapshot): snap is EmptySnapshot {
  return snap.empty === true
}

/** The next id: one past the largest of `ids` (`"1"` when none). */
export function mintId(ids: readonly string[]): string {
  const largest = ids.reduce((max, id) => Math.max(max, Number.parseInt(id, 10) || 0), 0)
  return String(largest + 1)
}

const two = (n: number): string => String(n).padStart(2, '0')

/**
 * The first line of a card's text: `obs snapshot <id> · HH:MM:SS · <n>
 * assistant steps · window <w>` (`at` in local time; `?` for an unknown
 * window), with TrajViz's load warnings after it when there are any.
 */
export function provenanceLine(id: string, snap: Snapshot | EmptySnapshot, at: Date): string {
  const time = `${two(at.getHours())}:${two(at.getMinutes())}:${two(at.getSeconds())}`
  const steps = isEmpty(snap) ? 0 : snap.counts.assistant_steps
  const window = isEmpty(snap) ? null : snap.context.window
  const warnings = isEmpty(snap) ? [] : snap.trajviz.warnings
  let line = `${PREFIX}${id} · ${time} · ${fmtInt(steps)} assistant steps · window ${window === null ? '?' : fmtInt(window)}`
  if (warnings.length) line += ` · warnings: ${warnings.join('; ')}`
  return line
}

/** The snapshot id a card's text opens with, null when the text is not a card's (an error row). */
export function parseSnapshotId(text: string): string | null {
  const m = ID_LINE.exec(text)
  return m?.[1] ?? null
}

/**
 * Keeps `snap` under a fresh id: writes the family member, appends the id,
 * prunes to the last five (older members cleared to `null`) and sets
 * `latest`. Answers the id.
 */
export async function storeSnapshot($: EngineInterface, snap: Snapshot | EmptySnapshot): Promise<string> {
  const ids = await readSnapshotIds($)
  const id = mintId(ids)
  await writeSnapshotMember($, id, snap)
  const all = [...ids, id]
  await writeSnapshotIds($, all.slice(-KEEP))
  for (const old of all.slice(0, -KEEP)) await writeSnapshotMember($, old, null)
  await writeLatest($, snap)
  return id
}

/** The engine's context window, null where the session has none to report. */
async function contextWindow($: EngineInterface): Promise<number | null> {
  try {
    const w = (await $.session.usage()).context.window
    return Number.isFinite(w) && w > 0 ? w : null
  } catch {
    return null
  }
}

/** The toast of the first run's setup (the spec's "setup line"). */
export const FIRST_RUN_TOAST = 'obs: setting up TrajViz for the first time; downloading packages'

/** What `setUp` has to do: build the environment, install `extra` into it, or nothing. */
async function pending($: EngineInterface, options: PluginOptions, extra?: Extra): Promise<'build' | 'extra' | null> {
  const state = await readEnvState($)
  if (state === null || needsSetup(state, options)) return 'build'
  return extra !== undefined && !state.extras.includes(extra) ? 'extra' : null
}

/** What each extra brings, for the question and the rows. */
const EXTRA_WHAT: Record<Extra, string> = { charts: 'the HTML report', app: 'the dashboards' }

/** What an extra's install costs, for the question and the announcing row. */
const EXTRA_COST = 'downloads additional packages'

/** The transcript row announcing the install of `extra`, before it starts. */
export function installingText(extra: Extra): string {
  return `obs: installing TrajViz's ${extra} extra (${EXTRA_WHAT[extra]}, ${EXTRA_COST})`
}

/**
 * The environment, built on the first run, and `extra` (`charts` for the
 * report, `app` for the dashboards) installed when it is not yet. The first
 * run is announced by its toast (the spec's setup line); an extra's install
 * by a row of the transcript before it starts (`$.ui.log`, which the person
 * sees in an interactive session and a headless host receives as `ui_log`).
 * Each progress line goes to the status line, which is cleared at the end.
 * The caller holds `refresh.busy` meanwhile (`ensureEnv` is not serialised,
 * and a venv must not be rebuilt under a running bridge).
 */
async function setUp($: EngineInterface, options: PluginOptions, extra?: Extra): Promise<void> {
  const work = await pending($, options, extra)
  if (work === null) return
  if (work === 'build') $.ui.toast(FIRST_RUN_TOAST)
  else if (extra !== undefined) $.ui.log(installingText(extra))
  const report = (line: string) => $.ui.status(`obs setup: ${clip(line, 80)}`)
  try {
    if (extra !== undefined) await ensureExtra($, options, extra, report)
    else await ensureEnv($, options, report)
  } finally {
    $.ui.status(undefined)
  }
}

/** The ask's option that installs; the other, a dismissal or free text is no. */
const INSTALL = 'Install'

/**
 * Whether `extra` may be installed: asked of the person when the session is
 * interactive (`$.ui.ask`; a dismissed ask, which rejects, counts as no), a
 * headless session installs without asking. The spec's confirmation for the
 * `charts` and `app` installs; the first run's own build is never asked.
 */
async function confirmExtra($: EngineInterface, extra: Extra): Promise<boolean> {
  if (!(await readFlags($)).isInteractive) return true
  try {
    return (await $.ui.ask(`Install TrajViz's ${extra} extra (${EXTRA_WHAT[extra]}, ${EXTRA_COST})?`, [INSTALL, 'Not now'])) === INSTALL
  } catch {
    return false
  }
}

/** The row when the install of `extra` was declined. */
export function declinedText(extra: Extra): string {
  return `TrajViz's ${extra} extra is not installed (${EXTRA_WHAT[extra]} need it); /obs setup --extras ${extra} installs it.`
}

/**
 * The environment (and `extra`, when given) ready for a bridge run that is
 * not a snapshot: the views, the exports and the listings. An `extra` not
 * yet installed is confirmed first (`confirmExtra`, before the lock, so a
 * dialog left open holds nothing up); a no is `BridgeError('declined')`.
 * Held by `refresh.busy` while it builds or installs, as `takeSnapshot`
 * holds it; nothing is claimed when there is nothing to do. A second caller
 * meanwhile gets `BridgeError('busy')`, as for a snapshot.
 */
export async function ensureReady($: EngineInterface, options: PluginOptions, extra?: Extra): Promise<void> {
  const work = await pending($, options, extra)
  if (work === null) return
  if (work === 'extra' && extra !== undefined && !(await confirmExtra($, extra))) throw new BridgeError('declined', declinedText(extra))
  const claim = await claimRefresh($, BUSY_STALE_MS)
  if (claim === null) throw new BridgeError('busy', BUSY_TEXT)
  try {
    await setUp($, options, extra)
  } finally {
    await releaseRefresh($, claim)
  }
}

/**
 * What every session-bound bridge subcommand gets after its name: the
 * transcript resolved for the current session or for `spec` (`--session`,
 * recorded as the selection; the current session clears it), the engine's
 * context window when it reports one, the artifacts folder, and `--kind
 * current|other`.
 */
export async function sessionArgv($: EngineInterface, spec?: string): Promise<{ transcript: Transcript; argv: string[] }> {
  const transcript = await resolveTranscript($, spec)
  await selectSession($, spec ? { path: transcript.path, id: transcript.sessionId, label: transcript.sessionId } : null)
  const argv = ['--transcript', transcript.path]
  // The current engine cannot report another session's context window.
  const window = spec ? null : await contextWindow($)
  if (window !== null) argv.push('--window', String(window))
  argv.push('--artifacts', await artifactsDir($, transcript.sessionId), '--kind', spec ? 'other' : 'current')
  return { transcript, argv }
}

/**
 * One `snapshot` through the bridge for the current session, or for `spec`
 * (`--session`): builds the environment when there is none, then the
 * session arguments of `sessionArgv`. Held by `refresh.busy` from before
 * the setup until the bridge answers: a second caller gets
 * `BridgeError('busy')` and starts nothing, unless the hold is older than
 * `BUSY_STALE_MS` (a cut run's, never released) and is taken over.
 */
export async function takeSnapshot($: EngineInterface, options: PluginOptions, spec?: string): Promise<Snapshot | EmptySnapshot> {
  const claim = await claimRefresh($, BUSY_STALE_MS)
  if (claim === null) throw new BridgeError('busy', BUSY_TEXT)
  try {
    await setUp($, options)
    const { argv } = await sessionArgv($, spec)
    const snap = await runBridge<Snapshot | EmptySnapshot>($, ['snapshot', ...argv], { timeoutMs: SNAPSHOT_TIMEOUT_MS })
    if (!spec && !isEmpty(snap)) {
      try { snap.engine_usage = await $.session.usage() } catch { /* The host may not expose usage. */ }
    }
    return snap
  } finally {
    await releaseRefresh($, claim)
  }
}
