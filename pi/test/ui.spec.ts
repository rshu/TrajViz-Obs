import { describe, expect, test } from 'bun:test'
import type { Theme, ThemeColor } from '@earendil-works/pi-coding-agent'
import { compositeTuiLine, stripTerminalSequences, visibleWidth } from '@earendil-works/pi-tui'
import { SNAPSHOT } from '../../hooks/testdata/snapshot.js'
import { FramedComponent, ObsPane, TABS, renderOverview, sparkline, type PaneActions, type PaneState } from '../src/ui.js'
import type { Snapshot } from '../src/types.js'

const shades: Partial<Record<ThemeColor, string>> = { accent: '34', muted: '90', warning: '33', error: '31', success: '32', borderMuted: '90', text: '39' }
const theme = { fg: (name: ThemeColor, value: string) => `\x1b[${shades[name] ?? '39'}m${value}\x1b[0m` } as Theme
const plain = (lines: string[]): string => lines.map(stripTerminalSequences).join('\n')
const snapshot = (): Snapshot => structuredClone(SNAPSHOT)
const harness = (initial: Partial<PaneState> = {}, rows = 60) => {
  const state: PaneState = { snapshot: snapshot(), tab: 0, watch: true, ...initial }
  const calls: string[] = []
  const actions: PaneActions = {
    refresh() { calls.push('refresh') }, dashboard() { calls.push('dashboard') },
    toggleWatch() { state.watch = !state.watch; calls.push('watch') }, close() { calls.push('close') },
    changed() { calls.push('changed') }, selectTab(tab) { state.tab = tab; calls.push(`tab:${tab}`) },
  }
  const pane = new ObsPane(() => state, actions, theme, { rows: () => rows })
  return { pane, state, calls, actions }
}

