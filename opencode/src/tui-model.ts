import type { EmptySnapshot, Snapshot } from './types.js'

export const TABS = ['Overview', 'Tools', 'Issues', 'Patterns', 'Agents', 'Context', 'Timeline'] as const
export type Tab = typeof TABS[number]
export type Section = { title: string; lines: string[] }

/** Wide Markdown tables become readable row cards in the native result dialog. */
export function commandText(result: { text: string; data?: unknown }): string {
  if (!result.data || typeof result.data !== 'object') return result.text
  const split = (line: string): string[] => {
    const body = line.trim().replace(/^\|/, '').replace(/\|$/, '')
    const cells: string[] = []
    let cell = ''
    for (let i = 0; i < body.length; i++) {
      const char = body[i]!
      if (char === '\\' && body[i + 1] === '|') { cell += '|'; i++ }
      else if (char === '|') { cells.push(cell.trim()); cell = '' }
      else cell += char
    }
    cells.push(cell.trim())
    return cells
  }
  const lines = result.text.split('\n')
  const output: string[] = []
  let fence: string | undefined
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]!
    const marker = /^\s*(`{3,}|~{3,})/.exec(line)?.[1]
    if (marker) {
      if (!fence) fence = marker
      else if (marker[0] === fence[0] && marker.length >= fence.length && /^\s*[`~]+\s*$/.test(line)) fence = undefined
      output.push(line); continue
    }
    if (fence || !line.trimStart().startsWith('|') || !lines[i + 1]?.trimStart().startsWith('|') || !split(lines[i + 1]!).every(cell => /^:?-{3,}:?$/.test(cell))) { output.push(line); continue }
    let end = i + 2
    while (end < lines.length && lines[end]!.trimStart().startsWith('|')) end++
    const headers = split(line).map(name => name.replaceAll('_', ' '))
    const rows = lines.slice(i + 2, end).map(split)
    if (rows.some(row => row.length !== headers.length)) output.push(...lines.slice(i, end))
    else output.push(rows.map((row, index) => [`${index + 1}.`, ...headers.map((name, column) => `${name}: ${row[column]}`)].join('\n')).join('\n\n') || 'No rows.')
    i = end - 1
  }
  return output.join('\n')
}

/** Display-only sanitation: native source and exported data remain unchanged. */
export function safeText(text: string): string {
  return text
    .replace(/(?:\u001b\]|\u009d)[\s\S]*?(?:\u0007|\u001b\\|\u009c|$)/g, '')
    .replace(/(?:\u001b[PX^_]|[\u0090\u0098\u009e\u009f])[\s\S]*?(?:\u001b\\|\u009c|$)/g, '')
    .replace(/(?:\u001b\[|\u009b)[0-?]*[ -/]*(?:[@-~]|$)/g, '')
    .replace(/\u001b[ -/]*[0-~]?/g, '')
    .replace(/\r\n?/g, '\n')
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/g, '')
    .replace(/\t/g, '  ')
    .replace(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/g, '�')
}

export function compact(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—'
  if (Math.abs(value) < 999.5) return String(Math.round(value))
  const units = ['k', 'M', 'G', 'T']
  let v = value
  let index = -1
  while (Math.abs(v) >= 999.5 && index < units.length - 1) { v /= 1000; index++ }
  return `${v.toFixed(Math.abs(v) < 9.95 ? 1 : 0).replace(/\.0$/, '')}${units[index]}`
}

export const percent = (value: number | null | undefined): string =>
  value == null || !Number.isFinite(value) ? '—' : `${value.toFixed(0)}%`

export function duration(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—'
  const s = Math.max(0, value)
  if (s < 10) return `${s.toFixed(1)}s`
  const rounded = Math.round(s)
  if (rounded < 60) return `${rounded}s`
  const m = Math.floor(rounded / 60)
  return m < 60 ? `${m}m${String(rounded % 60).padStart(2, '0')}s` : `${Math.floor(m / 60)}h${String(m % 60).padStart(2, '0')}m`
}

/** Missing evidence cannot contribute a measured zero to a total or chart point. */
const sumMeasured = (values: readonly (number | null | undefined)[]): number | null =>
  values.length && values.every((value): value is number => typeof value === 'number' && Number.isFinite(value))
    ? values.reduce((sum, value) => sum + value, 0) : null

const failures = (errors: number | null, unknown = 0): string => unknown > 0
  ? `${compact(errors)} observed failures · ${compact(unknown)} unknown outcome${unknown === 1 ? '' : 's'}`
  : errors === null ? 'failures unknown' : `${compact(errors)} failed`

