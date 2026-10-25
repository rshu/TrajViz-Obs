import { expect, test } from 'claude-code/testing'
import type { EngineInterface, PluginOptions, ProcessSpawnChunk, ProcessSpawnResult, StateAddress } from 'claude-code'
import { NoPythonError, SETUP_TIMEOUT_MS, ensureEnv, ensureExtra, findPython, findUv, isLocalPath, needsSetup, readEnvState, requirementArgv, withExtras } from './lib/python'

type RunAnswer = { exitCode: number; stdout: string; stderr?: string }
type SpawnAnswer = { chunks: ProcessSpawnChunk[]; code: number }
type FakeWorld = { files?: Record<string, string>; pythonExists?: boolean; run?: (argv: readonly string[]) => RunAnswer | undefined; spawn?: (argv: readonly string[]) => SpawnAnswer }
type Fake = { $: EngineInterface; spawned: string[][]; ran: string[][]; writes: Record<string, string>; files: Record<string, string> }
const HOME = '/home/u'
const CACHE = `${HOME}/.cache/trajviz-obs`
const VENV_PY = `${CACHE}/envs/0123456789abcdef01234567/0123456789abcdef0123456789abcdef/bin/python`
const STATE = `${CACHE}/state.json`
const BOOTSTRAP = '/plugin/bridge/bootstrap.py'
const OPTIONS: PluginOptions = { trajvizSource: 'agent-trajviz>=0.2.0,<0.3', python: '' }
const STATE_JSON = JSON.stringify({ revision: 4, python: VENV_PY, trajviz: '0.2.1', extras: ['llm', 'charts'], source: 'agent-trajviz>=0.2.0,<0.3', createdAt: 1700000000000 })

function fake(world: FakeWorld): Fake {
  const files = { ...(world.files ?? {}) }, writes: Record<string, string> = {}
  const spawned: string[][] = [], ran: string[][] = []
  const state = new Map<string, { value: unknown; version: number }>()
  let pythonExists = world.pythonExists !== false
  const $ = {
    plugin: { root: '/plugin' }, session: { cwd: async () => '/work' },
    env: { get: async (name: string) => ({ HOME })[name] },
    clock: { sleep: async (_ms: number, options?: { signal?: AbortSignal }) => new Promise<void>((_resolve, reject) => { options?.signal?.addEventListener('abort', () => reject(new Error('aborted')), { once: true }) }) },
    state: {
      get: async (ref: StateAddress) => state.get(ref.key) ?? { value: undefined, version: 0 },
      set: async (ref: StateAddress, value: unknown) => { const version = (state.get(ref.key)?.version ?? 0) + 1; state.set(ref.key, { value, version }); return { isSet: true, version } },
    },
    fs: {
      exists: async (path: string) => path === VENV_PY && pythonExists,
      stat: async () => { throw new Error('ENOENT') },
      read: async (path: string) => { if (path in files) return files[path]; throw new Error(`ENOENT: ${path}`) },
      write: async (path: string, text: string) => { files[path] = text; writes[path] = text },
    },
    process: {
      run: async (argv: readonly string[]) => { ran.push([...argv]); const a = world.run?.(argv); if (!a) throw new Error(`cannot start ${argv[0]}`); return { ...a, stderr: a.stderr ?? '', isStdoutTruncated: false, isStderrTruncated: false } },
      spawn: (request: { argv: readonly string[] }) => {
        spawned.push([...request.argv])
        let a = world.spawn?.(request.argv)
        if (!a) {
          const payload = JSON.parse(request.argv[request.argv.indexOf('--request') + 1] as string)
          const doc = { revision: 4, python: VENV_PY, pythonOption: payload.pythonOption, trajviz: '0.2.1', extras: [...new Set(['llm', 'charts', ...payload.extras])], source: payload.source, createdAt: 1 }
          files[STATE] = writes[STATE] = JSON.stringify(doc)
          pythonExists = true
          a = { chunks: [{ stream: 'stderr', text: 'Installing isolated environment\n' }, { stream: 'stdout', text: JSON.stringify(doc) }], code: 0 }
        }
        return (async function* (): AsyncGenerator<ProcessSpawnChunk, ProcessSpawnResult> { for (const c of a.chunks) yield c; return { code: a.code, signal: null } })()
      },
    },
  }
  return { $: $ as unknown as EngineInterface, spawned, ran, writes, files }
}
const versions = (table: Record<string, string>) => (argv: readonly string[]): RunAnswer | undefined => {
  const v = table[argv[0] ?? '']; return v && argv[1] === '--version' ? { exitCode: 0, stdout: `${v}\n` } : undefined
}
const payload = (argv: string[]) => JSON.parse(argv[argv.indexOf('--request') + 1] as string)

