import { createHash, randomUUID } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import { setTimeout as delay } from 'node:timers/promises'
import path from 'node:path'
import type { Backend } from './backend.js'
import { atomicJSON, atomicFile } from './runtime.js'
import { record } from './collect.js'
import { analyzeSelection, parseArgs, valueArgv } from './shared/args.js'
import type { CommandResult, RunInput } from './rpc.js'
import { inspectSource } from './source-info.js'

type Prompt = { index: number; system: string; prompt: string; max_tokens: number; timeout_s: number; delay_s?: number }
type Plan = { feature: string; hash: string; plan: {calls:number;approx_input_tokens:number}; prompts?: Prompt[]; path: string }
type Pending = { runID: string; input: string; current: string; plan: Plan; argv: string[]; feature: string; model?: {providerID:string;id:string;variant?:string}; artifacts: string; created: number; line: string; labelModel?: string }
const confirmations = new WeakMap<Backend, Map<string, Pending>>()

export function cancelModel(backend: Backend, runID?: string): boolean {
  const cache = confirmations.get(backend)
  if (!cache) return false
  let cancelled = false
  for (const [key, pending] of cache) if (!runID || pending.runID === runID) { cache.delete(key); cancelled = true }
  return cancelled
}

async function request(backend: Backend, input: RunInput, signal: AbortSignal): Promise<Pending | CommandResult> {
  const parsed = parseArgs(input.args), {sub:feature,flags} = parsed
  if (!['analyze', 'fixes', 'label'].includes(feature)) throw new Error('Unknown model feature.')
  const analyze = feature === 'analyze' ? analyzeSelection(input.args) : undefined
  const selector = analyze ? analyze.session : typeof flags.session === 'string' ? flags.session : undefined
  const captureSessionID = analyze ? analyze.captureSessionId : typeof flags['session-id'] === 'string' ? flags['session-id'] : undefined
  const source = await backend.session(input.sessionID, selector, signal, captureSessionID)
  const extras: string[] = []
  if (feature === 'analyze') {
    if (analyze?.question) extras.push('--question', analyze.question)
  } else if (feature === 'fixes') {
    if (flags.limit !== undefined) extras.push('--limit', String(flags.limit))
  } else {
    const steps = flags['max-steps'] ?? backend.options.labelMaxSteps ?? 50
    if (!/^\d+$/.test(String(steps)) || Number(steps) < 1) throw new Error('--max-steps must be a positive integer.')
    extras.push('--max-steps', String(steps))
    for (const name of ['taxonomy', 'max-content-chars', 'max-tokens', 'temperature', 'delay', 'user-phase', 'user-action']) if (flags[name] !== undefined) extras.push(`--${name}`, String(flags[name]))
    if (flags['assistant-only'] === true) extras.push('--assistant-only')
    const out = flags.out ?? flags.o
    if (out !== undefined) extras.push('--out', String(out))
    if (backend.options.llm !== 'env' && (flags['max-tokens'] !== undefined || flags.temperature !== undefined)) throw new Error('--max-tokens and --temperature require llm=env; OpenCode 2.2.2 native generation uses the host model settings.')
  }
  if (feature === 'label' && (flags.out !== undefined || flags.o !== undefined)) {
    // Validate against the original family before replay uses its immutable copy.
    // This keeps output aliases and hard links from overwriting a source or child log.
    await backend.runtime.run(['llm-prompt', ...source.argv, ...extras, '--feature', feature], signal)
  }
  // Freeze imported captures through the bundle writer: a directory or manifest
  // can reference companion traces/logs that a single-file copy would lose.
  let frozen: string
  const inspected = await inspectSource(source.transcript)
  if (inspected.compound) {
    frozen = path.join(source.artifacts, 'inputs', `${randomUUID()}.zip`)
    const bundle = await backend.runtime.run<{path:string}>(['bundle', '--transcript', source.transcript, '--artifacts', source.artifacts, ...(source.captureSessionID === undefined ? [] : valueArgv('session-id', source.captureSessionID)), '--out', frozen], signal, 600_000)
    frozen = bundle.path
  } else {
    frozen = path.join(source.artifacts, 'inputs', `${createHash('sha256').update(inspected.bytes!).digest('hex')}${path.extname(source.transcript) || '.json'}`)
    await atomicFile(frozen, inspected.bytes!)
  }
  const argv = source.argv.map(value => value === source.transcript ? frozen : value)
  argv.push(...extras)
  const response = await backend.runtime.run<Plan | {empty:true;reason:string}>(['llm-prompt', ...argv, '--feature', feature], signal)
  if ('empty' in response) return { text: response.reason }
  let model: Pending['model']
  const explicit = feature === 'label' && typeof flags.model === 'string' ? flags.model : undefined
  const configured = explicit ?? (backend.options.llm === 'env' ? undefined : feature === 'label' ? backend.options.labelModel : backend.options.analysisModel)
  if (backend.options.llm !== 'env') {
    const session = input.sessionID === 'home' ? {} : record(await backend.ctx.session.get({sessionID:input.sessionID} as Parameters<Backend['ctx']['session']['get']>[0]))
    model = input.model ?? (Object.keys(record(session.model)).length ? record(session.model) as Pending['model'] : undefined)
    if (configured && configured !== 'session') {
      const slash = configured.indexOf('/')
      if (slash <= 0 || slash === configured.length - 1) throw new Error('Model names use provider/model, or session.')
      model = { providerID: configured.slice(0, slash), id: configured.slice(slash + 1) }
    }
    if (!model) {
      const fallback = await backend.ctx.model.default()
      if (fallback.data) model = { providerID: fallback.data.providerID, id: fallback.data.id }
    }
    if (!model) throw new Error('Select an OpenCode model before using analysis or labeling.')
  }
  const labelModel = model ? `${model.providerID}/${model.id}${model.variant ? `@${model.variant}` : ''}` : configured
  const line = `${feature}: ${response.plan.calls} model call${response.plan.calls === 1 ? '' : 's'} · approximately ${response.plan.approx_input_tokens.toLocaleString('en-US')} input tokens · ${labelModel ?? 'environment provider'}`
  return { runID: input.runID, input: input.args, current: input.sessionID, plan: response, argv, feature, model, artifacts: source.artifacts, created: Date.now(), line, labelModel }
}

