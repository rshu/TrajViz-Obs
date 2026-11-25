import { afterEach, expect, test } from 'bun:test'
import { existsSync, realpathSync } from 'node:fs'
import { mkdtemp, readdir, readFile, rm, stat, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { PromptCapture, promptExplorerArgs } from '../../shared/prompt-capture.js'

const BRIDGE = path.resolve(import.meta.dir, '../../bridge')
const managedPython = path.join(BRIDGE, '.venv/bin/python')
const python = process.env.TRAJVIZ_OBS_TEST_PYTHON ?? (existsSync(managedPython) ? managedPython : Bun.which('python3') ?? undefined)
if (process.env.TRAJVIZ_OBS_TEST_PYTHON && !existsSync(process.env.TRAJVIZ_OBS_TEST_PYTHON)) throw new Error('TRAJVIZ_OBS_TEST_PYTHON must name an installed interpreter.')
const pythonTest = python ? test : test.skip
const folders: string[] = []
const captures: PromptCapture[] = []
const environment = new Map<string, string | undefined>()
function env(key: string, value: string | undefined): void {
  if (!environment.has(key)) environment.set(key, process.env[key])
  if (value === undefined) delete process.env[key]
  else process.env[key] = value
}
afterEach(async () => {
  for (const capture of captures.splice(0)) await capture.close()
  for (const folder of folders.splice(0)) await rm(folder, { recursive: true, force: true })
  for (const [key, value] of environment) {
    if (value === undefined) delete process.env[key]
    else process.env[key] = value
  }
  environment.clear()
})
async function fixture(resolvePython: () => string | undefined = () => python) {
  const temporary = realpathSync(await mkdtemp(path.join(tmpdir(), 'trajviz-prompt-storage-')))
  folders.push(temporary)
  const root = path.join(temporary, 'prompts')
  env('TRAJVIZ_OBS_PROMPT_CAPTURE', '1')
  env('TRAJVIZ_OBS_PROMPT_CAPTURE_DIR', root)
  const capture = new PromptCapture({ adapter: 'pi', bridge: BRIDGE, python: resolvePython, hostVersion: '1.5.2' })
  captures.push(capture)
  return { temporary, root, capture }
}
async function files(root: string): Promise<string[]> {
  const entries = await readdir(root, { withFileTypes: true }).catch((error: NodeJS.ErrnoException) => {
    if (error.code === 'ENOENT') return []
    throw error
  })
  return (await Promise.all(entries.map(entry => entry.isDirectory()
    ? files(path.join(root, entry.name))
    : entry.name.endsWith('.json') ? [path.join(root, entry.name)] : []))).flat()
}
async function records(root: string): Promise<any[]> {
  return (await Promise.all((await files(root)).map(async file => JSON.parse(await readFile(file, 'utf8')))))
    .sort((a, b) => a.sequence - b.sequence)
}
const observation = (value = 'original') => ({ session_id: 'isolated-session', stage: 'semantic.context', data: { messages: [{ role: 'user', content: value }] } })

test('Prompt Explorer arguments require the exact launch opt-in, an absolute local root, and a bounded native session ID', () => {
  env('TRAJVIZ_OBS_PROMPT_CAPTURE_DIR', '/private/capture')
  env('TRAJVIZ_OBS_TELEMETRY', '1')
  for (const flag of [undefined, '', '0', 'true', ' 1', '1 ']) {
    env('TRAJVIZ_OBS_PROMPT_CAPTURE', flag)
    expect(promptExplorerArgs('pi-native')).toEqual([])
  }
  env('TRAJVIZ_OBS_PROMPT_CAPTURE', '1')
  for (const root of [undefined, '', 'relative/prompts', '~/.capture']) {
    env('TRAJVIZ_OBS_PROMPT_CAPTURE_DIR', root)
    expect(promptExplorerArgs('pi-native')).toEqual([])
  }
  env('TRAJVIZ_OBS_PROMPT_CAPTURE_DIR', '/private/capture')
  for (const id of ['', 'bad\0session', 'x'.repeat(4097)]) expect(promptExplorerArgs(id)).toEqual([])
  env('OTEL_LOG_RAW_API_BODIES', 'file:/private/unrelated-claude-api')
  expect(promptExplorerArgs('pi-native')).toEqual(['--prompt-capture-root', '/private/capture', '--prompt-session-id', 'pi-native'])
  expect(promptExplorerArgs('x'.repeat(4096))).toHaveLength(4)
})

test('prompt capture requires exact opt-in and an absolute explicit destination independently of usage telemetry', async () => {
  let probes = 0
  const { temporary, root } = await fixture(() => { probes++; return python })
  env('TRAJVIZ_OBS_TELEMETRY', '1')
  for (const flag of [undefined, '', '0', 'true', ' 1', '1 ']) {
    env('TRAJVIZ_OBS_PROMPT_CAPTURE', flag)
    const capture = new PromptCapture({ adapter: 'pi', bridge: BRIDGE, python: () => { probes++; return python } })
    expect(capture.enabled).toBe(false)
    capture.record(observation())
    await capture.flush(); await capture.close()
  }
  env('TRAJVIZ_OBS_PROMPT_CAPTURE', '1')
  for (const destination of [undefined, '', 'relative/prompts', 'bad\0path']) {
    env('TRAJVIZ_OBS_PROMPT_CAPTURE_DIR', destination)
    const capture = new PromptCapture({ adapter: 'pi', bridge: BRIDGE, python: () => { probes++; return python } })
    expect(capture.enabled).toBe(false)
    capture.record(observation())
    await capture.flush(); await capture.close()
  }
  expect(probes).toBe(0)
  expect(existsSync(root)).toBe(false)
  expect(await readdir(temporary)).toEqual([])
})

pythonTest('records clone immediately, preserve JSON key order and leave caller data untouched', async () => {
  const { root, capture } = await fixture()
  env('TRAJVIZ_OBS_TELEMETRY', '0')
  const message = { role: 'system', sections: { zeta: 'first section', alpha: 'second section' }, content: '' }
  const data = { messages: [message], tools: [{ zeta: 1, alpha: 2 }] }
  const source = { session_id: 'isolated-session', stage: 'semantic.context', data }
  const before = JSON.stringify(source)
  capture.record(source)
  expect(JSON.stringify(source)).toBe(before)
  message.sections.zeta = 'changed after hook returned'
  data.messages.push({ role: 'user', sections: { zeta: '', alpha: '' }, content: 'later message' })
  data.tools[0]!.zeta = 9
  await capture.close()
  const stored = await records(root)
  expect(stored).toHaveLength(1)
  expect(stored[0].data).toEqual(JSON.parse(before).data)
  expect(Object.keys(stored[0].data.messages[0].sections)).toEqual(['zeta', 'alpha'])
  expect(Object.keys(stored[0].data.tools[0])).toEqual(['zeta', 'alpha'])
  expect(stored[0].host_version).toBe('1.5.2')
  expect(stored[0].capture_health).toEqual({ dropped: 0, failed: 0 })
  const raw = await readFile((await files(root))[0]!, 'utf8')
  expect(raw.indexOf('"zeta"')).toBeLessThan(raw.indexOf('"alpha"'))
})

pythonTest('capture accepts frozen native objects and writes private complete files', async () => {
  const { root, capture } = await fixture()
  const content = Object.freeze({ messages: Object.freeze([Object.freeze({ role: 'user', content: 'immutable native input' })]) })
  capture.record(Object.freeze({ session_id: 'isolated-session', stage: 'semantic.context', data: content }))
  await capture.flush()
  expect((await records(root))[0]?.data).toEqual(content)
  const stored = await files(root)
  expect(stored).toHaveLength(1)
  if (process.platform !== 'win32') {
    expect((await stat(stored[0]!)).mode & 0o777).toBe(0o600)
    expect((await stat(path.dirname(stored[0]!))).mode & 0o777).toBe(0o700)
    expect((await stat(root)).mode & 0o777).toBe(0o700)
  }
})

pythonTest('close drains accepted observations in sequence and refuses later records', async () => {
  const { root, capture } = await fixture()
  for (let index = 0; index < 6; index++) capture.record(observation(`step-${index}`))
  const closing = capture.close()
  expect(capture.close()).toBe(closing)
  await closing
  const stored = await records(root)
  expect(stored).toHaveLength(6)
  expect(stored.map(record => record.data.messages[0].content)).toEqual(Array.from({ length: 6 }, (_, index) => `step-${index}`))
  expect(new Set(stored.map(record => record.record_id)).size).toBe(6)
  expect(new Set(stored.map(record => record.instance_id)).size).toBe(1)
  expect(stored.map(record => record.sequence)).toEqual([1, 2, 3, 4, 5, 6])
  expect(capture.enabled).toBe(false)
  capture.record(observation('after close'))
  await capture.flush(); await capture.close()
  expect(await records(root)).toEqual(stored)
})

pythonTest('unavailable and throwing interpreter probes fail without escaping and later captures report the gap', async () => {
  let state: 'missing' | 'throws' | 'ready' = 'missing'
  const { root, capture } = await fixture(() => {
    if (state === 'throws') throw new Error('private setup failure')
    return state === 'ready' ? python : undefined
  })
  capture.record(observation('no interpreter'))
  await capture.flush()
  expect(existsSync(root)).toBe(false)
  state = 'throws'
  capture.record(observation('throwing interpreter'))
  await capture.flush()
  expect(existsSync(root)).toBe(false)
  state = 'ready'
  capture.record(observation('recovered'))
  await capture.close()
  const stored = await records(root)
  expect(stored.filter(record => record.stage === 'semantic.context')).toHaveLength(1)
  expect(stored[0].data.messages[0].content).toBe('recovered')
  expect(stored[0].capture_health).toEqual({ dropped: 0, failed: 2 })
  expect(stored.at(-1).stage).toBe('capture.status')
  expect(stored.at(-1).data).toEqual({ dropped: 0, failed: 2 })
  expect(JSON.stringify(stored)).not.toContain('private setup failure')
})

pythonTest('worker launch and writer failures remain nonthrowing and recover without partial records', async () => {
  let executable = path.join(realpathSync(tmpdir()), 'trajviz-nonexistent-prompt-python')
  const { root, capture } = await fixture(() => executable)
  capture.record(observation('unlaunchable worker'))
  await capture.flush()
  expect(existsSync(root)).toBe(false)
  executable = python!
  // Undefined data is not a valid JSON evidence record; the actual writer rejects it.
  capture.record({ session_id: 'isolated-session', stage: 'semantic.context', data: undefined })
  await capture.flush()
  capture.record(observation('valid after writer failure'))
  await capture.close()
  const stored = await records(root)
  expect(stored.filter(record => record.stage === 'semantic.context')).toHaveLength(1)
  expect(stored[0].capture_health).toEqual({ dropped: 0, failed: 2 })
  expect(stored[0].data.messages[0].content).toBe('valid after writer failure')
})

pythonTest('oversized and unserializable observations leave explicit health gaps without changing the previous record', async () => {
  const { root, capture } = await fixture()
  capture.record(observation('before gap'))
  await capture.flush()
  const firstFile = (await files(root))[0]!
  const original = await readFile(firstFile, 'utf8')
  capture.record(observation('x'.repeat(4 * 1024 * 1024)))
  const circular: { self?: unknown } = {}; circular.self = circular
  capture.record({ session_id: 'isolated-session', stage: 'semantic.context', data: circular })
  capture.record({ session_id: 'isolated-session', stage: 'semantic.context', data: 1n })
  capture.record(observation('after gap'))
  await capture.close()
  expect(await readFile(firstFile, 'utf8')).toBe(original)
  const semantic = (await records(root)).filter(record => record.stage === 'semantic.context')
  expect(semantic).toHaveLength(2)
  expect(semantic[1].capture_health).toEqual({ dropped: 3, failed: 0 })
  expect(semantic[1].data.messages[0].content).toBe('after gap')
  expect((await records(root)).at(-1).data).toEqual({ dropped: 3, failed: 0 })
})

pythonTest('changing capture destination disables the old instance without probing or creating the new directory', async () => {
  let probes = 0
  const { root, temporary, capture } = await fixture(() => { probes++; return python })
  const next = path.join(temporary, 'new-prompts')
  env('TRAJVIZ_OBS_PROMPT_CAPTURE_DIR', next)
  expect(capture.enabled).toBe(false)
  capture.record(observation())
  await capture.close()
  expect(probes).toBe(0)
  expect(existsSync(root)).toBe(false)
  expect(existsSync(next)).toBe(false)
})

pythonTest('bounded pending queues report dropped events on the next accepted observation', async () => {
  const { root, capture } = await fixture()
  for (let index = 0; index < 40; index++) capture.record(observation(`queued-${index}`))
  await capture.flush()
  const accepted = await records(root)
  expect(accepted).toHaveLength(16)
  expect(accepted.map(record => record.data.messages[0].content)).toEqual(Array.from({ length: 16 }, (_, index) => `queued-${index}`))
  capture.record(observation('after queue drained'))
  await capture.close()
  const after = (await records(root)).find(record => record.data?.messages?.[0]?.content === 'after queue drained')
  expect(after.capture_health).toEqual({ dropped: 24, failed: 0 })
})

pythonTest('closing a full pending queue persists terminal overflow health after accepted records drain', async () => {
  const { root, capture } = await fixture()
  for (let index = 0; index < 40; index++) capture.record(observation(`queued-${index}`))
  await capture.close()
  const stored = await records(root)
  const terminal = stored.filter(record => record.stage === 'capture.status').at(-1)
  expect(terminal).toBeDefined()
  expect(terminal.data.dropped).toBeGreaterThan(0)
  expect(stored.filter(record => record.stage === 'semantic.context')).toHaveLength(16)
})

pythonTest('queue byte bounds include the record currently owned by an active writer', async () => {
  const { root, capture } = await fixture()
  const content = 'x'.repeat(3 * 1024 * 1024)
  for (let index = 0; index < 3; index++) capture.record(observation(`${index}:${content}`))
  await capture.flush()
  expect(await records(root)).toHaveLength(2)
  capture.record(observation('after byte limit'))
  await capture.close()
  const after = (await records(root)).find(record => record.data?.messages?.[0]?.content === 'after byte limit')
  expect(after.capture_health).toEqual({ dropped: 1, failed: 0 })
})

pythonTest('close deadline kills an active writer instead of leaving it running after shutdown', async () => {
  const { temporary } = await fixture()
  const pids = path.join(temporary, 'writer-pids')
  await writeFile(path.join(temporary, 'prompt_capture.py'), [
    'import os, time',
    `with open(${JSON.stringify(pids)}, 'a') as output:`,
    '    output.write(str(os.getpid()) + "\\n")',
    'time.sleep(30)',
  ].join('\n'))
  const capture = new PromptCapture({ adapter: 'pi', bridge: temporary, python: () => python })
  captures.push(capture)
  const children = async () => (await readFile(pids, 'utf8').catch(() => '')).trim().split('\n').filter(Boolean).map(Number)
  const alive = (pid: number) => { try { process.kill(pid, 0); return true } catch { return false } }
  try {
    capture.record(observation('slow writer one'))
    capture.record(observation('slow writer two'))
    for (let attempt = 0; attempt < 100 && !(await children()).length; attempt++) await Bun.sleep(10)
    expect((await children()).length).toBe(1)
    await capture.close()
    const started = await children()
    expect(started).toHaveLength(2)
    for (let attempt = 0; attempt < 50 && started.some(alive); attempt++) await Bun.sleep(10)
    expect(started.some(alive)).toBe(false)
    expect(capture.enabled).toBe(false)
  } finally {
    for (const pid of await children()) { try { process.kill(pid, 'SIGKILL') } catch { /* Already exited. */ } }
  }
})
