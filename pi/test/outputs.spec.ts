import { afterEach, expect, test } from 'bun:test'
import { existsSync } from 'node:fs'
import { link, mkdir, mkdtemp, readFile, readdir, rm, symlink, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { randomUUID } from 'node:crypto'
import { SessionManager, type ExtensionContext } from '@earendil-works/pi-coding-agent'
import { quoteArgument } from '../../hooks/lib/args.js'
import { Backend, digest } from '../src/backend.js'
import { runCommand, type Actions } from '../src/commands.js'
import type { Usage } from '../src/telemetry.js'

const folders: string[] = [], backends: Backend[] = []
afterEach(async () => {
  for (const backend of backends.splice(0)) await backend.close()
  for (const folder of folders.splice(0)) await rm(folder, { recursive: true, force: true })
})
const root = path.resolve(import.meta.dir, '../..')
const python = process.env.TRAJVIZ_OBS_TEST_PYTHON ?? path.join(root, 'bridge/.venv/bin/python')
if (process.env.TRAJVIZ_OBS_TEST_PYTHON && !existsSync(python)) throw new Error('TRAJVIZ_OBS_TEST_PYTHON must name an installed bridge interpreter.')
const hasBridge = existsSync(python) && Bun.spawnSync([python, '-I', '-c', 'import agent_trajviz']).exitCode === 0
const bridgeTest = hasBridge ? test : test.skip
const signal = () => new AbortController().signal
const actions: Actions = { watch: value => value ?? true, statusline: value => value ?? true, closePane() {}, progress() {} }
const usage = { dashboardArgs: () => [] } as unknown as Usage
async function fixture(name = 'native-session.jsonl') {
  const directory = await mkdtemp(path.join(tmpdir(), 'trajviz-pi-output-')); folders.push(directory)
  const native = path.join(directory, name), id = randomUUID()
  const rows = [
    { type: 'session', version: 3, id, cwd: directory, timestamp: '2026-11-18T01:00:00Z' },
    { type: 'message', id: 'user', parentId: null, timestamp: '2026-11-18T01:00:01Z', message: { role: 'user', content: 'Preserve this coding session', timestamp: 1794963601000 } },
    { type: 'message', id: 'answer', parentId: 'user', timestamp: '2026-11-18T01:00:02Z', message: { role: 'assistant', provider: 'controlled', model: 'native', timestamp: 1794963601500, durationMs: 500, stopReason: 'stop', content: [{ type: 'text', text: 'Controlled answer' }], usage: { input: 10, output: 2, cacheRead: 0, cacheWrite: 0, totalTokens: 12 } } },
  ]
  await mkdir(path.dirname(native), { recursive: true })
  const original = rows.map(row => JSON.stringify(row)).join('\n') + '\n'
  await writeFile(native, original)
  const manager = SessionManager.open(native, path.dirname(native), directory)
  let modelCalls = 0
  const ctx = { cwd: directory, mode: 'rpc', hasUI: true, sessionManager: manager, getContextUsage: () => undefined,
    model: { provider: 'controlled', id: 'native', contextWindow: 200_000 },
    modelRegistry: { streamSimple() {
      modelCalls++
      return { result: async () => ({ stopReason: 'stop', content: [{ type: 'text', text: 'Controlled diagnosis' }], usage: { input: 3, output: 2 } }) }
    } },
  } as unknown as ExtensionContext
  const backend = new Backend(directory, { cacheRoot: path.join(directory, 'cache') }); backends.push(backend)
  // Reuse a known interpreter; argv, subprocess execution and every Python writer remain real.
  backend.runtime.ready = async () => ({ python, source: 'agent-trajviz==0.7.1', revision: 6, extras: ['llm', 'charts', 'zstd', 'app'] })
  return { directory, native, original, ctx, manager, backend, modelCalls: () => modelCalls,
    run: (input: string) => backend.revisions.run(() => runCommand(backend, ctx, input, signal(), actions, usage)) }
}

bridgeTest('real Runtime passes trusted native input before the command and allows safe sibling exports', async () => {
  const f = await fixture('out/native-session.jsonl')
  await f.backend.protect(f.ctx)
  const argv = await f.backend.runtime.argv(['json', '--help'])
  expect(argv).toContain(`--host-input=${f.native}`)
  expect(argv.indexOf(`--host-input=${f.native}`)).toBeLessThan(argv.indexOf('json'))
  const result = await f.run(`json -o ${quoteArgument(path.dirname(f.native))}`)
  expect(result.path).toBe(path.dirname(f.native))
  expect(await readdir(path.dirname(f.native))).toContain('trajectory.json')
  expect(await readFile(f.native, 'utf8')).toBe(f.original)
})

bridgeTest('directory JSON exports cannot replace the native source hidden behind the private capture', async () => {
  const f = await fixture('out/trajectory.json')
  await expect(f.run(`json -o ${quoteArgument(path.dirname(f.native))}`)).rejects.toThrow('overwrite input session file')
  expect(await readFile(f.native, 'utf8')).toBe(f.original)
  expect(await readdir(path.dirname(f.native))).toEqual(['trajectory.json'])
})

bridgeTest('JSON child hardlinks and symlinks are rejected before writing any export files', async () => {
  for (const create of [link, symlink]) {
    const f = await fixture(), output = path.join(f.directory, 'export'); await mkdir(output)
    await create(f.native, path.join(output, 'metrics.json'))
    await expect(f.run(`json -o ${quoteArgument(output)}`)).rejects.toThrow('overwrite input session file')
    expect(await readdir(output)).toEqual(['metrics.json'])
    expect(await readFile(f.native, 'utf8')).toBe(f.original)
  }
})

bridgeTest('positional schemas output protects exact filenames and every generated-file alias', async () => {
  const exact = await fixture('schemas/metrics.schema.json')
  await expect(exact.run(`schemas ${quoteArgument(path.dirname(exact.native))}`)).rejects.toThrow('overwrite input session file')
  expect(await readFile(exact.native, 'utf8')).toBe(exact.original)
  expect(await readdir(path.dirname(exact.native))).toEqual(['metrics.schema.json'])
  for (const create of [link, symlink]) {
    const f = await fixture(), output = path.join(f.directory, 'schemas'); await mkdir(output)
    await create(f.native, path.join(output, 'metrics.schema.json'))
    await expect(f.run(`schemas ${quoteArgument(output)}`)).rejects.toThrow('overwrite input session file')
    expect(await readdir(output)).toEqual(['metrics.schema.json'])
    expect(await readFile(f.native, 'utf8')).toBe(f.original)
  }
})

bridgeTest('a dash is a literal file output except for explicit Pi collection stdout', async () => {
  const f = await fixture('-')
  await expect(f.run('bundle -o "-"')).rejects.toThrow('overwrite input session file')
  const collected = await f.run('collect pi -')
  expect(collected.text).toContain('"type":"session"')
  expect(await readFile(f.native, 'utf8')).toBe(f.original)
})

bridgeTest('analysis questions containing output-like text remain read-only questions', async () => {
  const f = await fixture()
  const result = await f.run(`analyze why did --out ${quoteArgument(f.native)} fail?`)
  expect(result.text).toContain('Controlled diagnosis')
  expect(result.outcome).not.toBe('error')
  expect(f.modelCalls()).toBe(1)
  expect(await readFile(f.native, 'utf8')).toBe(f.original)
})

test('artifact-directory symlinks cannot redirect current or imported operations into native storage', async () => {
  for (const imported of [false, true]) {
    const f = await fixture('native/labels.json')
    const selector = imported ? path.join(f.directory, 'import.jsonl') : undefined
    if (selector) await writeFile(selector, f.original)
    const id = selector ? digest(JSON.stringify([selector, undefined])).slice(0, 32) : f.manager.getSessionId()
    const artifacts = path.join(f.backend.root, digest(id).slice(0, 32))
    await mkdir(path.dirname(artifacts), { recursive: true })
    await symlink(path.dirname(f.native), artifacts, 'dir')
    await expect(f.backend.source(f.ctx, selector)).rejects.toThrow('cache or artifact directory contains the live Pi session')
    expect(await readFile(f.native, 'utf8')).toBe(f.original)
    expect(await readdir(path.dirname(f.native))).toEqual(['labels.json'])
    expect(f.modelCalls()).toBe(0)
  }
})
