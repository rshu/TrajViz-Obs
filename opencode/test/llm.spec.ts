import { afterEach, expect, test } from 'bun:test'
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import type { Backend } from '../src/backend.js'
import type { RunInput } from '../src/rpc.js'
import { cancelModel, runModel } from '../src/llm.js'

const folders: string[] = []
afterEach(async () => { for (const folder of folders.splice(0)) await rm(folder, { recursive: true, force: true }) })

async function fixture(count: number, mode: 'session' | 'env' = 'session') {
  const folder = await mkdtemp(path.join(tmpdir(), 'trajviz-model-test-')); folders.push(folder)
  const transcript = path.join(folder, 'source.json'), original = JSON.stringify({ controlled: 'Original reviewed source' })
  await writeFile(transcript, original)
  const calls: string[][] = [], generations: { model: unknown; prompt: string }[] = [], progress: string[] = []
  const prompts = Array.from({ length: count }, (_, index) => ({ index, system: 'Controlled system', prompt: `Controlled request ${index}`, max_tokens: 256, timeout_s: 5 }))
  const plan = { feature: 'analyze', hash: 'controlled-plan-hash', path: path.join(folder, 'plan.json'), plan: { calls: count, approx_input_tokens: count * 100 }, prompts }
  const replies: unknown[] = []
  const backend = {
    options: { llm: mode },
    async progress(_id: string, line: string) { progress.push(line) },
    async session() { return { sessionID: 'ses_test', transcript, artifacts: folder, argv: ['--transcript', transcript, '--artifacts', folder] } },
    runtime: { async run<T>(argv: string[]): Promise<T> {
      calls.push(argv)
      if (argv[0] === 'llm-prompt') return plan as T
      if (argv[0] === 'llm-apply') replies.push(JSON.parse(await readFile(argv[argv.indexOf('--replies') + 1]!, 'utf8')))
      return { reply: 'Controlled analysis', path: path.join(folder, 'analysis.json') } as T
    } },
    ctx: { session: { async get() { return { model: { providerID: 'test', id: 'native' } } } },
      experimental: { generate: { async text(input: {model:unknown;prompt:string}, _options: {signal:AbortSignal}) {
        generations.push(input); return { text: 'Controlled reply' }
      } } },
    },
  } as unknown as Backend
  const input: RunInput = { sessionID: 'ses_test', args: 'analyze Explain the failure', runID: 'run_test', model: { providerID: 'test', id: 'selected' } }
  return { backend, input, folder, transcript, original, plan, calls, generations, progress, replies }
}

test('a large model plan previews and cancels with zero model calls', async () => {
  const f = await fixture(5)
  const preview = await runModel(f.backend, f.input, new AbortController().signal)
  expect(preview.text).toContain('5 model calls')
  expect(preview.confirmation).toBeDefined()
  expect(f.generations).toEqual([])
  expect(f.calls.map(c => c[0])).toEqual(['llm-prompt'])
  expect(cancelModel(f.backend, f.input.runID)).toBe(true)
  await expect(runModel(f.backend, { ...f.input, confirmationID: preview.confirmation!.id }, new AbortController().signal)).rejects.toThrow('expired')
  expect(f.generations).toEqual([])
})

test('confirmed replay uses frozen input and the reviewed model even after the live source changes', async () => {
  const f = await fixture(5)
  const preview = await runModel(f.backend, f.input, new AbortController().signal)
  const frozen = f.calls[0]![f.calls[0]!.indexOf('--transcript') + 1]!
  expect(frozen).not.toBe(f.transcript)
  expect(await readFile(frozen, 'utf8')).toBe(f.original)
  await writeFile(f.transcript, 'Changed live source')
  const result = await runModel(f.backend, { ...f.input, confirmationID: preview.confirmation!.id, model: {providerID:'different',id:'different'} }, new AbortController().signal)
  expect(f.generations).toHaveLength(5)
  expect(f.generations[0]!.model).toEqual({ providerID: 'test', id: 'selected' })
  expect(await readFile(frozen, 'utf8')).toBe(f.original)
  expect(await readFile(f.transcript, 'utf8')).toBe('Changed live source')
  expect(f.calls[1]![f.calls[1]!.indexOf('--transcript') + 1]).toBe(frozen)
  expect(result.text).toContain('Controlled analysis')
  await expect(runModel(f.backend, { ...f.input, confirmationID: preview.confirmation!.id }, new AbortController().signal)).rejects.toThrow('expired')
  expect(f.generations).toHaveLength(5)
})