export async function runModel(backend: Backend, input: RunInput, signal: AbortSignal): Promise<CommandResult> {
  let pending: Pending
  const cache = confirmations.get(backend) ?? new Map<string, Pending>()
  confirmations.set(backend, cache)
  for (const [id, value] of cache) if (Date.now() - value.created > 15 * 60_000) cache.delete(id)
  if (input.confirmationID) {
    const saved = cache.get(input.confirmationID)
    cache.delete(input.confirmationID)
    if (!saved || saved.input !== input.args || saved.current !== input.sessionID || saved.runID !== input.runID) throw new Error('This model plan expired or belongs to another request. Run the command again.')
    pending = saved
  } else {
    const prepared = await request(backend, input, signal)
    if ('text' in prepared) return prepared
    pending = prepared
    await backend.progress(input.runID, pending.line)
    if (pending.plan.plan.calls > 4) {
      while (cache.size >= 8) cache.delete(cache.keys().next().value!)
      const id = randomUUID(); cache.set(id, pending)
      return { text: pending.line, confirmation: { id, title: 'Run model analysis?', message: `${pending.line}\nThis uses your configured model provider and may incur usage charges.` } }
    }
  }
  signal.throwIfAborted()
  await backend.progress(input.runID, pending.line)
  const {plan,feature,argv,model,artifacts,line,labelModel} = pending
  let doc: Record<string, unknown>
  if (backend.options.llm === 'env') {
    doc = await backend.runtime.run([feature === 'analyze' ? 'ask' : feature, ...argv, ...(feature === 'label' && labelModel ? ['--model', labelModel] : [])], signal, 600_000)
  } else {
    const captured = plan.prompts ? plan : JSON.parse(await readFile(plan.path, 'utf8')) as Plan
    if (captured.hash !== plan.hash || !Array.isArray(captured.prompts) || captured.prompts.length !== plan.plan.calls) throw new Error('The model prompt plan changed. Run the command again.')
    const replies: {index:number;text?:string;error?:string}[] = []
    for (const prompt of captured.prompts) {
      signal.throwIfAborted()
      if (prompt.delay_s) await delay(prompt.delay_s * 1000, undefined, { signal })
      await backend.progress(input.runID, `${line}\nRunning call ${replies.length + 1} of ${captured.prompts.length}…`)
      try {
        const result = await backend.ctx.experimental.generate.text({ model, prompt: `${prompt.system ? `${prompt.system}\n\n` : ''}${prompt.prompt}` } as Parameters<Backend['ctx']['experimental']['generate']['text']>[0], { signal: AbortSignal.any([signal, AbortSignal.timeout(prompt.timeout_s * 1000)]) })
        if (Buffer.byteLength(result.text) > 1024 * 1024) throw new Error('Model response exceeded 1 MiB.')
        replies.push({ index: prompt.index, text: result.text })
      } catch (error) {
        signal.throwIfAborted()
        replies.push({ index: prompt.index, error: error instanceof Error ? error.message : String(error) })
      }
    }
    const file = path.join(artifacts, 'replies', `${randomUUID()}.json`)
    await atomicJSON(file, { hash: plan.hash, replies })
    doc = await backend.runtime.run(['llm-apply', ...argv, '--feature', feature, '--replies', file, ...(feature === 'label' && labelModel ? ['--model', labelModel] : [])], signal, 600_000)
  }
  const text = typeof doc.reply === 'string' ? doc.reply : typeof doc.text === 'string' ? doc.text : JSON.stringify(doc, null, 2)
  const errors = [typeof doc.error === 'string' ? doc.error : '', ...(Array.isArray(doc.errors) ? doc.errors.map(String) : [])].filter(Boolean)
  return { text: `${line}\n${text}${errors.length ? `\nModel errors: ${errors.join('; ')}` : ''}${typeof doc.path === 'string' ? `\nSaved to ${doc.path}` : ''}`, data: doc, ...(typeof doc.path === 'string' ? {path:doc.path} : {}) }
}
