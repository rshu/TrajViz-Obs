import { expect, mock, test, type Engine, type MockClock } from 'claude-code/testing'
import type { On, UiPane } from 'claude-code'
import type { RefreshState } from '../types'
import { LARGE_SESSION_TEXT, shouldRefresh, statusLine, TOO_LARGE_BYTES } from './lib/watch'
import { SNAPSHOT } from './testdata/snapshot'

const HOME = '/Users/me'
const CWD = '/Users/me/p'
const CACHE = `${HOME}/.cache/trajviz-obs`
const VENV_PY = `${CACHE}/envs/0123456789abcdef01234567/0123456789abcdef0123456789abcdef/bin/python`
const STATE_PATH = `${CACHE}/state.json`
const STATE_JSON = JSON.stringify({ revision: 4, python: VENV_PY, trajviz: '0.2.1', extras: ['llm', 'charts'], source: 'agent-trajviz>=0.2.0,<0.3', createdAt: 1 })
const DERIVED = `${HOME}/.claude/projects/-Users-me-p/abc.jsonl`
const USAGE = { startedAt: 0, context: { window: 1_000_000, percent: 24 }, rateLimits: [] }
const NOW = 1_761_150_000_000
const LINE = 'obs · ctx 24% · 196 tools 96% · 3 issues'

/** A second snapshot, told from the fixture by its step count. */
const SECOND = { ...SNAPSHOT, counts: { ...SNAPSHOT.counts, assistant_steps: 350 } }

/** The refresh value before any run. */
const IDLE: RefreshState = { lastRunAt: 0, lastDurationMs: 0, lastSize: 0, lastMtimeMs: 0, busy: false, busySince: 0 }
/** One after a run at `NOW - 10_000` that took 200 ms and saw a 1,000-byte transcript. */
const RAN: RefreshState = { ...IDLE, lastRunAt: NOW - 10_000, lastDurationMs: 200, lastSize: 1_000, lastMtimeMs: NOW - 11_000 }
const OBS_PANE: UiPane = { id: 'obs', title: 'obs', isShown: true, isFocused: false, isPlaced: true }

test('shouldRefresh: busy, too large, the debounce (and what is left of it), an unchanged transcript, then a run', () => {
  const changed = { size: 2_000, mtimeMs: NOW - 1_000 }
  expect(shouldRefresh(NOW, { ...RAN, busy: true, busySince: NOW - 1_000 }, changed, 2_000)).toEqual({ run: false, reason: 'busy' })
  // A hold older than the longest run is stale: not busy.
  expect(shouldRefresh(NOW, { ...RAN, busy: true, busySince: NOW - 3_600_000 }, changed, 2_000)).toEqual({ run: true })
  expect(shouldRefresh(NOW, RAN, changed, TOO_LARGE_BYTES + 1)).toEqual({ run: false, reason: 'too-large' })
  expect(shouldRefresh(NOW, RAN, changed, TOO_LARGE_BYTES)).toEqual({ run: true })
  // max(3 s, 10 × the last run): 200 ms → 3 s; 2 s → 20 s. Inside it, `waitMs` is what is left.
  expect(shouldRefresh(RAN.lastRunAt + 2_999, RAN, changed, 2_000)).toEqual({ run: false, reason: 'debounce', waitMs: 1 })
  expect(shouldRefresh(RAN.lastRunAt + 1_000, RAN, changed, 2_000)).toEqual({ run: false, reason: 'debounce', waitMs: 2_000 })
  expect(shouldRefresh(RAN.lastRunAt + 3_000, RAN, changed, 2_000)).toEqual({ run: true })
  expect(shouldRefresh(RAN.lastRunAt + 8_000, { ...RAN, lastDurationMs: 2_000 }, changed, 2_000)).toEqual({ run: false, reason: 'debounce', waitMs: 12_000 })
  expect(shouldRefresh(RAN.lastRunAt + 19_999, { ...RAN, lastDurationMs: 2_000 }, changed, 2_000)).toEqual({ run: false, reason: 'debounce', waitMs: 1 })
  expect(shouldRefresh(RAN.lastRunAt + 20_000, { ...RAN, lastDurationMs: 2_000 }, changed, 2_000)).toEqual({ run: true })
  // The same size and mtime as the last run saw.
  expect(shouldRefresh(NOW, RAN, { size: RAN.lastSize, mtimeMs: RAN.lastMtimeMs }, 1_000)).toEqual({ run: false, reason: 'unchanged' })
  expect(shouldRefresh(NOW, RAN, { size: RAN.lastSize, mtimeMs: RAN.lastMtimeMs + 1 }, 1_000)).toEqual({ run: true })
  // Never run: no debounce, nothing to be unchanged from, even at a clock of 0.
  expect(shouldRefresh(0, IDLE, { size: 0, mtimeMs: 0 }, 0)).toEqual({ run: true })
})

