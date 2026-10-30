import type { CommandRunResult, EngineInterface, PluginOptions } from 'claude-code'
import { analyzeSelection } from '../lib/args'
import { runBridge, spawnBridge } from '../lib/bridge'
import { ASK_ABOVE, SESSION_DELAY_TEXT, confirmCalls, planLine, resolveModel, runPlan, usageLine, type Feature, type PromptPlan } from '../lib/llm'
import { artifactsDir } from '../lib/paths'
import { ensureReady, sessionArgv } from '../lib/snapshot'
import { readFlags } from '../lib/state'
import { outFlag, outputError } from './exports'
import type { CommandContext } from './index'
import { EMPTY_TEXT, captureSessionSpec, errorRow, sessionSpec, SESSION_USAGE } from './overview'

/** Planning loads the session and builds the prompts; applying may write a sidecar and its views. */
const PROMPT_TIMEOUT_MS = 120_000
const APPLY_TIMEOUT_MS = 600_000
/** Env mode's `ask` and `fixes` make one request each; `label` (one per step) is spawned with no deadline. */
const DIRECT_TIMEOUT_MS = 600_000
let repliesSequence = 0

/** The most issues the fix judge takes (the bridge's `FIXES_CAP`). */
export const FIXES_CAP = 8
/** The bridge's default step cap for `label` (its `DEFAULT_MAX_STEPS`). */
export const DEFAULT_MAX_STEPS = 50

export const LIMIT_TEXT = `--limit must be a whole number from 1 to ${FIXES_CAP}.`
export const MAX_STEPS_TEXT = '--max-steps must be a whole number of 1 or more.'
export const CANCELLED_TEXT = 'Cancelled: no model call was made.'

type Empty = { empty: true; reason: string }
type AnalyzeDoc = { reply: string; history_path: string | null; error?: string }
type FixesDoc = { issues: unknown[]; errors: string[]; text: string }
type LabelDoc = { path: string; summary: unknown; text: string; errors: string[] }
type ResultDoc = Empty | AnalyzeDoc | FixesDoc | LabelDoc

/** What the person asked for, by feature: the bridge flags shared by `llm-prompt`, `llm-apply` and the direct subcommand. */
type Request = { feature: Feature; spec: string | undefined; captureSessionId?: string; argv: string[]; out?: string; model?: string }

/**
 * `analyze [question] [--session S]` read off the raw text after the
 * subcommand, so apostrophes survive (the tokenizer opens a quote on one).
 * `--session S` (or `--session=S`) anywhere in it is lifted out.
 */
export function analyzeArgs(args: string): { question: string | undefined; session: string | undefined; captureSessionId?: string } {
  return analyzeSelection(args)
}

/** A whole number from `flags[name]` within `[min, max]`; null when unset; undefined when refused. */
function wholeFlag(ctx: CommandContext, name: string, min: number, max: number): number | null | undefined {
  const v = ctx.parsed.flags[name]
  if (v === undefined) return null
  if (!/^\d+$/.test(String(v))) return undefined
  const n = Number(v)
  return n >= min && n <= max ? n : undefined
}

/** `options.labelMaxSteps` as a positive whole number, the manifest's 50 otherwise. */
function labelMaxSteps(options: PluginOptions): number {
  const v = options['labelMaxSteps']
  return typeof v === 'number' && Number.isInteger(v) && v >= 1 ? v : DEFAULT_MAX_STEPS
}

