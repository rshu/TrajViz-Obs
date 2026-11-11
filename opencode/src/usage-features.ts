import { parseArgs, SUBCOMMANDS } from './shared/args.js'

export const USAGE_FEATURES = [
  ...SUBCOMMANDS.filter(sub => sub !== 'telemetry'), 'brief', 'dashboard.html', 'dashboard.stop', 'converge.stop',
  'pane.close', 'pane.tab', 'pane.refresh', 'pane.expand',
] as const

/** Map a command to a fixed label without sending any of its arguments. */
export function usageFeature(args: string): string | undefined {
  const { sub, positional, flags } = parseArgs(args)
  if (sub === 'telemetry' || (sub !== 'brief' && !SUBCOMMANDS.includes(sub))) return undefined
  if (sub === 'dashboard' && flags.html !== undefined) return 'dashboard.html'
  if ((sub === 'dashboard' || sub === 'converge') && positional[0] === 'stop') return `${sub}.stop`
  if (sub === 'pane' && positional[0] === 'close') return 'pane.close'
  return sub
}
