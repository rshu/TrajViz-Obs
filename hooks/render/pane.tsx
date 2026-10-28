// The live pane: `$.ui.open({ id: 'obs' })`, drawn by the `Pane` render hook
// in register.tsx from the latest snapshot. A tab bar of plain Buttons with
// hotkeys `1`–`7` (writing `paneTab`), an action row (`r` refresh, `w`
// watch, `d` dashboard), the open tab's body sized to
// `e.props.bodyColumns`, and a footer naming the snapshot. `openPane` and
// `renderPane` are function declarations taking `$` (validate follows `$`
// across an import only into one); state is reached through state.ts.
import type { EngineInterface, HookFailure, PaneOpenArgs, PluginOptions, RenderElement, RenderInput, SessionUsage, Timer, UiOpenResult } from 'claude-code'
import type { Dashboards, EmptySnapshot, Flags, Snapshot } from '../../types'
import { EMPTY_TEXT } from '../commands/overview'
import { refreshPane } from '../commands/pane'
import { runSelectedCommand, runSnapshotCommand } from '../lib/actions'
import { fmtCompact, fmtInt, fmtPct, fmtSecs } from '../lib/format'
import { isEmpty } from '../lib/snapshot'
import { toggleWatch, writePaneTab } from '../lib/state'
import {
  ContextBlock,
  HealthChips,
  IssuesList,
  KpiRow,
  MdTable,
  PhaseRibbon,
  Swimlane,
  ToolBars,
  WarningsLine,
  seriesOf,
  type Els,
} from './widgets'

/** The pane's id: its `ui.render` requestId, what `$.ui.close` names. */
export const PANE_ID = 'obs'

/** Its tab's label while other panes are open. */
export const PANE_TITLE = 'obs'

/** The body columns asked for when docked beside a fullscreen transcript. */
export const DOCK_COLUMNS = 56

/** The body rows asked for when seated inline above the prompt. */
export const INLINE_ROWS = 14

/** Below this many body columns the overview omits its charts. */
export const NARROW_COLUMNS = 48

/** The tabs by hotkey: `1` Overview … `7` Timeline. */
export const TABS: readonly string[] = ['Overview', 'Tools', 'Issues', 'Patterns', 'Agents', 'Context', 'Timeline']

export const NO_SNAPSHOT_TEXT = 'No snapshot yet: press r or run /obs.'
export const DRAG_TEXT = 'Widen the pane for charts, or open Dashboard for the full view.'

/** Live engine figures must not leave the whole pane waiting for an RPC. */
export const PANE_USAGE_TIMEOUT_MS = 500

/** The pane as the hook sees it, on whichever surface. */
export type PaneEvent = RenderInput<'Pane'>

/** Captured figures suffice; live Context reads can also include the breakdown. */
export type PaneUsage = Pick<SessionUsage, 'context' | 'cost'>

/** What the render hook hands `renderPane` beyond the state: the options (for a refresh) and the engine's usage. */
export type PaneExtras = {
  options: PluginOptions
  /** `$.session.usage()`, with the breakdown on the Context tab; null where the session has none. */
  usage: PaneUsage | null
}

/** How many issues the overview tab lists; the Issues tab lists them all. */
const OVERVIEW_ISSUES = 3

/** How many rows a pattern's table shows (each list holds at most 8). */
const PATTERN_ROWS = 8

/** Rows a table of agents or timeline steps shows before counting the rest. */
const TABLE_ROWS = 20

/** The pattern lists in drawing order, each with its heading. */
const PATTERN_SECTIONS: readonly { key: keyof Snapshot['patterns']; title: string }[] = [
  { key: 'tool_sequences', title: 'tool sequences' },
  { key: 'failure_patterns', title: 'failure patterns' },
  { key: 'error_clusters', title: 'error clusters' },
  { key: 'failure_chains', title: 'failure chains' },
  { key: 'fruitless_streaks', title: 'fruitless streaks' },
  { key: 'tool_selection', title: 'tool selection' },
  { key: 'edit_thrash', title: 'edit thrash' },
  { key: 'repeated_searches', title: 'repeated searches' },
  { key: 'phase_regressions', title: 'phase regressions' },
  { key: 'premature_compactions', title: 'premature compactions' },
  { key: 'hotspots', title: 'hotspots' },
]

