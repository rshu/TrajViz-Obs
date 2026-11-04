import type { Snapshot } from './types.js'

export type Axis = { start: number; end: number }
export type Bin = { start: number; end: number; samples: number; unknown: number }
export type TokenBin = Bin & {
  step: number | null
  fresh: number | null
  cache_read: number | null
  cache_write: number | null
  output: number | null
  /** Sum of all four plotted components, only when every component was recorded. */
  total: number | null
  /** Sum of recorded components; null when no component was recorded. */
  knownTotal: number | null
}
export type DurationBin = Bin & { step: number | null; seconds: number | null; outlier: boolean; outliers: number[] }
export type ContextBin = Bin & { step: number | null; percent: number | null; min: number | null; max: number | null; compactions: number[] }
export type ToolBar = { name: string; calls: number; errors: number | null; unknown: number | null }
export type ActivityLane = {
  id: string; label: string; kind: 'agent' | 'user'
  segments: { start: number; end: number }[]
  omittedSegments: number
  /** Recorded work steps in each bin; includes user work belonging to a source lane. */
  bins: number[]
}
export type Charts = {
  axis: Axis
  tokens: { bins: TokenBin[]; max: number | null; unit: 'tokens'; sourcePoints: number }
  durations: { bins: DurationBin[]; max: number | null; unit: 'seconds'; sourcePoints: number }
  context: { bins: ContextBin[]; max: number | null; unit: 'percent'; sourcePoints: number; window: number | null; windowSource: string; windowNote: string | null }
  tools: ToolBar[]
  toolMax: number | null
  agents: ActivityLane[]
  omittedLanes: number
}

const measured = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value) && value >= 0
const count = (value: unknown): number | null => measured(value) && Number.isSafeInteger(value) ? value : null
const step = (value: unknown): value is number => count(value) !== null && (value as number) < Number.MAX_SAFE_INTEGER
const numeric = (value: unknown): number | null => measured(value) ? value : null
const max = (values: (number | null)[]): number | null => {
  let maximum: number | null = null
  for (const value of values) if (value !== null && (maximum === null || value > maximum)) maximum = value
  return maximum
}
const sum = (values: (number | null)[]): number | null => {
  const known = values.filter((value): value is number => value !== null)
  if (!known.length) return null
  return count(known.reduce((total, value) => total + value, 0))
}

function points<T>(steps: number[], read: (index: number) => T): { step: number; value: T }[] {
  const unique = new Map<number, T>()
  steps.forEach((value, index) => { if (step(value) && !unique.has(value)) unique.set(value, read(index)) })
  return [...unique].sort(([a], [b]) => a - b).map(([step, value]) => ({ step, value }))
}

