import type { CommandRunResult, EngineInterface } from 'claude-code'
import type { Snapshot } from '../../types'
import { derivedTranscriptPath, selectSession } from './session'
import { readSession } from './state'

/** Quote one argument for the plugin's tokenizer, preserving quotes, spaces and backslashes. */
function quoteArgument(value: string): string {
  return `"${value.replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`
}

/**
 * A card or pane action applies to the displayed trajectory. The active
 * session keeps a plain command so its live engine context window is used;
 * another session, including a card retained across /clear, names its path.
 */
export async function runSnapshotCommand($: EngineInterface, snap: Snapshot, command: string): Promise<CommandRunResult> {
  const session = await readSession($)
  const current = session.transcriptPath ?? await derivedTranscriptPath($, await $.session.cwd(), await $.session.id())
  const isCurrent = snap.transcript.path === current
  // /obs pane otherwise keeps a previously selected saved session when it
  // already has a latest snapshot. A current card explicitly returns to current.
  if (isCurrent && session.selected !== null) await selectSession($, null)
  const args = isCurrent ? command : `${command} --session ${quoteArgument(snap.transcript.path)}`
  return $.command.run({ command: 'obs', args })
}
