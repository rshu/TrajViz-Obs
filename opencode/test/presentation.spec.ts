import { describe, expect, test } from 'bun:test'
import { SNAPSHOT } from '../../hooks/testdata/snapshot.js'
import { PARTIAL_SNAPSHOT, UNKNOWN_SNAPSHOT, ZERO_SNAPSHOT } from '../../hooks/testdata/quality.js'
import { agentFailureTone, failureTone, healthRowTone, healthStatusLabel, healthTone, metricTone, resultFragments } from '../src/presentation.js'
import { safeText } from '../src/tui-model.js'

const joined = (fragments: ReturnType<typeof resultFragments>): string => fragments.map(fragment => fragment.text).join('')
const unclassified = (fragments: ReturnType<typeof resultFragments>): boolean =>
  fragments.every(fragment => !fragment.tone || fragment.tone === 'neutral')

describe('Diagnostic presentation', () => {
  test('status labels remain readable and sanitize unknown labels without prototype lookups', () => {
    expect(healthStatusLabel('good')).toBe('Good')
    expect(healthStatusLabel('warn')).toBe('Warning')
    expect(healthStatusLabel('bad')).toBe('Needs attention')
    for (const status of ['constructor', 'toString', '__proto__']) {
      expect(healthStatusLabel(status)).toBe(status)
      expect(healthTone(status)).toBe('neutral')
    }
    expect(healthStatusLabel('\u001b[31mfuture\u001b[0m')).toBe('future')
  })

  test('health statuses carry their source meaning while unknown statuses stay neutral', () => {
    expect(healthTone('good')).toBe('success')
    expect(healthTone('ok')).toBe('success')
    expect(healthTone('warn')).toBe('warning')
    expect(healthTone('bad')).toBe('danger')
    for (const status of ['', 'unavailable', 'unknown', 'partial', 'future-status', 'error in user text', 'toString', 'constructor', '__proto__']) {
      expect(healthTone(status)).toBe('neutral')
    }
  })

  test('observed failures remain red even when other tool outcomes are unknown', () => {
    expect(failureTone(1, 0, 4)).toBe('danger')
    expect(failureTone(1, 3, 4)).toBe('danger')
    expect(failureTone(7, 0, 196)).toBe('danger')
  })

  test('only observed zero failures with actual completed calls are successful', () => {
    expect(failureTone(0, 0, 1)).toBe('success')
    expect(failureTone(0, 1, 1)).toBe('neutral')
    expect(failureTone(0, 0, 0)).toBe('neutral')
    expect(failureTone(0)).toBe('neutral')
    for (const missing of [null, undefined, Number.NaN, Number.POSITIVE_INFINITY, -1]) {
      expect(failureTone(missing, 0, 1)).toBe('neutral')
    }
  })

  test('metric colors distinguish real failures, true success, and incomplete evidence', () => {
    expect(metricTone(SNAPSHOT, 'Tools')).toBe('danger')
    expect(metricTone(ZERO_SNAPSHOT, 'Tools')).toBe('success')
    expect(metricTone(PARTIAL_SNAPSHOT, 'Tools')).toBe('neutral')
    expect(metricTone(UNKNOWN_SNAPSHOT, 'Tokens')).toBe('neutral')
    expect(metricTone(PARTIAL_SNAPSHOT, 'Context')).toBe('neutral')
    const noActivity = structuredClone(ZERO_SNAPSHOT)
    noActivity.tools.calls = 0
    expect(metricTone(noActivity, 'Tools')).toBe('neutral')
  })

  test('context warnings come from source verdicts rather than invented percentage thresholds', () => {
    expect(metricTone(SNAPSHOT, 'Context')).toBe('warning')
    const withoutVerdict = structuredClone(SNAPSHOT)
    withoutVerdict.health = []
    withoutVerdict.context.current_pct = 99.9
    expect(['danger', 'warning']).not.toContain(metricTone(withoutVerdict, 'Context'))
    withoutVerdict.context.current_pct = null
    expect(metricTone(withoutVerdict, 'Context')).toBe('neutral')
  })

  test('no-data health rows remain neutral even if the engine emits a good placeholder verdict', () => {
    const snapshot = structuredClone(ZERO_SNAPSHOT)
    for (const label of ['N/A', 'unknown', 'unavailable', '—', '']) {
      expect(healthRowTone(snapshot, { metric: 'Throughput', status: 'good', label, detail: 'No measured data' })).toBe('neutral')
    }
    snapshot.tools.calls = 0
    expect(healthRowTone(snapshot, { metric: 'Tool Success', status: 'good', label: '100%', detail: 'No tool calls' })).toBe('neutral')
    snapshot.timing.output_tokens_per_sec = null
    expect(healthRowTone(snapshot, { metric: 'Throughput', status: 'good', label: '0', detail: 'No measured duration' })).toBe('neutral')
  })

  test('agent failure color remains separate from agent identity and preserves partial observed failures', () => {
    const agent = structuredClone(PARTIAL_SNAPSHOT.agents[0]!)
    expect(agentFailureTone(agent)).toBe('neutral')
    agent.observed_errors = 1
    expect(agentFailureTone(agent)).toBe('danger')
    expect(metricTone(PARTIAL_SNAPSHOT, 'Agents')).toBe('agent')
    expect(agentFailureTone(ZERO_SNAPSHOT.agents[0]!)).toBe('success')
  })

  test('ordinary unknown capture completeness is never a warning by itself', () => {
    const snapshot = structuredClone(ZERO_SNAPSHOT)
    snapshot.text = 'Data quality: Capture completeness unknown\nUnknown values use — / ·.'
    const fragments = resultFragments(snapshot.text, snapshot)
    expect(joined(fragments)).toBe(snapshot.text)
    expect(fragments.some(fragment => fragment.tone === 'warning' || fragment.tone === 'danger')).toBe(false)
    expect(metricTone(snapshot, 'Tools')).toBe('success')
  })

  test('plain model, tool, and user text never acquires inferred health status', () => {
    const text = 'ERROR: warning success failed 0 errors\n<img src=x onerror=alert(1)>\nTool output: test failed'
    const fragments = resultFragments(text)
    expect(unclassified(fragments)).toBe(true)
    expect(joined(fragments)).toBe(text)
    expect(unclassified(resultFragments(text, SNAPSHOT))).toBe(true)
    expect(unclassified(resultFragments(text, undefined, { message: 'failed', status: 'good' }))).toBe(true)
  })

  test('real multiline overview issues color only the typed title within its generated section', () => {
    const snapshot = structuredClone(SNAPSHOT)
    snapshot.counts = { ...snapshot.counts, assistant_steps: 14, prompts: 2, tool_calls: 14 }
    snapshot.tools = { ...snapshot.tools, calls: 14, failed: 1, unknown: 0, success_rate: 92.9 }
    snapshot.context = { ...snapshot.context, current_pct: 12, peak_pct: 13, window: 200000, compaction_count: 0 }
    snapshot.health = [
      { metric: 'Errors', status: 'warn', label: '1', detail: 'One observed failure' },
      { metric: 'Tool Success', status: 'good', label: '92.9%', detail: '13 of 14 tools succeeded' },
    ]
    snapshot.issues = [
      { kind: 'error', title: 'Bash: Exit code 1 (1×)', steps: [1], evidence: 'Bash: echo success && false', detail: 'Test failed', why: '' },
      { kind: 'bash_for_reading', title: 'Bash-for-reading (2×)', steps: [2, 3], evidence: 'Bash: cat example.py', detail: '', why: '' },
    ]
    snapshot.text = [
      'Health: Errors warn (1) · Tool Success good (92.9%)',
      '14 assistant steps · 2 prompts · 2 agents · 213805 tokens · 14 tool calls (92.9% ok)',
      'Context: 12% of 200000 · peak 13% · 0 compactions',
      'Issues:',
      '1. Bash: Exit code 1 (1×) — step 1 — Bash: echo success && false',
      '2. Bash-for-reading (2×) — steps 2, 3 — Bash: cat example.py',
      'Bottlenecks: none',
      'Hints:',
      '- User text also mentions Bash: Exit code 1 (1×), warning and success.',
    ].join('\n')
    const fragments = resultFragments(snapshot.text, snapshot)
    expect(joined(fragments)).toBe(snapshot.text)
    expect(fragments.filter(fragment => fragment.text === snapshot.issues[0]!.title && fragment.tone === 'danger')).toHaveLength(1)
    expect(fragments.filter(fragment => fragment.text === snapshot.issues[1]!.title && fragment.tone === 'warning')).toHaveLength(1)
    expect(fragments.find(fragment => fragment.text.includes('echo success && false'))?.tone).toBeUndefined()
    expect(fragments.find(fragment => fragment.text.includes('User text also mentions'))?.tone).toBeUndefined()
    expect(fragments.some(fragment => fragment.text === 'Errors warn' && fragment.tone === 'warning')).toBe(true)
    expect(fragments.some(fragment => fragment.text === 'Tool Success good' && fragment.tone === 'success')).toBe(true)
    const changedText = snapshot.text.replace('213805 tokens', '999 tokens')
    expect(unclassified(resultFragments(changedText, snapshot))).toBe(true)
  })

  test('structured row cards emphasize field syntax while values and fenced code remain neutral', () => {
    const data = { view: { id: 'tool_outcomes' }, inline: { table: '| Tool | Errors |\n|---|---|\n| shell | 1 |' } }
    const text = [
      'Tool outcomes · 1 row', '1.', 'Tool: shell', 'Errors: 1', 'Preview: failed warning success',
      '```text', 'Errors: do not classify code', '~~~', 'Warning: still inside the backtick fence', '```',
    ].join('\n')
    const fragments = resultFragments(text, undefined, data)
    expect(joined(fragments)).toBe(text)
    expect(fragments.find(fragment => fragment.text === 'Tool:')).toMatchObject({ tone: 'info', emphasis: 'key' })
    expect(fragments.find(fragment => fragment.text === 'Errors:')).toMatchObject({ tone: 'info', emphasis: 'key' })
    expect(fragments.some(fragment => fragment.tone === 'danger' || fragment.tone === 'warning' || fragment.tone === 'success')).toBe(false)
    expect(fragments.find(fragment => fragment.text === 'Errors: do not classify code')).toEqual({ text: 'Errors: do not classify code' })
    expect(fragments.find(fragment => fragment.text === 'Warning: still inside the backtick fence')).toEqual({ text: 'Warning: still inside the backtick fence' })
  })

  test('presentation strips terminal controls without mutating snapshot or structured data', () => {
    const snapshot = structuredClone(PARTIAL_SNAPSHOT)
    const data = { output: 'failed', labels: ['warning', 'success'] }
    const before = JSON.stringify({ snapshot, data })
    const text = '\u001b[31mfailed tool output\u001b[0m\n\u001b]8;;https://example.com\u0007link\u001b]8;;\u0007'
    expect(joined(resultFragments(text, snapshot, data))).toBe(safeText(text))
    for (const label of ['Tools', 'Context', 'Tokens', 'Cache efficiency', 'Agents', 'Cost']) metricTone(snapshot, label)
    expect(JSON.stringify({ snapshot, data })).toBe(before)
  })
})
