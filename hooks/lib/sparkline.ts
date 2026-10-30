// Text sparklines and bars in block glyphs, for surfaces without a Raster
// and for tables.

const LEVELS = '▁▂▃▄▅▆▇█'
const EIGHTHS = ' ▏▎▍▌▋▊▉'
const NULL_GLYPH = '·'

/**
 * `values` downsampled to at most `width` points: each point is the maximum
 * of its bucket of consecutive values (nulls skipped; an all-null bucket
 * stays null). Shorter input is returned as is.
 */
export function fit(values: (number | null)[], width: number): (number | null)[] {
  if (width <= 0) return []
  if (values.length <= width) return values.slice()
  const out: (number | null)[] = []
  for (let i = 0; i < width; i++) {
    const start = Math.floor((i * values.length) / width)
    const end = Math.floor(((i + 1) * values.length) / width)
    let max: number | null = null
    for (let j = start; j < end; j++) {
      const v = values[j]
      if (typeof v === 'number' && Number.isFinite(v) && (max === null || v > max)) max = v
    }
    out.push(max)
  }
  return out
}

/** The largest finite value, at least 0. */
export function maxOf(values: (number | null)[]): number {
  let max = 0
  for (const v of values) if (v !== null && Number.isFinite(v) && v > max) max = v
  return max
}

/**
 * One glyph per fitted value, scaled from 0 (`▁`) to the maximum (`█`) as
 * the rasters are, `·` for null. No padding: the string is as long as the
 * fitted series.
 */
export function sparkline(values: (number | null)[], width: number): string {
  const fitted = fit(values, width)
  const max = maxOf(fitted)
  let out = ''
  for (const v of fitted) {
    if (v === null || !Number.isFinite(v)) out += NULL_GLYPH
    else out += LEVELS[max > 0 ? Math.round((Math.max(0, v) / max) * 7) : 0]
  }
  return out
}

/** A horizontal bar `width` cells wide: full blocks, an eighth-block end, spaces after. */
export function bar(value: number, max: number, width: number): string {
  if (width <= 0) return ''
  const ratio = max > 0 && Number.isFinite(value) ? Math.min(1, Math.max(0, value / max)) : 0
  const eighths = Math.round(ratio * width * 8)
  const full = Math.floor(eighths / 8)
  const rest = eighths - full * 8
  const body = '█'.repeat(full) + (full < width ? (EIGHTHS[rest] as string) : '')
  return body.padEnd(width, ' ')
}
