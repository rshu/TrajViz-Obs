// The widgets the overview card and the pane tabs are built from: pure
// functions from the surface's element table (`els = $.ui.resolve(e)`) and
// the snapshot's data to a plain-data tree. None takes `$`, so nothing here
// is a hook or a state reader. A Raster is drawn only on the terminal (the
// one surface whose table has it); elsewhere the same series is block-glyph
// text. Colours are theme keys where a theme has one (status, dim), and raw
// RGB where a Raster's cells need numbers, with the same RGB as `#rrggbb`
// on the Text beside it.
import type { ElementTable, RenderElement, RenderSurface, ThemeKey } from 'claude-code'
import type { HealthRow, Issue, Phase, Segment, Snapshot, Tokens, ToolRow } from '../../types'
import { clip, fmtCompact, fmtInt, fmtPct, fmtSecs } from '../lib/format'
import { bar, sparkline } from '../lib/sparkline'
import { sparklineRaster, stackedRaster, swimlaneRaster } from '../lib/raster'

/** The element table of whichever surface draws: the constructors every surface carries, `Raster` only on the terminal's. */
export type Els = ElementTable

/** The Raster constructor when `surface` is the terminal and the table has it; null elsewhere. */
function rasterOf(els: Els, surface: RenderSurface): ElementTable<'terminal'>['Raster'] | null {
  return surface === 'terminal' && 'Raster' in els ? els.Raster : null
}

/** `0xRRGGBB` as the `#rrggbb` a Text's `color` takes. */
export const hex = (rgb: number): string => `#${(rgb & 0xffffff).toString(16).padStart(6, '0')}`

/** The swimlane's and the phase ribbon's lane colours, cycled by index. */
export const LANE_COLORS: readonly number[] = [0x3b82f6, 0xf59e0b, 0x10b981, 0xa855f7, 0xec4899, 0x14b8a6]

/** The theme key of a health status: `good`/`ok` → success, `warn` → warning, `bad`/`error` → error; undefined for anything else. */
export function statusColor(status: string): ThemeKey | undefined {
  switch (status.toLowerCase()) {
    case 'ok':
    case 'good':
    case 'pass':
      return 'success'
    case 'warn':
    case 'warning':
      return 'warning'
    case 'bad':
    case 'error':
    case 'fail':
    case 'critical':
      return 'error'
    default:
      return undefined
  }
}

/** Cache reads over every input token (fresh, cache read and cache write), in 0–100; null with no input at all. */
export function cacheEfficiency(t: Tokens): number | null {
  const all = t.input + t.cache_read + t.cache_write
  return all > 0 ? (t.cache_read / all) * 100 : null
}

/** The Raster's limits: 1 to 512 columns, 1 to 256 rows. */
const rasterColumns = (n: number): number => Math.max(1, Math.min(512, Math.floor(n)))
const rasterRows = (n: number): number => Math.max(1, Math.min(256, Math.floor(n)))

// --- health -----------------------------------------------------------------

/** One chip per health row, `● metric label`, in the status colour. */
export function HealthChips({ els, health }: { els: Els; health: HealthRow[] }): RenderElement {
  const { Box, Text } = els
  return (
    <Box flexDirection="row" flexWrap="wrap" gap={2}>
      {health.map(row => (
        <Text color={statusColor(row.status)} weight="semibold">
          {`● ${row.metric.replace(/_/g, ' ')} ${row.label}`}
        </Text>
      ))}
    </Box>
  )
}

// --- KPIs -------------------------------------------------------------------

type Kpi = { label: string; value: string; sub?: string }

/** The KPI tiles: steps, prompts, agents, tokens, cache efficiency, tools, wall clock, cost and the live context fill. */
function kpis(snap: Snapshot, usage?: Snapshot['engine_usage']): Kpi[] {
  const { counts, tokens, tools, timing, context } = snap
  const out: Kpi[] = [
    { label: 'Steps', value: fmtInt(counts.assistant_steps), sub: `${fmtInt(counts.prompts)} prompts` },
    { label: 'Agents', value: fmtInt(counts.agents), sub: `${fmtInt(counts.compactions)} compactions` },
    { label: 'Tokens', value: fmtCompact(tokens.total), sub: `${fmtCompact(tokens.input)} fresh · ${fmtCompact(tokens.cache_read)} cache · ${fmtCompact(tokens.output)} out` },
    { label: 'Cache Efficiency', value: fmtPct(cacheEfficiency(tokens)), sub: `${fmtCompact(tokens.cache_write)} written` },
    { label: 'Tools', value: fmtInt(tools.calls), sub: `${fmtPct(tools.success_rate)} ok · ${fmtInt(tools.failed)} failed` },
    { label: 'Wall clock', value: fmtSecs(timing.wall_clock_s), sub: `avg ${fmtSecs(timing.avg_step_s)} · p95 ${fmtSecs(timing.p95_step_s)}` },
  ]
  if (usage?.cost) out.push({ label: 'Cost', value: `$${usage.cost.usd.toFixed(2)}` })
  const live = usage?.context.percent
  const fill = live !== undefined ? fmtPct(live) : context.window === null ? '?' : fmtPct(context.current_pct)
  const sub = context.window === null ? (context.window_note ?? 'window unknown') : `peak ${fmtPct(context.peak_pct)} of ${fmtCompact(context.window)}`
  out.push({ label: 'Context', value: fill, sub })
  return out
}

