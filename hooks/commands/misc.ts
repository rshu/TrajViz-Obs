import type { CommandRunResult, EngineInterface, PluginOptions } from 'claude-code'
import { passArgs } from '../lib/args'
import { BridgeError, NO_ENV_TEXT, runBridge } from '../lib/bridge'
import { clip } from '../lib/format'
import { ensureEnv, ensureExtra, readEnvState, trajvizSource, type EnvState, type Extra } from '../lib/python'
import { BUSY_STALE_MS, BUSY_TEXT, FIRST_RUN_TOAST, ensureReady, sessionArgv } from '../lib/snapshot'
import { claimRefresh, releaseRefresh } from '../lib/state'
import type { CommandContext } from './index'
import { EMPTY_TEXT, errorRow, sessionSpec } from './overview'
import { fmtWhen } from './sessions'

/** `doctor` answers at once; `attribution` loads the run and a reference set, `collect` reads other tools' logs: the plan's 600 s. */
const DOCTOR_TIMEOUT_MS = 120_000
const PASS_TIMEOUT_MS = 600_000

export const ATTRIBUTION_USAGE = 'Usage: /obs attribution [--reference-root DIR] [--agent A] [--instance I]'
export const COLLECT_USAGE = "Usage: /obs collect <cursor|opencode|codearts> ...  (agent-trajviz-collect's arguments; --help lists them)"
export const SETUP_USAGE = 'Usage: /obs setup [--extras all|none|charts,app] [--source <pip requirement or checkout path>]'

type Empty = { empty: true; reason: string }

/** DECAF's capability row, as `agent_trajviz.attribution.diagnose` gives it. */
type CapabilityScore = { capability: string; assessed: boolean; blamed: boolean; weight: number; tier: string | null; top_error: string | null }

/** `agent_trajviz.attribution.diagnose`'s result: the fields the row draws. */
export type AttributionDoc = {
  available: boolean
  reason: string | null
  mode: string
  agent?: string | null
  instance_id?: string | null
  blame_status?: string | null
  primary?: { capability?: unknown; error_type?: unknown } | null
  scorecard: CapabilityScore[]
  faults: { capability?: unknown; error_type?: unknown; confidence?: unknown }[]
  notes: string[]
}

/** The bridge's `doctor` document. */
export type DoctorDoc = {
  python: string
  trajviz: { version: string; path: string }
  plugins: { loaded: string[]; error: string | null }
  extras: Record<string, boolean>
  decaf: { available: boolean; reason: string | null }
  llm_env: { available: boolean; ready: boolean; missing: string[]; reason: string | null }
  reference_root: string | null
}

type CollectDoc = { exit: number; stdout: string; stderr: string }

const str = (v: unknown): string => (typeof v === 'string' && v ? v : String(v ?? '?'))

/**
 * DECAF's result as the row: its reason verbatim when it could not run (the
 * usual case for a live session), else the verdict, the primary failure, the
 * blamed capabilities, the faults and TrajViz's notes.
 */
export function attributionText(doc: AttributionDoc): string {
  if (!doc.available) return doc.reason ?? 'DECAF attribution is not available here.'
  const who = doc.agent || doc.instance_id ? ` of ${str(doc.agent)}/${str(doc.instance_id)}` : ''
  const lines = [`DECAF attribution (${doc.mode})${who}: ${doc.blame_status ?? 'no verdict'}`]
  if (doc.primary) lines.push(`Primary failure: ${str(doc.primary.capability)} · ${str(doc.primary.error_type)}`)
  const blamed = doc.scorecard.filter(s => s.blamed).map(s => `${s.capability} (weight ${s.weight}${s.tier ? `, ${s.tier}` : ''})`)
  lines.push(`Blamed capabilities: ${blamed.length ? blamed.join(', ') : 'none'}`)
  if (doc.faults.length) lines.push('Faults:', ...doc.faults.map(f => `- ${str(f.capability)} · ${str(f.error_type)} (confidence ${str(f.confidence)})`))
  if (doc.notes.length) lines.push('Notes:', ...doc.notes.map(n => `- ${n}`))
  return lines.join('\n')
}

