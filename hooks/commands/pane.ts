import type { CommandRunResult, EngineInterface, PluginOptions } from 'claude-code'
import { captureSnapshot, isEmpty } from '../lib/snapshot'
import { readLatest, readSession } from '../lib/state'
import { openPane, PANE_ID, placePane } from '../render/pane'
import type { CommandContext } from './index'
import { EMPTY_TEXT, errorRow, sessionSpec } from './overview'

/** The fullscreen width from which `/obs` docks the pane unasked. */
export const AUTO_OPEN_COLUMNS = 140

export const PANE_OPENED_TEXT = 'obs pane opened.'
export const PANE_CLOSED_TEXT = 'obs pane closed.'

/**
 * Opens the pane beside a fullscreen transcript wide enough for it
 * (`presentation.isFullscreen && columns >= 140`, docked `56` columns wide,
 * without taking the keyboard); answers whether it is drawn.
 */
export async function maybeOpenPane($: EngineInterface, ctx: CommandContext): Promise<boolean> {
  const p = ctx.presentation
  if (!p?.isFullscreen || p.columns < AUTO_OPEN_COLUMNS) return false
  return openPane($, { inline: false })
}

/**
 * One more snapshot for the pane (the `r` key, or `/obs pane` before any):
 * through the bridge for `spec`, else the session `--session` selected,
 * else the current one; stored as the latest, which redraws the pane. No
 * transcript row. An empty session is not stored, as `/obs` stores none.
 * Answers the row to show when nothing was stored (the error's, or the
 * empty state's), null when a snapshot was.
 */
export async function refreshPane($: EngineInterface, options: PluginOptions, spec?: string): Promise<string | null> {
  const target = spec ?? (await readSession($)).selected?.path ?? undefined
  let snap
  try {
    ;({ snap } = await captureSnapshot($, options, target))
  } catch (e) {
    return errorRow(e, target)
  }
  if (isEmpty(snap)) return EMPTY_TEXT
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
  const placed = await placePane($, { focus: true, inline: ctx.presentation?.isFullscreen !== true })
  let text = placed.isPlaced ? PANE_OPENED_TEXT : `obs pane is open but not drawn: ${placed.reason}`
  const spec = sessionSpec(ctx)
  if (spec !== undefined || (await readLatest($)) === null) {
    const row = await refreshPane($, ctx.options, spec)
    if (row !== null) text += `\n${row}`
  }
  return { text }
}
