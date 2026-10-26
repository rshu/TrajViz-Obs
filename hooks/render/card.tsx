// The overview card: `/obs`'s CommandOutput row drawn from its own snapshot.
// `renderCard` is a function declaration taking `$` (validate follows `$`
// across an import only into one); it reads nothing from state, the hook in
// register.tsx hands it the snapshot the row's provenance line names. The
// Wide surfaces show the full charts; narrower surfaces lead with actions,
// compact metrics and ranked issue titles, with details in the pane/dashboard.
import type { EngineInterface, PluginOptions, RenderElement, RenderInput, SessionUsage } from 'claude-code'
import type { Snapshot } from '../../types'
import { runSnapshotCommand } from '../lib/actions'
import { fmtInt } from '../lib/format'
import {
  DimLine,
  HealthChips,
  IssuesList,
  KpiRow,
  PhaseRibbon,
  Swimlane,
  ToolBars,
  WarningsLine,
  seriesOf,
} from './widgets'

/** The row as the hook sees it, on whichever surface. */
export type CardEvent = RenderInput<'CommandOutput'>

export type CardOptions = {
  /** The plugin configuration captured by the render hook for its actions. */
  options: PluginOptions
  /** Whether the button row is drawn (where a press can land); else the dim line of `/obs` forms. */
  showButtons: boolean
  /** The viewport's columns, what the card is sized to. */
  columns: number
  /** The engine's usage for the live context fill and the cost; null where it has none. */
  usage: Snapshot['engine_usage'] | null
}

/** The columns assumed while no surface has measured. */
export const DEFAULT_COLUMNS = 80

/** From here the full layout: the series side by side and the swimlane. */
export const FULL_COLUMNS = 100

/** The button row, in order: each key is the `/obs` subcommand its press runs. */
export const BUTTONS: readonly { key: string; label: string }[] = [
  { key: 'dashboard', label: 'Dashboard' },
  { key: 'pane', label: 'Live pane' },
  { key: 'json', label: 'Export JSON' },
  { key: 'fixes', label: 'AI fixes' },
  { key: 'analyze', label: 'AI analyze' },
]

/** How many issues the full and compact cards list. */
const ISSUES_SHOWN = 5
const ISSUES_SHOWN_MINIMAL = 3

/** The cells between two series drawn side by side. */
const SERIES_GAP = 2

/** The viewport's columns, `DEFAULT_COLUMNS` while unmeasured. */
export function cardColumns(e: CardEvent): number {
  const c = e.viewport?.columns
  return c !== undefined && Number.isFinite(c) && c > 0 ? Math.floor(c) : DEFAULT_COLUMNS
}

/** Whether a button can be pressed where the row draws: the terminal's fullscreen layout, or any other surface. */
export function showsButtons(e: CardEvent): boolean {
  return e.surface !== 'terminal' || e.viewport?.isFullscreen === true
}

/** The engine's usage, null where the session has none to report. */
export async function sessionUsage($: EngineInterface): Promise<SessionUsage | null> {
  try {
    return await $.session.usage()
  } catch {
    return null
  }
}

/** The first line of the row's text, the provenance line, without its warnings (drawn in colour below). */
function provenance(text: string): string {
  const first = text.split('\n')[0] ?? ''
  const cut = first.indexOf(' · warnings:')
  return cut >= 0 ? first.slice(0, cut) : first
}

/**
 * The card: the provenance line with the snapshot's compute time, health
 * chips, the KPIs, the series (from 100 columns), tool bars, the phase
 * ribbon, the swimlane (from 100), the top issues, the bottlenecks, the
 * load warnings, and the button row or the dim line of `/obs` forms.
 */
export function renderCard($: EngineInterface, e: CardEvent, snap: Snapshot, opts: CardOptions): RenderElement {
  const els = $.ui.resolve(e)
  const { Box, Button, Text } = els
  const { columns, usage, showButtons } = opts
  const surface = e.surface
  const full = columns >= FULL_COLUMNS
  const series = full ? seriesOf({ els, surface, snap, columns: Math.floor((columns - 2 * SERIES_GAP) / 3) }) : []
  const bottlenecks = snap.bottlenecks.map(b => `step ${fmtInt(b.step)} ${b.duration_s.toFixed(1)}s (${b.title})`).join(' · ')
  return (
    <Box flexDirection="column" gap={1}>
      <Text dimColor>{`${provenance(e.props.text)} · computed in ${fmtInt(snap.duration_ms)} ms`}</Text>
      {showButtons ? (
        <Box flexDirection="row" flexWrap="wrap" columnGap={1} rowGap={0}>
          {BUTTONS.map(b => (
            <Button key={b.key} label={b.label} {...(b.key === 'dashboard' ? { variant: 'primary' as const } : {})} onPress={() => runSnapshotCommand($, snap, b.key, { options: opts.options, presentation: { columns, isFullscreen: e.viewport?.isFullscreen === true } })} />
          ))}
        </Box>
      ) : (
        DimLine({ els, text: BUTTONS.map(b => `/obs ${b.key}`).join(' · ') })
      )}
      {HealthChips({ els, health: snap.health })}
      {KpiRow({ els, snap, usage: usage ?? undefined, columns, summary: !full })}
      <Box flexDirection="column">
        <Text weight="semibold">Top issues</Text>
        {IssuesList({ els, issues: snap.issues, limit: full ? ISSUES_SHOWN : ISSUES_SHOWN_MINIMAL, total: snap.counts.issues, summary: !full })}
        {!full && snap.issues.length ? <Text dimColor>Run /obs issues for evidence and affected steps.</Text> : null}
      </Box>
      {WarningsLine({ els, warnings: snap.trajviz.warnings })}
      {full ? <Box flexDirection="row" gap={SERIES_GAP}>{series}</Box> : null}
      {full ? ToolBars({ els, byTool: snap.tools.by_tool, width: columns }) : null}
      {full ? PhaseRibbon({ els, phases: snap.phases, total: snap.counts.records, columns }) : null}
      {full ? Swimlane({ els, surface, lanes: snap.swimlane.lanes, total: snap.swimlane.total_steps, columns }) : null}
      {bottlenecks ? <Text dimColor>{`Bottlenecks: ${bottlenecks}`}</Text> : null}
    </Box>
  )
}
