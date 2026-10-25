// The mod's $.state values, one atom per key of the contract in
// types/index.d.ts (`PluginState['trajviz-obs']`), and the functions that
// read and write them. `claude plugin validate` accepts the state library's
// `read`/`update` only on an atom declared as a const of the same file, so
// no other file touches an atom directly: each imports a function declared
// here (validate follows `$` across an import into a function declaration)
// and the atoms are exported for their `ref` and `initial` alone. Snapshots
// are a family keyed by snapshot id, whose members `snapshotRef` names.
import { atom, read, update } from 'claude-code'
import type { EngineInterface, PluginOptions } from 'claude-code'
import type { Dashboards, EmptySnapshot, Flags, RefreshState, SessionState, Snapshot } from '../../types'

/** The newest snapshot: what the pane and the status line draw. */
export const latest = atom({ plugin: 'trajviz-obs', key: 'latest' } as const, null)

/** The ids of the snapshots kept (the last five), oldest first. */
export const snapshotIds = atom({ plugin: 'trajviz-obs', key: 'snapshotIds' } as const, [])

/** The current transcript as the classic hooks saw it, and the session `--session` selected. */
export const session = atom({ plugin: 'trajviz-obs', key: 'session' } as const, {
  transcriptPath: null,
  sessionId: null,
  selected: null,
})

/** Watch and statusline as configured, whether a person is at the prompt, and whether the environment is built. */
export const flags = atom({ plugin: 'trajviz-obs', key: 'flags' } as const, {
  watch: true,
  statusline: false,
  isInteractive: false,
  envReady: false,
})

/** The running dashboards, by their URL and port. */
export const dashboards = atom({ plugin: 'trajviz-obs', key: 'dashboards' } as const, { insight: null, converge: null })

/** The pane's open tab, `1` Overview … `7` Timeline. */
export const paneTab = atom({ plugin: 'trajviz-obs', key: 'paneTab' } as const, 1)

/** The refresh value's reference, for the compare-and-set of `claimRefresh`. */
const refreshRef = { plugin: 'trajviz-obs', key: 'refresh' } as const

/** The watch debounce: when the last snapshot ran, how long it took, and the transcript it saw; `busy` while one runs, since `busySince`. */
export const refresh = atom(refreshRef, {
  lastRunAt: 0,
  lastDurationMs: 0,
  lastSize: 0,
  lastMtimeMs: 0,
  busy: false,
  busySince: 0,
})

/** The snapshot family; a member is `{ ...snapshotFamily, id }`. */
const snapshotFamily = { plugin: 'trajviz-obs', key: 'snapshot' } as const

/** The family member holding snapshot `id`; `null` once pruned. */
export function snapshotRef(id: string) {
  return { ...snapshotFamily, id } as const
}

/** The ids of the snapshots kept, oldest first. */
export async function readSnapshotIds($: EngineInterface): Promise<string[]> {
  return read($, snapshotIds)
}

/** Replaces the kept ids. */
export async function writeSnapshotIds($: EngineInterface, ids: string[]): Promise<void> {
  await update($, snapshotIds, () => ids)
}

/** The family member of snapshot `id`: null once pruned or never stored. */
export async function readSnapshotMember($: EngineInterface, id: string): Promise<Snapshot | EmptySnapshot | null> {
  const { value } = await $.state.get({ ...snapshotFamily, id })
  return value ?? null
}

/** Writes the family member of snapshot `id` (`null` prunes it). */
export async function writeSnapshotMember($: EngineInterface, id: string, value: Snapshot | EmptySnapshot | null): Promise<void> {
  await $.state.set({ ...snapshotFamily, id }, value)
}

/** The newest snapshot, for the pane and the status line. */
export async function writeLatest($: EngineInterface, snap: Snapshot | EmptySnapshot | null): Promise<void> {
  await update($, latest, () => snap)
}

/** The newest snapshot; null before the first. A render hook's read subscribes it. */
export async function readLatest($: EngineInterface): Promise<Snapshot | EmptySnapshot | null> {
  return read($, latest)
}

/** The pane's open tab, `1` Overview … `7` Timeline. */
export async function readPaneTab($: EngineInterface): Promise<number> {
  return read($, paneTab)
}

/** Opens tab `tab` (a hotkey's press). */
export async function writePaneTab($: EngineInterface, tab: number): Promise<void> {
  await update($, paneTab, () => tab)
}

/** The flags as they stand. */
export async function readFlags($: EngineInterface): Promise<Flags> {
  return read($, flags)
}

