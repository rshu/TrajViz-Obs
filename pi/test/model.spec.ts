import { afterEach, expect, test } from 'bun:test'
import { execFile } from 'node:child_process'
import { existsSync } from 'node:fs'
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import type { ExtensionContext } from '@earendil-works/pi-coding-agent'
import { runModel } from '../src/model.js'
import type { Backend } from '../src/backend.js'

const folders: string[] = []
afterEach(async () => { for (const folder of folders.splice(0)) await rm(folder, { recursive: true, force: true }) })
type Generation = { model: unknown; context: { systemPrompt: string; messages: { role: string; content: string; timestamp: number }[] }; options: { maxTokens: number; signal: AbortSignal; temperature?: number } }
const response = (text = 'Controlled reply') => ({ stopReason: 'stop', content: [{ type: 'thinking', thinking: 'private reasoning' }, { type: 'text', text }], usage: { input: 100, output: 10 } })
async function fixture(count = 1, mode: 'session' | 'env' = 'session') {
  const folder = await mkdtemp(path.join(tmpdir(), 'trajviz-pi-model-')); folders.push(folder)
  const transcript = path.join(folder, 'source.json'), original = JSON.stringify({ controlled: 'Original reviewed input' })
  await writeFile(transcript, original)
  const calls: string[][] = [], generations: Generation[] = [], progress: string[] = [], replies: { hash: string; replies: { index: number; text?: string; error?: string }[] }[] = []
  const prompts = Array.from({ length: count }, (_, index) => ({ index, system: 'Controlled system', prompt: `Controlled prompt ${index}`, max_tokens: 256, timeout_s: 5, delay_s: 0 }))
  const plan = { hash: 'reviewed-plan-hash', path: path.join(folder, 'plan.json'), plan: { calls: count, approx_input_tokens: count * 100 }, prompts }
  let output: Record<string, unknown> = { reply: 'Controlled analysis', path: path.join(folder, 'analysis.json') }
  let answer = async (_call: Generation) => response()
  let confirm = async () => true
  const sessionModel = { provider: 'session-provider', id: 'selected-model' }
  const configuredModel = { provider: 'configured-provider', id: 'alternate-model' }
  const context = { model: sessionModel, hasUI: true, ui: { confirm: () => confirm() },
    modelRegistry: {
      find: (provider: string, id: string) => provider === configuredModel.provider && id === configuredModel.id ? configuredModel : undefined,
      streamSimple(model: unknown, context: Generation['context'], options: Generation['options']) {
        const generation = { model, context, options }; generations.push(generation)
        return { result: () => answer(generation) }
      },
    },
  }
  const options: Backend['options'] = { llm: mode }
  const backend = { options,
    async source(_ctx: ExtensionContext, _selector?: string, _signal?: AbortSignal, captureSessionID?: string) {
      return { sessionID: 'pi-session', transcript, artifacts: folder,
        argv: ['--transcript', transcript, '--artifacts', folder], ...(captureSessionID === undefined ? {} : { captureSessionID }) }
    },
    runtime: { async run<T>(argv: string[], signal: AbortSignal): Promise<T> {
      signal.throwIfAborted(); calls.push(argv)
      if (argv[0] === 'llm-prompt') return plan as T
      if (argv[0] === 'bundle') {
        const target = argv[argv.indexOf('--out') + 1]!
        await mkdir(path.dirname(target), { recursive: true })
        await writeFile(target, await readFile(transcript)); return { path: target } as T
      }
      if (argv[0] === 'llm-apply') replies.push(JSON.parse(await readFile(argv[argv.indexOf('--replies') + 1]!, 'utf8')))
      return output as T
    } },
  } as unknown as Backend
  return { backend, ctx: context as unknown as ExtensionContext, context, folder, transcript, original, plan, calls, generations, progress, replies,
    options, configuredModel, sessionModel, answer: (fn: typeof answer) => { answer = fn }, confirm: (fn: typeof confirm) => { confirm = fn }, output: (value: typeof output) => { output = value } }
}
const signal = () => new AbortController().signal

