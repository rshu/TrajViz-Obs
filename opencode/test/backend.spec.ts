import { afterEach, expect, spyOn, test } from 'bun:test'
import type { Plugin } from '@opencode/plugin'
import { chmod, link, mkdtemp, readFile, rm, stat, symlink, writeFile } from 'node:fs/promises'
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

function withoutService(backend: Backend): void {
  Object.defineProperty(backend, 'connection', { value: async () => undefined })
}

test('Desktop model-tool reads request a fresh authenticated capture rather than dirty cached history', async () => {
  const { backend, source } = await fixture()
  withoutService(backend)
  await backend.sync(source)
  backend.invalidate('ses_root')
  const fresh = { ...source, messages: [{ id: 'new', text: 'New native history' }] }
  const requests: string[] = []
  backend.requestCapture = async ({requestID,sessionID}) => {
    requests.push(sessionID)
    await backend.sync(fresh, requestID)
  }
  const selected = await backend.withSources(false, () => backend.session('ses_root'))
  expect(JSON.parse(await readFile(selected.transcript, 'utf8')).messages).toEqual(fresh.messages)
  expect(requests).toEqual(['ses_root'])
})

test('capture replies require the pending nonce, session and server location and cannot be replayed', async () => {
  const {backend,source} = await fixture()
  withoutService(backend)
  let request!: {requestID:string;sessionID:string}, ready!: () => void
  const published = new Promise<void>(resolve => { ready = resolve })
  backend.requestCapture = async value => { request = value; ready() }
  const pending = backend.session('ses_root')
  await published
  await expect(backend.sync(source, 'wrong-nonce')).rejects.toThrow('expired')
  await expect(backend.sync({...source,session:{...source.session,id:'ses_other'}}, request.requestID)).rejects.toThrow('another session')
  await expect(backend.sync({...source,directory:'/wrong'}, request.requestID)).rejects.toThrow('does not match')
  expect(backend.sources.size).toBe(0)
  await backend.sync(source,request.requestID)
  const selected = await pending
  await expect(backend.sync({...source,messages:[{id:'late'}]},request.requestID)).rejects.toThrow('expired')
  expect(JSON.parse(await readFile(selected.transcript,'utf8')).messages).toEqual([])
  expect(backend.sources.get('ses_root')?.messages).toEqual([])
})

test('cancelled capture requests reject late replies while a new UI connection can answer a new request', async () => {
  const {backend,source} = await fixture()
  withoutService(backend)
  const requests: {requestID:string;sessionID:string}[] = []
  let wake!: () => void
  backend.requestCapture = async value => { requests.push(value); wake() }
  const controller = new AbortController()
  let published = new Promise<void>(resolve => { wake = resolve })
  const old = backend.session('ses_root',undefined,controller.signal).catch(error => error as Error)
  await published
  controller.abort()
  expect(String(await old)).toContain('cancelled')
  published = new Promise<void>(resolve => { wake = resolve })
  const current = backend.session('ses_root')
  await published
  expect(requests[1]!.requestID).not.toBe(requests[0]!.requestID)
  await expect(backend.sync(source,requests[0]!.requestID)).rejects.toThrow('expired')
  await backend.sync(source,requests[1]!.requestID)
  expect((await current).sessionID).toBe('ses_root')
})

test('concurrent capture replies are single-use and cancellation during materialization publishes no cache', async () => {
  const {backend,source} = await fixture()
  withoutService(backend)
  let requestID!: string, announce!: () => void, started!: () => void, resume!: () => void
  const announced = new Promise<void>(resolve => { announce = resolve })
  const materializing = new Promise<void>(resolve => { started = resolve })
  const gate = new Promise<void>(resolve => { resume = resolve })
  backend.requestCapture = async request => { requestID = request.requestID; announce() }
  const controller = new AbortController()
  const pending = backend.session('ses_root',undefined,controller.signal).catch(error => error as Error)
  await announced
  const original = backend.materialize.bind(backend)
  backend.materialize = async captured => { started(); await gate; return original(captured) }
  const first = backend.sync(source,requestID).catch(error => error as Error)
  await materializing
  await expect(backend.sync({...source,messages:[{id:'duplicate'}]},requestID)).rejects.toThrow('expired')
  controller.abort()
  resume()
  expect(String(await pending)).toContain('cancelled')
  expect(String(await first)).toContain('cancelled')
  expect(backend.sources.size).toBe(0)
})

