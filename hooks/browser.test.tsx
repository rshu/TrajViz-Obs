import { expect, mock, test } from 'claude-code/testing'
import type { On, ProcessSpawnChunk } from 'claude-code'
import type { Dashboards } from '../types'
import { forgetPlatform } from './lib/open'
import type { SessionState } from '../types'

const HOME = '/Users/me'
const CWD = '/Users/me/p'
const CACHE = `${HOME}/.cache/trajviz-obs`
const VENV_PY = `${CACHE}/envs/0123456789abcdef01234567/0123456789abcdef0123456789abcdef/bin/python`
const STATE_PATH = `${CACHE}/state.json`
const PROJECT_DIR = `${HOME}/.claude/projects/-Users-me-p`
const DERIVED = `${PROJECT_DIR}/abc.jsonl`
const USAGE = { startedAt: 0, context: { window: 1_000_000, percent: 12 }, rateLimits: [] }
const NOW = 1_761_150_000_000
const URL = 'http://127.0.0.1:7860'
const URL_CHUNK: ProcessSpawnChunk = { stream: 'stdout', text: `{"url": "${URL}"}\n` }
const UNLOADED_CHUNK: ProcessSpawnChunk = { stream: 'stdout', text: `{"url": "${URL}", "preloaded": false}\n` }
/** Gradio's own launch print, which the bridge sends to stderr, and a Python warning. */
const NOISE_CHUNKS: ProcessSpawnChunk[] = [
  { stream: 'stderr', text: 'DeprecationWarning: something in pydantic\n' },
  { stream: 'stderr', text: `* Running on local URL:  ${URL}\n` },
]
const BUSY_TEXT = 'Port 7860 on 127.0.0.1 is in use or cannot be bound; pass another --port, or none to take a free one.'
const BUSY_CHUNKS: ProcessSpawnChunk[] = [
  { stream: 'stderr', text: 'OSError: [Errno 48] Address already in use\n' },
  { stream: 'stdout', text: `{"error": {"code": "port_busy", "message": "${BUSY_TEXT}", "port": 7860}}\n` },
]
const ASK_APP = "Install TrajViz's app extra (the dashboards, downloads additional packages)?"
/** The transcript row announcing the install, before pip starts. */
const INSTALLING_APP = "obs: installing TrajViz's app extra (the dashboards, downloads additional packages)"
const DECLINED = "TrajViz's app extra is not installed (the dashboards need it); /obs setup --extras app installs it."

const stateJson = (extras: string[]): string =>
  JSON.stringify({ revision: 4, python: VENV_PY, trajviz: '0.2.1', extras, source: 'agent-trajviz>=0.2.0,<0.3', createdAt: 1 })

type RunAnswer = { exitCode: number; stdout: string; stderr?: string }

type World = {
  /** The extras `state.json` lists. */
  extras?: string[]
  env?: Record<string, string>
  /** What a bridge child streams, by subcommand; absent: the URL line. A gate promise keeps the child alive until it resolves. */
  child?: Record<string, { chunks: ProcessSpawnChunk[]; code: number; gate?: Promise<void> }>
  /** Non-bridge commands by name; anything else cannot start. */
  commands?: Record<string, RunAnswer>
  /** What the person answers an install ask: an option's label, or `dismiss` (the dialog closed, the call rejects). Default: Install. */
  answer?: string
  bridge?: Record<string, RunAnswer>
  files?: Record<string, string>
  stats?: Record<string, { size: number; mtimeMs: number }>
  beforeExists?: (path: string) => Promise<void> | void
  beforeStat?: (path: string) => Promise<void> | void
}

type Host = {
  ran: string[][]
  spawned: { argv: string[]; cwd?: string }[]
  dashboards: () => Dashboards | undefined
  asked: string[]
  /** The transcript rows `$.ui.log` appended. */
  rows: string[]
  /** What happened, in order: `ask`, `row`, `pip`, `child`, `open`. */
  timeline: string[]
  selection: () => SessionState['selected']
}

