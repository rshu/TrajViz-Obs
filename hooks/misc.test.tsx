import { expect, mock, test, type MockClock } from 'claude-code/testing'
import type { On } from 'claude-code'
import { passArgs } from './lib/args'
import { attributionText, doctorText, parseExtras, setupText, type DoctorDoc } from './commands/misc'
import { SNAPSHOT } from './testdata/snapshot'

const HOME = '/Users/me'
const CWD = '/Users/me/p'
const CACHE = `${HOME}/.cache/trajviz-obs`
const VENV_PY = `${CACHE}/envs/0123456789abcdef01234567/0123456789abcdef0123456789abcdef/bin/python`
const STATE_PATH = `${CACHE}/state.json`
const DERIVED = `${HOME}/.claude/projects/-Users-me-p/abc.jsonl`
const ARTIFACTS = `${CACHE}/sessions/abc`
const USAGE = { startedAt: 0, context: { window: 1_000_000, percent: 12 }, rateLimits: [] }
const NOW = 1_761_150_000_000
const SOURCE = 'agent-trajviz==0.3.1'

/** `state.json` with the given extras installed. */
const stateJson = (extras: string[]): string =>
  JSON.stringify({ revision: 5, python: VENV_PY, trajviz: '0.3.1', extras, source: SOURCE, createdAt: NOW })

/** The argv every session-bound bridge subcommand gets for the current session. */
const SESSION_ARGV = ['--transcript', DERIVED, '--window', '1000000', '--artifacts', ARTIFACTS, '--kind', 'current']
/** `resolve`'s argv before its selector. */
const RESOLVE = ['resolve', '--cwd', CWD, '--config-dir', `${HOME}/.claude`]

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
  /** Non-bridge commands by name (`uv`, the venv's python, …); anything else cannot start. */
  commands?: Record<string, RunAnswer>
  /** The extras `state.json` lists; `null` for no environment at all. */
  extras?: string[] | null
}

type Host = {
  clock: MockClock
  ran: string[][]
  spawned: string[][]
  files: Record<string, string>
  /** Every `$.ui.status` text, `undefined` for a clear. */
  statuses: (string | undefined)[]
  toasts: string[]
}