test('unload and unanswered capture requests release their pending slots', async () => {
  const {backend,source} = await fixture()
  withoutService(backend)
  let requestID!: string, ready!: () => void
  const published = new Promise<void>(resolve => { ready = resolve })
  backend.requestCapture = async value => { requestID = value.requestID; ready() }
  const pending = backend.session('ses_root').catch(error => error as Error)
  await published
  await backend.close()
  expect(String(await pending)).toContain('unloading')
  await expect(backend.sync(source,requestID)).rejects.toThrow('expired')
  const other = await fixture()
  withoutService(other.backend)
  const timers: {callback:()=>void}[] = []
  const original = globalThis.setTimeout
  const timer = spyOn(globalThis,'setTimeout').mockImplementation(((callback:()=>void, milliseconds:number) => {
    if (milliseconds === 15_000) timers.push({callback})
    return original(callback,milliseconds)
  }) as typeof setTimeout)
  try {
    let announce!: () => void
    const announced = new Promise<void>(resolve => { announce = resolve })
    let unansweredID!: string
    other.backend.requestCapture = async request => { unansweredID = request.requestID; announce() }
    const waiting = other.backend.session('ses_root').catch(error => error as Error)
    await announced
    timers[0]!.callback()
    expect(String(await waiting)).toContain('No connected OpenCode UI')
    await expect(other.backend.sync(other.source,unansweredID)).rejects.toThrow('expired')
    expect(other.backend.sources.size).toBe(0)
  } finally { timer.mockRestore() }
})

test('pending capture requests are bounded and publisher failures do not leak slots', async () => {
  const {backend} = await fixture()
  withoutService(backend)
  let requestCount = 0, ready!: () => void
  const published = new Promise<void>(resolve => { ready = resolve })
  backend.requestCapture = async () => { if (++requestCount === 64) ready() }
  const pending = Array.from({length:64},(_,index) => backend.session(`ses_${index}`).catch(error => error as Error))
  await published
  await expect(backend.session('ses_overflow')).rejects.toThrow('Too many pending')
  await backend.close()
  expect((await Promise.all(pending)).every(result => result instanceof Error)).toBe(true)
  const other = await fixture()
  withoutService(other.backend)
  other.backend.requestCapture = async () => { throw new Error('UI disconnected') }
  for (let index = 0; index < 65; index++) await expect(other.backend.session('ses_root')).rejects.toThrow('UI disconnected')
  other.backend.requestCapture = undefined
  await expect(other.backend.session('ses_root')).rejects.toThrow('OpenCode Desktop')
})

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

