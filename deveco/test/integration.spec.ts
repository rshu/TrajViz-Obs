import { afterEach, expect, test } from 'bun:test'
import { mkdtemp, readFile, realpath, rm, symlink } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { Backend } from '../src/backend.js'
import { runCommand } from '../src/commands.js'
import plugin from '../src/index.js'
import { PromptCapture } from '../../shared/prompt-capture.js'
import { registerPromptCapture } from '../src/prompt-capture.js'
import type { HostClient } from '../src/host.js'

const cleanup: (() => Promise<unknown>)[] = []
afterEach(async () => { for (const task of cleanup.splice(0).reverse()) await task() })
const BASE = 1_790_000_000_000
function messages(text = 'A controlled answer') {
  return [
    { info: { id: 'u', sessionID: 'ses_main', role: 'user', time: { created: BASE } }, parts: [{ id: 'ut', type: 'text', text: 'Inspect a fixture', sessionID: 'ses_main' }] },
    { info: { id: 'a', sessionID: 'ses_main', role: 'assistant', agent: 'build', modelID: 'fixture', providerID: 'local',
      time: { created: BASE + 100, completed: BASE + 1100 }, cost: 0, tokens: { input: 20, output: 5, reasoning: 0, cache: { read: 0, write: 0 } } },
      parts: [{ id: 'at', type: 'text', text, sessionID: 'ses_main' }] },
  ]
}
async function fixture() {
  const root = await realpath(await mkdtemp(path.join(tmpdir(), 'trajviz-deveco-integration-')))
  cleanup.push(() => rm(root, { recursive: true, force: true }))
  let rows = messages(), reads = 0
  const client: HostClient = { session: {
    async get() { return { data: { id: 'ses_main', directory: root, title: 'Controlled DevEco test', time: { created: BASE, updated: BASE + 1100 } } } },
    async messages() { reads++; return { data: rows } },
    async list() { return { data: [{ id: 'ses_main', directory: root }, { id: 'other', directory: '/other-project' }] } },
  } }
  const backend = new Backend(client, root, { cacheRoot: path.join(root, 'cache') })
  cleanup.push(() => backend.close())
  return { root, client, backend, setRows(next: ReturnType<typeof messages>) { rows = next }, reads: () => reads }
}

test('legacy plugin exports callable hooks and help without preparing a runtime or touching host context', async () => {
  const h = await fixture()
  const hooks = await plugin({ client: h.client, directory: h.root }, { cacheRoot: path.join(h.root, 'plugin') })
  cleanup.push(() => hooks.dispose!())
  expect(Object.keys(hooks.tool!)).toEqual(['obs'])
  expect(typeof hooks.event).toBe('function')
  expect(typeof hooks.dispose).toBe('function')
  const output = await hooks.tool!.obs!.execute({ command: 'help' }, { sessionID: 'ses_main' })
  expect(output).toContain('TrajViz Obs for DevEco Code')
  expect(output).toContain('not verified provider payloads')
  expect(h.reads()).toBe(0)
  await hooks.dispose!()
  expect(await hooks.tool!.obs!.execute({ command: 'help' }, { sessionID: 'ses_main' })).toContain('unloading')
})

test('diagnostics retain immutable source revisions while native live copies advance', async () => {
  const h = await fixture()
  await h.backend.revisions.run(async () => {
    const old = await h.backend.source('ses_main')
    const before = await readFile(old.transcript, 'utf8')
    h.setRows(messages('A later answer'))
    const next = await h.backend.source('ses_main')
    expect(next.transcript).not.toBe(old.transcript)
    expect(await readFile(old.transcript, 'utf8')).toBe(before)
    expect(await readFile(next.liveTranscript, 'utf8')).toContain('A later answer')
    expect(JSON.parse(before).source_format).toBe('deveco_v1')
  })
})

test('a superseded asynchronous read cannot overwrite a newer live source', async () => {
  const h = await fixture()
  let release!: () => void, entered!: () => void, count = 0
  const gate = new Promise<void>(resolve => { release = resolve })
  const started = new Promise<void>(resolve => { entered = resolve })
  h.client.session.messages = async () => {
    if (++count === 1) { entered(); await gate; return { data: messages('Old slow answer') } }
    return { data: messages('New quick answer') }
  }
  await h.backend.revisions.run(async () => {
    const old = h.backend.source('ses_main')
    await started
    const next = await h.backend.source('ses_main')
    release()
    await old
    expect(await readFile(next.liveTranscript, 'utf8')).toContain('New quick answer')
  })
})

test('commands use bounded structured bridge arguments and reject unsupported write/model actions', async () => {
  const h = await fixture(), calls: unknown[][] = []
  h.backend.inspect = async (...args) => { calls.push(args); return { text: 'Fixture diagnostics' } }
  expect(await runCommand(h.backend, 'ses_main', 'issues')).toBe('Fixture diagnostics')
  expect(calls[0]!.slice(0, 3)).toEqual(['ses_main', 'view', ['issues', '--limit', '25', '--offset', '0', '--json']])
  for (const input of ['json --out /some/file', 'dashboard --html /some/file', 'view anything --limit 999999', 'view --offset -1', 'analyze explain', 'update', 'view ../../secret']) {
    await expect(runCommand(h.backend, 'ses_main', input)).rejects.toThrow()
  }
  expect(calls).toHaveLength(1)
  expect(JSON.parse(await runCommand(h.backend, '', 'sessions 20'))).toHaveLength(1)
})

