import type { CommandRunResult, EngineInterface } from 'claude-code'
import { runBridge } from '../lib/bridge'
import { fmtBytes, fmtInt } from '../lib/format'
import { configDir } from '../lib/paths'
import { resolveTranscript } from '../lib/session'
import { ensureReady } from '../lib/snapshot'
import type { CommandContext } from './index'
import { MAX_LIMIT } from './inline'
import { errorRow, sessionSpec } from './overview'

const SESSIONS_TIMEOUT_MS = 120_000

export const SESSIONS_USAGE = `Usage: /obs sessions [N]  (N from 1 to ${MAX_LIMIT})`

/** One of the bridge's `sessions` rows: stat and a glance at the file, never a load. */
export type SessionRow = { id: string; path: string; mtime_ms: number; size: number; records: number; started: string | null }

type SessionsDoc = { project_dir: string; sessions: SessionRow[]; cut?: number }

const two = (n: number): string => String(n).padStart(2, '0')

/** An ISO start time as local `YYYY-MM-DD HH:MM`; `?` when unknown. */
export function fmtWhen(iso: string | null): string {
  if (iso === null) return '?'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '?'
  return `${d.getFullYear()}-${two(d.getMonth() + 1)}-${two(d.getDate())} ${two(d.getHours())}:${two(d.getMinutes())}`
}

/** The listing as a table: id, start, size, records; the current session marked; the rows cut counted. */
export function sessionsTable(doc: SessionsDoc, currentId: string): string {
  if (doc.sessions.length === 0) return `No sessions for this project in ${doc.project_dir}.`
  const cells = doc.sessions.map(s => [s.id, fmtWhen(s.started), fmtBytes(s.size), fmtInt(s.records)])
  const widths = [0, 1, 2, 3].map(i => Math.max(...cells.map(row => (row[i] as string).length)))
  const lines = doc.sessions.map((s, r) => {
    const [id, when, size, records] = cells[r] as [string, string, string, string]
    const row = `  ${id.padEnd(widths[0] as number)}  ${when.padEnd(widths[1] as number)}  ${size.padEnd(widths[2] as number)}  ${records.padStart(widths[3] as number)} records`
    return s.id === currentId ? `${row}  ← this session` : row
  })
  const n = doc.sessions.length
  const head = `${fmtInt(n)} session${n === 1 ? '' : 's'} in ${doc.project_dir}, newest first:`
  if (doc.cut) lines.push(`  … ${fmtInt(doc.cut)} more`)
  return [head, ...lines].join('\n')
}

/** `/obs sessions [N]`: this project's sessions, newest first, through the bridge's `sessions`. */
export async function sessions($: EngineInterface, ctx: CommandContext): Promise<CommandRunResult> {
  const n = ctx.parsed.positional[0]
  const limit = n === undefined ? undefined : /^\d+$/.test(n) ? Number(n) : 0
  if (ctx.parsed.positional.length > 1 || (limit !== undefined && (limit < 1 || limit > MAX_LIMIT))) return { text: SESSIONS_USAGE }
  try {
    await ensureReady($, ctx.options)
    const spec = sessionSpec(ctx)
    if (spec) {
      const transcript = await resolveTranscript($, spec)
      const doc = await runBridge<{ text: string }>($, ['capture-sessions', '--transcript', transcript.path, '--limit', String(limit ?? 20)], { timeoutMs: SESSIONS_TIMEOUT_MS })
      return { text: doc.text }
    }
    const argv = ['sessions', '--cwd', await $.session.cwd(), '--config-dir', await configDir($)]
    if (limit !== undefined) argv.push('--limit', String(limit))
    const doc = await runBridge<SessionsDoc>($, argv, { timeoutMs: SESSIONS_TIMEOUT_MS })
    return { text: sessionsTable(doc, await $.session.id()) }
  } catch (e) {
    return { text: errorRow(e) }
  }
}
