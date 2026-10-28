import { expect, mock, test, type Engine, type MockClock } from 'claude-code/testing'
import type { On, PaneOpenArgs, RenderSurface, UiOpenResult } from 'claude-code'
import { parseArgs } from './lib/args'
import { SNAPSHOT } from './testdata/snapshot'
import { PANE_USAGE_TIMEOUT_MS } from './render/pane'

const HOME = '/Users/me'
const CWD = '/Users/me/p'
const CACHE = `${HOME}/.cache/trajviz-obs`
const VENV_PY = `${CACHE}/envs/0123456789abcdef01234567/0123456789abcdef0123456789abcdef/bin/python`
const STATE_PATH = `${CACHE}/state.json`
const STATE_JSON = JSON.stringify({ revision: 4, python: VENV_PY, trajviz: '0.2.1', extras: ['llm', 'charts'], source: 'agent-trajviz>=0.2.0,<0.3', createdAt: 1 })
const DERIVED = `${HOME}/.claude/projects/-Users-me-p/abc.jsonl`
const USAGE = { startedAt: 0, context: { window: 1_000_000, percent: 24 }, rateLimits: [], cost: { usd: 1.25 } }
const BREAKDOWN = {
  ...USAGE,
  context: {
    ...USAGE.context,
    breakdown: {
      categories: [
        { name: 'System prompt', tokens: 12_000, color: 'promptBorder', isDeferred: false, kind: 'used' },
        { name: 'Messages', tokens: 228_000, color: 'inactive', isDeferred: false, kind: 'used' },
        { name: 'Free space', tokens: 760_000, color: 'permission', isDeferred: false, kind: 'free' },
      ],
      totalTokens: 240_000,
      maxTokens: 1_000_000,
      rawMaxTokens: 1_000_000,
      autocompactSource: 'default',
      percentage: 24,
      gridRows: [],
      model: 'claude-sonnet-4-5',
      memoryFiles: [],
      mcpTools: [],
      agents: [],
      isAutoCompactEnabled: true,
    },
  },
}
const NOW = 1_761_150_000_000

/** A second snapshot, told from the fixture by its step count. */
const SECOND = { ...SNAPSHOT, counts: { ...SNAPSHOT.counts, assistant_steps: 350 } }

type RunAnswer = { exitCode: number; stdout: string }
const json = (doc: unknown): RunAnswer => ({ exitCode: 0, stdout: `${JSON.stringify(doc)}\n` })

type Host = {
  clock: MockClock
  runs: string[]
  opens: PaneOpenArgs[]
  closes: string[]
  snapshots: number
  usageArgs: unknown[]
  bridgeCalls: string[][]
  usageBlock: { wait: Promise<void>; started: () => void } | null
}

/**
 * The host beneath `/obs` and its pane: env, files, session, usage (the
 * breakdown when asked), the bridge's `snapshot` answers in order (the last
 * one repeats), and the engine's pane calls recorded.
 */