const AGENT_COLUMNS = ['label', 'type', 'steps', 'tokens', 'tool_calls', 'errors', 'duration_s', 'cache_pct'] as const
const TIMELINE_COLUMNS = ['step', 'role', 'category', 'agent', 'tool_calls', 'errors', 'duration_s', 'tokens', 'preview'] as const
const FILE_COLUMNS = ['file', 'reads', 'writes'] as const

/**
 * Opens the pane (or retitles it when open): docked `DOCK_COLUMNS` wide
 * beside a fullscreen transcript, else inline `INLINE_ROWS` tall; `focus`
 * asks for the keyboard. Answers the engine's result, with its reason when
 * the surface cannot draw it yet.
 */
export async function placePane($: EngineInterface, opts: { focus?: boolean; inline: boolean }): Promise<UiOpenResult> {
  const args: PaneOpenArgs = { id: PANE_ID, title: PANE_TITLE }
  if (opts.focus) args.focus = true
  if (opts.inline) args.rows = INLINE_ROWS
  else args.columns = DOCK_COLUMNS
  return $.ui.open(args)
}

/** `placePane`, answering whether the pane is drawn. */
export async function openPane($: EngineInterface, opts: { focus?: boolean; inline: boolean }): Promise<boolean> {
  return (await placePane($, opts)).isPlaced
}

/** Live usage, with a Context breakdown; the captured figures keep a delayed host usable. */
export async function paneUsage($: EngineInterface, tab: number, fallback: PaneUsage | null = null): Promise<PaneUsage | null> {
  let timer: Timer | undefined
  const expired = new Promise<PaneUsage | null>(resolve => {
    timer = $.clock.after(PANE_USAGE_TIMEOUT_MS, () => resolve(fallback))
  })
  try {
    const live = tab === 6 ? $.session.usage({ breakdown: 'summary' }) : $.session.usage()
    return await Promise.race([live.catch(() => fallback), expired])
  } catch {
    return fallback
  } finally {
    timer?.cancel()
  }
}

/** A failed drawing still offers recovery without repeating the state reads that failed. */
export function renderPaneFailure($: EngineInterface, e: PaneEvent, error: HookFailure): RenderElement {
  const { Box, Button, Text } = $.ui.resolve(e)
  $.ui.log(`obs pane: ${error.kind}${error.message ? `: ${error.message.slice(0, 2000)}` : ''}`, { to: 'debug' })
  return (
    <Box flexDirection="column" gap={1}>
      <Text color="warning">Unable to draw the obs pane.</Text>
      <Text dimColor>Retry the drawing, or run /obs dashboard for the full view.</Text>
      <Box flexDirection="row" columnGap={2}>
        <Button key="retry" label="Retry" hotkey="r" variant="primary" plain onPress={() => $.ui.invalidate('ui.render')} />
        <Button key="close" label="Close" hotkey="x" plain onPress={() => $.ui.close({ id: PANE_ID })} />
      </Box>
    </Box>
  )
}

const two = (n: number): string => String(n).padStart(2, '0')

/** `HH:MM:SS` of an ISO stamp in local time; `?` when it does not parse. */
function timeOf(iso: string): string {
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return '?'
  return `${two(at.getHours())}:${two(at.getMinutes())}:${two(at.getSeconds())}`
}

/** A table's columns: the first row's keys. */
const keysOf = (rows: Record<string, unknown>[]): string[] => Object.keys(rows[0] ?? {})

// --- tab bodies -------------------------------------------------------------

type Body = { els: Els; e: PaneEvent; snap: Snapshot; columns: number; usage: PaneUsage | null }