test('native generation uses Pi registry authentication and the selected provider model', async () => {
  const f = await fixture(2)
  const result = await runModel(f.backend, f.ctx, 'analyze why did this fail?', signal(), line => f.progress.push(line))
  expect(f.generations).toHaveLength(2)
  const first = f.generations[0]!
  expect(first.model).toBe(f.sessionModel)
  expect(first.context.systemPrompt).toBe('Controlled system')
  expect(first.context.messages[0]).toEqual({ role: 'user', content: 'Controlled prompt 0', timestamp: expect.any(Number) })
  expect(first.options.maxTokens).toBe(256)
  expect(first.options.signal).toBeInstanceOf(AbortSignal)
  expect(f.replies[0]).toMatchObject({ hash: f.plan.hash, replies: [{ index: 0, text: 'Controlled reply' }, { index: 1, text: 'Controlled reply' }] })
  expect(JSON.stringify(f.replies)).not.toContain('private reasoning')
  expect(result.text).toContain('session-provider/selected-model')
  expect(result.text).toContain('Controlled analysis')
  expect(await readFile(f.transcript, 'utf8')).toBe(f.original)
})

test('a current-session label output cannot replace the actual native source behind its private capture', async () => {
  const f = await fixture()
  Object.assign(f.context, { cwd: f.folder, sessionManager: { getSessionFile: () => f.transcript } })
  await expect(runModel(f.backend, f.ctx, `label --out "${f.transcript}" --max-steps 1`, signal(), () => {})).rejects.toThrow('overwrite input session file')
  expect(f.calls).toEqual([]); expect(f.generations).toEqual([])
  expect(await readFile(f.transcript, 'utf8')).toBe(f.original)
})

test('deferred, tool-only and empty native replies are failures, never empty successful analyses', async () => {
  for (const stopReason of ['deferred', 'pending', 'toolUse', 'stop']) {
    const f = await fixture()
    f.answer(async () => ({ ...response(''), stopReason }))
    f.output({ errors: ['Provider did not return completed text'] })
    const result = await runModel(f.backend, f.ctx, 'analyze', signal(), () => {})
    expect(result.outcome).toBe('error')
    expect(f.replies[0]!.replies[0]!.error).toContain(stopReason === 'stop' ? 'empty response' : 'did not complete')
    expect(f.replies[0]!.replies[0]!.text).toBeUndefined()
  }
})

test('configured and explicit label models resolve through Pi modelRegistry.find', async () => {
  for (const explicit of [false, true]) {
    const f = await fixture()
    if (!explicit) f.options.labelModel = 'configured-provider/alternate-model'
    const result = await runModel(f.backend, f.ctx, `label --max-steps 1${explicit ? ' --model configured-provider/alternate-model' : ''}`, signal(), () => {})
    expect(f.generations[0]!.model).toBe(f.configuredModel)
    expect(result.text).toContain('configured-provider/alternate-model')
    const apply = f.calls.find(args => args[0] === 'llm-apply')!
    expect(apply.slice(-2)).toEqual(['--model', 'configured-provider/alternate-model'])
  }
})

test('unavailable and malformed configured models fail before provider calls', async () => {
  for (const [name, message] of [['missing', 'provider/model'], ['other/missing', 'unavailable']]) {
    const f = await fixture(); f.options.analysisModel = name
    await expect(runModel(f.backend, f.ctx, 'analyze', signal(), () => {})).rejects.toThrow(message)
    expect(f.generations).toEqual([])
  }
  const f = await fixture(); f.context.model = undefined as never
  await expect(runModel(f.backend, f.ctx, 'analyze', signal(), () => {})).rejects.toThrow('Select a Pi model')
})

test('large plans require interactive approval and rejected confirmation makes no provider calls', async () => {
  for (const ui of [false, true]) {
    const f = await fixture(5); f.context.hasUI = ui; f.confirm(async () => false)
    const result = await runModel(f.backend, f.ctx, 'analyze', signal(), () => {})
    expect(result.outcome).toBe(ui ? 'cancelled' : 'confirmation')
    expect(f.generations).toEqual([])
    expect(f.calls.map(args => args[0])).toEqual(['llm-prompt'])
  }
})

test('cancelling a large-plan confirmation closes the native dialog without starting model calls', async () => {
  const f = await fixture(5), controller = new AbortController()
  let opened!: () => void
  const began = new Promise<void>(resolve => { opened = resolve })
  let received: AbortSignal | undefined
  f.ctx.ui.confirm = async (_title, _message, options) => {
    received = options?.signal; opened()
    if (!received) return false
    return new Promise(resolve => received!.addEventListener('abort', () => resolve(false), { once: true }))
  }
  const pending = runModel(f.backend, f.ctx, 'analyze', controller.signal, () => {})
  await began
  controller.abort(new Error('Cancelled confirmation'))
  await expect(pending).rejects.toThrow('Cancelled confirmation')
  expect(received).toBe(controller.signal)
  expect(f.generations).toEqual([])
  expect(f.calls.map(args => args[0])).toEqual(['llm-prompt'])
})

