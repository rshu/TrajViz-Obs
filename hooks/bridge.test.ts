import { expect, test } from 'claude-code/testing'
import type { EngineInterface, ProcessSpawnChunk, StateAddress } from 'claude-code'
import { BridgeError, MAX_BRIDGE_CHARS, runBridge, spawnBridge } from './lib/bridge'
import { resolveTranscript, selectSession } from './lib/session'
import { flags, latest, paneTab, refresh, session, snapshotIds, snapshotRef } from './lib/state'

type RunAnswer = { exitCode: number; stdout: string; stderr?: string; isStdoutTruncated?: boolean }
/** What a spawned child writes, piece by piece, and how it ends. */
type SpawnAnswer = { chunks: ProcessSpawnChunk[]; code: number | null; signal?: string | null }

type FakeWorld = {
  env?: Record<string, string>
  files?: Record<string, string>
  exists?: string[]
  sessionId?: string
  cwd?: string
  /** State as the engine would hold it, keyed `key` or `key/id`. */
  state?: Record<string, unknown>
  /** `process.run` by argv; undefined means the command cannot start (the call rejects). */
  run?: (argv: readonly string[]) => RunAnswer | undefined
  /** `process.spawn` by argv; undefined means the child cannot start (the first pull rejects). */
  spawn?: (argv: readonly string[]) => SpawnAnswer | undefined
}

type Fake = {
  $: EngineInterface
  ran: { argv: string[]; init: unknown }[]
  spawned: { argv: string[]; cwd: string | undefined }[]
  existsAsked: string[]
  state: Map<string, { value: unknown; version: number }>
}

const HOME = '/Users/me'
const CWD = '/Users/me/p'
const VENV_PY = `${HOME}/.cache/trajviz-obs/venv/bin/python`
const STATE_JSON = JSON.stringify({ revision: 4, python: VENV_PY, trajviz: '0.2.1', extras: ['llm'], source: 'agent-trajviz>=0.2.0,<0.3', createdAt: 1 })
const LAUNCHER = '/plugin/bridge/trajviz_obs_cli.py'
const DERIVED = `${HOME}/.claude/projects/-Users-me-p/abc.jsonl`

const stateKey = (ref: StateAddress): string => (ref.id === undefined ? ref.key : `${ref.key}/${ref.id}`)

