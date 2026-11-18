import type { LocationRef, OpenCodeClient, PluginInfo } from '@opencode/client'
import manifest from '../package.json' with { type: 'json' }
import { Obs } from './rpc.js'
import type { LocalUpdateResult } from '../../shared/local-update.js'
import { errorMessage, safeText } from './tui-model.js'

type Confirmation = { title: string; message: string }
export type UpdateResult = { text: string; state: 'manual' | 'pinned' | 'available' | 'current' | 'updated' | 'blocked' | 'cancelled' | 'error'; restartRequired?: true }
type UpdateOptions = {
  check: boolean
  location: LocationRef
  signal: AbortSignal
  confirm(question: Confirmation, signal: AbortSignal): Promise<boolean>
  progress(text: string): void
  current(): boolean
  server?: string
}
const running = new WeakSet<object>()
const PLUGIN = 'trajviz-obs'
const display = (value: string): string => safeText(value)
  .replace(/([a-z][a-z0-9+.-]*:\/\/)[^/\s@]+@/gi, '$1[redacted]@')
  .replace(/([a-z][a-z0-9+.-]*:\/\/[^?#\s]+)\?[^#\s]*/gi, '$1?[redacted]')
const clean = (value: string): string => display(value).replace(/\n/g, ' ')
const identity = (plugin: PluginInfo): string => plugin.source.type === 'package'
  ? JSON.stringify(['package', plugin.source.target, plugin.source.version ?? null])
  : JSON.stringify(plugin.source)

/** Exact package versions and full Git commits must retain their configured pin. */
export function pinnedTarget(target: string): boolean {
  return /^(?:@[^/\s]+\/)?[^@/\s]+@=?v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/.test(target.replace(/^npm:/, ''))
    || /#[0-9a-f]{40}(?:[0-9a-f]{24})?(?:::path:.+)?$/i.test(target)
}

async function confirmation(options: UpdateOptions, question: Confirmation): Promise<boolean> {
  options.signal.throwIfAborted()
  let abort: () => void = () => {}
  const cancelled = new Promise<false>(resolve => {
    abort = () => resolve(false)
    options.signal.addEventListener('abort', abort, { once: true })
  })
  try {
    return await Promise.race([
      cancelled,
      Promise.resolve().then(() => { options.signal.throwIfAborted(); return options.confirm(question, options.signal) }),
    ])
  } finally { options.signal.removeEventListener('abort', abort) }
}

async function pause(signal: AbortSignal, milliseconds = 100): Promise<void> {
  signal.throwIfAborted()
  await new Promise<void>((resolve, reject) => {
    const abort = () => { clearTimeout(timer); reject(signal.reason) }
    const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve() }, milliseconds)
    signal.addEventListener('abort', abort, { once: true })
  })
}

/** The connected host owns package updates and confirmed local-checkout builds. */
export async function updatePlugin(client: OpenCodeClient, options: UpdateOptions): Promise<UpdateResult> {
  if (running.has(client)) return { state: 'error', text: 'An Obs update check is already running on this connection. Wait for it to finish.' }
  running.add(client)
  const location = { ...options.location }
  let dispatched = false
  let localDispatched = false
  const check = () => {
    options.signal.throwIfAborted()
    if (!options.current()) throw new Error('The server reconnected. Run /obs update --check on the current connection.')
  }
  const find = async (signal = options.signal): Promise<PluginInfo> => {
    check()
    signal.throwIfAborted()
    const list = await client.plugin.list({ location }, { signal })
    check()
    const matches = list.data.filter(plugin => plugin.id === PLUGIN)
    if (matches.length !== 1) throw new Error(matches.length ? 'More than one Obs installation matched. Update it through OpenCode’s plugin manager.' : 'The connected server did not identify its Obs installation. Check OpenCode’s plugin settings.')
    return matches[0]!
  }
  const serverLabel = async (): Promise<string> => {
    if (options.server) return clean(options.server)
    const info = await client.server.info({ signal: options.signal })
    check()
    return clean([info.hostname, ...info.urls.map(value => {
      try { const url = new URL(value); return `${url.protocol}//${url.host}` } catch { return '' }
    })].filter(Boolean).join(' · ') || 'Connected OpenCode server')
  }
  const idle = async () => {
    check()
    const active = await client.session.active({ signal: options.signal })
    check()
    if (Object.keys(active).length) throw new Error('Wait for all running tasks on this OpenCode server to finish before updating the shared local checkout.')
  }
  const localResult = (result: LocalUpdateResult): UpdateResult => ({
    state: result.state,
    text: `Loaded UI version: ${manifest.version}.\n${display(result.text)}`,
    ...(result.restartRequired ? { restartRequired: true as const } : {}),
  })
  try {
    if (typeof location.directory !== 'string' || !location.directory.trim()) throw new Error('The project location is not ready. Open a project and run /obs update again.')
    check(); options.progress('Checking this server’s Obs installation…')
    const plugin = await find(), source = plugin.source
    if (source.type === 'local') {
      const sourcePath = source.path, rpc = client.rpc(Obs), request = { location, signal: options.signal }
      options.progress('Checking the local Obs checkout and its remote revision…')
      let reviewed: LocalUpdateResult
      for (let attempt = 0;; attempt++) {
        check()
        try { reviewed = await rpc.localUpdateCheck({ sourcePath }, request); break }
        catch (error) {
          check()
          const type = typeof error === 'object' && error !== null && 'type' in error ? error.type : undefined
          const delay = [200, 500, 1000][attempt]
          // A newly opened workspace can reject read-only calls until its
          // plugin registers. Never apply this retry to an update start.
          if (type === 'rpc.unavailable' && delay !== undefined) { await pause(options.signal, delay); continue }
          if (type === 'rpc.unavailable' || type === 'rpc.method_not_found') {
            return { state: 'error', text: 'Local update support is unavailable in the running Obs server. It may still be starting or may use older code than the UI. Finish current work, then restart the connected server with its matching-version OpenCode CLI in the original launch environment. For a managed local service, use `opencode service restart`. On macOS, Desktop’s matching CLI is `"/Applications/OpenCode.app/Contents/Resources/opencode-cli" service restart`. Reopen the interface afterward; quitting Desktop alone leaves its service running. Then run /obs update again.' }
          }
          throw error
        }
      }
      check()
      if (options.check || reviewed.state !== 'available') return localResult(reviewed)
      const plan = reviewed.plan
      if (!plan) throw new Error('The server did not provide a reviewed local update plan. Restart its Obs plugin and check again.')
      await idle()
      const server = await serverLabel()
      const transport = plan.effectiveRemote && plan.effectiveRemote !== plan.remote ? `\nTransport: ${clean(plan.effectiveRemote)}` : ''
      const accepted = await confirmation(options, {
        title: 'Update TrajViz Obs?',
        message: `Server: ${server}\nProject: ${clean(location.directory)}\nCheckout: ${clean(plan.root)}\nBranch: ${clean(plan.branch)}\nRemote: ${clean(plan.remote)}${transport}\nInstalled: ${clean(plan.current)}\nAvailable: ${clean(plan.available)}\nVersion: ${clean(plan.currentVersion)} → ${clean(plan.availableVersion)}\n\n${plan.steps.map((step, index) => `${index + 1}. ${clean(step)}`).join('\n')}\n\nThis checkout may serve other projects, plugins, and clients. Its shared files and generated adapter outputs will change. Configuration and telemetry preferences are kept. Restart the server only after the update finishes.`,
      })
      check()
      if (!accepted) return { state: 'cancelled', text: 'Obs update cancelled before dispatch. No checkout was changed.' }
      await idle()
      if (identity(await find()) !== identity(plugin)) throw new Error('The Obs installation changed while confirmation was open. Run /obs update --check again before updating.')
      check(); options.progress('Updating and building the local Obs checkout on the server. Let it finish before restarting…')
      check()
      const runID = crypto.randomUUID()
      dispatched = true; localDispatched = true
      // An acknowledgement can be lost after the server has started. Poll the
      // same job; never send a second apply request to resolve an uncertain one.
      let startError: unknown
      try { await rpc.localUpdateStart({ sourcePath, token: plan.token, runID }, request) }
      catch (error) { check(); startError = error }
      for (;;) {
        check()
        const state = await rpc.localUpdatePoll({ sourcePath, runID }, request)
        check()
        if (state.state === 'finished') return localResult(state.result)
        if (state.state === 'expired') throw startError ?? new Error('The local update result expired. Check the checkout state before trying again.')
        startError = undefined
        if (state.progress) options.progress(display(state.progress))
        await pause(options.signal)
      }
    }
    if (source.type !== 'package') return { state: 'manual', text: 'This Obs installation is not a host-managed package. Update its configured source manually, then reload the plugin.' }
    const target = source.target
    if (!target.trim()) throw new Error('OpenCode returned an empty Obs package target. Check the plugin configuration.')
    if (pinnedTarget(target)) return { state: 'pinned', text: `Obs is pinned to ${clean(target)} (installed ${source.version ? clean(source.version) : 'version unknown'}). Its configured version will stay pinned. Change the version explicitly in OpenCode’s plugin configuration to upgrade.` }
    if (source.updating) throw new Error('OpenCode is already updating Obs. Wait for that update to finish.')
    options.progress('Checking the configured Obs package for an update…')
    const available = await client.plugin.check({ location, target }, { signal: options.signal })
    check()
    const matches = available.data.filter(row => row.id === PLUGIN && row.source.type === 'package' && row.source.target === target)
    if (matches.length !== 1) throw new Error('OpenCode did not return update information for this Obs target. Check its installation in the native plugin manager.')
    const checked = matches[0]!
    if (checked.source.type !== 'package' || checked.source.target !== target) throw new Error('The configured Obs source changed during the check. Run /obs update --check again.')
    if (checked.state.status === 'failed') throw new Error(`OpenCode reports that Obs failed to load: ${checked.state.error}`)
    if (identity(await find()) !== identity(checked)) throw new Error('The configured Obs source changed during the check. Run /obs update --check again.')
    if (checked.source.updating) throw new Error('OpenCode is already updating Obs. Wait for that update to finish.')
    if (!checked.source.outdated) return { state: 'current', text: `OpenCode reports no available update for the configured Obs target: ${clean(target)} (installed ${checked.source.version ? clean(checked.source.version) : 'version unknown'}).` }
    const summary = `${clean(target)} · installed ${checked.source.version ? clean(checked.source.version) : 'version unknown'}`
    if (options.check) return { state: 'available', text: `An Obs package update is available: ${summary}.\nRun /obs update to review and apply it on this server.` }
    const server = await serverLabel()
    const accepted = await confirmation(options, {
      title: 'Update TrajViz Obs?',
      message: `Server: ${clean(server)}\nProject: ${clean(location.directory)}\nPackage: ${summary}\n\nOpenCode will update only this configured package and reload affected plugin instances. This installation may be shared by other sessions or clients on the server. Existing plugin work may be interrupted.`,
    })
    check()
    if (!accepted) return { state: 'cancelled', text: 'Obs update cancelled before dispatch. No package was changed.' }
    const latest = await find()
    if (identity(latest) !== identity(checked)) throw new Error('The Obs installation changed while confirmation was open. Run /obs update --check again before updating.')
    if (latest.state.status === 'failed') throw new Error(`Obs failed to load while confirmation was open: ${latest.state.error}`)
    if (latest.source.type === 'package' && latest.source.updating) throw new Error('OpenCode is already updating Obs. Wait for that update to finish.')
    check(); options.progress('Updating Obs through OpenCode. The host may reload this plugin; run /obs update --check after it reconnects.')
    check()
    dispatched = true
    await client.plugin.update({ location, targets: [target] }, { signal: options.signal })
    check()
    const verification = AbortSignal.any([options.signal, AbortSignal.timeout(5000)])
    for (;;) {
      const installed = await find(verification)
      if (installed.source.type !== 'package' || installed.source.target !== target) return { state: 'error', text: 'OpenCode completed its update request, but the configured Obs source changed. Check the installation in the native plugin manager.' }
      if (installed.state.status === 'failed') return { state: 'error', text: `OpenCode completed its update request, but Obs failed to load: ${clean(installed.state.error)}. Check the native plugin manager.` }
      // Native update completion can precede the location's reload notification.
      // Observe that reload; never send a second mutation to resolve an old view.
      if (!installed.source.updating && !installed.source.outdated) return { state: 'updated', text: `OpenCode completed the Obs update request (installed ${installed.source.version ? clean(installed.source.version) : 'version unknown'}) and requested plugin reloads. Run /obs update --check after the UI reconnects to verify its state. Existing configuration and telemetry preferences were kept.` }
      await pause(verification)
    }
  } catch (error) {
    if (localDispatched) return { state: options.signal.aborted ? 'cancelled' : 'error', text: 'Stopped waiting for the local Obs update. The server may still be updating or building the checkout. Let it finish; do not restart the server or start another update yet. Run /obs update --check after reconnecting to verify its state.' }
    if (dispatched) return { state: options.signal.aborted ? 'cancelled' : 'error', text: 'Stopped waiting for OpenCode’s update result. The server may still finish the update or reload the plugin. Run /obs update --check after reconnecting to verify its state; do not start another update yet.' }
    if (options.signal.aborted) return { state: 'cancelled', text: 'Obs update cancelled before dispatch. No package was changed.' }
    return { state: 'error', text: `Obs update: ${clean(errorMessage(error))}` }
  } finally { running.delete(client) }
}