function host(on: On, snapshots: unknown[], placed: UiOpenResult = { isPlaced: true }): Host {
  const clock = mock.clock(on, { now: NOW })
  const h: Host = { clock, runs: [], opens: [], closes: [], snapshots: 0, usageArgs: [], bridgeCalls: [], usageBlock: null }
  on('command.register', () => ({ value: { command: 'obs' } }))
  on('tool.register', () => ({ value: { tool: 'mcp__trajviz-obs__obs' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: 'abc' }))
  on('session.cwd', () => ({ value: CWD }))
  on('session.usage', async (_, e) => {
    h.usageArgs.push(e)
    const block = h.usageBlock
    h.usageBlock = null
    if (block) { block.started(); await block.wait }
    return { value: e?.breakdown ? BREAKDOWN : USAGE }
  })
  mock.env(on, { HOME })
  on('fs.read', (_, e) => (e.path === STATE_PATH ? { value: STATE_JSON } : { deny: `ENOENT: ${e.path}` }))
  on('fs.exists', (_, e) => ({ value: e.path === VENV_PY || e.path === DERIVED }))
  on('ui.toast', () => ({ value: undefined }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.open', (_, e) => {
    h.opens.push(e)
    return { value: placed }
  })
  on('ui.close', (_, e) => {
    h.closes.push(e.id)
    return { value: undefined }
  })
  on('process.run', (_, e) => {
    h.bridgeCalls.push(e.argv.slice(3))
    if (e.argv[3] === 'resolve') return { value: { exitCode: 0, stdout: JSON.stringify({ path: '/outside/my run.jsonl', session_id: 'other', kind: 'file' }), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    if (e.argv[3] !== 'snapshot') return { deny: `cannot start ${e.argv[0]}` }
    const a = json(snapshots[Math.min(h.snapshots++, snapshots.length - 1)])
    return { value: { exitCode: a.exitCode, stdout: a.stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  // Match native re-entry: a plugin's queued command cannot reach its own
  // command.run handler. Actions must dispatch directly and print the result.
  on('command.run', (_, e) => {
    if (e.origin.kind === 'plugin') throw new Error('native command.run re-entry')
    return { text: 'unexpected command.run fallback' }
  })
  on('ui.log', (_, e) => {
    if (e.to === 'transcript' && e.text.startsWith('/obs ')) h.runs.push(e.text.split('\n')[0]!.slice(5))
    return { value: undefined }
  })
  return h
}

type Presentation = { isFullscreen: boolean; columns: number }
const MAIN: Presentation = { isFullscreen: false, columns: 80 }
const WIDE: Presentation = { isFullscreen: true, columns: 160 }

const obs = ($: { command: { run: (args: never) => Promise<{ text?: string }> } }, args = '', presentation: Presentation = MAIN): Promise<string> =>
  $.command.run({ command: 'obs', args, origin: { kind: 'composer' }, presentation } as never).then(r => r.text ?? '')

/** The pane as a surface mounts it, `bodyColumns` wide. */
const pane = (surface: RenderSurface, bodyColumns: number) =>
  ({
    plugin: 'trajviz-obs',
    surface,
    component: 'Pane',
    requestId: 'obs',
    props: { title: 'obs', isFocused: true, bodyColumns, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} },
  }) as const

const TABS = ['Overview', 'Tools', 'Issues', 'Patterns', 'Agents', 'Context', 'Timeline']

/** The next usage RPC stays pending until the test releases it. */
function blockUsage(h: Host): { started: Promise<void>; release: () => void } {
  let started: () => void = () => {}
  let release: () => void = () => {}
  const entered = new Promise<void>(resolve => { started = resolve })
  const wait = new Promise<void>(resolve => { release = resolve })
  h.usageBlock = { wait, started }
  return { started: entered, release }
}

test('pending live usage falls back to the captured figures without leaving a blank pane', async ($, on) => {
  const h = host(on, [SNAPSHOT])
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  await obs($)
  const blocked = blockUsage(h)
  let drawn = false
  const mounting = $.ui.mount(pane('terminal', 56)).then(ui => { drawn = true; return ui })
  await blocked.started
  await h.clock.advance(PANE_USAGE_TIMEOUT_MS - 1)
  expect(drawn).toBe(false)
  await h.clock.advance(1)
  const wasDrawn = drawn
  blocked.release()
  const ui = await mounting
  expect(wasDrawn).toBe(true)
  expect(await ui.find({ key: 'refresh' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^24%$/ })).toBeDefined()
  await ui.unmount()
})

test('a delayed Context breakdown keeps controls and captured usage available', async ($, on) => {
  const h = host(on, [SNAPSHOT])
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  await obs($)
  const before = await $.ui.mount(pane('terminal', 56))
  await before.press({ key: 'tab:6' })
  await before.unmount()
  const blocked = blockUsage(h)
  const mounting = $.ui.mount(pane('terminal', 56))
  await blocked.started
  await h.clock.advance(PANE_USAGE_TIMEOUT_MS)
  const ui = await mounting
  expect(await ui.find({ key: 'tab:6' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /engine context · 24%/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /no breakdown from the engine/ })).toBeDefined()
  blocked.release()
  await ui.unmount()
})

test('a failed pane drawing offers retry and close without reading the broken state again', async ($, on) => {
  const h = host(on, [SNAPSHOT])
  let broken = true
  let latestReads = 0
  on('state.get', { plugin: 'trajviz-obs', key: 'latest' }, (_, e, next) => {
    latestReads++
    if (broken) return { deny: 'transient pane state read failure' }
    return next(e)
  })
  const ui = await $.ui.mount(pane('terminal', 56))
  expect(await ui.find({ type: 'Text', text: /Unable to draw the obs pane/ })).toBeDefined()
  expect(await ui.find({ key: 'retry' })).toBeDefined()
  expect(latestReads).toBe(1)
  broken = false
  await ui.press({ key: 'retry' })
  expect(await ui.find({ key: 'refresh' })).toBeDefined()
  expect(await ui.find({ key: 'retry' })).toBeUndefined()
  await ui.unmount()
  broken = true
  const failed = await $.ui.mount(pane('terminal', 56))
  await failed.press({ key: 'close' })
  expect(h.closes).toEqual(['obs'])
  await failed.unmount()
})

test('terminal output escape sequences in snapshot text do not invalidate a pane', async ($, on) => {
  const marked = {
    ...SNAPSHOT,
    tools: { ...SNAPSHOT.tools, by_tool: SNAPSHOT.tools.by_tool.map((tool, i) => i === 0 ? { ...tool, tool: '\u0000Bash' } : tool) },
    issues: SNAPSHOT.issues.map((issue, i) => i === 0 ? { ...issue, title: '\u001b[31mObserved failure\u001b[0m', evidence: 'tool output\u0000after nul' } : issue),
    patterns: { ...SNAPSHOT.patterns, repeated_searches: [{ query: 'search\u001b[31m output\u001b[0m', count: 3 }] },
    agents: SNAPSHOT.agents.map((agent, i) => i === 0 ? { ...agent, label: 'Agent diagnostic\u0007' } : agent),
    context: { ...SNAPSHOT.context, window_source: 'engine\u001b[31m' },
    timeline: SNAPSHOT.timeline.map((step, i) => i === 0 ? { ...step, preview: '\u001b]0;terminal title\u0007Timeline diagnostic text\u001b[32m\u0000\rprogress\u001b[0m' } : step),
  }
  host(on, [marked])
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  await obs($)
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount(pane(surface, 56))
    await ui.press({ key: 'tab:1' })
    expect(await ui.find({ key: 'refresh' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /Observed failure/ })).toBeDefined()
    await ui.press({ key: 'tab:3' })
    expect(await ui.find({ type: 'Text', text: /tool output.*after nul/ })).toBeDefined()
    await ui.press({ key: 'tab:7' })
    expect(await ui.find({ type: 'Text', text: /Timeline diagnostic text/ })).toBeDefined()
    for (let tab = 1; tab <= 7; tab++) {
      await ui.press({ key: `tab:${tab}` })
      expect(await ui.find({ type: 'Text', text: TABS[tab - 1] })).toBeDefined()
      expect(await ui.find({ key: 'refresh' })).toBeDefined()
      expect(await ui.find({ key: 'retry' })).toBeUndefined()
    }
    await ui.unmount()
  }
})

test('the pane draws the latest snapshot with its tabs, on terminal and desktop', async ($, on) => {
  host(on, [SNAPSHOT])
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  await obs($)

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount(pane(surface, 80))
    // The tab bar: seven plain Buttons with hotkeys 1–7, the open one primary.
    for (const [i, label] of TABS.entries()) {
      const tab = await ui.find({ key: `tab:${i + 1}` })
      expect(tab?.type).toBe('Button')
      expect(tab?.props).toMatchObject({ label, hotkey: String(i + 1), plain: true })
    }
    expect((await ui.find({ key: 'tab:1' }))?.props['variant']).toBe('primary')
    expect((await ui.find({ key: 'tab:3' }))?.props['dimColor']).toBe(true)
    // The action row.
    expect((await ui.find({ key: 'refresh' }))?.props).toMatchObject({ hotkey: 'r' })
    expect((await ui.find({ key: 'watch' }))?.props).toMatchObject({ hotkey: 'w', label: 'Watch on' })
    expect((await ui.find({ key: 'dashboard' }))?.props).toMatchObject({ hotkey: 'd' })
    // The overview uses aligned metrics and compact trends at every dock width.
    expect((await ui.find({ type: 'Text', text: /^● tool success / }))?.props['color']).toBe('success')
    expect(await ui.find({ type: 'Text', text: /^311$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^24%$/ })).toBeDefined()
    expect(await ui.find({ key: 'pane-metrics' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'Latest phase: verify' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /Edit loop on hooks\/lib\/state\.ts/ })).toBeDefined()
    expect((await ui.find({ type: 'Text', text: /skipped sub-agent log/ }))?.props['color']).toBe('warning')
    for (const key of ['tokens', 'durations', 'context']) expect(await ui.find({ key: `trend:${key}` })).toBeDefined()
    expect(await ui.findAll({ type: 'Raster' })).toHaveLength(0)
    // The footer exposes freshness and watch state without repeating the metrics above.
    expect(await ui.find({ type: 'Text', text: /Updated .* · 311 steps · watch on/ })).toBeDefined()
    await ui.unmount()
  }
})

/** Opens tab `n` as a hotkey does (a press on a mounted pane) and mounts the pane afresh on it. */
async function openTab($: Engine, n: number, bodyColumns = 80) {
  const before = await $.ui.mount(pane('terminal', bodyColumns))
  expect(await before.press({ key: `tab:${n}` })).toEqual({ element: `tab:${n}` })
  await before.unmount()
  const ui = await $.ui.mount(pane('terminal', bodyColumns))
  expect((await ui.find({ key: `tab:${n}` }))?.props['variant']).toBe('primary')
  return ui
}

test('the hotkeys switch tabs, and each tab draws its own rows', async ($, on) => {
  host(on, [SNAPSHOT])
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  await obs($)

  // Issues: every issue with its steps, evidence and why, and the bottlenecks; no KPIs.
  let ui = await openTab($, 3)
  expect((await ui.find({ key: 'tab:1' }))?.props['dimColor']).toBe(true)
  expect(await ui.findAll({ type: 'Text', text: /^\d\. / })).toHaveLength(3)
  expect(await ui.find({ type: 'Text', text: /steps 140, 142, 144/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /Edit: hooks\/lib\/state\.ts/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /step 231 184\.2s/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^311$/ })).toBeUndefined()
  expect(await ui.findAll({ type: 'Raster' })).toHaveLength(0)
  await ui.unmount()

  // Tools: the totals, the bars and the files.
  ui = await openTab($, 2)
  expect(await ui.find({ type: 'Text', text: /^196 calls · 96% ok · 7 failed$/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^Bash/ })).toBeDefined()
  expect((await ui.find({ type: 'Text', text: /5 err/ }))?.props['color']).toBe('error')
  expect((await ui.find({ type: 'Markdown' }))?.text).toContain('| hooks/lib/state.ts | 4 | 7 |')
  await ui.unmount()

  // Patterns: one table per non-empty list, named, the empty ones left out.
  ui = await openTab($, 4)
  const tables = await ui.findAll({ type: 'Markdown' })
  expect(tables.length).toBe(8)
  expect(tables.some(t => t.text?.includes('| sequence | count |'))).toBe(true)
  expect(await ui.find({ type: 'Text', text: /^Hotspots$/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^Failure chains$/ })).toBeUndefined()
  await ui.unmount()

  // Agents: the table and the swimlane.
  ui = await openTab($, 5)
  expect(await ui.find({ type: 'Text', text: /Agent: Builder check/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^Type: general-purpose$/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^Steps: 49$/ })).toBeDefined()
  expect((await ui.find({ key: 'swimlane' }))?.type).toBe('Raster')
  await ui.unmount()

  // Context: TrajViz's numbers and series beside the engine's own breakdown.
  ui = await openTab($, 6)
  expect(await ui.find({ type: 'Text', text: /window 1M \(engine\) · now 71% \(712k\) · peak 75% \(745k\) · 1 compactions · largest drop 402k/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /compactions at turns 209/ })).toBeDefined()
  expect((await ui.find({ key: 'context' }))?.type).toBe('Raster')
  expect(await ui.find({ type: 'Text', text: /^engine context · 24% of 1M$/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^System prompt\s+12k$/ })).toBeDefined()
  expect((await ui.find({ type: 'Text', text: /^Free space\s+760k$/ }))?.props['dimColor']).toBe(true)
  await ui.unmount()

  // Timeline: the last steps as a table.
  ui = await openTab($, 7)
  expect(await ui.find({ type: 'Markdown' })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: /Step: 311$/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^Role: assistant$/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /Bash: claude plugin test \./ })).toBeDefined()
  await ui.unmount()

  // Back to the overview.
  ui = await openTab($, 1)
  expect(await ui.find({ type: 'Text', text: /^311$/ })).toBeDefined()
  await ui.unmount()
})

test('unavailable health metrics remain unavailable rather than becoming good', async ($, on) => {
  host(on, [{ ...SNAPSHOT, health: [{ metric: 'throughput', status: 'good', label: 'N/A', detail: 'No timing data' }] }])
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  await obs($)
  const ui = await $.ui.mount(pane('terminal', 80))
  expect(await ui.find({ type: 'Text', text: '● throughput N/A' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: '● throughput good' })).toBeUndefined()
  await ui.unmount()
})

test('a narrow overview keeps readable metrics and trends with a route to detailed charts', async ($, on) => {
  host(on, [SNAPSHOT])
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  await obs($)
  const ui = await $.ui.mount(pane('terminal', 40))
  expect(await ui.find({ key: 'pane-metrics' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^95%$/ })).toBeDefined()
  const drag = await ui.find({ type: 'Text', text: /Open Dashboard for detailed charts/ })
  expect(drag?.text).toBe('Open Dashboard for detailed charts.')
  expect(drag?.props['dimColor']).toBe(true)
  expect(await ui.findAll({ type: 'Raster' })).toHaveLength(0)
  expect(await ui.find({ key: 'phases' })).toBeUndefined()
  expect(await ui.find({ key: 'pane-trends' })).toBeDefined()
  // The tabs still switch.
  expect((await ui.find({ key: 'tab:3' }))?.props['hotkey']).toBe('3')
  await ui.unmount()
})

test('the default dock leads with compact metrics and issue titles while preserving detailed issue navigation', async ($, on) => {
  host(on, [SNAPSHOT])
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  await obs($)
  const ui = await $.ui.mount(pane('terminal', 56))
  expect(await ui.find({ key: 'pane-metrics' })).toBeDefined()
  for (const value of ['311', '24%', '$1.25']) expect(await ui.find({ type: 'Text', text: value })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^Top issues$/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /Edit loop on hooks\/lib\/state\.ts/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /Run the test after each edit/ })).toBeUndefined()
  expect(await ui.findAll({ type: 'Raster' })).toHaveLength(0)
  expect(await ui.find({ type: 'Text', text: /Press 3 for evidence/ })).toBeDefined()
  await ui.press({ key: 'tab:3' })
  await ui.unmount()
  const details = await $.ui.mount(pane('terminal', 56))
  expect(await details.find({ type: 'Text', text: /Run the test after each edit/ })).toBeDefined()
  expect(await details.find({ type: 'Text', text: /steps 140, 142, 144/ })).toBeDefined()
  await details.unmount()
})