/** Flips `watch` and answers its new value. */
export async function toggleWatch($: EngineInterface): Promise<boolean> {
  let on = false
  await update($, flags, f => {
    on = !f.watch
    return { ...f, watch: on }
  })
  return on
}

/** Sets `watch` or `statusline` (`/obs watch on`, `/obs statusline off`). */
export async function setFlag($: EngineInterface, name: 'watch' | 'statusline', on: boolean): Promise<void> {
  await update($, flags, f => ({ ...f, [name]: on }))
}

/** The running dashboards. A render hook's read subscribes the drawing. */
export async function readDashboards($: EngineInterface): Promise<Dashboards> {
  return read($, dashboards)
}

/** No dashboard running (the module's load: a reload's children are gone with the old environment). */
export async function resetDashboards($: EngineInterface): Promise<void> {
  await update($, dashboards, () => dashboards.initial)
}

/** Records dashboard `kind` as running at `value`; `null` once it stops. */
export async function setDashboard($: EngineInterface, kind: keyof Dashboards, value: Dashboards[keyof Dashboards]): Promise<void> {
  await update($, dashboards, d => ({ ...d, [kind]: value }))
}

/** How often `claimRefresh` retries a write another beat. */
const CLAIM_TRIES = 8

/**
 * Takes the refresh lock: the claim's stamp (the host's clock) when `busy`
 * was clear and is now set, null when another analysis holds it (nothing
 * written then). A hold older than `staleAfterMs`, the longest a run can
 * legitimately last, was left by a dispatch cut before its `finally` ran
 * (Esc during a first-run setup, the plugin unloaded) and counts as clear:
 * `$.state` outlives the dispatch, so nothing else would ever release it.
 * A compare-and-set on the value's version, again on a miss, so two callers
 * never both claim it; one that keeps losing the race answers null as if held.
 */
export async function claimRefresh($: EngineInterface, staleAfterMs: number): Promise<number | null> {
  const now = await $.clock.now()
  for (let i = 0; i < CLAIM_TRIES; i++) {
    const { value, version } = await $.state.get(refreshRef)
    const r: RefreshState = value ?? refresh.initial
    if (r.busy && now - r.busySince < staleAfterMs) return null
    const w = await $.state.set(refreshRef, { ...r, busy: true, busySince: now }, { ifVersion: version })
    if (w.isSet) return now
  }
  return null
}

/**
 * Releases the refresh lock held by `claim` (the stamp `claimRefresh`
 * answered). A hold reclaimed as stale carries the reclaimer's stamp, so a
 * late release by the run that lost it leaves the lock to its new holder.
 */
export async function releaseRefresh($: EngineInterface, claim: number): Promise<void> {
  await update($, refresh, (r: RefreshState) => (r.busy && r.busySince === claim ? { ...r, busy: false, busySince: 0 } : r))
}

/** The refresh value as it stands (its initial while nothing is written). */
export async function readRefresh($: EngineInterface): Promise<RefreshState> {
  return read($, refresh)
}

/** Records a watch run: when it started, how long it took and the transcript stat it saw; the lock's fields are left alone. */
export async function stampRefresh($: EngineInterface, stamp: Pick<RefreshState, 'lastRunAt' | 'lastDurationMs' | 'lastSize' | 'lastMtimeMs'>): Promise<void> {
  await update($, refresh, (r: RefreshState) => ({ ...r, ...stamp }))
}

/** The session value (its initial while nothing is written). */
export async function readSession($: EngineInterface): Promise<SessionState> {
  return read($, session)
}

/** Applies `fn` to the session value and writes it back (again on a version miss). */
export async function updateSession($: EngineInterface, fn: (s: SessionState) => SessionState): Promise<void> {
  await update($, session, fn)
}

/** `options[name]` as a boolean; `fallback` when unset or not one. */
const optionBool = (options: PluginOptions, name: string, fallback: boolean): boolean => {
  const v = options[name]
  return typeof v === 'boolean' ? v : fallback
}

/**
 * The flags at session start: `watch` and `statusline` from the options
 * (`watch [on|off]` and `statusline [on|off]` change them later), whether a
 * person is at the prompt from the session; `envReady` is kept.
 */
export async function seedFlags($: EngineInterface, options: PluginOptions, isInteractive: boolean): Promise<void> {
  await update($, flags, f => ({
    ...f,
    watch: optionBool(options, 'watch', f.watch),
    statusline: optionBool(options, 'statusline', f.statusline),
    isInteractive,
  }))
}
