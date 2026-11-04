/** Presentation-only facts shared by the terminal and Desktop renderers. */
import type { Agent, EmptySnapshot, HealthRow, Snapshot } from './types.js'
import { safeText } from './tui-model.js'

export type Tone = 'neutral' | 'info' | 'success' | 'warning' | 'danger' | 'agent'
export type Fragment = { text: string; tone?: Tone; emphasis?: 'key' | 'value' }
const measured = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value)
const name = (value: string) => value.toLowerCase().replace(/[\s_]/g, '')
export function healthTone(status: string): Tone {
  switch (status) {
    case 'ok': case 'good': return 'success'
    case 'warn': return 'warning'
    case 'bad': return 'danger'
    default: return 'neutral'
  }
}

export function healthStatusLabel(status: string): string {
  switch (status) {
    case 'good': case 'ok': return 'Good'
    case 'warn': return 'Warning'
    case 'bad': return 'Needs attention'
    case 'unavailable': return 'Unavailable'
    case 'unknown': return 'Unknown'
    default: return safeText(status)
  }
}

/** Missing outcomes never become a measured zero or a successful tool run. */
export function failureTone(failed: number | null | undefined, unknown = 0, calls?: number): Tone {
  if (!measured(failed) || !Number.isSafeInteger(failed) || failed < 0) return 'neutral'
  if (failed > 0) return 'danger'
  return unknown === 0 && measured(calls) && Number.isSafeInteger(calls) && calls > 0 ? 'success' : 'neutral'
}

export function healthRowTone(snapshot: Snapshot, row: HealthRow): Tone {
  if (!row.label || ['n/a', 'unknown', 'unavailable', '—'].includes(row.label.toLowerCase())) return 'neutral'
  const metric = name(row.metric)
  if (metric === 'toolsuccess' || metric === 'errors') {
    const outcome = failureTone(snapshot.tools.failed, snapshot.tools.unknown, snapshot.tools.calls)
    if (outcome === 'neutral' || snapshot.tools.failed === null) return 'neutral'
  }
  if (metric === 'cacheefficiency' && (!measured(snapshot.tokens.input) || !measured(snapshot.tokens.cache_read) || !measured(snapshot.tokens.cache_write) || snapshot.tokens.input + snapshot.tokens.cache_read + snapshot.tokens.cache_write === 0)) return 'neutral'
  if (metric === 'throughput' && !measured(snapshot.timing.output_tokens_per_sec)) return 'neutral'
  return healthTone(row.status)
}

export function metricTone(snapshot: Snapshot, label: string): Tone {
  switch (label) {
    case 'Tools': {
      const tone = failureTone(snapshot.tools.failed, snapshot.tools.unknown, snapshot.tools.calls)
      if (tone === 'danger' || tone === 'neutral') return tone
      const verdict = snapshot.health.find(row => name(row.metric) === 'toolsuccess')
      return verdict ? healthRowTone(snapshot, verdict) : tone
    }
    case 'Cache efficiency': {
      const verdict = snapshot.health.find(row => name(row.metric) === 'cacheefficiency')
      return verdict ? healthRowTone(snapshot, verdict) : 'neutral'
    }
    case 'Cache read': return measured(snapshot.tokens.cache_read) ? 'info' : 'neutral'
    case 'Context': {
      if (!measured(snapshot.context.current_pct)) return 'neutral'
      const verdict = snapshot.health.find(row => ['context', 'contextusage', 'contextpressure'].includes(name(row.metric)))
      return verdict ? healthRowTone(snapshot, verdict) : 'info'
    }
    case 'Tokens': return measured(snapshot.tokens.total) ? 'info' : 'neutral'
    case 'Elapsed': return measured(snapshot.timing.wall_clock_s) ? 'info' : 'neutral'
    case 'Cost': return measured(snapshot.engine_usage?.cost?.usd) ? 'info' : 'neutral'
    case 'Steps': return measured(snapshot.counts.assistant_steps) ? 'info' : 'neutral'
    case 'Agents': return measured(snapshot.counts.agents) ? 'agent' : 'neutral'
    default: return 'neutral'
  }
}