test('statusLine: the engine context, the tool calls and their success, the issues', () => {
  expect(statusLine(SNAPSHOT, USAGE)).toBe(LINE)
  // Without the engine's figure, TrajViz's; one issue is singular; an unknown rate is `?`.
  const one = { ...SNAPSHOT, issues: SNAPSHOT.issues.slice(0, 1), tools: { ...SNAPSHOT.tools, calls: 49, success_rate: null } }
  expect(statusLine(one, null)).toBe('obs · ctx 71% · 49 tools ? · 1 issue')
  expect(statusLine({ ...SNAPSHOT, issues: [] }, { ...USAGE, context: { window: 1_000_000 } })).toBe('obs · ctx 71% · 196 tools 96% · 0 issues')
  expect(statusLine({ ...SNAPSHOT, counts: { ...SNAPSHOT.counts, issues: 37 } }, USAGE)).toContain('37 issues')
})

type Host = {
  clock: MockClock
  ran: string[][]
  statuses: (string | undefined)[]
  toasts: string[]
  panes: UiPane[]
  stat: { size: number; mtimeMs: number }
  snapshots: number
  /** How late `fs.stat` and the bridge's `snapshot` answer (0: at once). */
  delayMs: number
  /** Holds one usage read, without delaying later readers or other commands. */
  usageBlock: { wait: Promise<void>; started: () => void; usage: typeof USAGE } | null
}

/**
 * The host beneath the mod: the clock (starting at `NOW`), env, files (the
 * transcript's stat as the test sets it), session, usage, the pane list as
 * the test sets it, the status line and toasts recorded, the bridge's
 * `snapshot` answers in order (the last one repeats), and the engine's own
 * `turn.complete` beneath. With `delayMs` set, the stat and the snapshot
 * answer that much later on the clock, so a run is in flight meanwhile.
 */
