// Live refresh: after each main-loop turn, one more snapshot for the open
// pane (`watch`) or the status line (`statusline`), never concurrently,
// debounced on timestamps in `$.state` and one trailing `$.clock.after`
// (never `$.clock.sleep`), only when the transcript's size or mtime changed,
// and never above 32 MB of transcript. Every helper taking `$` is a function
// declaration (validate follows `$` into one); state is reached through
// state.ts.
import type { EngineInterface, PluginOptions, SessionUsage, Timer } from 'claude-code'
import type { EmptySnapshot, RefreshState, Snapshot } from '../../types'
import { BridgeError } from './bridge'
import { fmtInt, fmtPct } from './format'
import { lockPublication, publicationGeneration } from './publication'
import { resolveTranscript } from './session'
import { BUSY_STALE_MS, captureSnapshot, isEmpty } from './snapshot'
import { readFlags, readRefresh, readSession, stampRefresh } from './state'
import { sessionUsage } from '../render/card'
import { PANE_ID } from '../render/pane'

/** The least wait between two watch runs. */
export const MIN_DEBOUNCE_MS = 3_000

/** The wait is also this many times the last run's wall time. */
export const DEBOUNCE_FACTOR = 10

/** Above this many bytes of transcript the watch degrades to manual. */
export const TOO_LARGE_BYTES = 32 * 1024 * 1024

/** The status line (and the one toast) of a session too large to watch. */
export const LARGE_SESSION_TEXT = 'obs · large session · /obs to refresh'

export type RefreshReason = 'busy' | 'debounce' | 'unchanged' | 'too-large'

/**
 * Whether a watch run should start now, and why not; on `debounce`, how
 * long until it may (`waitMs`), for the trailing refresh.
 */
export type RefreshDecision = { run: boolean; reason?: RefreshReason; waitMs?: number }

/**
 * Whether the watch runs at `now`: not while a run holds the lock (a hold
 * older than `BUSY_STALE_MS` is stale and does not count), not above
 * `TOO_LARGE_BYTES` of transcript, not within `max(3 s, 10 × the last
 * run's wall time)` of the last run (then `waitMs` says how much of that
 * is left), and not while the transcript's size and mtime are what the
 * last run saw (before any run there is no last).
 */
export function shouldRefresh(now: number, refresh: RefreshState, stat: { size: number; mtimeMs: number }, transcriptBytes: number): RefreshDecision {
  if (refresh.busy && now - refresh.busySince < BUSY_STALE_MS) return { run: false, reason: 'busy' }
  if (transcriptBytes > TOO_LARGE_BYTES) return { run: false, reason: 'too-large' }
  if (refresh.lastRunAt === 0) return { run: true }
  const debounce = Math.max(MIN_DEBOUNCE_MS, DEBOUNCE_FACTOR * refresh.lastDurationMs)
  const elapsed = now - refresh.lastRunAt
  if (elapsed < debounce) return { run: false, reason: 'debounce', waitMs: debounce - elapsed }
  if (stat.size === refresh.lastSize && stat.mtimeMs === refresh.lastMtimeMs) return { run: false, reason: 'unchanged' }
  return { run: true }
}

/**
 * The pinned summary: `obs · ctx 24% · 49 tools 100% · 2 issues`, the
 * context figure the engine's (`$.session.usage()`), else TrajViz's.
 */
export function statusLine(snap: Snapshot, usage: SessionUsage | null): string {
  const ctx = usage?.context.percent ?? snap.context.current_pct
  const issues = snap.counts.issues ?? snap.issues.length
  return `obs · ctx ${fmtPct(ctx)} · ${fmtInt(snap.tools.calls)} tools ${fmtPct(snap.tools.success_rate)} · ${issues} issue${issues === 1 ? '' : 's'}`
}

/** Draws the status line from `snap` (nothing for no snapshot or an empty one). */
export async function drawStatusLine($: EngineInterface, snap: Snapshot | EmptySnapshot | null, generation = publicationGeneration()): Promise<void> {
  const release = await lockPublication()
  try {
    if (generation !== publicationGeneration()) return
    if (snap === null || isEmpty(snap)) { $.ui.status(undefined); return }
    const selected = (await readSession($)).selected
    const usage = selected ? null : await sessionUsage($)
    if ((await readFlags($)).statusline) $.ui.status(statusLine(snap, usage))
  } finally { release() }
}