test('confirmation is bound to its session, command, and run', async () => {
  for (const change of [{ sessionID: 'ses_other' }, { args: 'label --max-steps 5' }, { runID: 'other_run' }]) {
    const f = await fixture(5)
    const preview = await runModel(f.backend, f.input, new AbortController().signal)
    await expect(runModel(f.backend, { ...f.input, ...change, confirmationID: preview.confirmation!.id }, new AbortController().signal)).rejects.toThrow('another request')
    expect(f.generations).toEqual([])
  }
})

test('cancelling during generation stops subsequent calls and never applies partial replies', async () => {
  const f = await fixture(3), controller = new AbortController()
  let began!: () => void
  const started = new Promise<void>(resolve => { began = resolve })
  f.backend.ctx.experimental.generate.text = (async (_input: unknown, options: {signal:AbortSignal}) => {
    began()
    await new Promise<void>((_resolve, reject) => options.signal.addEventListener('abort', () => reject(new Error('Controlled cancellation')), { once: true }))
    return { text: 'Unexpected reply' }
  }) as typeof f.backend.ctx.experimental.generate.text
  const pending = runModel(f.backend, f.input, controller.signal)
  await started; controller.abort()
  await expect(pending).rejects.toThrow()
  expect(f.calls.map(c => c[0])).toEqual(['llm-prompt'])
  expect(f.replies).toEqual([])
})

test('generation failures become explicit error replies for the shared transaction validator', async () => {
  const f = await fixture(1)
  f.backend.ctx.experimental.generate.text = (async () => { throw new Error('Controlled provider failure') }) as typeof f.backend.ctx.experimental.generate.text
  await runModel(f.backend, f.input, new AbortController().signal)
  expect(f.replies).toEqual([{ hash: 'controlled-plan-hash', replies: [{ index: 0, error: 'Controlled provider failure' }] }])
})

test('native mode refuses unsupported generation overrides before calling a model', async () => {
  const f = await fixture(1)
  await expect(runModel(f.backend, { ...f.input, args: 'label --max-tokens 300' }, new AbortController().signal)).rejects.toThrow('require llm=env')
  expect(f.generations).toEqual([])
  expect(f.calls).toEqual([])
})

test('env mode uses the reviewed frozen input through the Python provider', async () => {
  const f = await fixture(1, 'env')
  await runModel(f.backend, f.input, new AbortController().signal)
  expect(f.generations).toEqual([])
  expect(f.calls.map(c => c[0])).toEqual(['llm-prompt', 'ask'])
  expect(f.calls[1]![f.calls[1]!.indexOf('--transcript') + 1]).not.toBe(f.transcript)
})

test('environment labeling ignores host model options and honors only its explicit override', async () => {
  const f = await fixture(1, 'env')
  f.backend.options.labelModel = 'host/expensive'
  await runModel(f.backend, {...f.input,args:'label --max-steps 1'},new AbortController().signal)
  const direct = f.calls.find(c=>c[0]==='label')!
  expect(direct).not.toContain('--model')
  expect(f.generations).toEqual([])
  const g = await fixture(1, 'env')
  await runModel(g.backend, {...g.input,args:'label --max-steps 1 --model environment-model'},new AbortController().signal)
  const override = g.calls.find(c=>c[0]==='label')!
  expect(override[override.indexOf('--model')+1]).toBe('environment-model')
})

test('analysis questions containing command flags do not change the selected model', async () => {
  const f = await fixture(1)
  await runModel(f.backend,{...f.input,args:'analyze Why did --model other/provider fail?'},new AbortController().signal)
  expect(f.generations[0]!.model).toEqual({providerID:'test',id:'selected'})
  expect(f.generations[0]!.prompt).toContain('Controlled request')
})

