import type { Theme, ThemeColor } from '@earendil-works/pi-coding-agent'
import { Key, isKeyRelease, matchesKey, stripTerminalSequences, truncateToWidth, visibleWidth, wrapTextWithAnsi, type Component } from '@earendil-works/pi-tui'
import type { EmptySnapshot, Snapshot } from './types.js'

export const TABS = ['Overview', 'Tools', 'Issues', 'Patterns', 'Agents', 'Context', 'Timeline'] as const
const TAB_KEYS = ['1', '2', '3', '4', '5', '6', '7'] as const

export type PaneState = {
  snapshot?: Snapshot | EmptySnapshot
  /** Zero based, in TABS order. */
  tab: number
  busy?: boolean
  error?: string
  watch: boolean
  /** Saved/imported selections refresh manually without changing the watch preference. */
  watchPaused?: boolean
  dashboardUrl?: string
}

export type PaneActions = {
  refresh(): void
  dashboard(): void
  toggleWatch(): void
  close(): void
  changed(): void
  selectTab?(tab: number): void
}

/** Paints a complete rectangle so Pi cannot show fragments of chat around a screen. */
export class FramedComponent implements Component {
  readonly capturesViewportInput = true
  constructor(
    private readonly child: Component,
    private readonly theme: Theme,
    private readonly rows: () => number,
    private readonly footer = 'Esc close',
  ) {}

  invalidate(): void { this.child.invalidate() }
  handleInput(data: string): void { this.child.handleInput?.(data) }

  render(columns: number): string[] {
    if (columns < 1) return []
    const width = Math.floor(columns)
    const requested = this.rows()
    const height = Math.max(1, Number.isFinite(requested) ? Math.floor(requested) : 24)
    const fit = (line: string, available: number): string => {
      const clipped = truncateToWidth(line, available, '…')
      return clipped + ' '.repeat(Math.max(0, available - visibleWidth(clipped)))
    }
    if (width < 6 || height < 3) {
      const lines = this.child.render(width)
      return Array.from({ length: height }, (_, i) => fit(i === height - 1 ? color(this.theme, 'muted', this.footer) : lines[i] ?? '', width))
    }
    const ascii = process.env.TERM === 'dumb'
    const edge = (value: string) => color(this.theme, 'border', value)
    const horizontal = ascii ? '-' : '─', vertical = ascii ? '|' : '│'
    const contentWidth = width - 4
    const content = this.child.render(contentWidth)
    const result = [edge(`${ascii ? '+' : '╭'}${horizontal.repeat(width - 2)}${ascii ? '+' : '╮'}`)]
    for (let i = 0; i < height - 2; i++) result.push(`${edge(vertical)} ${fit(content[i] ?? '', contentWidth)} ${edge(vertical)}`)
    const label = truncateToWidth(width >= visibleWidth(this.footer) + 4 ? ` ${this.footer} ` : this.footer, width - 2, '…')
    const remaining = width - 2 - visibleWidth(label)
    result.push(edge(ascii ? '+' : '╰') + edge(horizontal.repeat(Math.floor(remaining / 2))) + color(this.theme, 'muted', label) + edge(horizontal.repeat(Math.ceil(remaining / 2))) + edge(ascii ? '+' : '╯'))
    return result
  }
}

/** Never let transcript content inject terminal control sequences into Pi. */
const text = (value: unknown): string => stripTerminalSequences(String(value ?? '')
  .replace(/(?:\u001b[PX^_]|[\u0090\u0098\u009e\u009f])[\s\S]*?(?:\u001b\\|\u009c|$)/g, ''))
  .replace(/\r\n?/g, '\n').replace(/\t/g, '  ')
  .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/g, '')