/** Equal work-step bins. This never allocates an array whose length is a step id. */
export function charts(snapshot: Snapshot, columns = 80): Charts {
  const tokenSource = snapshot.series.tokens, durationSource = snapshot.series.durations, contextSource = snapshot.series.context
  const tokenPoints = points(tokenSource.steps, index => ({
    fresh: count(tokenSource.fresh[index]), cache_read: count(tokenSource.cache_read[index]),
    cache_write: count(tokenSource.cache_write[index]), output: count(tokenSource.output[index]),
  }))
  const durationPoints = points(durationSource.steps, index => numeric(durationSource.seconds[index]))
  const window = measured(snapshot.context.window) && snapshot.context.window > 0 ? snapshot.context.window : null
  const contextPoints = points(contextSource.turns, index => {
    const value = numeric(contextSource.occupancy[index])
    return value !== null && window !== null ? numeric(value / window * 100) : null
  })
  const compactions = [...new Set(contextSource.compactions.filter(step))].sort((a, b) => a - b)
  const outliers = [...new Set(durationSource.outliers.filter(step))].sort((a, b) => a - b)
  const lanes = snapshot.swimlane.lanes.map(lane => ({ ...lane, segments: lane.segments.filter(
    range => step(range.start) && step(range.end) && range.end >= range.start) }))
  const axis = { start: 0, end: Math.max(0, (count(snapshot.swimlane.total_steps) ?? 0) - 1) }
  // A large imported capture must not exceed the engine's function-argument limit.
  for (const points of [tokenPoints, durationPoints, contextPoints]) {
    for (const point of points) axis.end = Math.max(axis.end, point.step)
  }
  for (const markers of [compactions, outliers]) for (const value of markers) axis.end = Math.max(axis.end, value)
  for (const lane of lanes) for (const range of lane.segments) axis.end = Math.max(axis.end, range.end)
  const span = axis.end + 1
  const size = Math.min(span, Math.max(1, Math.min(160, Number.isFinite(columns) ? Math.floor(columns) : 80)))
  const ranges: Bin[] = Array.from({ length: size }, (_, index) => ({
    start: Math.floor(span * index / size), end: Math.floor(span * (index + 1) / size) - 1, samples: 0, unknown: 0,
  }))
  const binIndex = (value: number) => {
    let low = 0, high = ranges.length - 1
    while (low < high) {
      const middle = Math.floor((low + high) / 2)
      if (value > ranges[middle]!.end) low = middle + 1
      else high = middle
    }
    return low
  }
  const tokens: TokenBin[] = ranges.map(range => ({ ...range, step: null, fresh: null, cache_read: null,
    cache_write: null, output: null, total: null, knownTotal: null }))
  for (const point of tokenPoints) {
    const bin = tokens[binIndex(point.step)]!, values = Object.values(point.value)
    const knownTotal = sum(values), complete = values.every(value => value !== null)
    bin.samples++
    if (!complete) bin.unknown++
    // Keep one real stack: independent channel maxima would invent a sample.
    if (bin.step === null || (knownTotal !== null && (bin.knownTotal === null || knownTotal > bin.knownTotal))) {
      Object.assign(bin, point.value, { step: point.step, knownTotal, total: complete ? knownTotal : null })
    }
  }
  const durations: DurationBin[] = ranges.map(range => ({ ...range, step: null, seconds: null, outlier: false, outliers: [] }))
  for (const point of durationPoints) {
    const bin = durations[binIndex(point.step)]!
    bin.samples++
    if (point.value === null) bin.unknown++
    if (bin.step === null || (point.value !== null && (bin.seconds === null || point.value > bin.seconds))) {
      bin.step = point.step; bin.seconds = point.value
    }
  }
  for (const value of outliers) { const bin = durations[binIndex(value)]!; bin.outlier = true; bin.outliers.push(value) }
  const context: ContextBin[] = ranges.map(range => ({ ...range, step: null, percent: null, min: null, max: null, compactions: [] }))
  for (const point of contextPoints) {
    const bin = context[binIndex(point.step)]!
    bin.samples++; bin.step = point.step; bin.percent = point.value
    if (point.value === null) bin.unknown++
    else { bin.min = Math.min(bin.min ?? point.value, point.value); bin.max = Math.max(bin.max ?? point.value, point.value) }
  }
  for (const value of compactions) context[binIndex(value)]!.compactions.push(value)
  const tools: ToolBar[] = snapshot.tools.by_tool.filter(row => count(row.calls) !== null)
    .map(row => ({ name: row.tool, calls: row.calls, errors: count(row.errors), unknown: row.unknown === undefined ? 0 : count(row.unknown) }))
    .sort((a, b) => b.calls - a.calls).slice(0, 8)
  const agents: ActivityLane[] = lanes.slice(0, 8).map(lane => {
    const merged: { start: number; end: number }[] = []
    for (const range of [...lane.segments].sort((a, b) => a.start - b.start)) {
      const last = merged.at(-1)
      if (last && range.start <= last.end + 1) last.end = Math.max(last.end, range.end)
      else merged.push({ start: range.start, end: range.end })
    }
    return { id: lane.id, label: lane.label, kind: lane.id === '__user__' ? 'user' : 'agent',
      segments: merged.slice(0, 240), omittedSegments: Math.max(0, merged.length - 240),
      bins: ranges.map(bin => merged.reduce((total, range) => total + Math.max(0,
        Math.min(bin.end, range.end) - Math.max(bin.start, range.start) + 1), 0)) }
  })
  return {
    axis, tokens: { bins: tokens, max: max(tokens.map(bin => bin.knownTotal)), unit: 'tokens', sourcePoints: tokenPoints.length },
    durations: { bins: durations, max: max(durations.map(bin => bin.seconds)), unit: 'seconds', sourcePoints: durationPoints.length },
    context: { bins: context, max: max(context.map(bin => bin.max)), unit: 'percent', sourcePoints: contextPoints.length,
      window, windowSource: snapshot.context.window_source, windowNote: snapshot.context.window_note },
    tools, toolMax: max(tools.map(tool => tool.calls)), agents, omittedLanes: Math.max(0, lanes.length - agents.length),
  }
}
