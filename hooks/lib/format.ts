// Number and text formatting for cards, tables and the status line.

const UNKNOWN = '?'

/** An integer with thousands separators: `1,234,567`; `?` when not finite. */
export function fmtInt(n: number): string {
  if (!Number.isFinite(n)) return UNKNOWN
  return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ',')
}

/** A count in at most four characters: `999`, `1.2k`, `12k`, `3.4M`, `2.5G`; `?` when not finite. */
export function fmtCompact(n: number): string {
  if (!Number.isFinite(n)) return UNKNOWN
  const sign = n < 0 ? '-' : ''
  let v = Math.abs(n)
  if (v < 999.5) return sign + fmtInt(v)
  let unit = 0
  const units = ['', 'k', 'M', 'G', 'T']
  while (v >= 999.5 && unit < units.length - 1) {
    v /= 1000
    unit++
  }
  const s = v.toFixed(v < 9.95 ? 1 : 0).replace(/\.0$/, '')
  return `${sign}${s}${units[unit]}`
}

/** A file size: `512 B`, `1.0 KB`, `20 KB`, `1.2 MB`, `5.0 GB` (1024-based, one decimal under 10); `?` when not finite. */
export function fmtBytes(n: number): string {
  if (!Number.isFinite(n)) return UNKNOWN
  let v = Math.max(0, n)
  if (v < 1024) return `${Math.round(v)} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let unit = 0
  v /= 1024
  while (v >= 1023.5 && unit < units.length - 1) {
    v /= 1024
    unit++
  }
  return `${v < 9.95 ? v.toFixed(1) : String(Math.round(v))} ${units[unit]}`
}

/** A duration: `3.1s`, `46s`, `4m05s`, `1h02m`; `?` for null or not finite. */
export function fmtSecs(s: number | null): string {
  if (s === null || !Number.isFinite(s)) return UNKNOWN
  const v = Math.max(0, s)
  if (v < 9.95) return `${v.toFixed(1)}s`
  // Round the whole duration first so 59.5 carries into `1m00s`, never `0m60s`.
  const t = Math.round(v)
  if (t < 60) return `${t}s`
  const minutes = Math.floor(t / 60)
  if (minutes < 60) return `${minutes}m${String(t - minutes * 60).padStart(2, '0')}s`
  const hours = Math.floor(minutes / 60)
  return `${hours}h${String(minutes - hours * 60).padStart(2, '0')}m`
}

/** A percentage already in 0–100: `12%`, or `12.3%` with `digits`; `?` for null or not finite. */
export function fmtPct(p: number | null, digits = 0): string {
  if (p === null || !Number.isFinite(p)) return UNKNOWN
  return `${p.toFixed(digits)}%`
}

/** The first `n` characters of `s`, the last one an ellipsis when it was cut. */
export function clip(s: string, n: number): string {
  if (s.length <= n) return s
  if (n <= 0) return ''
  return `${s.slice(0, n - 1)}…`
}
