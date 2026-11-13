import type { CommandRunResult, EngineInterface, PluginOptions } from 'claude-code'
import { captureSnapshot, isEmpty } from '../lib/snapshot'
import { readLatest, readSession } from '../lib/state'
import { markOutcome, type OutcomeTracker } from '../lib/telemetry'
import { syncStatusLine } from '../lib/watch'
import { PANE_ID, placePane } from '../render/pane'
import type { CommandContext } from './index'
import { EMPTY_TEXT, captureSessionSpec, observedError, overview, sessionSpec } from './overview'

export const PANE_OPENED_TEXT = 'obs pane opened.'
export const PANE_CLOSED_TEXT = 'obs pane closed.'

/**
 * One more snapshot for the pane (the `r` key, or `/obs pane` before any):
 * through the bridge for `spec`, else the session `--session` selected,
 * else the current one. Publishing the result redraws the pane, including
 * when an empty snapshot replaces older data. Returns text for an empty
 * result or an error, and null for a populated snapshot.
 */
export async function refreshPane($: EngineInterface, options: PluginOptions, spec?: string, captureSessionId?: string, telemetry?: OutcomeTracker): Promise<string | null> {
  const selected = (await readSession($)).selected
  const target = spec ?? selected?.path ?? undefined
  const capture = captureSessionId ?? (spec === undefined ? selected?.captureSessionId : undefined)
  return updatePaneSnapshot($, options, target, capture, telemetry)
}

/** Capture exactly the requested session. An absent selector returns to the current session. */
async function updatePaneSnapshot($: EngineInterface, options: PluginOptions, target?: string, capture?: string, telemetry?: OutcomeTracker): Promise<string | null> {
  let snap
  let generation
  try {
    ;({ snap, generation } = await captureSnapshot($, options, target, undefined, capture))
  } catch (e) {
    return observedError(telemetry, e, target)
  }
  void syncStatusLine($, snap, generation)
  if (isEmpty(snap)) return markOutcome(telemetry, 'empty', EMPTY_TEXT)
  return null
}

/**
 * `/obs pane`: opens the pane with focus, docked beside a fullscreen
 * transcript and inline (`rows: 14`) on the main screen, and takes the
 * first snapshot when there is none yet; `/obs pane close` closes it.
 */
export async function pane($: EngineInterface, ctx: CommandContext): Promise<CommandRunResult> {
  if (ctx.parsed.positional[0] === 'close') {
    await $.ui.close({ id: PANE_ID })
    return { text: PANE_CLOSED_TEXT }
  }
  const inline = ctx.presentation?.isFullscreen !== true
  if (ctx.args.trim() === '') {
    try {
      if (!(await placePane($, { inline })).isPlaced) return overview($, ctx)
    } catch {
      return overview($, ctx)
    }
    const error = await updatePaneSnapshot($, ctx.options, undefined, undefined, ctx.telemetry)
    return error === null ? {} : { text: error }
  }
  const placed = await placePane($, { focus: true, inline })
  let text = placed.isPlaced ? PANE_OPENED_TEXT : `obs pane is open but not drawn: ${placed.reason}`
  const spec = sessionSpec(ctx)
  if (spec !== undefined || (await readLatest($)) === null) {
    const row = await refreshPane($, ctx.options, spec, captureSessionSpec(ctx), ctx.telemetry)
    if (row !== null) text += `\n${row}`
  }
  return { text: placed.isPlaced ? text : markOutcome(ctx.telemetry, 'error', text) }
}
