import { execFile } from 'node:child_process'
import { lstat, readFile, realpath } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { DefaultPackageManager, SettingsManager, getAgentDir, type ExtensionContext } from '@earendil-works/pi-coding-agent'
import { LocalCheckoutUpdater } from '../../shared/local-update.js'

type SourceInfo = { path: string; source: string; scope: 'user' | 'project' | 'temporary'; origin: 'package' | 'top-level' }
type Manager = Pick<DefaultPackageManager, 'listConfiguredPackages' | 'update'>
type Git = (args: string[], cwd: string, signal?: AbortSignal) => Promise<string>
export type UpdateResult = { text: string; outcome?: 'success' | 'error' | 'rejected' | 'cancelled'; reload?: true; applyUpdate?: () => Promise<UpdateResult> }
export type UpdateOptions = {
  sourceInfo?: SourceInfo; currentSourceInfo?: () => SourceInfo | undefined; entryPath?: string; agentDir?: string
  settings?: SettingsManager; git?: Git; manager?: (settings: SettingsManager) => Manager
  localLoadedAt?: number
  confirm?: (question: { title: string; message: string }, signal: AbortSignal) => Promise<boolean>
  localUpdater?: (options: ConstructorParameters<typeof LocalCheckoutUpdater>[0]) => Pick<LocalCheckoutUpdater, 'check' | 'apply'>
}
const inside = (root: string, file: string) => { const relative = path.relative(root, file); return !relative || (!path.isAbsolute(relative) && relative !== '..' && !relative.startsWith(`..${path.sep}`)) }
export function manualUpdateCommand(root: string, platform = process.platform): string | undefined {
  if (/[\x00-\x1f\x7f-\x9f]/.test(root)) return
  return `git -C '${root.replace(/'/g, platform === 'win32' || /^[A-Za-z]:[\\/]|^\\\\/.test(root) ? "''" : `'\\''`)}' pull --ff-only`
}
const git: Git = (args, cwd, signal) => new Promise((resolve, reject) => {
  execFile('git', args, { cwd, signal, timeout: 15_000, maxBuffer: 1024 * 1024, windowsHide: true,
    env: { ...process.env, GIT_TERMINAL_PROMPT: '0', GIT_OPTIONAL_LOCKS: '0' } },
  (error, stdout) => error ? reject(new Error('Git inspection failed. Check the installation and network access.')) : resolve(stdout))
})
function gitSource(source: string): { identity: string; ref?: string } | undefined {
  let value = source.replace(/^git:/, '')
  let host: string, pathname: string
  try {
    if (/^[a-z]+:\/\//i.test(value)) {
      const url = new URL(value), defaultPort = url.protocol === 'ssh:' ? '22' : url.protocol === 'git:' ? '9418' : ''
      host = `${url.hostname}${url.port && url.port !== defaultPort ? `:${url.port}` : ''}`; pathname = url.pathname
    }
    else if (value.startsWith('git@')) { const match = /^git@([^:]+):(.+)$/.exec(value); if (!match) return; host = match[1]!; pathname = match[2]! }
    else { const match = /^([^/]+\.[^/]+)\/(.+)$/.exec(value); if (!match) return; host = new URL(`https://${match[1]!}`).host; pathname = match[2]! }
    const separator = pathname.indexOf('@'), ref = separator < 0 ? undefined : pathname.slice(separator + 1)
    pathname = (separator < 0 ? pathname : pathname.slice(0, separator)).replace(/^\/+|\/+$/g, '').replace(/\.git$/, '')
    if (!host || !pathname || (separator >= 0 && !ref)) return
    host = host.toLowerCase()
    return { identity: `${host}/${host === 'github.com' ? pathname.toLowerCase() : pathname}`, ...(ref ? { ref } : {}) }
  } catch { return }
}
async function manifest(root: string): Promise<{ name?: string; version?: string }> {
  try { const doc = JSON.parse(await readFile(path.join(root, 'package.json'), 'utf8')); return { name: doc.name, version: doc.version } }
  catch { return {} }
}
async function localRoot(entry: string): Promise<string> {
  let directory = path.dirname(entry), found = directory
  for (let depth = 0; depth < 5; depth++) {
    const doc = await manifest(directory)
    if (['trajviz-obs', 'trajviz-obs-pi'].includes(doc.name ?? '')) found = directory
    const parent = path.dirname(directory); if (parent === directory) break; directory = parent
  }
  return found
}
function offline(): boolean { return /^(1|true|yes)$/i.test(process.env.PI_OFFLINE ?? '') }
async function localUpdate(ctx: Pick<ExtensionContext, 'cwd' | 'isProjectTrusted' | 'isIdle' | 'hasPendingMessages'>, check: boolean, signal: AbortSignal, entry: string, info: SourceInfo, options: UpdateOptions): Promise<UpdateResult> {
  const trusted = ctx.isProjectTrusted()
  if (info.scope === 'project' && !trusted) return { text: 'Approve this project’s settings in Pi before updating its plugin.', outcome: 'rejected' }
  if (offline()) return { text: 'Local TrajViz Obs checkout\nAvailable revision: unknown (Pi offline mode). No update performed.', outcome: check ? 'success' : 'rejected' }
  const original = JSON.stringify([info.path, info.source, info.scope, info.origin])
  let applying = false
  const idle = () => { if (applying && (!ctx.isIdle() || ctx.hasPendingMessages())) throw new Error('Pi started new work. Wait for it to finish before updating.') }
  const validateSource = async (adapter: string) => {
    idle()
    if (ctx.isProjectTrusted() !== trusted || offline()) throw new Error('Trust or offline settings changed. Review a new update plan.')
    const current = options.currentSourceInfo ? options.currentSourceInfo() : info
    if (!current || JSON.stringify([current.path, current.source, current.scope, current.origin]) !== original || await realpath(current.path) !== entry || !inside(adapter, entry)) throw new Error('The loaded Pi extension changed. Run /reload before updating.')
    idle()
    if (info.origin !== 'package' || info.scope === 'temporary') return
    const agentDir = options.agentDir ?? getAgentDir()
    const settings = options.settings ?? SettingsManager.create(ctx.cwd, agentDir, { projectTrusted: trusted })
    await settings.reload()
    if (settings.drainErrors().length) throw new Error('Pi package settings could not be read safely. Repair them before updating.')
    const manager = options.manager?.(settings) ?? new DefaultPackageManager({ cwd: ctx.cwd, agentDir, settingsManager: settings })
    const configured = manager.listConfiguredPackages()
    const matches = []
    for (const pkg of configured) if (pkg.installedPath) {
      try { if (inside(await realpath(pkg.installedPath), entry)) matches.push(pkg) } catch { /* An unrelated missing package is not this loaded source. */ }
    }
    if (matches.length !== 1 || matches[0]!.scope !== info.scope || matches[0]!.source !== info.source) throw new Error('The local Pi package registration changed or is ambiguous. Run /reload before updating.')
    idle()
  }
  const sourceWorker = fileURLToPath(new URL('../../shared/local-update.ts', import.meta.url))
  let workerSource = fileURLToPath(new URL('../runtime/local-update.ts', import.meta.url))
  try { if ((await lstat(sourceWorker)).isFile()) workerSource = sourceWorker } catch { /* Bundled installs package their own standalone worker. */ }
  const factory = options.localUpdater ?? (options => new LocalCheckoutUpdater(options))
  const updater = factory({ adapter: 'pi', entryURL: pathToFileURL(options.entryPath ?? info.path).href, workerSource, validateSource, loadedAt: options.localLoadedAt })
  const inspected = await updater.check(signal)
  signal.throwIfAborted()
  if (check || inspected.state !== 'available' || !inspected.plan) return { text: inspected.text, outcome: inspected.state === 'blocked' ? 'rejected' : inspected.state === 'error' ? 'error' : inspected.state === 'cancelled' ? 'cancelled' : 'success' }
  if (!options.confirm) return { text: `${inspected.text}\nUse the direct /obs update command in an interactive Pi session to review and confirm this update.`, outcome: 'rejected' }
  const plan = inspected.plan
  const transport = plan.effectiveRemote && plan.effectiveRemote !== plan.remote ? `\nTransport: ${plan.effectiveRemote}` : ''
  const accepted = await options.confirm({ title: 'Update the local TrajViz Obs checkout?', message: `Checkout: ${plan.root}\nBranch: ${plan.branch} → origin/main\nOrigin: ${plan.remote}${transport}\nVersion: ${plan.currentVersion} → ${plan.availableVersion}\nInstalled: ${plan.current.slice(0, 12)}\nReviewed revision: ${plan.available}\n\nFast-forward this shared repository, including other coding-agent adapters.\nReload Pi when idle; other Pi sessions also need /reload.` }, signal)
  signal.throwIfAborted()
  if (!accepted) return { text: 'Local Obs update cancelled. No checkout files were changed.', outcome: 'cancelled' }
  return { text: `Applying the reviewed local Obs revision ${plan.available.slice(0, 12)}. Let it finish before exiting Pi.`, applyUpdate: async () => {
    if (!ctx.isIdle() || ctx.hasPendingMessages()) return { text: 'Pi started new work. No checkout update was started. Run /reload after the task finishes, then try again.', outcome: 'error' }
    applying = true
    try {
      const applied = await updater.apply(plan.token, { signal })
      return applied.state === 'updated' ? { text: applied.text, reload: true, outcome: 'success' }
        : { text: `${applied.text}\nAfter resolving this problem, run /reload when Pi is idle.`, outcome: applied.state === 'cancelled' ? 'cancelled' : 'error' }
    } finally { applying = false }
  } }
}
function generated(status: string, file: string): boolean {
  if (status === '??' && file === 'package-lock.json') return true
  return status === '!!' && (file === 'node_modules/' || /(?:^|\/)__pycache__\/$/.test(file) || /\.py[co]$/.test(file))
}
async function clean(root: string, run: Git, signal?: AbortSignal): Promise<void> {
  const status = await run(['status', '--porcelain=v1', '-z', '--untracked-files=all', '--ignored=matching'], root, signal)
  if (status.split('\0').some(row => row && !generated(row.slice(0, 2), row.slice(3)))) {
    throw new Error('The Pi-managed plugin checkout contains local changes or files. Update it manually after saving your work; /obs update will not discard them.')
  }
}
async function complete(root: string): Promise<void> {
  const marker = path.join(path.dirname(root), `.${path.basename(root)}.pi-update-incomplete`)
  try { await lstat(marker) }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return
    throw new Error('Pi’s previous update state could not be verified. Inspect the installation in its native package manager.')
  }
  throw new Error('Pi marked this installation’s update as incomplete. Repair it with Pi’s native package manager before trying /obs update again.')
}
/** A read-only settings view limits Pi's native updater to one source and scope. */
function scopedSettings(settings: SettingsManager, source: string, scope: 'user' | 'project', trusted: boolean): SettingsManager {
  const values = {
    global: JSON.stringify({ ...settings.getGlobalSettings(), packages: scope === 'user' ? [source] : [] }),
    project: JSON.stringify({ ...settings.getProjectSettings(), packages: scope === 'project' ? [source] : [] }),
  }
  return SettingsManager.fromStorage({ withLock(key, callback) {
    if (callback(values[key]) !== undefined) throw new Error('Plugin updates cannot change Pi settings.')
  } }, { projectTrusted: trusted })
}
/** Preparation never updates files. The direct command invokes apply only after its revision has ended. */
export async function prepareUpdate(ctx: Pick<ExtensionContext, 'cwd' | 'isProjectTrusted' | 'isIdle' | 'hasPendingMessages'>, check: boolean, signal: AbortSignal, options: UpdateOptions = {}): Promise<UpdateResult> {
  signal.throwIfAborted()
  const entry = await realpath(options.entryPath ?? options.sourceInfo?.path ?? fileURLToPath(import.meta.url))
  const info = options.sourceInfo
  const manual = async (): Promise<UpdateResult> => {
    const root = await localRoot(entry), command = manualUpdateCommand(root)
    const instructions = command
      ? `Update this checkout yourself${process.platform === 'win32' ? ' in PowerShell' : ''}, then run /reload:\n${command}`
      : 'Open the plugin’s local checkout in a terminal and update it manually, then run /reload.'
    return { outcome: check ? 'success' : 'rejected', text: `Local TrajViz Obs checkout · ${(await manifest(root)).version ?? 'version unknown'}\nThe loaded extension could not be matched to a configured installation.\n${instructions}` }
  }
  if (!info || info.origin !== 'package' || !gitSource(info.source)) {
    if (info?.source.startsWith('npm:')) return { text: 'This package source is not supported by /obs update. Use Pi’s package manager, then /reload.', outcome: 'rejected' }
    return info ? localUpdate(ctx, check, signal, entry, info, options) : manual()
  }
  if (info.scope === 'temporary') return { text: 'Temporary Pi packages cannot be updated here. Install the package with pi install first.', outcome: 'rejected' }
  const trusted = ctx.isProjectTrusted()
  if (info.scope === 'project' && !trusted) return { text: 'Approve this project’s settings in Pi before updating its plugin.', outcome: 'rejected' }
  const agentDir = options.agentDir ?? getAgentDir()
  const settings = options.settings ?? SettingsManager.create(ctx.cwd, agentDir, { projectTrusted: trusted })
  if (settings.drainErrors().length) throw new Error('Pi package settings could not be read safely. Repair them before updating.')
  const create = options.manager ?? (settings => new DefaultPackageManager({ cwd: ctx.cwd, agentDir, settingsManager: settings }))
  const configured = create(settings).listConfiguredPackages()
  const identity = gitSource(info.source)!
  const candidates = configured.filter(pkg => gitSource(pkg.source)?.identity === identity.identity)
  if (candidates.length !== 1) return { text: candidates.length ? 'This plugin is configured more than once. Resolve the user/project package registrations before updating.' : 'The loaded extension does not match a configured Pi package. Use Pi’s package manager, then /reload.', outcome: 'rejected' }
  const selected = candidates[0]!
  if (selected.scope !== info.scope || selected.source !== info.source || !selected.installedPath) return { text: 'The loaded extension and configured installation differ. Reload Pi before updating.', outcome: 'rejected' }
  const root = await realpath(selected.installedPath)
  const base = info.scope === 'project' ? ctx.cwd : agentDir
  const relative = path.relative(path.resolve(base), path.resolve(selected.installedPath))
  if (!inside(path.resolve(base), path.resolve(selected.installedPath)) || root !== path.join(await realpath(base), relative)) return { text: 'The managed package path contains a symlink or is outside Pi’s package storage. Update it manually.', outcome: 'rejected' }
  if (!inside(root, entry) || (await manifest(root)).name !== 'trajviz-obs') return { text: 'The loaded extension is outside the expected TrajViz Obs installation. Use Pi’s package manager.', outcome: 'rejected' }
  await complete(root)
  const provenance = await Promise.all([lstat(root), lstat(path.join(root, '.git'))])
  if (provenance.some(item => !item.isDirectory())) throw new Error('The plugin must use an independent managed Git directory, without linked Git metadata.')
  const run = options.git ?? git
  if (path.resolve((await run(['rev-parse', '--show-toplevel'], root, signal)).trim()) !== root) throw new Error('The plugin is not an independent managed Git checkout.')
  const remoteIdentity = gitSource((await run(['remote', 'get-url', 'origin'], root, signal)).trim())?.identity
  if (remoteIdentity !== identity.identity) return { text: 'The checkout’s origin differs from its configured Pi package. Update it manually.', outcome: 'rejected' }
  const current = (await run(['rev-parse', '--verify', 'HEAD'], root, signal)).trim()
  if (!/^[a-f0-9]{40,64}$/.test(current)) throw new Error('The plugin’s installed Git revision is unknown.')
  const version = (await manifest(root)).version ?? 'unknown'
  const header = `TrajViz Obs ${version} · ${selected.scope} Git installation · ${current.slice(0, 12)}`
  if (identity.ref) return { text: `${header}\nConfigured ref: ${identity.ref}. /obs update preserves explicitly selected refs; use Pi’s package manager to change or reconcile this ref.`, outcome: check ? 'success' : 'rejected' }
  if (offline()) return { text: `${header}\nAvailable revision: unknown (Pi offline mode). No update performed.`, outcome: check ? 'success' : 'rejected' }
  let upstream: string
  try { upstream = (await run(['rev-parse', '--abbrev-ref', '@{upstream}'], root, signal)).trim() }
  catch { signal.throwIfAborted(); return { text: `${header}\nAvailable revision: unknown (no tracked origin branch). Use Pi’s package manager.`, outcome: 'rejected' } }
  if (!/^origin\/[^\s]+$/.test(upstream)) return { text: `${header}\nOnly an existing tracked origin branch can be updated here. Use Pi’s package manager.`, outcome: 'rejected' }
  const branch = (await run(['symbolic-ref', '--short', 'HEAD'], root, signal)).trim()
  if (branch !== upstream.slice(7)) return { text: `${header}\nThe managed checkout uses a custom local branch. Update it manually.`, outcome: 'rejected' }
  const divergence = (await run(['rev-list', '--left-right', '--count', 'HEAD...@{upstream}'], root, signal)).trim().split(/\s+/).map(Number)
  if (divergence.length !== 2 || divergence.some(n => !Number.isSafeInteger(n) || n < 0) || divergence[0]! > 0) return { text: `${header}\nThe managed checkout has local commits or unknown branch history. Save them and update manually.`, outcome: 'rejected' }
  let available: string
  try {
    const remote = await run(['ls-remote', '--exit-code', 'origin', `refs/heads/${upstream.slice(7)}`], root, signal)
    available = remote.split(/\s+/)[0] ?? ''
    if (!/^[a-f0-9]{40,64}$/.test(available)) throw new Error('Unknown remote revision')
  } catch { signal.throwIfAborted(); return { text: `${header}\nAvailable revision: unknown (remote check failed). No update performed.`, outcome: 'error' } }
  const changed = available !== current
  const text = `${header}\nAvailable revision: ${available.slice(0, 12)}${changed ? ' · update available' : ' · up to date'}. Available version: unknown until installed.`
  if (check || !changed) return { text }
  await clean(root, run, signal)
  signal.throwIfAborted()
  return { text: `${text}\nApplying Pi’s package update; let it finish before exiting Pi.`, applyUpdate: async () => {
    // Recheck local files at the mutation boundary. Native updates are not atomic;
    // do not reset or roll back a user checkout after a failure or interruption.
    let started = false
    try {
      if (ctx.isProjectTrusted() !== trusted || offline()) throw new Error('Trust or offline settings changed. No update started.')
      await settings.reload()
      const registrations = create(settings).listConfiguredPackages().filter(pkg => gitSource(pkg.source)?.identity === identity.identity)
      if (settings.drainErrors().length || registrations.length !== 1 || registrations[0]?.source !== selected.source || registrations[0]?.scope !== selected.scope || !registrations[0]?.installedPath || await realpath(registrations[0].installedPath) !== root) throw new Error('The package registration changed. Reload Pi before trying again.')
      const latestRoot = await Promise.all([lstat(root), lstat(path.join(root, '.git'))])
      if (root !== path.join(await realpath(base), relative)) throw new Error('The managed package storage changed. Inspect it before trying again.')
      if (latestRoot.some((item, index) => !item.isDirectory() || item.dev !== provenance[index]!.dev || item.ino !== provenance[index]!.ino)) throw new Error('The managed checkout or its Git metadata was replaced. Inspect it before trying again.')
      if ((await run(['rev-parse', '--verify', 'HEAD'], root)).trim() !== current || gitSource((await run(['remote', 'get-url', 'origin'], root)).trim())?.identity !== identity.identity) throw new Error('The managed checkout changed. Inspect it before trying again.')
      if ((await run(['symbolic-ref', '--short', 'HEAD'], root)).trim() !== branch || (await run(['rev-parse', '--abbrev-ref', '@{upstream}'], root)).trim() !== upstream) throw new Error('The managed checkout branch or upstream changed. Inspect it before trying again.')
      await clean(root, run)
      await complete(root)
      if (!ctx.isIdle() || ctx.hasPendingMessages()) throw new Error('Pi started new work. Wait for it to finish before updating.')
      started = true
      try { await create(scopedSettings(settings, selected.source, selected.scope, trusted)).update(selected.source) }
      catch { throw new Error('Pi’s native package update failed. Inspect its result in the native package manager.') }
      await complete(root)
      const next = (await run(['rev-parse', '--verify', 'HEAD'], root)).trim()
      const installedUpstream = (await run(['rev-parse', '--verify', '@{upstream}'], root)).trim()
      if (!/^[a-f0-9]{40,64}$/.test(next) || next !== installedUpstream || (await run(['symbolic-ref', '--short', 'HEAD'], root)).trim() !== branch) throw new Error('The installed revision or branch differs from its update target. Inspect the installation before reloading.')
      if (next === current) throw new Error('Pi did not advance the plugin revision. An updated installation could not be verified; check the native package manager.')
      return { text: `TrajViz Obs updated · ${(await manifest(root)).version ?? 'version unknown'} · ${next.slice(0, 12)}.`, reload: true }
    } catch (error) {
      return { outcome: 'error', text: `${error instanceof Error ? error.message : 'Plugin update failed.'}\n${started ? 'The managed installation may be incomplete. Repair it with Pi’s package manager, then restart Pi. No rollback was attempted.' : 'No update was started. Run /reload after resolving this problem.'}` }
    }
  } }
}