/** The cells between two tiles on a row. */
const TILE_GAP = 3

/** A tile's width in cells: its widest line. */
const tileWidth = (k: Kpi): number => Math.max(k.label.length, k.value.length, k.sub?.length ?? 0)

/**
 * Packs the tiles into rows by their measured widths: a tile joins the row
 * while the row's tiles plus `TILE_GAP` between each pair stay within
 * `columns`; a tile wider than `columns` has a row of its own.
 */
function packTiles(items: Kpi[], columns: number): Kpi[][] {
  const rows: Kpi[][] = []
  let row: Kpi[] = []
  let used = 0
  for (const k of items) {
    const w = tileWidth(k)
    const next = row.length === 0 ? w : used + TILE_GAP + w
    if (row.length > 0 && next > columns) {
      rows.push(row)
      row = []
      used = w
    } else {
      used = next
    }
    row.push(k)
  }
  if (row.length > 0) rows.push(row)
  return rows
}

/**
 * The KPIs by the breakpoints: from 72 columns tiles (a dim label over the
 * value over a dim sub), packed into as few rows as their measured widths
 * fit in `columns` (one row where the width allows, two or three on the
 * narrower terminals); below 72 a list of `label  value  (sub)` lines. The
 * tiles sit under a Box keyed `kpis`, one row Box each.
 */
export function KpiRow({ els, snap, usage, columns }: { els: Els; snap: Snapshot; usage?: Snapshot['engine_usage']; columns: number }): RenderElement {
  const { Box, Text } = els
  const items = kpis(snap, usage)
  if (columns < 72) {
    const width = Math.max(...items.map(k => k.label.length))
    return (
      <Box flexDirection="column">
        {items.map(k => (
          <Text>{`${k.label.padEnd(width)}  ${k.value}${k.sub ? `  (${k.sub})` : ''}`}</Text>
        ))}
      </Box>
    )
  }
  return (
    <Box key="kpis" flexDirection="column" gap={1}>
      {packTiles(items, columns).map(row => (
        <Box flexDirection="row" gap={TILE_GAP}>
          {row.map(k => (
            <Box flexDirection="column">
              <Text dimColor>{k.label}</Text>
              <Text weight="semibold">{k.value}</Text>
              {k.sub ? <Text dimColor>{k.sub}</Text> : null}
            </Box>
          ))}
        </Box>
      ))}
    </Box>
  )
}

// --- series -----------------------------------------------------------------

type SparklineProps = {
  els: Els
  surface: RenderSurface
  label: string
  values: (number | null)[]
  columns: number
  rows: number
  /** The bars' colour, `0xRRGGBB`. */
  color: number
  /** The Raster's address, unique in the tree. */
  key: string
}

/** A label line over one series: a Raster column chart on the terminal, a block-glyph line elsewhere. */
export function SparklineBlock({ els, surface, label, values, columns, rows, color, key }: SparklineProps): RenderElement {
  const { Box, Text } = els
  const Raster = rasterOf(els, surface)
  const cols = rasterColumns(columns)
  return (
    <Box flexDirection="column">
      <Text dimColor>{label}</Text>
      {Raster ? <Raster key={key} {...sparklineRaster(values, cols, rasterRows(rows), color)} /> : <Text color={hex(color)}>{sparkline(values, cols)}</Text>}
    </Box>
  )
}

type Stack = { name: string; values: number[]; color: number }

type StackedProps = {
  els: Els
  surface: RenderSurface
  label: string
  stacks: Stack[]
  columns: number
  rows: number
  key: string
}

/**
 * A label and legend over stacked series (tokens by segment): a stacked
 * Raster on the terminal, elsewhere a block-glyph line of the per-step
 * totals.
 */