function host(on: On, snapshots: unknown[]): Host {
  const clock = mock.clock(on, { now: NOW })
  const h: Host = { clock, ran: [], statuses: [], toasts: [], panes: [], stat: { size: 1_000, mtimeMs: NOW - 60_000 }, snapshots: 0, delayMs: 0, usageBlock: null }
  on('command.register', () => ({ value: { command: 'obs' } }))
  on('tool.register', () => ({ value: { tool: 'mcp__trajviz-obs__obs' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: 'abc' }))
  on('session.cwd', () => ({ value: CWD }))
  on('session.usage', async () => {
    const block = h.usageBlock
    h.usageBlock = null
    if (block) { block.started(); await block.wait }
    return { value: block?.usage ?? USAGE }
  })
  mock.env(on, { HOME })
  on('fs.read', (_, e) => (e.path === STATE_PATH ? { value: STATE_JSON } : { deny: `ENOENT: ${e.path}` }))
  on('fs.exists', (_, e) => ({ value: e.path === VENV_PY || e.path === DERIVED }))
  on('fs.stat', async (_, e) => {
    if (e.path !== DERIVED) return { deny: `ENOENT: ${e.path}` }
    if (h.delayMs > 0) await clock.sleep(h.delayMs)
    return { value: { kind: 'file', ...h.stat, isLink: false } }
  })
  on('ui.panes', () => ({ value: h.panes }))
  on('ui.toast', (_, e) => {
    h.toasts.push(e.text)
    return { value: undefined }
  })
  on('ui.status', (_, e) => {
    h.statuses.push(e.text)
    return { value: undefined }
  })
  on('ui.log', () => ({ value: undefined }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.close', () => ({ value: undefined }))
  on('turn.complete', (_, e) => ({ text: e.answer }))
  on('process.run', async (_, e) => {
    h.ran.push([...e.argv])
    if (e.argv[3] === 'report') return { value: { exitCode: 0, stdout: JSON.stringify({ path: `${CACHE}/sessions/abc/report.html`, bytes: 900 }), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    if (e.argv[3] !== 'snapshot') return { deny: `cannot start ${e.argv[0]}` }
    if (h.delayMs > 0) await clock.sleep(h.delayMs)
    const doc = snapshots[Math.min(h.snapshots++, snapshots.length - 1)]
    return { value: { exitCode: 0, stdout: `${JSON.stringify(doc)}\n`, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  return h
}

const obs = ($: Engine, args: string): Promise<string> =>
  $.command.run({ command: 'obs', args, origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 80 } } as never).then(r => r.text ?? '')

/** Delays just the next usage read and exposes when it is actually waiting. */
function blockUsage(h: Host, usage = USAGE): { started: Promise<void>; release: () => void } {
  let started: () => void = () => {}
  let release: () => void = () => {}
  const entered = new Promise<void>(resolve => { started = resolve })
  const wait = new Promise<void>(resolve => { release = resolve })
  h.usageBlock = { wait, started, usage }
  return { started: entered, release }
}

/** A turn's end as the engine raises it, on the main loop unless `agentId` is given, then the detached work settled (as far as the clock lets it). */
async function turnEnds($: Engine, h: Host, agentId?: string): Promise<void> {
  const e = { answer: 'done', durationMs: 1_200, isAborted: false, turnId: `t${Math.random()}`, reason: 'answer' as const }
  await $.turn.complete(agentId === undefined ? e : { ...e, agentId })
  await h.clock.settle()
}

/** The pane as the terminal mounts it. */
const PANE = {
  plugin: 'trajviz-obs',
  surface: 'terminal',
  component: 'Pane',
  requestId: 'obs',
  props: { title: 'obs', isFocused: true, bodyColumns: 80, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} },
} as const

/** The assistant-step count the pane draws. */
async function paneSteps($: Engine, steps: number): Promise<boolean> {
  const ui = await $.ui.mount(PANE)
  const found = (await ui.find({ type: 'Text', text: new RegExp(`^${steps}$`) })) !== undefined
  await ui.unmount()
  return found
}

test('a main-loop turn refreshes the open pane, debounced and only when the transcript changed; an agent turn never', async ($, on) => {
  const h = host(on, [SNAPSHOT, SECOND])
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  h.panes = [OBS_PANE]

  // An agent's turn: nothing, whatever the flags.
  await turnEnds($, h, 'agent-1')
  expect(h.ran).toEqual([])

  // The main loop's: one snapshot, stored as the latest.
  await turnEnds($, h)
  expect(h.ran.map(r => r[3])).toEqual(['snapshot'])
  expect(await paneSteps($, 311)).toBe(true)

  // Within the debounce: nothing now, even with a changed transcript (the trailing refresh is scheduled for the window's end).
  h.stat = { size: 2_000, mtimeMs: NOW + 1_000 }
  await h.clock.advance(2_999)
  await turnEnds($, h)
  expect(h.ran).toHaveLength(1)
  // At the window's end the trailing refresh finds the transcript as the run saw it: nothing.
  h.stat = { size: 1_000, mtimeMs: NOW - 60_000 }
  await h.clock.advance(1)
  expect(h.ran).toHaveLength(1)
  // Past it with the transcript unchanged: nothing; changed: the second snapshot, drawn by the pane.
  await turnEnds($, h)
  expect(h.ran).toHaveLength(1)
  h.stat = { size: 2_000, mtimeMs: NOW + 1_000 }
  await turnEnds($, h)
  expect(h.ran).toHaveLength(2)
  expect(await paneSteps($, 350)).toBe(true)
  // The status line is off: never drawn.
  expect(h.statuses).toEqual([])
})

test('a turn inside the debounce schedules one trailing refresh, which runs when the window ends and the transcript changed', async ($, on) => {
  const h = host(on, [SNAPSHOT, SECOND])
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  h.panes = [OBS_PANE]
  await turnEnds($, h)
  expect(h.ran).toHaveLength(1)

  // A short turn 1 s after the run (its window is 3 s): refused now, scheduled for 2 s later.
  h.stat = { size: 2_000, mtimeMs: NOW + 1_000 }
  await h.clock.advance(1_000)
  await turnEnds($, h)
  // Another inside the window adds no second timer.
  await h.clock.advance(500)
  await turnEnds($, h)
  expect(h.ran).toHaveLength(1)
  await h.clock.advance(1_499)
  expect(h.ran).toHaveLength(1)
  // The window ends: the trailing refresh runs once, with no turn, and the pane shows it.
  await h.clock.advance(1)
  expect(h.ran).toHaveLength(2)
  expect(await paneSteps($, 350)).toBe(true)
  // Nothing more fires later on its own.
  await h.clock.advance(60_000)
  expect(h.ran).toHaveLength(2)
})

test('the trailing refresh is decided afresh when it fires: watch off by then runs nothing; statusline on draws the line', { options: { statusline: true } }, async ($, on) => {
  const h = host(on, [SNAPSHOT, SECOND])
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  await turnEnds($, h)
  expect(h.ran).toHaveLength(1)
  expect(h.statuses).toEqual([LINE])

  // Inside the window; then the line is turned off before the window ends: the trailing refresh finds nothing to do.
  h.stat = { size: 2_000, mtimeMs: NOW + 1_000 }
  await h.clock.advance(1_000)
  await turnEnds($, h)
  expect(await obs($, 'statusline off')).toBe('obs: statusline off')
  await h.clock.advance(2_000)
  expect(h.ran).toHaveLength(1)
  expect(h.statuses).toEqual([LINE, undefined])

  // On again, a turn inside the next window, and the trailing refresh runs and draws the line.
  expect(await obs($, 'statusline on')).toBe('obs: statusline on')
  await h.clock.advance(3_000)
  await turnEnds($, h)
  expect(h.ran).toHaveLength(2)
  h.stat = { size: 3_000, mtimeMs: NOW + 2_000 }
  await h.clock.advance(1_000)
  await turnEnds($, h)
  expect(h.ran).toHaveLength(2)
  await h.clock.advance(2_000)
  expect(h.ran).toHaveLength(3)
  expect(h.statuses).toEqual([LINE, undefined, LINE, LINE, LINE])
})

test('with the pane closed, watch alone runs nothing; watch off leaves an open pane alone', async ($, on) => {
  const h = host(on, [SNAPSHOT])
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  await turnEnds($, h)
  expect(h.ran).toEqual([])
  h.panes = [OBS_PANE]
  expect(await obs($, 'watch off')).toBe('obs: watch off')
  await turnEnds($, h)
  expect(h.ran).toEqual([])
  expect(await obs($, 'watch on')).toBe('obs: watch on')
  await turnEnds($, h)
  expect(h.ran.map(r => r[3])).toEqual(['snapshot'])
  // No argument flips it; anything else is the usage line.
  expect(await obs($, 'watch')).toBe('obs: watch off')
  expect(await obs($, 'watch maybe')).toBe('Usage: /obs watch [on|off]')
})

test('statusline on runs the snapshot after a turn and pins the summary line', { options: { statusline: true } }, async ($, on) => {
  const h = host(on, [SNAPSHOT])
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  await turnEnds($, h)
  expect(h.ran.map(r => r[3])).toEqual(['snapshot'])
  expect(h.statuses).toEqual([LINE])
  // Off clears the line; on draws it again from the latest snapshot without a run.
  expect(await obs($, 'statusline off')).toBe('obs: statusline off')
  expect(h.statuses).toEqual([LINE, undefined])
  expect(await obs($, 'statusline on')).toBe('obs: statusline on')
  expect(h.statuses).toHaveLength(3)
  expect(h.statuses[2]).toBe(LINE)
  expect(h.ran).toHaveLength(1)
})

test('a blocked status-line usage read does not hold up report session selection', { options: { statusline: true, openBrowser: false } }, async ($, on) => {
  const h = host(on, [SNAPSHOT])
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  await obs($, '')
  const gate = blockUsage(h)
  const drawing = obs($, 'statusline on')
  await gate.started
  let completed = false
  const reporting = obs($, 'report').then(text => { completed = true; return text })
  try {
    await h.clock.settle()
    expect(completed).toBe(true)
    expect(await reporting).toContain('Report written to')
    expect(h.ran.map(argv => argv[3])).toEqual(['snapshot', 'report'])
  } finally {
    gate.release()
    await Promise.all([drawing, reporting])
  }
})

test('a newer status-line draw wins when an older usage read finishes later', { options: { statusline: true } }, async ($, on) => {
  const h = host(on, [SNAPSHOT])
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  await obs($, '')
  const gate = blockUsage(h, { ...USAGE, context: { ...USAGE.context, percent: 99 } })
  const older = obs($, 'statusline on')
  await gate.started
  let completed = false
  const newer = obs($, 'statusline on').then(text => { completed = true; return text })
  try {
    await h.clock.settle()
    expect(completed).toBe(true)
    expect(h.statuses).toEqual([LINE])
  } finally {
    gate.release()
    await Promise.all([older, newer])
  }
  expect(h.statuses).toEqual([LINE])
})

test('turning the status line off while its usage read is blocked keeps it cleared', { options: { statusline: true } }, async ($, on) => {
  const h = host(on, [SNAPSHOT])
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  await obs($, '')
  const gate = blockUsage(h)
  const drawing = obs($, 'statusline on')
  await gate.started
  try {
    expect(await obs($, 'statusline off')).toBe('obs: statusline off')
    expect(h.statuses).toEqual([undefined])
  } finally {
    gate.release()
    await drawing
  }
  expect(h.statuses).toEqual([undefined])
})

test('/clear completes while a status-line usage read is blocked and prevents its stale draw', { options: { statusline: true } }, async ($, on) => {
  const h = host(on, [SNAPSHOT])
  on('classic.SessionStart', () => ({}))
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  await $.classic.SessionStart({ source: 'startup', transcript_path: DERIVED, session_id: 'abc' })
  await obs($, '')
  const gate = blockUsage(h)
  const drawing = obs($, 'statusline on')
  await gate.started
  let completed = false
  const clearing = $.classic.SessionStart({ source: 'clear', transcript_path: '/new/def.jsonl', session_id: 'def' }).then(() => { completed = true })
  try {
    await h.clock.settle()
    expect(completed).toBe(true)
  } finally {
    gate.release()
    await Promise.all([drawing, clearing])
  }
  expect(h.statuses.every(line => line === undefined)).toBe(true)
})

test('statusline off while a run is in flight: the finished run leaves the line cleared', { options: { statusline: true } }, async ($, on) => {
  const h = host(on, [SNAPSHOT])
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  // The stat answers 5 s late and the snapshot 5 s after that: the run is under way for 10 s.
  h.delayMs = 5_000
  await turnEnds($, h)
  expect(h.ran).toEqual([])
  await h.clock.advance(5_000)
  expect(h.ran.map(r => r[3])).toEqual(['snapshot'])
  expect(await obs($, 'statusline off')).toBe('obs: statusline off')
  expect(h.statuses).toEqual([undefined])
  await h.clock.advance(5_000)
  // The run finished and stored its snapshot (`statusline on` draws it without a run), but drew no line.
  expect(h.statuses).toEqual([undefined])
  expect(await obs($, 'statusline on')).toBe('obs: statusline on')
  expect(h.statuses).toEqual([undefined, LINE])
  expect(h.ran).toHaveLength(1)
})

test('above 32 MB the watch degrades to manual and says so once', { options: { statusline: true } }, async ($, on) => {
  const h = host(on, [SNAPSHOT])
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  h.stat = { size: TOO_LARGE_BYTES + 1, mtimeMs: NOW }
  await turnEnds($, h)
  await h.clock.advance(5_000)
  await turnEnds($, h)
  expect(h.ran).toEqual([])
  expect(h.toasts).toEqual([LARGE_SESSION_TEXT])
  expect(h.statuses).toEqual([LARGE_SESSION_TEXT, LARGE_SESSION_TEXT])
  // `/obs` still computes.
  expect((await obs($, '')).split('\n')[0]).toMatch(/^obs snapshot 1 · /)
  expect(h.ran.map(r => r[3])).toEqual(['snapshot'])
})

test('statusline off while the stat of a too-large session is in flight: the large-session line is not pinned', { options: { statusline: true } }, async ($, on) => {
  const h = host(on, [SNAPSHOT])
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  h.stat = { size: TOO_LARGE_BYTES + 1, mtimeMs: NOW }
  h.delayMs = 5_000
  await turnEnds($, h)
  expect(await obs($, 'statusline off')).toBe('obs: statusline off')
  await h.clock.advance(5_000)
  expect(h.statuses).toEqual([undefined])
  // The toast is still said, once.
  expect(h.toasts).toEqual([LARGE_SESSION_TEXT])
  expect(h.ran).toEqual([])
})

test('an empty refresh replaces the previous pane data and clears its status line', { options: { statusline: true } }, async ($, on) => {
  const empty = { schema_version: 1, empty: true, reason: 'no_steps', transcript: { path: DERIVED } }
  const h = host(on, [SNAPSHOT, empty])
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  await turnEnds($, h)
  expect(await paneSteps($, 311)).toBe(true)
  h.stat = { size: 0, mtimeMs: NOW + 10_000 }
  await h.clock.advance(10_000)
  await turnEnds($, h)
  const ui = await $.ui.mount(PANE)
  expect(await ui.find({ type: 'Text', text: /Nothing to analyze yet/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^311$/ })).toBeUndefined()
  expect(h.statuses.at(-1)).toBeUndefined()
  await ui.unmount()
})

test('/clear during a watch child cannot republish the old session or its debounce stamp', { options: { statusline: true } }, async ($, on) => {
  const h = host(on, [SNAPSHOT])
  const latest: unknown[] = []
  const refresh: unknown[] = []
  on('state.set', (_, e, next) => {
    if (e.key === 'latest') latest.push(e.value)
    if (e.key === 'refresh') refresh.push(e.value)
    return next(e)
  })
  on('classic.SessionStart', () => ({}))
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  await $.classic.SessionStart({ source: 'startup', transcript_path: DERIVED, session_id: 'abc' })
  h.delayMs = 5_000
  await turnEnds($, h)
  await h.clock.advance(5_000)
  expect(h.ran.map(argv => argv[3])).toEqual(['snapshot'])
  await $.classic.SessionStart({ source: 'clear', transcript_path: '/new/def.jsonl', session_id: 'def' })
  await h.clock.advance(5_000)
  expect(latest.at(-1)).toBeNull()
  expect(refresh.at(-1)).toMatchObject({ lastRunAt: 0, lastSize: 0, busy: false })
  expect(h.statuses.every(line => line === undefined)).toBe(true)
  const ui = await $.ui.mount(PANE)
  expect(await ui.find({ type: 'Text', text: /No snapshot yet/ })).toBeDefined()
  await ui.unmount()
})