const number = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value)
const measured = (value: number | null | undefined): string => number(value) ? value.toLocaleString('en-US') : '—'
const compact = (value: number | null | undefined): string => {
  if (!number(value)) return '—'
  if (Math.abs(value) < 999.5) return String(Math.round(value))
  let v = value
  let unit = -1
  const units = ['k', 'M', 'G', 'T']
  while (Math.abs(v) >= 999.5 && unit < units.length - 1) { v /= 1000; unit++ }
  return `${v.toFixed(Math.abs(v) < 9.95 ? 1 : 0).replace(/\.0$/, '')}${units[unit]}`
}
const pct = (value: number | null | undefined): string => number(value) ? `${value.toFixed(value > 0 && value < 10 ? 1 : 0)}%` : '—'
const duration = (value: number | null | undefined): string => {
  if (!number(value)) return '—'
  const seconds = Math.max(0, value)
  if (seconds < 10) return `${seconds.toFixed(1)}s`
  const rounded = Math.round(seconds)
  if (rounded < 60) return `${rounded}s`
  const minutes = Math.floor(rounded / 60)
  return minutes < 60 ? `${minutes}m${String(rounded % 60).padStart(2, '0')}s` : `${Math.floor(minutes / 60)}h${String(minutes % 60).padStart(2, '0')}m`
}
const failures = (failed: number | null, unknown = 0): string => unknown > 0
  ? `${compact(failed)} observed failures · ${compact(unknown)} unknown outcomes`
  : failed === null ? 'failures unknown' : `${compact(failed)} failed`
const unavailable = (snap: Snapshot, key: string): boolean => snap.availability?.[key]?.status === 'unavailable'
const color = (theme: Theme, name: ThemeColor, value: string): string => theme.fg(name, value)
const tone = (status: string): ThemeColor => /^(error|bad|critical|failed)$/i.test(status) ? 'error'
  : /^(warn|warning|partial)$/i.test(status) ? 'warning'
    : /^(ok|good|success)$/i.test(status) ? 'success' : 'muted'

/** Bin maxima retain short spikes; missing observations remain visible gaps. */
export function sparkline(values: readonly (number | null | undefined)[], columns: number): string {
  if (!values.length) return 'No samples'
  const count = Math.max(1, Math.min(values.length, Math.floor(columns), 80))
  const bins = Array.from({ length: count }, (_, i) => {
    const observed = values.slice(Math.floor(i * values.length / count), Math.floor((i + 1) * values.length / count)).filter(number)
    return observed.length ? Math.max(0, ...observed) : null
  })
  const maximum = Math.max(0, ...bins.filter(number))
  const ascii = process.env.TERM === 'dumb'
  const blocks = ascii ? '_.,:;oO#' : '▁▂▃▄▅▆▇█'
  return bins.map(value => value === null ? '?' : blocks[maximum ? Math.min(7, Math.floor(value / maximum * 7)) : 0]).join('')
}

class Lines {
  readonly lines: string[] = []
  constructor(readonly width: number, readonly theme: Theme) {}
  add(value: unknown, name: ThemeColor = 'text'): void {
    for (const line of wrapTextWithAnsi(text(value), this.width)) this.lines.push(color(this.theme, name, truncateToWidth(line, this.width, '…')))
  }
  clip(value: unknown, name: ThemeColor = 'text'): void {
    this.lines.push(color(this.theme, name, truncateToWidth(text(value).replace(/\n/g, ' '), this.width, '…')))
  }
  heading(value: string): void {
    if (this.lines.length && this.lines[this.lines.length - 1] !== '') this.lines.push('')
    this.add(value, 'accent')
  }
}