test('readEnvState parses state.json', async () => {
  const ok = fake({ files: { [STATE]: STATE_JSON } })
  const state = await readEnvState(ok.$)
  expect(state).toEqual({ revision: 4, python: VENV_PY, trajviz: '0.2.1', extras: ['llm', 'charts'], source: 'agent-trajviz>=0.2.0,<0.3', createdAt: 1700000000000 })
  expect(needsSetup(state, OPTIONS)).toBe(false)
  expect(needsSetup(state, { ...OPTIONS, trajvizSource: '/Users/me/TrajViz' })).toBe(true)
  expect(needsSetup(null, OPTIONS)).toBe(true)

  expect(await readEnvState(fake({}).$)).toBe(null)
  expect(await readEnvState(fake({ files: { [STATE]: '{not json' } }).$)).toBe(null)
  expect(await readEnvState(fake({ files: { [STATE]: '{"python": 3}' } }).$)).toBe(null)
})

test('findPython skips 3.9 and takes 3.12', async () => {
  const w = fake({ run: versions({ '/opt/py39/bin/python': 'Python 3.9.6', 'python3.12': 'Python 3.12.4', python3: 'Python 3.9.6' }) })
  expect(await findPython(w.$, '/opt/py39/bin/python')).toEqual({ path: 'python3.12', version: '3.12.4' })
  expect(w.ran.map(a => a[0])).toEqual(['/opt/py39/bin/python', 'python3.13', 'python3.12'])
  // An override that is new enough is taken as is, before any PATH lookup.
  expect(await findPython(fake({ run: versions({ '/opt/py311': 'Python 3.11.0' }) }).$, '/opt/py311')).toEqual({ path: '/opt/py311', version: '3.11.0' })
  // Python 2 wrote its version to stderr.
  const old = fake({ run: argv => (argv[0] === 'python' ? { exitCode: 0, stdout: '', stderr: 'Python 2.7.18\n' } : undefined) })
  expect(await findPython(old.$, '')).toBe(null)
  expect(await findPython(fake({}).$, '')).toBe(null)

  expect(await findUv(fake({ run: versions({ uv: 'uv 0.4.20' }) }).$)).toBe('uv')
  expect(await findUv(fake({}).$)).toBe(null)
  expect(await findUv(fake({ run: argv => (argv[0] === 'uv' ? { exitCode: 1, stdout: '' } : undefined) }).$)).toBe(null)
})