/** The host beneath `/obs`: env, files (writes kept), session, canned process answers; every run and spawn recorded. */
function host(on: On, world: World = {}): Host {
  const ran: string[][] = []
  const spawned: string[][] = []
  const statuses: (string | undefined)[] = []
  const toasts: string[] = []
  const files: Record<string, string> = {}
  if (world.extras !== null) files[STATE_PATH] = stateJson(world.extras ?? ['llm', 'charts', 'zstd'])
  const answers = answerOf(world.bridge ?? {}, world.commands ?? {})
  on('command.register', () => ({ value: { command: 'obs' } }))
  on('tool.register', () => ({ value: { tool: 'mcp__trajviz-obs__obs' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: 'abc' }))
  on('session.cwd', () => ({ value: CWD }))
  on('session.usage', () => ({ value: USAGE }))
  mock.env(on, { HOME })
  const clock = mock.clock(on, { now: NOW })
  on('fs.read', (_, e) => (e.path in files ? { value: files[e.path] as string } : { deny: `ENOENT: ${e.path}` }))
  on('fs.write', (_, e) => {
    files[e.path] = e.text
    return { value: undefined }
  })
  on('fs.exists', (_, e) => ({ value: e.path === VENV_PY || e.path === DERIVED }))
  on('ui.toast', (_, e) => {
    toasts.push(e.text)
    return { value: undefined }
  })
  on('ui.status', (_, e) => {
    statuses.push(e.text)
    return { value: undefined }
  })
  on('ui.log', () => ({ value: undefined }))
  on('process.run', (_, e) => {
    ran.push([...e.argv])
    const a = answers(e.argv)
    if (!a) return { deny: `cannot start ${e.argv[0]}` }
    return { value: { exitCode: a.exitCode, stdout: a.stdout, stderr: a.stderr ?? '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('process.spawn', async function* (_, e) {
    spawned.push([...e.argv])
    if (e.argv.some(a => a.endsWith('/bridge/bootstrap.py'))) {
      const req = JSON.parse(e.argv[e.argv.indexOf('--request') + 1] as string)
      const state = { revision: 5, python: VENV_PY, pythonOption: req.pythonOption, trajviz: (world.commands?.[VENV_PY]?.stdout.trim() || '0.3.1'), extras: [...new Set(['llm', 'charts', 'zstd', ...req.extras])], source: req.source, ...(req.local ? { sourceCwd: CWD } : {}), createdAt: 1 }
      files[STATE_PATH] = JSON.stringify(state)

      yield { stream: 'stderr' as const, text: 'Installed\n' }
      yield { stream: 'stdout' as const, text: JSON.stringify(state) }
      return { value: { code: 0, signal: null } }
    }

    yield { stream: 'stdout' as const, text: 'Installed\n' }
    return { value: { code: 0, signal: null } }
  })
  return { clock, ran, spawned, files, statuses, toasts }
}

const json = (doc: unknown): RunAnswer => ({ exitCode: 0, stdout: `${JSON.stringify(doc)}\n` })
const envelope = (code: string, message: string, extra: Record<string, unknown> = {}): RunAnswer => ({
  exitCode: 1,
  stdout: `${JSON.stringify({ error: { code, message, ...extra } })}\n`,
})

const obs = ($: { command: { run: (args: never) => Promise<{ text?: string; context?: readonly string[] }> } }, args: string) =>
  $.command.run({ command: 'obs', args, origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 80 } } as never)

/** The argv of every bridge call, after the launcher. */
const bridgeCalls = (ran: string[][]): string[][] => ran.filter(r => r[1] === '-I').map(r => r.slice(3))

/** A `resolve` answer for session `id` at `/x/<id>.jsonl`. */
const resolved = (id: string): RunAnswer => json({ path: `/x/${id}.jsonl`, session_id: id, kind: 'file' })

test('sessions --session lists captured IDs without selecting a model source', async ($, on) => {
  const { ran } = host(on, { bridge: { resolve: resolved('capture'), 'capture-sessions': json({ text: 'Captured sessions: A, B', sessions: [{ session_id: 'A' }, { session_id: 'B' }] }) } })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  expect((await obs($, 'sessions 2 --session /x/capture.jsonl')).text).toBe('Captured sessions: A, B')
  expect(bridgeCalls(ran).at(-1)).toEqual(['capture-sessions', '--transcript', '/x/capture.jsonl', '--limit', '2'])
})

const COMPARE_TEXT = 'Comparison: reference `def.jsonl` → compared `abc.jsonl` (anchor: none, evidence: medium)\n\n| | reference | compared |\n|---|---|---|\n| finished | yes | no |'

test('compare resolves the other run, passes its flags and prints the comparison', async ($, on) => {
  const { ran } = host(on, {
    bridge: { resolve: resolved('def'), compare: json({ path: `${ARTIFACTS}/compare/def.json`, orientation: {}, summary: {}, text: COMPARE_TEXT }) },
  })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const out = await obs($, 'compare def --anchor-patch /tmp/p.diff --fuzzy-commands --reverse')
  expect(bridgeCalls(ran)).toEqual([
    [...RESOLVE, '--session', 'def'],
    ['compare', ...SESSION_ARGV, '--other', '/x/def.jsonl', '--anchor-patch', '/tmp/p.diff', '--fuzzy-commands', '--reverse'],
  ])
  expect(out.text).toBe(`${COMPARE_TEXT}\nComparison views written to ${ARTIFACTS}/compare/def.json.`)
  // Bare: no flags passed; `--session` picks the current side.
  await obs($, 'compare def')
  expect(bridgeCalls(ran)[3]).toEqual(['compare', ...SESSION_ARGV, '--other', '/x/def.jsonl'])
  // Usage before anything runs.
  const usage = 'Usage: /obs compare <session|path> [--anchor-patch P] [--fuzzy-commands] [--reverse]'
  expect((await obs($, 'compare')).text).toBe(usage)
  expect((await obs($, 'compare a b')).text).toBe(usage)
  expect((await obs($, 'compare def --anchor-patch')).text).toBe(usage)
  expect(bridgeCalls(ran)).toHaveLength(4)
})

test("an other run the bridge cannot find is its message, never the current session's row", async ($, on) => {
  host(on, {
    bridge: {
      resolve: [
        envelope('not_found', "No session 'nope' in this project.", { path: 'nope' }),
        envelope('ambiguous', "More than one session starts with 'd'", { candidates: [{ id: 'def', started: '2026-10-22T14:03:10Z' }, { id: 'dgh', started: null }] }),
      ],
      compare: json({ empty: true, reason: 'no_steps' }),
    },
  })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  expect((await obs($, 'compare nope')).text).toBe("No session 'nope' in this project.")
  expect((await obs($, 'compare d')).text).toBe("More than one session starts with 'd': def (2026-10-22T14:03:10Z), dgh (?)")
  expect((await obs($, 'compare /x/def.jsonl')).text).toBe("More than one session starts with 'd': def (2026-10-22T14:03:10Z), dgh (?)")
})

test('compare of an empty session is the spec row', async ($, on) => {
  host(on, { bridge: { resolve: resolved('def'), compare: json({ empty: true, reason: 'no_steps' }) } })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  expect((await obs($, 'compare def')).text).toBe('Nothing to analyze yet: this session has no assistant steps. Run /obs after the first turn.')
})

test('scorecard resolves every other run and passes labels and --no-self', async ($, on) => {
  const text = '| run | finished | steps |\n|---|---|---|\n| me | yes | 12 |'
  const { ran } = host(on, {
    bridge: { resolve: [resolved('b'), resolved('c')], scorecard: json({ path: `${ARTIFACTS}/compare/scorecard.json`, rows: [], text }) },
  })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const out = await obs($, 'scorecard b c --labels me,b,c --fuzzy-commands --no-self')
  expect(bridgeCalls(ran)).toEqual([
    [...RESOLVE, '--session', 'b'],
    [...RESOLVE, '--session', 'c'],
    ['scorecard', ...SESSION_ARGV, '--others', '/x/b.jsonl', '/x/c.jsonl', '--fuzzy-commands', '--labels', 'me,b,c', '--no-self'],
  ])
  expect(out.text).toBe(`${text}\nScorecard views written to ${ARTIFACTS}/compare/scorecard.json.`)
  const usage = 'Usage: /obs scorecard <session|path> [<session|path>...] [--fuzzy-commands] [--labels a,b] [--no-self]'
  expect((await obs($, 'scorecard')).text).toBe(usage)
  expect((await obs($, 'scorecard b --labels')).text).toBe(usage)
  expect(bridgeCalls(ran)).toHaveLength(3)
})

test('converge-cli passes its arguments through and names the report or prints the summary', async ($, on) => {
  const { ran } = host(on, {
    bridge: {
      'converge-cli': [
        json({ exit: 0, path: `${ARTIFACTS}/converge/1.json`, text: null }),
        json({ exit: 0, path: null, text: 'reference: a.jsonl\ncompared: b.jsonl\nalignment F1 0.8\n' }),
        json({ exit: 2, path: null, text: 'usage: agent-trajviz-converge ...\nerror: provide ref_file\n' }),
      ],
    },
  })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const a = await obs($, 'converge-cli -- /x/a.jsonl /x/b.jsonl --task-id t2')
  expect(bridgeCalls(ran)[0]).toEqual(['converge-cli', '--artifacts', ARTIFACTS, '--', '/x/a.jsonl', '/x/b.jsonl', '--task-id', 't2'])
  expect(a.text).toBe(`Converge report written to ${ARTIFACTS}/converge/1.json.`)
  // Without `--` the arguments go through just the same (a flag the converge CLI owns is never read as /obs's).
  const b = await obs($, 'converge-cli /x/a.jsonl /x/b.jsonl --output summary')
  expect(bridgeCalls(ran)[1]).toEqual(['converge-cli', '--artifacts', ARTIFACTS, '--', '/x/a.jsonl', '/x/b.jsonl', '--output', 'summary'])
  expect(b.text).toBe('reference: a.jsonl\ncompared: b.jsonl\nalignment F1 0.8')
  const c = await obs($, 'converge-cli -- --batch')
  expect(c.text).toBe('usage: agent-trajviz-converge ...\nerror: provide ref_file\nagent-trajviz-converge exited with 2.')
  expect((await obs($, 'converge-cli')).text).toBe('Usage: /obs converge-cli [--] <agent-trajviz-converge arguments>  (e.g. --batch MANIFEST --output summary; --help lists them)')
  expect((await obs($, 'converge-cli --')).text).toContain('Usage: /obs converge-cli')
  expect(bridgeCalls(ran)).toHaveLength(3)
})

test('labels passes the sidecars and prints the label views', async ($, on) => {
  const text = 'Labels: 12 steps · 11 classified (92%) · 1 unknown · model haiku · taxonomy 1\n\n| phase | steps | time |\n|---|---|---|\n| build | 8 | 4m05s |'
  const { ran } = host(on, { bridge: { labels: json({ summary: {}, phases: {}, text }) } })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  expect((await obs($, 'labels /l/abc.json /l/def.json')).text).toBe(text)
  expect(bridgeCalls(ran)[0]).toEqual(['labels', '--sidecar', '/l/abc.json', '--other', '/l/def.json'])
  await obs($, 'labels /l/abc.json')
  expect(bridgeCalls(ran)[1]).toEqual(['labels', '--sidecar', '/l/abc.json'])
  const usage = 'Usage: /obs labels <sidecar> [<other sidecar>]  (a labels.json /obs label wrote)'
  expect((await obs($, 'labels')).text).toBe(usage)
  expect((await obs($, 'labels a b c')).text).toBe(usage)
  expect(bridgeCalls(ran)).toHaveLength(2)
})

test('attribution passes its flags and prints its availability or verdict', async ($, on) => {
  const verdict = {
    available: true,
    reason: null,
    mode: 'reference',
    agent: 'claude-code',
    instance_id: 'django__django-11477',
    blame_status: 'blamed',
    primary: { capability: 'localization', error_type: 'wrong_file' },
    scorecard: [
      { capability: 'localization', assessed: true, blamed: true, weight: 0.7, tier: 'deductive', top_error: 'wrong_file' },
      { capability: 'editing', assessed: true, blamed: false, weight: 0, tier: null, top_error: null },
      { capability: 'testing', assessed: false, blamed: false, weight: 0, tier: null, top_error: null },
    ],
    faults: [
      { capability: 'localization', error_type: 'wrong_file', confidence: 0.9 },
      { capability: 'planning', error_type: 'skipped_repro', confidence: 'low' },
    ],
    arbiter: null,
    used_judge: false,
    task: null,
    notes: ['DECAF judge verdict ignored: its recorded provenance differs'],
  }
  const { ran } = host(on, {
    bridge: {
      attribution: [
        json({ available: false, reason: 'DECAF is not installed: pip install decaf-attribution', mode: 'gold_free', scorecard: [], faults: [], notes: [] }),
        json(verdict),
        json({ empty: true, reason: 'no_steps' }),
      ],
    },
  })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const a = await obs($, 'attribution --reference-root /refs --agent a --instance i')
  expect(bridgeCalls(ran)[0]).toEqual(['attribution', ...SESSION_ARGV, '--reference-root', '/refs', '--agent', 'a', '--instance', 'i'])
  expect(a.text).toBe('Attribution is not available here.')
  const b = await obs($, 'attribution')
  expect(bridgeCalls(ran)[1]).toEqual(['attribution', ...SESSION_ARGV])
  expect(b.text).toBe(
    [
      'Attribution (reference) of claude-code/django__django-11477: blamed',
      'Primary failure: localization · wrong_file',
      'Blamed capabilities: localization (weight 0.7, deductive)',
      'Faults:',
      '- localization · wrong_file (confidence 0.9)',
      '- planning · skipped_repro (confidence low)',
      'Notes:',
      '- attribution judge verdict ignored: its recorded provenance differs',
    ].join('\n'),
  )
  expect((await obs($, 'attribution')).text).toBe('Nothing to analyze yet: this session has no assistant steps. Run /obs after the first turn.')
  expect((await obs($, 'attribution --agent')).text).toBe('Usage: /obs attribution [--reference-root DIR] [--agent A] [--instance I]')
  expect(bridgeCalls(ran)).toHaveLength(3)
})

test('attributionText with nothing blamed and no verdict', () => {
  const quiet = { available: true, reason: null, mode: 'gold_free', agent: null, instance_id: null, blame_status: null, primary: null, scorecard: [], faults: [], notes: [] }
  expect(attributionText(quiet)).toBe('Attribution (gold_free): no verdict\nBlamed capabilities: none')
  expect(attributionText({ ...quiet, available: false, reason: null })).toBe('Attribution is not available here.')
  expect(attributionText({ ...quiet, available: false, reason: 'No gold reference was found.' })).toBe('No gold reference was found.')
  expect(attributionText({ ...quiet, available: false, reason: 'Try pip install decaf-attribution.' }))
    .toBe('Attribution is not available here.')
  const reason = 'No gold reference under /tmp/DECAF/data; check the reference root.'
  expect(attributionText({ ...quiet, available: false, reason })).toBe(reason)
  expect(attributionText({ ...quiet, notes: ['DECAF cache /tmp/DECAF/data; DECAF.json; DECAF_AVAILABLE'] }))
    .toBe('Attribution (gold_free): no verdict\nBlamed capabilities: none\nNotes:\n- attribution cache /tmp/DECAF/data; DECAF.json; DECAF_AVAILABLE')
})

test('collect passes its arguments through and prints what the tool wrote', async ($, on) => {
  const { ran } = host(on, {
    bridge: {
      collect: [
        json({ exit: 0, stdout: 'agent-trajviz-collect 0.3.1\n', stderr: '' }),
        json({ exit: 1, stdout: '', stderr: 'error: unknown tool zed\n' }),
        json({ exit: 0, stdout: '', stderr: '' }),
      ],
    },
  })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  expect((await obs($, 'collect --version')).text).toBe('agent-trajviz-collect 0.3.1')
  expect(bridgeCalls(ran)[0]).toEqual(['collect', '--', '--version'])
  expect((await obs($, 'collect -- zed --out /tmp/z')).text).toBe('error: unknown tool zed\nagent-trajviz-collect exited with 1.')
  expect(bridgeCalls(ran)[1]).toEqual(['collect', '--', 'zed', '--out', '/tmp/z'])
  expect((await obs($, 'collect cursor')).text).toBe('agent-trajviz-collect printed nothing.')
  expect((await obs($, 'collect')).text).toBe('Usage: /obs collect <cursor|opencode|codearts> ...  (agent-trajviz-collect\'s arguments; --help lists them)')
  expect(bridgeCalls(ran)).toHaveLength(3)
})

test('passArgs is every token after the subcommand, a leading -- dropped', () => {
  expect(passArgs('collect -- cursor --out "/tmp/a b"')).toEqual(['cursor', '--out', '/tmp/a b'])
  expect(passArgs('collect cursor --out x')).toEqual(['cursor', '--out', 'x'])
  expect(passArgs('converge-cli')).toEqual([])
  expect(passArgs('converge-cli --')).toEqual([])
  expect(passArgs('converge-cli -- -- x')).toEqual(['--', 'x'])
})

test('setup --extras all builds llm and charts together, then installs app once', async ($, on) => {
  const { ran, spawned, files, statuses, toasts } = host(on, {
    extras: null,
    commands: { uv: { exitCode: 0, stdout: 'uv 0.9.0\n' }, [VENV_PY]: { exitCode: 0, stdout: '0.3.1\n' } },
  })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const out = await obs($, 'setup --extras all')
  expect(setupRequests(spawned).map(r => r.extras)).toEqual([['llm', 'charts', 'zstd'], ['llm', 'charts', 'zstd', 'llm', 'charts', 'zstd', 'app']])
  expect(JSON.parse(files[STATE_PATH] as string).extras).toEqual(['llm', 'charts', 'zstd', 'app'])
  expect(out.text).toBe(`TrajViz 0.3.1 is set up at ${VENV_PY} (source ${SOURCE}; extras llm, charts, zstd, app).`)
  // Progress went to the status line, cleared at the end; the first run's toast, no ask.
  expect(statuses[0]).toBe('obs setup: Installed')
  expect(statuses[statuses.length - 1]).toBeUndefined()
  expect(toasts).toEqual(['obs: setting up TrajViz for the first time; downloading packages'])
  expect(bridgeCalls(ran)).toEqual([])
  // Again: nothing to do, the same line.
  await obs($, 'setup --extras charts,app')
  expect(spawned).toHaveLength(2)
})

test('setup alone leaves a ready environment alone; --extras names are checked; --source rebuilds', async ($, on) => {
  const { spawned, files } = host(on, {
    extras: ['llm', 'charts', 'zstd'],
    commands: { uv: { exitCode: 0, stdout: 'uv 0.9.0\n' }, [VENV_PY]: { exitCode: 0, stdout: '0.2.2\n' } },
  })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  expect((await obs($, 'setup')).text).toBe(`TrajViz 0.3.1 is set up at ${VENV_PY} (source ${SOURCE}; extras llm, charts, zstd).`)
  expect((await obs($, 'setup --extras none')).text).toContain('extras llm, charts, zstd)')
  expect((await obs($, 'setup --extras llm')).text).toContain('extras llm, charts, zstd)')
  expect(spawned).toEqual([])
  const usage = 'Usage: /obs setup [--extras all|none|charts,app] [--source <pip requirement or checkout path>]'
  expect((await obs($, 'setup --extras bogus')).text).toBe(usage)
  expect((await obs($, 'setup --extras')).text).toBe(usage)
  expect((await obs($, 'setup --source')).text).toBe(usage)
  expect(spawned).toEqual([])
  // Another source: rebuilt from it (a checkout editable), and the config's source named, since the next /obs reads that.
  const out = await obs($, 'setup --source ~/tv')
  expect(setupRequests(spawned)[0]).toMatchObject({ source: '~/tv', installSource: `${HOME}/tv`, local: true, extras: ['llm', 'charts', 'zstd'] })
  expect(JSON.parse(files[STATE_PATH] as string).source).toBe('~/tv')
  expect(out.text).toBe(
    `TrajViz 0.2.2 is set up at ${VENV_PY} (source ~/tv; extras llm, charts, zstd).\n` +
      `trajvizSource in /config is still ${SOURCE}: the next /obs rebuilds from it unless you set it to ~/tv there.`,
  )
})

test('manual setup and a later automatic rebuild restore the enabled summary after progress', { options: { statusline: true } }, async ($, on) => {
  const h = host(on, {
    bridge: { snapshot: json(SNAPSHOT), json: json({ path: '/tmp/views', bytes: 1, files: ['index.json'] }) },
    commands: { uv: { exitCode: 0, stdout: 'uv 0.9.0\n' }, [VENV_PY]: { exitCode: 0, stdout: '0.3.1\n' } },
  })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  await obs($, '')
  await h.clock.settle()
  const line = h.statuses.at(-1)
  expect(line).toContain('196 tools')
  await obs($, 'setup --source ~/tv')
  await h.clock.settle()
  expect(h.statuses.at(-1)).toBe(line)
  h.statuses.length = 0
  // The source is now different from config, so a regular export rebuilds it.
  await obs($, 'json')
  await h.clock.settle()
  expect(h.statuses[0]).toBe('obs setup: Installed')
  expect(h.statuses.at(-1)).toBe(line)
  expect(setupRequests(h.spawned)).toHaveLength(2)
})

test('setup without uv or Python 3.11+ is the row naming what was looked for', async ($, on) => {
  const { spawned } = host(on, { extras: null, commands: { python3: { exitCode: 0, stdout: 'Python 3.9.6\n' } } })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const out = await obs($, 'setup')
  expect(out.text).toContain('No Python 3.11+ found')
  expect(out.text).toContain('python3 (3.9.6, too old)')
  expect(spawned).toEqual([])
})

test('parseExtras', () => {
  expect(parseExtras(undefined)).toEqual([])
  expect(parseExtras('all')).toEqual(['charts', 'app'])
  expect(parseExtras('none')).toEqual([])
  expect(parseExtras('charts,llm')).toEqual(['charts'])
  expect(parseExtras('app, charts ,app')).toEqual(['app', 'charts'])
  expect(parseExtras('bogus')).toBeUndefined()
  expect(parseExtras(true)).toBeUndefined()
  expect(parseExtras('')).toBeUndefined()
})

const DOCTOR: DoctorDoc = {
  python: '3.12.4',
  trajviz: { version: '0.3.1', path: `${CACHE}/venv/lib/python3.12/site-packages/agent_trajviz` },
  plugins: { loaded: [], error: null },
  extras: { charts: true, llm: true, app: false },
  attribution: { available: false, reason: "No module named 'decaf'" },
  llm_env: { available: true, ready: false, missing: ['ANALYZE_MODEL', 'ANALYZE_API_KEY'], reason: null },
  reference_root: null,
}

test('doctor is a table of the bridge\'s doctor and the environment', async ($, on) => {
  const { ran } = host(on, { extras: ['llm', 'charts', 'zstd'], bridge: { doctor: json(DOCTOR) } })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const out = await obs($, 'doctor')
  expect(bridgeCalls(ran)).toEqual([['doctor', '--cwd', CWD]])
  const lines = out.text!.split('\n')
  expect(lines[0]).toBe('obs doctor')
  // `createdAt` (NOW) in local time, as the sessions table prints start times.
  expect(lines[1]).toMatch(new RegExp(`^  environment {3}${VENV_PY} \\(built from ${SOURCE.replace(/[.<>]/g, '\\$&')} on 2025-10-2[23] \\d\\d:\\d\\d\\)$`))
  expect(lines[2]).toBe('  python        3.12.4')
  expect(lines[3]).toMatch(/^ {2}trajviz {7}0\.3\.1 at \//)
  expect(lines[4]).toBe('  plugins       none')
  expect(lines[5]).toBe('  extras        llm, charts installed; app not installed (/obs setup --extras app)')
  expect(lines[6]).toBe('  attribution   not available: Attribution is not available here.')
  expect(lines[7]).toBe('  llm env       not ready: ANALYZE_MODEL, ANALYZE_API_KEY unset (names only)')
  expect(lines[8]).toBe('  reference     TRAJVIZ_REFERENCE_ROOT unset')
  expect(lines[9]).toBe('  llm mode      session (analysisModel sonnet, labelModel haiku)')
  expect(lines).toHaveLength(10)
})

test('doctorText: every extra, plugins, a ready llm env, env mode and a reference root', () => {
  const doc: DoctorDoc = {
    ...DOCTOR,
    plugins: { loaded: ['my_plugin', 'other'], error: null },
    extras: { charts: true, llm: true, app: true },
    attribution: { available: true, reason: null },
    llm_env: { available: true, ready: true, missing: [], reason: null },
    reference_root: '/refs',
  }
  const state = { python: VENV_PY, trajviz: '0.3.1', extras: ['llm', 'charts', 'zstd', 'app'], source: SOURCE, createdAt: NOW }
  const lines = doctorText(doc, state, { llm: 'env', analysisModel: 'session', labelModel: 'haiku' }).split('\n')
  expect(lines[4]).toBe('  plugins       my_plugin, other')
  expect(lines[5]).toBe('  extras        llm, charts, app installed')
  expect(lines[6]).toBe('  attribution   available')
  expect(lines[7]).toBe('  llm env       ready')
  expect(lines[8]).toBe('  reference     /refs')
  expect(lines[9]).toBe("  llm mode      env (TrajViz's own client from ANALYZE_*/LABEL_*)")
  const broken = doctorText({ ...doc, plugins: { loaded: [], error: 'my_plugin: ImportError: no' }, llm_env: { available: false, ready: false, missing: [], reason: 'no llm extra' } }, state, {}).split('\n')
  expect(broken[4]).toBe('  plugins       error: my_plugin: ImportError: no')
  expect(broken[7]).toBe('  llm env       no llm extra')
  expect(broken[9]).toBe('  llm mode      session (analysisModel sonnet, labelModel haiku)')
  const trimmed = doctorText(doc, state, { analysisModel: '  opus  ', labelModel: ' \t ' }).split('\n')
  expect(trimmed[9]).toBe('  llm mode      session (analysisModel opus, labelModel haiku)')
  const spaced = doctorText(doc, state, { analysisModel: '  ', labelModel: '  haiku  ' }).split('\n')
  expect(spaced[9]).toBe('  llm mode      session (analysisModel sonnet, labelModel haiku)')
})

test('doctor without an environment is the setup line, and a broken one its error under the environment', async ($, on) => {
  const { ran } = host(on, { extras: null })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  expect((await obs($, 'doctor')).text).toBe('TrajViz is not set up yet: run /obs setup, or /obs, which sets it up first.')
  expect(bridgeCalls(ran)).toEqual([])
})

test('doctor with a broken environment keeps the environment lines and ends with the error', async ($, on) => {
  host(on, { bridge: { doctor: envelope('load_error', "agent_trajviz is missing or broken: No module named 'agent_trajviz'") } })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const lines = (await obs($, 'doctor')).text!.split('\n')
  expect(lines[0]).toBe('obs doctor')
  expect(lines[1]).toContain('environment')
  expect(lines[2]).toBe("  error         agent_trajviz is missing or broken: No module named 'agent_trajviz'")
  expect(lines).toHaveLength(3)
})

test('setupText', () => {
  expect(setupText({ python: VENV_PY, trajviz: null, extras: ['llm'], source: SOURCE, createdAt: 1 }, SOURCE)).toBe(
    `TrajViz (version unknown) is set up at ${VENV_PY} (source ${SOURCE}; extras llm).`,
  )
})

const setupRequests = (spawned: string[][]) => spawned.map(s => s).filter(a => a.some(v => v.endsWith('/bridge/bootstrap.py'))).map(a => JSON.parse(a[a.indexOf('--request') + 1] as string))