/** Labeler options shared by prompt capture, session replay and environment execution. */
function extraLabelArgs(ctx: CommandContext): string[] | string {
  const argv: string[] = []
  for (const name of ['taxonomy', 'user-phase', 'user-action']) {
    const value = ctx.parsed.flags[name]
    if (value === undefined) continue
    if (typeof value !== 'string' || !value.trim()) return `--${name} needs ${name === 'taxonomy' ? 'a taxonomy path' : 'a label'}.`
    argv.push(`--${name}`, value.trim())
  }
  for (const [name, max] of [['max-content-chars', Number.MAX_SAFE_INTEGER], ['max-tokens', 64000]] as const) {
    const value = wholeFlag(ctx, name, 1, max)
    if (value === undefined) return `--${name} must be a whole number from 1 to ${max}.`
    if (value !== null) argv.push(`--${name}`, String(value))
  }
  for (const name of ['delay', 'temperature']) {
    const value = ctx.parsed.flags[name]
    if (value === undefined) continue
    const number = typeof value === 'string' && value.trim() ? Number(value) : Number.NaN
    if (!Number.isFinite(number) || number < 0) return `--${name} must be a finite number of zero or more.`
    if (name === 'delay' && number > 3600) return '--delay must be from 0 to 3600 seconds.'
    if (name === 'delay' && number > 0 && ctx.options['llm'] !== 'env') return SESSION_DELAY_TEXT
    if (name === 'temperature' && ctx.options['llm'] !== 'env') return '--temperature requires llm=env; Claude Code’s session model API does not expose temperature.'
    argv.push(`--${name}`, String(number))
  }
  return argv
}

/**
 * The request for `ctx`, or the usage row refusing it. A headless `label`
 * is planned no further than `labelMaxSteps`, so the bridge expects no
 * reply that is never made.
 */
function parseRequest(ctx: CommandContext, interactive: boolean): Request | string {
  const spec = sessionSpec(ctx)
  switch (ctx.parsed.sub) {
    case 'analyze': {
      const a = analyzeArgs(ctx.args)
      if (/(^|\s)--?session(?=\s|=|$)/.test(ctx.args) && !a.session) return SESSION_USAGE
      return { feature: 'analyze', spec: a.session ?? spec, captureSessionId: a.captureSessionId, argv: a.question ? ['--question', a.question] : [] }
    }
    case 'fixes': {
      const limit = wholeFlag(ctx, 'limit', 1, FIXES_CAP)
      if (limit === undefined) return LIMIT_TEXT
      return { feature: 'fixes', spec, captureSessionId: captureSessionSpec(ctx), argv: limit === null ? [] : ['--limit', String(limit)] }
    }
    default: {
      const invalidOutput = outputError(ctx)
      if (invalidOutput) return invalidOutput
      const model = ctx.parsed.flags['model']
      if (model === true || (typeof model === 'string' && !model.trim())) return '--model needs a model name.'
      let steps = wholeFlag(ctx, 'max-steps', 1, Number.MAX_SAFE_INTEGER)
      if (steps === undefined) return MAX_STEPS_TEXT
      if (!interactive) steps = Math.min(steps ?? DEFAULT_MAX_STEPS, labelMaxSteps(ctx.options))
      const argv: string[] = []
      if (steps !== null) argv.push('--max-steps', String(steps))
      if (ctx.parsed.flags['assistant-only'] !== undefined) argv.push('--assistant-only')
      const extra = extraLabelArgs(ctx)
      if (typeof extra === 'string') return extra
      argv.push(...extra)
      return { feature: 'label', spec, captureSessionId: captureSessionSpec(ctx), argv, out: outFlag(ctx), model: typeof model === 'string' ? model.trim() : undefined }
    }
  }
}

/** `--out` and `--model` for `llm-apply label` and the direct `label`, when given. */
const labelArgv = (req: Request, model?: string): string[] => [
  ...(req.out ? ['--out', req.out] : []),
  ...(model ? ['--model', model] : []),
]

/** TrajViz's result as the row's lines. */
function resultText(feature: Feature, doc: AnalyzeDoc | FixesDoc | LabelDoc): string {
  if (feature === 'analyze') return (doc as AnalyzeDoc).reply
  if (feature === 'fixes') return (doc as FixesDoc).text
  const l = doc as LabelDoc
  return `${l.text}\nSidecar written to ${l.path}`
}

/**
 * Env mode: the plan (for the row's first line and the ask, as in session
 * mode), then TrajViz's own client (`ANALYZE_*`/`LABEL_*`) through the
 * bridge's `ask`, `fixes` or `label`. `ask` and `fixes` run under a
 * deadline; `label` makes one request per step for as many steps as the
 * person confirmed, so it is spawned with none. No usage line: TrajViz's
 * client reports none.
 */