test('withExtras puts the extras before the version specifier', () => {
  expect(withExtras('agent-trajviz>=0.2.0,<0.3', ['llm'])).toBe('agent-trajviz[llm]>=0.2.0,<0.3')
  expect(withExtras('agent-trajviz', ['llm', 'charts'])).toBe('agent-trajviz[llm,charts]')
  expect(withExtras('agent-trajviz[charts]>=0.2', ['llm'])).toBe('agent-trajviz[charts,llm]>=0.2')
  expect(withExtras('agent-trajviz[llm]', ['llm'])).toBe('agent-trajviz[llm]')
  expect(withExtras('Agent_Trajviz ~= 0.2.0', ['llm'])).toBe('Agent_Trajviz[llm] ~= 0.2.0')
  expect(withExtras('agent-trajviz; python_version >= "3.11"', ['llm'])).toBe('agent-trajviz[llm]; python_version >= "3.11"')
  // PEP 508 URL requirements, with and without the spaces around `@`.
  expect(withExtras('agent-trajviz @ git+https://example.test/trajviz.git', ['llm'])).toBe('agent-trajviz[llm] @ git+https://example.test/trajviz.git')
  expect(withExtras('agent-trajviz@git+https://x/y.git', ['llm'])).toBe('agent-trajviz[llm]@git+https://x/y.git')
  // A bare URL (a VCS ref or a wheel) is given the package name: extras never go on the URL.
  expect(withExtras('git+https://example.test/agent-trajviz.git@v0.2.0', ['llm'])).toBe('agent-trajviz[llm] @ git+https://example.test/agent-trajviz.git@v0.2.0')
  expect(withExtras('https://example.test/agent_trajviz-0.2.0-py3-none-any.whl', ['llm', 'charts'])).toBe(
    'agent-trajviz[llm,charts] @ https://example.test/agent_trajviz-0.2.0-py3-none-any.whl',
  )
  // Local paths: `~`, `=` and `@` inside them are not specifiers.
  expect(withExtras('/Users/me/TrajViz', ['llm'])).toBe('/Users/me/TrajViz[llm]')
  expect(withExtras('./trajviz', ['llm'])).toBe('./trajviz[llm]')
  expect(withExtras('~/src/TrajViz', ['llm'])).toBe('~/src/TrajViz[llm]')
  expect(withExtras('/home/a/my~repo', ['llm'])).toBe('/home/a/my~repo[llm]')
  expect(withExtras('/home/a/v=2/TrajViz', ['llm'])).toBe('/home/a/v=2/TrajViz[llm]')
  expect(withExtras('/home/a/TrajViz[charts]', ['llm'])).toBe('/home/a/TrajViz[charts,llm]')
  expect(withExtras('C:\\src\\TrajViz', ['llm'])).toBe('C:\\src\\TrajViz[llm]')
  expect(withExtras('src/TrajViz', ['llm'])).toBe('src/TrajViz[llm]')

  for (const path of ['/Users/me/TrajViz', './trajviz', '../trajviz', '~/src/TrajViz', '/home/a/my~repo', 'C:\\src\\TrajViz', 'C:/src/TrajViz', 'src/TrajViz']) {
    expect(isLocalPath(path)).toBe(true)
  }
  for (const req of ['agent-trajviz', 'agent-trajviz>=0.2.0,<0.3', 'agent-trajviz[llm]', 'agent-trajviz@git+https://x/y.git', 'git+https://x/y.git', 'https://x/a.whl', 'file:///x/y']) {
    expect(isLocalPath(req)).toBe(false)
  }
})

test('requirementArgv installs a local checkout editable with ~ expanded', async () => {
  const $ = fake({}).$
  expect(await requirementArgv($, 'agent-trajviz>=0.2.0,<0.3', ['llm'])).toEqual(['agent-trajviz[llm]>=0.2.0,<0.3'])
  expect(await requirementArgv($, '/Users/me/TrajViz', ['llm'])).toEqual(['-e', '/Users/me/TrajViz[llm]'])
  expect(await requirementArgv($, '~/src/TrajViz', ['llm', 'charts'])).toEqual(['-e', `${HOME}/src/TrajViz[llm,charts]`])
  expect(await requirementArgv($, 'git+https://x/y.git', ['llm'])).toEqual(['agent-trajviz[llm] @ git+https://x/y.git'])
})

test('uv runs the stdlib bootstrap and a warm environment spawns nothing', async () => {
  const w = fake({ run: versions({ uv: 'uv 0.9.0' }) })
  const progress: string[] = []
  const state = await ensureEnv(w.$, OPTIONS, line => progress.push(line))
  expect(w.spawned[0]?.slice(0, -1)).toEqual(['uv', 'run', '--no-project', '--python', '>=3.11', '--', 'python', '-I', BOOTSTRAP, '--request'])
  expect(payload(w.spawned[0]!)).toMatchObject({ cache: CACHE, source: OPTIONS['trajvizSource'], extras: ['llm', 'charts'], local: false, uv: 'uv' })
  expect(state.extras).toEqual(['llm', 'charts'])
  expect(progress).toEqual(['Installing isolated environment'])
  const ran = w.ran.length
  expect(await ensureEnv(w.$, OPTIONS, () => {})).toEqual(state)
  expect(w.spawned).toHaveLength(1)
  expect(w.ran).toHaveLength(ran)
})