export function StackedBlock({ els, surface, label, stacks, columns, rows, key }: StackedProps): RenderElement {
  const { Box, Text } = els
  const Raster = rasterOf(els, surface)
  const cols = rasterColumns(columns)
  const n = Math.max(0, ...stacks.map(s => s.values.length))
  const totals = Array.from({ length: n }, (_, i) => stacks.reduce((sum, s) => sum + Math.max(0, s.values[i] ?? 0), 0))
  return (
    <Box flexDirection="column">
      <Box flexDirection="row" gap={2}>
        <Text dimColor>{label}</Text>
        {stacks.map(s => (
          <Text color={hex(s.color)}>{`■ ${s.name}`}</Text>
        ))}
      </Box>
      {Raster ? (
        <Raster key={key} {...stackedRaster(stacks.map(s => ({ values: s.values, fg: s.color })), cols, rasterRows(rows))} />
      ) : (
        <Text>{sparkline(totals, cols)}</Text>
      )}
    </Box>
  )
}

// --- the snapshot's series --------------------------------------------------

/** The series' colours, `0xRRGGBB`: fresh, cache read, cache write, output; step seconds; context occupancy. */
const FRESH = 0x3b82f6
const CACHE_READ = 0x10b981
const CACHE_WRITE = 0x14b8a6
const OUTPUT = 0xa855f7
const SECONDS = 0xf59e0b
const OCCUPANCY = 0xec4899

/** `a, b, c` of the numbers, or '' with none. */
const list = (ns: number[]): string => ns.map(n => fmtInt(n)).join(', ')

/** The steps of the duration outliers: each an index into the series (the step number when the index is past it). */
function outlierSteps(d: Snapshot['series']['durations']): number[] {
  return d.outliers.map(i => d.steps[i] ?? i)
}

type SeriesProps = { els: Els; surface: RenderSurface; snap: Snapshot; columns: number }

/** The context occupancy series, keyed `context`, the compaction turns named on its label. */
export function ContextBlock({ els, surface, snap, columns }: SeriesProps): RenderElement {
  const c = snap.series.context
  return SparklineBlock({
    els,
    surface,
    label: c.compactions.length ? `context occupancy · compactions at turns ${list(c.compactions)}` : 'context occupancy',
    key: 'context',
    values: c.occupancy,
    columns,
    rows: 3,
    color: OCCUPANCY,
  })
}

/**
 * The snapshot's three series, each `columns` wide and in this order: the
 * tokens per step stacked by segment (keyed `tokens`, 4 rows), the step
 * seconds with the outlier steps named (`durations`, 3 rows) and the
 * context occupancy (`context`, 3 rows). The card and the pane draw the
 * same elements, laid out as each sees fit.
 */
export function seriesOf(p: SeriesProps): RenderElement[] {
  const { els, surface, snap, columns } = p
  const t = snap.series.tokens
  const d = snap.series.durations
  const outliers = outlierSteps(d)
  return [
    StackedBlock({
      els,
      surface,
      label: 'tokens per step',
      key: 'tokens',
      columns,
      rows: 4,
      stacks: [
        { name: 'fresh', values: t.fresh, color: FRESH },
        { name: 'cache', values: t.cache_read, color: CACHE_READ },
        { name: 'write', values: t.cache_write, color: CACHE_WRITE },
        { name: 'out', values: t.output, color: OUTPUT },
      ],
    }),
    SparklineBlock({
      els,
      surface,
      label: outliers.length ? `step seconds · outliers at steps ${list(outliers)}` : 'step seconds',
      key: 'durations',
      values: d.seconds,
      columns,
      rows: 3,
      color: SECONDS,
    }),
    ContextBlock(p),
  ]
}

// --- tools ------------------------------------------------------------------

/** One line per tool: the name, a bar by calls and the counts, errors in the error colour. */
export function ToolBars({ els, byTool, width }: { els: Els; byTool: ToolRow[]; width: number }): RenderElement {
  const { Box, Text } = els
  if (byTool.length === 0) return <Text dimColor>No tool calls.</Text>
  const name = Math.min(14, Math.max(...byTool.map(t => t.tool.length)))
  const max = Math.max(...byTool.map(t => t.calls))
  const barWidth = Math.max(4, Math.min(40, width - name - 16))
  return (
    <Box flexDirection="column">
      {byTool.map(t => (
        <Box flexDirection="row" gap={1}>
          <Text>{clip(t.tool, name).padEnd(name)}</Text>
          <Text color="claude">{bar(t.calls, max, barWidth)}</Text>
          <Text dimColor>{fmtInt(t.calls).padStart(5)}</Text>
          {t.errors > 0 ? <Text color="error">{`${fmtInt(t.errors)} err`}</Text> : null}
        </Box>
      ))}
    </Box>
  )
}