test('dashboard launch uses host-owned live input and shutdown terminates its owned process', async () => {
  const h = await fixture(), launches: string[][] = []
  h.backend.runtime.argv = async args => {
    launches.push([...args])
    return [process.execPath, '-e', 'console.log(JSON.stringify({url:"http://127.0.0.1:54321/"})); setInterval(()=>{},1000)']
  }
  expect(await h.backend.dashboard('ses_main', 54321)).toEqual({ url: 'http://127.0.0.1:54321/' })
  expect(launches[0]).toContain('--exit-on-stdin')
  const transcript = launches[0]![launches[0]!.indexOf('--transcript') + 1]!
  expect(path.basename(transcript)).toStartWith('live-')
  expect(JSON.parse(await readFile(transcript, 'utf8')).session.id).toBe('ses_main')
  const server = h.backend.servers.get('ses_main')!
  h.setRows(messages('Refreshed while dashboard open'))
  h.backend.changed(); h.backend.changed(); h.backend.changed()
  await Bun.sleep(350)
  expect(await readFile(transcript, 'utf8')).toContain('Refreshed while dashboard open')
  expect(h.reads()).toBe(2)
  await h.backend.close()
  expect(h.backend.servers.size).toBe(0)
  expect(server.child.exitCode !== null || server.child.signalCode !== null).toBe(true)
  expect(await Bun.file(transcript).exists()).toBe(false)
})

test('aborted host commands stop before native reads and disposal cancels in-flight collection', async () => {
  const h = await fixture()
  await expect(h.backend.source('ses_main', AbortSignal.abort())).rejects.toThrow()
  expect(h.reads()).toBe(0)
  let announce!: () => void
  const started = new Promise<void>(resolve => { announce = resolve })
  h.client.session.messages = async options => {
    announce()
    await new Promise((_, reject) => options.signal!.addEventListener('abort', () => reject(new Error('cancelled')), { once: true }))
    return { data: [] }
  }
  const pending = h.backend.source('ses_main').catch(error => String(error))
  await started
  await h.backend.close()
  expect(await pending).toContain('cancelled')
})

const python = process.env.TRAJVIZ_OBS_TEST_PYTHON
test.skipIf(!python)('real bridge produces DevEco trajectory diagnostics from native SDK rows without changing the source', async () => {
  const h = await fixture()
  const bridge = fileURLToPath(new URL('../../bridge/trajviz_obs_cli.py', import.meta.url))
  h.backend.runtime.argv = async args => [python!, '-I', bridge, ...args]
  const snapshot = await h.backend.inspect('ses_main', 'snapshot')
  expect(snapshot.empty).not.toBe(true)
  expect((snapshot.trajviz as { format: string }).format).toBe('deveco')
  expect((snapshot.counts as { assistant_steps: number }).assistant_steps).toBe(1)
  expect(h.reads()).toBe(1)
})


test('parallel native reads have a fixed memory/concurrency budget and release it after cancellation', async () => {
  const h = await fixture(), controllers = Array.from({ length: 4 }, () => new AbortController())
  let announce!: () => void, reads = 0
  const started = new Promise<void>(resolve => { announce = resolve })
  h.client.session.messages = async options => {
    if (++reads === 4) announce()
    await new Promise((_, reject) => options.signal!.addEventListener('abort', () => reject(new Error('cancelled')), { once: true }))
    return { data: [] }
  }
  const pending = controllers.map(controller => h.backend.source('ses_main', controller.signal).catch(() => undefined))
  await started
  await expect(h.backend.source('ses_main')).rejects.toThrow('Four native history reads')
  controllers.forEach(controller => controller.abort())
  await Promise.all(pending)
  h.client.session.messages = async () => ({ data: messages() })
  expect((await h.backend.source('ses_main')).sessionID).toBe('ses_main')
})


test('a failed newer read does not suppress the first valid live copy', async () => {
  const h = await fixture()
  let release!: () => void, entered!: () => void, reads = 0
  const gate = new Promise<void>(resolve => { release = resolve })
  const started = new Promise<void>(resolve => { entered = resolve })
  h.client.session.messages = async () => {
    if (++reads === 1) { entered(); await gate; return { data: messages('Valid original') } }
    throw new Error('New read failed')
  }
  const first = h.backend.source('ses_main')
  await started
  await expect(h.backend.source('ses_main')).rejects.toThrow('New read failed')
  release()
  const source = await first
  expect(await readFile(source.liveTranscript, 'utf8')).toContain('Valid original')
})

