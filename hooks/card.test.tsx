import { expect, mock, test } from 'claude-code/testing'
import type { On, PaneOpenArgs, RenderSurface, RenderViewport } from 'claude-code'
import { parseArgs } from './lib/args'
import { SNAPSHOT } from './testdata/snapshot'

const HOME = '/Users/me'
const CWD = '/Users/me/p'
const CACHE = `${HOME}/.cache/trajviz-obs`
const VENV_PY = `${CACHE}/envs/0123456789abcdef01234567/0123456789abcdef0123456789abcdef/bin/python`
const STATE_PATH = `${CACHE}/state.json`
const STATE_JSON = JSON.stringify({ revision: 4, python: VENV_PY, trajviz: '0.2.1', extras: ['llm', 'charts'], source: 'agent-trajviz>=0.2.0,<0.3', createdAt: 1 })
const DERIVED = `${HOME}/.claude/projects/-Users-me-p/abc.jsonl`
const USAGE = { startedAt: 0, context: { window: 1_000_000, percent: 24 }, rateLimits: [], cost: { usd: 1.25 } }
const NOW = 1_761_150_000_000

/** A second snapshot, told from the fixture by its step count. */
const SECOND = { ...SNAPSHOT, counts: { ...SNAPSHOT.counts, assistant_steps: 350 } }

type RunAnswer = { exitCode: number; stdout: string }
const json = (doc: unknown): RunAnswer => ({ exitCode: 0, stdout: `${JSON.stringify(doc)}\n` })

/**
 * The host beneath `/obs`: env, files, session, usage and the bridge's
 * `snapshot` answers in order (the last one repeats); the engine's own
 * CommandOutput row is the Text `engine`.
 */
