import type { CommandPresentation, CommandRunResult, EngineInterface, HookFailure, PluginOptions } from 'claude-code'
import { BOOLEAN_FLAGS, parseArgs, selectionError, suggest, TELEMETRY_DESTINATION_USAGE, type Parsed } from '../lib/args'
import { commandFeature, markOutcome, reportTelemetry, setTelemetryConsent, telemetryConsentAtStart, telemetryDestination, telemetryStatus, type OutcomeTracker } from '../lib/telemetry'
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
  personRequested?: boolean
  telemetry?: OutcomeTracker
}

export type Handler = ($: EngineInterface, ctx: CommandContext) => Promise<CommandRunResult>

/** Host-level hook failures bypass the handler's JavaScript try/catch. */
export function commandFailure(args: string, error: HookFailure): CommandRunResult {
  const sub = parseArgs(args).sub
  const detail = error.kind === 'timeout'
    ? 'Claude Code stopped the command handler after its hook time budget expired.'
    : `The command handler failed${error.message ? `: ${error.message.slice(0, 2000)}` : ' in Claude Code.'}`
  return { text: `obs ${sub}: ${detail}\nRestart Claude Code, then retry the command.`, exitCode: 1 }
}

const VIEW_FLAGS = ['limit', 'offset', 'json']
const OUTPUT_FLAGS = ['o', 'out']
const COMMAND_FLAGS: Record<string, readonly string[]> = {
  overview: [], pane: [], view: VIEW_FLAGS,
  issues: VIEW_FLAGS, health: VIEW_FLAGS, patterns: VIEW_FLAGS, agents: VIEW_FLAGS, context: VIEW_FLAGS, timeline: VIEW_FLAGS,
  json: [...OUTPUT_FLAGS, 'view', 'with-trajectory', 'views-only'], bundle: OUTPUT_FLAGS, schemas: OUTPUT_FLAGS,
  dashboard: ['port', 'html', 'dark', 'inline-plotly'], converge: ['data-root', 'port'],
  compare: ['anchor-patch', 'fuzzy-commands', 'reverse'], scorecard: ['fuzzy-commands', 'labels', 'no-self'], labels: [],
  fixes: ['limit'], label: [...OUTPUT_FLAGS, 'assistant-only', 'model', 'max-steps', 'taxonomy', 'max-content-chars', 'delay', 'user-phase', 'user-action', 'max-tokens', 'temperature'],
  attribution: ['reference-root', 'agent', 'instance'], sessions: [], watch: [], statusline: [], setup: ['extras', 'source'], doctor: [],
  telemetry: [], telemetry_destination: ['use-launch-token'], enable_telemetry: [], disable_telemetry: [], help: [],
}

const NO_POSITIONALS = ['overview', 'help', 'doctor', 'telemetry', 'enable_telemetry', 'disable_telemetry', 'setup', 'json', 'bundle', 'fixes', 'label', 'attribution', 'issues', 'health', 'patterns', 'agents', 'context', 'timeline']
const SESSION_COMMANDS = ['overview', 'pane', 'view', 'issues', 'health', 'patterns', 'agents', 'context', 'timeline', 'json', 'bundle', 'dashboard', 'compare', 'scorecard', 'analyze', 'fixes', 'label', 'attribution', 'sessions']

export async function dispatch($: EngineInterface, ctx: CommandContext): Promise<CommandRunResult> {
  const feature = commandFeature(ctx.parsed)
  if (!feature) return route($, ctx)
  const started = Date.now()
  const consentAtStart = telemetryConsentAtStart($)
  const telemetry: OutcomeTracker = { outcome: 'success' }
  try {
    return await route($, { ...ctx, telemetry })
  } catch (error) {
    telemetry.outcome = 'error'
    throw error
  } finally {
    void reportTelemetry($, ctx.options, feature, telemetry.outcome, Date.now() - started, consentAtStart)
  }
}

