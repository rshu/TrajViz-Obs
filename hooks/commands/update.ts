import type { CommandRunResult, EngineInterface, ProcessRunResult } from 'claude-code'
import { configDir } from '../lib/paths'
import { displayText } from '../lib/format'
import { publicationGeneration } from '../lib/publication'
import { markOutcome } from '../lib/telemetry'
import { restoreStatusLine } from '../lib/watch'
import type { CommandContext } from './index'

const NAME = 'trajviz-obs'
const UPDATE_USAGE = 'Usage: /obs update [--check]'
const READ_TIMEOUT_MS = 30_000
const UPDATE_TIMEOUT_MS = 120_000
const MAX_JSON = 1024 * 1024
type Installation = { id: string; scope: 'user' | 'project' | 'local' | 'managed'; version: string; installPath: string; readFromFolder?: string; projectPath?: string }
type Marketplace = { source?: { source?: unknown; repo?: unknown; ref?: unknown; path?: unknown }; installLocation?: unknown }
type PluginEntry = { name?: unknown; version?: unknown }
type Target = { installation: Installation; marketplace: string; metadata: Marketplace }
type Available = { version?: string; source: string }
let running = false

/** Strip terminal controls from host diagnostics; no native command text is ever executed. */
function clean(value: string): string {
  return displayText(value).slice(0, 2000)
}