test('host cancellation interrupts a model call and never applies partial replies', async () => {
  const f = await fixture(3), controller = new AbortController()
  let started!: () => void
  const began = new Promise<void>(resolve => { started = resolve })
  f.answer(call => new Promise((_resolve, reject) => {
    started(); call.options.signal.addEventListener('abort', () => reject(new Error('Cancelled provider')), { once: true })
  }))
  const pending = runModel(f.backend, f.ctx, 'analyze', controller.signal, () => {})
  await began; controller.abort()
  await expect(pending).rejects.toThrow()
  expect(f.generations).toHaveLength(1)
  expect(f.replies).toEqual([])
  expect(f.calls.some(args => args[0] === 'llm-apply')).toBe(false)
})

test('provider failures and timeouts reach bridge replay as indexed errors and mark the feature failed', async () => {
  const f = await fixture(3)
  let index = 0
  f.answer(async () => {
    if (index++ === 0) throw new Error('Provider unavailable')
    if (index === 2) return { ...response(), stopReason: 'aborted', errorMessage: 'Provider timeout' }
    return response()
  })
  f.output({ reply: 'Partial diagnosis', errors: ['Provider unavailable', 'Provider timeout'] })
  const result = await runModel(f.backend, f.ctx, 'fixes --limit 3', signal(), () => {})
  expect(f.replies[0]!.replies).toEqual([{ index: 0, error: 'Provider unavailable' }, { index: 1, error: 'Provider timeout' }, { index: 2, text: 'Controlled reply' }])
  expect(result.outcome).toBe('error')
  expect(result.text).toContain('Model errors:')
})

test('oversized responses cannot enter the persisted reply transaction', async () => {
  const f = await fixture(); f.answer(async () => response('x'.repeat(1024 * 1024 + 1)))
  f.output({ error: 'Model response exceeded 1 MiB.' })
  const result = await runModel(f.backend, f.ctx, 'analyze', signal(), () => {})
  expect(f.replies[0]!.replies[0]).toEqual({ index: 0, error: 'Model response exceeded 1 MiB.' })
  expect(result.outcome).toBe('error')
})

test('imported inputs are bundled before prompt planning and reused when the original changes', async () => {
  const f = await fixture()
  f.answer(async () => { await writeFile(f.transcript, 'Changed after prompt review'); return response() })
  const result = await runModel(f.backend, f.ctx, `analyze --session "${f.transcript}" explain`, signal(), () => {})
  expect(result.text).toContain('Controlled analysis')
  expect(f.calls[0]![0]).toBe('bundle')
  const prompt = f.calls.find(args => args[0] === 'llm-prompt')!, apply = f.calls.find(args => args[0] === 'llm-apply')!
  const frozen = prompt[prompt.indexOf('--transcript') + 1]!
  expect(frozen).not.toBe(f.transcript)
  expect(await readFile(frozen, 'utf8')).toBe(f.original)
  expect(apply[apply.indexOf('--transcript') + 1]).toBe(frozen)
})

test('an exact captured session id starting with a dash remains a bundle argument value', async () => {
  const f = await fixture()
  await runModel(f.backend, f.ctx, `analyze --session "${f.transcript}" --session-id=-captured-session explain`, signal(), () => {})
  const bundle = f.calls.find(args => args[0] === 'bundle')!
  expect(bundle).toContain('--session-id=-captured-session')
  expect(bundle).not.toContain('--session-id')
})

test('empty plans skip both provider and apply', async () => {
  const f = await fixture()
  f.backend.runtime.run = async <T>() => ({ empty: true, reason: 'No matching steps.' }) as T
  const result = await runModel(f.backend, f.ctx, 'fixes', signal(), () => {})
  expect(result.outcome).toBe('empty')
  expect(result.text).toBe('No matching steps.')
  expect(f.generations).toEqual([])
})

test('large disk-backed plans reject changed prompt hashes before calling a model', async () => {
  const f = await fixture()
  await writeFile(f.plan.path, JSON.stringify({ ...f.plan, hash: 'different-plan' }))
  const { prompts: _prompts, ...prepared } = f.plan
  f.backend.runtime.run = async <T>() => prepared as T
  await expect(runModel(f.backend, f.ctx, 'analyze', signal(), () => {})).rejects.toThrow('prompt plan changed')
  expect(f.generations).toEqual([])
})

