import { expect, mock, test } from 'claude-code/testing'
import type { EngineInterface, On } from 'claude-code'
import { forgetPlatform, openTarget } from './lib/open'
import { flagValues } from './lib/args'
import { fmtBytes } from './lib/format'

const HOME = '/Users/me'
const CWD = '/Users/me/p'
const CACHE = `${HOME}/.cache/trajviz-obs`
const VENV_PY = `${CACHE}/envs/0123456789abcdef01234567/0123456789abcdef0123456789abcdef/bin/python`
const STATE_PATH = `${CACHE}/state.json`
const DERIVED = `${HOME}/.claude/projects/-Users-me-p/abc.jsonl`
const ARTIFACTS = `${CACHE}/sessions/abc`
const USAGE = { startedAt: 0, context: { window: 1_000_000, percent: 12 }, rateLimits: [] }
const NOW = 1_761_150_000_000

/** `state.json` with the given extras installed. */
const stateJson = (extras: string[]): string =>
  JSON.stringify({ revision: 4, python: VENV_PY, trajviz: '0.2.1', extras, source: 'agent-trajviz>=0.2.0,<0.3', createdAt: 1 })

/** The argv every session-bound bridge subcommand gets for the current session. */
const SESSION_ARGV = ['--transcript', DERIVED, '--window', '1000000', '--artifacts', ARTIFACTS, '--kind', 'current']

type RunAnswer = { exitCode: number; stdout: string; stderr?: string }

/** `process.run` answers: the bridge by subcommand (a list answers call by call, its last repeating), other commands by their first word. */
const answerOf = (bridge: Record<string, RunAnswer | RunAnswer[]>, other: Record<string, RunAnswer>) => (argv: readonly string[]): RunAnswer | undefined => {
  if (argv[1] !== '-I' || !argv[2]?.endsWith('/bridge/trajviz_obs_cli.py')) return other[argv[0] ?? '']
  const a = bridge[argv[3] ?? '']
  if (!Array.isArray(a)) return a
  return a.length > 1 ? a.shift() : a[0]
}

type World = {
  bridge?: Record<string, RunAnswer | RunAnswer[]>
  /** Non-bridge commands by name (`uname`, `open`, …); anything else cannot start. */
  commands?: Record<string, RunAnswer>
  /** The extras `state.json` lists; `null` for no environment at all. */
  extras?: string[] | null
  env?: Record<string, string>
}

type Host = {
  ran: string[][]
  spawned: string[][]
  files: Record<string, string>
  /** The transcript rows `$.ui.log` appended. */
  rows: string[]
  /** Rows and spawns in the order they happened: `row`, `spawn`. */
  timeline: string[]
}