test('a short inline pane avoids chart stacks and long issue evidence even on a wide terminal', async ($, on) => {
  host(on, [SNAPSHOT])
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  await obs($)
  const event = pane('terminal', 100)
  const ui = await $.ui.mount({ ...event, props: { ...event.props, placement: 'inline', scroll: { offset: 0, bodyRows: 14 } } })
  expect(await ui.find({ key: 'pane-metrics' })).toBeDefined()
  expect(await ui.find({ key: 'pane-trends' })).toBeUndefined()
  expect(await ui.find({ key: 'latest-phase' })).toBeUndefined()
  expect(await ui.findAll({ type: 'Raster' })).toHaveLength(0)
  expect(await ui.findAll({ type: 'Text', text: /^\d\. / })).toHaveLength(1)
  expect(await ui.find({ type: 'Text', text: /2 more/ })).toBeDefined()
  expect((await ui.find({ key: 'dashboard' }))?.props['variant']).toBe('primary')
  await ui.unmount()
})

test('the action keys refresh, toggle watch, and run dashboard', async ($, on) => {
  const h = host(on, [SNAPSHOT, SECOND])
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  await obs($)
  expect(h.snapshots).toBe(1)

  let ui = await $.ui.mount(pane('terminal', 80))
  expect(await ui.find({ type: 'Text', text: /^311$/ })).toBeDefined()
  // `r`: one more snapshot through the bridge, stored as the latest, no `/obs` row.
  await ui.press({ key: 'refresh' })
  expect(h.snapshots).toBe(2)
  expect(h.runs).toEqual([])
  await ui.unmount()
  ui = await $.ui.mount(pane('terminal', 80))
  expect(await ui.find({ type: 'Text', text: /^350$/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^311$/ })).toBeUndefined()
  // `w`: watch off, then on again.
  await ui.press({ key: 'watch' })
  await ui.unmount()
  ui = await $.ui.mount(pane('terminal', 80))
  expect((await ui.find({ key: 'watch' }))?.props['label']).toBe('Watch off')
  expect(await ui.find({ type: 'Text', text: /Updated .* · 350 steps · watch off$/ })).toBeDefined()
  await ui.press({ key: 'watch' })
  await ui.unmount()
  ui = await $.ui.mount(pane('terminal', 80))
  expect((await ui.find({ key: 'watch' }))?.props['label']).toBe('Watch on')
  // `d` opens the full interactive dashboard.
  await ui.press({ key: 'dashboard' })
  expect(h.runs).toEqual(['dashboard'])
  await ui.unmount()
  // The refresh's snapshot took id 2: the next `/obs` is 3.
  expect(await obs($)).toMatch(/^obs snapshot 3 · /)
})