/** The dock and short inline surface show a compact summary before detailed diagnostics. */
function overviewBody(b: Body): RenderElement {
  const { els, e, snap, columns, usage } = b
  const { Box, Text } = els
  const short = e.props.scroll.bodyRows < 24
  if (columns < 72 || short) {
    return (
      <Box flexDirection="column" gap={1}>
        {KpiRow({ els, snap, usage: usage ?? undefined, columns, summary: true })}
        <Box flexDirection="column">
          <Text weight="semibold">Top issues</Text>
          {IssuesList({ els, issues: snap.issues, limit: short ? 1 : OVERVIEW_ISSUES, total: snap.counts.issues, summary: true })}
          {snap.issues.length ? <Text dimColor>Press 3 for evidence and affected steps.</Text> : null}
        </Box>
        {WarningsLine({ els, warnings: snap.trajviz.warnings })}
        {HealthChips({ els, health: snap.health })}
        {columns < NARROW_COLUMNS ? <Text dimColor>{DRAG_TEXT}</Text> : null}
      </Box>
    )
  }
  return (
    <Box flexDirection="column" gap={1}>
      {HealthChips({ els, health: snap.health })}
      {KpiRow({ els, snap, usage: usage ?? undefined, columns })}
      <Box flexDirection="column">
        <Text weight="semibold">Top issues</Text>
        {IssuesList({ els, issues: snap.issues, limit: OVERVIEW_ISSUES, total: snap.counts.issues, summary: true })}
        {snap.issues.length ? <Text dimColor>Press 3 for evidence and affected steps.</Text> : null}
      </Box>
      {seriesOf({ els, surface: e.surface, snap, columns })}
      {PhaseRibbon({ els, phases: snap.phases, total: snap.counts.records, columns })}
      {WarningsLine({ els, warnings: snap.trajviz.warnings })}
    </Box>
  )
}

/** Tab 2: the totals, one bar per tool, the files touched and the skills used. */
function toolsBody({ els, snap, columns }: Body): RenderElement {
  const { Box, Text } = els
  const { tools, files, skills } = snap
  return (
    <Box flexDirection="column" gap={1}>
      <Text>{`${fmtInt(tools.calls)} calls · ${fmtPct(tools.success_rate)} ok · ${fmtInt(tools.failed)} failed`}</Text>
      {ToolBars({ els, byTool: tools.by_tool, width: columns })}
      <Box flexDirection="column">
        <Text dimColor>{`files: ${fmtInt(files.unique)} touched · ${fmtInt(files.reads)} reads · ${fmtInt(files.writes)} writes · ${fmtInt(files.searches)} searches`}</Text>
        {files.top.length ? MdTable({ els, rows: files.top, columns: FILE_COLUMNS, limit: files.top.length, width: columns }) : null}
      </Box>
      {skills.length ? <Text dimColor>{`skills: ${skills.map(s => `${s.skill} ×${fmtInt(s.calls)}`).join(' · ')}`}</Text> : null}
    </Box>
  )
}

/** Tab 3: every issue, then the bottlenecks. */
function issuesBody({ els, snap }: Body): RenderElement {
  const { Box, Text } = els
  return (
    <Box flexDirection="column" gap={1}>
      {IssuesList({ els, issues: snap.issues, limit: snap.issues.length, total: snap.counts.issues })}
      {snap.bottlenecks.length ? (
        <Box flexDirection="column">
          <Text weight="semibold">Bottlenecks</Text>
          {snap.bottlenecks.map(b => (
            <Text>{`step ${fmtInt(b.step)} ${b.duration_s.toFixed(1)}s (${b.title}) · ${b.cause} · ${b.detail}`}</Text>
          ))}
        </Box>
      ) : null}
    </Box>
  )
}

/** Pattern views can be lists or named groups (clusters, chains, hotspots). */
export function patternRows(data: Snapshot['patterns'][keyof Snapshot['patterns']]): Record<string, unknown>[] {
  if (Array.isArray(data)) return data
  return Object.entries(data).flatMap(([group, value]) =>
    Array.isArray(value) ? value.map(row => ({ group, ...row })) : [],
  )
}

