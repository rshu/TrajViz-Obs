import { describe, expect, test } from 'bun:test'
import { SNAPSHOT } from '../../hooks/testdata/snapshot.js'
import { charts } from '../src/charts.js'

function source() {
  const snapshot = structuredClone(SNAPSHOT)
  snapshot.series = {
    tokens: { steps: [], fresh: [], cache_read: [], cache_write: [], output: [] },
    durations: { steps: [], seconds: [], outliers: [] },
    context: { turns: [], occupancy: [], compactions: [] },
  }
  snapshot.context = { ...snapshot.context, window: 100, window_source: 'engine', window_note: null }
  snapshot.swimlane = { total_steps: 0, lanes: [] }
  snapshot.tools.by_tool = []
  return snapshot
}

describe('shared measured chart data', () => {
  test('numeric work-step bins align every chart, including uneven integer boundaries', () => {
    const snapshot = source()
    snapshot.swimlane.total_steps = 10
    snapshot.series.tokens = { steps: [3, 6], fresh: [1, 2], cache_read: [0, 0], cache_write: [0, 0], output: [0, 0] }
    snapshot.series.durations = { steps: [3, 6], seconds: [1, 2], outliers: [] }
    snapshot.series.context = { turns: [3, 6], occupancy: [10, 20], compactions: [] }
    const model = charts(snapshot, 3)
    expect(model.axis).toEqual({ start: 0, end: 9 })
    for (const bins of [model.tokens.bins, model.durations.bins, model.context.bins]) {
      expect(bins.map(bin => [bin.start, bin.end])).toEqual([[0, 2], [3, 5], [6, 9]])
      expect(bins.map(bin => bin.samples)).toEqual([0, 1, 1])
    }
    snapshot.timing.wall_clock_s = 999999
    expect(charts(snapshot, 3)).toEqual(model)
  })

  test('a token stack is one actual peak sample, never independently maximized components', () => {
    const snapshot = source()
    snapshot.series.tokens = { steps: [0, 1], fresh: [100, 0], cache_read: [0, 90], cache_write: [0, 0], output: [0, 0] }
    const model = charts(snapshot, 1)
    expect(model.tokens.bins[0]).toMatchObject({ step: 0, fresh: 100, cache_read: 0, total: 100, knownTotal: 100, samples: 2, unknown: 0 })
    expect(model.tokens.max).toBe(100)
    expect(model.tokens.unit).toBe('tokens')
    expect(model.tokens.sourcePoints).toBe(2)
  })

  test('partial token components, all-unknown samples, no sample, and measured zero remain distinct', () => {
    const snapshot = source()
    snapshot.swimlane.total_steps = 4
    snapshot.series.tokens = { steps: [0, 1, 3], fresh: [0, 7, null], cache_read: [0, 0, null], cache_write: [0, null, null], output: [0, null, null] }
    const { bins, max } = charts(snapshot, 4).tokens
    expect(bins[0]).toMatchObject({ step: 0, total: 0, knownTotal: 0, samples: 1, unknown: 0 })
    expect(bins[1]).toMatchObject({ step: 1, fresh: 7, total: null, knownTotal: 7, samples: 1, unknown: 1 })
    expect(bins[2]).toMatchObject({ step: null, total: null, knownTotal: null, samples: 0, unknown: 0 })
    expect(bins[3]).toMatchObject({ step: 3, total: null, knownTotal: null, samples: 1, unknown: 1 })
    expect(max).toBe(7)
  })

  test('token totals describe only plotted components and do not invent reasoning or billed usage', () => {
    const snapshot = source()
    snapshot.tokens.total = 900000
    snapshot.tokens.reasoning = 800000
    snapshot.series.tokens = { steps: [0], fresh: [1], cache_read: [2], cache_write: [3], output: [4] }
    expect(charts(snapshot).tokens.bins[0]?.total).toBe(10)
  })

  test('duration maxima retain real seconds and exact outlier steps separately', () => {
    const snapshot = source()
    snapshot.swimlane.total_steps = 10
    snapshot.series.durations = { steps: [0, 1, 4, 8], seconds: [0, 2, 9, null], outliers: [0, 7, 7] }
    const model = charts(snapshot, 3).durations
    expect(model.unit).toBe('seconds')
    expect(model.max).toBe(9)
    expect(model.bins[0]).toMatchObject({ step: 1, seconds: 2, outlier: true, outliers: [0] })
    expect(model.bins[1]).toMatchObject({ step: 4, seconds: 9, outlier: false, outliers: [] })
    expect(model.bins[2]).toMatchObject({ step: 8, seconds: null, samples: 1, unknown: 1, outlier: true, outliers: [7] })
  })

  test('context bins keep the last sample, true extrema, and compactions at canonical steps', () => {
    const snapshot = source()
    snapshot.swimlane.total_steps = 10
    snapshot.series.context = { turns: [0, 1, 4, 8], occupancy: [0, 90, 20, null], compactions: [0, 4, 6] }
    const model = charts(snapshot, 3).context
    expect(model.unit).toBe('percent')
    expect(model.max).toBe(90)
    expect(model.bins[0]).toMatchObject({ step: 1, percent: 90, min: 0, max: 90, compactions: [0] })
    expect(model.bins[1]).toMatchObject({ step: 4, percent: 20, min: 20, max: 20, compactions: [4] })
    expect(model.bins[2]).toMatchObject({ step: 8, percent: null, min: null, max: null, compactions: [6], unknown: 1 })
  })

  test('a missing or zero context window cannot become a percentage or use the latest card as history', () => {
    const snapshot = source()
    snapshot.context.current_pct = 71
    snapshot.series.context = { turns: [0, 1], occupancy: [0, 200], compactions: [] }
    for (const window of [null, 0, Number.NaN, Number.POSITIVE_INFINITY, -1]) {
      snapshot.context.window = window
      const model = charts(snapshot).context
      expect(model.window).toBeNull()
      expect(model.max).toBeNull()
      expect(model.bins.every(bin => bin.percent === null && bin.unknown === 1)).toBe(true)
    }
  })

  test('context values above capacity and fractional seconds remain observed values', () => {
    const snapshot = source()
    snapshot.context.window = 20
    snapshot.context.window_source = 'default'
    snapshot.context.window_note = 'Assumed engine window'
    snapshot.series.context = { turns: [0], occupancy: [30], compactions: [] }
    snapshot.series.durations = { steps: [0], seconds: [0.125], outliers: [] }
    const model = charts(snapshot)
    expect(model.context.bins[0]?.percent).toBe(150)
    expect(model.context.max).toBe(150)
    expect(model.context.windowSource).toBe('default')
    expect(model.context.windowNote).toBe('Assumed engine window')
    expect(model.durations.bins[0]?.seconds).toBe(0.125)
  })

  test('empty data never becomes a measured zero; observed zeros retain a zero scale', () => {
    const snapshot = source()
    const empty = charts(snapshot)
    expect([empty.tokens.max, empty.durations.max, empty.context.max, empty.toolMax]).toEqual([null, null, null, null])
    snapshot.series.tokens = { steps: [0], fresh: [0], cache_read: [0], cache_write: [0], output: [0] }
    snapshot.series.durations = { steps: [0], seconds: [0], outliers: [] }
    snapshot.series.context = { turns: [0], occupancy: [0], compactions: [] }
    snapshot.tools.by_tool = [{ tool: 'Complete', calls: 0, errors: 0 }]
    const zero = charts(snapshot)
    expect([zero.tokens.max, zero.durations.max, zero.context.max, zero.toolMax]).toEqual([0, 0, 0, 0])
  })

  test('tool bars rank by observed calls, keep outcome gaps, and bound rows', () => {
    const snapshot = source()
    snapshot.tools.by_tool = Array.from({ length: 12 }, (_, calls) => ({ tool: `Tool ${calls}`, calls, errors: calls === 11 ? null : 0, unknown: calls === 11 ? 1 : 0 }))
    const model = charts(snapshot)
    expect(model.tools).toHaveLength(8)
    expect(model.tools[0]).toEqual({ name: 'Tool 11', calls: 11, errors: null, unknown: 1 })
    expect(model.tools.map(row => row.calls)).toEqual([11, 10, 9, 8, 7, 6, 5, 4])
    expect(model.toolMax).toBe(11)
  })

  test('activity counts unioned source work-step ranges without double counting or treating user lanes as agents', () => {
    const snapshot = source()
    snapshot.swimlane = { total_steps: 10, lanes: [
      { id: 'main', label: 'Main', segments: [{ start: 0, end: 2 }, { start: 2, end: 5 }, { start: 0, end: 0 }] },
      { id: 'child', label: 'Child with recorded user prompt', segments: [{ start: 1, end: 1 }] },
      { id: '__user__', label: 'User', segments: [{ start: 0, end: 0 }] },
    ] }
    const model = charts(snapshot, 3)
    expect(model.agents[0]).toMatchObject({ kind: 'agent', segments: [{ start: 0, end: 5 }], bins: [3, 3, 0] })
    expect(model.agents[1]?.bins).toEqual([1, 0, 0])
    expect(model.agents[2]).toMatchObject({ kind: 'user', bins: [1, 0, 0] })
  })

  test('large sparse step ids, lane counts, and segment metadata stay bounded without dense allocation', () => {
    const snapshot = source()
    snapshot.swimlane = { total_steps: 1_000_000_001, lanes: Array.from({ length: 10 }, (_, id) => ({
      id: String(id), label: `Agent ${id}`, segments: id === 0 ? [{ start: 0, end: 1_000_000_000 }]
        : Array.from({ length: 300 }, (_, index) => ({ start: index * 2, end: index * 2 })),
    })) }
    snapshot.series.durations = { steps: [1_000_000_000], seconds: [2], outliers: [] }
    const model = charts(snapshot, 10000)
    expect(model.axis.end).toBe(1_000_000_000)
    expect(model.durations.bins).toHaveLength(160)
    expect(model.agents).toHaveLength(8)
    expect(model.omittedLanes).toBe(2)
    expect(model.agents[0]!.bins.reduce((a, b) => a + b, 0)).toBe(1_000_000_001)
    expect(model.agents[1]!.segments).toHaveLength(240)
    expect(model.agents[1]!.omittedSegments).toBe(60)
    expect(model.agents[1]!.bins.reduce((a, b) => a + b, 0)).toBe(300)
    expect(charts(snapshot, 0).durations.bins).toHaveLength(1)
    expect(charts(snapshot, Number.NaN).durations.bins).toHaveLength(80)
  })

  test('large source arrays do not exceed function argument limits or expand the rendered model', () => {
    const snapshot = source()
    const length = 100000
    snapshot.series.durations = { steps: Array.from({ length }, (_, index) => index), seconds: Array(length).fill(0), outliers: [length - 1] }
    const model = charts(snapshot, 32)
    expect(model.axis.end).toBe(length - 1)
    expect(model.durations.sourcePoints).toBe(length)
    expect(model.durations.bins).toHaveLength(32)
    expect(model.durations.max).toBe(0)
    expect(model.durations.bins.reduce((total, bin) => total + bin.samples, 0)).toBe(length)
    expect(model.durations.bins.at(-1)?.outliers).toEqual([length - 1])
  })

  test('invalid measurements remain unknown, duplicate steps do not duplicate work, and inputs stay unchanged', () => {
    const snapshot = source()
    snapshot.series.tokens = { steps: [6, 3, 3, -1, 1.5], fresh: [Number.NaN, 2, 99, 0, 0], cache_read: [0, 0, 0, 0, 0], cache_write: [0, 0, 0, 0, 0], output: [-1, 0, 0, 0, 0] }
    snapshot.series.durations = { steps: [0], seconds: [Number.POSITIVE_INFINITY], outliers: [] }
    snapshot.tools.by_tool = [{ tool: 'Invalid outcome', calls: 1, errors: -1, unknown: Number.NaN }]
    const before = structuredClone(snapshot)
    const model = charts(snapshot)
    expect(model.tokens.sourcePoints).toBe(2)
    expect(model.tokens.bins.find(bin => bin.step === 3)).toMatchObject({ fresh: 2, knownTotal: 2 })
    expect(model.tokens.bins.find(bin => bin.step === 6)).toMatchObject({ fresh: null, output: null, total: null })
    expect(model.durations.max).toBeNull()
    expect(model.tools[0]).toMatchObject({ errors: null, unknown: null })
    expect(snapshot).toEqual(before)
  })
})
