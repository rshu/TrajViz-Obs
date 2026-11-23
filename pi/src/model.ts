import { randomUUID } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import path from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import type { ExtensionContext } from '@earendil-works/pi-coding-agent'
import { analyzeSelection, parseArgs, valueArgv } from '../../hooks/lib/args.js'
import { atomicJSON } from '../../opencode/src/runtime.js'
import { inspectSource } from '../../opencode/src/source-info.js'
import type { Backend } from './backend.js'
import type { Outcome } from './telemetry.js'
import { protectNativeOutput } from './outputs.js'

type Prompt = { index: number; system: string; prompt: string; max_tokens: number; timeout_s: number; delay_s?: number }
type Plan = { hash: string; plan: { calls: number; approx_input_tokens: number }; prompts?: Prompt[]; path: string }
export type Result = { text: string; outcome?: Outcome; path?: string; data?: unknown }
export type ModelConfirmation = (title: string, message: string, signal: AbortSignal) => Promise<boolean>
export async function runModel(backend: Backend, ctx: ExtensionContext, input: string, signal: AbortSignal, progress: (text: string) => void,
  confirm: ModelConfirmation = (title, message, signal) => ctx.ui.confirm(title, message, { signal })): Promise<Result> {
  const { sub: feature, flags } = parseArgs(input)
  if (feature === 'label') await protectNativeOutput(ctx, [flags.out, flags.o])
  const selection = feature === 'analyze' ? analyzeSelection(input) : undefined
  const selector = selection?.session ?? (typeof flags.session === 'string' ? flags.session : undefined)
  const capturedID = selection?.captureSessionId ?? (typeof flags['session-id'] === 'string' ? flags['session-id'] : undefined)
  const source = await backend.source(ctx, selector, signal, capturedID)
  const extras: string[] = []
  if (feature === 'analyze' && selection?.question) extras.push(...valueArgv('question', selection.question))
  if (feature === 'fixes' && flags.limit !== undefined) extras.push('--limit', String(flags.limit))
  if (feature === 'label') {
    extras.push('--max-steps', String(flags['max-steps'] ?? backend.options.labelMaxSteps ?? 50))
    for (const key of ['taxonomy', 'max-content-chars', 'max-tokens', 'temperature', 'delay', 'user-phase', 'user-action']) if (flags[key] !== undefined) extras.push(...valueArgv(key, String(flags[key])))
    if (flags['assistant-only']) extras.push('--assistant-only')
    if (flags.out ?? flags.o) extras.push(...valueArgv('out', String(flags.out ?? flags.o)))
    // Validate user-selected output against the actual input before freezing it.
    await backend.runtime.run(['llm-prompt', ...source.argv, ...extras, '--feature', feature], signal)
  }
  let argv = [...source.argv, ...extras]
  if (selector) {
    const inspected = await inspectSource(source.transcript)
    const frozen = path.join(source.artifacts, 'inputs', `${randomUUID()}.zip`)
    // Bundle compound imports, including their companion logs. Plain files can also
    // use the bundle contract, which preserves source hashes and output protections.
    if (inspected.compound || inspected.bytes) {
      const bundle = await backend.runtime.run<{ path: string }>(['bundle', '--transcript', source.transcript, '--artifacts', source.artifacts,
        ...(source.captureSessionID ? valueArgv('session-id', source.captureSessionID) : []), '--out', frozen], signal, 600_000)
      argv = argv.map(value => value === source.transcript ? bundle.path : value)
    }
  }
  const prepared = await backend.runtime.run<Plan | { empty: true; reason: string }>(['llm-prompt', ...argv, '--feature', feature], signal)
  if ('empty' in prepared) return { text: prepared.reason, outcome: 'empty' }
  const explicit = feature === 'label' && typeof flags.model === 'string' ? flags.model : undefined
  const configured = explicit ?? (backend.options.llm === 'env' ? undefined : feature === 'label' ? backend.options.labelModel : backend.options.analysisModel)
  let model = ctx.model
  if (backend.options.llm !== 'env' && configured && configured !== 'session') {
    const slash = configured.indexOf('/')
    if (slash <= 0) throw new Error('Model names use provider/model, or session.')
    model = ctx.modelRegistry.find(configured.slice(0, slash), configured.slice(slash + 1))
    if (!model) throw new Error(`The configured ${feature} model is unavailable. Select a model in Pi.`)
  }
  if (backend.options.llm !== 'env' && !model) throw new Error('Select a Pi model before using AI analysis or labeling.')
  const modelName = backend.options.llm === 'env' ? configured : `${model!.provider}/${model!.id}`
  const line = `${feature}: ${prepared.plan.calls} model call${prepared.plan.calls === 1 ? '' : 's'} · approximately ${prepared.plan.approx_input_tokens.toLocaleString()} input tokens · ${modelName ?? 'environment provider'}`
  progress(line)
  if (prepared.plan.calls > 4) {
    if (!ctx.hasUI) return { text: `${line}\nMore than four calls require an interactive confirmation. Reduce --max-steps or --limit.`, outcome: 'confirmation' }
    const accepted = await confirm('Run model analysis?', `${line}\nThis uses your model provider and may incur usage charges.`, signal)
    signal.throwIfAborted()
    if (!accepted) return { text: 'Model analysis cancelled.', outcome: 'cancelled' }
  }
  signal.throwIfAborted()
  let doc: Record<string, unknown>
  if (backend.options.llm === 'env') {
    doc = await backend.runtime.run([feature === 'analyze' ? 'ask' : feature, ...argv, ...(feature === 'label' && modelName ? ['--model', modelName] : [])], signal, 600_000)
  } else {
    const plan: Plan = prepared.prompts ? prepared : JSON.parse(await readFile(prepared.path, 'utf8'))
    if (plan.hash !== prepared.hash || !plan.prompts || plan.prompts.length !== plan.plan.calls) throw new Error('Model prompt plan changed. Run the command again.')
    const replies: { index: number; text?: string; error?: string }[] = []
    const usage: unknown[] = []
    for (const prompt of plan.prompts) {
      signal.throwIfAborted()
      if (prompt.delay_s) await delay(prompt.delay_s * 1000, undefined, { signal })
      progress(`${line}\nRunning call ${replies.length + 1} of ${plan.prompts.length}…`)
      try {
        const stream = ctx.modelRegistry.streamSimple(model!, { systemPrompt: prompt.system,
          messages: [{ role: 'user', content: prompt.prompt, timestamp: Date.now() }] },
        { maxTokens: prompt.max_tokens, ...(typeof flags.temperature === 'string' ? { temperature: Number(flags.temperature) } : {}),
          signal: AbortSignal.any([signal, AbortSignal.timeout(prompt.timeout_s * 1000)]) })
        const response = await stream.result()
        if (!['stop', 'length'].includes(response.stopReason)) throw new Error(response.errorMessage ?? `Pi model call did not complete (${response.stopReason}).`)
        const text = response.content.filter(part => part.type === 'text').map(part => part.text).join('\n')
        if (!text.trim()) throw new Error('Pi model returned an empty response.')
        if (Buffer.byteLength(text) > 1024 * 1024) throw new Error('Model response exceeded 1 MiB.')
        replies.push({ index: prompt.index, text }); usage.push(response.usage)
      } catch (error) {
        signal.throwIfAborted()
        replies.push({ index: prompt.index, error: error instanceof Error ? error.message : String(error) })
      }
    }
    const file = path.join(source.artifacts, 'replies', `${randomUUID()}.json`)
    await atomicJSON(file, { hash: plan.hash, replies, usage })
    doc = await backend.runtime.run(['llm-apply', ...argv, '--feature', feature, '--replies', file, ...(feature === 'label' && modelName ? ['--model', modelName] : [])], signal, 600_000)
  }
  const errors = [doc.error, ...(Array.isArray(doc.errors) ? doc.errors : [])].filter(Boolean)
  const saved = typeof doc.path === 'string' ? doc.path : typeof doc.history_path === 'string' ? doc.history_path : undefined
  return { text: `${line}\n${doc.reply ?? doc.text ?? JSON.stringify(doc, null, 2)}${errors.length ? `\nModel errors: ${errors.join('; ')}` : ''}${saved ? `\nSaved to ${saved}` : ''}`,
    data: doc, ...(saved ? { path: saved } : {}), ...(errors.length ? { outcome: 'error' as const } : {}) }
}
