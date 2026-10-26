import type { CommandPresentation, CommandRunResult, EngineInterface, PluginOptions } from 'claude-code'
import { dispatch } from '../commands/index'
import { parseArgs } from './args'
import { derivedTranscriptPath, selectSession } from './session'
import { readSession } from './state'

/** Quote one argument for the plugin's tokenizer, preserving quotes, spaces and backslashes. */
function quoteArgument(value: string): string {
  return `"${value.replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`
}

export type ActionContext = {
  options: PluginOptions
  presentation?: CommandPresentation
}

/**
 * A plugin's command.run skips its own hooks in the native host. UI actions
 * therefore call the same dispatcher directly, then print its answer; they
 * never queue a slash command that cannot reach this plugin's handler.
 */
async function runAction($: EngineInterface, args: string, ctx: ActionContext): Promise<CommandRunResult> {
  let result: CommandRunResult
  try {
    result = await dispatch($, { ...ctx, args, parsed: parseArgs(args) })
  } catch (error) {
    result = { text: `obs: ${error instanceof Error ? error.message : String(error)}` }
  }
  $.ui.log(`/obs ${args}`)
  // Native log rows sanitize embedded newlines as replacement characters.
  // Keep each result line readable, including multiline AI answers/errors.
  for (const line of result.text?.split(/\r?\n/) ?? []) if (line) $.ui.log(line)
  return result
}

/**
 * A card or pane action applies to the displayed trajectory. The active
 * session keeps a plain command so its live engine context window is used;
 * another session, including a card retained across /clear, names its path.
 */
export async function runSnapshotCommand($: EngineInterface, snap: { transcript: { path: string } }, command: string, ctx: ActionContext): Promise<CommandRunResult> {
  const session = await readSession($)
  const current = session.transcriptPath ?? await derivedTranscriptPath($, await $.session.cwd(), await $.session.id())
  const isCurrent = snap.transcript.path === current
  // /obs pane otherwise keeps a previously selected saved session when it
  // already has a latest snapshot. A current card explicitly returns to current.
  if (isCurrent && session.selected !== null) await selectSession($, null)
  const args = isCurrent ? command : `${command} --session ${quoteArgument(snap.transcript.path)}`
  return runAction($, args, ctx)
}

/** A pane awaiting its first snapshot still applies actions to its selected session. */
export async function runSelectedCommand($: EngineInterface, command: string, ctx: ActionContext): Promise<CommandRunResult> {
  const selected = (await readSession($)).selected
  return runAction($, selected ? `${command} --session ${quoteArgument(selected.path)}` : command, ctx)
}
