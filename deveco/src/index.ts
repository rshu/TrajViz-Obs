import { z } from 'zod'
import { Backend } from './backend.js'
import { runCommand } from './commands.js'
import { PromptCapture } from '../../shared/prompt-capture.js'
import { registerPromptCapture } from './prompt-capture.js'
import { record } from './collect.js'
import type { HostHooks, HostPlugin } from './host.js'

const optionsSchema = z.object({
  python: z.string().optional(), trajvizSource: z.string().min(1).optional(),
  cacheRoot: z.string().min(1).optional(), dashboardPort: z.number().int().min(1).max(65535).optional(),
}).strict()

/** Legacy function-based DevEco Code plugin. Loading it performs no model calls. */
const plugin: HostPlugin = async (ctx, rawOptions = {}) => {
  const options = optionsSchema.parse(rawOptions)
  const backend = new Backend(ctx.client, ctx.directory, options)
  const capture = new PromptCapture({ adapter: 'deveco', bridge: backend.runtime.bridge,
    python: () => backend.runtime.telemetryPython() })
  const prompt = registerPromptCapture(capture)
  let disposed = false
  const jobs = new Set<Promise<unknown>>()
  const hooks: HostHooks = {
    ...prompt.hooks,
    tool: {
      obs: {
        description: 'Inspect the current DevEco Code session using TrajViz. Read-only diagnostics and a local browser dashboard; no extra model calls. Use command "help" for options.',
        args: { command: z.string().max(8192).optional().describe('overview, dashboard, issues, health, agents, context, timeline, view, brief, json, sessions, doctor, or help') },
        async execute(args, context) {
          if (disposed) return 'TrajViz Obs is unloading. Restart DevEco Code.'
          const task = runCommand(backend, context.sessionID, typeof args.command === 'string' ? args.command : '', context.abort)
          jobs.add(task)
          try {
            const text = await task
            return Buffer.byteLength(text) <= 65536 ? text : text.slice(0, 16000) + '\nOutput preview truncated. Open the local dashboard or use a smaller view limit.'
          } catch (error) {
            return `obs: ${error instanceof Error ? error.message : String(error)}`
          } finally { jobs.delete(task) }
        },
      },
    },
    async event(input) {
      if (disposed) return
      const event = record(input.event)
      const type = typeof event.type === 'string' ? event.type : ''
      if (/^(?:message\.|session\.|tool\.)/.test(type)) backend.changed()
    },
    async dispose() {
      if (disposed) return
      disposed = true
      await Promise.allSettled([prompt.close(), backend.close()])
      await Promise.allSettled(jobs)
    },
  }
  return hooks
}

export default plugin