/** Reject grammar that would otherwise be ignored, before setup or external work. */
function argumentError(ctx: CommandContext): string | undefined {
  const { sub, positional, passthrough, flags } = ctx.parsed
  if (sub === 'analyze' || sub === 'collect' || sub === 'converge-cli') return undefined
  if (!COMMAND_FLAGS[sub]) return undefined
  if (flags['session'] !== undefined && !SESSION_COMMANDS.includes(sub)) return `--session is not supported by /obs ${sub}.`
  if (flags['session-id'] !== undefined && (!SESSION_COMMANDS.includes(sub) || sub === 'sessions')) return `--session-id is not supported by /obs ${sub}.`
  let invalid = passthrough.length > 0 || (NO_POSITIONALS.includes(sub) && positional.length > 0)
  if (sub === 'pane') invalid ||= positional.length > 1 || (positional.length === 1 && positional[0] !== 'close')
  if (sub === 'schemas') invalid ||= positional.length > 1
  if (sub === 'view') invalid ||= positional.slice(1).some(p => !/^[^=]+=/.test(p))
  if (invalid) return `Unexpected arguments for /obs ${sub}. /obs help lists the supported form.`
  return undefined
}

/**
 * Routes `ctx.parsed.sub` to its handler. A static switch, not a record of
 * handlers: the engine refuses `$` passed through anything but a function
 * declaration imported from a plugin file (or declared at the top of this
 * one), so every subcommand is one `case` here calling its handler by name,
 * and every handler is an `export async function name($, ctx)`.
 */
async function route($: EngineInterface, ctx: CommandContext): Promise<CommandRunResult> {
  const { sub } = ctx.parsed
  const session = ctx.parsed.flags['session']
  if (sub !== 'collect' && sub !== 'converge-cli') {
    const invalidSelection = selectionError(ctx.parsed.flags, sub === 'analyze' ? ctx.args : undefined)
    if (invalidSelection) return { text: markOutcome(ctx.telemetry, 'rejected', invalidSelection) }
  }
  // The two CLI passthroughs own their flags; analyze reads its question raw.
  if (sub !== 'collect' && sub !== 'converge-cli' && sub !== 'analyze' && (session === true || (typeof session === 'string' && !session.trim()))) {
    return { text: markOutcome(ctx.telemetry, 'rejected', SESSION_USAGE) }
  }
  const invalidArguments = argumentError(ctx)
  if (invalidArguments) return { text: markOutcome(ctx.telemetry, 'rejected', invalidArguments) }
  const allowed = COMMAND_FLAGS[sub]
  if (allowed) {
    for (const [flag, value] of Object.entries(ctx.parsed.flags)) {
      if (flag !== 'session' && flag !== 'session-id' && !allowed.includes(flag)) {
        return { text: markOutcome(ctx.telemetry, 'rejected', `Unknown option '--${flag}' for /obs ${sub}. /obs help lists the supported options.`) }
      }
      if (BOOLEAN_FLAGS.includes(flag) && value !== true) return { text: markOutcome(ctx.telemetry, 'rejected', `--${flag} does not take a value.`) }
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
    case 'telemetry':
      return telemetryStatus($, ctx.options)
    case 'telemetry_destination': {
      const [destination, ...rest] = ctx.parsed.positional
      const bindLaunchToken = ctx.parsed.flags['use-launch-token'] === true
      if (rest.length || destination === '' || (bindLaunchToken && (destination === undefined || destination === 'default'))) {
        return { text: TELEMETRY_DESTINATION_USAGE }
      }
      if (destination !== undefined && !ctx.personRequested) {
        return { text: 'Run /obs telemetry_destination directly in the Claude Code composer to change the collector.' }
      }
      return telemetryDestination($, ctx.options, destination, bindLaunchToken)
    }
    case 'enable_telemetry':
    case 'disable_telemetry':
      if (!ctx.personRequested) return { text: `Run /obs ${sub} directly in the Claude Code composer to change usage telemetry.` }
      return setTelemetryConsent($, ctx.options, sub === 'enable_telemetry' ? 'on' : 'off')
    case 'sessions':
      return sessions($, ctx)
    case 'watch':
    case 'statusline':
      return toggles($, ctx)
    case 'help':
      return help($, ctx)
    case 'report':
      return { text: 'The /obs report command was removed. Use /obs dashboard to inspect the session or /obs dashboard --html PATH to save an offline copy.' }
    default:
      break
  }
  const near = suggest(sub)
  const hint = near ? ` Did you mean '${near}'?` : ''
  return { text: `Unknown subcommand '${sub}'.${hint} /obs help lists everything.` }
}