const unavailable = (snap: Snapshot, view: string): boolean => snap.availability?.[view]?.status === 'unavailable'

function qualityLines(snap: Snapshot): string[] {
  const fields = Object.values(snap.data_quality?.fields ?? {})
  const notes: string[] = []
  if (Object.values(snap.tokens).some(value => value === null)) notes.push('Usage incomplete')
  if (snap.tools.unknown) notes.push(`${compact(snap.tools.unknown)} unknown tool outcome${snap.tools.unknown === 1 ? '' : 's'}`)
  else if (snap.tools.failed === null || snap.tools.calls > 0 && snap.tools.success_rate === null) notes.push('Tool outcomes incomplete')
  const missing = fields.filter(field => field.status === 'unavailable').length
  const unknown = fields.filter(field => field.status === 'unknown').length
  const partial = fields.filter(field => field.status === 'partial').length
  if (missing) notes.push(`${missing} field${missing === 1 ? '' : 's'} unavailable`)
  if (unknown) notes.push(`${unknown} field${unknown === 1 ? '' : 's'} unknown`)
  if (partial) notes.push(`${partial} field${partial === 1 ? '' : 's'} partial`)
  if (!notes.length && !snap.data_quality) return []
  return [notes.join(' · ') || 'Capture completeness unknown', 'Unknown values use — / ·. /obs view data_quality']
}

/** A bounded sparkline: bin maxima retain short spikes and missing values stay unknown. */
export function trend(values: readonly (number | null)[], columns: number): string {
  if (!values.length) return 'No samples'
  const size = Math.max(1, Math.min(48, Math.floor(columns)))
  const bins: (number | null)[] = []
  for (let i = 0; i < Math.min(size, values.length); i++) {
    const start = Math.floor(i * values.length / Math.min(size, values.length))
    const end = Math.floor((i + 1) * values.length / Math.min(size, values.length))
    const observed = values.slice(start, end).filter((v): v is number => v !== null && Number.isFinite(v))
    bins.push(observed.length ? Math.max(...observed) : null)
  }
  const maximum = Math.max(0, ...bins.filter((v): v is number => v !== null))
  const blocks = '▁▂▃▄▅▆▇█'
  return bins.map(v => v === null ? '·' : blocks[maximum ? Math.min(7, Math.floor(Math.max(0, v) / maximum * 7)) : 0]).join('')
}

export function metrics(snap: Snapshot): { label: string; value: string; detail?: string }[] {
  const cacheTotal = sumMeasured([snap.tokens.input, snap.tokens.cache_read, snap.tokens.cache_write])
  const cacheHealth = snap.health.find(row => /cache[\s_]*efficien/i.test(row.metric))
  return [
    { label: 'Steps', value: compact(snap.counts.assistant_steps), detail: `${compact(snap.counts.prompts)} prompts` },
    { label: 'Tools', value: compact(snap.tools.calls), detail: `${percent(snap.tools.success_rate)} success · ${failures(snap.tools.failed, snap.tools.unknown)}` },
    { label: 'Tokens', value: compact(snap.tokens.total), detail: `${compact(snap.tokens.output)} output` },
    { label: 'Context', value: percent(snap.context.current_pct), detail: `peak ${percent(snap.context.peak_pct)} of ${compact(snap.context.window)}` },
    { label: cacheHealth ? 'Cache efficiency' : 'Cache read', value: cacheHealth ? cacheHealth.status === 'unavailable' ? 'unavailable' : cacheHealth.label : cacheTotal && snap.tokens.cache_read !== null ? percent(snap.tokens.cache_read / cacheTotal * 100) : '—', detail: `${compact(snap.tokens.cache_write)} written` },
    { label: 'Agents', value: compact(snap.counts.agents) },
    { label: 'Elapsed', value: duration(snap.timing.wall_clock_s), detail: `p95 ${duration(snap.timing.p95_step_s)}` },
    ...(snap.engine_usage?.cost && Number.isFinite(snap.engine_usage.cost.usd) ? [{ label: 'Cost', value: `$${snap.engine_usage.cost.usd.toFixed(2)}`, detail: 'reported by OpenCode' }] : []),
  ]
}

export function statusText(snap: Snapshot | EmptySnapshot | undefined): string {
  if (!snap || snap.empty) return 'obs: waiting for session activity'
  return `obs: ${compact(snap.counts.assistant_steps)} steps · ctx ${percent(snap.context.current_pct)} · ${failures(snap.tools.failed, snap.tools.unknown)} · ${snap.counts.issues ?? snap.issues.length} issues`
}