test('snapshot preserves bridge-reported family cost and a reported zero', async () => {
  const { backend, source } = await fixture()
  const seen: string[] = []
  const reported = [0, 3]
  backend.runtime.run = async <T>(argv: readonly string[]) => {
    seen.push(argv[argv.indexOf('--transcript') + 1]!)
    return { empty: false, context: { window: 200000 }, engine_usage: { context: { window: 200000 }, cost: { usd: reported.shift() } } } as unknown as T
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

test('snapshot never invents a cost absent from the bridge result', async () => {
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
    return { empty: false, context: { window: 200000 }, engine_usage: { context: { window: 200000 }, cost: { usd: loadedCost } } } as unknown as T
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

test('capture sessions in one file have isolated identities, artifacts and dashboard ownership', async () => {
  const { backend, directory } = await fixture()
  const file = path.join(directory, 'capture.json')
  await writeFile(file, '{}')
  const a = await backend.session('ses_root', file, undefined, '../../A')
  const b = await backend.session('ses_root', file, undefined, '../../B')
  expect(a.sessionID).not.toBe(b.sessionID)
  expect(a.artifacts).not.toBe(b.artifacts)
  expect(a.artifacts).toMatch(/\/[a-f0-9]{32}$/)
  expect(a.argv.slice(-2)).toEqual(['--session-id', '../../A'])
  expect(a.argv).not.toContain('--window')
  expect((await backend.session('ses_root', file, undefined, '--A')).argv).toContain('--session-id=--A')
  let began!: () => void
  const ready = new Promise<void>(resolve => { began = resolve })
  backend.runtime.argv = async (_args, signal) => {
    began()
    return new Promise((_resolve, reject) => signal?.addEventListener('abort', () => reject(new Error('Cancelled controlled setup')), { once: true }))
  }
  const pending = backend.serve('dashboard', ['dashboard', '--transcript', file, '--session-id', '../../A'], undefined, a.sessionID).catch(error => error)
  await ready
  expect(await backend.stop('dashboard', b.sessionID)).toBe(false)
  expect(await backend.stop('dashboard', a.sessionID)).toBe(true)
  expect(String(await pending)).toContain('Cancelled')
  await expect(backend.resolve('ses_root', undefined, 'A')).rejects.toThrow('requires --session')
  await expect(backend.resolve('ses_root', 'ses_root', 'A')).rejects.toThrow('existing capture path')
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

test('prompt sources are stable private native aliases, independent of immutable revisions and backend instances', async () => {
  const { backend, source } = await fixture()
  const first = await backend.materialize(source)
  const alias = await backend.promptSource(first)
  expect(alias).toContain(path.join(first.artifacts, 'prompt-live'))
  expect(alias).not.toBe(first.transcript)
  expect(first.promptLiveSource).toBe(alias)
  expect((await stat(alias)).mode & 0o777).toBe(0o600)
  expect((await stat(path.dirname(alias))).mode & 0o777).toBe(0o700)
  const child = { ...source, session: { ...source.session, id: 'ses_child', parentID: 'ses_root' }, messages: [{ id: 'child' }], children: [] }
  const next = { ...source, messages: [{ id: 'new' }], children: [child] }
  const second = await backend.materialize(next)
  expect(await backend.promptSource(second)).toBe(alias)
  expect(JSON.parse(await readFile(alias, 'utf8')).children[0].session.id).toBe('ses_child')
  expect(JSON.parse(await readFile(first.transcript, 'utf8')).messages).toEqual([])
  expect(JSON.parse(await readFile(second.transcript, 'utf8')).messages).toEqual([{ id: 'new' }])
  const other = new Backend(backend.ctx, backend.options); backends.push(other)
  expect(await other.promptSource(await other.materialize(source))).not.toBe(alias)
  await backend.close()
  await expect(readFile(alias)).rejects.toThrow()
})

test('saved imports and unsafe or foreign prompt alias storage cannot become live native sources', async () => {
  const { backend, source, directory } = await fixture()
  const saved = path.join(directory, 'saved.json')
  await writeFile(saved, JSON.stringify(source))
  await expect(backend.promptSource(await backend.session('ses_root', saved))).rejects.toThrow('native OpenCode')
  const selected = await backend.materialize(source)
  await expect(backend.promptSource({ ...selected, artifacts: directory })).rejects.toThrow('does not match')
  await expect(backend.promptSource({ ...selected, native: { ...source, directory: '/foreign' } })).rejects.toThrow('does not match')
  const alias = await backend.promptSource(selected)
  const folder = path.dirname(alias)
  await chmod(folder, 0o755)
  await expect(backend.promptSource(await backend.materialize({ ...source, messages: [{ id: 'next' }] }))).rejects.toThrow('not private')
  await chmod(folder, 0o700)
  await rm(alias)
  await symlink(saved, alias)
  await expect(backend.promptSource(await backend.materialize({ ...source, messages: [{ id: 'other' }] }))).rejects.toThrow('not private')
  expect(JSON.parse(await readFile(saved, 'utf8'))).toEqual(source)
})

async function openPromptDashboard(backend: Backend, source: NativeSource) {
  const selected = await backend.materialize(source)
  const alias = await backend.promptSource(selected)
  backend.runtime.argv = async () => [process.execPath, '-e', 'console.log(JSON.stringify({url:"http://127.0.0.1:12345/"}));process.stdin.resume()']
  await backend.serve('dashboard', ['dashboard', ...selected.argv, '--prompt-live-source', alias], undefined, selected.sessionID)
  return alias
}

async function eventually(check: () => boolean): Promise<void> {
  for (let attempt = 0; attempt < 400 && !check(); attempt++) await Bun.sleep(5)
  expect(check()).toBe(true)
}

test('active prompt dashboards coalesce new-child native events and refresh through authenticated UI capture', async () => {
  const { backend, source } = await fixture()
  withoutService(backend)
  const scheduled: { callback: () => void; handle: ReturnType<typeof setTimeout> }[] = []
  const original = globalThis.setTimeout
  const timer = spyOn(globalThis, 'setTimeout').mockImplementation(((callback: () => void, milliseconds: number) => {
    const handle = original(milliseconds === 1000 ? () => {} : callback, milliseconds === 1000 ? 60_000 : milliseconds)
    if (milliseconds === 1000) scheduled.push({ callback, handle })
    return handle
  }) as typeof setTimeout)
  let requests = 0, answer!: () => void, done = false
  const gate = new Promise<void>(resolve => { answer = resolve })
  const child = { ...source, session: { ...source.session, id: 'ses_new_child', parentID: 'ses_root' }, messages: [{ id: 'child-work' }], children: [] }
  backend.requestCapture = async ({ requestID }) => { requests++; await gate; await backend.sync({ ...source, messages: [{ id: 'work' }], children: [child] }, requestID); done = true }
  try {
    const alias = await openPromptDashboard(backend, source)
    for (let index = 0; index < 20; index++) expect(backend.invalidate('ses_new_child')).toBeUndefined()
    expect(scheduled.length).toBe(1)
    const first = scheduled.shift()!; clearTimeout(first.handle); first.callback()
    await eventually(() => requests === 1)
    for (let index = 0; index < 20; index++) backend.invalidate('ses_new_child')
    expect(scheduled.length).toBe(0)
    answer()
    await eventually(() => done && scheduled.length === 1)
    const native = JSON.parse(await readFile(alias, 'utf8'))
    expect(native.children[0].session.id).toBe('ses_new_child')
    expect(native.messages).toEqual([{ id: 'work' }])
    expect(requests).toBe(1)
    await backend.stop('dashboard', 'ses_root')
    for (const next of scheduled.splice(0)) clearTimeout(next.handle)
    backend.invalidate('ses_root')
    expect(scheduled.length).toBe(0)
    expect(await readFile(alias, 'utf8')).toContain('ses_new_child')
  } finally { answer(); timer.mockRestore() }
})

test('closing a prompt dashboard cancels pending UI capture, refuses late responses and does not dispatch agent work', async () => {
  const { backend, source } = await fixture()
  withoutService(backend)
  let requestID = ''
  backend.requestCapture = async request => { requestID = request.requestID }
  const alias = await openPromptDashboard(backend, source)
  await eventually(() => !!requestID)
  await backend.stop('dashboard', 'ses_root')
  await expect(backend.sync({ ...source, messages: [{ id: 'late' }] }, requestID)).rejects.toThrow('expired')
  expect(JSON.parse(await readFile(alias, 'utf8')).messages).toEqual([])
  await backend.close()
  await expect(readFile(alias)).rejects.toThrow()
})

test('prompt alias writes retain only the latest pending capture during a burst of direct syncs', async () => {
  const { backend, source } = await fixture()
  const alias = await backend.promptSource(await backend.materialize(source))
  type Prompt = { sessionID: string; file: string; latest?: { text: string }; writes: Promise<void> }
  const internal = backend as unknown as {
    promptSources: Map<string, Prompt>
    publishPromptSource(prompt: Prompt, value: { text: string; digest: string }): Promise<void>
    writePromptSource(prompt: Prompt, value: NativeSource): Promise<void>
  }
  const prompt = internal.promptSources.get('ses_root')!
  const publish = internal.publishPromptSource.bind(internal)
  let begin!: () => void, release!: () => void
  const started = new Promise<void>(resolve => { begin = resolve })
  const resumed = new Promise<void>(resolve => { release = resolve })
  const published: number[] = []
  const held = spyOn(internal, 'publishPromptSource').mockImplementation(async (entry, value) => {
    published.push(JSON.parse(value.text).messages[0].index)
    if (published.length === 1) { begin(); await resumed }
    return publish(entry, value)
  })
  try {
    const first = internal.writePromptSource(prompt, { ...source, messages: [{ index: 0 }] })
    await started
    const later = Array.from({ length: 50 }, (_, index) => internal.writePromptSource(prompt, { ...source, messages: [{ index: index + 1 }] }))
    expect(published).toEqual([0])
    expect(JSON.parse(prompt.latest!.text).messages[0].index).toBe(50)
    release()
    await Promise.all([first, ...later])
    expect(published).toEqual([0, 50])
    expect(prompt.latest).toBeUndefined()
    expect(JSON.parse(await readFile(alias, 'utf8')).messages[0].index).toBe(50)
  } finally { release(); held.mockRestore() }
})

test('inactive alias cleanup cannot delete a concurrently reopened generation or exceed the owner bound', async () => {
  const { backend, source, directory } = await fixture()
  const selected = await backend.materialize(source)
  const oldAlias = await backend.promptSource(selected)
  type Entry = { sessionID: string; file: string; owners: Set<symbol>; writes: Promise<void>; idleSince: number; dirty: boolean }
  const entries = (backend as unknown as { promptSources: Map<string, Entry> }).promptSources
  let release!: () => void
  const cleanupGate = new Promise<void>(resolve => { release = resolve })
  const old = entries.get('ses_root')!
  old.idleSince = Date.now() - 600_000; old.writes = cleanupGate
  for (let index = 0; index < 63; index++) {
    const sessionID = `inactive-${index}`
    entries.set(sessionID, { sessionID, file: path.join(directory, 'inactive', `${index}.json`),
      owners: new Set(), writes: Promise.resolve(), idleSince: Date.now(), dirty: false })
  }
  const other = await backend.materialize({ ...source, session: { ...source.session, id: 'ses_other' } })
  const opening = backend.promptSource(other).catch(error => error as Error)
  try {
    await eventually(() => !entries.has('ses_root'))
    const reopened = await backend.promptSource(selected)
    expect(reopened).not.toBe(oldAlias)
    release()
    expect(String(await opening)).toContain('Too many recent prompt dashboards')
    expect(entries.size).toBe(64)
    expect(JSON.parse(await readFile(reopened, 'utf8')).session.id).toBe('ses_root')
    await expect(readFile(oldAlias)).rejects.toThrow()
  } finally { release() }
})