function metrics(snap: Snapshot, out: Lines, compactMode = false): void {
  const pairs = [
    ['Steps', compact(snap.counts.assistant_steps)], ['Tools', compact(snap.tools.calls)],
    ['Tokens', compact(snap.tokens.total)], ['Elapsed', snap.timing.wall_clock_fmt ?? duration(snap.timing.wall_clock_s)],
    ['Agents', compact(snap.counts.agents)], ['Context', pct(snap.context.current_pct)],
  ]
  if (number(snap.engine_usage?.cost?.usd)) pairs.push(['Cost', `${snap.engine_usage.cost.source === 'estimate' ? '~' : ''}$${snap.engine_usage.cost.usd.toFixed(2)}`])
  const cell = out.width >= 76 ? 4 : out.width >= 40 ? 3 : out.width >= 25 ? 2 : 1
  const cellWidth = Math.max(1, Math.floor((out.width - (cell - 1) * 2) / cell))
  for (let i = 0; i < pairs.length; i += cell) {
    const row = pairs.slice(i, i + cell).map(([label, value]) => {
      const plain = truncateToWidth(`${label} ${value}`, cellWidth, '…')
      return color(out.theme, 'text', plain) + ' '.repeat(Math.max(0, cellWidth - visibleWidth(plain)))
    }).join('  ')
    out.lines.push(row.trimEnd())
  }
  const detail = `${compact(snap.counts.prompts)} prompts · ${compact(snap.counts.compactions)} compactions · ${pct(snap.tools.success_rate)} tool success · p95 ${duration(snap.timing.p95_step_s)}`
  if (compactMode) out.clip(detail, 'muted'); else out.add(detail, 'muted')
}

function quality(snap: Snapshot, out: Lines, compactMode = false): void {
  const notes: string[] = []
  if (Object.values(snap.tokens).some(value => value === null)) notes.push('usage incomplete')
  if (snap.tools.unknown) notes.push(`${compact(snap.tools.unknown)} unknown tool outcomes`)
  else if (snap.tools.failed === null || snap.tools.calls > 0 && snap.tools.success_rate === null) notes.push('tool outcomes incomplete')
  const fields = Object.values(snap.data_quality?.fields ?? {})
  for (const status of ['unavailable', 'unknown', 'partial']) {
    const count = fields.filter(field => field.status === status).length
    if (count) notes.push(`${count} fields ${status}`)
  }
  if (notes.length) {
    const line = `Data quality: ${notes.join(' · ')}. Unknown values use — / ?.`
    if (compactMode) out.clip(line, 'warning'); else out.add(line, 'warning')
  }
}

function trends(snap: Snapshot, out: Lines): void {
  const totals = snap.series.tokens.steps.map((_, i) => {
    const values = [snap.series.tokens.fresh[i], snap.series.tokens.cache_read[i], snap.series.tokens.cache_write[i], snap.series.tokens.output[i]]
    return values.every(number) ? values.reduce((sum, value) => sum + value, 0) : null
  })
  out.heading('Trends · earlier → later')
  for (const [label, values, shade] of [
    ['Tokens/step', totals, 'success'], ['Step time', snap.series.durations.seconds, 'warning'], ['Context', snap.series.context.occupancy, 'accent'],
  ] as const) {
    const observed = values.filter(number)
    const maximum = observed.length ? ` max ${label === 'Step time' ? duration(Math.max(...observed)) : compact(Math.max(...observed))}` : ''
    if (out.width >= 36) {
      const prefix = label.padEnd(12)
      const width = Math.max(1, out.width - visibleWidth(prefix) - visibleWidth(maximum))
      out.lines.push(color(out.theme, 'muted', prefix) + color(out.theme, shade, sparkline(values, width)) + color(out.theme, 'muted', maximum))
    } else {
      out.clip(`${label}${maximum}`, 'muted')
      out.clip(sparkline(values, out.width), shade)
    }
  }
}

function overview(snap: Snapshot, out: Lines, compactMode: boolean): void {
  metrics(snap, out, compactMode)
  quality(snap, out, compactMode)
  if (!compactMode && snap.health.length) {
    out.heading('Health')
    for (const row of snap.health) out.add(`${row.metric.replaceAll('_', ' ')}: ${row.label} · ${row.detail}`, tone(row.status))
  }
  out.heading('Top issues')
  if (!snap.issues.length) out.add(unavailable(snap, 'issues') ? 'Issues unavailable: required evidence was not recorded.' : 'No observed issues.', 'muted')
  for (const [i, issue] of snap.issues.slice(0, compactMode ? 1 : 3).entries()) out.clip(`${i + 1}. ${issue.title}`, 'warning')
  const total = snap.counts.issues ?? snap.issues.length
  if (total) {
    const line = `${compact(total)} issue${total === 1 ? '' : 's'} · ${compactMode ? '/obs pane' : 'press 3'} for evidence.`
    if (compactMode) out.clip(line, 'muted'); else out.add(line, 'muted')
  }
  if (!compactMode) {
    trends(snap, out)
    const phase = snap.phases.reduce<Snapshot['phases'][number] | undefined>((a, b) => !a || b.end >= a.end ? b : a, undefined)
    if (phase) out.add(`Latest phase: ${phase.name}`, 'muted')
  }
}