/** Whether the pane is open (placed or not), by the engine's own record. */
async function isPaneOpen($: EngineInterface): Promise<boolean> {
  return (await $.ui.panes()).some(p => p.id === PANE_ID)
}

/**
 * Whether `statusline` is on, read afresh: a run can last long enough for
 * `/obs statusline off` to clear the line meanwhile, and a draw from the
 * flag as it was before the run would pin the line again with nothing to
 * clear it (later turns skip the watch with the line off).
 */
async function statuslineOn($: EngineInterface): Promise<boolean> {
  return (await readFlags($)).statusline
}

/** Whether the large-session line was said already (per load: a reload says it again, harmlessly). */
let saidLarge = false

/** Says the session is too large to watch: on the status line when it is on (as it stands now), and as a toast once. */
async function sayLarge($: EngineInterface): Promise<void> {
  if (await statuslineOn($)) $.ui.status(LARGE_SESSION_TEXT)
  if (saidLarge) return
  saidLarge = true
  $.ui.toast(LARGE_SESSION_TEXT)
}

/** The trailing refresh a turn inside the debounce scheduled; one at most, cleared as it fires. */
let trailing: Timer | null = null

/**
 * Schedules the trailing refresh `waitMs` from now, when a turn ended inside
 * the debounce window and nothing else would show it: `onTurnComplete`
 * again, decided afresh (flags, stat, `shouldRefresh`) when it fires. One
 * is enough while it is pending; a module reload cancels it.
 */
function scheduleTrailing($: EngineInterface, options: PluginOptions, waitMs: number): void {
  if (trailing !== null) return
  trailing = $.clock.after(waitMs, () => {
    trailing = null
    void onTurnComplete($, options)
  })
}

/**
 * The watch's work after a main-loop turn (or when the trailing refresh
 * fires): when `statusline` is on, or `watch` is and the pane is open,
 * stats the selected session's transcript (else the current one's) and,
 * when `shouldRefresh` says so, takes one snapshot (`--session` selection
 * kept), stores it as the latest (which redraws the pane), stamps the run
 * and draws the status line when it is still on. A turn inside the
 * debounce schedules the trailing refresh instead. A session too large is
 * said once. The run's errors go to the debug log; a `busy` refusal
 * (another run holds the lock) changes nothing.
 */
async function refreshAfterTurn($: EngineInterface, options: PluginOptions): Promise<void> {
  const flags = await readFlags($)
  if (!flags.statusline && !(flags.watch && (await isPaneOpen($)))) return
  const beforeStat = publicationGeneration()
  const selected = (await readSession($)).selected
  const path = selected?.path ?? (await resolveTranscript($)).path
  const stat = await $.fs.stat(path)
  if (publicationGeneration() !== beforeStat) return
  const startedAt = await $.clock.now()
  const decision = shouldRefresh(startedAt, await readRefresh($), stat, stat.size)
  if (decision.reason === 'too-large') return sayLarge($)
  if (decision.reason === 'debounce') scheduleTrailing($, options, decision.waitMs ?? MIN_DEBOUNCE_MS)
  if (!decision.run) return
  let snap: Snapshot | EmptySnapshot | null = null
  let generation = beforeStat
  try {
    ;({ snap, generation } = await captureSnapshot($, options, selected?.path, beforeStat))
  } catch (e) {
    if (e instanceof BridgeError && (e.code === 'busy' || e.code === 'session_changed')) return
    $.ui.log(`obs watch: ${e instanceof Error ? e.message : String(e)}`, { to: 'debug' })
  }
  const release = await lockPublication()
  try {
    if (publicationGeneration() !== generation) return
    await stampRefresh($, { lastRunAt: startedAt, lastDurationMs: (await $.clock.now()) - startedAt, lastSize: stat.size, lastMtimeMs: stat.mtimeMs })
  } finally { release() }
  if (snap === null) return
  if (await statuslineOn($)) await drawStatusLine($, snap, generation)
}

/**
 * What the `turn.complete` hook starts, detached, after `next(e)` on the
 * main loop, and what the trailing refresh runs again: `refreshAfterTurn`,
 * its failures (no transcript, a stat that fails) logged to the debug log
 * and never raised.
 */
export async function onTurnComplete($: EngineInterface, options: PluginOptions): Promise<void> {
  try {
    await refreshAfterTurn($, options)
  } catch (e) {
    $.ui.log(`obs watch: ${e instanceof Error ? e.message : String(e)}`, { to: 'debug' })
  }
}