test('one caller can cancel waiting on shared startup without cancelling its owner', async () => {
  const h = await fixture()
  let entered!: () => void, release!: () => void
  const started = new Promise<void>(resolve => { entered = resolve })
  const gate = new Promise<void>(resolve => { release = resolve })
  h.backend.runtime.argv = async () => {
    entered(); await gate
    return [process.execPath, '-e', 'console.log(JSON.stringify({url:"http://127.0.0.1:54322/"})); setInterval(()=>{},1000)']
  }
  const owner = h.backend.dashboard('ses_main')
  await started
  const controller = new AbortController()
  const waiting = h.backend.dashboard('ses_main', undefined, controller.signal)
  controller.abort()
  await expect(waiting).rejects.toThrow('cancelled')
  release()
  expect(await owner).toEqual({ url: 'http://127.0.0.1:54322/' })
})


test('failed live refresh is labeled stale and a later successful refresh clears that state', async () => {
  const h = await fixture()
  h.backend.runtime.argv = async () => [process.execPath, '-e', 'console.log(JSON.stringify({url:"http://127.0.0.1:54323/"})); setInterval(()=>{},1000)']
  await h.backend.dashboard('ses_main')
  const source = await h.backend.source('ses_main')
  h.client.session.messages = async () => { throw new Error('fixture read unavailable') }
  h.backend.changed()
  await Bun.sleep(350)
  const stale = JSON.parse(await readFile(source.liveTranscript, 'utf8'))
  expect(stale.source_health.status).toBe('stale')
  expect(stale.warnings[0]).toContain('last successfully captured')
  h.client.session.messages = async () => ({ data: messages('Recovered') })
  h.backend.changed()
  await Bun.sleep(350)
  const fresh = JSON.parse(await readFile(source.liveTranscript, 'utf8'))
  expect(fresh.source_health).toBeUndefined()
  expect(JSON.stringify(fresh)).toContain('Recovered')
})


test.skipIf(!python)('real browser dashboard mounts DevEco trajectory and private Captured components without a model', async () => {
  const h = await fixture()
  const keys = ['TRAJVIZ_OBS_PROMPT_CAPTURE', 'TRAJVIZ_OBS_PROMPT_CAPTURE_DIR'] as const
  const previous = keys.map(key => [key, process.env[key]] as const)
  process.env.TRAJVIZ_OBS_PROMPT_CAPTURE = '1'
  process.env.TRAJVIZ_OBS_PROMPT_CAPTURE_DIR = path.join(h.root, 'prompts')
  const bridge = fileURLToPath(new URL('../../bridge/', import.meta.url))
  const capture = new PromptCapture({ adapter: 'deveco', bridge, python: () => python })
  const observed = registerPromptCapture(capture)
  try {
    await observed.hooks['experimental.chat.system.transform']!({ sessionID: 'ses_main' }, { system: ['Controlled system instructions'] })
    await capture.flush()
    h.backend.runtime.argv = async args => [python!, '-I', path.join(bridge, 'trajviz_obs_cli.py'), ...args]
    const { url } = await h.backend.dashboard('ses_main')
    const response = await fetch(url + 'config', { signal: AbortSignal.timeout(15000) })
    expect(response.ok).toBe(true)
    const config = JSON.stringify(await response.json())
    expect(config).toContain('Captured components')
    expect(config).toContain('Capture lane')
    expect(h.backend.servers.has('ses_main')).toBe(true)
  } finally {
    await observed.close()
    for (const [key, value] of previous) if (value === undefined) delete process.env[key]; else process.env[key] = value
  }
}, 60000)


test('session listing accepts verified filesystem aliases while excluding other projects', async () => {
  const h = await fixture(), alias = path.join(h.root, 'project-alias')
  await symlink(h.root, alias, 'dir')
  h.client.session.list = async () => ({ data: [{ id: 'ses_main', directory: alias }, { id: 'foreign', directory: '/unrelated' }] })
  expect((await h.backend.sessions(20)).map(row => row.id)).toEqual(['ses_main'])
})


test('reusing a running or starting dashboard rejects a different requested port', async () => {
  const h = await fixture()
  let entered!: () => void, release!: () => void
  const started = new Promise<void>(resolve => { entered = resolve })
  const gate = new Promise<void>(resolve => { release = resolve })
  h.backend.runtime.argv = async () => {
    entered(); await gate
    return [process.execPath, '-e', 'console.log(JSON.stringify({url:"http://127.0.0.1:54321/"})); setInterval(()=>{},1000)']
  }
  const owner = h.backend.dashboard('ses_main', 54321)
  await started
  const conflict = h.backend.dashboard('ses_main', 54322).then(() => 'unexpected success', error => String(error))
  release()
  expect(await owner).toEqual({ url: 'http://127.0.0.1:54321/' })
  expect(await conflict).toContain('dashboard stop')
  await expect(h.backend.dashboard('ses_main', 54322)).rejects.toThrow('dashboard stop')
  expect(h.reads()).toBe(1)
  expect(await h.backend.dashboard('ses_main', 54321)).toEqual({ url: 'http://127.0.0.1:54321/' })
  expect(h.reads()).toBe(2)
})