test('per-call timeout becomes a reported provider error while a user cancellation stays separate', async () => {
  const f = await fixture()
  f.plan.prompts[0]!.timeout_s = 0.01
  f.answer(call => new Promise((_resolve, reject) => call.options.signal.addEventListener('abort', () => reject(new Error('Provider timeout')), { once: true })))
  f.output({ errors: ['Provider timeout'] })
  const result = await runModel(f.backend, f.ctx, 'analyze', signal(), () => {})
  expect(result.outcome).toBe('error')
  expect(f.replies[0]!.replies[0]).toEqual({ index: 0, error: 'Provider timeout' })
})

test('native labeling honors an explicitly requested sampling temperature', async () => {
  const f = await fixture()
  await runModel(f.backend, f.ctx, 'label --max-steps 1 --temperature 0.25', signal(), () => {})
  expect(f.generations[0]!.options.temperature).toBe(0.25)
})

test('environment mode uses environment provider defaults rather than the native session-model setting', async () => {
  const f = await fixture(1, 'env'); f.options.labelModel = 'session'; f.context.model = undefined as never
  const result = await runModel(f.backend, f.ctx, 'label --max-steps 1', signal(), () => {})
  expect(f.generations).toEqual([])
  const direct = f.calls.find(args => args[0] === 'label')!
  expect(direct).not.toContain('--model')
  expect(result.text).toContain('environment provider')
})

const root = path.resolve(import.meta.dir, '../..')
const python = process.env.TRAJVIZ_OBS_TEST_PYTHON ?? path.join(root, 'bridge/.venv/bin/python')
const hasBridge = existsSync(python) && Bun.spawnSync([python, '-I', '-c', 'import agent_trajviz']).exitCode === 0
function bridge<T>(argv: string[], abort: AbortSignal): Promise<T> {
  return new Promise((resolve, reject) => execFile(python, ['-I', path.join(root, 'bridge/trajviz_obs_cli.py'), ...argv],
    { signal: abort, timeout: 30000, maxBuffer: 4 * 1024 * 1024 }, (error, stdout) => {
      try {
        const doc = JSON.parse(stdout)
        error ? reject(new Error(doc.error?.message ?? error.message)) : resolve(doc)
      } catch { reject(new Error(error?.message ?? 'The bridge did not return JSON.')) }
    }))
}
async function piInput(file: string): Promise<void> {
  await writeFile(file, JSON.stringify([
    { type: 'session', version: 3, id: 'pi-native-model', timestamp: '2026-11-18T01:00:00Z', cwd: '/tmp' },
    { type: 'message', id: 'u', parentId: null, timestamp: '2026-11-18T01:00:00Z', message: { role: 'user', content: 'Fix addition' } },
    { type: 'message', id: 'a', parentId: 'u', timestamp: '2026-11-18T01:00:01Z', message: { role: 'assistant', provider: 'test', model: 'selected', content: [{ type: 'text', text: 'Fixed and tested addition.' }], usage: { input: 100, output: 10, cacheRead: 0, cacheWrite: 0, totalTokens: 110 } } },
  ]))
}
test.skipIf(!hasBridge)('real bridge refuses label output aliases to the original imported source before model calls', async () => {
  const f = await fixture()
  f.backend.runtime.run = bridge
  await piInput(f.transcript)
  const original = await readFile(f.transcript, 'utf8')
  await expect(runModel(f.backend, f.ctx, `label --session "${f.transcript}" --out "${f.transcript}" --max-steps 1`, signal(), () => {})).rejects.toThrow('overwrite input session file')
  expect(f.generations).toEqual([])
  expect(await readFile(f.transcript, 'utf8')).toBe(original)
})

test.skipIf(!hasBridge)('real Pi prompt and apply transaction uses the frozen capture if the source changes during generation', async () => {
  const f = await fixture()
  f.backend.runtime.run = bridge
  await piInput(f.transcript)
  f.answer(async () => {
    await writeFile(f.transcript, 'Changed after prompt review')
    return response('Controlled diagnosis from native Pi model.')
  })
  const result = await runModel(f.backend, f.ctx, `analyze --session "${f.transcript}" explain the session`, signal(), () => {})
  expect(result.outcome).toBeUndefined()
  expect(result.text).toContain('Controlled diagnosis from native Pi model.')
  expect(f.generations).toHaveLength(1)
  expect(await readFile(f.transcript, 'utf8')).toBe('Changed after prompt review')
  const saved = JSON.parse(await readFile(path.join(f.folder, 'analyze.json'), 'utf8'))
  expect(JSON.stringify(saved)).toContain('Controlled diagnosis from native Pi model.')
})