test('the selected interpreter stays pinned when another session replaces the global pointer', async () => {
  const w = fake({ files: { [STATE]: STATE_JSON } })
  const pinned = await ensureEnv(w.$, OPTIONS, () => {})
  w.files[STATE] = JSON.stringify({ ...pinned, python: '/another/session/python', source: '/other/source' })
  expect(await readEnvState(w.$)).toEqual(pinned)
  expect(w.spawned).toEqual([])
})

test('a pinned relative checkout is invalidated when the session changes directory', async () => {
  const state = { ...JSON.parse(STATE_JSON), source: './checkout', sourceCwd: '/work' }
  const w = fake({ files: { [STATE]: JSON.stringify(state) } })
  expect(await readEnvState(w.$)).toEqual(state)
  w.$.session.cwd = async () => '/another/work'
  expect(await readEnvState(w.$)).toBeNull()
  const missing = fake({ files: { [STATE]: JSON.stringify({ ...JSON.parse(STATE_JSON), source: './checkout' }) } })
  expect(await readEnvState(missing.$)).toBeNull()
})

test('transient metadata sharing errors are retried without rebuilding', async () => {
  const w = fake({ files: { [STATE]: STATE_JSON } })
  const read = w.$.fs.read
  let attempts = 0
  w.$.fs.read = (async (path: string) => { if (++attempts <= 2) throw new Error('EACCES: Permission denied'); return read(path) }) as typeof read
  w.$.clock.sleep = async () => {}
  expect(await ensureEnv(w.$, OPTIONS, () => {})).toEqual(JSON.parse(STATE_JSON))
  expect(attempts).toBe(3)
  expect(w.spawned).toEqual([])
})

test('metadata outside the managed generations is never selected', async () => {
  for (const python of ['/usr/bin/python', `${CACHE}/venv/bin/python`, `/other/envs/0123456789abcdef01234567/0123456789abcdef0123456789abcdef/bin/python`]) {
    const w = fake({ files: { [STATE]: JSON.stringify({ ...JSON.parse(STATE_JSON), python }) } })
    w.$.fs.exists = async () => true
    w.$.fs.stat = async () => { throw new Error('ENOENT') }
    expect(await readEnvState(w.$)).toBeNull()
  }
  for (const changed of [{ extras: ['llm'] }, { trajviz: null }, { trajviz: '' }]) {
    expect(needsSetup({ ...JSON.parse(STATE_JSON), ...changed }, OPTIONS)).toBe(true)
  }
})

test('the canonical cache root permits symlink aliases without resolving the interpreter itself', async () => {
  const state = { ...JSON.parse(STATE_JSON), python: VENV_PY.replace(CACHE, '/canonical/cache') }
  const w = fake({ files: { [STATE]: JSON.stringify(state) } })
  w.$.fs.exists = async path => path === state.python
  w.$.fs.stat = async path => ({ kind: 'dir', size: 0, mtimeMs: 0, isLink: true, realPath: path === CACHE ? '/canonical/cache' : undefined })
  expect(await readEnvState(w.$)).toEqual(state)
})

test('changed source, Python override, old revision and deleted interpreter use the worker', async () => {
  for (const options of ([{ ...OPTIONS, trajvizSource: '~/checkout' }, { ...OPTIONS, python: '/opt/py312' }] as PluginOptions[])) {
    const w = fake({ files: { [STATE]: STATE_JSON }, run: versions({ uv: 'uv 0.9.0' }) })
    await ensureEnv(w.$, options, () => {})
    expect(w.spawned).toHaveLength(1)
    expect(payload(w.spawned[0]!)).toMatchObject({ source: options.trajvizSource, pythonOption: options.python })
  }
  const old = { ...JSON.parse(STATE_JSON), revision: 3 }
  expect(needsSetup(old, OPTIONS)).toBe(true)
  const w = fake({ files: { [STATE]: JSON.stringify(old) }, run: versions({ uv: 'uv 0.9.0' }) })
  expect((await ensureEnv(w.$, OPTIONS, () => {})).revision).toBe(4)
  expect(await readEnvState(fake({ files: { [STATE]: STATE_JSON }, pythonExists: false }).$)).toBeNull()
})