const issueLines = (snap: Snapshot, limit: number): string[] => {
  if (!snap.issues.length) return [unavailable(snap, 'issues') ? 'Issues unavailable: required evidence was not recorded.' : 'No observed issues.']
  const lines = snap.issues.slice(0, limit).flatMap((issue, index) => [
    `${index + 1}. ${issue.title} · steps ${issue.steps.join(', ') || '—'}`,
    ...(issue.evidence ? [issue.evidence] : []),
    ...(issue.detail ? [issue.detail] : []),
    ...(issue.why ? [issue.why] : []),
  ])
  const total = snap.counts.issues ?? snap.issues.length
  if (total > limit) lines.push(`${total - limit} more · /obs issues for details`)
  return lines
}

const recordLines = (value: unknown, limit = 8): string[] => {
  if (Array.isArray(value)) return value.slice(0, limit).flatMap((row, index) => {
    if (!row || typeof row !== 'object' || Array.isArray(row)) return [String(row)]
    const pairs = Object.entries(row)
    const title = pairs.find(([key]) => ['title', 'label', 'cluster_label', 'name', 'pattern', 'tool', 'file'].includes(key))
    const printable = (item: unknown): string => item == null ? '—' : typeof item === 'string' ? item : Array.isArray(item) ? item.map(v => v == null ? '—' : typeof v === 'object' ? JSON.stringify(v) : String(v)).join(', ') : typeof item === 'object' ? JSON.stringify(item) : String(item)
    return [
      `${index + 1}. ${title ? printable(title[1]) : 'Pattern'}`,
      ...pairs.filter(([key]) => key !== title?.[0]).map(([key, item]) => `${key.replaceAll('_', ' ')}: ${printable(item)}`),
    ]
  })
  if (value && typeof value === 'object') return Object.entries(value).flatMap(([key, rows]) => {
    if (Array.isArray(rows)) return rows.length ? [`${key}:`, ...recordLines(rows, limit)] : []
    return rows == null ? [] : [`${key}: ${typeof rows === 'object' ? JSON.stringify(rows) : String(rows)}`]
  })
  return []
}

