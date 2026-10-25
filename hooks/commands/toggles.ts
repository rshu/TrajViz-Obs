import type { CommandRunResult, EngineInterface } from 'claude-code'
import { readFlags, readLatest, setFlag } from '../lib/state'
import { drawStatusLine } from '../lib/watch'
import type { CommandContext } from './index'

/**
 * `/obs watch [on|off]` and `/obs statusline [on|off]`: sets the flag (no
 * argument flips it) and answers its new value. `statusline on` draws the
 * line from the latest snapshot at once; `off` clears it.
 */
export async function toggles($: EngineInterface, ctx: CommandContext): Promise<CommandRunResult> {
  const name = ctx.parsed.sub === 'watch' ? 'watch' : 'statusline'
  const arg = ctx.parsed.positional[0]
  if (ctx.parsed.positional.length > 1) return { text: `Usage: /obs ${name} [on|off]` }
  let on: boolean
  if (arg === undefined) on = !(await readFlags($))[name]
  else if (arg === 'on' || arg === 'off') on = arg === 'on'
  else return { text: `Usage: /obs ${name} [on|off]` }
  await setFlag($, name, on)
  if (name === 'statusline') {
    if (on) await drawStatusLine($, await readLatest($))
    else $.ui.status(undefined)
  }
  return { text: `obs: ${name} ${on ? 'on' : 'off'}` }
}
