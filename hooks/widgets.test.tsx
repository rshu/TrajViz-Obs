import { expect, test } from 'claude-code/testing'
import type { RenderElement, RenderSurface } from 'claude-code'
import {
  DimLine,
  HealthChips,
  IssuesList,
  KpiRow,
  MdTable,
  PhaseRibbon,
  SparklineBlock,
  StackedBlock,
  Swimlane,
  ToolBars,
  WarningsLine,
  cacheEfficiency,
  seriesOf,
  statusColor,
  type Els,
} from './render/widgets'
import { SNAPSHOT } from './testdata/snapshot'

const SURFACES = ['terminal', 'desktop', 'vscode', 'mobile'] as const
const USAGE = { startedAt: 0, context: { window: 1_000_000, percent: 24 }, rateLimits: [], cost: { usd: 1.25 } }

/** A Pane instance no plugin hook draws: the test's own `ui.render` hook beneath the plugin draws the probe tree there. */
const pane = (bodyColumns: number) =>
  ({
    plugin: 'trajviz-obs',
    component: 'Pane',
    requestId: 'widgets-probe',
    props: { title: 'Widgets', isFocused: false, bodyColumns, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} },
  }) as const

/** Every widget off the fixture snapshot at `columns`, as a card draws them. */
function probe(els: Els, surface: RenderSurface, columns: number): RenderElement {
  const { Box } = els
  const s = SNAPSHOT
  const t = s.series.tokens
  return (
    <Box flexDirection="column">
      {HealthChips({ els, health: s.health })}
      {KpiRow({ els, snap: s, usage: USAGE, columns })}
      {StackedBlock({
        els,
        surface,
        label: 'tokens per step',
        key: 'tokens',
        columns: 40,
        rows: 4,
        stacks: [
          { name: 'fresh', values: t.fresh, color: 0x3b82f6 },
          { name: 'cache', values: t.cache_read, color: 0x10b981 },
          { name: 'out', values: t.output, color: 0xa855f7 },
        ],
      })}
      {SparklineBlock({ els, surface, label: 'step seconds', key: 'durations', values: s.series.durations.seconds, columns: 40, rows: 3, color: 0x3b82f6 })}
      {ToolBars({ els, byTool: s.tools.by_tool, width: columns })}
      {PhaseRibbon({ els, phases: s.phases, total: s.counts.assistant_steps, columns })}
      {Swimlane({ els, surface, lanes: s.swimlane.lanes, total: s.swimlane.total_steps, columns })}
      {IssuesList({ els, issues: s.issues, limit: 2 })}
      {WarningsLine({ els, warnings: s.trajviz.warnings })}
      {MdTable({ els, rows: s.agents, columns: ['label', 'steps', 'tokens'], limit: 1 })}
      {DimLine({ els, text: 'equivalent: /obs pane · /obs report' })}
    </Box>
  )
}

/** A found element's child descriptions (strings left out). */
type Found = { type?: string; text?: string; props?: Record<string, unknown>; children?: unknown[] }
const kids = (n: unknown): Found[] =>
  typeof n === 'object' && n !== null && Array.isArray((n as Found).children)
    ? ((n as Found).children as unknown[]).filter((c): c is Found => typeof c === 'object' && c !== null)
    : []

/** A child Text's string: its string children joined (a child description carries no `text`). */
const textOf = (n: Found): string => (Array.isArray(n.children) ? n.children.filter((c): c is string => typeof c === 'string').join('') : '')

/** The cells a tile Box takes: its widest Text. */
const tileCells = (tile: Found): number => Math.max(0, ...kids(tile).map(t => textOf(t).length))

/** The cells each KPI row (a row Box under the Box keyed `kpis`) takes: its tiles plus three between each pair. */
const kpiRowCells = (kpis: unknown): number[] => kids(kpis).map(row => kids(row).reduce((sum, tile, i) => sum + tileCells(tile) + (i > 0 ? 3 : 0), 0))

/** The cells of each Text on the ribbon (the Box keyed `phases`). */
const ribbonCells = (ribbon: unknown): number[] => kids(ribbon).map(t => textOf(t).length)