/** The host beneath `/obs`: env, files (writes kept), session, canned process answers; every run and spawn recorded. */
function host(on: On, world: World = {}): Host {
  const ran: string[][] = []
  const spawned: string[][] = []
  const rows: string[] = []
  const timeline: string[] = []
  const files: Record<string, string> = {}
  if (world.extras !== null) files[STATE_PATH] = stateJson(world.extras ?? ['llm', 'charts'])
  const answers = answerOf(world.bridge ?? {}, world.commands ?? {})
  on('command.register', () => ({ value: { command: 'obs' } }))
  on('tool.register', () => ({ value: { tool: 'mcp__trajviz-obs__obs' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: 'abc' }))
  on('session.cwd', () => ({ value: CWD }))
  on('session.usage', () => ({ value: USAGE }))
  mock.env(on, { HOME, ...(world.env ?? {}) })
  mock.clock(on, { now: NOW })
  on('fs.read', (_, e) => (e.path in files ? { value: files[e.path] as string } : { deny: `ENOENT: ${e.path}` }))
  on('fs.write', (_, e) => {
    files[e.path] = e.text
    return { value: undefined }
  })
  on('fs.exists', (_, e) => ({ value: e.path === VENV_PY || e.path === DERIVED }))
  on('ui.toast', () => ({ value: undefined }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.log', (_, e) => {
    if (e.to === 'transcript') {
      rows.push(e.text)
      timeline.push('row')
    }
    return { value: undefined }
  })
  on('process.run', (_, e) => {
    ran.push([...e.argv])
    if (e.argv[0] === 'uv' && e.argv[1] === '--version') return { value: { exitCode: 0, stdout: 'uv 0.9.0\n', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    const a = answers(e.argv)
    if (!a) return { deny: `cannot start ${e.argv[0]}` }
    return { value: { exitCode: a.exitCode, stdout: a.stdout, stderr: a.stderr ?? '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('process.spawn', async function* (_, e) {
    spawned.push([...e.argv])
    timeline.push('spawn')
    if (e.argv.some(a => a.endsWith('/bridge/bootstrap.py'))) {
      const req = JSON.parse(e.argv[e.argv.indexOf('--request') + 1] as string)
      const state = { revision: 4, python: VENV_PY, pythonOption: req.pythonOption, trajviz: '0.2.1', extras: [...new Set(['llm', 'charts', ...req.extras])], source: req.source, ...(req.local ? { sourceCwd: CWD } : {}), createdAt: 1 }
      files[STATE_PATH] = JSON.stringify(state)

      yield { stream: 'stderr' as const, text: 'Installed\n' }
      yield { stream: 'stdout' as const, text: JSON.stringify(state) }
      return { value: { code: 0, signal: null } }
    }

    yield { stream: 'stdout' as const, text: 'Installed\n' }
    return { value: { code: 0, signal: null } }
  })
  return { ran, spawned, files, rows, timeline }
}

const json = (doc: unknown): RunAnswer => ({ exitCode: 0, stdout: `${JSON.stringify(doc)}\n` })

const obs = ($: { command: { run: (args: never) => Promise<{ text?: string; context?: readonly string[] }> } }, args: string) =>
  $.command.run({ command: 'obs', args, origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 80 } } as never)

/** The argv of every bridge call, after the launcher. */
const bridgeCalls = (ran: string[][]): string[][] => ran.filter(r => r[1] === '-I').map(r => r.slice(3))

const STEPS_TABLE = '| step | role | tokens |\n|---|---|---|\n| 1 | assistant | 120 |\n\nrows 1–5 of 311'
const STEPS_VIEW = {
  view: { id: 'steps', title: 'Steps', kind: 'table', description: 'Every step.' },
  path: `${ARTIFACTS}/views/steps.json`,
  bytes: 48_213,
  rows_total: 311,
  inline: { table: STEPS_TABLE },
}

test('view passes paging flags, draws the inline table and names the file in context', async ($, on) => {
  const { ran } = host(on, { bridge: { view: json(STEPS_VIEW) } })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const out = await obs($, 'view steps --limit 5')
  expect(bridgeCalls(ran)).toEqual([['view', ...SESSION_ARGV, 'steps', '--limit', '5']])
  const lines = out.text!.split('\n')
  expect(lines[0]).toBe('obs view steps · Steps · 311 rows')
  expect(lines.slice(1).join('\n')).toBe(STEPS_TABLE)
  expect(out.context).toHaveLength(1)
  expect(out.context![0]).toContain(`${ARTIFACTS}/views/steps.json`)
  expect(out.context![0]).toContain('mcp__trajviz-obs__obs')
  // Options, offset and --json go through; a bad page is refused before the bridge runs.
  await obs($, 'view context_pressure agent=main --offset 3 --json')
  expect(bridgeCalls(ran)[1]).toEqual(['view', ...SESSION_ARGV, 'context_pressure', '--option', 'agent=main', '--offset', '3', '--json'])
  expect((await obs($, 'view steps --limit 0')).text).toBe('--limit must be a whole number from 1 to 500.')
  expect((await obs($, 'view steps --limit')).text).toBe('--limit must be a whole number from 1 to 500.')
  expect((await obs($, 'view steps --offset -1')).text).toBe('--offset must be a whole number of 0 or more.')
  expect((await obs($, 'view steps --offset 9007199254740992')).text).toBe('--offset must be a whole number of 0 or more.')
  expect(bridgeCalls(ran)).toHaveLength(2)
})

test('view with no id lists the catalog', async ($, on) => {
  const catalog = {
    views: [
      { id: 'steps', kind: 'table', title: 'Steps', description: 'Every step.', options: {} },
      { id: 'context_pressure', kind: 'chart', title: 'Context pressure', description: '', options: { agent: 'an agent', window_limit: 'tokens' } },
    ],
  }
  const { ran } = host(on, { bridge: { view: json(catalog) } })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const out = await obs($, 'view')
  expect(bridgeCalls(ran)).toEqual([['view', ...SESSION_ARGV]])
  expect(out.text).toContain('2 TrajViz views')
  expect(out.text).toContain('steps')
  expect(out.text).toContain('context_pressure')
  expect(out.text).toContain('agent, window_limit')
  expect(out.context).toBeUndefined()
})

test('a chart view answers its series; --json answers the data', async ($, on) => {
  const series = { ...STEPS_VIEW, view: { id: 'token_usage', title: 'Token usage', kind: 'chart', description: '' }, inline: { series: 'fresh ▁▂▃ max 120\n311 points' } }
  const data = { ...STEPS_VIEW, inline: { data: [{ step: 1 }] } }
  host(on, { bridge: { view: [json(series), json(data)] } })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  expect((await obs($, 'view token_usage')).text).toBe('obs view token_usage · Token usage · 311 rows\nfresh ▁▂▃ max 120\n311 points')
  expect((await obs($, 'view steps --json')).text).toBe('obs view steps · Steps · 311 rows\n[\n  {\n    "step": 1\n  }\n]')
})

test('the tab shortcuts are views with the same paging', async ($, on) => {
  const { ran } = host(on, { bridge: { view: json(STEPS_VIEW) } })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  for (const tab of ['issues', 'health', 'patterns', 'agents', 'context', 'timeline']) await obs($, `${tab} --limit 10`)
  expect(bridgeCalls(ran).map(c => c[c.length - 3])).toEqual(['issues', 'health', 'hotspots', 'agents', 'context_pressure', 'workflow'])
  expect(bridgeCalls(ran).every(c => c.slice(-2).join(' ') === '--limit 10')).toBe(true)
})

test('an unknown view is the bridge message; an empty session is the spec row', async ($, on) => {
  host(on, { bridge: { view: { exitCode: 1, stdout: '{"error": {"code": "unknown_view", "message": "No view \'nope\'; `view` with no id lists them.", "views": ["steps"]}}' } } })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  expect((await obs($, 'view nope')).text).toBe("No view 'nope'; `view` with no id lists them.")
})

test('an empty session answers the spec row for view and the exports', async ($, on) => {
  const empty = json({ empty: true, reason: 'no_steps' })
  host(on, { extras: ['llm', 'charts'], bridge: { view: empty, json: empty } })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const row = 'Nothing to analyze yet: this session has no assistant steps. Run /obs after the first turn.'
  expect((await obs($, 'issues')).text).toBe(row)
  expect((await obs($, 'json')).text).toBe(row)
})

test('json, bundle and schemas pass their flags and print what was written', async ($, on) => {
  const { ran } = host(on, {
    bridge: {
      json: json({ path: `${ARTIFACTS}/views`, files: ['index.json', 'steps.json', 'tools.json'], bytes: 20_480 }),
      bundle: json({ path: `${ARTIFACTS}/bundle.zip`, bytes: 3_145_728, members: ['abc.jsonl', 'abc/subagents/x.jsonl'], members_total: 2 }),
      schemas: json({ path: `${CACHE}/schemas`, files: ['steps.schema.json', 'health.schema.json'], bytes: 4_096 }),
    },
  })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const j = await obs($, 'json --view steps --view tools --with-trajectory -o /tmp/out')
  expect(bridgeCalls(ran)[0]).toEqual(['json', ...SESSION_ARGV, '--out', '/tmp/out', '--view', 'steps', '--view', 'tools', '--with-trajectory'])
  expect(j.text).toBe(`3 files (20 KB) written to ${ARTIFACTS}/views.`)
  const b = await obs($, 'bundle')
  expect(bridgeCalls(ran)[1]).toEqual(['bundle', '--transcript', DERIVED, '--artifacts', ARTIFACTS])
  expect(b.text).toBe(`Bundle of 2 files (3.0 MB) written to ${ARTIFACTS}/bundle.zip; upload it to a hosted TrajViz dashboard.`)
  const s = await obs($, 'schemas /tmp/schemas')
  expect(bridgeCalls(ran)[2]).toEqual(['schemas', '--out', '/tmp/schemas'])
  expect(s.text).toBe(`JSON Schemas of 2 views (4.0 KB) written to ${CACHE}/schemas.`)
  await obs($, 'schemas')
  expect(bridgeCalls(ran)[3]).toEqual(['schemas'])
})

test('bundle counts all ZIP members when the bridge caps its member preview', async ($, on) => {
  host(on, { bridge: { bundle: json({ path: `${ARTIFACTS}/bundle.zip`, bytes: 1_024, members: Array.from({ length: 200 }, (_, i) => `${i}.jsonl`), members_total: 321 }) } })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  expect((await obs($, 'bundle')).text).toContain('Bundle of 321 files')
})

test('an output flag without a path is refused before exports run', async ($, on) => {
  const { ran } = host(on)
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  for (const args of ['json -o', 'json --out', 'bundle --out=']) {
    expect((await obs($, args)).text).toBe('-o/--out needs an output path.')
  }
  expect(bridgeCalls(ran)).toEqual([])
})

test('json takes every --view as the spec writes it and refuses one without an id', async ($, on) => {
  const { ran } = host(on, { bridge: { json: json({ path: `${ARTIFACTS}/views`, files: ['index.json', 'steps.json'], bytes: 2_048 }) } })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  // `--view=ID` reads as `--view ID`; a comma is part of the id (the bridge answers unknown_view), never a separator.
  await obs($, 'json --view=steps')
  expect(bridgeCalls(ran)[0]).toEqual(['json', ...SESSION_ARGV, '--view', 'steps'])
  await obs($, 'json --view steps,tools')
  expect(bridgeCalls(ran)[1]).toEqual(['json', ...SESSION_ARGV, '--view', 'steps,tools'])
  // A bare `--view` (anywhere among several) is refused before the bridge runs.
  const usage = '--view needs a view id (/obs view lists them).'
  expect((await obs($, 'json --view')).text).toBe(usage)
  expect((await obs($, 'json --view=')).text).toBe(usage)
  expect((await obs($, 'json --view steps --view --with-trajectory')).text).toBe(usage)
  expect(bridgeCalls(ran)).toHaveLength(2)
})

test('a bare --session is refused instead of analyzing the current session', async ($, on) => {
  const { ran } = host(on)
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  for (const args of ['--session', 'view steps --session', 'json --session=']) {
    expect((await obs($, args)).text).toBe('--session needs a session id, prefix, or transcript path.')
  }
  expect(bridgeCalls(ran)).toEqual([])
})

test('flagValues collects every value of a repeated flag in order', () => {
  expect(flagValues('json --view steps --view=tools -o d --view health', 'view')).toEqual(['steps', 'tools', 'health'])
  expect(flagValues('json --view steps --view --with-trajectory', 'view')).toEqual(['steps', true])
  expect(flagValues('json -o d', 'view')).toEqual([])
  // Nothing after `--` is a flag; `-view` and `--view` are the same name, as in parseArgs.
  expect(flagValues('json -view a -- --view b', 'view')).toEqual(['a'])
})

test('sessions prints a table of this project, newest first', async ($, on) => {
  const { ran } = host(on, {
    bridge: {
      sessions: json({
        project_dir: `${HOME}/.claude/projects/-Users-me-p`,
        sessions: [
          { id: 'abc', path: DERIVED, mtime_ms: NOW, size: 2_300_000, records: 812, started: '2026-10-22T14:03:10.000Z' },
          { id: 'def456', path: '/x/def456.jsonl', mtime_ms: NOW - 1, size: 512, records: 3, started: null },
        ],
        cut: 1,
      }),
    },
  })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const out = await obs($, 'sessions')
  expect(bridgeCalls(ran)).toEqual([['sessions', '--cwd', CWD, '--config-dir', `${HOME}/.claude`]])
  const lines = out.text!.split('\n')
  expect(lines[0]).toBe(`2 sessions in ${HOME}/.claude/projects/-Users-me-p, newest first:`)
  // Columns aligned: ids and times left, sizes left, record counts right; the current session marked.
  expect(lines[1]).toMatch(/^  abc {5}2026-10-22 \d\d:\d\d {2}2\.2 MB {2}812 records {2}← this session$/)
  expect(lines[2]).toMatch(/^  def456 {2}\? {17}512 B {5}3 records$/)
  expect(lines[3]).toBe('  … 1 more')
  expect(lines).toHaveLength(4)
  // `sessions N` bounds the list; anything else is the usage line.
  await obs($, 'sessions 5')
  expect(bridgeCalls(ran)[1]).toEqual(['sessions', '--cwd', CWD, '--config-dir', `${HOME}/.claude`, '--limit', '5'])
  expect((await obs($, 'sessions zero')).text).toBe('Usage: /obs sessions [N]  (N from 1 to 500)')
  expect((await obs($, 'sessions 5 extra')).text).toBe('Usage: /obs sessions [N]  (N from 1 to 500)')
  expect(bridgeCalls(ran)).toHaveLength(2)
})

test('sessions with none, and the first run sets the environment up first', async ($, on) => {
  const { ran, spawned, files } = host(on, {
    extras: null,
    bridge: { sessions: json({ project_dir: '/pd', sessions: [] }) },
    commands: { uv: { exitCode: 0, stdout: 'uv 0.9.0\n' } },
  })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  expect((await obs($, 'sessions')).text).toBe('No sessions for this project in /pd.')
  expect(setupRequests(spawned)).toHaveLength(1)
  expect(setupRequests(spawned)[0]).toMatchObject({ source: 'agent-trajviz>=0.2.0,<0.3', extras: ['llm', 'charts'] })
  expect(STATE_PATH in files).toBe(true)
  expect(bridgeCalls(ran)).toEqual([['sessions', '--cwd', CWD, '--config-dir', `${HOME}/.claude`]])
})

// --- openTarget over a fake `$` ---

type FakeOpen = { $: EngineInterface; ran: string[][] }

function fakeOpen(env: Record<string, string>, exit: Record<string, number> = {}): FakeOpen {
  const ran: string[][] = []
  const $ = {
    env: { get: async (name: string) => env[name] },
    process: {
      run: async (argv: readonly string[]) => {
        ran.push([...argv])
        const cmd = argv[0] as string
        if (!(cmd in exit)) throw new Error(`cannot start ${cmd}`)
        return { exitCode: exit[cmd], stdout: cmd === 'uname' ? 'Linux\n' : '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false }
      },
    },
  }
  return { $: $ as unknown as EngineInterface, ran }
}

test('openTarget: disabled, SSH, Linux, Windows and a failed open', async () => {
  forgetPlatform()
  const off = fakeOpen({})
  expect(await openTarget(off.$, { openBrowser: false }, '/r.html')).toBe('disabled')
  expect(off.ran).toEqual([])
  const ssh = fakeOpen({ SSH_CONNECTION: '1.2.3.4 1 5.6.7.8 22' })
  expect(await openTarget(ssh.$, {}, 'http://127.0.0.1:7860')).toBe('skipped-ssh')
  expect(ssh.ran).toEqual([])
  const linux = fakeOpen({}, { uname: 0, 'xdg-open': 0 })
  expect(await openTarget(linux.$, {}, 'http://127.0.0.1:7860')).toBe('opened')
  expect(await openTarget(linux.$, {}, '/r.html')).toBe('opened')
  // `uname -s` once, cached across calls.
  expect(linux.ran).toEqual([['uname', '-s'], ['xdg-open', 'http://127.0.0.1:7860'], ['xdg-open', '/r.html']])
  const missing = fakeOpen({}, { uname: 0 })
  expect(await openTarget(missing.$, {}, '/r.html')).toBe('failed')
  forgetPlatform()
  const win = fakeOpen({ OS: 'Windows_NT' }, { cmd: 0 })
  expect(await openTarget(win.$, {}, 'C:\\r.html')).toBe('opened')
  expect(win.ran).toEqual([['cmd', '/c', 'start', '', 'C:\\r.html']])
  const bad = fakeOpen({ OS: 'Windows_NT' }, { cmd: 1 })
  expect(await openTarget(bad.$, {}, 'C:\\r.html')).toBe('failed')
  forgetPlatform()
})

test('fmtBytes', () => {
  expect(fmtBytes(0)).toBe('0 B')
  expect(fmtBytes(512)).toBe('512 B')
  expect(fmtBytes(1023)).toBe('1023 B')
  expect(fmtBytes(1024)).toBe('1.0 KB')
  expect(fmtBytes(20_480)).toBe('20 KB')
  expect(fmtBytes(1_234_567)).toBe('1.2 MB')
  expect(fmtBytes(3_145_728)).toBe('3.0 MB')
  expect(fmtBytes(5 * 1024 ** 3)).toBe('5.0 GB')
  expect(fmtBytes(Number.NaN)).toBe('?')
})

const setupRequests = (spawned: string[][]) => spawned.map(s => s).filter(a => a.some(v => v.endsWith('/bridge/bootstrap.py'))).map(a => JSON.parse(a[a.indexOf('--request') + 1] as string))
