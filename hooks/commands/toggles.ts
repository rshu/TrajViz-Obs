import type { CommandRunResult, EngineInterface } from 'claude-code'
import { readFlags, readLatest, setFlag } from '../lib/state'
import { markOutcome } from '../lib/telemetry'
import { drawStatusLine, setStatusLineEnabled } from '../lib/watch'
import type { CommandContext } from './index'

/**
 * `/obs watch [on|off]` and `/obs statusline [on|off]`: sets the flag (no
 * argument flips it) and answers its new value. `statusline on` draws the
 * line from the latest snapshot at once; `off` clears it.
 */
export async function toggles($: EngineInterface, ctx: CommandContext): Promise<CommandRunResult> {
  const name = ctx.parsed.sub === 'watch' ? 'watch' : 'statusline'
  const arg = ctx.parsed.positional[0]
  if (ctx.parsed.positional.length > 1 || (arg !== undefined && arg !== 'on' && arg !== 'off')) {
    return { text: markOutcome(ctx.telemetry, 'rejected', `Usage: /obs ${name} [on|off]`) }
  }
  const on = arg === undefined ? !(await readFlags($))[name] : arg === 'on'
  await setFlag($, name, on)
  if (name === 'statusline') {
    setStatusLineEnabled(on)
    if (on) await drawStatusLine($, await readLatest($))
    else $.ui.status(undefined)
  }
  return { text: `obs: ${name} ${on ? 'on' : 'off'}` }
}