/** The host beneath `/obs`: env, files, session, canned process answers, the `dashboards` state held here; every run and spawn recorded. */
function host(on: On, world: World = {}): Host {
  const ran: string[][] = []
  const spawned: Host['spawned'] = []
  const rows: string[] = []
  const timeline: string[] = []
  const files: Record<string, string> = { [STATE_PATH]: stateJson(world.extras ?? ['llm', 'charts', 'app']), ...world.files }
  on('command.register', () => ({ value: { command: 'obs' } }))
  on('tool.register', () => ({ value: { tool: 'mcp__trajviz-obs__obs' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.end', (_, e) => ({ sessionId: e.sessionId }))
  on('session.id', () => ({ value: 'abc' }))
  on('session.cwd', () => ({ value: CWD }))
  on('session.usage', () => ({ value: USAGE }))
  mock.env(on, { HOME, ...(world.env ?? {}) })
  mock.clock(on, { now: NOW })
  on('fs.read', (_, e) => e.path in files ? { value: files[e.path] as string } : { deny: `ENOENT: ${e.path}` })
  on('fs.write', (_, e) => {
    files[e.path] = e.text
    return { value: undefined }
  })
  on('fs.exists', async (_, e) => {
    await world.beforeExists?.(e.path)
    return { value: e.path in files || e.path === VENV_PY || e.path === DERIVED }
  })
  on('fs.stat', async (_, e) => {
    await world.beforeStat?.(e.path)
    const stat = world.stats?.[e.path]
    return stat ? { value: { kind: 'file' as const, ...stat, isLink: false } } : { deny: `ENOENT: ${e.path}` }
  })
  on('ui.toast', () => ({ value: undefined }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.log', (_, e) => {
    if (e.to === 'transcript') {
      rows.push(e.text)
      timeline.push('row')
    }
    return { value: undefined }
  })
  // `$.ui.ask` is the engine's AskUserQuestion tool call; the question text is recorded, the answer is the world's.
  const asked: string[] = []
  on('tool.call', { tool: 'AskUserQuestion' }, (_, e) => {
    timeline.push('ask')
    const questions = (e as { questions?: { question: string }[] }).questions ?? []
    for (const q of questions) asked.push(q.question)
    const answer = world.answer ?? 'Install'
    if (answer === 'dismiss') return { deny: 'the dialog was dismissed' }
    return { result: { questions, answers: Object.fromEntries(questions.map(q => [q.question, answer])) } }
  })
  // The `dashboards` state, kept here so the test reads it without mounting the pane
  // (a mount waits on the detached child loop).
  const held: { value: Dashboards | undefined; version: number } = { value: undefined, version: 0 }
  const session: { value: SessionState; version: number } = { value: { transcriptPath: null, sessionId: null, selected: null }, version: 0 }
  on('state.get', { plugin: 'trajviz-obs', key: 'session' }, () => ({ value: { value: session.value, version: session.version } }))
  on('state.set', { plugin: 'trajviz-obs', key: 'session' }, (_, e) => {
    session.value = e.value
    session.version++
    return { value: { isSet: true, version: session.version } }
  })
  on('state.get', { plugin: 'trajviz-obs', key: 'dashboards' }, () => ({ value: { value: held.value, version: held.version } }))
  on('state.set', { plugin: 'trajviz-obs', key: 'dashboards' }, (_, e) => {
    if (e.ifVersion !== undefined && e.ifVersion !== held.version) return { value: { isSet: false, version: held.version } }
    held.value = e.value
    held.version += 1
    return { value: { isSet: true, version: held.version } }
  })
  on('process.run', (_, e) => {
    ran.push([...e.argv])
    if (e.argv[0] === 'uv' && e.argv[1] === '--version') return { value: { exitCode: 0, stdout: 'uv 0.9.0\n', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    if (e.argv[0] === 'open') timeline.push('open')
    const a = e.argv[1] === '-I' ? world.bridge?.[e.argv[3] ?? ''] : (world.commands ?? {})[e.argv[0] ?? '']
    if (!a) return { deny: `cannot start ${e.argv[0]}` }
    return { value: { exitCode: a.exitCode, stdout: a.stdout, stderr: a.stderr ?? '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('process.spawn', async function* (_, e) {
    spawned.push({ argv: [...e.argv], cwd: e.cwd })
    if (e.argv.some(a => a.endsWith('/bridge/bootstrap.py'))) {
      const req = JSON.parse(e.argv[e.argv.indexOf('--request') + 1] as string)
      const state = { revision: 4, python: VENV_PY, pythonOption: req.pythonOption, trajviz: '0.2.1', extras: [...new Set(['llm', 'charts', ...req.extras])], source: req.source, ...(req.local ? { sourceCwd: CWD } : {}), createdAt: 1 }
      files[STATE_PATH] = JSON.stringify(state)
      timeline.push('pip')
      yield { stream: 'stderr' as const, text: 'Installed\n' }
      yield { stream: 'stdout' as const, text: JSON.stringify(state) }
      return { value: { code: 0, signal: null } }
    }

    if (e.argv[1] !== '-I') {
      timeline.push('pip')
      yield { stream: 'stdout' as const, text: 'Installed\n' }
      return { value: { code: 0, signal: null } }
    }
    timeline.push('child')
    const c = (world.child ?? {})[e.argv[3] ?? ''] ?? { chunks: [URL_CHUNK], code: 0 }
    for (const chunk of c.chunks) yield chunk
    if (c.gate) await c.gate
    return { value: { code: c.code, signal: null } }
  })
  return { ran, spawned, dashboards: () => held.value, asked, rows, timeline, selection: () => session.value.selected }
}

const DARWIN = { uname: { exitCode: 0, stdout: 'Darwin\n' }, open: { exitCode: 0, stdout: '' } }

const obs = ($: { command: { run: (args: never) => Promise<{ text?: string; context?: readonly string[] }> } }, args: string) =>
  $.command.run({ command: 'obs', args, origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 80 } } as never)

/** The argv of every bridge child, after the launcher. */
const children = (spawned: Host['spawned']): string[][] => spawned.filter(s => s.argv[1] === '-I').map(s => s.argv.slice(3))

const END = { reason: 'prompt_input_exit' as const, sessionId: 'abc', resume: { id: 'abc' } }

test('dashboard asks, announces and installs the app extra, starts the bridge child with the transcript and port, and opens the URL', async ($, on) => {
  forgetPlatform()
  const { ran, spawned, asked, rows, timeline } = host(on, { extras: ['llm', 'charts'], commands: DARWIN })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const out = await obs($, 'dashboard --port 7777')
  // An interactive session is asked once; the install is then announced in a row of its own before pip starts.
  expect(asked).toEqual([ASK_APP])
  expect(rows).toEqual([INSTALLING_APP])
  expect(timeline).toEqual(['ask', 'row', 'pip', 'child', 'open'])
  expect(setupRequests(spawned)[0]).toMatchObject({ source: 'agent-trajviz>=0.2.0,<0.3', extras: ['llm', 'charts', 'llm', 'charts', 'app'] })
  expect(children(spawned)).toEqual([['dashboard', '--transcript', DERIVED, '--window', '1000000', '--port', '7777']])
  expect(spawned[1]?.argv.slice(0, 2)).toEqual([VENV_PY, '-I'])
  expect(spawned[1]?.argv[2]).toMatch(/\/bridge\/trajviz_obs_cli\.py$/)
  expect(spawned[1]?.cwd).toBe(CWD)
  expect(ran.filter(r => r[0] === 'open')).toEqual([['open', URL]])
  expect(out.text).toBe(`Insight dashboard at ${URL} with session abc loaded; opened in the browser. /obs dashboard stop ends it.`)
  await $.session.end(END)
})

test('a running dashboard is named again, kept in state, stopped by stop and by the session end', async ($, on) => {
  forgetPlatform()
  let release = () => {}
  const world: World = { commands: DARWIN, child: { dashboard: { chunks: [URL_CHUNK], code: 0, gate: new Promise<void>(r => (release = r)) } } }
  const { ran, spawned, dashboards } = host(on, world)
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  expect((await obs($, 'dashboard')).text).toContain('opened in the browser')
  expect(spawned).toHaveLength(1)
  expect(dashboards()).toEqual({ insight: { url: URL, port: 7860 }, converge: null })

  // The child lives on after the command: a second `dashboard` names it, opens it again, starts nothing.
  const again = await obs($, 'dashboard')
  expect(again.text).toBe(`Insight dashboard already running at ${URL}; opened in the browser. /obs dashboard stop ends it.`)
  expect(spawned).toHaveLength(1)
  expect(ran.filter(r => r[0] === 'open')).toHaveLength(2)

  // Stop while the child is still silent: the state is cleared at once; a second stop is nothing.
  expect((await obs($, 'dashboard stop')).text).toBe('Insight dashboard stopped.')
  expect(dashboards()).toEqual({ insight: null, converge: null })
  expect((await obs($, 'dashboard stop')).text).toBe('No Insight dashboard is running.')
  release()

  // Started again, the session's end ends it.
  let release2 = () => {}
  world.child = { dashboard: { chunks: [URL_CHUNK], code: 0, gate: new Promise<void>(r => (release2 = r)) } }
  expect((await obs($, 'dashboard')).text).toBe(`Insight dashboard at ${URL} with session abc loaded; opened in the browser. /obs dashboard stop ends it.`)
  expect(spawned).toHaveLength(2)
  expect(dashboards()).toEqual({ insight: { url: URL, port: 7860 }, converge: null })
  await $.session.end(END)
  expect(dashboards()).toEqual({ insight: null, converge: null })
  release2()
})

test('dashboard --session persists its resolved selection and sends only dashboard flags', async ($, on) => {
  const { spawned, selection } = host(on, { bridge: { resolve: { exitCode: 0, stdout: JSON.stringify({ path: '/outside/my run.jsonl', session_id: 'other', kind: 'file' }) } } })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  await obs($, 'dashboard --session "/outside/my run.jsonl" --port 7899')
  expect(children(spawned)).toEqual([['dashboard', '--transcript', '/outside/my run.jsonl', '--port', '7899']])
  expect(selection()).toEqual({ path: '/outside/my run.jsonl', id: 'other', label: 'other' })
  await $.session.end(END)
})

test('dashboard passes available labels and explains a label preload fallback', async ($, on) => {
  const labels = `${CACHE}/sessions/abc/labels.json`
  const { spawned } = host(on, {
    files: { [labels]: '{"labels":[]}' },
    child: { dashboard: { chunks: [{ stream: 'stdout', text: `{"url":"${URL}","preloaded":true,"labels_preloaded":false}\n` }], code: 0 } },
  })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const out = await obs($, 'dashboard')
  expect(children(spawned)).toEqual([['dashboard', '--transcript', DERIVED, '--window', '1000000', '--labels', labels]])
  expect(out.text).toContain(`labels were not preloaded: upload ${labels} in the Labels tab`)
  await $.session.end(END)
})

test('dashboard and Converge restart for a changed session or data root', async ($, on) => {
  const releases: (() => void)[] = []
  const alive = () => ({ chunks: [URL_CHUNK], code: 0, gate: new Promise<void>(resolve => { releases.push(resolve) }) })
  const world: World = {
    child: { dashboard: alive(), converge: alive() },
    bridge: { resolve: { exitCode: 0, stdout: JSON.stringify({ path: '/runs/other.jsonl', session_id: 'other', kind: 'file' }) } },
  }
  const { spawned } = host(on, world)
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  await obs($, 'dashboard')
  world.child!.dashboard = alive()
  expect((await obs($, 'dashboard --session /runs/other.jsonl')).text).toContain('with session other loaded')
  expect(children(spawned).slice(0, 2)).toEqual([
    ['dashboard', '--transcript', DERIVED, '--window', '1000000'],
    ['dashboard', '--transcript', '/runs/other.jsonl'],
  ])
  await obs($, 'converge --data-root /runs/first')
  world.child!.converge = alive()
  expect((await obs($, 'converge --data-root /runs/second')).text).toContain('over /runs/second')
  expect(children(spawned).slice(2)).toEqual([
    ['converge', '--data-root', '/runs/first'], ['converge', '--data-root', '/runs/second'],
  ])
  await $.session.end(END)
  for (const release of releases) release()
})

test('regenerating the same labels sidecar refreshes the dashboard without changing its launch arguments', async ($, on) => {
  const labels = `${CACHE}/sessions/abc/labels.json`
  const releases: (() => void)[] = []
  const alive = () => ({ chunks: [URL_CHUNK], code: 0, gate: new Promise<void>(resolve => { releases.push(resolve) }) })
  const world: World = {
    files: { [labels]: '{"labels":[]}' }, stats: { [labels]: { size: 100, mtimeMs: NOW } }, child: { dashboard: alive() },
  }
  const { spawned } = host(on, world)
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  await obs($, 'dashboard')
  expect((await obs($, 'dashboard')).text).toContain('already running')
  expect(children(spawned)).toHaveLength(1)
  world.stats![labels]!.mtimeMs++
  world.child!.dashboard = alive()
  expect((await obs($, 'dashboard')).text).not.toContain('already running')
  expect(children(spawned)).toHaveLength(2)
  expect(children(spawned)[1]).toEqual(children(spawned)[0])
  world.stats![labels]!.size++
  world.child!.dashboard = alive()
  await obs($, 'dashboard')
  expect(children(spawned)).toHaveLength(3)
  await $.session.end(END)
  for (const release of releases) release()
})

test('a dashboard delayed in labels metadata cannot replace a newer selected session', async ($, on) => {
  const labels = `${CACHE}/sessions/abc/labels.json`
  let releaseMetadata = () => {}
  const metadataGate = new Promise<void>(resolve => { releaseMetadata = resolve })
  let metadataStarted = () => {}
  const metadataReady = new Promise<void>(resolve => { metadataStarted = resolve })
  let releaseChild = () => {}
  const { spawned, ran } = host(on, {
    files: { [labels]: '{"labels":[]}' }, commands: DARWIN,
    stats: { [labels]: { size: 100, mtimeMs: NOW } },
    beforeStat: async path => {
      if (path !== labels) return
      metadataStarted()
      await metadataGate
    },
    child: { dashboard: { chunks: [URL_CHUNK], code: 0, gate: new Promise<void>(resolve => { releaseChild = resolve }) } },
    bridge: { resolve: { exitCode: 0, stdout: JSON.stringify({ path: '/runs/newer.jsonl', session_id: 'newer', kind: 'file' }) } },
  })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const older = obs($, 'dashboard')
  await metadataReady
  expect((await obs($, 'dashboard --session /runs/newer.jsonl')).text).toContain('with session newer loaded')
  releaseMetadata()
  expect((await older).text).toBe('The selected session changed during analysis; run /obs again.')
  expect(children(spawned)).toEqual([['dashboard', '--transcript', '/runs/newer.jsonl']])
  expect(ran.filter(argv => argv[0] === 'open')).toHaveLength(1)
  await $.session.end(END)
  releaseChild()
})

test('a dashboard delayed while validating its launcher cannot replace a newer selected session', async ($, on) => {
  const labels = `${CACHE}/sessions/abc/labels.json`
  let armLauncher = false
  let releaseLauncher = () => {}
  const launcherGate = new Promise<void>(resolve => { releaseLauncher = resolve })
  let launcherStarted = () => {}
  const launcherReady = new Promise<void>(resolve => { launcherStarted = resolve })
  let releaseChild = () => {}
  const { spawned, ran } = host(on, {
    files: { [labels]: '{"labels":[]}' }, commands: DARWIN,
    stats: { [labels]: { size: 100, mtimeMs: NOW } },
    beforeStat: path => { if (path === labels) armLauncher = true },
    beforeExists: async path => {
      if (path === VENV_PY && armLauncher) {
        armLauncher = false
        launcherStarted()
        await launcherGate
      }
    },
    child: { dashboard: { chunks: [URL_CHUNK], code: 0, gate: new Promise<void>(resolve => { releaseChild = resolve }) } },
    bridge: { resolve: { exitCode: 0, stdout: JSON.stringify({ path: '/runs/newer.jsonl', session_id: 'newer', kind: 'file' }) } },
  })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const older = obs($, 'dashboard')
  await launcherReady
  expect((await obs($, 'dashboard --session /runs/newer.jsonl')).text).toContain('with session newer loaded')
  releaseLauncher()
  expect((await older).text).toBe('The selected session changed during analysis; run /obs again.')
  expect(children(spawned)).toEqual([['dashboard', '--transcript', '/runs/newer.jsonl']])
  expect(ran.filter(argv => argv[0] === 'open')).toHaveLength(1)
  await $.session.end(END)
  releaseChild()
})

test('a busy port is the row, nothing opens and nothing stays registered', async ($, on) => {
  const { ran, dashboards } = host(on, { commands: DARWIN, child: { dashboard: { chunks: BUSY_CHUNKS, code: 1 } } })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const out = await obs($, 'dashboard --port 7860')
  // The bridge's port_busy envelope is the row; Gradio's stderr line before it is not.
  expect(out.text).toBe(`obs dashboard: ${BUSY_TEXT}`)
  expect(ran.filter(r => r[0] === 'open')).toEqual([])
  // The load's reset is the only write: nothing was recorded for the child.
  expect(dashboards()).toEqual({ insight: null, converge: null })
  expect((await obs($, 'dashboard stop')).text).toBe('No Insight dashboard is running.')
})

test('stderr before the URL does not end the dashboard; a child that dies with only stderr is the exit row', async ($, on) => {
  forgetPlatform()
  let release = () => {}
  const world: World = {
    commands: DARWIN,
    child: { dashboard: { chunks: [...NOISE_CHUNKS, URL_CHUNK], code: 0, gate: new Promise<void>(r => (release = r)) } },
  }
  const { ran, dashboards } = host(on, world)
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  expect((await obs($, 'dashboard')).text).toBe(`Insight dashboard at ${URL} with session abc loaded; opened in the browser. /obs dashboard stop ends it.`)
  expect(ran.filter(r => r[0] === 'open')).toEqual([['open', URL]])
  expect(dashboards()).toEqual({ insight: { url: URL, port: 7860 }, converge: null })
  expect((await obs($, 'dashboard stop')).text).toBe('Insight dashboard stopped.')
  release()

  world.child = { dashboard: { chunks: [{ stream: 'stderr', text: 'Traceback (most recent call last):\nModuleNotFoundError: No module named gradio\n' }], code: 1 } }
  expect((await obs($, 'dashboard')).text).toBe('obs dashboard: the dashboard exited with 1 before printing its URL: ModuleNotFoundError: No module named gradio')
  expect(ran.filter(r => r[0] === 'open')).toHaveLength(1)
  expect(dashboards()).toEqual({ insight: null, converge: null })
})

test('a dashboard whose preload could not be wired names the bundle ZIP to drop in', async ($, on) => {
  forgetPlatform()
  const { ran } = host(on, { commands: DARWIN, child: { dashboard: { chunks: [UNLOADED_CHUNK], code: 0 } } })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const out = await obs($, 'dashboard')
  expect(out.text).toBe(
    `Insight dashboard at ${URL} without session abc loaded (the preload could not be wired): drop the ZIP /obs bundle writes into it; opened in the browser. /obs dashboard stop ends it.`,
  )
  expect(ran.filter(r => r[0] === 'open')).toEqual([['open', URL]])
  await $.session.end(END)
})

test('the app install is declined by Not now or a dismissed ask, and not asked of a headless session', async ($, on) => {
  forgetPlatform()
  const world: World = { extras: ['llm', 'charts'], commands: DARWIN, answer: 'Not now' }
  const { spawned, asked, rows, timeline } = host(on, world)
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  expect((await obs($, 'dashboard')).text).toBe(DECLINED)
  world.answer = 'dismiss'
  expect((await obs($, 'converge')).text).toBe(DECLINED)
  expect(asked).toEqual([ASK_APP, ASK_APP])
  // Nothing was announced: no install started.
  expect(rows).toEqual([])
  expect(spawned).toEqual([])

  // Headless: no one to ask; the install is still announced in its row, then runs, and the dashboard starts.
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: false })
  expect((await obs($, 'dashboard')).text).toBe(`Insight dashboard at ${URL} with session abc loaded; opened in the browser. /obs dashboard stop ends it.`)
  expect(asked).toHaveLength(2)
  expect(rows).toEqual([INSTALLING_APP])
  expect(timeline).toEqual(['ask', 'ask', 'row', 'pip', 'child', 'open'])
  expect(setupRequests(spawned)[0]).toMatchObject({ source: 'agent-trajviz>=0.2.0,<0.3', extras: ['llm', 'charts', 'llm', 'charts', 'app'] })
  expect(children(spawned)).toEqual([['dashboard', '--transcript', DERIVED, '--window', '1000000']])
  await $.session.end(END)
})

test('a reload (session.start again) ends the dashboards and clears their state', async ($, on) => {
  forgetPlatform()
  let release = () => {}
  const world: World = { commands: DARWIN, child: { dashboard: { chunks: [URL_CHUNK], code: 0, gate: new Promise<void>(r => (release = r)) } } }
  const { dashboards } = host(on, world)
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  await obs($, 'dashboard')
  expect(dashboards()).toEqual({ insight: { url: URL, port: 7860 }, converge: null })
  // The module reloaded (a config change, /reload-plugins): the engine killed the child, session.start runs again.
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  expect(dashboards()).toEqual({ insight: null, converge: null })
  expect((await obs($, 'dashboard stop')).text).toBe('No Insight dashboard is running.')
  release()
})

test('converge asks, announces and installs the app extra once, and takes the data root and port, defaulting to the project folder', async ($, on) => {
  forgetPlatform()
  const { ran, spawned, asked, rows, timeline } = host(on, { extras: ['llm', 'charts'], commands: DARWIN })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const out = await obs($, 'converge')
  // The same ask and announcing row as `dashboard`, in the same order, before pip and the child.
  expect(asked).toEqual([ASK_APP])
  expect(rows).toEqual([INSTALLING_APP])
  expect(timeline).toEqual(['ask', 'row', 'pip', 'child', 'open'])
  expect(setupRequests(spawned)[0]).toMatchObject({ source: 'agent-trajviz>=0.2.0,<0.3', extras: ['llm', 'charts', 'llm', 'charts', 'app'] })
  expect(children(spawned)).toEqual([['converge', '--data-root', PROJECT_DIR]])
  expect(out.text).toBe(`Converge dashboard at ${URL} over ${PROJECT_DIR}; opened in the browser. /obs converge stop ends it.`)
  expect(ran.filter(r => r[0] === 'open')).toEqual([['open', URL]])
  // The child exited at once (no gate): nothing is left to stop.
  expect((await obs($, 'converge stop')).text).toBe('No Converge dashboard is running.')

  // The extra is installed now: a second converge neither asks nor announces nor installs.
  await obs($, 'converge --data-root /data/runs --port 7861')
  expect(asked).toEqual([ASK_APP])
  expect(rows).toEqual([INSTALLING_APP])
  expect(children(spawned)).toEqual([['converge', '--data-root', PROJECT_DIR], ['converge', '--data-root', '/data/runs', '--port', '7861']])
  expect(setupRequests(spawned)).toHaveLength(1)
  await $.session.end(END)
})

test('over SSH the URL is printed with a port-forward hint and nothing opens', async ($, on) => {
  const { ran } = host(on, { env: { SSH_CONNECTION: '1.2.3.4 5 6.7.8.9 22' }, commands: DARWIN })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const out = await obs($, 'dashboard')
  expect(out.text).toBe(
    `Insight dashboard at ${URL} with session abc loaded; not opened over SSH: forward the port (ssh -L 7860:127.0.0.1:7860 <host>) and open it on your machine. /obs dashboard stop ends it.`,
  )
  expect(ran.filter(r => r[0] === 'open')).toEqual([])
  await $.session.end(END)
})

test('with openBrowser off the URL is printed and nothing opens', { options: { openBrowser: false } }, async ($, on) => {
  const { ran } = host(on, { commands: DARWIN })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const out = await obs($, 'dashboard')
  expect(out.text).toBe(`Insight dashboard at ${URL} with session abc loaded; not opened (openBrowser is off in /config). /obs dashboard stop ends it.`)
  expect(ran.filter(r => r[0] === 'open')).toEqual([])
  await $.session.end(END)
})

test('bad arguments are the usage rows, before any child', async ($, on) => {
  const { spawned } = host(on, { commands: DARWIN })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  expect((await obs($, 'dashboard --port x')).text).toBe('Usage: /obs dashboard [--port N] | dashboard stop  (N from 1 to 65535)')
  expect((await obs($, 'dashboard --port 70000')).text).toBe('Usage: /obs dashboard [--port N] | dashboard stop  (N from 1 to 65535)')
  expect((await obs($, 'dashboard restart')).text).toBe('Usage: /obs dashboard [--port N] | dashboard stop  (N from 1 to 65535)')
  expect((await obs($, 'converge --port')).text).toBe('Usage: /obs converge [--data-root DIR] [--port N] | converge stop  (N from 1 to 65535)')
  expect((await obs($, 'converge --data-root')).text).toBe('Usage: /obs converge [--data-root DIR] [--port N] | converge stop  (N from 1 to 65535)')
  expect(spawned).toEqual([])
})

const setupRequests = (spawned: Host['spawned']) => spawned.map(s => s.argv).filter(a => a.some(v => v.endsWith('/bridge/bootstrap.py'))).map(a => JSON.parse(a[a.indexOf('--request') + 1] as string))
