import { spawn } from 'node:child_process'
import { createHash, randomUUID } from 'node:crypto'
import { lstat, mkdir, readFile, readdir, realpath, rename, unlink, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

type RunOptions = { signal?: AbortSignal; timeout?: number }
type Run = (command: 'git' | 'bun', args: string[], cwd: string, options?: RunOptions) => Promise<string>
export type LocalUpdatePlan = {
  token: string; root: string; branch: string; remote: string; effectiveRemote: string; current: string; available: string
  version: string; currentVersion: string; availableVersion: string; expiresAt: number; steps: string[]
}
export type LocalUpdateResult = {
  state: 'available' | 'current' | 'blocked' | 'error' | 'cancelled' | 'updated'
  text: string; plan?: LocalUpdatePlan; restartRequired?: true; receipt?: string
}
type Provenance = { root: string; adapter: string; git: string; rootID: string; gitID: string; configID: string; remoteID: string; entry: string }
type Prepared = { plan: LocalUpdatePlan; provenance: Provenance }
type Adapter = 'opencode' | 'pi'
type Options = { adapter?: Adapter; entryURL?: string; workerSource?: string; validateSource?: (directory: string) => Promise<void>; run?: Run; now?: () => number; loadedAt?: number; remoteVersion?: (commit: string, adapter: Adapter) => Promise<string>; inlineWorker?: boolean }
export type LocalUpdatePoll = { state: 'pending'; progress?: string } | { state: 'finished'; result: LocalUpdateResult } | { state: 'expired' }
type ApplyOptions = { jobID?: string; signal?: AbortSignal; progress?: (text: string) => void; beforeApply?: () => Promise<void> }
type WorkerInput = { adapter: Adapter; entryURL: string; root: string; token: string; prepared: Prepared; createdAt: number }
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const SHA = /^[a-f0-9]{40}(?:[a-f0-9]{24})?$/
const CONTROL = /[\x00-\x1f\x7f-\x9f]/
const MAX_OUTPUT = 256 * 1024
const safe = (value: string): string => value
  .replace(/(?:\u001b\]|\u009d)[\s\S]*?(?:\u0007|\u001b\\|\u009c|$)/g, '')
  .replace(/(?:\u001b\[|\u009b)[0-?]*[ -/]*[@-~]/g, '')
  .replace(/[\x00-\x08\x0b-\x1f\x7f-\x9f]/g, '')
  .replace(/([a-z][a-z0-9+.-]*:\/\/)[^/\s]*@/gi, '$1[redacted]@')
  .replace(/([a-z][a-z0-9+.-]*:\/\/[^?#\s]+)[?#][^\s]*/gi, '$1?[redacted]')
const message = (error: unknown): string => safe(error instanceof Error ? error.message : 'Update failed.')
const nodeID = (node: { dev: number; ino: number }): string => `${node.dev}:${node.ino}`
const abort = (signal?: AbortSignal) => signal?.throwIfAborted()
export function updateProcessTreeCommand(pid: number | undefined, platform = process.platform): { command: string; args: string[] } | undefined {
  if (platform !== 'win32' || !Number.isSafeInteger(pid) || !pid || pid < 1) return
  return { command: 'taskkill', args: ['/pid', String(pid), '/t', '/f'] }
}
async function atomic(file: string, value: unknown): Promise<void> {
  const temporary = `${file}.${randomUUID()}.tmp`
  try { await writeFile(temporary, JSON.stringify(value), { mode: 0o600, flag: 'wx' }); await rename(temporary, file) }
  finally { await unlink(temporary).catch(() => {}) }
}
async function versionAt(commit: string, adapter: Adapter): Promise<string> {
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 5000)
  try {
    const response = await fetch(`https://raw.githubusercontent.com/rshu/TrajViz-Obs/${commit}/${adapter}/package.json`, { signal: controller.signal, redirect: 'error', credentials: 'omit' })
    if (!response.ok || !response.body) return 'unknown'
    const reader = response.body.getReader(), chunks: Uint8Array[] = []; let size = 0
    for (;;) { const item = await reader.read(); if (item.done) break; size += item.value.length; if (size > 65536) { await reader.cancel(); return 'unknown' }; chunks.push(item.value) }
    const doc = JSON.parse(Buffer.concat(chunks).toString('utf8')) as { name?: unknown; version?: unknown }
    return doc.name === `trajviz-obs-${adapter}` && typeof doc.version === 'string' && /^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$/.test(doc.version) ? doc.version : 'unknown'
  } catch { return 'unknown' } finally { clearTimeout(timer) }
}

/** Captured, noninteractive processes cannot write into OpenCode's terminal or wait for input. */
export const runLocalUpdateCommand: Run = (command, args, cwd, options = {}) => new Promise((resolve, reject) => {
  try { abort(options.signal) } catch (error) { reject(error); return }
  const env = { ...process.env, GIT_TERMINAL_PROMPT: '0', GIT_OPTIONAL_LOCKS: '0',
    GIT_SSH_COMMAND: 'ssh -o BatchMode=yes', BUN_INSTALL_CACHE_DIR: process.env.BUN_INSTALL_CACHE_DIR,
    npm_config_audit: 'false', npm_config_fund: 'false', CI: '1' }
  // The checkout is fixed by this loaded module, never by inherited Git overrides.
  for (const key of Object.keys(env)) if (/^GIT_(?:DIR|WORK_TREE|INDEX_FILE|OBJECT_DIRECTORY|ALTERNATE_OBJECT_DIRECTORIES|CONFIG|PREFIX|CEILING_DIRECTORIES)/.test(key)) delete env[key as keyof typeof env]
  const child = spawn(command, args, { cwd, env, windowsHide: true, detached: process.platform !== 'win32', stdio: ['ignore', 'pipe', 'pipe'] })
  let output = '', stderr = '', bytes = 0, failure: Error | undefined
  const stop = () => {
    if (child.exitCode !== null || child.signalCode !== null) return
    const tree = updateProcessTreeCommand(child.pid)
    if (tree) {
      const killer = spawn(tree.command, tree.args, { windowsHide: true, stdio: 'ignore' })
      killer.once('error', () => child.kill('SIGKILL'))
      killer.once('close', code => { if (code !== 0) child.kill('SIGKILL') }); killer.unref()
    } else if (process.platform !== 'win32' && child.pid) { try { process.kill(-child.pid, 'SIGKILL') } catch { child.kill('SIGKILL') } }
    else child.kill('SIGKILL')
  }
  const fail = (error: Error) => { failure ??= error; stop() }
  const cancelled = () => fail(new Error('Cancelled before applying the update.'))
  const timer = setTimeout(() => fail(new Error(`${command} exceeded its update time limit.`)), options.timeout ?? 20_000)
  options.signal?.addEventListener('abort', cancelled, { once: true })
  child.stdout.on('data', chunk => { bytes += chunk.length; if (bytes > MAX_OUTPUT) fail(new Error(`${command} output exceeded its update size limit.`)); else output += chunk.toString() })
  child.stderr.on('data', chunk => { bytes += chunk.length; if (bytes > MAX_OUTPUT) fail(new Error(`${command} output exceeded its update size limit.`)); else stderr = (stderr + chunk.toString()).slice(-4096) })
  child.once('error', () => { failure ??= new Error(`${command} could not start. Check its installation on the server.`) })
  child.once('close', code => {
    clearTimeout(timer); options.signal?.removeEventListener('abort', cancelled)
    if (failure) reject(failure)
    else if (code !== 0) reject(new Error(`${command} failed${stderr.trim() ? `: ${safe(stderr.trim()).slice(-1200)}` : ` (exit ${code})`}`))
    else resolve(output)
  })
})

function officialOrigin(value: string): boolean {
  if (CONTROL.test(value)) return false
  if (/^git@github\.com:rshu\/TrajViz-Obs(?:\.git)?$/i.test(value)) return true
  try {
    const url = new URL(value)
    return ['https:', 'ssh:'].includes(url.protocol) && url.hostname.toLowerCase() === 'github.com'
      && (!url.port || (url.protocol === 'ssh:' && url.port === '22')) && !url.password
      && (!url.username || (url.protocol === 'ssh:' && url.username === 'git')) && !url.search && !url.hash
      && /^\/rshu\/TrajViz-Obs(?:\.git)?\/?$/i.test(url.pathname)
  } catch { return false }
}
async function json(file: string): Promise<Record<string, unknown>> {
  let data: unknown
  try { data = JSON.parse(await readFile(file, 'utf8')) as unknown }
  catch { throw new Error('The plugin metadata or saved update state could not be read.') }
  if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('The plugin package metadata is invalid.')
  return data as Record<string, unknown>
}
async function present(file: string): Promise<boolean> {
  try { await lstat(file); return true } catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return false; throw error }
}
function generated(row: string): boolean {
  if (row.slice(0, 2) !== '!!') return false
  const file = row.slice(3).replaceAll('\\', '/')
  return /(?:^|\/)(?:node_modules|__pycache__|\.venv|\.pytest_cache|\.ruff_cache|\.mypy_cache|htmlcov|build|dist|\.mcpb-cache)\/$/.test(file)
    || /(?:^|\/)[^/]+\.egg-info\/$/.test(file)
    || /(?:^|\/)(?:\.env(?:\.[^/]*)?|\.DS_Store|\.coverage|[^/]+\.log|[^/]+\.py[co])$/.test(file)
    || /^(?:\.claude-plugin\/types\/|\.claude\/|\.claude\/settings\.local\.json|\.playwright-mcp\/|docs\/)$/.test(file)
    || /^(?:opencode|pi)\/(?:dist|runtime)\/$/.test(file)
    || /^(?:opencode|pi)\/[^/]+\.tgz$/.test(file)
    || /^opencode\/(?:src\/shared\/|src\/types\.ts|LICENSE)$/.test(file)
    || /^pi\/LICENSE$/.test(file)
}

/** Server-owned plans are bound to the checkout containing this loaded module. */
export class LocalCheckoutUpdater {
  private readonly entryURL: string
  private readonly run: Run
  private readonly now: () => number
  private readonly loadedAt: number
  private readonly adapter: Adapter
  private readonly workerSource: string
  private readonly validateSource?: (directory: string) => Promise<void>
  private readonly remoteVersion: NonNullable<Options['remoteVersion']>
  private readonly inlineWorker: boolean
  private readonly plans = new Map<string, Prepared>()
  constructor(options: Options = {}) {
    // Host reload tags identify module revisions, not different installations.
    this.entryURL = pathToFileURL(fileURLToPath(options.entryURL ?? import.meta.url)).href
    this.run = options.run ?? runLocalUpdateCommand
    this.now = options.now ?? Date.now
    this.loadedAt = typeof options.loadedAt === 'number' && Number.isFinite(options.loadedAt) ? options.loadedAt : this.now()
    this.adapter = options.adapter ?? 'opencode'
    this.workerSource = options.workerSource ?? fileURLToPath(import.meta.url)
    this.validateSource = options.validateSource
    this.remoteVersion = options.remoteVersion ?? versionAt
    this.inlineWorker = options.inlineWorker ?? false
  }
  private async provenance(): Promise<Provenance> {
    const lexical = fileURLToPath(this.entryURL), entry = await realpath(lexical)
    if (CONTROL.test(entry) || entry !== lexical) throw new Error('The loaded adapter path is not an independent local source checkout. Update it manually.')
    let adapter = path.dirname(entry)
    for (let depth = 0; path.basename(adapter) !== this.adapter && depth < 5; depth++) adapter = path.dirname(adapter)
    const root = path.dirname(adapter), git = path.join(root, '.git')
    if (this.adapter === 'pi' && path.relative(adapter, entry).split(path.sep)[0] === 'dist') throw new Error('This Pi extension uses a built package. Update its source and rebuild it manually; automatic local updates support source installations.')
    if (path.basename(adapter) !== this.adapter || (await json(path.join(adapter, 'package.json'))).name !== `trajviz-obs-${this.adapter}`
      || (await json(path.join(root, 'package.json'))).name !== 'trajviz-obs') throw new Error('The loaded adapter does not belong to the expected TrajViz Obs checkout.')
    const [rootNode, gitNode] = await Promise.all([lstat(root), lstat(git)])
    if (!rootNode.isDirectory() || !gitNode.isDirectory() || await realpath(root) !== root || await realpath(git) !== git) throw new Error('Linked worktrees and symlinked Git metadata require a manual update.')
    const provenance = { root, adapter, git, entry, rootID: nodeID(rootNode), gitID: nodeID(gitNode), configID: createHash('sha256').update(await readFile(path.join(git, 'config'))).digest('hex'), remoteID: '' }
    const raw = await this.git(provenance, ['config', '--get-all', 'remote.origin.url'])
    const effective = await this.git(provenance, ['remote', 'get-url', '--all', 'origin'])
    provenance.remoteID = createHash('sha256').update(JSON.stringify([raw, effective])).digest('hex')
    return provenance
  }
  private paths(provenance: Provenance) {
    const directory = path.join(provenance.git, 'trajviz-obs-update')
    return { directory, lock: path.join(directory, 'lock.json'), marker: path.join(directory, 'incomplete.json'), receipt: path.join(directory, 'receipt.json') }
  }
  private git(provenance: Provenance, args: string[], signal?: AbortSignal): Promise<string> {
    return this.run('git', ['-c', `core.hooksPath=${path.join(this.paths(provenance).directory, 'disabled-hooks')}`, ...args], provenance.root, { signal, timeout: 30_000 })
  }
  private async inspect(provenance: Provenance, signal?: AbortSignal, ownLock = false): Promise<{ current: string; remote: string; effectiveRemote: string; version: string }> {
    abort(signal)
    const paths = this.paths(provenance)
    if (await present(paths.directory)) {
      if (!(await lstat(paths.directory)).isDirectory() || await realpath(paths.directory) !== paths.directory) throw new Error('The plugin update state directory is not safe. Inspect it manually.')
      if (await present(paths.marker)) throw new Error('A previous local update is incomplete. Repair the checkout and rebuild the adapter manually, then remove its update marker before trying again.')
      if (!ownLock && await present(paths.lock)) throw new Error('Another server or project owns the checkout update lock. Wait for it to finish; inspect a stale lock manually.')
    }
    if ((await this.git(provenance, ['rev-parse', '--show-toplevel'], signal)).trim() !== provenance.root) throw new Error('The loaded plugin is not the root of its own Git checkout.')
    const remote = (await this.git(provenance, ['config', '--get-all', 'remote.origin.url'], signal)).trim()
    if (!officialOrigin(remote)) throw new Error('The configured checkout origin must be the official rshu/TrajViz-Obs repository. Update other sources manually.')
    const effectiveRemote = safe((await this.git(provenance, ['remote', 'get-url', '--all', 'origin'], signal)).trim())
    if ((await this.git(provenance, ['symbolic-ref', '--short', 'HEAD'], signal)).trim() !== 'main'
      || (await this.git(provenance, ['rev-parse', '--abbrev-ref', '@{upstream}'], signal)).trim() !== 'origin/main') throw new Error('Only main tracking origin/main can update here. Preserve custom branches and pinned revisions with a manual update.')
    const divergence = (await this.git(provenance, ['rev-list', '--left-right', '--count', 'HEAD...@{upstream}'], signal)).trim().split(/\s+/).map(Number)
    if (divergence.length !== 2 || divergence.some(value => !Number.isSafeInteger(value) || value < 0) || divergence[0]! > 0) throw new Error('The checkout has local commits or unknown branch history. Preserve them with a manual update.')
    for (const file of ['MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply', 'BISECT_LOG']) if (await present(path.join(provenance.git, file))) throw new Error('Finish the checkout’s existing Git operation before updating.')
    const status = await this.git(provenance, ['status', '--porcelain=v1', '-z', '--untracked-files=all', '--ignored=matching'], signal)
    if (status.split('\0').some(row => row && !generated(row))) throw new Error('The checkout contains local changes or files. Save them and update manually; /obs update does not discard local work.')
    if (this.adapter === 'opencode') {
      // The packaging step replaces runtime/ and the UI bundles. Other ignored
      // config and caches remain in place; private files inside these outputs do not.
      for (const directory of ['runtime', 'dist']) await this.generatedOutput(provenance, directory)
    }
    const current = (await this.git(provenance, ['rev-parse', '--verify', 'HEAD'], signal)).trim()
    if (!SHA.test(current)) throw new Error('The installed Git revision is unknown.')
    const version = (await json(path.join(provenance.adapter, 'package.json'))).version
    return { current, remote, effectiveRemote, version: typeof version === 'string' && !CONTROL.test(version) ? version : 'unknown' }
  }
  private async generatedOutput(provenance: Provenance, kind: string): Promise<void> {
    const root = path.join(provenance.adapter, kind)
    if (!await present(root)) return
    let count = 0
    const visit = async (directory: string, depth: number) => {
      if (depth > 16 || ++count > 10_000) throw new Error('Generated adapter outputs exceed the safe inspection limit. Update this checkout manually.')
      if (!(await lstat(directory)).isDirectory() || await realpath(directory) !== directory) throw new Error('Generated adapter outputs contain a symlink. Preserve local files and update manually.')
      for (const name of await readdir(directory)) {
        if (++count > 10_000) throw new Error('Generated adapter outputs exceed the safe inspection limit. Update this checkout manually.')
        const file = path.join(directory, name), node = await lstat(file)
        if (node.isDirectory()) { await visit(file, depth + 1); continue }
        const relative = path.relative(root, file).split(path.sep).join('/')
        let known = name === '.DS_Store' || (kind === 'dist' && /^(?:tui\.js|gui-experimental\.cjs)(?:\.map)?$/.test(relative))
        if (kind === 'runtime') {
          known = known || /(?:^|\/)__pycache__\/[^/]+\.py[co]$/.test(relative)
          const source = relative === 'local-update.ts' ? path.join(provenance.root, 'shared/local-update.ts')
            : /^bridge\/(?:[^/]+\.py|trajviz_obs\/.+\.py)$/.test(relative) ? path.join(provenance.root, relative) : undefined
          if (source) { try { known = (await lstat(source)).isFile() } catch { /* Unknown outputs must be preserved manually. */ } }
        }
        if (!node.isFile() || !known) throw new Error('The generated adapter outputs contain files that the build cannot safely replace. Move private files out of runtime/dist and update manually.')
      }
    }
    await visit(root, 0)
  }
  private async available(provenance: Provenance, signal?: AbortSignal): Promise<string> {
    const rows = (await this.git(provenance, ['ls-remote', '--exit-code', 'origin', 'refs/heads/main'], signal)).trim().split('\n')
    const [commit, ref] = rows[0]?.split(/\s+/) ?? []
    if (rows.length !== 1 || !commit || !SHA.test(commit) || ref !== 'refs/heads/main') throw new Error('The available main revision could not be verified. No update was applied.')
    return commit
  }
  async check(signal?: AbortSignal): Promise<LocalUpdateResult> {
    try {
      const provenance = await this.provenance()
      await this.validateSource?.(provenance.adapter)
      const state = await this.inspect(provenance, signal), available = await this.available(provenance, signal)
      abort(signal)
      let availableVersion = 'unknown'
      try { availableVersion = await this.remoteVersion(available, this.adapter) } catch { /* Revision checks remain useful when version metadata is unavailable. */ }
      abort(signal)
      const header = `Local TrajViz Obs ${safe(state.version)} · ${safe(provenance.root)}\nmain → origin/main\nConfigured origin: ${safe(state.remote)}\nEffective origin: ${state.effectiveRemote}\nVersion: ${safe(state.version)} → ${safe(availableVersion)}\nInstalled: ${state.current}\nAvailable: ${available}`
      if (available === state.current) {
        const receipt = this.paths(provenance).receipt
        let needsRestart = false
        try { const saved = await json(receipt); needsRestart = saved.state === 'updated' && saved.installed === state.current && saved.restartRequired === true && typeof saved.finishedAt === 'number' && this.loadedAt < saved.finishedAt } catch { /* An older checkout may have no receipt. */ }
        const restart = this.adapter === 'pi' ? 'Run /reload when Pi is idle if it has not reloaded since the checkout changed.' : 'Restart the connected server and reopen the interface if it has not restarted since the checkout changed.'
        return { state: 'current', text: `${header}\nThe checkout is current.${needsRestart ? ` A completed local update was recorded. ${restart}` : ''}`,
          ...(needsRestart ? { restartRequired: true as const, receipt } : {}) }
      }
      // An old plan is never reused across checks or retained indefinitely.
      for (const [token, item] of this.plans) if (item.plan.expiresAt <= this.now()) this.plans.delete(token)
      if (this.plans.size >= 8) this.plans.delete(this.plans.keys().next().value!)
      const plan: LocalUpdatePlan = { token: randomUUID(), root: provenance.root, branch: 'main', remote: state.remote, effectiveRemote: state.effectiveRemote,
        current: state.current, available, version: state.version, currentVersion: state.version, availableVersion,
        expiresAt: this.now() + 5 * 60_000,
        steps: ['Fetch the reviewed main revision', 'Fast-forward this shared checkout without resetting local files',
          ...(this.adapter === 'opencode' ? ['bun install --frozen-lockfile', 'bun run build', 'Restart the connected server and reopen the interface after current work finishes'] : ['Reload Pi extensions after the update when Pi is idle'])] }
      this.plans.set(plan.token, { plan: { ...plan, steps: [...plan.steps] }, provenance })
      return { state: 'available', plan, text: `${header}\nAn update is available. This checkout may also serve other projects and plugins; its shared files${this.adapter === 'opencode' ? ' and generated adapter outputs' : ''} will change. Configuration, credentials, and telemetry preferences are not changed. ${this.adapter === 'opencode' ? 'A server restart' : 'An extension reload'} is required afterward.` }
    } catch (error) {
      return { state: signal?.aborted ? 'cancelled' : 'blocked', text: `Obs local update: ${message(error)}` }
    }
  }
  async apply(token: string, options: ApplyOptions = {}): Promise<LocalUpdateResult> {
    const prepared = this.plans.get(token)
    this.plans.delete(token)
    if (!prepared || prepared.plan.expiresAt <= this.now()) return { state: 'blocked', text: 'The update plan expired or was already used. Run /obs update again to review a new plan.' }
    try {
      abort(options.signal)
      await this.validateSource?.(prepared.provenance.adapter)
      abort(options.signal)
      if (!this.inlineWorker) return await this.worker(prepared, options)
    } catch (error) { return { state: options.signal?.aborted ? 'cancelled' : 'error', text: `Obs local update: ${message(error)}\nNo checkout update was applied.` } }
    return this.applyPrepared(prepared, options)
  }
  async poll(jobID: string): Promise<LocalUpdatePoll> {
    if (!UUID.test(jobID)) return { state: 'expired' }
    try {
      const provenance = await this.provenance(), directory = path.join(this.paths(provenance).directory, 'jobs', jobID)
      if (await realpath(directory) !== directory) return { state: 'expired' }
      const input = await json(path.join(directory, 'input.json'))
      if (input.root !== provenance.root || input.adapter !== this.adapter || input.entryURL !== this.entryURL) return { state: 'expired' }
      try {
        const result = await json(path.join(directory, 'result.json'))
        if (typeof result.text === 'string' && ['current', 'blocked', 'error', 'cancelled', 'updated'].includes(String(result.state))) return { state: 'finished', result: result as LocalUpdateResult }
      } catch { /* The standalone worker has not published its result yet. */ }
      try {
        const started = await json(path.join(directory, 'started.json'))
        if (typeof started.pid === 'number' && typeof started.createdAt === 'number' && this.now() - started.createdAt > 2000) {
          try { process.kill(started.pid, 0) } catch (error) {
            if ((error as NodeJS.ErrnoException).code === 'ESRCH') return { state: 'finished', result: { state: 'error', text: 'The independent update worker exited without a verified result. Inspect the checkout and .git/trajviz-obs-update before restarting or trying again.' } }
          }
        }
      } catch { /* The worker is still being dispatched. */ }
      const progress = await json(path.join(directory, 'progress.json'))
      if (typeof progress.updatedAt !== 'number' || this.now() - progress.updatedAt > 15 * 60_000) return { state: 'finished', result: { state: 'error', text: 'The update worker stopped reporting progress. Its outcome is unknown; inspect .git/trajviz-obs-update before restarting or trying another update.' } }
      return { state: 'pending', ...(typeof progress.text === 'string' ? { progress: safe(progress.text) } : {}) }
    } catch { return { state: 'expired' } }
  }
  private async worker(prepared: Prepared, options: ApplyOptions): Promise<LocalUpdateResult> {
    const provenance = await this.provenance()
    if (JSON.stringify(provenance) !== JSON.stringify(prepared.provenance)) throw new Error('The checkout identity changed. Review a new plan.')
    abort(options.signal)
    const id = options.jobID ?? randomUUID()
    if (!UUID.test(id)) throw new Error('An update job requires a random UUID.')
    const parent = path.join(this.paths(provenance).directory, 'jobs'), directory = path.join(parent, id)
    if (await present(directory)) {
      const existing = await json(path.join(directory, 'input.json'))
      if (existing.token !== prepared.plan.token || existing.root !== provenance.root || existing.adapter !== this.adapter || existing.entryURL !== this.entryURL) throw new Error('This update job ID already belongs to a different plan.')
    } else {
      const state = await this.inspect(provenance, options.signal)
      if (state.current !== prepared.plan.current || state.remote !== prepared.plan.remote || await this.available(provenance, options.signal) !== prepared.plan.available) throw new Error('The checkout or remote revision changed. Review a new update plan.')
      await options.beforeApply?.()
      abort(options.signal)
      await mkdir(parent, { recursive: true, mode: 0o700 })
      if (await realpath(parent) !== parent) throw new Error('The update job storage is not safe.')
      await mkdir(directory, { mode: 0o700 })
      try {
        const source = await readFile(this.workerSource, 'utf8')
        if (source.length > 1024 * 1024 || !source.includes('class LocalCheckoutUpdater')) throw new Error('The packaged standalone updater is unavailable. Reinstall or update this plugin manually once.')
        await writeFile(path.join(directory, 'worker.ts'), source, { mode: 0o600, flag: 'wx' })
        const input: WorkerInput = { adapter: this.adapter, entryURL: this.entryURL, root: provenance.root, token: prepared.plan.token, prepared, createdAt: this.now() }
        await atomic(path.join(directory, 'input.json'), input)
        await atomic(path.join(directory, 'progress.json'), { text: 'Starting the independent plugin update worker…', updatedAt: this.now() })
        abort(options.signal)
        if (JSON.stringify(await this.provenance()) !== JSON.stringify(provenance)) throw new Error('The checkout or effective origin changed before worker dispatch. Review a new plan.')
        await this.validateSource?.(provenance.adapter)
        abort(options.signal)
        const executable = this.adapter === 'opencode' ? 'bun' : process.execPath
        const argv = [...(this.adapter === 'pi' && !process.versions.bun ? ['--experimental-strip-types'] : []), path.join(directory, 'worker.ts'), '--trajviz-local-update-worker', path.join(directory, 'input.json')]
        const child = spawn(executable, argv, { cwd: provenance.root, detached: true, windowsHide: true, stdio: 'ignore', env: { ...process.env, TRAJVIZ_OBS_LOCAL_UPDATE_WORKER: id } })
        await new Promise<void>((resolve, reject) => { child.once('spawn', resolve); child.once('error', () => reject(new Error('The standalone update worker could not start.'))) })
        try { await atomic(path.join(directory, 'started.json'), { pid: child.pid, createdAt: this.now() }) }
        catch { child.unref(); return { state: 'error', text: 'The independent update worker was dispatched, but its status could not be saved. It may still complete the update. Inspect .git/trajviz-obs-update before restarting or trying again.' } }
        child.unref()
      } catch (error) {
        const result: LocalUpdateResult = { state: options.signal?.aborted ? 'cancelled' : 'error', text: `Obs local update: ${message(error)}\nNo checkout update was applied.` }
        await atomic(path.join(directory, 'result.json'), result).catch(() => {})
        return result
      }
    }
    const cancel = () => { void writeFile(path.join(directory, 'cancel'), '', { flag: 'wx', mode: 0o600 }).catch(() => {}) }
    options.signal?.addEventListener('abort', cancel, { once: true })
    if (options.signal?.aborted) cancel()
    try {
      for (;;) {
        const state = await this.poll(id)
        if (state.state === 'finished') return state.result
        if (state.state === 'expired') return { state: 'error', text: 'The update job could not be read. Its outcome is unknown; inspect .git/trajviz-obs-update before restarting or starting another update.' }
        try { if (state.progress) options.progress?.(state.progress) } catch { /* UI teardown must not stop independent updates. */ }
        await new Promise(resolve => setTimeout(resolve, 200))
      }
    } finally { options.signal?.removeEventListener('abort', cancel) }
  }
  static async runWorker(inputFile: string): Promise<void> {
    const directory = path.dirname(inputFile)
    let result: LocalUpdateResult
    try {
      if (path.basename(inputFile) !== 'input.json' || !UUID.test(path.basename(directory)) || process.env.TRAJVIZ_OBS_LOCAL_UPDATE_WORKER !== path.basename(directory)) throw new Error('Invalid standalone update job.')
      const input = await json(inputFile) as unknown as WorkerInput
      if (!['opencode', 'pi'].includes(input.adapter) || !input.prepared?.plan || input.prepared.plan.token !== input.token || input.prepared.provenance.root !== input.root
        || !SHA.test(input.prepared.plan.current) || !SHA.test(input.prepared.plan.available)) throw new Error('Invalid standalone update plan.')
      const updater = new LocalCheckoutUpdater({ adapter: input.adapter, entryURL: input.entryURL, inlineWorker: true })
      const provenance = await updater.provenance()
      if (JSON.stringify(provenance) !== JSON.stringify(input.prepared.provenance) || path.join(updater.paths(provenance).directory, 'jobs', path.basename(directory)) !== directory || await realpath(directory) !== directory) throw new Error('The standalone update job does not belong to this checkout.')
      const controller = new AbortController()
      const timer = setInterval(() => { void present(path.join(directory, 'cancel')).then(cancelled => { if (cancelled) controller.abort() }).catch(() => controller.abort()) }, 40)
      timer.unref()
      let progress = Promise.resolve()
      try {
        if (await present(path.join(directory, 'cancel'))) controller.abort()
        result = await updater.applyPrepared(input.prepared, { signal: controller.signal, progress: text => { progress = progress.then(() => atomic(path.join(directory, 'progress.json'), { text, updatedAt: Date.now() })).catch(() => {}) } })
        await progress
      } finally { clearInterval(timer) }
    } catch (error) { result = { state: 'error', text: `Obs standalone update: ${message(error)}. Inspect the local update job before retrying.` } }
    await atomic(path.join(directory, 'result.json'), result)
  }
  private async applyPrepared(prepared: Prepared, options: ApplyOptions = {}): Promise<LocalUpdateResult> {
    const { plan, provenance } = prepared, paths = this.paths(provenance), lockID = randomUUID()
    let locked = false, applying = false
    const progress = (text: string) => { try { options.progress?.(text) } catch { /* Observers cannot interrupt a dispatched update. */ } }
    const revalidate = async (signal?: AbortSignal) => {
      const latest = await this.provenance()
      if (JSON.stringify(latest) !== JSON.stringify(provenance)) throw new Error('The loaded checkout or Git metadata changed. Review a new update plan.')
      const state = await this.inspect(provenance, signal, locked)
      if (state.current !== plan.current || state.remote !== plan.remote) throw new Error('The checkout revision or origin changed while confirmation was open. Review a new update plan.')
      if (await this.available(provenance, signal) !== plan.available) throw new Error('Remote main changed while confirmation was open. Review the new revision before updating.')
    }
    try {
      abort(options.signal)
      await revalidate(options.signal)
      await options.beforeApply?.()
      abort(options.signal)
      await mkdir(paths.directory, { recursive: true, mode: 0o700 })
      if (await realpath(paths.directory) !== paths.directory) throw new Error('The update state directory changed. Inspect the checkout manually.')
      await writeFile(paths.lock, JSON.stringify({ id: lockID, pid: process.pid, createdAt: this.now() }), { flag: 'wx', mode: 0o600 })
      locked = true
      const hooks = path.join(paths.directory, 'disabled-hooks')
      await mkdir(hooks, { recursive: true, mode: 0o700 })
      if (!(await lstat(hooks)).isDirectory() || await realpath(hooks) !== hooks || (await readdir(hooks)).length) throw new Error('The update hook suppression directory is not empty and safe. Inspect it manually.')
      await revalidate(options.signal)
      if (this.adapter === 'opencode') {
        const bun = (await this.run('bun', ['--version'], provenance.adapter, { signal: options.signal, timeout: 10_000 })).trim().match(/^(\d+)\.(\d+)\.(\d+)/)
        if (!bun || +bun[1]! < 1 || (+bun[1]! === 1 && +bun[2]! < 3)) throw new Error('Bun 1.3.0 or newer must be available on the server before updating.')
      }
      progress('Fetching the reviewed Obs revision…')
      await this.git(provenance, ['fetch', '--no-tags', '--no-recurse-submodules', 'origin', `${plan.available}:refs/remotes/origin/main`], options.signal)
      const fetched = (await this.git(provenance, ['rev-parse', '--verify', 'FETCH_HEAD^{commit}'], options.signal)).trim()
      if (fetched !== plan.available) throw new Error('Git did not fetch the reviewed revision. No checkout update was applied.')
      try { await this.git(provenance, ['merge-base', '--is-ancestor', plan.current, plan.available], options.signal) }
      catch { abort(options.signal); throw new Error('Remote main is not a fast-forward of this checkout. Preserve local commits and update manually.') }
      await revalidate(options.signal)
      abort(options.signal)
      await writeFile(paths.marker, JSON.stringify({ current: plan.current, target: plan.available, startedAt: this.now() }), { flag: 'wx', mode: 0o600 })
      // After this boundary, RPC cancellation must not terminate checkout/build subprocesses.
      applying = true
      progress('Applying the Obs fast-forward. Let the update and build finish before restarting the server…')
      await this.git(provenance, ['merge', '--ff-only', '--no-edit', '--no-overwrite-ignore', plan.available])
      if (this.adapter === 'opencode') {
        progress('Installing the adapter’s locked dependencies…')
        await this.run('bun', ['install', '--frozen-lockfile'], provenance.adapter, { timeout: 180_000 })
        progress('Building the Obs terminal and Desktop adapters…')
        await this.run('bun', ['run', 'build'], provenance.adapter, { timeout: 180_000 })
      }
      const latest = await this.provenance()
      if (JSON.stringify(latest) !== JSON.stringify(provenance) || (await this.git(provenance, ['rev-parse', '--verify', 'HEAD'])).trim() !== plan.available
        || (await this.git(provenance, ['symbolic-ref', '--short', 'HEAD'])).trim() !== 'main'
        || (await this.git(provenance, ['rev-parse', '--abbrev-ref', '@{upstream}'])).trim() !== 'origin/main') throw new Error('The updated checkout identity or revision could not be verified.')
      for (const file of this.adapter === 'opencode' ? ['dist/tui.js', 'dist/gui-experimental.cjs', 'runtime/bridge/trajviz_obs_cli.py'] : ['trajviz-obs.ts', 'src/index.ts']) {
        const output = path.join(provenance.adapter, file)
        if (!(await lstat(output)).isFile() || !(await readFile(output)).length) throw new Error('The adapter build did not produce every required output.')
      }
      if ((await this.git(provenance, ['diff', '--name-only', 'HEAD'])).trim()) throw new Error('The adapter build changed tracked checkout files. Inspect and repair it before restarting.')
      const version = (await json(path.join(provenance.adapter, 'package.json'))).version
      if (plan.availableVersion !== 'unknown' && version !== plan.availableVersion) throw new Error('The installed plugin version differs from the reviewed version. Inspect it before restarting.')
      const receipt = { state: 'updated', current: plan.current, installed: plan.available, version, finishedAt: this.now(), restartRequired: true }
      const temporary = `${paths.receipt}.${lockID}.tmp`
      await writeFile(temporary, JSON.stringify(receipt), { mode: 0o600, flag: 'wx' }); await rename(temporary, paths.receipt)
      await unlink(paths.marker)
      return { state: 'updated', restartRequired: true, receipt: paths.receipt,
        text: `Obs checkout updated${this.adapter === 'opencode' ? ' and built' : ''} · ${safe(typeof version === 'string' ? version : 'version unknown')} · ${plan.available.slice(0, 12)}.\nThe running plugin still uses its loaded code. ${this.adapter === 'opencode' ? 'Finish current work, restart the connected OpenCode server with its matching-version CLI and original launch environment, then reopen the terminal or Desktop. Use service restart for a managed service. Quitting Desktop alone can leave its server running. Other projects sharing this checkout also need a restart.' : 'Reload Pi extensions when Pi is idle. Other sessions sharing this checkout also need a reload.'}` }
    } catch (error) {
      const text = `Obs local update: ${message(error)}\n${applying ? 'The checkout or adapter build may be incomplete. No rollback was attempted. Repair it manually before restarting; the update marker and any receipt are retained in .git/trajviz-obs-update.' : 'No checkout update was applied. Review a new plan after resolving this problem.'}`
      if (applying) {
        try {
          const temporary = `${paths.receipt}.${lockID}.tmp`
          await writeFile(temporary, JSON.stringify({ state: 'error', current: plan.current, target: plan.available, finishedAt: this.now(), error: message(error) }), { mode: 0o600, flag: 'wx' })
          await rename(temporary, paths.receipt)
        } catch { /* The durable incomplete marker still blocks another update. */ }
      }
      return { state: !applying && options.signal?.aborted ? 'cancelled' : 'error', text, ...(applying ? { receipt: paths.receipt } : {}) }
    } finally {
      if (locked) {
        try { if ((await json(paths.lock)).id === lockID) await unlink(paths.lock) } catch { /* Never remove another process's lock. */ }
      }
    }
  }
}

if (process.argv[2] === '--trajviz-local-update-worker' && process.argv[1]
  && path.resolve(fileURLToPath(import.meta.url)) === path.resolve(process.argv[1])) {
  await LocalCheckoutUpdater.runWorker(process.argv[3] ?? '')
}
