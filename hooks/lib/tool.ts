// The `obs` tool the model calls (`mcp__trajviz-obs__obs`, behind
// ToolSearch): the same data as `/obs` for the selected session, served by
// the `tool.call` hook in register.tsx through `serveTool`. `brief` is the
// bridge's `brief`; `issues` and `health` are the rows of the latest
// snapshot (one is taken and kept when none is stored); `view` is the
// bridge's `view --json`, its data inline only at most 64 KB, else the path
// of the file it wrote. Every helper taking `$` is a function declaration
// (validate follows `$` into one); state is reached only through state.ts.
import type { EngineInterface, PluginOptions, ToolCallResult, ToolSpec } from 'claude-code'
import { runBridge } from './bridge'
import { fmtBytes, fmtInt } from './format'
import { captureSnapshot, ensureReady, isEmpty, sessionArgv } from './snapshot'
import { readLatest, readSession } from './state'
import { EMPTY_TEXT, errorRow } from '../commands/overview'
import { MAX_LIMIT, catalogText, viewText, type CatalogEntry, type ViewPayload } from '../commands/inline'

/** The tool as `$.tool.register` takes it, at session start. */
export const OBS_TOOL: ToolSpec = {
  name: 'obs',
  description:
    "TrajViz data about the selected Claude Code session (the same data /obs shows): a statistics brief, the issues or health rows, or one TrajViz view by id. Answers top-N JSON (at most 64 KB) and the path of the full file.",
  inputSchema: {
    type: 'object',
    properties: {
      what: { type: 'string', enum: ['brief', 'issues', 'health', 'view'] },
      view: { type: 'string', description: 'The view id, for what: "view" (left out: the catalog of view ids)' },
      options: { type: 'object', description: 'View options as k: v' },
      limit: { type: 'number', description: 'Rows to answer (default 25)' },
    },
    required: ['what'],
  },
  isDeferred: true,
}

/** The arguments the model passes, `what`, `view`, `options` and `limit` among them (unchecked: the engine hands them on as given). */
export type ObsInput = { readonly [argument: string]: unknown }

/** The maximum UTF-8 bytes of the final MCP string, including metadata and paths. */
const INLINE_MAX_BYTES = 65_536

const DEFAULT_LIMIT = 25
const BRIEF_TIMEOUT_MS = 120_000
const VIEW_TIMEOUT_MS = 120_000

const WHATS = ['brief', 'issues', 'health', 'view'] as const

const WHAT_TEXT = `what must be one of ${WHATS.join(', ')}.`
const TOOL_LIMIT_TEXT = `limit must be a whole number from 1 to ${MAX_LIMIT}.`
const VIEW_ID_TEXT = 'view must be a view id (a string).'
const OPTIONS_TEXT = 'options must be an object of k: v (strings, numbers or booleans).'

// Registered MCP tools map a native string (or MCP content blocks) for the
// model. A nested {text,path} object passes the generic type but core rejects it.
type ObsResult = ToolCallResult<'mcp__trajviz-obs__obs'> & { result: string }

const answer = (text: string): ObsResult => ({
  result: new TextEncoder().encode(text).length <= INLINE_MAX_BYTES ? text
    : 'TrajViz output exceeds the 64 KB tool limit. Run /obs for the overview, or /obs view <id> for its full view file.',
})

/** `limit` as a row count (the default when left out), or null when it is not a whole number from 1 to MAX_LIMIT. */
function limitOf(limit: unknown): number | null {
  if (limit === undefined) return DEFAULT_LIMIT
  return typeof limit === 'number' && Number.isInteger(limit) && limit >= 1 && limit <= MAX_LIMIT ? limit : null
}

/** `options` as the bridge's `--option k=v` pairs, or null when it is not an object of scalars. */
function optionArgv(options: unknown): string[] | null {
  if (options === undefined) return []
  if (typeof options !== 'object' || options === null || Array.isArray(options)) return null
  const argv: string[] = []
  for (const [k, v] of Object.entries(options)) {
    if (typeof v !== 'string' && typeof v !== 'number' && typeof v !== 'boolean') return null
    argv.push('--option', `${k}=${String(v)}`)
  }
  return argv
}

/** The text over 64 KB: where the whole view is, since its data is not inlined. */
function pathText(p: ViewPayload): string {
  return (
    `TrajViz view ${p.view.id} (${p.view.kind}, ${fmtInt(p.rows_total)} rows; file ${fmtBytes(p.bytes)}). ` +
    `Its full response exceeds the 64 KB this tool answers inline; the whole view is in ${p.path}.`
  )
}

