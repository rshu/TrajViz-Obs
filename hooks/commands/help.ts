import type { CommandRunResult, EngineInterface } from 'claude-code'
import type { CommandContext } from './index'

/** The grouped command table, under 30 lines. */
export const HELP_TEXT = [
  '/obs [subcommand] [options]  TrajViz observability; session commands take --session <id|prefix|path>',
  'Start here: /obs for a quick summary; /obs dashboard for the full interactive view.',
  'Look',
  '  overview (default)   health, KPIs, sparklines, top issues    pane | pane close   open/close the live pane',
  '  issues | health | patterns | agents | context | timeline   the pane\'s tabs inline [--limit N]',
  '  view [<id>] [k=v]... [--limit N] [--json]   the view catalog, or one view inline (full data to views/<id>.json)',
  'Export',
  '  json [-o DIR] [--view ID]... [--with-trajectory]   export view data',
  '  bundle [-o PATH]   ZIP for a hosted dashboard               schemas [DIR]   the JSON Schema of every view',
  'Browser',
  '  dashboard [--port N] | dashboard stop                 the Insight dashboard with this session loaded',
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
  '  attribution [--reference-root DIR] [--agent A] [--instance I]   collect <cursor|opencode|codearts> ...',
  '  help   this table',
].join('\n')

// A function declaration, as every handler is: validate follows `$` across an
// import only into one (never into a const bound to an arrow).
export async function help(_$: EngineInterface, _ctx: CommandContext): Promise<CommandRunResult> {
  return { text: HELP_TEXT }
}
