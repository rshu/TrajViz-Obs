import { expect, mock, test } from 'claude-code/testing'
import type { EngineInterface, On, StateAddress } from 'claude-code'
import type { EmptySnapshot } from '../types'
import { BridgeError } from './lib/bridge'
import { BUSY_STALE_MS, captureSnapshot, mintId, parseSnapshotId, provenanceLine, storeSnapshot, takeSnapshot } from './lib/snapshot'
import { claimRefresh, releaseRefresh } from './lib/state'
import { rememberTranscript } from './lib/session'
import { errorRow } from './commands/overview'
import { maybeOpenPane } from './commands/pane'
import { SNAPSHOT } from './testdata/snapshot'

const HOME = '/Users/me'
const CWD = '/Users/me/p'
const CACHE = `${HOME}/.cache/trajviz-obs`
const VENV_PY = `${CACHE}/envs/0123456789abcdef01234567/0123456789abcdef0123456789abcdef/bin/python`
const STATE_PATH = `${CACHE}/state.json`
const STATE_JSON = JSON.stringify({ revision: 4, python: VENV_PY, trajviz: '0.2.1', extras: ['llm', 'charts'], source: 'agent-trajviz>=0.2.0,<0.3', createdAt: 1 })
const DERIVED = `${HOME}/.claude/projects/-Users-me-p/abc.jsonl`
const ARTIFACTS = `${CACHE}/sessions/abc`
const USAGE = { startedAt: 0, context: { window: 1_000_000, percent: 12 }, rateLimits: [] }
/** The host's clock while a kit test runs. */
const NOW = 1_761_150_000_000
/** The refresh value while an analysis runs, claimed at `busySince`. */
const HELD = { lastRunAt: 0, lastDurationMs: 0, lastSize: 0, lastMtimeMs: 0, busy: true, busySince: NOW - 1_000 }

const EMPTY: EmptySnapshot = { schema_version: 1, empty: true, reason: 'no_steps', transcript: { path: DERIVED } }

type RunAnswer = { exitCode: number; stdout: string; stderr?: string }

/** `process.run` answers for the bridge by subcommand; anything else cannot start. */
const bridgeOf = (by: Record<string, RunAnswer>) => (argv: readonly string[]): RunAnswer | undefined =>
  argv[1] === '-I' && argv[2]?.endsWith('/bridge/trajviz_obs_cli.py') ? by[argv[3] ?? ''] : undefined

type World = {
  /** The bridge's answers by subcommand. */
  bridge?: Record<string, RunAnswer>
  /** Whether state.json exists (the environment is built). */
  env?: boolean
  /** Whether the derived transcript path exists. */
  transcript?: boolean
}