/** Tab 4: one table per non-empty pattern view, TrajViz's rows as given. */
function patternsBody({ els, snap, columns }: Body): RenderElement {
  const { Box, Text } = els
  const sections = PATTERN_SECTIONS.map(s => ({ ...s, rows: patternRows(snap.patterns[s.key]) })).filter(s => s.rows.length > 0)
  if (sections.length === 0) return <Text dimColor>No patterns found.</Text>
  return (
    <Box flexDirection="column" gap={1}>
      {sections.map(s => (
        <Box flexDirection="column">
          <Text weight="semibold">{s.title.replace(/^./, c => c.toUpperCase())}</Text>
          {MdTable({ els, rows: s.rows, columns: keysOf(s.rows), limit: PATTERN_ROWS, width: columns })}
        </Box>
      ))}
    </Box>
  )
}

/** Tab 5: the agents table and the swimlane. */
function agentsBody({ els, e, snap, columns }: Body): RenderElement {
  const { Box } = els
  return (
    <Box flexDirection="column" gap={1}>
      {MdTable({ els, rows: snap.agents, columns: AGENT_COLUMNS, limit: TABLE_ROWS, width: columns })}
      {Swimlane({ els, surface: e.surface, lanes: snap.swimlane.lanes, total: snap.swimlane.total_steps, columns })}
    </Box>
  )
}

/** Tab 6: TrajViz's context numbers and series beside the engine's own breakdown. */
function contextBody({ els, e, snap, columns, usage }: Body): RenderElement {
  const { Box, Text } = els
  const c = snap.context
  const window = c.window === null ? `window ? (${c.window_note ?? c.window_source})` : `window ${fmtCompact(c.window)} (${c.window_source})`
  const now = c.current_occupancy === null ? 'now ?' : `now ${fmtPct(c.current_pct)} (${fmtCompact(c.current_occupancy)})`
  const peak = c.peak_occupancy === null ? 'peak ?' : `peak ${fmtPct(c.peak_pct)} (${fmtCompact(c.peak_occupancy)})`
  const drop = c.largest_drop === null ? '' : ` · largest drop ${fmtCompact(c.largest_drop)}`
  const rows = usage?.context.breakdown?.categories ?? []
  const width = Math.max(0, ...rows.map(r => r.name.length))
  return (
    <Box flexDirection="column" gap={1}>
      <Text>{`${window} · ${now} · ${peak} · ${fmtInt(c.compaction_count)} compactions${drop}`}</Text>
      {ContextBlock({ els, surface: e.surface, snap, columns })}
      <Box flexDirection="column">
        <Text weight="semibold">{`engine context${usage?.context.percent !== undefined ? ` · ${fmtPct(usage.context.percent)} of ${fmtCompact(usage.context.window)}` : ''}`}</Text>
        {rows.length ? (
          rows.map(r => <Text dimColor={r.kind !== 'used'}>{`${r.name.padEnd(width)}  ${fmtCompact(r.tokens)}`}</Text>)
        ) : (
          <Text dimColor>no breakdown from the engine (/context)</Text>
        )}
      </Box>
    </Box>
  )
}

/** Tab 7: the last steps as a table. */
function timelineBody({ els, snap, columns }: Body): RenderElement {
  return MdTable({ els, rows: snap.timeline, columns: TIMELINE_COLUMNS, limit: TABLE_ROWS, width: columns })
}

function tabBody(tab: number, b: Body): RenderElement {
  switch (tab) {
    case 2:
      return toolsBody(b)
    case 3:
      return issuesBody(b)
    case 4:
      return patternsBody(b)
    case 5:
      return agentsBody(b)
    case 6:
      return contextBody(b)
    case 7:
      return timelineBody(b)
    default:
      return overviewBody(b)
  }
}

// --- the pane ---------------------------------------------------------------