describe('Pi diagnostics presentation', () => {
  test('framed screens cover every terminal cell through Pi’s native compositor', () => {
    for (const width of [1, 2, 4, 6, 12, 32, 80, 140]) for (const rows of [1, 2, 3, 4, 12, 30, 60]) {
      const { pane } = harness({}, Math.max(1, rows - 2))
      const screen = new FramedComponent(pane, theme, () => rows)
      const lines = screen.render(width)
      expect(lines).toHaveLength(rows)
      for (const line of lines) {
        expect(visibleWidth(line)).toBe(width)
        const composite = stripTerminalSequences(compositeTuiLine('CHAT_FRAGMENT'.repeat(width), line, 0, width, width))
        expect(composite).not.toContain('CHAT_FRAGMENT')
        expect(visibleWidth(composite)).toBe(width)
      }
      if (width >= 12) expect(plain(lines)).toContain('Esc close')
    }
    let pressed = '', invalidations = 0
    const frame = new FramedComponent({ render: () => ['content'], handleInput: key => { pressed = key }, invalidate: () => { invalidations++ } }, theme, () => 6)
    frame.handleInput('r'); frame.invalidate()
    expect(pressed).toBe('r')
    expect(invalidations).toBe(1)
    expect(frame.render(0)).toEqual([])
  })

  test('every tab preserves source data and fits terminal columns and rows', () => {
    const snap = snapshot()
    snap.tools.by_tool[0]!.tool = '读取工具🧪'.repeat(15) + '\nmore'
    snap.issues[0]!.title = 'A long issue with 中文🧪 '.repeat(12)
    snap.timeline[0]!.preview = 'e\u0301 👩🏽‍💻 日志 '.repeat(30)
    snap.agents[0]!.label = '代理🧪'.repeat(20)
    const before = JSON.stringify(snap)
    for (const tab of TABS.keys()) for (const width of [1, 2, 4, 10, 18, 32, 48, 60, 80, 120]) for (const height of [1, 2, 3, 4, 12, 24, 60]) {
      const { pane } = harness({ snapshot: snap, tab }, height)
      const lines = pane.render(width)
      expect(lines.length).toBeLessThanOrEqual(height)
      for (const line of lines) {
        expect(visibleWidth(line)).toBeLessThanOrEqual(width)
        expect(line).not.toContain('\n')
        expect(line).not.toContain('\r')
      }
      pane.invalidate()
      expect(pane.render(width)).toEqual(lines)
    }
    expect(JSON.stringify(snap)).toBe(before)
    expect(harness().pane.render(0)).toEqual([])
  })

  test('seven tabs expose the complete diagnostics navigation', () => {
    const expected = ['Top issues', 'Files', 'Suggestion:', 'tool sequences', 'Activity by step', 'Window source:', 'Recent steps']
    for (const [tab, heading] of expected.entries()) {
      const result = plain(harness({ tab }, 200).pane.render(100))
      expect(result).toContain(heading)
      expect(result).toContain(TABS[tab]!)
      expect(result).toContain('r Refresh')
      expect(result).toContain('Esc Close')
    }
  })

  test('widget remains compact, provides entry points and shows one top issue', () => {
    const lines = renderOverview(snapshot(), 80, theme)
    const result = plain(lines)
    expect(result).toContain('/obs pane')
    expect(result).toContain('/obs dashboard')
    expect(result).toContain('Steps 311')
    expect(result).toContain('Edit loop')
    expect(result).not.toContain('Trends')
    expect(result).not.toContain('claude plugin validate failed')
    expect(lines.length).toBeLessThan(14)
    for (const width of [1, 2, 10, 24, 32, 56, 100]) {
      const frame = renderOverview(snapshot(), width, theme)
      expect(frame.length).toBeLessThanOrEqual(12)
      for (const line of frame) expect(visibleWidth(line)).toBeLessThanOrEqual(width)
    }
    expect(renderOverview(undefined, 0, theme)).toEqual([])
  })

  test('missing and measured zero evidence stay distinct in every relevant view', () => {
    const snap = snapshot()
    snap.tokens = { total: null, input: null, output: 0, reasoning: null, cache_read: null, cache_write: 0 }
    snap.timing.wall_clock_s = null
    snap.timing.p95_step_s = null
    snap.tools = { ...snap.tools, success_rate: null, failed: null, unknown: 3 }
    snap.context = { ...snap.context, current_occupancy: null, current_pct: null, window: null, compaction_count: 0 }
    snap.series.tokens = { steps: [1, 2], fresh: [null, 0], cache_read: [null, 0], cache_write: [null, 0], output: [null, 0] }
    snap.series.durations.seconds = [null, 0]
    const overview = plain(harness({ snapshot: snap }, 200).pane.render(100))
    expect(overview).toContain('Tokens —')
    expect(overview).toContain('Elapsed —')
    expect(overview).toContain('usage incomplete')
    expect(overview).toContain('3 unknown tool outcomes')
    expect(overview).toMatch(/\?[▁_]/)
    const context = plain(harness({ snapshot: snap, tab: 5 }, 200).pane.render(100))
    expect(context).toContain('input: —')
    expect(context).toContain('output: 0')
    expect(context).toContain('0 compactions')
    expect(context).toContain('— of — tokens')
    const tools = plain(harness({ snapshot: snap, tab: 1 }, 200).pane.render(100))
    expect(tools).toContain('— observed failures · 3 unknown outcomes')
    expect(tools).not.toContain('0 failed')
  })

  test('empty, loading and failed states offer recovery without losing last snapshot', () => {
    expect(plain(harness({ snapshot: undefined, busy: true }).pane.render(80))).toContain('Loading session data')
    expect(plain(harness({ snapshot: undefined, busy: false }).pane.render(80))).toContain('Press r')
    expect(plain(harness({ snapshot: { schema_version: 1, empty: true, reason: 'No turns captured yet.', transcript: { path: '/tmp/pi.jsonl' } } }).pane.render(80))).toContain('No turns captured yet.')
    const failed = plain(harness({ error: 'Could not read session' }).pane.render(80))
    expect(failed).toContain('Unable to refresh: Could not read session')
    expect(failed).toContain('Showing the last snapshot')
    expect(failed).toContain('Steps 311')
    expect(plain(renderOverview(undefined, 80, theme))).toContain('Waiting for session activity')
  })

  test('dashboard URL stays visible inside the pane and untrusted control sequences are removed', () => {
    const result = plain(harness({ dashboardUrl: 'http://127.0.0.1:7891/' }).pane.render(80))
    expect(result).toContain('Dashboard: http://127.0.0.1:7891/')
    expect(plain(harness({ watchPaused: true }).pane.render(100))).toContain('Watch paused (saved session)')
    const unsafe = harness({ dashboardUrl: 'http://127.0.0.1:7891/\x1b]52;c;c2VjcmV0\x07' }).pane.render(80).join('\n')
    expect(unsafe).not.toContain('52;c;')
    for (const width of [1, 2, 12, 32, 80]) for (const line of harness({ dashboardUrl: 'http://127.0.0.1:7891/' }, 16).pane.render(width)) expect(visibleWidth(line)).toBeLessThanOrEqual(width)
  })

  test('terminal sequences and controls from source fields never reach output', () => {
    const snap = snapshot()
    const injection = '\x1b[31mINJECTED\x1b[0m\x1b]52;c;c2VjcmV0\x07\x1b[2J\x00\x1bPescape payload\x1b\\'
    snap.issues[0]!.title = injection
    snap.trajviz.warnings = [injection]
    snap.tools.by_tool[0]!.tool = injection
    for (const tab of TABS.keys()) {
      const lines = harness({ snapshot: snap, tab }, 500).pane.render(80)
      const joined = lines.join('\n')
      expect(joined).not.toContain('52;c;')
      expect(joined).not.toContain('\x1b[2J')
      expect(joined).not.toContain('escape payload')
      expect(joined).not.toContain('\x00')
    }
    expect(plain(renderOverview(snap, 80, theme))).toContain('INJECTED')
  })

  test('theme colors distinguish headings, measured health and problems', () => {
    const snap = snapshot()
    snap.health = [
      { metric: 'working', status: 'ok', label: 'good', detail: 'observed' },
      { metric: 'attention', status: 'warning', label: 'check', detail: 'observed' },
      { metric: 'failure', status: 'error', label: 'failed', detail: 'observed' },
    ]
    const result = harness({ snapshot: snap }, 150).pane.render(100).join('\n')
    expect(result).toContain('\x1b[32mworking:')
    expect(result).toContain('\x1b[33mattention:')
    expect(result).toContain('\x1b[31mfailure:')
    expect(result).toContain('\x1b[34mTop issues')
  })

  test('native model-pricing estimates are marked while provider costs retain exact zeroes', () => {
    const snap = snapshot()
    snap.engine_usage = { cost: { usd: 0, source: 'estimate' } }
    expect(plain(renderOverview(snap, 100, theme))).toContain('Cost ~$0.00')
    expect(plain(harness({ snapshot: snap }, 100).pane.render(100))).toContain('Cost ~$0.00')
    snap.engine_usage.cost!.source = 'provider'
    const reported = plain(renderOverview(snap, 100, theme))
    expect(reported).toContain('Cost $0.00')
    expect(reported).not.toContain('~$')
    snap.engine_usage.cost!.usd = 1.23
    expect(plain(renderOverview(snap, 100, theme))).toContain('Cost $1.23')
    delete snap.engine_usage.cost
    expect(plain(renderOverview(snap, 100, theme))).not.toContain('Cost $')
  })

  test('sparkline bins preserve spikes, gaps and zeroes with ASCII fallback', () => {
    const original = process.env.TERM
    try {
      process.env.TERM = 'xterm-256color'
      expect(sparkline([null, 0, 10, 0], 4)).toBe('?▁█▁')
      expect(sparkline([0, 0, 100, 0], 2)).toBe('▁█')
      expect(sparkline([null, Number.NaN], 2)).toBe('??')
      expect(sparkline([], 10)).toBe('No samples')
      process.env.TERM = 'dumb'
      expect(sparkline([null, 0, 10], 3)).toBe('?_#')
      expect(harness({ tab: 1 }).pane.render(80).join('\n')).not.toMatch(/[━─]/)
    } finally { if (original === undefined) delete process.env.TERM; else process.env.TERM = original }
  })
})

