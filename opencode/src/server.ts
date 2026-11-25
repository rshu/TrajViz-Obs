import { Plugin } from '@opencode/plugin'
import { z } from 'zod'
import { Backend } from './backend.js'
import { Obs, type RunInput } from './rpc.js'
import { runCommand } from './commands.js'
import { cancelModel } from './llm.js'
import { errorMessage } from './tui-model.js'
import { CommandJobs } from './jobs.js'
import { commandUsageOutcome, createUsageReporter, type UsageOutcome, type UsageSurface } from './telemetry.js'
import { usageFeature } from './usage-features.js'
import { realpath } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { LocalCheckoutUpdater } from './shared/local-update.js'
import { LocalUpdateJobs } from './local-update-jobs.js'
import { PromptCapture } from './shared/prompt-capture.js'
import { registerPromptCapture } from './prompt-capture.js'

const optionsSchema = z.object({
  python: z.string().optional(), trajvizSource: z.string().min(1).optional(), cacheRoot: z.string().min(1).optional(),
  serverUrl: z.url().refine(value => {
    const url = new URL(value)
    return ['http:', 'https:'].includes(url.protocol) && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname) && !url.username && !url.password && !url.search && !url.hash && url.pathname === '/'
  }, 'serverUrl must be a loopback HTTP(S) URL on the OpenCode server host.').optional(),
  serverPasswordEnv: z.string().regex(/^[A-Za-z_][A-Za-z0-9_]*$/).optional(),
  llm: z.enum(['session', 'env']).optional(), analysisModel: z.string().optional(), labelModel: z.string().optional(),
  labelMaxSteps: z.number().int().min(1).max(10000).optional(), openBrowser: z.boolean().optional(), watch: z.boolean().optional(), statusline: z.boolean().optional(),
  dashboardPort: z.number().int().min(0).max(65535).optional(),
}).strict()