/** All seven views derive from the same immutable snapshot. */
export function sections(snap: Snapshot | EmptySnapshot | undefined, tab: number, columns: number): Section[] {
  if (!snap) return [{ title: 'Session', lines: ['Loading session data…'] }]
  if (snap.empty) return [{ title: 'Session', lines: [safeText(!snap.reason || /^no trajectory steps found\b/i.test(snap.reason)
    ? 'Nothing to analyze yet. Start a turn or choose a captured session with /obs sessions.'
    : snap.reason)] }]
  let result: Section[]
  switch (tab) {
    case 1: {
      const maximum = Math.max(1, ...snap.tools.by_tool.map(row => row.calls))
      const width = Math.max(4, Math.min(20, Math.floor(columns / 3)))
      result = [
        { title: 'Tools', lines: [`${snap.tools.calls} calls · ${percent(snap.tools.success_rate)} success · ${failures(snap.tools.failed, snap.tools.unknown)}`, ...snap.tools.by_tool.map(row => `${row.tool} ${'━'.repeat(Math.max(1, Math.round(row.calls / maximum * width)))} ${row.calls}${row.errors !== 0 || row.unknown ? ` · ${failures(row.errors, row.unknown)}` : ''}`)] },
        { title: 'Files', lines: unavailable(snap, 'file_interactions') ? ['Unavailable: tool inputs were not recorded.'] : [
          ...(snap.availability?.file_interactions?.status === 'partial' ? ['Partial evidence: counts reflect recorded activity.'] : []),
          `${compact(snap.files.unique)} files · ${compact(snap.files.reads)} reads · ${compact(snap.files.writes)} writes · ${compact(snap.files.searches)} searches`, ...snap.files.top.map(row => `${row.file}: ${row.reads} reads · ${row.writes} writes`),
        ] },
        { title: 'Skills', lines: snap.skills.length ? snap.skills.map(row => `${row.skill}: ${row.calls} calls`) : [unavailable(snap, 'skill_calls') ? 'Unavailable: tool inputs were not recorded.' : 'No skill calls recorded.'] },
      ]
      break
    }
    case 2:
      result = [{ title: 'Issues', lines: issueLines(snap, snap.issues.length || 1) }, { title: 'Bottlenecks', lines: snap.bottlenecks.length ? snap.bottlenecks.map(row => `Step ${row.step} · ${duration(row.duration_s)} · ${row.title}\n${row.detail}`) : [unavailable(snap, 'bottlenecks') ? 'Unavailable: timing evidence is incomplete.' : 'No bottlenecks detected.'] }]
      break
    case 3:
      result = Object.entries(snap.patterns).flatMap(([key, value]) => {
        const lines = recordLines(value)
        return lines.length ? [{ title: key.replaceAll('_', ' '), lines }] : unavailable(snap, key) ? [{ title: key.replaceAll('_', ' '), lines: ['Unavailable: required evidence was not recorded.'] }] : []
      })
      if (!result.length) result = [{ title: 'Patterns', lines: ['No recurring patterns detected.'] }]
      break
    case 4:
      result = [{ title: 'Agents', lines: snap.agents.length ? snap.agents.flatMap(row => [
        `${row.label}${row.type ? ` (${row.type})` : ''}`,
        `${row.steps} steps · ${compact(row.tokens)} tokens · ${row.tool_calls} tools · ${failures(row.unknown_outcomes ? row.observed_errors ?? row.errors : row.errors, row.unknown_outcomes)} · ${duration(row.duration_s)}`,
        ...(row.description ? [row.description] : []),
      ]) : ['No agent activity recorded.'] }]
      if (snap.tools.unknown && !snap.agents.some(row => row.unknown_outcomes)) result.push({ title: 'Tool outcomes', lines: [`${compact(snap.tools.unknown)} outcomes unknown; agent error counts show observed failures.`] })
      result.push({ title: 'Activity by step', lines: ['/obs view agent_swimlane for the full agent activity view.'] })
      break
    case 5:
      result = [{ title: 'Context', lines: [
        `Current ${percent(snap.context.current_pct)} · peak ${percent(snap.context.peak_pct)}`,
        `${compact(snap.context.current_occupancy)} tokens of ${compact(snap.context.window)} context window`,
        `Window source: ${snap.context.window_source}`,
        ...(snap.context.window_note ? [snap.context.window_note] : []),
        `${compact(snap.context.compaction_count)} compactions · largest drop ${compact(snap.context.largest_drop)}`,
        trend(snap.series.context.occupancy, columns - 4),
      ] }, { title: 'Token use', lines: [
        `${compact(snap.tokens.input)} input · ${compact(snap.tokens.output)} output · ${compact(snap.tokens.reasoning)} reasoning`,
        `${compact(snap.tokens.cache_read)} cache read · ${compact(snap.tokens.cache_write)} cache write`,
      ] }]
      break
    case 6:
      result = [{ title: 'Recent steps', lines: snap.timeline.length ? snap.timeline.slice(-30).flatMap(row => [
        `#${row.step} ${row.role} · ${row.category}${row.agent ? ` · ${row.agent}` : ''}`,
        `${row.tool_calls} tools · ${failures(row.errors, row.unknown_outcomes)} · ${duration(row.duration_s)} · ${compact(row.tokens)} tokens`,
        ...(row.preview ? [row.preview] : []),
        '',
      ]) : ['No timeline steps yet.'] }]
      break
    default: {
      const tokenValues = snap.series.tokens.steps.map((_, i) => sumMeasured([snap.series.tokens.fresh[i], snap.series.tokens.cache_read[i], snap.series.tokens.cache_write[i], snap.series.tokens.output[i]]))
      const latest = snap.phases.reduce<Snapshot['phases'][number] | undefined>((a, b) => !a || b.end >= a.end ? b : a, undefined)
      result = [
        { title: 'Health', lines: [snap.health.map(row => `${row.metric.replaceAll('_', ' ')} ${row.status}`).join(' · ')] },
        { title: 'Top issues', lines: snap.issues.length ? [
          ...snap.issues.slice(0, 3).map((issue, index) => `${index + 1}. ${issue.title}`),
          ...((snap.counts.issues ?? snap.issues.length) > 3 ? [`${(snap.counts.issues ?? snap.issues.length) - 3} more`] : []),
          'Press 3 for evidence and affected steps.',
        ] : issueLines(snap, 1) },
        { title: 'Trends · earlier → later', lines: [
          `Tokens/step ${trend(tokenValues, columns - 16)}`,
          `Step time   ${trend(snap.series.durations.seconds, columns - 16)}`,
          `Context     ${trend(snap.series.context.occupancy, columns - 16)}`,
          ...(latest ? [`Latest phase: ${latest.name}`] : []),
        ] },
      ]
      const quality = qualityLines(snap)
      if (quality.length) result.splice(1, 0, { title: 'Data quality', lines: quality })
    }
  }
  if (snap.trajviz.warnings.length) result.push({ title: 'Notes', lines: snap.trajviz.warnings })
  return result.map(section => ({ title: safeText(section.title), lines: section.lines.map(safeText) }))
}
