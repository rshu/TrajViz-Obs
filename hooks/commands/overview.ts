import type { CommandRunResult, EngineInterface } from 'claude-code'
import { BridgeError } from '../lib/bridge'
import { artifactsDir } from '../lib/paths'
import { isProcessWithheld } from '../lib/python'
import { captureSnapshot, isEmpty, provenanceLine } from '../lib/snapshot'
import type { CommandContext } from './index'
import { maybeOpenPane } from './pane'

/**
 * The rows of the spec's "Error and degraded states" table that `/obs`
 * answers itself. A host that withholds `$.process` is known by its refusal
 * of the first call (`isProcessWithheld`): validate allows no read of `$`
 * or of a noun as a value, so there is no check up front.
 */
export const NO_PROCESS_TEXT = "This session's host runs no commands, so /obs cannot compute here; run it from a terminal session."
export const EMPTY_TEXT = 'Nothing to analyze yet: this session has no assistant steps. Run /obs after the first turn.'
export const NO_TRANSCRIPT_TEXT = 'No transcript for this session yet. Send one prompt, then run /obs.'
export const SESSION_USAGE = '--session needs a session id, prefix, or transcript path.'

/** The hidden note the model reads after the card: where the artifacts are and how to get more. */
const contextNote = (dir: string): string =>
  `TrajViz artifacts for this session go under ${dir} (report.html, views/<id>.json, labels.json, analyze.json). ` +
  'For more of the same data, call the obs tool (mcp__trajviz-obs__obs, listed behind ToolSearch) with ' +
  '{ what: "brief" | "issues" | "health" | "view", view?: string, options?: object, limit?: number }: ' +
  'it answers top-N JSON and the path of the full file.'

/** `--session <id|prefix|path>` when given as a value. */
export function sessionSpec(ctx: CommandContext): string | undefined {
  const s = ctx.parsed.flags['session']
  return typeof s === 'string' && s ? s : undefined
}

type Candidate = { id?: unknown; started?: unknown }

/**
 * The row for what a snapshot threw: `not_found` on the current session is
 * the "no transcript yet" row; `ambiguous` lists the candidates with their
 * start times; any other BridgeError is its message verbatim with the path
 * when the envelope carries one; `NoPythonError` and a setup failure are
 * their messages; the engine refusing `process` is the one-line answer.
 */
export function errorRow(e: unknown, spec?: string): string {
  if (isProcessWithheld(e)) return NO_PROCESS_TEXT
  if (e instanceof BridgeError) {
    if (e.code === 'not_found' && !spec) return NO_TRANSCRIPT_TEXT
    const candidates = e.extra['candidates']
    if (e.code === 'ambiguous' && Array.isArray(candidates)) {
      const list = (candidates as Candidate[]).map(c => `${String(c.id ?? '?')} (${typeof c.started === 'string' ? c.started : '?'})`)
      return `${e.message}: ${list.join(', ')}`
    }
    const path = e.extra['path']
    return typeof path === 'string' && path && !e.message.includes(path) ? `${e.message} (${path})` : e.message
  }
  return e instanceof Error ? e.message : String(e)
}

/** `/obs [overview] [--session …]`: the snapshot's text under its provenance line, and the note for the model. */
export async function overview($: EngineInterface, ctx: CommandContext): Promise<CommandRunResult> {
  const spec = sessionSpec(ctx)
  let snap
  let id
  try {
    ;({ snap, id } = await captureSnapshot($, ctx.options, spec))
  } catch (e) {
    return { text: errorRow(e, spec) }
  }
  if (isEmpty(snap)) return { text: EMPTY_TEXT }
  const at = new Date(snap.generated_at)
  const text = `${provenanceLine(id, snap, Number.isNaN(at.getTime()) ? new Date() : at)}\n${snap.text}`
  await maybeOpenPane($, ctx)
  return { text, context: [contextNote(await artifactsDir($, snap.transcript.session_id))] }
}