export function agentFailureTone(agent: Agent): Tone {
  return failureTone(agent.unknown_outcomes ? agent.observed_errors ?? agent.errors : agent.errors, agent.unknown_outcomes, agent.tool_calls)
}
const record = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}

/** Annotate generated facts or structural view keys; never infer a verdict from prose. */
export function resultFragments(input: string, snapshot?: Snapshot | EmptySnapshot, data?: unknown): Fragment[] {
  const text = safeText(input)
  const facts = snapshot && !snapshot.empty && safeText(snapshot.text) === text ? snapshot : undefined
  const structured = record(data), view = record(structured.view), inline = record(structured.inline)
  const rows = typeof view.id === 'string' && typeof inline.table === 'string'
  if (!facts && !rows) return [{ text }]
  const result: Fragment[] = []
  let fence: string | undefined, inIssues = false
  for (const [lineIndex, line] of text.split('\n').entries()) {
    if (lineIndex) result.push({ text: '\n' })
    const marker = /^\s*(`{3,}|~{3,})/.exec(line)?.[1]
    if (marker) {
      if (!fence) fence = marker
      else if (marker[0] === fence[0] && marker.length >= fence.length && /^\s*[`~]+\s*$/.test(line)) fence = undefined
      result.push({ text: line }); continue
    }
    if (fence) { result.push({ text: line }); continue }
    const ranges: { start: number; end: number; tone: Tone; emphasis: 'key' | 'value' }[] = []
    const mark = (value: string, tone: Tone, emphasis: 'key' | 'value', start = 0) => {
      const index = line.indexOf(value, start)
      if (index >= 0 && value) ranges.push({ start: index, end: index + value.length, tone, emphasis })
    }
    if (facts) {
      if (line === 'Issues:') inIssues = true
      else if (['Health:', 'Context:', 'Bottlenecks:', 'Hints:', 'Warnings:'].some(key => line.startsWith(key))) inIssues = false
      for (const key of ['Health:', 'Context:', 'Issues:', 'Bottlenecks:']) if (line.startsWith(key)) mark(key, 'info', 'key')
      if (line.startsWith('Health:')) for (const row of facts.health) mark(`${safeText(row.metric)} ${row.status}`, healthRowTone(facts, row), 'value')
      if (line.startsWith(`${facts.counts.assistant_steps.toLocaleString('en-US')} assistant step`)) mark(line, 'info', 'value')
      if (line.startsWith('Context:')) {
        const occupancy = line.slice('Context:'.length).match(/^\s*(\S+)/)?.[1]
        if (occupancy) mark(occupancy, measured(facts.context.current_pct) ? 'info' : 'neutral', 'value', 'Context:'.length)
      }
      if (inIssues) facts.issues.forEach((issue, index) => {
        const title = Array.from(safeText(issue.title || issue.kind || 'issue').trim().replace(/\s+/g, ' '))
        const shown = title.length > 100 ? title.slice(0, 99).join('') + '…' : title.join('')
        if (line.startsWith(`${index + 1}. ${shown}`)) mark(shown, issue.kind === 'error' ? 'danger' : 'warning', 'value', String(index + 1).length + 2)
      })
    } else if (rows) {
      // A row card's field name is syntax, not a severity inferred from its value.
      const field = /^([A-Za-z][A-Za-z0-9 _/-]{0,63}:)(\s|$)/.exec(line)
      if (field) mark(field[1]!, 'info', 'key')
    }
    ranges.sort((a, b) => a.start - b.start)
    let at = 0
    for (const range of ranges) {
      if (range.start < at) continue
      if (range.start > at) result.push({ text: line.slice(at, range.start) })
      result.push({ text: line.slice(range.start, range.end), tone: range.tone, emphasis: range.emphasis })
      at = range.end
    }
    if (at < line.length || !line.length) result.push({ text: line.slice(at) })
  }
  return result
}