/** A `$` over an in-memory host: env, files, state, a session and canned process answers, every call recorded. */
function fake(world: FakeWorld): Fake {
  const ran: Fake['ran'] = []
  const spawned: Fake['spawned'] = []
  const existsAsked: string[] = []
  const state = new Map<string, { value: unknown; version: number }>()
  for (const [k, v] of Object.entries(world.state ?? {})) state.set(k, { value: v, version: 1 })
  const $ = {
    plugin: { name: 'trajviz-obs', root: '/plugin' },
    env: { get: async (name: string) => ({ HOME, ...(world.env ?? {}) })[name] },
    fs: {
      read: async (path: string) => {
        const files = world.files ?? {}
        if (path in files) return files[path]
        throw new Error(`ENOENT: ${path}`)
      },
      exists: async (path: string) => {
        existsAsked.push(path)
        return path === VENV_PY || (world.exists ?? []).includes(path)
      },
    },
    session: {
      id: async () => world.sessionId ?? 'abc',
      cwd: async () => world.cwd ?? CWD,
    },
    process: {
      run: async (argv: readonly string[], init: unknown) => {
        ran.push({ argv: [...argv], init })
        const a = world.run?.(argv)
        if (!a) throw new Error(`cannot start ${argv[0]}`)
        return { exitCode: a.exitCode, stdout: a.stdout, stderr: a.stderr ?? '', isStdoutTruncated: a.isStdoutTruncated ?? false, isStderrTruncated: false }
      },
      spawn: (request: { argv: readonly string[]; cwd?: string }) => {
        spawned.push({ argv: [...request.argv], cwd: request.cwd })
        const a = world.spawn?.(request.argv)
        return (async function* () {
          if (!a) throw new Error(`cannot start ${request.argv[0]}`)
          for (const chunk of a.chunks) yield chunk
          return { code: a.code, signal: a.signal ?? null }
        })()
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
  }
  return { $: $ as unknown as EngineInterface, ran, spawned, existsAsked, state }
}

/** `process.run` answers for the bridge: `by` maps a subcommand to its answer; anything else cannot start. */
const bridge =
  (by: Record<string, RunAnswer>) =>
  (argv: readonly string[]): RunAnswer | undefined =>
    argv[0] === VENV_PY && argv[1] === '-I' && argv[2] === LAUNCHER ? by[argv[3] ?? ''] : undefined

/** `process.spawn` answers for the bridge, as `bridge` for `process.run`. */
const spawnedBridge =
  (by: Record<string, SpawnAnswer>) =>
  (argv: readonly string[]): SpawnAnswer | undefined =>
    argv[0] === VENV_PY && argv[1] === '-I' && argv[2] === LAUNCHER ? by[argv[3] ?? ''] : undefined

const out = (text: string): ProcessSpawnChunk => ({ stream: 'stdout', text })
const err = (text: string): ProcessSpawnChunk => ({ stream: 'stderr', text })

const withEnv = (world: FakeWorld): FakeWorld => ({ files: { [`${HOME}/.cache/trajviz-obs/state.json`]: STATE_JSON }, ...world })

async function rejection(p: Promise<unknown>): Promise<BridgeError> {
  try {
    await p
  } catch (e) {
    expect(e).toBeInstanceOf(BridgeError)
    return e as BridgeError
  }
  throw new Error('expected a rejection')
}

test('runBridge parses the envelope and maps errors', async () => {
  const ok = fake(withEnv({ run: bridge({ snapshot: { exitCode: 0, stdout: '{"schema_version": 1, "text": "hi"}\n' } }) }))
  expect(await runBridge(ok.$, ['snapshot', '--transcript', '/t'], { timeoutMs: 120_000 })).toEqual({ schema_version: 1, text: 'hi' })
  expect(ok.ran).toEqual([{ argv: [VENV_PY, '-I', LAUNCHER, 'snapshot', '--transcript', '/t'], init: { cwd: CWD, timeoutMs: 120_000 } }])

  // The error envelope: code and message verbatim, the other fields as `extra`.
  const bad = fake(withEnv({ run: bridge({ snapshot: { exitCode: 1, stdout: '{"error":{"code":"load_error","message":"bad","path":"/t"}}' } }) }))
  const e = await rejection(runBridge(bad.$, ['snapshot'], { timeoutMs: 120_000 }))
  expect(e.code).toBe('load_error')
  expect(e.message).toBe('bad')
  expect(e.extra).toEqual({ path: '/t' })

  // Output cut at the host's limit is never parsed as if whole.
  const cut = fake(withEnv({ run: bridge({ view: { exitCode: 0, stdout: '{"rows": [', isStdoutTruncated: true } }) }))
  expect((await rejection(runBridge(cut.$, ['view', 'steps'], { timeoutMs: 120_000 }))).code).toBe('truncated')

  // No environment yet: nothing runs.
  const none = fake({ run: bridge({ doctor: { exitCode: 0, stdout: '{}' } }) })
  expect((await rejection(runBridge(none.$, ['doctor'], { timeoutMs: 120_000 }))).code).toBe('no_env')
  expect(none.ran).toEqual([])

  // A usage error (exit 2, nothing on stdout) names the exit and stderr's last line.
  const usage = fake(withEnv({ run: bridge({ view: { exitCode: 2, stdout: '', stderr: 'usage: trajviz_obs_cli.py view\nerror: unrecognized arguments: --nope\n' } }) }))
  const u = await rejection(runBridge(usage.$, ['view', '--nope'], { timeoutMs: 120_000 }))
  expect(u.code).toBe('exit')
  expect(u.message).toContain('exited with 2')
  expect(u.message).toContain('unrecognized arguments: --nope')

  // Exit 0 without a JSON document is reported, not returned as undefined.
  const junk = fake(withEnv({ run: bridge({ doctor: { exitCode: 0, stdout: 'not json\n' } }) }))
  expect((await rejection(runBridge(junk.$, ['doctor'], { timeoutMs: 120_000 }))).code).toBe('bad_output')
  for (const stdout of ['null', '[]', '42', '"text"']) {
    const malformed = fake(withEnv({ run: bridge({ doctor: { exitCode: 0, stdout } }) }))
    expect((await rejection(runBridge(malformed.$, ['doctor'], { timeoutMs: 120_000 }))).code).toBe('bad_output')
  }

  // The child could not start (or timed out): the host's reason is kept.
  const dead = fake(withEnv({ run: () => undefined }))
  const d = await rejection(runBridge(dead.$, ['doctor'], { timeoutMs: 120_000 }))
  expect(d.code).toBe('run_failed')
  expect(d.message).toContain(`cannot start ${VENV_PY}`)
})

test('spawned bridge output is bounded and an oversized child is closed', async () => {
  let closed = false
  const w = fake(withEnv({ spawn: spawnedBridge({ label: { chunks: [out('x'.repeat(MAX_BRIDGE_CHARS)), out('x')], code: 0 } }) }))
  const spawn = w.$.process.spawn
  const wrapped = { ...w.$, process: { ...w.$.process, spawn: (request: Parameters<typeof spawn>[0]) => (async function* () {
    try { return yield* spawn(request) } finally { closed = true }
  })() } } as EngineInterface
  expect((await rejection(spawnBridge(wrapped, ['label']))).code).toBe('truncated')
  expect(closed).toBe(true)
})

test('spawnBridge collects the child\'s stdout with no timeout and decodes it as runBridge does', async () => {
  // The document may arrive in pieces, with progress on stderr between them; the child is given no deadline.
  const ok = fake(withEnv({ spawn: spawnedBridge({ label: { chunks: [err('step 1/3\n'), out('{"path": "/l.json", '), err('step 2/3\n'), out('"text": "Labels"}\n')], code: 0 } }) }))
  expect(await spawnBridge(ok.$, ['label', '--transcript', '/t'])).toEqual({ path: '/l.json', text: 'Labels' })
  expect(ok.spawned).toEqual([{ argv: [VENV_PY, '-I', LAUNCHER, 'label', '--transcript', '/t'], cwd: CWD }])
  expect(ok.ran).toEqual([])

  // The error envelope, as runBridge maps it.
  const bad = fake(withEnv({ spawn: spawnedBridge({ label: { chunks: [out('{"error":{"code":"missing_extra","message":"no llm","extra":"llm"}}\n')], code: 1 } }) }))
  const e = await rejection(spawnBridge(bad.$, ['label']))
  expect(e.code).toBe('missing_extra')
  expect(e.message).toBe('no llm')
  expect(e.extra).toEqual({ extra: 'llm' })

  // No environment yet: nothing is spawned.
  const none = fake({ spawn: spawnedBridge({ label: { chunks: [out('{}')], code: 0 } }) })
  expect((await rejection(spawnBridge(none.$, ['label']))).code).toBe('no_env')
  expect(none.spawned).toEqual([])

  // A non-zero exit without an envelope names the exit and stderr's last line.
  const usage = fake(withEnv({ spawn: spawnedBridge({ label: { chunks: [err('usage: trajviz_obs_cli.py label\nerror: unrecognized arguments: --nope\n')], code: 2 } }) }))
  const u = await rejection(spawnBridge(usage.$, ['label', '--nope']))
  expect(u.code).toBe('exit')
  expect(u.message).toContain('exited with 2')
  expect(u.message).toContain('unrecognized arguments: --nope')

  // A child ended by a signal (the module unloading, a kill) is an exit too, named by the signal.
  const killed = fake(withEnv({ spawn: spawnedBridge({ label: { chunks: [], code: null, signal: 'SIGTERM' } }) }))
  const k = await rejection(spawnBridge(killed.$, ['label']))
  expect(k.code).toBe('exit')
  expect(k.message).toContain('SIGTERM')

  // Exit 0 without a JSON document is reported.
  const junk = fake(withEnv({ spawn: spawnedBridge({ label: { chunks: [out('not json\n')], code: 0 } }) }))
  expect((await rejection(spawnBridge(junk.$, ['label']))).code).toBe('bad_output')

  // The child could not start: the host's reason is kept.
  const dead = fake(withEnv({ spawn: () => undefined }))
  const d = await rejection(spawnBridge(dead.$, ['label']))
  expect(d.code).toBe('run_failed')
  expect(d.message).toContain(`cannot start ${VENV_PY}`)
})

test('classic hooks store the transcript and pass the event on', async ($, on) => {
  const writes: { key: string; value: unknown }[] = []
  on('state.set', (_, e, next) => {
    if (e.plugin === 'trajviz-obs') writes.push({ key: e.key, value: e.value })
    return next(e)
  })
  on('classic.SessionStart', () => ({}))
  on('classic.Stop', () => ({}))
  on('classic.UserPromptSubmit', () => ({}))
  on('command.register', () => ({ value: { command: 'obs' } }))
  on('tool.register', () => ({ value: { tool: 'mcp__trajviz-obs__obs' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })

  // Flags: the manifest's defaults seed watch and statusline; isInteractive is the session's.
  expect(writes.filter(w => w.key === 'flags').map(w => w.value)).toEqual([{ watch: true, statusline: false, isInteractive: true, envReady: false }])

  await $.classic.SessionStart({ source: 'startup', transcript_path: '/t/x.jsonl', session_id: 'x' })
  const sessionWrites = () => writes.filter(w => w.key === 'session').map(w => w.value)
  expect(sessionWrites()).toEqual([{ transcriptPath: '/t/x.jsonl', sessionId: 'x', selected: null }])

  // A /clear starts a new transcript: the latest one seen wins.
  await $.classic.UserPromptSubmit({ prompt: 'hi', transcript_path: '/t/y.jsonl', session_id: 'y' })
  await $.classic.Stop({ stop_hook_active: false, transcript_path: '/t/z.jsonl', session_id: 'z' })
  expect(sessionWrites().at(-1)).toEqual({ transcriptPath: '/t/z.jsonl', sessionId: 'z', selected: null })

  // An event without a local transcript (the kit stamps '') keeps what is known.
  await $.classic.Stop({ stop_hook_active: false })
  expect(sessionWrites().at(-1)).toEqual({ transcriptPath: '/t/z.jsonl', sessionId: 'z', selected: null })
  expect(sessionWrites().length).toBe(3)
})

test('session.start seeds watch and statusline from the options', { options: { watch: false, statusline: true } }, async ($, on) => {
  const flagWrites: unknown[] = []
  on('state.set', (_, e, next) => {
    if (e.plugin === 'trajviz-obs' && e.key === 'flags') flagWrites.push(e.value)
    return next(e)
  })
  on('command.register', () => ({ value: { command: 'obs' } }))
  on('tool.register', () => ({ value: { tool: 'mcp__trajviz-obs__obs' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  await $.session.start({ cwd: CWD, surface: null, isInteractive: false })
  expect(flagWrites).toEqual([{ watch: false, statusline: true, isInteractive: false, envReady: false }])
})

test('transcript from classic.SessionStart wins over the derived path', async () => {
  // What the classic hooks wrote; the derived path exists too, and is not asked for.
  const w = fake(withEnv({ state: { session: { transcriptPath: '/t/x.jsonl', sessionId: 'x', selected: null } }, exists: [DERIVED] }))
  expect(await resolveTranscript(w.$)).toEqual({ path: '/t/x.jsonl', sessionId: 'x' })
  expect(w.existsAsked).toEqual([])
  expect(w.ran).toEqual([])
})

test('derived path used when it exists', async () => {
  const w = fake(withEnv({ exists: [DERIVED] }))
  expect(await resolveTranscript(w.$)).toEqual({ path: DERIVED, sessionId: 'abc' })
  expect(w.existsAsked).toEqual([DERIVED])
  expect(w.ran).toEqual([])

  // CLAUDE_CONFIG_DIR moves the projects folder.
  const cfg = fake(withEnv({ env: { CLAUDE_CONFIG_DIR: '/cfg/' }, exists: ['/cfg/projects/-Users-me-p/abc.jsonl'] }))
  expect((await resolveTranscript(cfg.$)).path).toBe('/cfg/projects/-Users-me-p/abc.jsonl')

  // Not there: the bridge scans every project folder for the id.
  const scan = fake(
    withEnv({
      run: bridge({ resolve: { exitCode: 0, stdout: '{"path": "/elsewhere/abc.jsonl", "session_id": "abc", "project_dir": "/x", "kind": "file"}' } }),
    }),
  )
  expect(await resolveTranscript(scan.$)).toEqual({ path: '/elsewhere/abc.jsonl', sessionId: 'abc' })
  expect(scan.ran.map(r => r.argv.slice(3))).toEqual([['resolve', '--cwd', CWD, '--config-dir', `${HOME}/.claude`, '--session-id', 'abc']])
  expect(scan.ran[0]?.init).toEqual({ cwd: CWD, timeoutMs: 120_000 })

  // No transcript anywhere: the bridge's not_found reaches the caller as is.
  const lost = fake(withEnv({ run: bridge({ resolve: { exitCode: 1, stdout: '{"error": {"code": "not_found", "message": "no session abc"}}' } }) }))
  expect((await rejection(resolveTranscript(lost.$))).code).toBe('not_found')
})

test('--session spec goes to the bridge resolve', async () => {
  const w = fake(
    withEnv({
      state: { session: { transcriptPath: '/t/x.jsonl', sessionId: 'x', selected: null } },
      exists: [DERIVED],
      run: bridge({ resolve: { exitCode: 0, stdout: '{"path": "/p/bbb333.jsonl", "session_id": "bbb333", "project_dir": "/p", "kind": "file"}' } }),
    }),
  )
  expect(await resolveTranscript(w.$, 'bbb')).toEqual({ path: '/p/bbb333.jsonl', sessionId: 'bbb333' })
  expect(w.ran.map(r => r.argv.slice(3))).toEqual([['resolve', '--cwd', CWD, '--config-dir', `${HOME}/.claude`, '--session', 'bbb']])
  expect(w.existsAsked).toEqual([VENV_PY])

  // An ambiguous prefix: the candidates ride on the error for the row to list.
  const amb = fake(
    withEnv({
      run: bridge({
        resolve: {
          exitCode: 1,
          stdout: '{"error": {"code": "ambiguous", "message": "2 sessions start with aaa", "candidates": [{"id": "aaa111", "started": "2026-10-22T10:00:00Z", "size": 10}, {"id": "aaa222", "started": null, "size": 20}]}}',
        },
      }),
    }),
  )
  const e = await rejection(resolveTranscript(amb.$, 'aaa'))
  expect(e.code).toBe('ambiguous')
  expect((e.extra['candidates'] as { id: string }[]).map(c => c.id)).toEqual(['aaa111', 'aaa222'])
})

test('selectSession records the selected session beside the current transcript', async () => {
  const w = fake({ state: { session: { transcriptPath: '/t/x.jsonl', sessionId: 'x', selected: null } } })
  await selectSession(w.$, { path: '/p/bbb333.jsonl', id: 'bbb333', label: 'bbb333 · 2026-10-22 10:00' })
  expect(w.state.get('session')?.value).toEqual({
    transcriptPath: '/t/x.jsonl',
    sessionId: 'x',
    selected: { path: '/p/bbb333.jsonl', id: 'bbb333', label: 'bbb333 · 2026-10-22 10:00' },
  })
  await selectSession(w.$, null)
  expect(w.state.get('session')?.value).toEqual({ transcriptPath: '/t/x.jsonl', sessionId: 'x', selected: null })
  // Nothing written yet: the atom's initial is the base.
  const fresh = fake({})
  await selectSession(fresh.$, { path: '/q.jsonl', id: 'q', label: 'q' })
  expect(fresh.state.get('session')?.value).toEqual({ transcriptPath: null, sessionId: null, selected: { path: '/q.jsonl', id: 'q', label: 'q' } })
})

test('state atoms name the contract keys and snapshotRef adds the id', () => {
  expect(snapshotRef('3')).toEqual({ plugin: 'trajviz-obs', key: 'snapshot', id: '3' })
  expect(latest.ref).toEqual({ plugin: 'trajviz-obs', key: 'latest' })
  expect(latest.initial).toBe(null)
  expect(snapshotIds.ref).toEqual({ plugin: 'trajviz-obs', key: 'snapshotIds' })
  expect(snapshotIds.initial).toEqual([])
  expect(session.initial).toEqual({ transcriptPath: null, sessionId: null, selected: null })
  expect(flags.initial).toEqual({ watch: true, statusline: false, isInteractive: false, envReady: false })
  expect(paneTab.initial).toBe(1)
  expect(refresh.initial).toEqual({ lastRunAt: 0, lastDurationMs: 0, lastSize: 0, lastMtimeMs: 0, busy: false, busySince: 0 })
})
