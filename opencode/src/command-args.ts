import { BOOLEAN_FLAGS, parseArgs, selectionError, suggest, type Parsed } from './shared/args.js'

const VIEW = ['limit', 'offset', 'json']
const OUTPUT = ['o', 'out']
const FLAGS: Record<string, readonly string[]> = {
  overview: [], pane: [], view: VIEW, brief: [],
  issues: VIEW, health: VIEW, patterns: VIEW, agents: VIEW, context: VIEW, timeline: VIEW,
  json: [...OUTPUT, 'view', 'with-trajectory'], bundle: OUTPUT, schemas: OUTPUT,
  dashboard: ['port'], converge: ['data-root', 'port'],
  compare: ['anchor-patch', 'fuzzy-commands', 'reverse'], scorecard: ['fuzzy-commands', 'labels', 'no-self'], labels: [],
  fixes: ['limit'], label: [...OUTPUT, 'assistant-only', 'model', 'max-steps', 'taxonomy', 'max-content-chars', 'delay', 'user-phase', 'user-action', 'max-tokens', 'temperature'],
  attribution: ['reference-root', 'agent', 'instance'], sessions: [], watch: [], statusline: [],
  setup: ['extras', 'source'], doctor: [], help: [], cancel: [], report: [],
}
const SESSION = ['overview', 'pane', 'brief', 'view', ...Object.keys(FLAGS).filter(k => FLAGS[k] === VIEW), 'json', 'bundle', 'dashboard', 'compare', 'scorecard', 'analyze', 'fixes', 'label', 'attribution', 'sessions']
const NO_POSITIONALS = ['overview', 'brief', 'help', 'doctor', 'setup', 'json', 'bundle', 'fixes', 'label', 'attribution', 'issues', 'health', 'patterns', 'agents', 'context', 'timeline', 'cancel', 'report']

/** Pure validation shared by local TUI commands and the server. */
export function validateArguments(args: string, parsed: Parsed = parseArgs(args)): string | undefined {
  const { sub, positional, flags, passthrough } = parsed
  if (sub === 'collect' || sub === 'converge-cli') return undefined
  const invalidSelection = selectionError(flags, sub === 'analyze' ? args : undefined)
  if (invalidSelection) return invalidSelection
  if (sub === 'analyze') return undefined
  const allowed = FLAGS[sub]
  if (!allowed) {
    const near = suggest(sub)
    return `Unknown subcommand '${sub}'.${near ? ` Did you mean '${near}'?` : ''} /obs help lists everything.`
  }
  if (flags.session === true || (typeof flags.session === 'string' && !flags.session.trim())) return '--session needs a session id, prefix, or transcript path.'
  if (flags.session !== undefined && !SESSION.includes(sub)) return `--session is not supported by /obs ${sub}.`
  if (flags['session-id'] !== undefined && (!SESSION.includes(sub) || sub === 'sessions')) return `--session-id is not supported by /obs ${sub}.`
  for (const [flag, value] of Object.entries(flags)) {
    if (flag !== 'session' && flag !== 'session-id' && !allowed.includes(flag)) return `Unknown option '--${flag}' for /obs ${sub}. /obs help lists the supported options.`
    if (BOOLEAN_FLAGS.includes(flag) && value !== true) return `--${flag} does not take a value.`
    if (OUTPUT.includes(flag) && (value === true || !value.trim())) return '-o/--out needs an output path.'
    if (!BOOLEAN_FLAGS.includes(flag) && value === true) return `--${flag} needs a value.`
  }
  let invalid = passthrough.length > 0 || (NO_POSITIONALS.includes(sub) && positional.length > 0)
  if (sub === 'pane') invalid ||= positional.length > 1 || (positional.length === 1 && positional[0] !== 'close')
  if (sub === 'watch' || sub === 'statusline') invalid ||= positional.length > 1 || (positional.length === 1 && !['on', 'off'].includes(positional[0]!))
  if (sub === 'schemas') invalid ||= positional.length > 1
  if (sub === 'view') invalid ||= positional.slice(1).some(p => !/^[^=]+=/.test(p))
  if ((sub === 'dashboard' || sub === 'converge') && positional[0] === 'stop') invalid ||= Object.keys(flags).some(name => name !== 'session' && name !== 'session-id')
  if (invalid) return `Unexpected arguments for /obs ${sub}. /obs help lists the supported form.`
  return undefined
}

export function pageArguments(flags: Parsed['flags']): string[] | string {
  const result: string[] = []
  for (const [name, minimum, maximum] of [['limit', 1, 500], ['offset', 0, Number.MAX_SAFE_INTEGER]] as const) {
    const value = flags[name]
    if (value === undefined) continue
    const n = typeof value === 'string' && /^\d+$/.test(value) ? Number(value) : NaN
    if (!Number.isSafeInteger(n) || n < minimum || n > maximum) return name === 'limit'
      ? '--limit must be a whole number from 1 to 500.' : '--offset must be a whole number of 0 or more.'
    result.push(`--${name}`, String(n))
  }
  return result
}

export function parseExtras(flag: string | true | undefined): string[] | undefined {
  if (flag === undefined || flag === 'none') return []
  if (flag === true) return undefined
  if (flag === 'all') return ['charts', 'app']
  const result: string[] = []
  for (const name of flag.split(',').map(s => s.trim())) {
    if (name === 'llm') continue
    if (!['charts', 'app'].includes(name)) return undefined
    if (!result.includes(name)) result.push(name)
  }
  return result
}