test('a running dashboard is named beside its key', async ($, on) => {
  host(on, [SNAPSHOT])
  on('state.get', { plugin: 'trajviz-obs', key: 'dashboards' }, () => ({
    value: { value: { insight: { url: 'http://127.0.0.1:7860', port: 7860 }, converge: null }, version: 1 },
  }))
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  await obs($)
  const ui = await $.ui.mount(pane('terminal', 80))
  expect((await ui.find({ type: 'Text', text: /Dashboard:/ }))?.text).toBe('Dashboard: http://127.0.0.1:7860')
  await ui.unmount()
})

test('the pane without a snapshot says so, and with an empty one', async ($, on) => {
  const h = host(on, [{ schema_version: 1, empty: true, reason: 'no_steps', transcript: { path: DERIVED } }])
  await $.session.start({ cwd: CWD, surface: 'desktop', isInteractive: true })
  let ui = await $.ui.mount(pane('desktop', 80))
  expect((await ui.find({ type: 'Text', text: /No snapshot yet/ }))?.text).toBe('No snapshot yet: press r or run /obs.')
  expect((await ui.find({ key: 'refresh' }))?.props['hotkey']).toBe('r')
  expect(await ui.find({ key: 'tab:3' })).toBeDefined()
  await ui.press({ key: 'dashboard' })
  expect(h.runs).toEqual(['dashboard'])
  await ui.unmount()

  // An empty result is stored too, so the pane replaces any preceding data.
  expect(await obs($, 'pane')).toBe('obs pane opened.\nNothing to analyze yet: this session has no assistant steps. Run /obs after the first turn.')
  expect(h.snapshots).toBe(1)
  ui = await $.ui.mount(pane('desktop', 80))
  expect(await ui.find({ type: 'Text', text: /Nothing to analyze yet/ })).toBeDefined()
  await ui.unmount()
})