test('the widgets draw on every surface; Rasters only on the terminal', async ($, on) => {
  on('ui.render', { component: 'Pane', requestId: 'widgets-probe' }, ($, e) => probe($.ui.resolve(e), e.surface, e.props.bodyColumns))

  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ ...pane(100), surface })

    // Health chips take the theme's status colours.
    expect((await ui.find({ type: 'Text', text: /tool success/ }))?.props['color']).toBe('success')
    expect((await ui.find({ type: 'Text', text: /context/ }))?.props['color']).toBe('warning')

    // The KPI row: a tile is a dim label over its value (wide layout).
    const label = await ui.find({ type: 'Text', text: /Cache Efficiency/ })
    expect(label?.text).toBe('Cache Efficiency')
    expect(label?.props['dimColor']).toBe(true)
    expect(await ui.find({ type: 'Text', text: /^95%$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^311$/ })).toBeDefined()
    // Live context from usage, the snapshot's peak beside it; the cost from usage.
    expect(await ui.find({ type: 'Text', text: /^24%$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /peak 75%/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^\$1\.25$/ })).toBeDefined()
    // Every tile row fits the 100 columns, the tiles packed by their measured widths: the first row fills exactly 100.
    const rows = kpiRowCells(await ui.find({ key: 'kpis' }))
    expect(rows.length).toBe(2)
    expect(rows[0]).toBe(100)
    for (const w of rows) expect(w).toBeLessThanOrEqual(100)
    expect(kids(await ui.find({ key: 'kpis' })).reduce((n, row) => n + kids(row).length, 0)).toBe(8)

    if (surface === 'terminal') {
      const tokens = await ui.find({ key: 'tokens' })
      expect(tokens?.type).toBe('Raster')
      expect(tokens?.props).toMatchObject({ columns: 40, rows: 4 })
      expect((await ui.find({ key: 'durations' }))?.type).toBe('Raster')
      expect((await ui.find({ key: 'swimlane' }))?.type).toBe('Raster')
      expect(await ui.findAll({ type: 'Raster' })).toHaveLength(3)
    } else {
      expect(await ui.find({ key: 'tokens' })).toBeUndefined()
      expect(await ui.findAll({ type: 'Raster' })).toHaveLength(0)
      // The same series as block-glyph text instead.
      expect((await ui.findAll({ type: 'Text', text: /[▁▂▃▄▅▆▇█·]{20,}/ })).length).toBeGreaterThanOrEqual(2)
      expect(await ui.find({ type: 'Text', text: /^main\s+█/ })).toBeDefined()
    }
    // The stacked block's legend names each segment.
    expect(await ui.find({ type: 'Text', text: /fresh/ })).toBeDefined()

    // Tool bars: a name, a bar and the counts; errors in the error colour.
    expect(await ui.find({ type: 'Text', text: /^Bash/ })).toBeDefined()
    expect((await ui.find({ type: 'Text', text: /5 err/ }))?.props['color']).toBe('error')
    expect(await ui.find({ type: 'Text', text: /^█+/ })).toBeDefined()

    // The phase ribbon names each band.
    for (const name of ['explore', 'implement', 'verify']) expect(await ui.find({ type: 'Text', text: name })).toBeDefined()
    // The bands meet with no overlap and no gap: three Texts, 100 cells in all.
    const bands = ribbonCells(await ui.find({ key: 'phases' }))
    expect(bands).toEqual([13, 61, 26])

    // Issues to the limit: title, steps, evidence, dim why.
    expect(await ui.find({ type: 'Text', text: /Edit loop on hooks\/lib\/state\.ts/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /steps 140, 142, 144/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /Edit: hooks\/lib\/state\.ts/ })).toBeDefined()
    expect((await ui.find({ type: 'Text', text: /Run the test after each edit/ }))?.props['dimColor']).toBe(true)
    expect(await ui.find({ type: 'Text', text: /Compaction at 62%/ })).toBeUndefined()

    // Warnings in the warning colour.
    expect((await ui.find({ type: 'Text', text: /skipped sub-agent log/ }))?.props['color']).toBe('warning')

    // A Markdown table to the limit, the rest counted.
    const table = await ui.find({ type: 'Markdown' })
    expect(table?.text).toContain('| label | steps | tokens |')
    expect(table?.text).toContain('| main | 262 | 4120000 |')
    expect(table?.text).not.toContain('Builder check')
    expect(await ui.find({ type: 'Text', text: /1 more/ })).toBeDefined()

    const dim = await ui.find({ type: 'Text', text: /equivalent:/ })
    expect(dim?.text).toBe('equivalent: /obs pane · /obs report')
    expect(dim?.props['dimColor']).toBe(true)

    await ui.unmount()
  }
})

