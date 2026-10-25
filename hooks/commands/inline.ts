import type { CommandRunResult, EngineInterface } from 'claude-code'
import type { Parsed } from '../lib/args'
import { runBridge } from '../lib/bridge'
import { fmtBytes, fmtInt } from '../lib/format'
import { ensureReady, sessionArgv } from '../lib/snapshot'
import type { CommandContext } from './index'
import { EMPTY_TEXT, errorRow, sessionSpec } from './overview'

const VIEW_TIMEOUT_MS = 120_000

/** The most rows one `--limit` asks for (the bridge's `MAX_LIMIT`). */
export const MAX_LIMIT = 500

export const LIMIT_TEXT = `--limit must be a whole number from 1 to ${MAX_LIMIT}.`
export const OFFSET_TEXT = '--offset must be a whole number of 0 or more.'

/** The pane's tabs as the TrajViz views that back them (Patterns: TrajViz's `hotspots`, the top rows of every pattern list). */
const TAB_VIEWS: Record<string, string> = {
  issues: 'issues',
  health: 'health',
  patterns: 'hotspots',
  agents: 'agents',
  context: 'context_pressure',
  timeline: 'workflow',
}

/** A row of the bridge's catalog (`view` with no id). */
export type CatalogEntry = { id: string; kind: string; title: string; description: string; options: Record<string, string> }

/** The bridge's `view <id>` document: the file written and a page of it inline. */
export type ViewPayload = {
  view: { id: string; title: string; kind: string; description: string }
  path: string
  bytes: number
  rows_total: number
  inline: { table?: string; series?: string; data?: unknown }
}

type ViewDoc = { empty: true; reason: string } | { views: CatalogEntry[] } | ViewPayload

/** `--limit`/`--offset` as bridge arguments (only those given), or the usage row refusing one. */
export function pageArgv(flags: Parsed['flags']): string[] | string {
  const argv: string[] = []
  const limit = flags['limit']
  if (limit !== undefined) {
    const n = /^\d+$/.test(String(limit)) ? Number(limit) : 0
    if (n < 1 || n > MAX_LIMIT) return LIMIT_TEXT
    argv.push('--limit', String(n))
  }
  const offset = flags['offset']
  if (offset !== undefined) {
    const n = Number(offset)
    if (!/^\d+$/.test(String(offset)) || !Number.isSafeInteger(n)) return OFFSET_TEXT
    argv.push('--offset', String(n))
  }
  return argv
}

/** The catalog as a table: id, kind, title, and the option names a view takes. */
export function catalogText(views: CatalogEntry[]): string {
  const idWidth = Math.max(0, ...views.map(v => v.id.length))
  const kindWidth = Math.max(0, ...views.map(v => v.kind.length))
  const rows = views.map(v => {
    const options = Object.keys(v.options)
    return `  ${v.id.padEnd(idWidth)}  ${v.kind.padEnd(kindWidth)}  ${v.title}${options.length ? `  (${options.join(', ')})` : ''}`
  })
  return [`${fmtInt(views.length)} TrajViz views; /obs view <id> [k=v]… draws one:`, ...rows].join('\n')
}

/** The view's header line over its inline part: the table, the series, or the data as JSON. */
export function viewText(p: ViewPayload): string {
  const head = `obs view ${p.view.id} · ${p.view.title} · ${fmtInt(p.rows_total)} rows`
  const body = p.inline.data !== undefined ? JSON.stringify(p.inline.data, null, 2) : (p.inline.table ?? p.inline.series ?? '')
  return `${head}\n${body}`
}

/** The hidden note after a view: where the whole of it is, and how the model gets top-N without a file read. */
const viewNote = (p: ViewPayload): string =>
  `The whole TrajViz view ${p.view.id} (${p.view.kind}, ${fmtInt(p.rows_total)} rows) is in ${p.path} (${fmtBytes(p.bytes)}). ` +
  `For top-N rows of it without a file read, call the obs tool (mcp__trajviz-obs__obs, listed behind ToolSearch) with { what: "view", view: "${p.view.id}", limit }.`

/**
 * `/obs view [<id>] [k=v]… [--limit N] [--offset N] [--json]`, and the tab
 * shortcuts `issues`, `health`, `patterns`, `agents`, `context`, `timeline`
 * (each a view, with the same paging): the bridge's `view`, whose catalog
 * (no id) is a table, and whose view is its inline table or series under a
 * header, the written file named in the hidden context.
 */
export async function inline($: EngineInterface, ctx: CommandContext): Promise<CommandRunResult> {
  const { sub, positional, flags } = ctx.parsed
  const id = sub === 'view' ? positional[0] : TAB_VIEWS[sub]
  const options = sub === 'view' ? positional.slice(1) : positional
  const page = pageArgv(flags)
  if (typeof page === 'string') return { text: page }
  const spec = sessionSpec(ctx)
  try {
    await ensureReady($, ctx.options)
    const { argv } = await sessionArgv($, spec)
    const call = ['view', ...argv]
    if (id !== undefined) call.push(id)
    for (const option of options) call.push('--option', option)
    call.push(...page)
    if (flags['json'] !== undefined) call.push('--json')
    const doc = await runBridge<ViewDoc>($, call, { timeoutMs: VIEW_TIMEOUT_MS })
    if ('empty' in doc) return { text: EMPTY_TEXT }
    if ('views' in doc) return { text: catalogText(doc.views) }
    return { text: viewText(doc), context: [viewNote(doc)] }
  } catch (e) {
    return { text: errorRow(e, spec) }
  }
}