// A valid stored ZIP containing a UTF-8 JSON document. Its binary headers
// include invalid UTF-8 bytes, so a string-based copy corrupts this archive.
const ZIP = Buffer.from('UEsDBBQAAAAAAAAAIVyOI6hHEQAAABEAAAALAAAAc291cmNlLmpzb257InRleHQiOiLkvaDlpb0ifVBLAQIUAxQAAAAAAAAAIVyOI6hHEQAAABEAAAALAAAAAAAAAAAAAACAAQAAAABzb3VyY2UuanNvblBLBQYAAAAAAQABADkAAAA6AAAAAAA=', 'base64')

test('binary ZIP input is frozen byte for byte before a model plan is reviewed', async () => {
  const f = await fixture(5), source = path.join(f.folder, 'session.zip')
  await writeFile(source, ZIP)
  f.backend.session = async () => ({ sessionID: 'ses_test', transcript: source, artifacts: f.folder,
    argv: ['--transcript', source, '--artifacts', f.folder] })
  const preview = await runModel(f.backend, f.input, new AbortController().signal)
  expect(preview.confirmation).toBeDefined()
  const plan = f.calls.find(call => call[0] === 'llm-prompt')!
  const frozen = plan[plan.indexOf('--transcript') + 1]!
  expect(frozen).not.toBe(source)
  expect(frozen).toEndWith('.zip')
  expect(await readFile(frozen)).toEqual(ZIP)
  expect(await readFile(source)).toEqual(ZIP)
  expect(f.generations).toEqual([])
  cancelModel(f.backend)
})

test('JSONL families pass through the shared bundle boundary before the frozen model plan', async () => {
  const f = await fixture(5), source = path.join(f.folder, 'session.jsonl')
  const content = '{"type":"user","message":{"role":"user","content":"Controlled"}}\n'
  await writeFile(source, content)
  const child = path.join(f.folder, 'session', 'subagents', 'agent-child.jsonl')
  await mkdir(path.dirname(child), { recursive: true }); await writeFile(child, content)
  f.backend.session = async () => ({ sessionID: 'ses_test', transcript: source, artifacts: f.folder,
    argv: ['--transcript', source, '--artifacts', f.folder, '--kind', 'current'] })
  const originalRun = f.backend.runtime.run.bind(f.backend.runtime)
  f.backend.runtime.run = async <T>(argv: readonly string[]): Promise<T> => {
    if (argv[0] !== 'bundle') return await originalRun(argv) as T
    f.calls.push([...argv])
    const output = argv[argv.indexOf('--out') + 1]!
    await mkdir(path.dirname(output), { recursive: true }); await writeFile(output, ZIP)
    return { path: output } as T
  }
  await runModel(f.backend, f.input, new AbortController().signal)
  expect(f.calls.map(call => call[0])).toEqual(['bundle', 'llm-prompt'])
  const bundle = f.calls[0]!, plan = f.calls[1]!
  expect(bundle.slice(0, 5)).toEqual(['bundle', '--transcript', source, '--artifacts', f.folder])
  expect(bundle).not.toContain('--kind')
  expect(plan[plan.indexOf('--transcript') + 1]).toBe(bundle[bundle.indexOf('--out') + 1])
  expect(plan).toContain('--kind')
  expect(await readFile(source, 'utf8')).toBe(content)
  expect(await readFile(child, 'utf8')).toBe(content)
  expect(f.generations).toEqual([])
  cancelModel(f.backend)
})

test('label output preflight validates the original source before any model call or replay plan', async () => {
  const f = await fixture(1)
  f.backend.runtime.run = async <T>(argv: readonly string[]): Promise<T> => {
    f.calls.push([...argv])
    throw new Error('Output path would overwrite input session source')
  }
  await expect(runModel(f.backend, { ...f.input, args: `label -o "${f.transcript}"` },
    new AbortController().signal)).rejects.toThrow('overwrite')
  expect(f.calls).toHaveLength(1)
  expect(f.calls[0]![0]).toBe('llm-prompt')
  expect(f.calls[0]![f.calls[0]!.indexOf('--transcript') + 1]).toBe(f.transcript)
  expect(f.calls[0]![f.calls[0]!.indexOf('--out') + 1]).toBe(f.transcript)
  expect(f.generations).toEqual([])
  expect(f.replies).toEqual([])
  expect(await readFile(f.transcript, 'utf8')).toBe(f.original)
})