/** The optional editor widget stays compact; detailed diagnostics live in the pane. */
export function renderOverview(snapshot: Snapshot | EmptySnapshot | undefined, width: number, theme: Theme): string[] {
  if (width <= 0) return []
  const out = new Lines(Math.floor(width), theme)
  out.clip('TrajViz · /obs pane · /obs dashboard', 'accent')
  if (!snapshot || snapshot.empty) {
    out.clip(snapshot?.empty ? snapshot.reason : 'Waiting for session activity.', 'muted')
    return out.lines
  }
  if (width < 30) {
    out.clip(`${compact(snapshot.counts.assistant_steps)} steps · ${compact(snapshot.tools.calls)} tools`)
    out.clip(`${compact(snapshot.tokens.total)} tokens · ctx ${pct(snapshot.context.current_pct)}`)
    out.clip(`Elapsed ${snapshot.timing.wall_clock_fmt ?? duration(snapshot.timing.wall_clock_s)}`, 'muted')
    out.clip(`${compact(snapshot.counts.issues ?? snapshot.issues.length)} issues · /obs pane`, snapshot.issues.length ? 'warning' : 'muted')
    return out.lines
  }
  overview(snapshot, out, true)
  return out.lines
}

function tools(snap: Snapshot, out: Lines): void {
  out.heading('Tool calls')
  out.add(`${measured(snap.tools.calls)} calls · ${pct(snap.tools.success_rate)} success · ${failures(snap.tools.failed, snap.tools.unknown)}`)
  const maximum = Math.max(1, ...snap.tools.by_tool.map(row => row.calls))
  const barWidth = Math.max(1, Math.min(24, Math.floor(out.width / 3)))
  for (const row of snap.tools.by_tool) {
    out.add(`${row.tool} · ${measured(row.calls)} calls${row.errors || row.errors === null || row.unknown ? ` · ${failures(row.errors, row.unknown)}` : ''}`)
    out.clip((process.env.TERM === 'dumb' ? '#' : '━').repeat(Math.max(row.calls > 0 ? 1 : 0, Math.round(row.calls / maximum * barWidth))), row.errors ? 'warning' : 'success')
  }
  if (!snap.tools.by_tool.length) out.add('No tool calls recorded.', 'muted')
  out.heading('Files')
  if (unavailable(snap, 'file_interactions')) out.add('Unavailable: tool inputs were not recorded.', 'muted')
  else {
    if (snap.availability?.file_interactions?.status === 'partial') out.add('Partial evidence: counts reflect recorded activity.', 'warning')
    out.add(`${compact(snap.files.unique)} files · ${compact(snap.files.reads)} reads · ${compact(snap.files.writes)} writes · ${compact(snap.files.searches)} searches`)
    for (const file of snap.files.top) out.add(`${file.file}: ${file.reads} reads · ${file.writes} writes`, 'muted')
  }
  out.heading('Skills')
  for (const skill of snap.skills) out.add(`${skill.skill} · ${measured(skill.calls)} calls`)
  if (!snap.skills.length) out.add(unavailable(snap, 'skill_calls') ? 'Unavailable: tool inputs were not recorded.' : 'No skill calls recorded.', 'muted')
}

