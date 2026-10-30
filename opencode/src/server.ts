import { Plugin } from '@opencode/plugin'
import { z } from 'zod'
import { Backend } from './backend.js'
import { Obs, type RunInput } from './rpc.js'
import { runCommand } from './commands.js'
import { cancelModel } from './llm.js'
import { safeText } from './tui-model.js'

const optionsSchema = z.object({
  python: z.string().optional(), trajvizSource: z.string().min(1).optional(), cacheRoot: z.string().min(1).optional(),
  serverUrl: z.url().refine(value => {
    const url = new URL(value)
    return ['http:', 'https:'].includes(url.protocol) && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname) && !url.username && !url.password && !url.search && !url.hash && url.pathname === '/'
  }, 'serverUrl must be a loopback HTTP(S) URL on the OpenCode server host.').optional(),
  serverPasswordEnv: z.string().regex(/^[A-Za-z_][A-Za-z0-9_]*$/).optional(),
  llm: z.enum(['session', 'env']).optional(), analysisModel: z.string().optional(), labelModel: z.string().optional(),
  labelMaxSteps: z.number().int().min(1).max(10000).optional(), openBrowser: z.boolean().optional(), watch: z.boolean().optional(), statusline: z.boolean().optional(),
}).strict()

export default Plugin.define({
  id: 'trajviz-obs',
  async setup(ctx) {
    const backend = new Backend(ctx, optionsSchema.parse(ctx.options))
    const jobs = new Map<string, AbortController>()
    const lifetime = new AbortController()
    const tasks = new Set<Promise<unknown>>()
    const track = <T>(task: Promise<T>): Promise<T> => { tasks.add(task); void task.then(() => tasks.delete(task), () => tasks.delete(task)); return task }
    async function run(input: RunInput, requestSignal?: AbortSignal) {
      if (jobs.has(input.runID)) throw new Error('This obs command is already running.')
      const controller = new AbortController()
      jobs.set(input.runID, controller)
      const signal = AbortSignal.any([controller.signal, lifetime.signal, ...(requestSignal ? [requestSignal] : [])])
      try {
        const result = await backend.withSources(input.useSynced, () => runCommand(backend, input, signal))
        // RPC's JSON codec rejects present-but-undefined optional properties.
        return JSON.parse(JSON.stringify(result)) as typeof result
      }
      catch (error) { return { text: signal.aborted ? 'Cancelled.' : `obs: ${safeText(error instanceof Error ? error.message : String(error))}` } }
      finally { jobs.delete(input.runID) }
    }
    const rpc = await ctx.rpc.register(Obs, {
      run: (input, call) => track(run(input, call.signal)),
      sync: input => track(backend.sync(input.source)),
      resolve: input => track(backend.resolve(input.sessionID, input.selector)),
      snapshot: (input, call) => track(backend.withSources(input.useSynced, () => backend.snapshot(input.sessionID, AbortSignal.any([call.signal,lifetime.signal]), input.selector))),
      cancel: async input => { const job = jobs.get(input.runID); job?.abort(); const plan = cancelModel(backend, input.runID); return {cancelled: Boolean(job) || plan} },
    })
    backend.progress = (runID, text) => rpc.events.emit('progress', {runID, text})
    // Read-only data for the agent. This tool never calls another model or runs a diagnostic shell command.
    const tool = await ctx.tool.transform(editor => {
      editor.add({
        name: 'obs', description: 'Read TrajViz diagnostics for this OpenCode session. Returns a brief, issues, health, or one paged TrajViz view; makes no model calls.',
        input: z.object({ what: z.enum(['brief','issues','health','view']), view: z.string().optional(), options: z.record(z.string(), z.union([z.string(),z.number(),z.boolean()])).optional(), limit: z.number().int().min(1).max(500).default(25) }),
        async execute(input, call) {
          const signal = AbortSignal.any([call.signal, lifetime.signal])
          return track(backend.withSources(false, async () => {
            try {
              const source = await backend.session(call.sessionID, undefined, signal)
              const args = input.what === 'brief' ? ['brief', ...source.argv] : ['view', ...source.argv, ...(input.what === 'view' ? input.view ? [input.view] : [] : [input.what]), '--json', '--limit', String(input.limit)]
              for (const [key,value] of Object.entries(input.options ?? {})) args.push('--option', `${key}=${value}`)
              const data = await backend.runtime.run<Record<string, unknown>>(args, signal)
              let text = typeof data.brief === 'string' ? data.brief : JSON.stringify(data)
              if (Buffer.byteLength(text) > 65536) text = JSON.stringify({ path: data.path, note: 'Full output exceeds 64 KiB. Request a smaller limit or open the saved view.' })
              return { content: text }
            } catch (error) { return { content: `obs: ${safeText(error instanceof Error ? error.message : String(error))}` } }
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
      } catch (error) { if (!lifetime.signal.aborted) console.error('TrajViz event observation stopped:', error instanceof Error ? error.message : String(error)) }
    })()
    tasks.add(observing)
    return async () => {
      lifetime.abort()
      for (const job of jobs.values()) job.abort()
      cancelModel(backend)
      const closed = backend.close()
      await Promise.allSettled([...tasks, rpc.dispose(), tool.dispose(), closed])
    }
  },
})