// --- phases and agents ------------------------------------------------------

/** The columns step `lo`..`hi` (inclusive, 1-based or 0-based alike) own of `columns` over `total` steps, as the swimlane raster maps them. */
function span(lo: number, hi: number, total: number, columns: number): [number, number] {
  const t = Math.max(1, total)
  const first = Math.max(0, Math.min(columns - 1, Math.floor((Math.min(lo, hi) * columns) / t)))
  const last = Math.max(first, Math.min(columns - 1, Math.ceil(((Math.max(lo, hi) + 1) * columns) / t) - 1))
  return [first, last]
}

/**
 * The cells of `columns` the steps `lo`..`hi` own over `total`, half-open
 * `[first, next)`: step `k` owns `[k * columns / total, (k + 1) * columns / total)`,
 * so bands over adjacent steps meet with no overlap and no gap, and a band
 * is at least one cell wide.
 */
function cellRange(lo: number, hi: number, total: number, columns: number): [number, number] {
  const t = Math.max(1, total)
  const first = Math.max(0, Math.min(columns - 1, Math.floor((Math.min(lo, hi) * columns) / t)))
  const next = Math.max(first + 1, Math.min(columns, Math.floor(((Math.max(lo, hi) + 1) * columns) / t)))
  return [first, next]
}

/**
 * One line of bands, at most `columns` cells in all (at most 512): each
 * phase's name on the cells its steps own over `total`,
 * in a lane colour; the cells no phase owns (before the first, between two)
 * are blank spacers. Under a Box keyed `phases`.
 */
export function PhaseRibbon({ els, phases, total, columns }: { els: Els; phases: Phase[]; total: number; columns: number }): RenderElement {
  const { Box, Text } = els
  if (phases.length === 0) return <Text dimColor>No phases.</Text>
  const width = Math.max(1, Math.min(512, Math.floor(columns)))
  // Which phase owns each cell, in step order so a later phase settles a shared boundary cell.
  const owner: (number | null)[] = Array.from({ length: width }, () => null)
  const order = phases.map((p, i) => i).sort((a, b) => Math.min(phases[a]!.start, phases[a]!.end) - Math.min(phases[b]!.start, phases[b]!.end))
  for (const i of order) {
    const p = phases[i]!
    const [first, next] = cellRange(p.start, p.end, total, width)
    for (let c = first; c < next; c++) owner[c] = i
  }
  // Runs of one owner, each one Text: a band with the name, or a blank spacer.
  const runs: { owner: number | null; cells: number }[] = []
  for (const o of owner) {
    const last = runs[runs.length - 1]
    if (last && last.owner === o) last.cells++
    else runs.push({ owner: o, cells: 1 })
  }
  return (
    <Box key="phases" flexDirection="row">
      {runs.map(run =>
        run.owner === null ? (
          <Text>{' '.repeat(run.cells)}</Text>
        ) : (
          <Text color="inverseText" backgroundColor={hex(LANE_COLORS[run.owner % LANE_COLORS.length] as number)}>
            {clip(phases[run.owner]!.name, run.cells).padEnd(run.cells)}
          </Text>
        ),
      )}
    </Box>
  )
}

/** A lane as block glyphs: full blocks over its segments, spaces elsewhere. */
function laneText(segments: Segment[], total: number, columns: number): string {
  const cells = Array.from({ length: columns }, () => ' ')
  for (const seg of segments) {
    const [first, last] = span(seg.start, seg.end, total, columns)
    for (let c = first; c <= last; c++) cells[c] = '█'
  }
  return cells.join('')
}

type Lane = { id: string; label: string; segments: Segment[] }

/**
 * The agent swimlane: a label column beside one row per lane, a Raster on
 * the terminal and block-glyph lines elsewhere, over `columns` in all.
 */