function issues(snap: Snapshot, out: Lines): void {
  out.heading('Issues')
  if (!snap.issues.length) out.add(unavailable(snap, 'issues') ? 'Unavailable: required evidence was not recorded.' : 'No observed issues.', 'muted')
  for (const [i, issue] of snap.issues.entries()) {
    out.heading(`${i + 1}. ${issue.title}`)
    out.add(`Steps: ${issue.steps.join(', ') || '—'}`, 'accent')
    if (issue.evidence) out.add(issue.evidence, 'muted')
    if (issue.detail) out.add(issue.detail)
    if (issue.why) out.add(`Suggestion: ${issue.why}`, 'success')
  }
  const omitted = (snap.counts.issues ?? snap.issues.length) - snap.issues.length
  if (omitted > 0) out.add(`${omitted} more issues · open Dashboard for the complete list.`, 'muted')
  out.heading('Bottlenecks')
  if (!snap.bottlenecks.length) out.add(unavailable(snap, 'bottlenecks') ? 'Unavailable: timing evidence is incomplete.' : 'No bottlenecks detected.', 'muted')
  for (const row of snap.bottlenecks) {
    out.add(`#${row.step} · ${duration(row.duration_s)} · ${row.title}`, 'warning')
    out.add(`${row.cause} · ${row.detail}`, 'muted')
  }
}

function recordLines(value: unknown): string[] {
  const printable = (item: unknown): string => item == null ? '—' : typeof item === 'object' ? JSON.stringify(item) : String(item)
  if (Array.isArray(value)) return value.slice(0, 8).flatMap((row, i) => row && typeof row === 'object' && !Array.isArray(row)
    ? [`${i + 1}. ${Object.entries(row).map(([key, item]) => `${key.replaceAll('_', ' ')}: ${printable(item)}`).join(' · ')}`]
    : [printable(row)])
  if (value && typeof value === 'object') return Object.entries(value).flatMap(([key, item]) => Array.isArray(item)
    ? item.length ? [`${key.replaceAll('_', ' ')}:`, ...recordLines(item)] : [] : [])
  return []
}

function patterns(snap: Snapshot, out: Lines): void {
  let found = false
  for (const [key, value] of Object.entries(snap.patterns)) {
    const lines = recordLines(value)
    if (!lines.length && !unavailable(snap, key)) continue
    found = true
    out.heading(key.replaceAll('_', ' '))
    for (const line of lines) out.add(line)
    if (!lines.length) out.add('Unavailable: required evidence was not recorded.', 'muted')
  }
  if (!found) out.add('No recurring patterns detected.', 'muted')
}

function agents(snap: Snapshot, out: Lines): void {
  out.heading('Agents')
  if (!snap.agents.length) out.add('No agent activity recorded.', 'muted')
  for (const agent of snap.agents) {
    out.heading(`${agent.label}${agent.type ? ` (${agent.type})` : ''}`)
    out.add(`${compact(agent.steps)} steps · ${compact(agent.tokens)} tokens · ${compact(agent.tool_calls)} tools · ${duration(agent.duration_s)}`)
    out.add(failures(agent.unknown_outcomes ? agent.observed_errors ?? agent.errors : agent.errors, agent.unknown_outcomes), agent.errors ? 'warning' : 'muted')
    if (number(agent.spawned_by_step)) out.add(`Spawned at step #${agent.spawned_by_step}`, 'accent')
    if (agent.description) out.add(agent.description, 'muted')
  }
  if (snap.swimlane.lanes.length) {
    out.heading('Activity by step')
    const width = Math.max(1, Math.min(out.width, 70))
    for (const lane of snap.swimlane.lanes) {
      out.clip(lane.label, 'muted')
      const count = Math.max(1, snap.swimlane.total_steps)
      const bars = Array.from({ length: width }, (_, i) => lane.segments.some(segment => segment.start < (i + 1) / width * count && segment.end >= i / width * count) ? process.env.TERM === 'dumb' ? '#' : '━' : '·').join('')
      out.clip(bars, 'accent')
    }
    out.add('Recorded step activity; open Dashboard for the full workflow.', 'muted')
  }
}

