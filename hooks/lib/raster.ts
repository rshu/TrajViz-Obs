// Cells for the terminal's `Raster` element: colored block glyphs packed as
// RasterProps asks (row-major little-endian u32 triplets, standard base64).
import { fit, maxOf } from './sparkline'

/** Bit 24 alone: the terminal's own default colour. */
export const DEFAULT_COLOR = 0x01000000

export type Cell = { cp: number; fg: number; bg: number }

export type RasterCells = { columns: number; rows: number; cells: string }

const FULL = 0x2588
const SPACE = 0x20
const MIDDLE_DOT = 0xb7
/** Lower blocks by eighths: index 1 is `▁`, 7 is `▇`. */
const LOWER = [SPACE, 0x2581, 0x2582, 0x2583, 0x2584, 0x2585, 0x2586, 0x2587, FULL]
const B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'

const blank = (): Cell => ({ cp: SPACE, fg: DEFAULT_COLOR, bg: DEFAULT_COLOR })

/** `cells` row-major as `[codePoint, fg, bg]` little-endian u32 triplets, standard padded base64. */
export function encodeCells(cells: Cell[]): string {
  const bytes = new Uint8Array(cells.length * 12)
  const view = new DataView(bytes.buffer)
  cells.forEach((c, i) => {
    view.setUint32(i * 12, c.cp >>> 0, true)
    view.setUint32(i * 12 + 4, c.fg >>> 0, true)
    view.setUint32(i * 12 + 8, c.bg >>> 0, true)
  })
  let out = ''
  for (let i = 0; i < bytes.length; i += 3) {
    const b0 = bytes[i] as number
    const b1 = bytes[i + 1]
    const b2 = bytes[i + 2]
    const n = (b0 << 16) | ((b1 ?? 0) << 8) | (b2 ?? 0)
    out += B64[(n >> 18) & 63]
    out += B64[(n >> 12) & 63]
    out += b1 === undefined ? '=' : B64[(n >> 6) & 63]
    out += b2 === undefined ? '=' : B64[n & 63]
  }
  return out
}

/** A blank grid, row-major. */
function grid(columns: number, rows: number): Cell[] {
  return Array.from({ length: columns * rows }, blank)
}

const at = (columns: number, col: number, row: number): number => row * columns + col

/**
 * A column chart: `values` fitted to `columns`, each a bar of `height / max * rows`
 * cells in `fg`, full blocks below the level and a partial block (`▁`…`▇`)
 * on its top row. A value of 0 keeps a hairline, a null is a middle dot on
 * the bottom row, and columns past the series stay blank.
 */
export function sparklineRaster(values: (number | null)[], columns: number, rows: number, fg: number): RasterCells {
  columns = Math.max(1, Math.floor(columns))
  rows = Math.max(1, Math.floor(rows))
  const cells = grid(columns, rows)
  const fitted = fit(values, columns)
  const max = maxOf(fitted)
  fitted.forEach((v, col) => {
    if (v === null || !Number.isFinite(v)) {
      cells[at(columns, col, rows - 1)] = { cp: MIDDLE_DOT, fg: DEFAULT_COLOR, bg: DEFAULT_COLOR }
      return
    }
    const eighths = Math.max(1, Math.round((max > 0 ? Math.max(0, v) / max : 0) * rows * 8))
    for (let i = 0; i < rows; i++) {
      const filled = Math.min(8, eighths - i * 8)
      if (filled <= 0) break
      cells[at(columns, col, rows - 1 - i)] = { cp: LOWER[filled] as number, fg, bg: DEFAULT_COLOR }
    }
  })
  return { columns, rows, cells: encodeCells(cells) }
}

/**
 * A stacked column chart: each stack's `values` (one per column, fitted to
 * `columns` by bucket max) sits on the ones before it, scaled so the tallest
 * total fills `rows`. Each cell takes the colour of the segment that owns
 * its lowest filled eighth; the top of a column is a partial block.
 */