export function Swimlane({ els, surface, lanes, total, columns }: { els: Els; surface: RenderSurface; lanes: Lane[]; total: number; columns: number }): RenderElement {
  const { Box, Text } = els
  if (lanes.length === 0) return <Text dimColor>No agents.</Text>
  const Raster = rasterOf(els, surface)
  const shown = lanes.slice(0, 256)
  const labelWidth = Math.min(16, Math.max(0, columns - 2), Math.max(...shown.map(l => l.label.length)))
  const gap = labelWidth > 0 ? 1 : 0
  const width = rasterColumns(columns - labelWidth - gap)
  const colored = shown.map((l, i) => ({ segments: l.segments, fg: LANE_COLORS[i % LANE_COLORS.length] as number }))
  const more = lanes.length > shown.length ? <Text dimColor>{`… ${fmtInt(lanes.length - shown.length)} more agent lanes`}</Text> : null
  if (Raster) {
    return (
      <Box flexDirection="column">
        <Box flexDirection="row" gap={gap}>
          {labelWidth > 0 ? <Box flexDirection="column">
            {shown.map(l => <Text>{clip(l.label, labelWidth).padEnd(labelWidth)}</Text>)}
          </Box> : null}
          <Raster key="swimlane" {...swimlaneRaster(colored, total, width)} />
        </Box>
        {more}
      </Box>
    )
  }
  return (
    <Box flexDirection="column">
      {shown.map((l, i) => (
        <Text color={hex(colored[i]?.fg ?? 0)}>{`${labelWidth > 0 ? `${clip(l.label, labelWidth).padEnd(labelWidth)} ` : ''}${laneText(l.segments, total, width)}`}</Text>
      ))}
      {more}
    </Box>
  )
}

// --- issues and warnings ----------------------------------------------------

/** How many step numbers an issue line lists before `+n`. */
const STEPS_SHOWN = 6

/** `steps 140, 142, 144 +3`, or '' with none. */
function stepsText(steps: number[]): string {
  if (steps.length === 0) return ''
  const shown = steps.slice(0, STEPS_SHOWN).map(s => fmtInt(s)).join(', ')
  const more = steps.length - STEPS_SHOWN
  return `${steps.length === 1 ? 'step' : 'steps'} ${shown}${more > 0 ? ` +${more}` : ''}`
}

/** The top `limit` issues, each its title with its steps, its evidence and, dim, its `why`. */
export function IssuesList({ els, issues, limit, total = issues.length }: { els: Els; issues: Issue[]; limit: number; total?: number }): RenderElement {
  const { Box, Text } = els
  const shown = issues.slice(0, Math.max(0, limit))
  if (shown.length === 0) return <Text dimColor>No issues found.</Text>
  return (
    <Box flexDirection="column">
      {shown.map((issue, i) => (
        <Box flexDirection="column">
          <Box flexDirection="row" gap={1}>
            <Text weight="semibold">{`${i + 1}. ${issue.title}`}</Text>
            {issue.steps.length > 0 ? <Text dimColor>{stepsText(issue.steps)}</Text> : null}
          </Box>
          {issue.evidence ? <Text color="subtle">{`   ${issue.evidence}`}</Text> : null}
          {issue.why ? <Text dimColor>{`   ${issue.why}`}</Text> : null}
        </Box>
      ))}
      {total > shown.length ? <Text dimColor>{`… ${fmtInt(total - shown.length)} more`}</Text> : null}
    </Box>
  )
}

/** TrajViz's load warnings on one line in the warning colour; null with none. */
export function WarningsLine({ els, warnings }: { els: Els; warnings: string[] }): RenderElement | null {
  const { Text } = els
  if (warnings.length === 0) return null
  return <Text color="warning">{`⚠ ${warnings.join('; ')}`}</Text>
}

// --- tables and lines -------------------------------------------------------

/** A cell's text: numbers and strings as they are, null and undefined empty, anything else as JSON; `|` escaped. */
function cellText(v: unknown): string {
  if (v === null || v === undefined) return ''
  const s = typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean' ? String(v) : JSON.stringify(v)
  return s.replace(/\|/g, '\\|').replace(/\n/g, ' ')
}

/** The Markdown table of `rows` over `columns` (its keys, in order), to `limit` rows, the rest counted. */
export function MdTable({ els, rows, columns, limit }: { els: Els; rows: Record<string, unknown>[]; columns: readonly string[]; limit: number }): RenderElement {
  const { Box, Markdown, Text } = els
  const shown = rows.slice(0, Math.max(0, limit))
  if (shown.length === 0 || columns.length === 0) return <Text dimColor>No rows.</Text>
  const lines = [`| ${columns.join(' | ')} |`, `| ${columns.map(() => '---').join(' | ')} |`]
  for (const row of shown) lines.push(`| ${columns.map(c => cellText(row[c])).join(' | ')} |`)
  const more = rows.length - shown.length
  return (
    <Box flexDirection="column">
      <Markdown text={lines.join('\n')} />
      {more > 0 ? <Text dimColor>{`… ${fmtInt(more)} more`}</Text> : null}
    </Box>
  )
}

/** One dim line. */
export function DimLine({ els, text }: { els: Els; text: string }): RenderElement {
  const { Text } = els
  return <Text dimColor>{text}</Text>
}