export default Plugin.define({
  id: 'trajviz-obs',
  async setup(ctx) {
    const backend = new Backend(ctx, optionsSchema.parse(ctx.options))
    const promptCapture = new PromptCapture({ adapter: 'opencode', bridge: backend.runtime.bridge,
      python: () => backend.runtime.telemetryPython(), hostVersion: ctx.app?.version })
    const closePromptCapture = await registerPromptCapture(ctx, promptCapture)
    const report = createUsageReporter(backend.runtime, ctx.app?.version)
    const awaiting = new Map<string, { confirmationID: string; feature: string; durationMs: number; surface: UsageSurface }>()
    const jobs = new Map<string, AbortController>()
    const asyncJobs = new CommandJobs({ cancelWork: runID => { jobs.get(runID)?.abort(); cancelModel(backend, runID) } })
    const lifetime = new AbortController()
    const tasks = new Set<Promise<unknown>>()
    const readers = new Map<AbortController, Promise<unknown>>()
    const track = <T>(task: Promise<T>): Promise<T> => { tasks.add(task); void task.then(() => tasks.delete(task), () => tasks.delete(task)); return task }
    function read<T>(requested: AbortSignal, work: (signal: AbortSignal) => Promise<T>): Promise<T> {
      if (localUpdates.busy) return Promise.reject(new Error('Obs is updating its checkout. Let the update finish before refreshing diagnostics.'))
      const controller = new AbortController(), signal = AbortSignal.any([requested, lifetime.signal, controller.signal])
      const pending = Promise.resolve().then(() => { signal.throwIfAborted(); return work(signal) })
      readers.set(controller, pending)
      void pending.finally(() => readers.delete(controller)).catch(() => {})
      return track(pending)
    }
    const samePath = (left: string, right: string) => process.platform === 'win32' ? left.toLowerCase() === right.toLowerCase() : left === right
    async function localSource(expected?: string): Promise<string> {
      lifetime.signal.throwIfAborted()
      const rows = (await ctx.plugin.list()).data.filter(row => row.id === 'trajviz-obs')
      lifetime.signal.throwIfAborted()
      if (rows.length !== 1 || rows[0]!.source.type !== 'local' || rows[0]!.state.status !== 'active') throw new Error('The server did not identify an active local Obs installation. Reconnect and check again.')
      const source = rows[0]!.source
      if (expected !== undefined && source.path !== expected) throw new Error('The configured Obs source changed. Check its installation before retrying.')
      const directory = await realpath(source.path), loaded = await realpath(fileURLToPath(new URL('..', import.meta.url)))
      lifetime.signal.throwIfAborted()
      if (!samePath(directory, loaded)) throw new Error('The loaded Obs server and its configured checkout differ. Restart the server before updating.')
      return directory
    }
    const localUpdater = new LocalCheckoutUpdater({
      adapter: 'opencode', loadedAt: Date.now(), entryURL: import.meta.url, workerSource: fileURLToPath(new URL('./shared/local-update.ts', import.meta.url)),
      validateSource: async directory => { if (!samePath(await localSource(), directory)) throw new Error('The local Obs installation changed before its update.') },
    })
    const localUpdates = new LocalUpdateJobs(localUpdater, async () => {
      lifetime.signal.throwIfAborted()
      if (jobs.size || awaiting.size) throw new Error('Finish running Obs commands before updating the shared checkout.')
      for (const controller of readers.keys()) controller.abort(new Error('Obs is preparing a confirmed plugin update.'))
      await Promise.allSettled(readers.values())
      await Promise.all([backend.stop('dashboard'), backend.stop('converge')])
      await localSource()
    })
    async function run(input: RunInput, surface: UsageSurface, requestSignal?: AbortSignal) {
      if (localUpdates.busy) return { text: 'Obs is updating its local checkout. Let it finish, then reload the plugin before running diagnostics.' }
      if (jobs.has(input.runID)) throw new Error('This obs command is already running.')
      const requested = report.enabled() ? usageFeature(input.args) : undefined
      // The client already reports the pane gesture. An overview here only seeds its saved session.
      const feature = input.paneSnapshot && requested === 'overview' ? undefined : requested
      const started = feature ? performance.now() : 0
      const saved = awaiting.get(input.runID)
      const prior = input.confirmationID === saved?.confirmationID ? saved : undefined
      if (prior) awaiting.delete(input.runID)
      let outcome: UsageOutcome = 'error'
      let confirmationID: string | undefined
      const controller = new AbortController()
      jobs.set(input.runID, controller)
      const signal = AbortSignal.any([controller.signal, lifetime.signal, ...(requestSignal ? [requestSignal] : [])])
      try {
        const result = await backend.withSources(input.useSynced, () => runCommand(backend, input, signal))
        // RPC's JSON codec rejects present-but-undefined optional properties.
        const response = JSON.parse(JSON.stringify(result)) as typeof result
        outcome = commandUsageOutcome(result)
        confirmationID = result.confirmation?.id
        return response
      }
      catch (error) {
        if (signal.aborted) outcome = 'cancelled'
        return { text: signal.aborted ? 'Cancelled.' : `obs: ${errorMessage(error)}` }
      }
      finally {
        jobs.delete(input.runID)
        if (signal.aborted) cancelModel(backend, input.runID)
        if (feature) {
          const durationMs = performance.now() - started + (prior?.durationMs ?? 0)
          if (confirmationID && !signal.aborted) {
            if (awaiting.size >= 8) awaiting.delete(awaiting.keys().next().value!)
            awaiting.set(input.runID, { confirmationID, feature, durationMs, surface })
          } else report({ feature, surface: prior?.surface ?? surface, durationMs,
            outcome: signal.aborted ? 'cancelled' : outcome })
        }
      }
    }
    const rpc = await ctx.rpc.register(Obs, {
      localUpdateCheck: async (input, call) => { await localSource(input.sourcePath); return localUpdater.check(call.signal) },
      localUpdateStart: async (input, call) => { await localSource(input.sourcePath); call.signal.throwIfAborted(); return localUpdates.start(input) },
      localUpdatePoll: async input => { await localSource(input.sourcePath); return localUpdates.poll(input) },
      run: (input, call) => track(run(input, 'terminal', call.signal)),
      start: async (input, call) => { call.signal.throwIfAborted(); return asyncJobs.start(input, fixed => track(run(fixed, 'desktop'))) },
      poll: async input => asyncJobs.poll(input),
      sync: input => track(backend.sync(input.source, input.requestID)),
      resolve: input => track(backend.resolve(input.sessionID, input.selector, input.captureSessionID)),
      snapshot: (input, call) => read(call.signal, signal => backend.withSources(input.useSynced, () => backend.snapshot(input.sessionID, signal, input.selector, input.captureSessionID))),
      usageStatus: async () => ({ enabled: report.enabled() }),
      usageInteraction: async input => { report(input); return {} },
      cancel: async input => {
        const job = jobs.get(input.runID)
        job?.abort()
        const plan = cancelModel(backend, input.runID)
        const pending = awaiting.get(input.runID)
        awaiting.delete(input.runID)
        if (pending && plan) report({ feature: pending.feature, surface: pending.surface,
          durationMs: pending.durationMs, outcome: 'cancelled' })
        const retained = asyncJobs.cancel(input.runID)
        return { cancelled: Boolean(job) || plan || retained }
      },
    })
    backend.progress = async (runID, text) => { asyncJobs.progress(runID, text); await rpc.events.emit('progress', {runID, text}) }
    backend.requestCapture = request => rpc.events.emit('capture', request)
    // Read-only data for the agent. This tool never calls another model or runs a diagnostic shell command.
    const tool = await ctx.tool.transform(editor => {
      editor.add({
        name: 'obs', description: 'Read TrajViz diagnostics for this OpenCode session. Returns a brief, issues, health, or one paged TrajViz view; makes no model calls.',
        input: z.object({ what: z.enum(['brief','issues','health','view']), view: z.string().optional(), options: z.record(z.string(), z.union([z.string(),z.number(),z.boolean()])).optional(), limit: z.number().int().min(1).max(500).default(25) }),
        async execute(input, call) {
          if (localUpdates.busy) return { content: 'Obs is updating its checkout. Let the update finish before reading diagnostics.' }
          const started = report.enabled() ? performance.now() : undefined
          let outcome: UsageOutcome = 'error'
          return read(call.signal, signal => backend.withSources(false, async () => {
            try {
              const source = await backend.session(call.sessionID, undefined, signal)
              const args = input.what === 'brief' ? ['brief', ...source.argv] : ['view', ...source.argv, ...(input.what === 'view' ? input.view ? [input.view] : [] : [input.what]), '--json', '--limit', String(input.limit)]
              for (const [key,value] of Object.entries(input.options ?? {})) args.push('--option', `${key}=${value}`)
              const data = await backend.runtime.run<Record<string, unknown>>(args, signal)
              let text = typeof data.brief === 'string' ? data.brief : JSON.stringify(data)
              if (Buffer.byteLength(text) > 65536) text = JSON.stringify({ path: data.path, note: 'Full output exceeds 64 KiB. Request a smaller limit or open the saved view.' })
              outcome = data.empty === true ? 'empty' : 'success'
              return { content: text }
            } catch (error) { return { content: `obs: ${errorMessage(error)}` } }
            finally {
              if (started !== undefined) report({ feature: 'tool', surface: 'unknown', durationMs: performance.now() - started,
                outcome: signal.aborted ? 'cancelled' : outcome })
            }
          }))
        },
      })
    })
    const observing = (async () => {
      try {
        for await (const event of ctx.event.subscribe({signal:lifetime.signal})) {
          if (!/^session\.(?:step\.|execution\.|tool\.|compaction\.|usage\.|revert\.|model\.|agent\.|deleted|moved)/.test(event.type)) continue
          const data = event.data as {sessionID?:unknown}
          if (typeof data.sessionID !== 'string') continue
          backend.invalidate(data.sessionID)
          if (event.type === 'session.deleted') await backend.stop('dashboard', data.sessionID)
          await rpc.events.emit('changed', {sessionID:data.sessionID})
        }
      } catch (error) { if (!lifetime.signal.aborted) console.error('TrajViz event observation stopped:', errorMessage(error)) }
    })()
    tasks.add(observing)
    report({ feature: 'activation', surface: 'unknown', durationMs: 0, outcome: 'success' })
    return async () => {
      localUpdates.close()
      lifetime.abort()
      asyncJobs.close()
      for (const job of jobs.values()) job.abort()
      cancelModel(backend)
      const closed = backend.close()
      await Promise.allSettled([...tasks, rpc.dispose(), tool.dispose(), closePromptCapture(), closed])
      await report.flush()
    }
  },
})
