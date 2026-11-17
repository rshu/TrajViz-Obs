import type { CommandRunResult, EngineInterface } from 'claude-code'
import type { CommandContext } from './index'

/** The grouped command table, under 30 lines. */
export const HELP_TEXT = [
  '/obs [subcommand] [options]  TrajViz observability; session commands take --session <id|prefix|path>',
  'Start here: /obs opens the live pane. /obs overview prints a summary. /obs dashboard opens the browser.',
  'Captured sessions: sessions --session PATH lists IDs; add --session-id ID to a session command.',
  'Look',
  '  pane | pane close   focus/close the live pane (1–7 tabs, d dashboard, r refresh, w watch)',
  '  issues | health | patterns | agents | context | timeline   diagnostic shortcuts (same options as view)',
  '  view [<id>] [k=v]... [--limit N] [--offset N] [--json]   the catalog, or one view (full data to views/<id>.json)',
  '    view and shortcuts: --limit 1–500 (default 25), --offset N (default 0), --json; k=v only for view.',
  'Export',
  '  json [-o DIR] [--view ID]... [--views-only]   export views and a reloadable trajectory',
  '  bundle [-o PATH]   ZIP for a hosted dashboard               schemas [DIR]   the JSON Schema of every view',
  'Browser',
  '  dashboard [--port N] [--dark] [--inline-plotly] | dashboard stop   the local Insight dashboard',
  '  dashboard --html PATH [--dark]   save standalone HTML   dashboard port [auto|default|N]   default port',
  '  converge [--data-root DIR] [--port N] | converge stop   the Converge comparison dashboard',
  'Compare',
  '  compare <session|path> [--anchor-patch P] [--fuzzy-commands] [--reverse]',
  '  scorecard <b> [<c>...] [--fuzzy-commands] [--labels a,b] [--no-self]',
  '  converge-cli ...   pass-through to agent-trajviz-converge      labels <sidecar> [<other>]   label views',
  'LLM  (only on request; the plan, calls, tokens and model, is printed first)',
  '  analyze [question]   fixes [--limit N]   label [-o PATH] [--assistant-only] [--model M] [--max-steps N]',
  '    label options: --taxonomy PATH --user-phase TAG --user-action TAG --max-content-chars N --max-tokens N',
  '    llm=env also accepts --delay SEC (0–3600) and --temperature N (or LABEL_TEMPERATURE).',
  'Setup',
  '  setup [--extras ...] [--source ...]   doctor   sessions [N]   watch [on|off]   statusline [on|off]',
  '  enable_telemetry | disable_telemetry   change usage telemetry consent    telemetry   check delivery',
  '  telemetry_destination [URL [--use-launch-token] | default]   show or save the collector',
  '  attribution [--reference-root DIR] [--agent A] [--instance I]   collect <cursor|opencode|codearts> ...',
  '  help   this table',
].join('\n')

// A function declaration, as every handler is: validate follows `$` across an
// import only into one (never into a const bound to an arrow).
export async function help(_$: EngineInterface, _ctx: CommandContext): Promise<CommandRunResult> {
  return { text: HELP_TEXT }
}