/** `attribution [--reference-root DIR] [--agent A] [--instance I]`: the bridge's `attribution` over the session, DECAF's result as text. */
async function attribution($: EngineInterface, ctx: CommandContext, spec: string | undefined): Promise<string> {
  const values: string[] = []
  for (const name of ['reference-root', 'agent', 'instance']) {
    const v = ctx.parsed.flags[name]
    if (v === true || (typeof v === 'string' && !v.trim())) return ATTRIBUTION_USAGE
    if (v !== undefined) values.push(`--${name}`, v)
  }
  await ensureReady($, ctx.options)
  const { argv } = await sessionArgv($, spec)
  const doc = await runBridge<Empty | AttributionDoc>($, ['attribution', ...argv, ...values], { timeoutMs: PASS_TIMEOUT_MS })
  if ('empty' in doc) return EMPTY_TEXT
  return attributionText(doc)
}

/** `collect …`: every argument through to `agent-trajviz-collect`; what it wrote, and its exit status when not 0. */
async function collect($: EngineInterface, ctx: CommandContext): Promise<string> {
  const args = passArgs(ctx.args)
  if (args.length === 0) return COLLECT_USAGE
  await ensureReady($, ctx.options)
  const doc = await runBridge<CollectDoc>($, ['collect', '--', ...args], { timeoutMs: PASS_TIMEOUT_MS })
  const parts = [doc.stdout.trimEnd(), doc.stderr.trimEnd()].filter(Boolean)
  if (doc.exit !== 0) parts.push(`agent-trajviz-collect exited with ${doc.exit}.`)
  return parts.length ? parts.join('\n') : 'agent-trajviz-collect printed nothing.'
}

const EXTRAS: readonly Extra[] = ['charts', 'app']

/**
 * The extras `--extras` names: `all` is both, `none` (or no flag) neither,
 * else a comma list of `llm` (always installed, so dropped), `charts` and
 * `app`, each once; undefined for a bare flag or an unknown name.
 */
export function parseExtras(flag: string | true | undefined): Extra[] | undefined {
  if (flag === undefined || flag === 'none') return []
  if (flag === true) return undefined
  if (flag === 'all') return [...EXTRAS]
  const out: Extra[] = []
  for (const name of flag.split(',').map(s => s.trim())) {
    if (name === 'llm') continue
    if (!(EXTRAS as readonly string[]).includes(name)) return undefined
    if (!out.includes(name as Extra)) out.push(name as Extra)
  }
  return out
}

/** The row after a setup: version, interpreter, source and extras of the environment as it stands. */
export function setupText(state: EnvState, source: string): string {
  const version = state.trajviz === null ? '(version unknown)' : state.trajviz
  return `TrajViz ${version} is set up at ${state.python} (source ${source}; extras ${state.extras.join(', ')}).`
}

/**
 * `setup [--extras all|none|charts,app] [--source S]`: the environment built
 * when missing (or built from another source than `S`, the config's when
 * not given), then each extra installed when not yet, under the refresh
 * lock as a first run holds it; progress on the status line, the first run
 * announced by its toast. No ask: the person asked. A `--source` other than
 * the config's is named, since the next `/obs` rebuilds from the config's.
 */
async function setup($: EngineInterface, ctx: CommandContext): Promise<string> {
  const extras = parseExtras(ctx.parsed.flags['extras'])
  const sourceFlag = ctx.parsed.flags['source']
  if (extras === undefined || sourceFlag === true || (typeof sourceFlag === 'string' && !sourceFlag.trim())) return SETUP_USAGE
  const options: PluginOptions = sourceFlag === undefined ? ctx.options : { ...ctx.options, trajvizSource: sourceFlag }
  const source = trajvizSource(options)
  const claim = await claimRefresh($, BUSY_STALE_MS)
  if (claim === null) throw new BridgeError('busy', BUSY_TEXT)
  const report = (line: string) => $.ui.status(`obs setup: ${clip(line, 80)}`)
  try {
    if ((await readEnvState($)) === null) $.ui.toast(FIRST_RUN_TOAST)
    let state = await ensureEnv($, options, report)
    for (const extra of extras) await ensureExtra($, options, extra, report)
    state = (await readEnvState($)) ?? state
    const text = setupText(state, source)
    const configured = trajvizSource(ctx.options)
    return source === configured ? text : `${text}\ntrajvizSource in /config is still ${configured}: the next /obs rebuilds from it unless you set it to ${source} there.`
  } finally {
    $.ui.status(undefined)
    await releaseRefresh($, claim)
  }
}

