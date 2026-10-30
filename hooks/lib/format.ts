// Number and text formatting for cards, tables and the status line.
import type { RenderElement } from 'claude-code'
import type { Snapshot } from '../../types'

const UNKNOWN = '?'

/** An integer with thousands separators: `1,234,567`; `?` when not finite. */
export function fmtInt(n: number | null | undefined): string {
  if (typeof n !== 'number' || !Number.isFinite(n)) return UNKNOWN
  return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ',')
}

/** A count in at most four characters: `999`, `1.2k`, `12k`, `3.4M`, `2.5G`; `?` when not finite. */
export function fmtCompact(n: number | null | undefined): string {
  if (typeof n !== 'number' || !Number.isFinite(n)) return UNKNOWN
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
export function fmtSecs(s: number | null | undefined): string {
  if (typeof s !== 'number' || !Number.isFinite(s)) return UNKNOWN
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
export function fmtPct(p: number | null | undefined, digits = 0): string {
  if (typeof p !== 'number' || !Number.isFinite(p)) return UNKNOWN
  return `${p.toFixed(digits)}%`
}

/** A measured total requires every component; an absent component is never zero. */
export function sumMeasured(values: readonly (number | null | undefined)[]): number | null {
  if (!values.length || values.some(value => typeof value !== 'number' || !Number.isFinite(value))) return null
  return (values as readonly number[]).reduce((sum, value) => sum + value, 0)
}

/** Known failures and unrecorded outcomes remain separate, including a true zero. */
export function failureSummary(errors: number | null, unknown = 0): string {
  if (unknown > 0) return `${errors === null ? '?' : fmtInt(errors)} observed failures · ${fmtInt(unknown)} unknown outcome${unknown === 1 ? '' : 's'}`
  return errors === null ? 'failures unknown' : `${fmtInt(errors)} failed`
}

export function unavailableView(snap: Snapshot, view: string): boolean {
  return snap.availability?.[view]?.status === 'unavailable'
}

/** A short evidence note; full field counts and reasons stay in the existing data-quality view. */
export function qualitySummary(snap: Snapshot): string | null {
  const fields = Object.values(snap.data_quality?.fields ?? {})
  const notes: string[] = []
  if (Object.values(snap.tokens).some(value => value === null)) notes.push('usage incomplete')
  if ((snap.tools.unknown ?? 0) > 0) notes.push(`${fmtInt(snap.tools.unknown)} unknown tool outcome${snap.tools.unknown === 1 ? '' : 's'}`)
  else if (snap.tools.failed === null || snap.tools.success_rate === null && snap.tools.calls > 0) notes.push('tool outcomes incomplete')
  const missing = fields.filter(field => field.status === 'unavailable').length
  const unknown = fields.filter(field => field.status === 'unknown').length
  const partial = fields.filter(field => field.status === 'partial').length
  if (missing) notes.push(`${fmtInt(missing)} field${missing === 1 ? '' : 's'} unavailable`)
  if (unknown) notes.push(`${fmtInt(unknown)} field${unknown === 1 ? '' : 's'} unknown`)
  if (partial) notes.push(`${fmtInt(partial)} field${partial === 1 ? '' : 's'} partial`)
  if (!notes.length && !snap.data_quality) return null
  return notes.length ? `Data quality: ${notes.join(' · ')}` : 'Data quality: capture completeness unknown'
}

/** The first `n` characters of `s`, the last one an ellipsis when it was cut. */
export function clip(s: string, n: number): string {
  if (s.length <= n) return s
  if (n <= 0) return ''
  return `${s.slice(0, n - 1)}…`
}

/** Plain display text: keep Unicode, tabs and line breaks, without terminal commands. */
export function displayText(text: string): string {
  return text
    // OSC hyperlinks/titles, including a sequence cut short by a preview limit.
    .replace(/(?:\u001b\]|\u009d)[\s\S]*?(?:\u0007|\u001b\\|\u009c|$)/g, '')
    // DCS, SOS, PM and APC string commands end at ST, rather than a printable byte.
    .replace(/(?:\u001b[PX^_]|[\u0090\u0098\u009e\u009f])[\s\S]*?(?:\u001b\\|\u009c|$)/g, '')
    .replace(/(?:\u001b\[|\u009b)[0-?]*[ -/]*(?:[@-~]|$)/g, '')
    .replace(/\u001b[ -/]*[0-~]?/g, '')
    .replace(/\r\n?/g, '\n')
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/g, '')
}

/** Sanitize only rendered text; keep source data, action handles and element addresses intact. */
export function safeRenderText(tree: RenderElement): RenderElement {
  let node = tree
  if (node.type === 'Markdown') node = { ...node, props: { ...node.props, text: displayText(node.props.text) } }
  else if (node.type === 'Code') node = { ...node, props: { ...node.props, source: displayText(node.props.source) } }
  else if (node.type === 'Button') node = { ...node, props: { ...node.props, label: displayText(node.props.label) } }
  if ('children' in node && node.children !== undefined) {
    return { ...node, children: node.children.map(child => typeof child === 'string' ? displayText(child) : safeRenderText(child)) } as RenderElement
  }
  return node
}
