import { afterEach, describe, expect, test } from 'bun:test'
import { mkdtemp, mkdir, readFile, readdir, rm, stat, symlink, utimes, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { SessionManager, type ExtensionContext } from '@earendil-works/pi-coding-agent'
import { Backend } from '../src/backend.js'
import { dashboardPort, loadOptions } from '../src/config.js'

const folders: string[] = []
const backends: Backend[] = []
const environment = new Map<string, string | undefined>()
function setenv(key: string, value: string): void { if (!environment.has(key)) environment.set(key, process.env[key]); process.env[key] = value }
afterEach(async () => {
  for (const backend of backends.splice(0)) await backend.close()
  for (const folder of folders.splice(0)) await rm(folder, { recursive: true, force: true })
  for (const [key, value] of environment) { if (value === undefined) delete process.env[key]; else process.env[key] = value }
  environment.clear()
})

async function fixture(id = 'native-session') {
  const directory = await mkdtemp(path.join(tmpdir(), 'trajviz-pi-backend-')); folders.push(directory)
  const manager = SessionManager.inMemory(directory, { id })
  const ctx = { cwd: directory, sessionManager: manager, model: { contextWindow: 200_000 }, getContextUsage: () => ({ tokens: 0, contextWindow: 200_000, percent: 0 }) } as unknown as ExtensionContext
  const backend = new Backend(directory, { cacheRoot: path.join(directory, 'cache') }); backends.push(backend)
  return { backend, ctx, manager, directory }
}
const captured = async (file: string): Promise<any[]> => JSON.parse(await readFile(file, 'utf8'))
const user = (content: string, timestamp = 1_790_000_000_000) => ({ role: 'user' as const, content, timestamp })
function deferred() { let resolve!: () => void; const promise = new Promise<void>(done => { resolve = done }); return { promise, resolve } }

describe('Pi SDK capture and selection', () => {
  test('an overlapping cache cannot replace a native session with automatic artifacts', async () => {
    const { backend, ctx, manager, directory } = await fixture()
    manager.appendMessage(user('Native history'))
    const source = await backend.source(ctx)
    const original = path.join(source.artifacts, 'labels.json')
    await writeFile(original, 'Original Pi source')
    for (const file of [original, path.join(directory, 'alias.jsonl')]) {
      if (file !== original) await symlink(original, file)
      manager.getSessionFile = () => file
      await expect(backend.source(ctx)).rejects.toThrow('directory contains the live Pi session')
      expect(await readFile(original, 'utf8')).toBe('Original Pi source')
    }
  })
  test('captures in-memory sessions privately, preserving active leaf, full history and measured zero', async () => {
    const { backend, ctx, manager } = await fixture()
    const first = manager.appendMessage(user('first'))
    manager.appendMessage(user('discarded branch'))
    manager.branch(first)
    const entries = JSON.stringify(manager.getEntries())
    const source = await backend.source(ctx)
    const rows = await captured(source.transcript)
    expect(rows.slice(1)).toEqual(manager.getEntries())
    expect(rows[0]._trajviz_obs_pi).toEqual({ schema_version: 1, leaf_id: first, context_window: 200_000, context_tokens: 0 })
    expect(rows[0].id).toBe('native-session')
    expect(source.argv).toEqual(['--transcript', source.transcript, '--artifacts', source.artifacts, '--kind', 'current'])
    expect(path.relative(backend.root, source.artifacts)).not.toContain('..')
    expect((await stat(source.transcript)).mode & 0o777).toBe(0o600)
    expect((await stat(path.dirname(source.transcript))).mode & 0o777).toBe(0o700)
    expect(manager.getSessionFile()).toBeUndefined()
    expect(JSON.stringify(manager.getEntries())).toBe(entries)
    expect(await captured(source.liveTranscript!)).toEqual(rows)
    expect(source.liveTranscript).toBe(path.join(source.artifacts, `live-${backend.revisions.instance}.json`))
  })

  test('branch changes produce immutable captures and do not infer the last physical entry as active', async () => {
    const { backend, ctx, manager } = await fixture()
    const first = manager.appendMessage(user('first'))
    const last = manager.appendMessage(user('alternate'))
    const one = await backend.source(ctx)
    manager.branch(first)
    const two = await backend.source(ctx)
    expect(one.artifacts).toBe(two.artifacts)
    expect(one.transcript).not.toBe(two.transcript)
    expect((await captured(one.transcript))[0]._trajviz_obs_pi.leaf_id).toBe(last)
    expect((await captured(two.transcript))[0]._trajviz_obs_pi.leaf_id).toBe(first)
    expect((await captured(two.transcript)).at(-1).id).toBe(last)
    expect((await captured(two.liveTranscript!))[0]._trajviz_obs_pi.leaf_id).toBe(first)
    expect((await backend.source(ctx)).transcript).toBe(two.transcript)
  })

  test('new empty sessions, reset leaf and unknown context remain valid private captures', async () => {
    const { backend, ctx, manager } = await fixture()
    ctx.getContextUsage = () => undefined
    Object.assign(ctx, { model: undefined })
    const empty = await captured((await backend.source(ctx)).transcript)
    expect(empty).toHaveLength(1)
    expect(empty[0]._trajviz_obs_pi).toEqual({ schema_version: 1, leaf_id: null, context_tokens: null })
    manager.appendMessage(user('first'))
    manager.resetLeaf()
    const reset = await captured((await backend.source(ctx)).transcript)
    expect(reset[0]._trajviz_obs_pi.leaf_id).toBeNull()
    expect(reset).toHaveLength(2)
  })

  test('concurrent captures never mutate request revisions and publish the newest live source', async () => {
    const { backend, ctx, manager } = await fixture()
    const requests: Promise<Awaited<ReturnType<Backend['source']>>>[] = []
    await backend.revisions.run(async () => {
      for (let i = 0; i < 15; i++) { manager.appendMessage(user(`message-${i}`)); requests.push(backend.source(ctx)) }
      const sources = await Promise.all(requests)
      for (const [i, source] of sources.entries()) expect((await captured(source.transcript)).filter(row => row.type === 'message')).toHaveLength(i + 1)
      const live = await captured(sources[0]!.liveTranscript!)
      expect(live.filter(row => row.type === 'message')).toHaveLength(15)
    })
  })

  test('snapshot retains its exact input during later refresh pruning', async () => {
    const { backend, ctx, manager } = await fixture()
    manager.appendMessage(user('original'))
    const begun = deferred(), resume = deferred()
    let file = ''
    backend.runtime.run = async <T>(argv: readonly string[]) => {
      file = argv[argv.indexOf('--transcript') + 1]!
      await utimes(file, 1, 1)
      begun.resolve(); await resume.promise
      return { empty: false, captured: await captured(file) } as T
    }
    const pending = backend.snapshot(ctx)
    await begun.promise
    for (let i = 0; i < 16; i++) { manager.appendMessage(user(`new-${i}`)); await backend.source(ctx) }
    expect((await captured(file)).filter(row => row.type === 'message')).toHaveLength(1)
    resume.resolve()
    const result = await pending as unknown as { captured: any[] }
    expect(result.captured[1].message.content).toBe('original')
  })

  test('import paths and selected sessions have isolated identities and safe argv', async () => {
    const { backend, ctx, directory } = await fixture()
    const imported = path.join(directory, '-saved transcript.json')
    await writeFile(imported, '[]')
    const one = await backend.source(ctx, imported, undefined, '--special-id')
    const two = await backend.source(ctx, imported, undefined, 'second')
    expect(one.transcript).toBe(imported)
    expect(one.argv).toContain('--session-id=--special-id')
    expect(one.sessionID).not.toBe(two.sessionID)
    expect(one.artifacts).not.toBe(two.artifacts)
    expect(one.argv).not.toContain('--kind')
    expect(one.liveTranscript).toBeUndefined()
    expect(await readFile(imported, 'utf8')).toBe('[]')
    expect(path.relative(backend.root, one.artifacts)).not.toContain('..')
    expect(await backend.source(ctx, ctx.sessionManager.getSessionId())).toHaveProperty('sessionID', 'native-session')
    const captureDirectory = await backend.source(ctx, directory, undefined, 'nested-session')
    expect(captureDirectory.transcript).toBe(directory)
    expect(captureDirectory.argv).toContain('nested-session')
    expect(captureDirectory.liveTranscript).toBeUndefined()
  })

  test('invalid capture selectors and cancellation fail before creating private output', async () => {
    const { backend, ctx, directory } = await fixture()
    await expect(backend.source(ctx, undefined, undefined, 'selected')).rejects.toThrow('--session-id requires')
    await expect(backend.source(ctx, 'native-session', undefined, 'selected')).rejects.toThrow('imported capture path')
    await expect(backend.source(ctx, 'missing.json', undefined, 'selected')).rejects.toThrow('existing capture path')
    for (const selector of ['', 'x'.repeat(4097), 'nul\0id']) await expect(backend.source(ctx, 'missing.json', undefined, selector)).rejects.toThrow('exact nonempty')
    const abort = new AbortController(); abort.abort(new Error('Test cancellation'))
    await expect(backend.source(ctx, undefined, abort.signal)).rejects.toThrow('Test cancellation')
    expect(await readdir(directory)).toEqual([])
  })

  test('real SDK session discovery accepts exact and unique prefixes and rejects ambiguity', async () => {
    const { backend, ctx, directory } = await fixture()
    const sessionsDir = path.join(directory, 'saved')
    const first = SessionManager.create(directory, sessionsDir, { id: 'saved-first' })
    first.appendMessage(user('saved-one'))
    const second = SessionManager.create(directory, sessionsDir, { id: 'saved-second' })
    second.appendMessage(user('saved-two'))
    Object.assign(ctx, { sessionManager: { getSessionId: () => 'current', getSessionDir: () => sessionsDir, getSessionFile: () => undefined } })
    const listed = await backend.sessions(ctx)
    expect(listed.map(row => row.id).sort()).toEqual(['saved-first', 'saved-second'])
    expect((await backend.source(ctx, 'saved-first')).transcript).toBe(first.getSessionFile()!)
    expect((await backend.source(ctx, 'saved-s')).sessionID).toBe('saved-second')
    await expect(backend.source(ctx, 'saved-')).rejects.toThrow('Ambiguous')
    await expect(backend.source(ctx, 'unknown')).rejects.toThrow('No matching')
  })
})

describe('Pi dashboard lifecycle', () => {
  const serverScript = `console.log(JSON.stringify({url:'http://127.0.0.1:39481/'}));process.stdin.resume();process.stdin.on('end',()=>process.exit(0))`
  const stub = (backend: Backend, script = serverScript) => {
    const launches: string[][] = []
    backend.runtime.argv = async args => { launches.push([...args]); return [process.execPath, '-e', script] }
    return launches
  }
  test('same-key concurrent starts reuse one process and native dashboards read the live capture', async () => {
    const { backend, ctx } = await fixture()
    const source = await backend.source(ctx)
    const launches = stub(backend)
    const signal = new AbortController().signal
    const [one, two] = await Promise.all([backend.serve('current', ['serve', '--transcript', source.liveTranscript!], [], signal), backend.serve('current', ['serve', '--transcript', source.liveTranscript!], [], signal)])
    expect(one).toEqual({ url: 'http://127.0.0.1:39481/' })
    expect(two).toEqual(one)
    expect(launches).toHaveLength(1)
    expect(launches[0]).toContain(source.liveTranscript!)
    expect(backend.servers.size).toBe(1)
    expect(await backend.stop('current')).toBe(true)
    expect(backend.servers.size).toBe(0)
    expect(await backend.stop('current')).toBe(false)
  })

  test('changed capture restarts its dashboard and external files remain direct inputs', async () => {
    const { backend, ctx, manager, directory } = await fixture()
    const launches = stub(backend)
    const first = await backend.source(ctx)
    const signal = new AbortController().signal
    await backend.serve('current', ['serve', '--transcript', first.liveTranscript!], [], signal)
    const child = backend.servers.get('current')!.child
    manager.appendMessage(user('new turn'))
    const second = await backend.source(ctx)
    await backend.serve('current', ['serve', '--transcript', second.liveTranscript!], [], signal)
    expect(backend.servers.get('current')!.child).not.toBe(child)
    expect(child.exitCode !== null || child.signalCode !== null).toBe(true)
    const imported = path.join(directory, 'external.json'); await writeFile(imported, '[]')
    const source = await backend.source(ctx, imported)
    await backend.serve('imported', ['serve', ...source.argv], [], signal)
    expect(launches.at(-1)).toContain(imported)
    expect(backend.servers.size).toBe(2)
    await backend.close()
    expect(backend.servers.size).toBe(0)
    expect(await readFile(imported, 'utf8')).toBe('[]')
  })

  test('two Pi processes viewing different branches cannot overwrite each other’s live input', async () => {
    const { backend, ctx, manager, directory } = await fixture()
    const first = manager.appendMessage(user('shared root'))
    const last = manager.appendMessage(user('second branch'))
    const one = await backend.source(ctx)
    const other = new Backend(directory, { cacheRoot: path.join(directory, 'cache') }); backends.push(other)
    manager.branch(first)
    const two = await other.source(ctx)
    expect(one.artifacts).toBe(two.artifacts)
    expect(one.liveTranscript).not.toBe(two.liveTranscript)
    expect((await captured(one.liveTranscript!))[0]._trajviz_obs_pi.leaf_id).toBe(last)
    expect((await captured(two.liveTranscript!))[0]._trajviz_obs_pi.leaf_id).toBe(first)
    await backend.close()
    expect(await stat(one.liveTranscript!).catch(() => undefined)).toBeUndefined()
    expect((await captured(two.liveTranscript!))[0]._trajviz_obs_pi.leaf_id).toBe(first)
  })

  test('stop and unload cancel a dashboard before setup completes', async () => {
    const { backend, ctx } = await fixture()
    const source = await backend.source(ctx)
    const entered = deferred(), resume = deferred()
    backend.runtime.argv = async () => { entered.resolve(); await resume.promise; return [process.execPath, '-e', serverScript] }
    const pending = backend.serve('current', ['serve', ...source.argv], [], new AbortController().signal).catch(error => error as Error)
    await entered.promise
    expect(await backend.stop('current')).toBe(true)
    resume.resolve()
    expect(String(await pending)).toContain('Cancelled')
    expect(backend.runtime.children.size).toBe(0)
    await backend.close()
    await expect(backend.serve('after-close', [], [], new AbortController().signal)).rejects.toThrow('unloading')
    await expect(backend.source(ctx)).rejects.toThrow('unloading')
  })

  test('startup failures and non-loopback URLs never register a dashboard', async () => {
    const { backend } = await fixture()
    stub(backend, `console.log(JSON.stringify({error:{message:'Cannot start dashboard'}}))`)
    await expect(backend.serve('bad', ['serve'], [], new AbortController().signal)).rejects.toThrow('Cannot start dashboard')
    stub(backend, `console.log(JSON.stringify({url:'https://external.invalid/'}));process.exit(0)`)
    await expect(backend.serve('remote', ['serve'], [], new AbortController().signal)).rejects.toThrow('before becoming ready')
    expect(backend.servers.size).toBe(0)
  })
})

describe('Pi settings', () => {
  async function optionsFixture() {
    const { directory } = await fixture()
    setenv('PI_CODING_AGENT_DIR', path.join(directory, 'agent'))
    setenv('XDG_STATE_HOME', path.join(directory, 'state'))
    await mkdir(path.join(directory, 'agent'), { recursive: true })
    await mkdir(path.join(directory, '.pi'), { recursive: true })
    return directory
  }
  test('global settings and project overrides validate all public option types', async () => {
    const cwd = await optionsFixture()
    await writeFile(path.join(cwd, 'agent', 'trajviz-obs.json'), JSON.stringify({ watch: false, statusline: false, openBrowser: true, dashboardPort: 8765, llm: 'env', analysisModel: 'model-one', labelMaxSteps: 300 }))
    await writeFile(path.join(cwd, '.pi', 'trajviz-obs.json'), JSON.stringify({ watch: true, dashboardPort: 0, analysisModel: 'model-two' }))
    const options = await loadOptions(cwd)
    expect(options).toEqual({ watch: true, statusline: false, openBrowser: true, dashboardPort: 0, llm: 'env', analysisModel: 'model-two', labelMaxSteps: 300 })
    for (const value of [{ watch: 'yes' }, { dashboardPort: '8765' }, { dashboardPort: 65536 }, { labelMaxSteps: 0 }, { llm: 'other' }, { python: '' }, { unknown: true }]) {
      await writeFile(path.join(cwd, '.pi', 'trajviz-obs.json'), JSON.stringify(value))
      await expect(loadOptions(cwd)).rejects.toThrow('Invalid TrajViz option')
    }
    await writeFile(path.join(cwd, '.pi', 'trajviz-obs.json'), '[]')
    await expect(loadOptions(cwd)).rejects.toThrow('Expected a JSON object')
  })

  test('dashboard port changes persist privately and auto overrides the configured default', async () => {
    const cwd = await optionsFixture()
    expect((await dashboardPort(8765)).port).toBe(8765)
    expect((await dashboardPort(8765, '9123')).port).toBe(9123)
    expect((await dashboardPort(8765)).port).toBe(9123)
    expect((await dashboardPort(8765, 'auto')).port).toBe(0)
    const file = path.join(cwd, 'state', 'trajviz-obs', 'pi-dashboard-port')
    expect((await stat(file)).mode & 0o777).toBe(0o600)
    await writeFile(file, '65536\n')
    expect((await dashboardPort(8765)).port).toBe(8765)
    setenv('XDG_STATE_HOME', 'relative')
    await expect(dashboardPort()).rejects.toThrow('absolute state directory')
  })
})