/** A view's answer: its data (the top `limit` rows of a list) as JSON at most 64 KB, else the path alone; the path either way. */
function viewAnswer(p: ViewPayload, limit: number): ObsResult {
  if (p.bytes > INLINE_MAX_BYTES) return answer(pathText(p))
  const data = p.inline.data
  const top = Array.isArray(data) ? data.slice(0, limit) : data
  const text = data === undefined ? `${viewText(p)}\nFull view: ${p.path}`
    : JSON.stringify({ view: p.view, rows_total: p.rows_total, data: top, path: p.path }, null, 2)
  return answer(new TextEncoder().encode(text).length <= INLINE_MAX_BYTES ? text : pathText(p))
}

/** The session the tool reads: the one `--session` selected, else the current one. */
async function selectedSpec($: EngineInterface): Promise<string | undefined> {
  return (await readSession($)).selected?.path ?? undefined
}

/** `issues` or `health`: the top `limit` rows of the latest snapshot, one taken and kept when none is stored. */
async function snapshotRows($: EngineInterface, options: PluginOptions, what: 'issues' | 'health', limit: number, spec: string | undefined): Promise<ObsResult> {
  let snap = await readLatest($)
  const selected = await readSession($)
  const expectedPath = selected.selected?.path ?? selected.transcriptPath
  let stale = snap === null || (expectedPath !== null && snap.transcript.path !== expectedPath)
  if (!stale && snap !== null) {
    try {
      const stat = await $.fs.stat(expectedPath ?? snap.transcript.path)
      stale = stat.size !== snap.transcript.size || Math.floor(stat.mtimeMs) !== snap.transcript.mtime_ms
    } catch { /* A host without stat can still serve the recorded snapshot. */ }
  }
  if (stale) {
    ;({ snap } = await captureSnapshot($, options, spec))
  }
  if (snap === null) return answer(EMPTY_TEXT)
  if (isEmpty(snap)) return answer(EMPTY_TEXT)
  const rows: unknown[] = snap[what]
  return answer(JSON.stringify({ [what]: rows.slice(0, limit), total: what === 'issues' ? (snap.counts.issues ?? rows.length) : rows.length }, null, 2))
}

/** The bridge's `brief` of the session. */
async function briefOf($: EngineInterface, options: PluginOptions, spec: string | undefined): Promise<ObsResult> {
  await ensureReady($, options)
  const { argv } = await sessionArgv($, spec)
  const doc = await runBridge<{ brief: string } | { empty: true; reason: string }>($, ['brief', ...argv], { timeoutMs: BRIEF_TIMEOUT_MS })
  return 'brief' in doc ? answer(doc.brief) : answer(EMPTY_TEXT)
}

/** The bridge's `view --json` of `id` (the catalog when none). */
async function viewOf($: EngineInterface, options: PluginOptions, id: string | undefined, viewOptions: string[], limit: number, spec: string | undefined): Promise<ObsResult> {
  await ensureReady($, options)
  const { argv } = await sessionArgv($, spec)
  const call = ['view', ...argv]
  if (id !== undefined) call.push(id)
  call.push(...viewOptions, '--limit', String(limit), '--json')
  const doc = await runBridge<{ empty: true; reason: string } | { views: CatalogEntry[] } | ViewPayload>($, call, { timeoutMs: VIEW_TIMEOUT_MS })
  if ('empty' in doc) return answer(EMPTY_TEXT)
  if ('views' in doc) return answer(catalogText(doc.views))
  return viewAnswer(doc, limit)
}

/**
 * The answer to one call of the obs tool: `{ result: string }`, whose
 * text is the JSON (with its file path) or brief, a usage line for arguments it
 * cannot take, or the error row of what a bridge run threw. Never throws.
 */
export async function serveTool($: EngineInterface, options: PluginOptions, input: ObsInput): Promise<ObsResult> {
  const what = input['what']
  if (typeof what !== 'string' || !(WHATS as readonly string[]).includes(what)) return answer(WHAT_TEXT)
  const limit = limitOf(input['limit'])
  if (limit === null) return answer(TOOL_LIMIT_TEXT)
  const viewId = input['view']
  if (viewId !== undefined && typeof viewId !== 'string') return answer(VIEW_ID_TEXT)
  const viewOptions = optionArgv(input['options'])
  if (viewOptions === null) return answer(OPTIONS_TEXT)
  let spec: string | undefined
  try {
    spec = await selectedSpec($)
    if (what === 'brief') return await briefOf($, options, spec)
    if (what === 'view') return await viewOf($, options, viewId || undefined, viewOptions, limit, spec)
    return await snapshotRows($, options, what as 'issues' | 'health', limit, spec)
  } catch (e) {
    return answer(errorRow(e, spec))
  }
}
