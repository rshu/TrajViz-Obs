// TrajViz's LLM features through the session's own model: the bridge's
// `llm-prompt` plan, its calls run one by one through `$.model.complete`,
// the replies for `llm-apply`. Every helper taking `$` is a function
// declaration (validate follows `$` into one).
import type { EngineInterface, ModelEffort, PluginOptions } from 'claude-code'
import { fmtCompact, fmtInt } from './format'

export type Feature = 'analyze' | 'fixes' | 'label'

/** One prompt of a plan, as TrajViz would send it. */
export type PlannedPrompt = { index: number; system: string; prompt: string; max_tokens: number; timeout_s: number }

/**
 * The bridge's `llm-prompt` document: the calls the feature makes and what
 * they cost, the hash `llm-apply` checks the replies against, and the
 * prompts themselves (left out, and read from `path`, past 256 KB).
 */
export type PromptPlan = {
  feature: Feature
  hash: string
  plan: { calls: number; approx_input_tokens: number }
  prompts?: PlannedPrompt[]
  path: string
  bytes: number
}

/** The model's answer to one prompt, or why there is none, as `llm-apply` takes it. */
export type Reply = { index: number; text: string } | { index: number; error: string }

/** The summed cost of a plan's calls. */
export type Usage = { input: number; cache_read: number; cache_write: number; output: number }

export type RunResult = { replies: Reply[]; usage: Usage }

/** The most calls run without asking in an interactive session. */
export const ASK_ABOVE = 4

/** The ask's option that runs the plan; the other, a dismissal or free text is no. */
const RUN = 'Run'

/** `<feature>: <n> calls · ~<tokens> input tokens · model <m>`: the row's first line, printed before anything runs. */
export function planLine(plan: PromptPlan, model: string): string {
  const { calls, approx_input_tokens } = plan.plan
  return `${plan.feature}: ${fmtInt(calls)} call${calls === 1 ? '' : 's'} · ~${fmtCompact(approx_input_tokens)} input tokens · model ${model}`
}

/** The summed cost as the row's last line. */
export function usageLine(usage: Usage): string {
  return `usage: ${fmtInt(usage.input)} input · ${fmtInt(usage.cache_read)} cache read · ${fmtInt(usage.cache_write)} cache write · ${fmtInt(usage.output)} output tokens`
}

/** `options[name]` as a non-empty string, else `fallback`. */
const optionModel = (options: PluginOptions, name: string, fallback: string): string => {
  const v = options[name]
  return typeof v === 'string' && v.trim() ? v.trim() : fallback
}

/**
 * The model `feature` runs on: `labelModel` (default `haiku`) for `label`,
 * `analysisModel` (default `sonnet`) otherwise; `session` is the main
 * loop's model as `/model` shows it.
 */
export async function resolveModel($: EngineInterface, options: PluginOptions, feature: Feature): Promise<string> {
  const model = feature === 'label' ? optionModel(options, 'labelModel', 'haiku') : optionModel(options, 'analysisModel', 'sonnet')
  return model === 'session' ? $.session.model() : model
}

/** The plan's prompts: inline, or from the file the bridge wrote when they were too large to inline. */
export async function loadPrompts($: EngineInterface, plan: PromptPlan): Promise<PlannedPrompt[]> {
  if (plan.prompts) return plan.prompts
  const doc = JSON.parse(await $.fs.read(plan.path)) as Partial<PromptPlan>
  if (doc.hash !== plan.hash || doc.feature !== plan.feature) throw new Error('The prompt plan changed while it was being read; run the command again.')
  if (!Array.isArray(doc.prompts) || doc.prompts.length !== plan.plan.calls) throw new Error('The prompt plan file is incomplete; run the command again.')
  return doc.prompts
}

/** Whether the person lets `calls` model calls run (a dismissed ask, which rejects, is no). */
export async function confirmCalls($: EngineInterface, calls: number): Promise<boolean> {
  try {
    return (await $.ui.ask(`Run ${fmtInt(calls)} model calls?`, [RUN, 'Cancel'])) === RUN
  } catch {
    return false
  }
}

const sum = (into: Usage, u: { input_tokens: number; cache_read_input_tokens: number; cache_creation_input_tokens: number; output_tokens: number }): void => {
  into.input += u.input_tokens
  into.cache_read += u.cache_read_input_tokens
  into.cache_write += u.cache_creation_input_tokens
  into.output += u.output_tokens
}

/**
 * Runs the plan's prompts through `$.model.complete`, one after another,
 * each with TrajViz's own cap and time limit. With `confirm` (an interactive
 * session) a plan over `ASK_ABOVE` calls is asked first; anything but `Run`
 * answers null and nothing is sent. At most `cap` prompts are run (a
 * headless session's `labelMaxSteps`). A request that got no reply is
 * `{ index, error: '<reason> <status?>' }` for TrajViz to report in its own
 * words; `usage` sums every call's cost.
 */
export async function runPlan(
  $: EngineInterface,
  plan: PromptPlan,
  model: string,
  opts: { effort?: ModelEffort; confirm: boolean; cap: number },
): Promise<RunResult | null> {
  const prompts = (await loadPrompts($, plan)).slice(0, Math.max(0, opts.cap))
  if (opts.confirm && prompts.length > ASK_ABOVE && !(await confirmCalls($, prompts.length))) return null
  const usage: Usage = { input: 0, cache_read: 0, cache_write: 0, output: 0 }
  const replies: Reply[] = []
  for (const p of prompts) {
    const r = await $.model.complete({
      model,
      ...(p.system ? { system: p.system } : {}),
      prompt: p.prompt,
      maxTokens: p.max_tokens,
      ...(opts.effort ? { effort: opts.effort } : {}),
      timeoutMs: p.timeout_s * 1000,
    })
    sum(usage, r.usage)
    if (r.isAnswered) replies.push({ index: p.index, text: r.text })
    else replies.push({ index: p.index, error: r.reason === 'api-error' && r.status !== null ? `${r.reason} ${r.status}` : r.reason })
  }
  return { replies, usage }
}
