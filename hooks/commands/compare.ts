import type { CommandRunResult, EngineInterface } from 'claude-code'
import { passArgs } from '../lib/args'
import { runBridge } from '../lib/bridge'
import { artifactsDir } from '../lib/paths'
import { resolveTranscript } from '../lib/session'
import { ensureReady, sessionArgv } from '../lib/snapshot'
import type { CommandContext } from './index'
import { EMPTY_TEXT, errorRow, captureSessionSpec, sessionSpec } from './overview'

/** A comparison loads every run whole, and the converge CLI may run a batch: the plan's 600 s. */
const COMPARE_TIMEOUT_MS = 600_000

export const COMPARE_USAGE = 'Usage: /obs compare <session|path> [--anchor-patch P] [--fuzzy-commands] [--reverse]'
export const SCORECARD_USAGE = 'Usage: /obs scorecard <session|path> [<session|path>...] [--fuzzy-commands] [--labels a,b] [--no-self]'
export const CONVERGE_CLI_USAGE = 'Usage: /obs converge-cli [--] <agent-trajviz-converge arguments>  (e.g. --batch MANIFEST --output summary; --help lists them)'
export const LABELS_USAGE = 'Usage: /obs labels <sidecar> [<other sidecar>]  (a labels.json /obs label wrote)'

type Empty = { empty: true; reason: string }
type ViewsDoc = Empty | { path: string; text: string }
type LabelsDoc = { text: string }
type ConvergeDoc = { exit: number; path: string | null; text: string | null }

/**
 * The transcript of each other run named on the command line, resolved as
 * `--session` is (an id, a unique id prefix, or a path to any TrajViz-readable
 * file). A failure is its own row, with the spec it was for, so the bridge's
 * `not_found` is its message and never the current session's "no transcript" row.
 */
async function otherPaths($: EngineInterface, specs: readonly string[]): Promise<{ paths: string[] } | { row: string }> {
  const paths: string[] = []
  for (const spec of specs) {
    try {
      paths.push((await resolveTranscript($, spec)).path)
    } catch (e) {
      return { row: errorRow(e, spec) }
    }
  }
  return { paths }
}

/**
 * `compare <other> [--anchor-patch P] [--fuzzy-commands] [--reverse]`: the
 * other run resolved, then the bridge's `compare … --other <path>` (the other
 * run is the reference unless `--reverse`); its text and the views file.
 */
async function compareRuns($: EngineInterface, ctx: CommandContext, spec: string | undefined): Promise<string> {
  const { positional, flags } = ctx.parsed
  const other = positional[0]
  const patch = flags['anchor-patch']
  if (other === undefined || positional.length > 1 || patch === true) return COMPARE_USAGE
  await ensureReady($, ctx.options)
  const { argv } = await sessionArgv($, spec, undefined, captureSessionSpec(ctx))
  const resolved = await otherPaths($, [other])
  if ('row' in resolved) return resolved.row
  const call = ['compare', ...argv, '--other', ...resolved.paths]
  if (patch !== undefined) call.push('--anchor-patch', patch)
  if (flags['fuzzy-commands'] !== undefined) call.push('--fuzzy-commands')
  if (flags['reverse'] !== undefined) call.push('--reverse')
  const doc = await runBridge<ViewsDoc>($, call, { timeoutMs: COMPARE_TIMEOUT_MS })
  if ('empty' in doc) return EMPTY_TEXT
  return `${doc.text}\nComparison views written to ${doc.path}.`
}

/**
 * `scorecard <b> [<c>…] [--fuzzy-commands] [--labels a,b] [--no-self]`: every
 * other run resolved, then the bridge's `scorecard … --others <paths>` (the
 * current session leads as the baseline unless `--no-self`); its table and
 * the views file.
 */
async function scorecard($: EngineInterface, ctx: CommandContext, spec: string | undefined): Promise<string> {
  const { positional, flags } = ctx.parsed
  const labels = flags['labels']
  if (positional.length === 0 || labels === true) return SCORECARD_USAGE
  await ensureReady($, ctx.options)
  const { argv } = await sessionArgv($, spec, undefined, captureSessionSpec(ctx))
  const resolved = await otherPaths($, positional)
  if ('row' in resolved) return resolved.row
  const call = ['scorecard', ...argv, '--others', ...resolved.paths]
  if (flags['fuzzy-commands'] !== undefined) call.push('--fuzzy-commands')
  if (labels !== undefined) call.push('--labels', labels)
  if (flags['no-self'] !== undefined) call.push('--no-self')
  const doc = await runBridge<ViewsDoc>($, call, { timeoutMs: COMPARE_TIMEOUT_MS })
  if ('empty' in doc) return EMPTY_TEXT
  return `${doc.text}\nScorecard views written to ${doc.path}.`
}

/**
 * `converge-cli [--] …`: every argument through to `agent-trajviz-converge`
 * (the bridge runs it in process). A JSON report lands in this session's
 * artifact folder and is named; any other output is printed, with the
 * tool's exit status when it is not 0.
 */
async function convergeCli($: EngineInterface, ctx: CommandContext): Promise<string> {
  const args = passArgs(ctx.args)
  if (args.length === 0) return CONVERGE_CLI_USAGE
  await ensureReady($, ctx.options)
  const call = ['converge-cli', '--artifacts', await artifactsDir($, await $.session.id()), '--', ...args]
  const doc = await runBridge<ConvergeDoc>($, call, { timeoutMs: COMPARE_TIMEOUT_MS })
  if (doc.path !== null) return `Converge report written to ${doc.path}.`
  const text = (doc.text ?? '').trimEnd()
  return doc.exit === 0 ? text : `${text}${text ? '\n' : ''}agent-trajviz-converge exited with ${doc.exit}.`
}

/** `labels <sidecar> [<other>]`: the label views of a sidecar, compared with another when given; sidecars are files, not sessions. */
async function labels($: EngineInterface, ctx: CommandContext): Promise<string> {
  const [sidecar, other] = ctx.parsed.positional
  if (sidecar === undefined || ctx.parsed.positional.length > 2) return LABELS_USAGE
  await ensureReady($, ctx.options)
  const call = ['labels', '--sidecar', sidecar]
  if (other !== undefined) call.push('--other', other)
  return (await runBridge<LabelsDoc>($, call, { timeoutMs: COMPARE_TIMEOUT_MS })).text
}

/** `/obs compare | scorecard | converge-cli | labels`: TrajViz's comparisons, each printing the bridge's text. */
export async function compare($: EngineInterface, ctx: CommandContext): Promise<CommandRunResult> {
  const spec = sessionSpec(ctx)
  try {
    switch (ctx.parsed.sub) {
      case 'compare':
        return { text: await compareRuns($, ctx, spec) }
      case 'scorecard':
        return { text: await scorecard($, ctx, spec) }
      case 'converge-cli':
        return { text: await convergeCli($, ctx) }
      default:
        return { text: await labels($, ctx) }
    }
  } catch (e) {
    return { text: errorRow(e, spec) }
  }
}