test('seriesOf draws the three series the card and the pane share, in order', async ($, on) => {
  on('ui.render', { component: 'Pane', requestId: 'widgets-probe' }, ($, e) => {
    const els = $.ui.resolve(e)
    return <els.Box flexDirection="column">{seriesOf({ els, surface: e.surface, snap: SNAPSHOT, columns: e.props.bodyColumns })}</els.Box>
  })

  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ ...pane(56), surface })
    // The labels, in order, each the dim line over its series; the markers named from the snapshot.
    const labels = (await ui.findAll({ type: 'Text', text: /^(tokens per step|step seconds|context occupancy)/ })).map(t => t.text)
    expect(labels).toEqual(['tokens per step', 'step seconds · outliers at steps 97, 233', 'context occupancy · compactions at turns 209'])
    // The stacked legend: the four segments in their colours.
    for (const [name, color] of [['fresh', '#3b82f6'], ['cache', '#10b981'], ['write', '#14b8a6'], ['out', '#a855f7']] as const) {
      expect((await ui.find({ type: 'Text', text: `■ ${name}` }))?.props['color']).toBe(color)
    }
    if (surface === 'terminal') {
      // Three Rasters at the given columns, keyed as both renderers address them.
      expect((await ui.find({ key: 'tokens' }))?.props).toMatchObject({ columns: 56, rows: 4 })
      expect((await ui.find({ key: 'durations' }))?.props).toMatchObject({ columns: 56, rows: 3 })
      expect((await ui.find({ key: 'context' }))?.props).toMatchObject({ columns: 56, rows: 3 })
      expect(await ui.findAll({ type: 'Raster' })).toHaveLength(3)
    } else {
      // Elsewhere block glyphs, the two single series in their colours.
      expect(await ui.findAll({ type: 'Raster' })).toHaveLength(0)
      const glyphs = await ui.findAll({ type: 'Text', text: /^[▁▂▃▄▅▆▇█·]{20,}$/ })
      expect(glyphs).toHaveLength(3)
      expect(glyphs.map(g => g.props['color'])).toEqual([undefined, '#f59e0b', '#ec4899'])
    }
    await ui.unmount()
  }
})

test('below 72 columns the KPIs are a list, one line each', async ($, on) => {
  on('ui.render', { component: 'Pane', requestId: 'widgets-probe' }, ($, e) => probe($.ui.resolve(e), e.surface, e.props.bodyColumns))
  const ui = await $.ui.mount({ ...pane(60), surface: 'terminal' })
  expect((await ui.find({ type: 'Text', text: /Cache Efficiency/ }))?.text).toMatch(/^Cache Efficiency\s+95%  \(113k written\)$/)
  expect(await ui.findAll({ type: 'Raster' })).toHaveLength(3)
  await ui.unmount()
})