function host(on: On, snapshots: unknown[], bridge: Record<string, unknown> = {}): { runs: string[]; rows: string[]; calls: string[][]; opens: PaneOpenArgs[] } {
  let n = 0
  const runs: string[] = []
  const rows: string[] = []
  const calls: string[][] = []
  const opens: PaneOpenArgs[] = []
  on('command.register', () => ({ value: { command: 'obs' } }))
  on('tool.register', () => ({ value: { tool: 'mcp__trajviz-obs__obs' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: 'abc' }))
  on('session.cwd', () => ({ value: CWD }))
  on('session.usage', () => ({ value: USAGE }))
  mock.env(on, { HOME })
  mock.clock(on, { now: NOW })
  on('fs.read', (_, e) => (e.path === STATE_PATH ? { value: STATE_JSON } : { deny: `ENOENT: ${e.path}` }))
  on('fs.exists', (_, e) => ({ value: e.path === VENV_PY || e.path === DERIVED }))
  on('ui.toast', () => ({ value: undefined }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.open', (_, e) => {
    opens.push(e)
    return { value: { isPlaced: true } }
  })
  on('process.run', (_, e) => {
    calls.push(e.argv.slice(3))
    if (e.argv[3] && e.argv[3] in bridge) return { value: { exitCode: 0, stdout: JSON.stringify(bridge[e.argv[3]]), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    if (e.argv[3] === 'resolve') return { value: { exitCode: 0, stdout: JSON.stringify({ path: '/outside/saved.jsonl', session_id: 'saved', kind: 'file' }), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    if (e.argv[3] !== 'snapshot') return { deny: `cannot start ${e.argv[0]}` }
    const a = json(snapshots[Math.min(n++, snapshots.length - 1)])
    return { value: { exitCode: a.exitCode, stdout: a.stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>engine</Text>
  })
  // Match native re-entry: a plugin's queued command cannot reach its own
  // command.run handler. Actions must dispatch directly and print the result.
  on('command.run', (_, e) => {
    if (e.origin.kind === 'plugin') throw new Error('native command.run re-entry')
    return { text: 'unexpected command.run fallback' }
  })
  on('ui.log', (_, e) => {
    if (e.to === 'transcript') {
      rows.push(e.text)
      if (e.text.startsWith('/obs ')) runs.push(e.text.slice(5))
    }
    return { value: undefined }
  })
  return { runs, rows, calls, opens }
}

const obs = ($: { command: { run: (args: never) => Promise<{ text?: string }> } }, args = ''): Promise<string> =>
  $.command.run({ command: 'obs', args, origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 80 } } as never).then(r => r.text ?? '')

/** The CommandOutput row of `/obs` carrying `text`, as the surface mounts it. */
const row = (text: string, surface: RenderSurface, viewport: RenderViewport) =>
  ({ plugin: 'trajviz-obs', surface, component: 'CommandOutput', requestId: `row-${surface}-${viewport.columns}`, props: { command: 'obs', args: '', text, isErrored: false }, viewport }) as const

const BUTTON_KEYS = ['dashboard', 'pane', 'json', 'fixes', 'analyze']

test('terminal control text cannot discard the overview card or break its actions', async ($, on) => {
  const marked = { ...SNAPSHOT, issues: SNAPSHOT.issues.map((issue, i) => i === 0
    ? { ...issue, title: '\u001b[31mObserved failure\u001b[0m', evidence: 'tool output\u0000after nul' } : issue) }
  const { calls } = host(on, [marked], { json: { files: ['index.json'], bytes: 100, path: '/exports/views' } })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const text = await obs($)
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount(row(text, surface, { columns: 120, rows: 40, isFullscreen: true }))
    expect(await ui.find({ type: 'Text', text: /Observed failure/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /tool outputafter nul/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^engine$/ })).toBeUndefined()
    await ui.press({ key: 'json' })
    await ui.unmount()
  }
  expect(calls.filter(call => call[0] === 'json')).toHaveLength(2)
  expect(marked.issues[0]!.title).toBe('\u001b[31mObserved failure\u001b[0m')
})

test('card draws from its own snapshot id, not latest', async ($, on) => {
  host(on, [SNAPSHOT, SECOND])
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const first = await obs($)
  const second = await obs($)
  expect(first).toMatch(/^obs snapshot 1 · /)
  expect(second).toMatch(/^obs snapshot 2 · /)

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount(row(first, surface, { columns: 120, rows: 40, isFullscreen: true }))
    // The first card keeps snapshot 1's numbers after snapshot 2 became the latest.
    expect(await ui.find({ type: 'Text', text: /^311$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^350$/ })).toBeUndefined()
    expect(await ui.find({ type: 'Text', text: /^engine$/ })).toBeUndefined()
    // The provenance line with the snapshot's own compute time, dim.
    const head = await ui.find({ type: 'Text', text: /snapshot 1/ })
    expect(head?.text).toContain('842 ms')
    expect(head?.props['dimColor']).toBe(true)
    // Health chips, KPIs (live context from usage), tools, phases, issues and the warning.
    expect((await ui.find({ type: 'Text', text: /tool success/ }))?.props['color']).toBe('success')
    expect(await ui.find({ type: 'Text', text: /^24%$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^Bash/ })).toBeDefined()
    expect(await ui.find({ key: 'phases' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /Edit loop on hooks\/lib\/state\.ts/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /Compaction at 62%/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /step 231 184\.2s/ })).toBeDefined()
    expect((await ui.find({ type: 'Text', text: /skipped sub-agent log/ }))?.props['color']).toBe('warning')
    // The series: Rasters on the terminal only, the markers named on their labels.
    expect(await ui.find({ type: 'Text', text: /outliers at steps 97, 233/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /compactions at turns 209/ })).toBeDefined()
    if (surface === 'terminal') {
      for (const key of ['tokens', 'durations', 'context', 'swimlane']) expect((await ui.find({ key }))?.type).toBe('Raster')
      expect(await ui.findAll({ type: 'Raster' })).toHaveLength(4)
    } else {
      expect(await ui.findAll({ type: 'Raster' })).toHaveLength(0)
      expect((await ui.findAll({ type: 'Text', text: /[▁▂▃▄▅▆▇█·]{20,}/ })).length).toBeGreaterThanOrEqual(3)
    }
    await ui.unmount()
  }

  // The second card draws its own.
  const ui = await $.ui.mount(row(second, 'terminal', { columns: 120, rows: 40, isFullscreen: true }))
  expect(await ui.find({ type: 'Text', text: /^350$/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^311$/ })).toBeUndefined()
  await ui.unmount()
})

test('native plugin-prefixed command output renders the referenced old card on classic and fullscreen surfaces', async ($, on) => {
  host(on, [SNAPSHOT, SECOND])
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const first = await obs($)
  await obs($)
  for (const isFullscreen of [false, true]) {
    const ui = await $.ui.mount(row(`trajviz-obs: ${first}`, 'terminal', { columns: 80, rows: 32, isFullscreen }))
    expect(await ui.find({ key: 'kpis-summary' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^311$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^350$/ })).toBeUndefined()
    expect(await ui.find({ type: 'Text', text: /computed in 842 ms/ })).toBeDefined()
    if (isFullscreen) expect((await ui.find({ key: 'dashboard' }))?.props['variant']).toBe('primary')
    else expect(await ui.find({ type: 'Text', text: /\/obs dashboard/ })).toBeDefined()
    await ui.unmount()
  }
})

test('buttons only in fullscreen terminal', async ($, on) => {
  const { runs } = host(on, [SNAPSHOT])
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const text = await obs($)

  // The main screen: no button can be pressed, so the equivalent forms on one dim line.
  let ui = await $.ui.mount(row(text, 'terminal', { columns: 120, rows: 40, isFullscreen: false }))
  for (const key of BUTTON_KEYS) expect(await ui.find({ key })).toBeUndefined()
  const forms = await ui.find({ type: 'Text', text: /\/obs dashboard/ })
  expect(forms?.text).toBe('/obs dashboard · /obs pane · /obs json · /obs fixes · /obs analyze')
  expect(forms?.props['dimColor']).toBe(true)
  await ui.unmount()

  // Fullscreen: the button row, each pressing its `/obs` form.
  ui = await $.ui.mount(row(text, 'terminal', { columns: 120, rows: 40, isFullscreen: true }))
  for (const key of BUTTON_KEYS) expect((await ui.find({ key }))?.type).toBe('Button')
  expect((await ui.find({ key: 'dashboard' }))?.props['label']).toBe('Dashboard')
  expect((await ui.find({ key: 'json' }))?.props['label']).toBe('Export JSON')
  expect((await ui.find({ key: 'fixes' }))?.props['label']).toBe('AI fixes')
  expect((await ui.find({ key: 'analyze' }))?.props['label']).toBe('AI analyze')
  expect(await ui.find({ type: 'Text', text: /\/obs dashboard/ })).toBeUndefined()
  expect(await ui.press({ key: 'dashboard' })).toEqual({ element: 'dashboard' })
  await ui.press({ key: 'analyze' })
  expect(runs).toEqual(['dashboard', 'analyze'])
  await ui.unmount()

  // A remote surface presses buttons whatever its layout says.
  ui = await $.ui.mount(row(text, 'desktop', { columns: 120, rows: 40, isFullscreen: false }))
  expect((await ui.find({ key: 'dashboard' }))?.type).toBe('Button')
  await ui.unmount()
  ui = await $.ui.mount(row(text, 'vscode', { columns: 120, rows: 40 }))
  expect((await ui.find({ key: 'analyze' }))?.type).toBe('Button')
  await ui.unmount()
})

test('native card actions execute JSON and pane handlers without command re-entry and show their answers', async ($, on) => {
  const { rows, calls, opens } = host(on, [SNAPSHOT], { json: { files: ['index.json'], bytes: 100, path: '/exports/views' } })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const text = await obs($)
  const ui = await $.ui.mount(row(text, 'terminal', { columns: 80, rows: 40, isFullscreen: true }))
  await ui.press({ key: 'json' })
  await ui.press({ key: 'pane' })
  expect(calls.filter(call => call[0] === 'json')).toHaveLength(1)
  expect(calls.find(call => call[0] === 'json')).toContain(DERIVED)
  expect(opens).toEqual([{ id: 'obs', title: 'obs', focus: true, columns: 56 }])
  expect(rows).toEqual(['/obs json', '1 file (100 B) written to /exports/views.', '/obs pane', 'obs pane opened.'])
  await ui.unmount()
})

test('every action on an old card keeps its transcript after the native session changes', async ($, on) => {
  const { runs } = host(on, [SNAPSHOT])
  on('classic.SessionStart', () => ({}))
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const first = await obs($)
  await $.classic.SessionStart({ source: 'clear', transcript_path: '/logs/new-session.jsonl', session_id: 'new' })

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount(row(first, surface, { columns: 120, rows: 40, isFullscreen: true }))
    for (const key of BUTTON_KEYS) await ui.press({ key })
    await ui.unmount()
  }
  expect(runs.map(args => parseArgs(args))).toEqual([...BUTTON_KEYS, ...BUTTON_KEYS].map(sub => ({
    sub, positional: [], flags: { session: DERIVED }, passthrough: [],
  })))
})

test('saved-card actions quote its path and never follow a newer selected snapshot', async ($, on) => {
  const { runs } = host(on, [SNAPSHOT])
  const path = '/outside/my "quoted" run\\draft.jsonl'
  const saved = { ...SNAPSHOT, transcript: { ...SNAPSHOT.transcript, path, session_id: 'saved' } }
  on('state.get', { plugin: 'trajviz-obs', key: 'snapshot', id: '41' }, () => ({ value: { value: saved, version: 1 } }))
  on('state.get', { plugin: 'trajviz-obs', key: 'latest' }, () => ({ value: { value: SECOND, version: 1 } }))
  on('state.get', { plugin: 'trajviz-obs', key: 'session' }, () => ({ value: {
    value: { transcriptPath: null, sessionId: null, selected: { path: '/other/newer.jsonl', id: 'newer', label: 'newer' } }, version: 1,
  } }))
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount(row('obs snapshot 41 · saved', 'desktop', { columns: 120, rows: 40 }))
  await ui.press({ key: 'dashboard' })
  await ui.press({ key: 'analyze' })
  expect(runs.map(args => parseArgs(args))).toEqual([
    { sub: 'dashboard', positional: [], flags: { session: path }, passthrough: [] },
    { sub: 'analyze', positional: [], flags: { session: path }, passthrough: [] },
  ])
  await ui.unmount()
})

test('native transcript path keeps current-card actions plain even outside the derived project folder', async ($, on) => {
  const { runs } = host(on, [SNAPSHOT])
  const path = '/native/location/current.jsonl'
  on('classic.SessionStart', () => ({}))
  on('state.get', { plugin: 'trajviz-obs', key: 'snapshot', id: '51' }, () => ({ value: {
    value: { ...SNAPSHOT, transcript: { ...SNAPSHOT.transcript, path } }, version: 1,
  } }))
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  await $.classic.SessionStart({ source: 'startup', transcript_path: path, session_id: 'current' })
  const ui = await $.ui.mount(row('obs snapshot 51 · native', 'desktop', { columns: 120, rows: 40 }))
  await ui.press({ key: 'dashboard' })
  expect(runs).toEqual(['dashboard'])
  await ui.unmount()
})

test('a current-card Pane action clears a newer saved-session selection before opening', async ($, on) => {
  const saved = { ...SECOND, transcript: { ...SECOND.transcript, path: '/outside/saved.jsonl', session_id: 'saved' } }
  const { runs } = host(on, [SNAPSHOT, saved, SNAPSHOT])
  const selected: unknown[] = []
  const latest: unknown[] = []
  on('state.set', (_, e, next) => {
    if (e.plugin === 'trajviz-obs' && e.key === 'session') selected.push((e.value as { selected: unknown }).selected)
    if (e.plugin === 'trajviz-obs' && e.key === 'latest') latest.push(e.value)
    return next(e)
  })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const current = await obs($)
  await obs($, '--session /outside/saved.jsonl')
  expect(selected.at(-1)).toMatchObject({ path: '/outside/saved.jsonl' })
  const ui = await $.ui.mount(row(current, 'desktop', { columns: 120, rows: 40 }))
  await ui.press({ key: 'pane' })
  expect(runs).toEqual(['pane'])
  expect(selected.at(-1)).toBeNull()
  expect(latest).toContain(null)
  expect(latest.at(-1)).toMatchObject({ transcript: { path: DERIVED } })
  await ui.unmount()
})

test('narrow viewport has no raster', async ($, on) => {
  host(on, [SNAPSHOT])
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const text = await obs($)

  // Below 72: chips, a KPI list, the top three issues, no Raster and no swimlane.
  let ui = await $.ui.mount(row(text, 'terminal', { columns: 60, rows: 40, isFullscreen: true }))
  expect(await ui.find({ key: 'tokens' })).toBeUndefined()
  expect(await ui.find({ key: 'swimlane' })).toBeUndefined()
  expect(await ui.findAll({ type: 'Raster' })).toHaveLength(0)
  expect((await ui.find({ type: 'Text', text: /tool success/ }))?.props['color']).toBe('success')
  expect(await ui.find({ key: 'kpis-summary' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^95%$/ })).toBeDefined()
  expect(await ui.findAll({ type: 'Text', text: /^\d\. / })).toHaveLength(3)
  expect(await ui.find({ key: 'dashboard' })).toBeDefined()
  await ui.unmount()

  // The default terminal width keeps the summary short; detailed charts are in the pane and dashboard.
  ui = await $.ui.mount(row(text, 'terminal', { columns: 80, rows: 40, isFullscreen: true }))
  expect(await ui.findAll({ type: 'Raster' })).toHaveLength(0)
  expect(await ui.find({ key: 'swimlane' })).toBeUndefined()
  expect(await ui.find({ key: 'kpis-summary' })).toBeDefined()
  await ui.unmount()

  // From 100: the three series side by side within the columns, and the swimlane.
  ui = await $.ui.mount(row(text, 'terminal', { columns: 120, rows: 40, isFullscreen: true }))
  const widths = await Promise.all(['tokens', 'durations', 'context'].map(async key => (await ui.find({ key }))?.props['columns'] as number))
  expect(widths.reduce((a, b) => a + b, 0)).toBeLessThanOrEqual(120)
  for (const w of widths) expect(w).toBeGreaterThanOrEqual(30)
  expect((await ui.find({ key: 'swimlane' }))?.type).toBe('Raster')
  await ui.unmount()

  // No viewport measured yet: drawn as 80 columns.
  ui = await $.ui.mount({ ...row(text, 'terminal', { columns: 80, rows: 1 }), viewport: undefined })
  expect(await ui.find({ key: 'kpis-summary' })).toBeDefined()
  await ui.unmount()
})

test('pruned id falls through to the engine', async ($, on) => {
  host(on, [SNAPSHOT])
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  await obs($)
  const vp = { columns: 120, rows: 40, isFullscreen: true }

  // A card whose snapshot was pruned (or never stored): the engine's own row.
  let ui = await $.ui.mount(row('obs snapshot 99 · 16:40:12 · 311 assistant steps · window 1,000,000\nHealth: ok', 'terminal', vp))
  expect((await ui.find({ type: 'Text', text: /^engine$/ }))?.text).toBe('engine')
  expect(await ui.find({ type: 'Text', text: /^311$/ })).toBeUndefined()
  await ui.unmount()

  // An error row carries no id: the engine's own row.
  ui = await $.ui.mount(row('No transcript for this session yet. Send one prompt, then run /obs.', 'terminal', vp))
  expect((await ui.find({ type: 'Text', text: /^engine$/ }))?.text).toBe('engine')
  await ui.unmount()

  // Another command's row is never ours.
  ui = await $.ui.mount({ ...row('obs snapshot 1 · x', 'terminal', vp), props: { command: 'cost', args: '', text: 'obs snapshot 1 · x', isErrored: false } })
  expect((await ui.find({ type: 'Text', text: /^engine$/ }))?.text).toBe('engine')
  await ui.unmount()
})
