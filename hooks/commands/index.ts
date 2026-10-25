import type { CommandPresentation, CommandRunResult, EngineInterface, PluginOptions } from 'claude-code'
import { BOOLEAN_FLAGS, suggest, type Parsed } from '../lib/args'
import { browser } from './browser'
import { compare } from './compare'
import { exportCmd } from './exports'
import { help } from './help'
import { inline } from './inline'
import { llm } from './llm'
import { misc } from './misc'
import { overview, SESSION_USAGE } from './overview'
import { pane } from './pane'
import { sessions } from './sessions'
import { toggles } from './toggles'

export type CommandContext = {
  /** The argument string as typed after `/obs` (what `parsed` was read from). */
  args: string
  parsed: Parsed
  options: PluginOptions
  presentation?: CommandPresentation
}

export type Handler = ($: EngineInterface, ctx: CommandContext) => Promise<CommandRunResult>

const VIEW_FLAGS = ['limit', 'offset', 'json']
const OUTPUT_FLAGS = ['o', 'out']
const COMMAND_FLAGS: Record<string, readonly string[]> = {
  overview: [], pane: [], view: VIEW_FLAGS,
  issues: VIEW_FLAGS, health: VIEW_FLAGS, patterns: VIEW_FLAGS, agents: VIEW_FLAGS, context: VIEW_FLAGS, timeline: VIEW_FLAGS,
  report: [...OUTPUT_FLAGS, 'dark', 'offline'], json: [...OUTPUT_FLAGS, 'view', 'with-trajectory'], bundle: OUTPUT_FLAGS, schemas: OUTPUT_FLAGS,
  dashboard: ['port'], converge: ['data-root', 'port'],
  compare: ['anchor-patch', 'fuzzy-commands', 'reverse'], scorecard: ['fuzzy-commands', 'labels', 'no-self'], labels: [],
  fixes: ['limit'], label: [...OUTPUT_FLAGS, 'assistant-only', 'model', 'max-steps'],
  attribution: ['reference-root', 'agent', 'instance'], sessions: [], watch: [], statusline: [], setup: ['extras', 'source'], doctor: [], help: [],
}

/**
 * Routes `ctx.parsed.sub` to its handler. A static switch, not a record of
 * handlers: the engine refuses `$` passed through anything but a function
 * declaration imported from a plugin file (or declared at the top of this
 * one), so every subcommand is one `case` here calling its handler by name,
 * and every handler is an `export async function name($, ctx)`.
 */
export async function dispatch($: EngineInterface, ctx: CommandContext): Promise<CommandRunResult> {
  const { sub } = ctx.parsed
  const session = ctx.parsed.flags['session']
  // The two CLI passthroughs own their flags; analyze reads its question raw.
  if (sub !== 'collect' && sub !== 'converge-cli' && sub !== 'analyze' && (session === true || (typeof session === 'string' && !session.trim()))) {
    return { text: SESSION_USAGE }
  }
  const allowed = COMMAND_FLAGS[sub]
  if (allowed) {
    for (const [flag, value] of Object.entries(ctx.parsed.flags)) {
      if (flag !== 'session' && !allowed.includes(flag)) return { text: `Unknown option '--${flag}' for /obs ${sub}. /obs help lists the supported options.` }
      if (BOOLEAN_FLAGS.includes(flag) && value !== true) return { text: `--${flag} does not take a value.` }
    }
  }
  switch (sub) {
    case 'overview':
      return overview($, ctx)
    case 'pane':
      return pane($, ctx)
    case 'view':
    case 'issues':
    case 'health':
    case 'patterns':
    case 'agents':
    case 'context':
    case 'timeline':
      return inline($, ctx)
    case 'report':
    case 'json':
    case 'bundle':
    case 'schemas':
      return exportCmd($, ctx)
    case 'dashboard':
    case 'converge':
      return browser($, ctx)
    case 'compare':
    case 'scorecard':
    case 'converge-cli':
    case 'labels':
      return compare($, ctx)
    case 'analyze':
    case 'fixes':
    case 'label':
      return llm($, ctx)
    case 'attribution':
    case 'collect':
    case 'setup':
    case 'doctor':
      return misc($, ctx)
    case 'sessions':
      return sessions($, ctx)
    case 'watch':
    case 'statusline':
      return toggles($, ctx)
    case 'help':
      return help($, ctx)
    default:
      break
  }
  const near = suggest(sub)
  const hint = near ? ` Did you mean '${near}'?` : ''
  return { text: `Unknown subcommand '${sub}'.${hint} /obs help lists everything.` }
}