/** The extras in the order they are installed (`llm` first, always); one the bridge adds later comes after. */
const EXTRA_ORDER: readonly string[] = ['llm', ...EXTRAS]
const extraRank = (name: string): number => {
  const i = EXTRA_ORDER.indexOf(name)
  return i === -1 ? EXTRA_ORDER.length : i
}

const LABEL_WIDTH = 14
const row = (label: string, value: string): string => `  ${label.padEnd(LABEL_WIDTH)}${value}`

/** The environment's rows: the interpreter, and what it was built from and when. */
function envRows(state: EnvState): string[] {
  return [row('environment', `${state.python} (built from ${state.source} on ${fmtWhen(new Date(state.createdAt).toISOString())})`)]
}

/** How LLM features run, from the options (the manifest's defaults when unset). */
function llmModeRow(options: PluginOptions): string {
  if (options['llm'] === 'env') return row('llm mode', "env (TrajViz's own client from ANALYZE_*/LABEL_*)")
  const name = (key: string, fallback: string): string => {
    const v = options[key]
    return typeof v === 'string' && v ? v : fallback
  }
  return row('llm mode', `session (analysisModel ${name('analysisModel', 'sonnet')}, labelModel ${name('labelModel', 'haiku')})`)
}

/**
 * `obs doctor` as a table: the environment (`state.json`), then the bridge's
 * doctor: Python, TrajViz and where it is, its plugins, which extras import,
 * DECAF, the LLM environment (the names of unset variables, never values),
 * the reference root, and how LLM features run per the options.
 */
export function doctorText(doc: DoctorDoc, state: EnvState, options: PluginOptions): string {
  const names = Object.keys(doc.extras).sort((a, b) => extraRank(a) - extraRank(b))
  const installed = names.filter(e => doc.extras[e])
  const missing = names.filter(e => !doc.extras[e])
  const extras = `${installed.join(', ') || 'none'} installed${missing.length ? `; ${missing.join(', ')} not installed (/obs setup --extras ${missing.join(',')})` : ''}`
  const plugins = doc.plugins.error !== null ? `error: ${doc.plugins.error}` : doc.plugins.loaded.join(', ') || 'none'
  const decaf = doc.decaf.available ? 'available' : `not available: ${doc.decaf.reason ?? '?'}`
  const llm = doc.llm_env.ready ? 'ready' : doc.llm_env.available ? `not ready: ${doc.llm_env.missing.join(', ')} unset (names only)` : (doc.llm_env.reason ?? 'not available')
  return [
    'obs doctor',
    ...envRows(state),
    row('python', doc.python),
    row('trajviz', `${doc.trajviz.version} at ${doc.trajviz.path}`),
    row('plugins', plugins),
    row('extras', extras),
    row('decaf', decaf),
    row('llm env', llm),
    row('reference', doc.reference_root ?? 'TRAJVIZ_REFERENCE_ROOT unset'),
    llmModeRow(options),
  ].join('\n')
}

/**
 * `doctor`: the table above. No environment is the setup line (nothing is
 * built); a bridge that cannot answer keeps the environment rows and ends
 * with its error, so a broken venv is still named.
 */
async function doctor($: EngineInterface, ctx: CommandContext): Promise<string> {
  const state = await readEnvState($)
  if (state === null) return NO_ENV_TEXT
  try {
    const doc = await runBridge<DoctorDoc>($, ['doctor', '--cwd', await $.session.cwd()], { timeoutMs: DOCTOR_TIMEOUT_MS })
    return doctorText(doc, state, ctx.options)
  } catch (e) {
    return ['obs doctor', ...envRows(state), row('error', errorRow(e))].join('\n')
  }
}

/** `/obs attribution | collect | setup | doctor`: DECAF, the collect CLI and the environment. */
export async function misc($: EngineInterface, ctx: CommandContext): Promise<CommandRunResult> {
  const spec = sessionSpec(ctx)
  try {
    switch (ctx.parsed.sub) {
      case 'attribution':
        return { text: await attribution($, ctx, spec) }
      case 'collect':
        return { text: await collect($, ctx) }
      case 'setup':
        return { text: await setup($, ctx) }
      default:
        return { text: await doctor($, ctx) }
    }
  } catch (e) {
    return { text: errorRow(e, spec) }
  }
}