describe('Pi diagnostics interactions', () => {
  test('number, arrow and Tab keys select tabs and wrap in both directions', () => {
    const { pane, state } = harness()
    for (let i = 1; i <= 7; i++) { pane.handleInput(String(i)); expect(state.tab).toBe(i - 1) }
    pane.handleInput('\x1b[C'); expect(state.tab).toBe(0)
    pane.handleInput('\x1b[D'); expect(state.tab).toBe(6)
    pane.handleInput('\t'); expect(state.tab).toBe(0)
    pane.handleInput('\x1b[Z'); expect(state.tab).toBe(6)
  })

  test('actions are routed once, busy refreshes and key releases are ignored', () => {
    const { pane, calls, state } = harness()
    pane.handleInput('r')
    state.busy = true
    pane.handleInput('r')
    pane.handleInput('d')
    pane.handleInput('w')
    pane.handleInput('\x1b[114;1:3u')
    pane.handleInput('\x1b')
    expect(calls).toEqual(['refresh', 'dashboard', 'watch', 'close'])
    expect(state.watch).toBe(false)
    expect(plain(pane.render(80))).toContain('Watch off')
  })

  test('scrolling reaches all issue evidence and clamps after resize or refresh', () => {
    const { pane, state } = harness({ tab: 2 }, 15)
    const first = plain(pane.render(70))
    pane.handleInput('\x1b[6~')
    const second = plain(pane.render(70))
    expect(second).not.toBe(first)
    pane.handleInput('\x1b[F')
    const bottom = plain(pane.render(70))
    expect(bottom).toContain('Bottlenecks')
    expect(bottom).toContain('4,120 output tokens')
    pane.handleInput('\x1b[6~')
    expect(plain(pane.render(70))).toBe(bottom)
    pane.handleInput('\x1b[H')
    expect(plain(pane.render(70))).toBe(first)
    pane.handleInput('\x1b[5~')
    expect(plain(pane.render(70))).toBe(first)
    pane.handleInput('\x1b[B')
    expect(plain(pane.render(70))).not.toBe(first)
    pane.handleInput('\x1b[A')
    expect(plain(pane.render(70))).toBe(first)
    pane.handleInput('\x1b[F')
    state.snapshot = undefined
    expect(plain(pane.render(70))).toContain('No snapshot yet')
    state.snapshot = snapshot()
    pane.handleInput('1')
    expect(plain(pane.render(70))).toContain('Steps 311')
  })

  test('local tab navigation works when controller does not persist the selected tab', () => {
    const { state, actions } = harness()
    delete actions.selectTab
    const pane = new ObsPane(() => state, actions, theme)
    pane.handleInput('6')
    expect(plain(pane.render(80))).toContain('Window source:')
    expect(state.tab).toBe(0)
  })
})
