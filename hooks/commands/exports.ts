import type { CommandRunResult, EngineInterface } from 'claude-code'
import { flagValues } from '../lib/args'
import { runBridge } from '../lib/bridge'
import { fmtBytes, fmtInt } from '../lib/format'
import { openTarget, type OpenResult } from '../lib/open'
import { artifactsDir } from '../lib/paths'
import { ensureReady, sessionArgv } from '../lib/snapshot'
import type { CommandContext } from './index'
import { EMPTY_TEXT, errorRow, sessionSpec } from './overview'

/** The exports load every step and may draw figures: the plan's 600 s. */
const EXPORT_TIMEOUT_MS = 600_000

type Empty = { empty: true; reason: string }
type Written = { path: string; bytes: number }
type ReportDoc = Empty | Written
type JsonDoc = Empty | (Written & { files: string[] })
type BundleDoc = Written & { members: string[]; members_total: number }
type SchemasDoc = Written & { files: string[] }

/** `<n> <word>s`. */
const count = (n: number, word: string): string => `${fmtInt(n)} ${word}${n === 1 ? '' : 's'}`

/** The usage row for a `--view` with no id. */
export const VIEW_USAGE = '--view needs a view id (/obs view lists them).'
export const OUT_USAGE = '-o/--out needs an output path.'

/** Refuse a supplied output flag that would otherwise silently use the default file. */
export function outputError(ctx: CommandContext): string | undefined {
  for (const name of ['o', 'out']) {
    const value = ctx.parsed.flags[name]
    if (value === true || (typeof value === 'string' && !value.trim())) return OUT_USAGE
  }
  return undefined
}

/** `-o PATH` (or `--out PATH`) when given as a value. */
export function outFlag(ctx: CommandContext): string | undefined {
  const o = ctx.parsed.flags['o'] ?? ctx.parsed.flags['out']
  return typeof o === 'string' && o ? o : undefined
}

/** The report's row: where it is, then whether the browser has it (the path is always printed). */
export function reportLine(path: string, bytes: number, opened: OpenResult): string {
  const written = `Report written to ${path} (${fmtBytes(bytes)})`
  switch (opened) {
    case 'opened':
      return `${written}; opened in the browser.`
    case 'skipped-ssh':
      return `${written}; not opened over SSH: copy it to your machine (scp) and open it there.`
    case 'disabled':
      return `${written}; not opened (openBrowser is off in /config).`
    case 'failed':
      return `${written}; no browser could be opened here, so open the file yourself.`
  }
}

/** `report [--dark] [--offline] [-o PATH]`: the `charts` extra, the bridge's report, the browser, the path. */
async function report($: EngineInterface, ctx: CommandContext, spec: string | undefined): Promise<string> {
  await ensureReady($, ctx.options, 'charts')
  const { argv } = await sessionArgv($, spec)
  const call = ['report', ...argv]
  const out = outFlag(ctx)
  if (out) call.push('--out', out)
  if (ctx.parsed.flags['dark'] !== undefined) call.push('--dark')
  if (ctx.parsed.flags['offline'] !== undefined) call.push('--offline')
  const doc = await runBridge<ReportDoc>($, call, { timeoutMs: EXPORT_TIMEOUT_MS })
  if ('empty' in doc) return EMPTY_TEXT
  return reportLine(doc.path, doc.bytes, await openTarget($, ctx.options, doc.path))
}

/**
 * `json [-o DIR] [--view ID]… [--with-trajectory]`: every view (or the named
 * ones) as files. `--view` repeats, so its ids come from `flagValues` over the
 * raw arguments (`parsed.flags` keeps only the last); a bare `--view` is
 * refused before anything runs.
 */
async function exportJson($: EngineInterface, ctx: CommandContext, spec: string | undefined): Promise<string> {
  const views = flagValues(ctx.args, 'view')
  if (views.some(v => v === true || !v.trim())) return VIEW_USAGE
  await ensureReady($, ctx.options)
  const { argv } = await sessionArgv($, spec)
  const call = ['json', ...argv]
  const out = outFlag(ctx)
  if (out) call.push('--out', out)
  for (const id of views) call.push('--view', String(id))
  if (ctx.parsed.flags['with-trajectory'] !== undefined) call.push('--with-trajectory')
  const doc = await runBridge<JsonDoc>($, call, { timeoutMs: EXPORT_TIMEOUT_MS })
  if ('empty' in doc) return EMPTY_TEXT
  return `${count(doc.files.length, 'file')} (${fmtBytes(doc.bytes)}) written to ${doc.path}.`
}

/** `bundle [-o PATH]`: the transcript and its sub-agent logs as a ZIP for a hosted dashboard. */
async function bundle($: EngineInterface, ctx: CommandContext, spec: string | undefined): Promise<string> {
  await ensureReady($, ctx.options)
  const { transcript } = await sessionArgv($, spec)
  const call = ['bundle', '--transcript', transcript.path, '--artifacts', await artifactsDir($, transcript.sessionId)]
  const out = outFlag(ctx)
  if (out) call.push('--out', out)
  const doc = await runBridge<BundleDoc>($, call, { timeoutMs: EXPORT_TIMEOUT_MS })
  return `Bundle of ${count(doc.members_total, 'file')} (${fmtBytes(doc.bytes)}) written to ${doc.path}; upload it to a hosted TrajViz dashboard.`
}

/** `schemas [DIR]`: the JSON Schema of every view, as files. */
async function schemas($: EngineInterface, ctx: CommandContext): Promise<string> {
  await ensureReady($, ctx.options)
  const call = ['schemas']
  if (ctx.parsed.positional.length > 1) return 'Usage: /obs schemas [DIR]'
  const dir = outFlag(ctx) ?? ctx.parsed.positional[0]
  if (dir) call.push('--out', dir)
  const doc = await runBridge<SchemasDoc>($, call, { timeoutMs: EXPORT_TIMEOUT_MS })
  return `JSON Schemas of ${count(doc.files.length, 'view')} (${fmtBytes(doc.bytes)}) written to ${doc.path}.`
}

/** `/obs report | json | bundle | schemas`: one bridge export each, its row naming what was written. */
export async function exportCmd($: EngineInterface, ctx: CommandContext): Promise<CommandRunResult> {
  const invalidOutput = outputError(ctx)
  if (invalidOutput) return { text: invalidOutput }
  const spec = sessionSpec(ctx)
  try {
    switch (ctx.parsed.sub) {
      case 'report':
        return { text: await report($, ctx, spec) }
      case 'json':
        return { text: await exportJson($, ctx, spec) }
      case 'bundle':
        return { text: await bundle($, ctx, spec) }
      default:
        return { text: await schemas($, ctx) }
    }
  } catch (e) {
    return { text: errorRow(e, spec) }
  }
}