function context(snap: Snapshot, out: Lines): void {
  out.heading('Context window')
  out.add(`Current ${pct(snap.context.current_pct)} · peak ${pct(snap.context.peak_pct)}`)
  out.add(`${measured(snap.context.current_occupancy)} of ${measured(snap.context.window)} tokens`)
  out.add(`Window source: ${snap.context.window_source}`, 'muted')
  if (snap.context.window_note) out.add(snap.context.window_note, 'muted')
  out.add(`${compact(snap.context.compaction_count)} compactions · largest drop ${compact(snap.context.largest_drop)}`)
  out.clip(sparkline(snap.series.context.occupancy, out.width), 'accent')
  if (snap.series.context.compactions.length) out.add(`Compactions at turns: ${snap.series.context.compactions.join(', ')}`, 'warning')
  out.heading('Token use')
  for (const [key, value] of Object.entries(snap.tokens)) out.add(`${key.replaceAll('_', ' ')}: ${measured(value)}`)
  quality(snap, out)
}

function timeline(snap: Snapshot, out: Lines): void {
  out.heading('Recent steps')
  out.add('Latest recorded steps. Open Dashboard to inspect the full workflow.', 'muted')
  if (!snap.timeline.length) out.add('No timeline steps yet.', 'muted')
  for (const row of snap.timeline) {
    out.heading(`#${row.step} · ${row.role} · ${row.category}${row.agent ? ` · ${row.agent}` : ''}`)
    out.add(`${compact(row.tool_calls)} tools · ${failures(row.errors, row.unknown_outcomes)} · ${duration(row.duration_s)} · ${compact(row.tokens)} tokens`, 'muted')
    if (row.preview) out.add(row.preview)
  }
}

/** A Pi-owned custom screen/overlay; never replaces Pi's terminal renderer. */
export class ObsPane implements Component {
  private offset = 0
  private bodyRows = 1
  private maxOffset = 0
  private localTab: number | undefined
  private lastTab = -1

  constructor(
    private readonly state: () => PaneState,
    private readonly actions: PaneActions,
    private readonly theme: Theme,
    private readonly options: { rows?: () => number } = {},
  ) {}

  invalidate(): void { /* Render from current state; no mutable frame cache. */ }

  private tab(): number {
    const value = this.localTab ?? this.state().tab
    return Number.isInteger(value) ? Math.max(0, Math.min(TABS.length - 1, value)) : 0
  }

  private select(tab: number): void {
    if (this.actions.selectTab) this.actions.selectTab(tab)
    else this.localTab = tab
    this.offset = 0
    this.actions.changed()
  }

  handleInput(data: string): void {
    if (isKeyRelease(data)) return
    for (const [i, key] of TAB_KEYS.entries()) if (matchesKey(data, key)) { this.select(i); return }
    if (matchesKey(data, Key.escape) || matchesKey(data, 'q')) { this.actions.close(); return }
    if (matchesKey(data, Key.left) || matchesKey(data, Key.shift('tab'))) { this.select((this.tab() + TABS.length - 1) % TABS.length); return }
    if (matchesKey(data, Key.right) || matchesKey(data, Key.tab)) { this.select((this.tab() + 1) % TABS.length); return }
    if (matchesKey(data, 'r')) { if (!this.state().busy) this.actions.refresh(); return }
    if (matchesKey(data, 'd')) { this.actions.dashboard(); return }
    if (matchesKey(data, 'w')) { this.actions.toggleWatch(); return }
    const previous = this.offset
    if (matchesKey(data, Key.down) || matchesKey(data, 'j')) this.offset++
    else if (matchesKey(data, Key.up) || matchesKey(data, 'k')) this.offset--
    else if (matchesKey(data, Key.pageDown)) this.offset += this.bodyRows
    else if (matchesKey(data, Key.pageUp)) this.offset -= this.bodyRows
    else if (matchesKey(data, Key.home)) this.offset = 0
    else if (matchesKey(data, Key.end)) this.offset = this.maxOffset
    this.offset = Math.max(0, Math.min(this.maxOffset, this.offset))
    if (previous !== this.offset) this.actions.changed()
  }