/**
 * The pane: the tab bar, the action row, the open tab's body from `snap`
 * (a line when there is none yet, or it is the empty state) and the footer.
 * Every press writes state or runs a command; nothing here reads state.
 */
export function renderPane(
  $: EngineInterface,
  e: PaneEvent,
  snap: Snapshot | EmptySnapshot | null,
  tab: number,
  flags: Flags,
  dashboards: Dashboards,
  extras: PaneExtras,
): RenderElement {
  const els = $.ui.resolve(e)
  const { Box, Button, Text } = els
  const columns = Math.max(1, Math.floor(e.props.bodyColumns))
  const refresh = async (): Promise<void> => {
    const row = await refreshPane($, extras.options)
    if (row !== null) $.ui.toast(`obs: ${row}`)
  }
  const watch = async (): Promise<void> => {
    $.ui.toast(`obs: watch ${(await toggleWatch($)) ? 'on' : 'off'}`)
  }
  const body =
    snap === null ? (
      <Text dimColor>{NO_SNAPSHOT_TEXT}</Text>
    ) : isEmpty(snap) ? (
      <Text dimColor>{EMPTY_TEXT}</Text>
    ) : (
      tabBody(tab, { els, e, snap, columns, usage: extras.usage })
    )
  const footer =
    snap === null || isEmpty(snap)
      ? null
      : `snapshot ${timeOf(snap.generated_at)} · ${fmtInt(snap.counts.assistant_steps)} steps · computed in ${fmtInt(snap.duration_ms)} ms · wall ${fmtSecs(snap.timing.wall_clock_s)} · watch ${flags.watch ? 'on' : 'off'}`
  return (
    <Box flexDirection="column" gap={1}>
      <Box flexDirection="row" flexWrap="wrap" columnGap={1} rowGap={0}>
        {TABS.map((label, i) => {
          const n = i + 1
          const style = n === tab ? { variant: 'primary' as const } : { dimColor: true }
          return <Button key={`tab:${n}`} label={label} hotkey={String(n)} plain {...style} onPress={() => writePaneTab($, n)} />
        })}
      </Box>
      <Box flexDirection="row" flexWrap="wrap" columnGap={2} rowGap={0}>
        <Button key="dashboard" label="Dashboard" hotkey="d" variant="primary" plain onPress={() => snap !== null ? runSnapshotCommand($, snap, 'dashboard', extras) : runSelectedCommand($, 'dashboard', extras)} />
        <Button key="refresh" label="Refresh" hotkey="r" plain onPress={refresh} />
        <Button key="watch" label={`Watch ${flags.watch ? 'on' : 'off'}`} hotkey="w" plain onPress={watch} />
      </Box>
      {dashboards.insight ? <Text dimColor wrap="wrap">{`Dashboard: ${dashboards.insight.url}`}</Text> : null}
      {dashboards.converge ? <Text dimColor wrap="wrap">{`Compare: ${dashboards.converge.url}`}</Text> : null}
      <Box flexDirection="column">
        <Text weight="semibold">{TABS[tab - 1] ?? TABS[0]}</Text>
        {columns >= 72 && e.props.scroll.bodyRows >= 24 ? <Text dimColor wrap="wrap">{TAB_GUIDES[tab - 1] ?? TAB_GUIDES[0]}</Text> : null}
      </Box>
      {body}
      {footer ? <Text dimColor>{footer}</Text> : null}
    </Box>
  )
}

/** Explain the question each diagnostic tab answers, without adding controls. */
const TAB_GUIDES = [
  'Session health and recent activity. Start with the top issues below.',
  'Tool usage, failures, files touched, and skills used.',
  'Ranked findings with evidence, affected steps, and suggested next checks.',
  'Repeated behavior that may explain wasted work or failures.',
  'Agent workload and when each agent was active.',
  'Transcript context estimates alongside Claude Code’s current context usage.',
  'Recent steps in order. Open Dashboard to inspect the full workflow.',
] as const