test('the KPI tiles and the phase ribbon fit their columns at every breakpoint', async ($, on) => {
  on('ui.render', { component: 'Pane', requestId: 'widgets-probe' }, ($, e) => probe($.ui.resolve(e), e.surface, e.props.bodyColumns))
  const labels = ['Steps', 'Agents', 'Tokens', 'Cache Efficiency', 'Tools', 'Wall clock', 'Cost', 'Context']
  for (const columns of [72, 80, 99, 100, 132, 200]) {
    const ui = await $.ui.mount({ ...pane(columns), surface: 'terminal' })
    const kpis = await ui.find({ key: 'kpis' })
    const rows = kpiRowCells(kpis)
    for (const w of rows) expect(w).toBeLessThanOrEqual(columns)
    // Wide enough for every tile (125 cells plus seven gaps of 3), the tiles are one row; narrower, they wrap,
    // and tightly: a row ends only where the next tile would not have fit.
    expect(rows.length).toBe(columns >= 146 ? 1 : rows.length)
    const rowTiles = kids(kpis).map(row => kids(row).map(tileCells))
    rowTiles.forEach((tiles, i) => {
      const next = rowTiles[i + 1]?.[0]
      if (next !== undefined) expect((rows[i] ?? 0) + 3 + next).toBeGreaterThan(columns)
    })
    // Every tile is drawn once, in order.
    expect(kids(kpis).flatMap(row => kids(row).map(tile => textOf(kids(tile)[0] ?? {})))).toEqual(labels)
    // The ribbon is exactly `columns` wide, each phase one band, in order.
    const bands = ribbonCells(await ui.find({ key: 'phases' }))
    expect(bands.length).toBe(3)
    expect(bands.reduce((a, b) => a + b, 0)).toBe(columns)
    expect(kids(await ui.find({ key: 'phases' })).map(t => textOf(t).trim())).toEqual(['explore', 'implement', 'verify'])
    await ui.unmount()
  }
})

test('the phase ribbon leaves blank cells where no phase is, and never exceeds its columns', async ($, on) => {
  on('ui.render', { component: 'Pane', requestId: 'widgets-probe' }, ($, e) => {
    const els = $.ui.resolve(e)
    const { Box } = els
    return (
      <Box flexDirection="column">
        {PhaseRibbon({ els, phases: [{ name: 'plan', start: 10, end: 40 }, { name: 'build', start: 60, end: 100 }], total: 100, columns: 50 })}
      </Box>
    )
  })
  const ui = await $.ui.mount({ ...pane(50), surface: 'desktop' })
  const ribbon = await ui.find({ key: 'phases' })
  // A spacer before `plan` (steps 1-9), one between the phases (41-59), 50 cells in all.
  expect(ribbonCells(ribbon)).toEqual([5, 15, 10, 20])
  const texts = kids(ribbon)
  expect(textOf(texts[0] ?? {})).toBe('     ')
  expect(texts[0]?.props?.['backgroundColor']).toBeUndefined()
  expect(textOf(texts[1] ?? {}).trim()).toBe('plan')
  expect(texts[1]?.props?.['backgroundColor']).toBeDefined()
  expect(textOf(texts[3] ?? {}).trim()).toBe('build')
  await ui.unmount()
})

test('status colours, cache efficiency and the empty cases', async ($, on) => {
  expect(statusColor('ok')).toBe('success')
  expect(statusColor('good')).toBe('success')
  expect(statusColor('warn')).toBe('warning')
  expect(statusColor('bad')).toBe('error')
  expect(statusColor('error')).toBe('error')
  expect(statusColor('unknown')).toBeUndefined()
  expect(Math.round((cacheEfficiency(SNAPSHOT.tokens) ?? 0) * 10) / 10).toBe(95.1)
  expect(cacheEfficiency({ ...SNAPSHOT.tokens, input: 0, cache_read: 0, cache_write: 0 })).toBe(null)

  on('ui.render', { component: 'Pane', requestId: 'widgets-probe' }, ($, e) => {
    const els = $.ui.resolve(e)
    const { Box } = els
    return (
      <Box flexDirection="column">
        {WarningsLine({ els, warnings: [] })}
        {IssuesList({ els, issues: [], limit: 3 })}
        {ToolBars({ els, byTool: [], width: 40 })}
        {MdTable({ els, rows: [], columns: ['a'], limit: 5 })}
      </Box>
    )
  })
  const ui = await $.ui.mount({ ...pane(80), surface: 'desktop' })
  expect(await ui.findAll({ type: 'Text', text: /warning/i })).toHaveLength(0)
  expect(await ui.find({ type: 'Text', text: /No issues/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /No tool calls/ })).toBeDefined()
  expect(await ui.find({ type: 'Markdown' })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: /No rows/ })).toBeDefined()
  await ui.unmount()
})