async function direct($: EngineInterface, req: Request, plan: PromptPlan, session: string[], interactive: boolean): Promise<string> {
  const line = planLine(plan, `${req.model ?? (req.feature === 'label' ? 'LABEL_MODEL' : 'ANALYZE_MODEL')} (env)`)
  $.ui.log(line)
  if (interactive && plan.plan.calls > ASK_ABOVE && !(await confirmCalls($, plan.plan.calls))) return `${line}\n${CANCELLED_TEXT}`
  const argv = [...session, ...req.argv, ...labelArgv(req, req.model)]
  const doc =
    req.feature === 'label'
      ? await spawnBridge<ResultDoc>($, ['label', ...argv])
      : await runBridge<ResultDoc>($, [req.feature === 'analyze' ? 'ask' : 'fixes', ...argv], { timeoutMs: DIRECT_TIMEOUT_MS })
  if ('empty' in doc) return EMPTY_TEXT
  return `${line}\n${resultText(req.feature, doc)}`
}

/**
 * Session mode: the plan's calls through the session's model (asked when
 * over 4 in an interactive session, capped at `labelMaxSteps` in a headless
 * one); the replies written beside the artifacts for `llm-apply`, whose
 * result and the summed usage end the row under the plan line.
 */
async function viaSession($: EngineInterface, ctx: CommandContext, req: Request, plan: PromptPlan, session: string[], sessionId: string, interactive: boolean): Promise<string> {
  const model = req.model ?? (await resolveModel($, ctx.options, req.feature))
  const line = planLine(plan, model)
  $.ui.log(line)
  const cap = req.feature === 'label' && !interactive ? labelMaxSteps(ctx.options) : Number.POSITIVE_INFINITY
  const run = await runPlan($, plan, model, { confirm: interactive, cap })
  if (run === null) return `${line}\n${CANCELLED_TEXT}`
  const artifactsAt = session.indexOf('--artifacts')
  const repliesPath = `${artifactsAt >= 0 ? session[artifactsAt + 1]! : await artifactsDir($, sessionId)}/replies-${req.feature}-${await $.clock.now()}-${++repliesSequence}.json`
  await $.fs.write(repliesPath, JSON.stringify({ hash: plan.hash, replies: run.replies }))
  const apply = ['llm-apply', ...session, '--feature', req.feature, '--replies', repliesPath, ...req.argv]
  if (req.feature === 'label') apply.push(...labelArgv(req, model))
  const doc = await runBridge<ResultDoc>($, apply, { timeoutMs: APPLY_TIMEOUT_MS })
  if ('empty' in doc) return EMPTY_TEXT
  return `${line}\n${resultText(req.feature, doc)}\n${usageLine(run.usage)}`
}

/**
 * `/obs analyze | fixes | label`: TrajViz's LLM features. The bridge's plan
 * comes first in either mode (the row opens with it, and an interactive
 * session is asked above 4 calls); then the session's model, or TrajViz's
 * own client when `llm` is `env` in /config.
 */
export async function llm($: EngineInterface, ctx: CommandContext): Promise<CommandRunResult> {
  const interactive = (await readFlags($)).isInteractive
  const req = parseRequest(ctx, interactive)
  if (typeof req === 'string') return { text: req }
  try {
    await ensureReady($, ctx.options)
    const { transcript, argv } = await sessionArgv($, req.spec, undefined, req.captureSessionId)
    const plan = await runBridge<PromptPlan | Empty>($, ['llm-prompt', ...argv, '--feature', req.feature, ...req.argv,
      ...(req.out ? ['--out', req.out] : [])], { timeoutMs: PROMPT_TIMEOUT_MS })
    if ('empty' in plan) return { text: EMPTY_TEXT }
    if (ctx.options['llm'] === 'env') return { text: await direct($, req, plan, argv, interactive) }
    return { text: await viaSession($, ctx, req, plan, argv, transcript.sessionId, interactive) }
  } catch (e) {
    return { text: errorRow(e, req.spec) }
  }
}