test('an empty latest snapshot draws the empty row', async ($, on) => {
  host(on, [SNAPSHOT])
  on('state.get', { plugin: 'trajviz-obs', key: 'latest' }, () => ({
    value: { value: { schema_version: 1, empty: true, reason: 'no_steps', transcript: { path: DERIVED } }, version: 1 },
  }))
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount(pane('terminal', 80))
  expect((await ui.find({ type: 'Text', text: /Nothing to analyze yet/ }))?.text).toBe('Nothing to analyze yet: this session has no assistant steps. Run /obs after the first turn.')
  expect(await ui.find({ type: 'Text', text: /computed in/ })).toBeUndefined()
  await ui.unmount()
})

test('empty and not-yet-loaded saved-session panes keep dashboard actions on the selection', async ($, on) => {
  const h = host(on, [SNAPSHOT])
  const path = '/outside/my run.jsonl'
  let snapshot: unknown = null
  on('state.get', { plugin: 'trajviz-obs', key: 'latest' }, () => ({ value: { value: snapshot, version: 1 } }) as never)
  on('state.get', { plugin: 'trajviz-obs', key: 'session' }, () => ({ value: { value: { transcriptPath: DERIVED, sessionId: 'abc', selected: { path, id: 'other', label: 'other' } }, version: 1 } }))
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  for (const value of [null, { schema_version: 1, empty: true, reason: 'no_steps', transcript: { path } }]) {
    snapshot = value
    const ui = await $.ui.mount(pane('terminal', 80))
    await ui.press({ key: 'dashboard' })
    await ui.unmount()
  }
  expect(h.runs.map(args => parseArgs(args).flags['session'])).toEqual([path, path])
})