export function stackedRaster(stacks: { values: (number | null)[]; fg: number }[], columns: number, rows: number): RasterCells {
  columns = Math.max(1, Math.floor(columns))
  rows = Math.max(1, Math.floor(rows))
  const cells = grid(columns, rows)
  const length = Math.max(0, ...stacks.map(stack => stack.values.length))
  const count = Math.min(columns, length)
  // Keep one actual complete stack per bucket. Independent segment maxima
  // can invent a total that was never observed, and null segments cannot be zero.
  const fitted: (number[] | null)[] = []
  for (let col = 0; col < count; col++) {
    let selected: number[] | null = null
    let largest = -1
    for (let i = Math.floor(col * length / count); i < Math.floor((col + 1) * length / count); i++) {
      const values = stacks.map(stack => stack.values[i])
      if (values.some(value => typeof value !== 'number' || !Number.isFinite(value))) continue
      const measured = (values as number[]).map(value => Math.max(0, value))
      const total = measured.reduce((sum, value) => sum + value, 0)
      if (total > largest) { largest = total; selected = measured }
    }
    fitted.push(selected)
  }
  const totals = fitted.map(values => values?.reduce((sum, value) => sum + value, 0) ?? null)
  const max = maxOf(totals)
  for (let col = 0; col < count; col++) {
    const values = fitted[col]
    if (!values) {
      cells[at(columns, col, rows - 1)] = { cp: MIDDLE_DOT, fg: DEFAULT_COLOR, bg: DEFAULT_COLOR }
      continue
    }
    if (totals[col] === 0) {
      cells[at(columns, col, rows - 1)] = { cp: LOWER[1]!, fg: stacks[0]?.fg ?? DEFAULT_COLOR, bg: DEFAULT_COLOR }
      continue
    }
    // The boundaries between segments, in eighths from the bottom.
    const tops: number[] = []
    let cum = 0
    for (const value of values) {
      cum += value
      tops.push(Math.round((cum / max) * rows * 8))
    }
    const total = tops[tops.length - 1] ?? 0
    for (let i = 0; i < rows; i++) {
      const base = i * 8
      const filled = Math.min(8, total - base)
      if (filled <= 0) break
      const owner = tops.findIndex(t => t > base)
      const fg = (stacks[owner] ?? stacks[stacks.length - 1])?.fg ?? DEFAULT_COLOR
      cells[at(columns, col, rows - 1 - i)] = { cp: LOWER[filled] as number, fg, bg: DEFAULT_COLOR }
    }
  }
  return { columns, rows, cells: encodeCells(cells) }
}

/**
 * One row per lane over `columns` cells for `totalSteps` steps: each
 * segment (`start`..`end`, inclusive step indices) is full blocks in the
 * lane's colour, at least one cell wide. Step `i` owns the columns from
 * `i / totalSteps` up to (not including) `(i + 1) / totalSteps`, so a
 * segment over every step reaches the right edge and adjacent segments
 * leave no gap even when the steps are fewer than the columns.
 */
export function swimlaneRaster(
  lanes: { segments: { start: number; end: number }[]; fg: number }[],
  totalSteps: number,
  columns: number,
): RasterCells {
  columns = Math.max(1, Math.floor(columns))
  const rows = Math.max(1, lanes.length)
  const cells = grid(columns, rows)
  const span = Math.max(1, totalSteps)
  lanes.forEach((lane, row) => {
    for (const seg of lane.segments) {
      const lo = Math.min(seg.start, seg.end)
      const hi = Math.max(seg.start, seg.end)
      // Integer products before the division keep exact ratios exact (no `0.3 * 10` drift).
      const first = Math.max(0, Math.min(columns - 1, Math.floor((lo * columns) / span)))
      const last = Math.max(first, Math.min(columns - 1, Math.ceil(((hi + 1) * columns) / span) - 1))
      for (let col = first; col <= last; col++) cells[at(columns, col, row)] = { cp: FULL, fg: lane.fg, bg: DEFAULT_COLOR }
    }
  })
  return { columns, rows, cells: encodeCells(cells) }
}