/** Native Git errors can echo authenticated URLs; redact before truncating diagnostics. */
function diagnostic(value: string): string {
  return displayText(value)
    .replace(/([a-z][a-z0-9+.-]*:\/\/)[^/\s@]+@/gi, '$1[redacted]@')
    .replace(/([a-z][a-z0-9+.-]*:\/\/[^?#\s]+)\?[^#\s]*/gi, '$1?[redacted]')
    .replace(/([a-z][a-z0-9+.-]*:\/\/[^#\s]+)#[^\s]*/gi, '$1#[redacted]')
    .slice(0, 2000)
}

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function json(text: string): unknown {
  if (text.length > MAX_JSON) throw new Error('Native plugin metadata is too large to inspect safely.')
  try { return JSON.parse(text) }
  catch { throw new Error('Claude Code returned invalid plugin metadata. Inspect /plugin and retry.') }
}

/** Native mutation commands print their structured result after any progress lines. */
function nativeDocument(result: ProcessRunResult): Record<string, unknown> | undefined {
  if (result.isStdoutTruncated || result.isStderrTruncated) return undefined
  try {
    const value = json(result.stdout.trim().split('\n').at(-1) ?? '')
    return object(value) ? value : undefined
  } catch { return undefined }
}

function nativeMessage(result: ProcessRunResult): string {
  const doc = nativeDocument(result)
  if (typeof doc?.message === 'string') return diagnostic(doc.message)
  const error = result.stderr.trim()
  return error && !/^[{[]/.test(error) ? diagnostic(error) : 'The native Claude Code command did not complete. Check /plugin for details.'
}

function installedRows(value: unknown): Installation[] {
  if (!Array.isArray(value)) throw new Error('Claude Code returned an unsupported plugin inventory. Update through /plugin instead.')
  const rows: Installation[] = []
  for (const row of value) {
    if (!object(row) || typeof row.id !== 'string' || !row.id.startsWith(`${NAME}@`)) continue
    if (typeof row.installPath !== 'string' || !row.installPath || typeof row.version !== 'string' || !row.version.trim()
      || row.version.length > 128 || clean(row.version) !== row.version
      || !['user', 'project', 'local', 'managed'].includes(String(row.scope))
      || ((row.scope === 'project' || row.scope === 'local') && (typeof row.projectPath !== 'string' || !row.projectPath))
      || (row.projectPath !== undefined && (typeof row.projectPath !== 'string' || !row.projectPath))
      || (row.readFromFolder !== undefined && (typeof row.readFromFolder !== 'string' || !row.readFromFolder))) {
      throw new Error('Claude Code returned an incomplete installation record for this plugin. Update through /plugin instead.')
    }
    rows.push(row as Installation)
  }
  return rows
}

async function nativeConfiguration($: EngineInterface): Promise<string> {
  const stat = await $.fs.stat(await configDir($), { resolve: true })
  if (stat.kind !== 'dir' || !stat.realPath) throw new Error('Cannot resolve Claude Code’s configuration directory. Update through /plugin instead.')
  return stat.realPath
}

async function inventory($: EngineInterface, configuration: string): Promise<Installation[]> {
  const result = await $.process.run(['claude', 'plugin', 'list', '--json'], { cwd: await $.session.cwd(), env: { CLAUDE_CONFIG_DIR: configuration }, timeoutMs: READ_TIMEOUT_MS, stdin: '' })
  if (result.exitCode !== 0 || result.isStdoutTruncated) throw new Error('Cannot read the native Claude Code plugin inventory. Run claude plugin list in your shell.')
  return installedRows(json(result.stdout))
}

function pathKey(value: string, windows: boolean): string {
  const normalized = value.replace(/\\/g, '/').replace(/\/+$/, '')
  return windows ? normalized.toLowerCase() : normalized
}

async function realDirectory($: EngineInterface, path: string, windows: boolean): Promise<string | undefined> {
  try {
    const stat = await $.fs.stat(path, { resolve: true })
    return stat.kind === 'dir' && stat.realPath ? pathKey(stat.realPath, windows) : undefined
  } catch { return undefined }
}

/** Source objects come from JSON; sort fields without exposing their private values in output. */
function sourceIdentity(value: unknown, depth = 0): string {
  if (depth > 16) throw new Error('The marketplace source metadata is too deeply nested to verify.')
  if (Array.isArray(value)) return `[${value.map(item => sourceIdentity(item, depth + 1)).join(',')}]`
  if (object(value)) return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${sourceIdentity(value[key], depth + 1)}`).join(',')}}`
  return JSON.stringify(value) ?? 'null'
}

/** Capture both registration and filesystem targets before waiting on the native updater. */
async function targetIdentity($: EngineInterface, found: Target, configuration: string): Promise<string> {
  const windows = /^windows/i.test((await $.env.get('OS')) ?? '')
  const row = found.installation
  const source = found.metadata.source
  const location = found.metadata.installLocation
  if (!object(source) || typeof source.source !== 'string' || !source.source || typeof location !== 'string') {
    throw new Error('The marketplace registration is incomplete. Update through /plugin instead.')
  }
  const market = await realDirectory($, location, windows)
  const installed = await realDirectory($, row.installPath, windows)
  const marketStore = await realDirectory($, `${configuration}/plugins/marketplaces`, windows)
  const cacheStore = await realDirectory($, `${configuration}/plugins/cache`, windows)
  const project = row.projectPath ? await realDirectory($, row.projectPath, windows) : undefined
  const folder = row.readFromFolder ? await realDirectory($, row.readFromFolder, windows) : undefined
  if (!market || !installed || ((row.scope === 'project' || row.scope === 'local') && !project) || (row.readFromFolder && !folder)) {
    throw new Error('The installation or marketplace paths could not be verified. Update through /plugin instead.')
  }
  if (!marketStore || !cacheStore || !market.startsWith(`${marketStore}/`) || !installed.startsWith(`${cacheStore}/`)) {
    throw new Error('The installation or marketplace resolves outside Claude Code’s managed stores. Update the checkout manually or inspect /plugin instead.')
  }
  return JSON.stringify([row.id, row.scope, row.version, installed, project, folder, market, cacheStore, marketStore, sourceIdentity(source)])
}

async function revalidateTarget($: EngineInterface, configuration: string, expected: string): Promise<void> {
  let identity: string | undefined
  try {
    const current = await target($, await inventory($, configuration), configuration)
    if (current) identity = await targetIdentity($, current, configuration)
  } catch { /* A missing or changed registration is no longer the authorized update target. */ }
  if (identity !== expected) throw new Error('The installation or marketplace source changed during refresh or could not be verified afterward. No plugin update was started; inspect /plugin and retry.')
}

/** Match the actual running directory, applicable project, and native scope precedence. */
async function target($: EngineInterface, rows: Installation[], configuration: string): Promise<Target | undefined> {
  const windows = /^windows/i.test((await $.env.get('OS')) ?? '')
  const root = await realDirectory($, $.plugin.root, windows)
  const cwd = await realDirectory($, await $.session.cwd(), windows)
  if (!root || !cwd) throw new Error('Cannot resolve this plugin’s installation identity. Use /plugin to update it manually.')
  const matches: { row: Installation; depth: number }[] = []
  for (const row of rows) {
    let depth = 0
    if (row.scope === 'project' || row.scope === 'local') {
      if (typeof row.projectPath !== 'string') continue
      const project = await realDirectory($, row.projectPath, windows)
      if (!project || (cwd !== project && !cwd.startsWith(`${project}/`))) continue
      depth = project.length
    }
    // Local directory marketplaces load their source directly instead of the cache.
    const loaded = row.readFromFolder ? await realDirectory($, row.readFromFolder, windows) : undefined
    const cached = await realDirectory($, row.installPath, windows)
    if (loaded === root || cached === root) matches.push({ row, depth })
  }
  if (!matches.length) return undefined
  const precedence = { managed: 4, local: 3, project: 2, user: 1 }
  matches.sort((a, b) => precedence[b.row.scope] - precedence[a.row.scope] || b.depth - a.depth)
  const first = matches[0]!
  if (matches.slice(1).some(other => other.row.scope === first.row.scope && other.depth === first.depth)) {
    throw new Error('More than one installation matches this running plugin. Update the intended scope through /plugin instead.')
  }
  const marketplace = first.row.id.slice(NAME.length + 1)
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(marketplace)) throw new Error('This installation has no supported marketplace identity. Update through /plugin instead.')
  const known = json(await $.fs.read(`${configuration}/plugins/known_marketplaces.json`))
  if (!object(known) || !object(known[marketplace])) throw new Error('The installation’s marketplace is unavailable. Update through /plugin instead.')
  return { installation: first.row, marketplace, metadata: known[marketplace] as Marketplace }
}

function catalogVersion(value: unknown, marketplace: string): string | undefined {
  if (!object(value) || value.name !== marketplace || !Array.isArray(value.plugins)) return undefined
  const entries = value.plugins.filter((entry): entry is PluginEntry => object(entry) && entry.name === NAME)
  if (entries.length !== 1) return undefined
  const version = entries[0]!.version
  return typeof version === 'string' && /^\d+\.\d+\.\d+(?:[-+][\w.+-]+)?$/.test(version) && version.length < 128 ? version : undefined
}

/** A remote check reads the configured ref, including pins; it never refreshes a marketplace. */
async function remoteCatalog($: EngineInterface, found: Target): Promise<Available | undefined> {
  const source = found.metadata.source
  if (source?.source !== 'github' || typeof source.repo !== 'string' || !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(source.repo)) return undefined
  if (source.ref !== undefined && (typeof source.ref !== 'string' || !source.ref || source.ref.length > 256)) return undefined
  const ref = typeof source.ref === 'string' ? source.ref : 'HEAD'
  const url = `https://raw.githubusercontent.com/${source.repo}/${encodeURIComponent(ref)}/.claude-plugin/marketplace.json`
  let expire = () => {}
  const deadline = new Promise<undefined>(resolve => { expire = () => resolve(undefined) })
  let timer: { cancel(): void } | undefined
  try {
    timer = $.clock.after(10_000, expire)
    const response = await Promise.race([$.http.fetch(url), deadline])
    if (!response?.ok) return undefined
    const version = catalogVersion(json(response.text), found.marketplace)
    return version ? { version, source: `remote catalog (${clean(ref)})` } : undefined
  } catch { return undefined }
  finally { timer?.cancel() }
}

async function available($: EngineInterface, found: Target): Promise<Available> {
  const remote = await remoteCatalog($, found)
  if (remote) return remote
  const location = found.metadata.installLocation
  if (typeof location === 'string') {
    try {
      const version = catalogVersion(json(await $.fs.read(`${location}/.claude-plugin/marketplace.json`)), found.marketplace)
      if (version) return { version, source: found.metadata.source?.source === 'directory' ? 'local marketplace catalog' : 'cached catalog; latest remote version unknown' }
    } catch { /* Missing or offline metadata is unknown, never proof of freshness. */ }
  }
  return { source: 'unavailable; latest remote version unknown' }
}

async function sourceGuidance($: EngineInterface, includeVersion: boolean, configuration: string, directory = $.plugin.root, refreshLocal = false): Promise<string> {
  let version = 'unknown'
  if (includeVersion) {
    try {
      const manifest = json(await $.fs.read(`${$.plugin.root}/.claude-plugin/plugin.json`))
      if (object(manifest) && manifest.name === NAME && typeof manifest.version === 'string') version = clean(manifest.version)
    } catch { /* Unmanaged sources still receive guidance when their manifest is unavailable. */ }
  }
  const versions = includeVersion ? `Current files: ${version}\nAvailable: unknown\n` : ''
  const windows = /^windows/i.test((await $.env.get('OS')) ?? '')
  const root = await realDirectory($, directory, windows)
  const cache = await realDirectory($, `${configuration}/plugins/cache`, windows)
  if (includeVersion && root && cache && root.startsWith(`${cache}/`)) {
    return `${versions}This session’s cached plugin no longer matches a current installation. Run /reload-plugins or restart Claude Code, then retry. No installation was changed.`
  }
  const command = manualUpdateCommand(directory, windows)
  const reload = refreshLocal ? 'Then refresh this plugin in /plugin and run /reload-plugins.' : 'Then run /reload-plugins.'
  const next = command ? `To update its Git checkout, run${windows ? ' in PowerShell' : ''}:\n${command}\n${reload}` : `Open this checkout in your shell and update it manually. ${reload}`
  return `${versions}This plugin uses a local or unmanaged directory:\n${clean(directory)}\n${next}\nNo installation was changed.`
}

/** A literal path in the user's shell; omit copyable commands for paths with controls. */
function manualUpdateCommand(root: string, windows: boolean): string | undefined {
  if (/[\u0000-\u001f\u007f-\u009f]/.test(root)) return undefined
  const quoted = windows ? `'${root.replace(/'/g, "''")}'` : `'${root.replace(/'/g, `'"'"'`)}'`
  return `git -C ${quoted} pull --ff-only`
}

/** The native updater may move the cache path; scope and project identify the resulting record. */
async function verifiedVersion($: EngineInterface, before: Installation, configuration: string): Promise<string> {
  const windows = /^windows/i.test((await $.env.get('OS')) ?? '')
  const project = before.projectPath ? await realDirectory($, before.projectPath, windows) : undefined
  const matches: Installation[] = []
  for (const row of await inventory($, configuration)) {
    if (row.id !== before.id || row.scope !== before.scope) continue
    if (before.scope === 'project' || before.scope === 'local') {
      if (!project || !row.projectPath || await realDirectory($, row.projectPath, windows) !== project) continue
    }
    matches.push(row)
  }
  if (matches.length !== 1) throw new Error('The native update finished, but its installed version could not be verified. Inspect the installation in /plugin before retrying.')
  return clean(matches[0]!.version)
}

/** Updates only this native installation; the host decides pins, downloads, and approval. */
export async function updatePlugin($: EngineInterface, ctx: CommandContext): Promise<CommandRunResult> {
  if (ctx.parsed.positional.length || Object.keys(ctx.parsed.flags).some(flag => flag !== 'check') || (ctx.parsed.flags.check !== undefined && ctx.parsed.flags.check !== true)) {
    return { text: markOutcome(ctx.telemetry, 'rejected', UPDATE_USAGE) }
  }
  const check = ctx.parsed.flags.check === true
  if (!check && !ctx.personRequested) return { text: markOutcome(ctx.telemetry, 'rejected', 'Run /obs update directly in the Claude Code composer to update this plugin. /obs update --check is read-only.') }
  if (running) return { text: markOutcome(ctx.telemetry, 'rejected', 'A plugin update or version check is already running. Wait for it to finish.') }
  running = true
  const generation = publicationGeneration()
  try {
    if ($.plugin.name !== NAME) throw new Error('This update command does not match the running plugin’s identity.')
    $.ui.status(check ? 'obs update: checking this installation…' : 'obs update: locating this installation…')
    const configuration = await nativeConfiguration($)
    const rows = await inventory($, configuration)
    const found = await target($, rows, configuration)
    if (!found) return { text: await sourceGuidance($, true, configuration) }
    const row = found.installation
    const current = json(await $.fs.read(`${$.plugin.root}/.claude-plugin/plugin.json`))
    if (!object(current) || current.name !== NAME) throw new Error('The running plugin manifest does not match its installation identity.')
    const currentVersion = object(current) && typeof current.version === 'string' ? clean(current.version) : 'unknown'
    const header = `TrajViz Obs · Claude Code\nCurrent files: ${currentVersion}\nInstalled: ${clean(row.version)} · ${row.scope} · ${row.id}`
    const local = typeof row.readFromFolder === 'string' || found.metadata.source?.source === 'directory'
    const localDirectory = row.readFromFolder ?? (typeof found.metadata.source?.path === 'string' ? found.metadata.source.path : typeof found.metadata.installLocation === 'string' ? found.metadata.installLocation : $.plugin.root)
    if (check) {
      const latest = await available($, found)
      if (local && row.scope !== 'managed') return { text: `${header}\nAvailable: ${latest.version ?? 'unknown'} · ${latest.source}\n${await sourceGuidance($, false, configuration, localDirectory, !row.readFromFolder)}` }
      return { text: `${header}\nAvailable: ${latest.version ?? 'unknown'} · ${latest.source}\nNo installation was changed.${row.scope === 'managed' ? '\nThis installation is managed by your administrator.' : '\nRun /obs update to ask Claude Code to update this installation.'}` }
    }
    if (row.scope === 'managed') return { text: markOutcome(ctx.telemetry, 'rejected', `${header}\nThis installation is managed by your administrator. No installation was changed.`) }
    if (local) return { text: `${header}\n${await sourceGuidance($, false, configuration, localDirectory, !row.readFromFolder)}` }
    const windows = /^windows/i.test((await $.env.get('OS')) ?? '')
    const nativeCwd = await realDirectory($, row.scope === 'project' || row.scope === 'local' ? row.projectPath! : await $.session.cwd(), windows)
    if (!nativeCwd) throw new Error('The update working directory could not be verified. Update through /plugin instead.')
    const identity = await targetIdentity($, found, configuration)
    $.ui.status('obs update: refreshing this marketplace…')
    const refresh = await $.process.run(['claude', 'plugin', 'marketplace', 'update', found.marketplace, '--json'], { cwd: nativeCwd, env: { CLAUDE_CONFIG_DIR: configuration }, timeoutMs: UPDATE_TIMEOUT_MS, stdin: '' })
    if (refresh.exitCode !== 0 || nativeDocument(refresh)?.outcome !== 'ok') {
      return { text: markOutcome(ctx.telemetry, 'error', `${header}\nMarketplace refresh did not complete: ${nativeMessage(refresh)}\nComplete the update in /plugin or your shell. No approval was accepted automatically.`), exitCode: 1 }
    }
    await revalidateTarget($, configuration, identity)
    $.ui.status('obs update: updating this plugin…')
    const result = await $.process.run(['claude', 'plugin', 'update', row.id, '--scope', row.scope, '--json'], { cwd: nativeCwd, env: { CLAUDE_CONFIG_DIR: configuration }, timeoutMs: UPDATE_TIMEOUT_MS, stdin: '' })
    if (result.exitCode !== 0 || nativeDocument(result)?.outcome !== 'ok') {
      return { text: markOutcome(ctx.telemetry, 'error', `${header}\nPlugin update did not complete: ${nativeMessage(result)}\nComplete the update in /plugin or your shell. No approval was accepted automatically.`), exitCode: 1 }
    }
    const installedVersion = await verifiedVersion($, row, configuration)
    const claimedVersion = nativeDocument(result)?.newVersion
    if (typeof claimedVersion === 'string' && claimedVersion !== installedVersion) throw new Error('The native updater’s result and installed version disagree. Inspect the installation in /plugin before retrying.')
    return { text: `${header}\nVerified installed: ${installedVersion} · ${row.scope}\nRun /reload-plugins or restart Claude Code to load updated files. Your current session keeps its loaded plugin until then.` }
  } catch (error) {
    const detail = error instanceof Error ? error.message : typeof error === 'string' ? error : 'The native plugin update could not be completed.'
    return { text: markOutcome(ctx.telemetry, 'error', `obs update: ${diagnostic(detail)}\nUpdate through /plugin if the native CLI or installation metadata is unavailable.`), exitCode: 1 }
  } finally {
    running = false
    $.ui.status(undefined)
    restoreStatusLine($, generation)
  }
}