test('/obs pane opens with focus, inline on the main screen; pane close closes', async ($, on) => {
  const h = host(on, [SNAPSHOT])
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })

  // On the main screen: inline, 14 rows, focused; with no snapshot yet one is taken.
  expect(await obs($, 'pane')).toBe('obs pane opened.')
  expect(h.opens).toEqual([{ id: 'obs', title: 'obs', focus: true, rows: 14 }])
  expect(h.snapshots).toBe(1)
  // Fullscreen: docked at 56 columns; the snapshot is kept.
  expect(await obs($, 'pane', WIDE)).toBe('obs pane opened.')
  expect(h.opens[1]).toEqual({ id: 'obs', title: 'obs', focus: true, columns: 56 })
  expect(h.snapshots).toBe(1)

  expect(await obs($, 'pane close')).toBe('obs pane closed.')
  expect(h.closes).toEqual(['obs'])
})

test('/obs pane --session refreshes an existing pane and its refresh keeps the selected path', async ($, on) => {
  const h = host(on, [SNAPSHOT, { ...SECOND, transcript: { ...SECOND.transcript, path: '/outside/my run.jsonl', session_id: 'other', kind: 'other' } }])
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  await obs($)
  expect(await obs($, 'pane --session "/outside/my run.jsonl"')).toBe('obs pane opened.')
  expect(h.snapshots).toBe(2)
  expect(h.bridgeCalls.filter(c => c[0] === 'resolve').map(c => c.slice(-2))).toEqual([['--session', '/outside/my run.jsonl']])
  const ui = await $.ui.mount(pane('terminal', 80))
  expect(await ui.find({ type: 'Text', text: /^350$/ })).toBeDefined()
  await ui.press({ key: 'dashboard' })
  expect(h.runs.map(args => parseArgs(args))).toEqual([
    { sub: 'dashboard', positional: [], flags: { session: '/outside/my run.jsonl' }, passthrough: [] },
  ])
  await ui.press({ key: 'refresh' })
  await ui.unmount()
  expect(h.bridgeCalls.filter(c => c[0] === 'resolve').map(c => c.slice(-2))).toEqual([['--session', '/outside/my run.jsonl'], ['--session', '/outside/my run.jsonl']])
})

test('/obs opens the pane unasked only beside a wide fullscreen transcript', async ($, on) => {
  const h = host(on, [SNAPSHOT])
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  await obs($, '', MAIN)
  await obs($, '', { isFullscreen: true, columns: 139 })
  expect(h.opens).toEqual([])
  await obs($, '', { isFullscreen: true, columns: 140 })
  expect(h.opens).toEqual([{ id: 'obs', title: 'obs', columns: 56 }])
})

test('an open the surface cannot place is said in the row', async ($, on) => {
  host(on, [SNAPSHOT], { isPlaced: false, reason: 'no-surface: none attached' })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  expect(await obs($, 'pane')).toBe('obs pane is open but not drawn: no-surface: none attached')
})