/** The host beneath `/obs`: the kit's `on`, env, files, session, usage and canned bridge answers; every process.run recorded. */
function host(on: On, world: World = {}): { ran: string[][] } {
  const ran: string[][] = []
  const answers = bridgeOf(world.bridge ?? {})
  on('command.register', () => ({ value: { command: 'obs' } }))
  on('tool.register', () => ({ value: { tool: 'mcp__trajviz-obs__obs' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: 'abc' }))
  on('session.cwd', () => ({ value: CWD }))
  on('session.usage', () => ({ value: USAGE }))
  mock.env(on, { HOME })
  mock.clock(on, { now: NOW })
  on('fs.read', (_, e) => (e.path === STATE_PATH && world.env !== false ? { value: STATE_JSON } : { deny: `ENOENT: ${e.path}` }))
  on('fs.exists', (_, e) => ({ value: e.path === VENV_PY || (e.path === DERIVED && world.transcript !== false) }))
  on('ui.toast', () => ({ value: undefined }))
  on('ui.status', () => ({ value: undefined }))
  on('process.run', (_, e) => {
    ran.push([...e.argv])
    const a = answers(e.argv)
    if (!a) return { deny: `cannot start ${e.argv[0]}` }
    return { value: { exitCode: a.exitCode, stdout: a.stdout, stderr: a.stderr ?? '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  return { ran }
}

const json = (doc: unknown): RunAnswer => ({ exitCode: 0, stdout: `${JSON.stringify(doc)}\n` })

const obs = ($: { command: { run: (args: never) => Promise<{ text?: string; context?: readonly string[] }> } }, args: string) =>
  $.command.run({ command: 'obs', args, origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 80 } } as never)

test('/obs returns provenance + text and a context note', async ($, on) => {
  const { ran } = host(on, { bridge: { snapshot: json(SNAPSHOT) } })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const out = await obs($, '')
  const lines = out.text!.split('\n')
  expect(lines[0]).toMatch(/^obs snapshot 1 · \d\d:\d\d:\d\d · 311 assistant steps · window 1,000,000/)
  expect(lines[0]).toContain('skipped sub-agent log agent-9f3c.jsonl')
  expect(lines.slice(1).join('\n')).toBe(SNAPSHOT.text)
  expect(out.context).toHaveLength(1)
  expect(out.context![0]).toContain('mcp__trajviz-obs__obs')
  expect(out.context![0]).toContain(ARTIFACTS)
  // One bridge call through the managed interpreter and the plugin's launcher, with the engine's window, the artifacts folder and the kind.
  expect(ran).toHaveLength(1)
  expect(ran[0]!.slice(0, 2)).toEqual([VENV_PY, '-I'])
  expect(ran[0]![2]).toMatch(/\/bridge\/trajviz_obs_cli\.py$/)
  expect(ran[0]!.slice(3)).toEqual(['snapshot', '--transcript', DERIVED, '--window', '1000000', '--artifacts', ARTIFACTS, '--kind', 'current'])
  // A second run continues the ids.
  expect((await obs($, 'overview')).text!.split('\n')[0]).toMatch(/^obs snapshot 2 · /)
})

test('empty session row text', async ($, on) => {
  host(on, { bridge: { snapshot: json(EMPTY) } })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const out = await obs($, '')
  expect(out.text).toBe('Nothing to analyze yet: this session has no assistant steps. Run /obs after the first turn.')
  expect(out.context).toBeUndefined()
})

test('busy guard', async ($, on) => {
  const { ran } = host(on, { bridge: { snapshot: json(SNAPSHOT) } })
  on('state.get', { plugin: 'trajviz-obs', key: 'refresh' }, () => ({ value: { value: HELD, version: 1 } }))
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  expect((await obs($, '')).text).toBe('An analysis is already running; try again in a moment.')
  expect(ran).toEqual([])
})

test('a stale busy is reclaimed', async ($, on) => {
  const { ran } = host(on, { bridge: { snapshot: json(SNAPSHOT) } })
  // The lock a cut dispatch left (Esc during setup, a reload): `busy` since before the longest run could last.
  // Answered once at the kit store's version, so the claim's compare-and-set lands there; later reads see the store.
  let seeded = false
  on('state.get', { plugin: 'trajviz-obs', key: 'refresh' }, (_, e, next) => {
    if (seeded) return next(e)
    seeded = true
    return { value: { value: { ...HELD, busySince: NOW - BUSY_STALE_MS }, version: 0 } }
  })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  expect((await obs($, '')).text!.split('\n')[0]).toMatch(/^obs snapshot 1 · /)
  expect(ran.map(r => r[3])).toEqual(['snapshot'])
  // Released at the end: the next run claims it as usual.
  expect((await obs($, '')).text!.split('\n')[0]).toMatch(/^obs snapshot 2 · /)
})

test('no transcript on the current session: the spec row, not the bridge message', async ($, on) => {
  // The current session has no transcript anywhere: the spec's row, not the bridge's message.
  const { ran } = host(on, { transcript: false, bridge: { resolve: { exitCode: 1, stdout: '{"error": {"code": "not_found", "message": "no session abc"}}' } } })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  expect((await obs($, '')).text).toBe('No transcript for this session yet. Send one prompt, then run /obs.')
  expect(ran.map(r => r[3])).toEqual(['resolve'])
})

test('load error verbatim with its path', async ($, on) => {
  host(on, { bridge: { snapshot: { exitCode: 1, stdout: `{"error": {"code": "load_error", "message": "line 12: bad record", "path": "${DERIVED}"}}` } } })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  expect((await obs($, '')).text).toBe(`line 12: bad record (${DERIVED})`)
})

test('--session resolves through the bridge and snapshots as kind other', async ($, on) => {
  const { ran } = host(on, {
    bridge: {
      resolve: json({ path: '/p/bbb333.jsonl', session_id: 'bbb333', project_dir: '/p', kind: 'file' }),
      snapshot: json({ ...SNAPSHOT, transcript: { ...SNAPSHOT.transcript, path: '/p/bbb333.jsonl', session_id: 'bbb333' } }),
    },
  })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const out = await obs($, '--session bbb')
  expect(out.text!.split('\n')[0]).toMatch(/^obs snapshot 1 · /)
  expect(out.context![0]).toContain(`${CACHE}/sessions/bbb333`)
  expect(ran.map(r => r.slice(3))).toEqual([
    ['resolve', '--cwd', CWD, '--config-dir', `${HOME}/.claude`, '--session', 'bbb'],
    ['snapshot', '--transcript', '/p/bbb333.jsonl', '--artifacts', `${CACHE}/sessions/bbb333`, '--kind', 'other'],
  ])
})

test('ambiguous --session lists the candidates with their start times', async ($, on) => {
  host(on, {
    bridge: {
      resolve: {
        exitCode: 1,
        stdout:
          '{"error": {"code": "ambiguous", "message": "2 sessions start with aaa", "candidates": [{"id": "aaa111", "started": "2026-10-22T10:00:00Z", "size": 10}, {"id": "aaa222", "started": null, "size": 20}]}}',
      },
    },
  })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  expect((await obs($, '--session aaa')).text).toBe('2 sessions start with aaa: aaa111 (2026-10-22T10:00:00Z), aaa222 (?)')
})

/** Withholds the `process` noun beneath the plugin, as a host without it does. */
const withholdProcess = (on: On) =>
  on('engine.create', async (_, e, next) => {
    const { process: _process, ...rest } = (await next(e)) as Record<string, unknown>
    return rest as never
  })

test('a host that withholds process answers one line (environment built)', async ($, on) => {
  withholdProcess(on)
  host(on, { bridge: { snapshot: json(SNAPSHOT) } })
  await $.session.start({ cwd: CWD, surface: 'desktop', isInteractive: true })
  expect((await obs($, '')).text).toBe("This session's host runs no commands, so /obs cannot compute here; run it from a terminal session.")
})

test('a host that withholds process answers one line (no environment yet)', async ($, on) => {
  withholdProcess(on)
  host(on, { env: false })
  await $.session.start({ cwd: CWD, surface: 'desktop', isInteractive: true })
  expect((await obs($, '')).text).toBe("This session's host runs no commands, so /obs cannot compute here; run it from a terminal session.")
})

test('no python and no uv: the NoPythonError message is the row', async ($, on) => {
  const { ran } = host(on, { env: false })
  on('fs.write', () => ({ value: undefined }))
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const out = await obs($, '')
  expect(out.text).toContain('No Python 3.11+ found to set TrajViz up with.')
  expect(out.text).toContain('uv (not found)')
  expect(ran.every(r => r[1] === '--version')).toBe(true)
})

// --- the library, over a fake `$` with state in memory ---

type Fake = { $: EngineInterface; state: Map<string, { value: unknown; version: number }>; ran: string[][]; clock: { now: number }; opens: unknown[] }

const stateKey = (ref: StateAddress): string => (ref.id === undefined ? ref.key : `${ref.key}/${ref.id}`)

/** A `$` over an in-memory host: env, files, state, session, usage and canned process answers. */
function fake(world: { state?: Record<string, unknown>; bridge?: Record<string, RunAnswer>; now?: number } = {}): Fake {
  const state = new Map<string, { value: unknown; version: number }>()
  const clock = { now: world.now ?? 0 }
  for (const [k, v] of Object.entries(world.state ?? {})) state.set(k, { value: v, version: 1 })
  const ran: string[][] = []
  const opens: unknown[] = []
  const answers = bridgeOf(world.bridge ?? {})
  const $ = {
    plugin: { name: 'trajviz-obs', root: '/plugin' },
    env: { get: async (name: string) => ({ HOME })[name] },
    fs: {
      read: async (path: string) => {
        if (path === STATE_PATH) return STATE_JSON
        throw new Error(`ENOENT: ${path}`)
      },
      exists: async (path: string) => path === VENV_PY || path === DERIVED,
    },
    session: { id: async () => 'abc', cwd: async () => CWD, usage: async () => USAGE },
    clock: { now: async () => clock.now },
    ui: {
      status: async () => undefined,
      toast: async () => undefined,
      open: async (args: unknown) => {
        opens.push(args)
        return { isPlaced: true }
      },
    },
    state: {
      get: async (ref: StateAddress) => {
        const e = state.get(stateKey(ref))
        return { value: e?.value, version: e?.version ?? 0 }
      },
      set: async (ref: StateAddress, value: unknown, options?: { ifVersion?: number }) => {
        const k = stateKey(ref)
        const current = state.get(k)?.version ?? 0
        if (options?.ifVersion !== undefined && options.ifVersion !== current) return { isSet: false, version: current }
        state.set(k, { value, version: current + 1 })
        return { isSet: true, version: current + 1 }
      },
    },
    process: {
      run: async (argv: readonly string[]) => {
        ran.push([...argv])
        const a = answers(argv)
        if (!a) throw new Error(`cannot start ${argv[0]}`)
        return { exitCode: a.exitCode, stdout: a.stdout, stderr: a.stderr ?? '', isStdoutTruncated: false, isStderrTruncated: false }
      },
    },
  }
  return { $: $ as unknown as EngineInterface, state, ran, clock, opens }
}

const CTX = { args: '', parsed: { sub: 'overview', positional: [], flags: {}, passthrough: [] }, options: {} }

test('ids continue and prune to five', async () => {
  const w = fake()
  const ids: string[] = []
  for (let i = 0; i < 6; i++) ids.push(await storeSnapshot(w.$, { ...SNAPSHOT, duration_ms: i }))
  expect(ids).toEqual(['1', '2', '3', '4', '5', '6'])
  expect(w.state.get('snapshotIds')?.value).toEqual(['2', '3', '4', '5', '6'])
  expect(w.state.get('snapshot/1')?.value).toBe(null)
  expect((w.state.get('snapshot/6')?.value as { duration_ms: number }).duration_ms).toBe(5)
  expect((w.state.get('latest')?.value as { duration_ms: number }).duration_ms).toBe(5)
  // Ids continue from the largest kept, never reuse a pruned one.
  expect(mintId([])).toBe('1')
  expect(mintId(['2', '3', '4', '5', '6'])).toBe('7')
  expect(mintId(['9', '10'])).toBe('11')
})

test('concurrent snapshot publications keep distinct immutable cards and prune the oldest members', async () => {
  const w = fake()
  const ids = await Promise.all(Array.from({ length: 8 }, (_, i) => storeSnapshot(w.$, { ...SNAPSHOT, duration_ms: i })))
  expect(ids).toEqual(['1', '2', '3', '4', '5', '6', '7', '8'])
  expect(w.state.get('snapshotIds')?.value).toEqual(['4', '5', '6', '7', '8'])
  for (let i = 0; i < 8; i++) {
    const snap = w.state.get(`snapshot/${i + 1}`)?.value
    if (i < 3) expect(snap).toBeNull()
    else expect(snap).toMatchObject({ duration_ms: i })
  }
  expect(w.state.get('latest')?.value).toMatchObject({ duration_ms: 7 })
})

test('the refresh claim includes publication so a second analysis cannot overtake a slow state write', async () => {
  const w = fake({ bridge: { snapshot: json(SNAPSHOT) }, now: NOW })
  const set = w.$.state.set.bind(w.$.state)
  let unblock: () => void = () => {}
  const blocked = new Promise<void>(resolve => { unblock = resolve })
  let storageStarted: () => void = () => {}
  const storage = new Promise<void>(resolve => { storageStarted = resolve })
  w.$.state.set = (async (ref: StateAddress, value: unknown, options?: { ifVersion?: number }) => {
    if (ref.key === 'snapshot') { storageStarted(); await blocked }
    return set(ref as never, value as never, options)
  }) as typeof w.$.state.set
  const first = captureSnapshot(w.$, {})
  await storage
  await expect(captureSnapshot(w.$, {})).rejects.toMatchObject({ code: 'busy' })
  expect(w.ran.filter(argv => argv[3] === 'snapshot')).toHaveLength(1)
  unblock()
  await expect(first).resolves.toMatchObject({ id: '1' })
  expect(w.state.get('refresh')?.value).toMatchObject({ busy: false })
})

test('/clear while context usage is resolving cancels the old transcript before any snapshot child starts', async () => {
  const w = fake({ state: { session: { transcriptPath: DERIVED, sessionId: 'abc', selected: null } }, bridge: { snapshot: json(SNAPSHOT) }, now: NOW })
  let release: () => void = () => {}
  const gate = new Promise<void>(resolve => { release = resolve })
  let usageStarted: () => void = () => {}
  const usage = new Promise<void>(resolve => { usageStarted = resolve })
  w.$.session.usage = (async () => { usageStarted(); await gate; return USAGE }) as typeof w.$.session.usage
  const pending = captureSnapshot(w.$, {})
  await usage
  await rememberTranscript(w.$, { transcript_path: '/new/def.jsonl', session_id: 'def' })
  release()
  await expect(pending).rejects.toMatchObject({ code: 'session_changed' })
  expect(w.ran).toEqual([])
  expect(w.state.get('latest')?.value).toBeNull()
  expect(w.state.get('refresh')?.value).toMatchObject({ lastRunAt: 0, busy: false })
})

test('provenance line and its id', () => {
  const at = new Date(2026, 9, 22, 16, 40, 12)
  const line = provenanceLine('12', { ...SNAPSHOT, trajviz: { ...SNAPSHOT.trajviz, warnings: [] } }, at)
  expect(line).toBe('obs snapshot 12 · 16:40:12 · 311 assistant steps · window 1,000,000')
  expect(provenanceLine('3', SNAPSHOT, at)).toBe(`${line.replace(' 12 ', ' 3 ')} · warnings: skipped sub-agent log agent-9f3c.jsonl: unterminated last record`)
  expect(provenanceLine('4', EMPTY, new Date(2026, 0, 1, 9, 5, 7))).toBe('obs snapshot 4 · 09:05:07 · 0 assistant steps · window ?')
  expect(parseSnapshotId(`${line}\nHealth: ok`)).toBe('12')
  expect(parseSnapshotId('Nothing to analyze yet')).toBe(null)
  expect(parseSnapshotId('')).toBe(null)
})

test('takeSnapshot releases the busy flag after a failure and refuses while one is held', async () => {
  const w = fake({ bridge: { snapshot: { exitCode: 1, stdout: '{"error": {"code": "load_error", "message": "bad", "path": "/t"}}' } } })
  await expect(takeSnapshot(w.$, {})).rejects.toBeInstanceOf(BridgeError)
  expect(w.state.get('refresh')?.value).toMatchObject({ busy: false, busySince: 0 })
  const busy = fake({ state: { refresh: HELD }, now: NOW })
  await expect(takeSnapshot(busy.$, {})).rejects.toMatchObject({ code: 'busy' })
  expect(busy.ran).toEqual([])
})

test('takeSnapshot reclaims a busy held past the longest run and refuses one held less', async () => {
  // A hold that outlived setup + resolve + snapshot was left by a cut dispatch: the next caller takes it over.
  const stale = fake({ state: { refresh: HELD }, bridge: { snapshot: json(SNAPSHOT) }, now: HELD.busySince + BUSY_STALE_MS })
  await expect(takeSnapshot(stale.$, {})).resolves.toMatchObject({ counts: { assistant_steps: 311 } })
  expect(stale.ran.map(r => r[3])).toEqual(['snapshot'])
  expect(stale.state.get('refresh')?.value).toMatchObject({ busy: false, busySince: 0 })
  // One millisecond short of that: a run under way.
  const fresh = fake({ state: { refresh: HELD }, bridge: { snapshot: json(SNAPSHOT) }, now: HELD.busySince + BUSY_STALE_MS - 1 })
  await expect(takeSnapshot(fresh.$, {})).rejects.toMatchObject({ code: 'busy' })
  expect(fresh.ran).toEqual([])
})

test('claimRefresh stamps the claim and a late release by a reclaimed holder leaves the lock to its new holder', async () => {
  const w = fake({ now: 10 })
  const a = await claimRefresh(w.$, 100)
  expect(a).toBe(10)
  expect(w.state.get('refresh')?.value).toMatchObject({ busy: true, busySince: 10 })
  expect(await claimRefresh(w.$, 100)).toBe(null)
  w.clock.now = 110
  const b = await claimRefresh(w.$, 100)
  expect(b).toBe(110)
  // The first run was not cut after all and its `finally` lands now: the lock is the second run's, so it stays.
  await releaseRefresh(w.$, a!)
  expect(w.state.get('refresh')?.value).toMatchObject({ busy: true, busySince: 110 })
  await releaseRefresh(w.$, b!)
  expect(w.state.get('refresh')?.value).toMatchObject({ busy: false, busySince: 0 })
})

test('errorRow maps a withheld process, a setup failure and a plain bridge error', () => {
  expect(errorRow(new Error('$.process.run: withheld by the host'))).toBe("This session's host runs no commands, so /obs cannot compute here; run it from a terminal session.")
  expect(errorRow(new Error('obs setup: uv exited with 1: no network'))).toBe('obs setup: uv exited with 1: no network')
  expect(errorRow(new BridgeError('run_failed', 'obs snapshot: the bridge could not run: spawn ENOENT'))).toBe('obs snapshot: the bridge could not run: spawn ENOENT')
  expect(errorRow(new BridgeError('not_found', 'no session zzz'), 'zzz')).toBe('no session zzz')
})

test('the pane opens unasked only beside a fullscreen transcript of 140 columns or more', async () => {
  const w = fake()
  expect(await maybeOpenPane(w.$, { ...CTX, presentation: { isFullscreen: false, columns: 160 } })).toBe(false)
  expect(await maybeOpenPane(w.$, { ...CTX, presentation: { isFullscreen: true, columns: 139 } })).toBe(false)
  expect(await maybeOpenPane(w.$, { ...CTX })).toBe(false)
  expect(w.opens).toEqual([])
  expect(await maybeOpenPane(w.$, { ...CTX, presentation: { isFullscreen: true, columns: 140 } })).toBe(true)
  expect(w.opens).toEqual([{ id: 'obs', title: 'obs', columns: 56 }])
})
