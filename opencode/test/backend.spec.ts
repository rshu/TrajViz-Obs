import { afterEach, expect, test } from 'bun:test'
import type { Plugin } from '@opencode/plugin'
import { link, mkdtemp, readFile, rm, stat, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { Backend } from '../src/backend.js'
import { runCommand } from '../src/commands.js'
import type { NativeSource } from '../src/collect.js'
import type { Snapshot } from '../src/types.js'

const folders: string[] = []
const backends: Backend[] = []
afterEach(async () => {
  for (const backend of backends.splice(0)) await backend.close()
  for (const folder of folders.splice(0)) await rm(folder, { recursive: true, force: true })
})

async function fixture() {
  const directory = await mkdtemp(path.join(tmpdir(), 'trajviz-backend-test-')); folders.push(directory)
  const session = { id: 'ses_root', location: { directory }, model: { providerID: 'test', id: 'model' }, cost: 0,
    time: { created: 1790000000000, updated: 1790000000001 } }
  const ctx = { app: { version: '2.2.2' }, location: { directory },
    session: { async get({ sessionID }: {sessionID:string}) { return { ...session, id: sessionID } },
      async list() { return { data: [session], cursor: {} } } },
    model: { async list() { return { data: [{ id: 'model', providerID: 'test', limit: { context: 200000 } }] } } },
  } as unknown as Plugin.Context
  const backend = new Backend(ctx, { cacheRoot: path.join(directory, 'cache') }); backends.push(backend)
  const source: NativeSource = { source_format: 'opencode_v2', schema_version: 1, directory, session, messages: [], children: [] }
  return { backend, source, directory }
}

test('materialized source references are immutable under concurrent captures', async () => {
  const { backend, source } = await fixture()
  const next = { ...source, messages: [{ id: 'msg_new', type: 'user', text: 'New content', time: { created: 1790000000001 } }] }
  const [first, second] = await Promise.all([backend.materialize(source), backend.materialize(next)])
  expect(first.transcript).not.toBe(second.transcript)
  expect(JSON.parse(await readFile(first.transcript, 'utf8'))).toEqual({ ...source, window: 200000 })
  expect(JSON.parse(await readFile(second.transcript, 'utf8'))).toEqual({ ...next, window: 200000 })
  expect(source).not.toHaveProperty('window')
  expect(second.artifacts).toBe(first.artifacts)
  expect(first.argv).toContain('200000')
  expect((await stat(first.transcript)).mode & 0o777).toBe(0o600)
})

test('sync refuses a source from a different server location', async () => {
  const { backend, source } = await fixture()
  await expect(backend.sync({ ...source, directory: '/different/project' })).rejects.toThrow('does not match')
  expect(backend.sources.size).toBe(0)
})

test('snapshot cost comes from the exact pinned family and preserves a reported zero', async () => {
  const { backend, source } = await fixture()
  const seen: string[] = []
  backend.runtime.run = async <T>(argv: readonly string[]) => {
    seen.push(argv[argv.indexOf('--transcript') + 1]!)
    return { empty: false, context: { window: 200000 } } as unknown as T
  }
  await backend.sync(source)
  const zero = await backend.withSources(true, () => backend.snapshot('ses_root')) as Snapshot
  expect(zero.engine_usage?.cost?.usd).toBe(0)
  const child = { ...source, session: { ...source.session, id: 'ses_child', cost: 2 }, children: [] }
  await backend.sync({ ...source, session: { ...source.session, cost: 1 }, children: [child] })
  const total = await backend.withSources(true, () => backend.snapshot('ses_root')) as Snapshot
  expect(total.engine_usage?.cost?.usd).toBe(3)
  expect(seen[0]).not.toBe(seen[1])
})

test('unknown child cost stays unknown instead of appearing free', async () => {
  const { backend, source } = await fixture()
  backend.runtime.run = async <T>() => ({ empty: false, context: { window: 200000 } }) as unknown as T
  const child = { ...source, session: { id: 'ses_child' }, children: [] }
  await backend.sync({ ...source, session: { ...source.session, cost: 1 }, children: [child] })
  const result = await backend.withSources(true, () => backend.snapshot('ses_root')) as Snapshot
  expect(result.engine_usage?.cost).toBeUndefined()
})

test('a pinned request keeps source metrics and cost consistent during another session sync', async () => {
  const { backend, source } = await fixture()
  const old = { ...source, session: { ...source.session, cost: 1 } }
  const next = { ...source, session: { ...source.session, cost: 9 } }
  let begin!: () => void, resume!: () => void
  const started = new Promise<void>(resolve => { begin = resolve })
  const resumed = new Promise<void>(resolve => { resume = resolve })
  let loadedCost: unknown
  backend.runtime.run = async <T>(argv: readonly string[]) => {
    const native = JSON.parse(await readFile(argv[argv.indexOf('--transcript') + 1]!, 'utf8'))
    loadedCost = native.session.cost
    return { empty: false, context: { window: 200000 } } as unknown as T
  }
  await backend.sync(old)
  const pending = backend.withSources(true, async () => {
    begin(); await resumed
    return backend.snapshot('ses_root')
  })
  await started
  await backend.sync(next)
  resume()
  const result = await pending as Snapshot
  expect(loadedCost).toBe(1)
  expect(result.engine_usage?.cost?.usd).toBe(1)
})

test('file selection is resolved without treating the virtual id as a native session', async () => {
  const { backend, directory } = await fixture()
  const file = path.join(directory, 'external source.json')
  await writeFile(file, '{}')
  const selected = await backend.session('ses_root', file)
  expect(selected.transcript).toBe(file)
  expect(selected.sessionID).not.toBe('ses_root')
  expect(selected.argv).toEqual(['--transcript', file, '--artifacts', selected.artifacts])
})

test('current-session analysis retains the empty-session contract without changing saved selectors', async () => {
  const { backend, source } = await fixture()
  await backend.sync(source)
  const current = await backend.withSources(true, () => backend.session('ses_root'))
  expect(current.argv.slice(-2)).toEqual(['--kind', 'current'])
  const selected = await backend.withSources(true, () => backend.session('ses_root', 'ses_root'))
  expect(selected.argv).not.toContain('--kind')
})

test('native OpenCode collection exports the connected source without opening a legacy database', async () => {
  const { backend, source } = await fixture()
  await backend.sync(source)
  const calls: string[][] = []
  backend.runtime.run = async <T>(argv: readonly string[]) => { calls.push([...argv]); throw new Error('Native collection must not invoke Python or a database') }
  const execute = (args: string) => backend.withSources(true, () => runCommand(backend,
    { sessionID: 'ses_root', args, runID: 'collect_test' }, new AbortController().signal))
  const result = await execute('collect opencode ses_root')
  expect(result.path).toEndWith('native-export.json')
  expect(JSON.parse(await readFile(result.path!, 'utf8'))).toEqual({ ...source, window: 200000 })
  const inline = await execute('collect opencode ses_root -')
  expect(JSON.parse(inline.text)).toEqual({ ...source, window: 200000 })
  expect((await execute('collect opencode --help')).text).toContain('connected server API')
  expect(calls).toEqual([])
})

test('native collection refuses source overwrite, including a hard-linked output alias', async () => {
  const { backend, source, directory } = await fixture()
  await backend.sync(source)
  const ref = await backend.withSources(true, () => backend.session('ses_root'))
  const alias = path.join(directory, 'source alias.json')
  await link(ref.transcript, alias)
  const before = await readFile(ref.transcript, 'utf8')
  await expect(backend.withSources(true, () => runCommand(backend,
    { sessionID: 'ses_root', args: `collect opencode ses_root "${alias}"`, runID: 'collect_test' },
    new AbortController().signal))).rejects.toThrow('overwrite')
  expect(await readFile(ref.transcript, 'utf8')).toBe(before)
})

test('dashboard stop cancels a pending dependency setup for its stable session owner', async () => {
  const {backend,source} = await fixture()
  const ref = await backend.materialize(source)
  let began!: () => void
  const started = new Promise<void>(resolve => { began = resolve })
  backend.runtime.argv = async (_args, signal) => {
    began()
    await new Promise<void>((_resolve,reject) => signal!.addEventListener('abort', () => reject(new Error('Startup cancelled')), {once:true}))
    return []
  }
  const pending = backend.serve('dashboard', ['dashboard','--transcript',ref.transcript]).catch(error => error)
  await started
  expect(await backend.stop('dashboard',ref.sessionID)).toBe(true)
  expect((await pending as Error).message).toBe('Startup cancelled')
  expect(backend.runtime.children.size).toBe(0)
})