  render(columns: number): string[] {
    if (columns < 1) return []
    const width = Math.floor(columns)
    const state = this.state()
    const tab = this.tab()
    if (tab !== this.lastTab) { this.offset = 0; this.lastTab = tab }
    const header = new Lines(width, this.theme)
    header.clip(`TrajViz · ${TABS[tab]}${state.busy ? ' · refreshing…' : ''}`, 'accent')
    if (width >= 48) {
      const tabs = TABS.map((name, i) => color(this.theme, i === tab ? 'accent' : 'muted', `${i + 1} ${name}${i === tab ? ' ‹' : ''}`)).join('  ')
      header.lines.push(...wrapTextWithAnsi(tabs, width))
      header.add(`r Refresh · d Dashboard · w Watch ${state.watch ? state.watchPaused ? 'paused (saved session)' : 'on' : 'off'} · Esc Close`, 'muted')
    } else {
      header.clip('1–7 tabs · ←/→ · Esc close', 'muted')
      header.clip(`r refresh · d dashboard · w ${state.watch ? state.watchPaused ? 'paused' : 'on' : 'off'}`, 'muted')
    }
    if (state.dashboardUrl) header.add(`Dashboard: ${state.dashboardUrl}`, 'accent')
    header.clip((process.env.TERM === 'dumb' ? '-' : '─').repeat(width), 'borderMuted')
    const body = new Lines(width, this.theme)
    if (state.error) {
      body.add(`Unable to refresh: ${state.error}`, 'error')
      body.add(state.snapshot ? 'Showing the last snapshot. Press r to retry.' : 'Press r to retry, or Esc to close.', 'muted')
    }
    const snap = state.snapshot
    if (!snap || snap.empty) body.add(snap?.empty ? snap.reason : state.busy ? 'Loading session data…' : 'No snapshot yet. Press r, or start a turn.', 'muted')
    else {
      if (tab === 0) overview(snap, body, false)
      else if (tab === 1) tools(snap, body)
      else if (tab === 2) issues(snap, body)
      else if (tab === 3) patterns(snap, body)
      else if (tab === 4) agents(snap, body)
      else if (tab === 5) context(snap, body)
      else timeline(snap, body)
      if (snap.trajviz.warnings.length) {
        body.heading('Capture notes')
        for (const warning of snap.trajviz.warnings) body.add(warning, 'warning')
      }
    }
    const requested = this.options.rows?.() ?? Math.max(12, (process.stdout.rows || 28) - 4)
    const rows = Math.max(1, Number.isFinite(requested) ? Math.floor(requested) : 24)
    // A very short terminal still gets a close hint and at least one body line.
    if (rows === 1) return [color(this.theme, 'muted', truncateToWidth('TrajViz · Esc close', width, '…'))]
    if (rows === 2) return [body.lines[0] ?? header.lines[0]!, color(this.theme, 'muted', truncateToWidth('Esc close', width, '…'))]
    if (header.lines.length > rows - 2) header.lines.splice(rows - 2)
    this.bodyRows = Math.max(1, rows - header.lines.length - 1)
    this.maxOffset = Math.max(0, body.lines.length - this.bodyRows)
    this.offset = Math.max(0, Math.min(this.maxOffset, this.offset))
    const page = body.lines.slice(this.offset, this.offset + this.bodyRows)
    const stamp = snap && !snap.empty && !Number.isNaN(Date.parse(snap.generated_at)) ? new Date(snap.generated_at).toLocaleTimeString([], { hour12: false }) : ''
    const footer = this.maxOffset ? `↑/↓ scroll · ${this.offset + 1}–${Math.min(body.lines.length, this.offset + this.bodyRows)}/${body.lines.length} · Esc close` : `${stamp ? `Updated ${stamp} · ` : ''}Esc close`
    return [...header.lines, ...page, color(this.theme, 'muted', truncateToWidth(footer, width, '…'))].map(line => truncateToWidth(line, width, '…'))
  }
}