test('a failed generation leaves the previous environment ready and its state unchanged', async () => {
  const w = fake({ files: { [STATE]: STATE_JSON }, run: versions({ uv: 'uv 0.9.0' }), spawn: () => ({ code: 1, chunks: [{ stream: 'stdout', text: JSON.stringify({ error: { message: 'network unavailable' } }) }] }) })
  await expect(ensureEnv(w.$, { ...OPTIONS, trajvizSource: '/other/source' }, () => {})).rejects.toThrow('network unavailable')
  expect(w.files[STATE]).toBe(STATE_JSON)
  expect(w.writes[STATE]).toBeUndefined()
  expect((await ensureEnv(w.$, OPTIONS, () => {})).source).toBe(OPTIONS.trajvizSource)
  expect(w.spawned).toHaveLength(1)
})

test('without uv the bootstrap uses a compatible host interpreter, never the target venv', async () => {
  const w = fake({ run: versions({ 'python3.12': 'Python 3.12.4' }) })
  await ensureEnv(w.$, OPTIONS, () => {})
  expect(w.spawned[0]?.slice(0, -1)).toEqual(['python3.12', '-I', BOOTSTRAP, '--request'])
  expect(payload(w.spawned[0]!)).toMatchObject({ uv: null })
})

test('no compatible host interpreter reports what was tried and starts no worker', async () => {
  const w = fake({ run: versions({ python3: 'Python 3.9.6' }) })
  await expect(ensureEnv(w.$, OPTIONS, () => {})).rejects.toBeInstanceOf(NoPythonError)
  expect(w.spawned).toEqual([])
})

test('extras request a new generation and an installed extra remains a warm no-op', async () => {
  const w = fake({ files: { [STATE]: STATE_JSON }, run: versions({ uv: 'uv 0.9.0' }) })
  await ensureExtra(w.$, OPTIONS, 'app', () => {})
  expect(payload(w.spawned[0]!)).toMatchObject({ extras: ['llm', 'charts', 'llm', 'charts', 'app'] })
  expect((await readEnvState(w.$))?.extras).toEqual(['llm', 'charts', 'app'])
  await ensureExtra(w.$, OPTIONS, 'app', () => {})
  expect(w.spawned).toHaveLength(1)
})

test('malformed and oversized bootstrap results fail explicitly', async () => {
  for (const text of ['null', '{}', 'not json', 'x'.repeat(65_537)]) {
    const w = fake({ run: versions({ uv: 'uv 0.9.0' }), spawn: () => ({ code: 0, chunks: [{ stream: 'stdout', text }] }) })
    await expect(ensureEnv(w.$, OPTIONS, () => {})).rejects.toThrow('obs setup:')
  }
})

test('bootstrap replies must match the requested source, interpreter option and extras', async () => {
  for (const changed of [{ source: '/wrong/source' }, { pythonOption: '/wrong/python' }, { extras: ['llm'] }, { extras: ['llm', 'charts'] }]) {
    const w = fake({ run: versions({ uv: 'uv 0.9.0' }), spawn: () => ({ code: 0, chunks: [{ stream: 'stdout', text: JSON.stringify({ ...JSON.parse(STATE_JSON), ...changed }) }] }) })
    await expect(ensureExtra(w.$, OPTIONS, 'app', () => {})).rejects.toThrow('invalid environment document')
  }
})

test('an unresponsive bootstrap is bounded and its stream is closed', async () => {
  const w = fake({ run: versions({ uv: 'uv 0.9.0' }) })
  let closed = false
  w.$.process.spawn = () => ({
    result: new Promise(() => {}),
    next: () => new Promise(() => {}),
    return: async () => { closed = true; return { done: true, value: { code: null, signal: null } } },
    throw: async e => { throw e },
    [Symbol.asyncIterator]() { return this },
  })
  w.$.clock.sleep = async ms => { expect(ms).toBeLessThanOrEqual(SETUP_TIMEOUT_MS) }
  await expect(ensureEnv(w.$, OPTIONS, () => {})).rejects.toThrow('exceeded 10 minutes')
  expect(closed).toBe(true)
})
