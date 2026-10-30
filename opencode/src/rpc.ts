import { Rpc } from '@opencode/schema/rpc'
import { z } from 'zod'
import type { EmptySnapshot, Snapshot } from './types.js'
import { validateSource, type NativeSource } from './collect.js'

export type CommandResult = {
  text: string
  snapshot?: Snapshot | EmptySnapshot
  data?: unknown
  url?: string
  path?: string
  sessionID?: string
  tab?: string
  confirmation?: { id: string; title: string; message: string }
}
export type RunInput = { sessionID: string; args: string; runID: string; useSynced?: boolean; confirmationID?: string; model?: { providerID: string; id: string; variant?: string } }

const sessionID = z.string().min(1).max(256)
const snapshot = z.custom<Snapshot | EmptySnapshot>(v => typeof v === 'object' && v !== null)
const result = z.object({
  text: z.string(), snapshot: snapshot.optional(), data: z.unknown().optional(),
  url: z.string().optional(), path: z.string().optional(), sessionID: sessionID.optional(), tab: z.string().optional(),
  confirmation: z.object({ id: z.string(), title: z.string(), message: z.string() }).optional(),
})

export const Obs = Rpc.define({
  id: 'trajviz-obs',
  methods: {
    sync: { input: z.object({ source: z.custom<NativeSource>(v => { try { validateSource(v); return true } catch { return false } }) }), output: z.object({ sessionID }) },
    resolve: { input: z.object({ sessionID, selector: z.string().optional(), captureSessionID: z.string().min(1).max(4096).optional() }), output: z.object({ sessionID, path: z.string().optional() }) },
    run: {
      input: z.object({ sessionID, args: z.string().max(32768), runID: z.string().min(1).max(256), useSynced: z.boolean().optional(), confirmationID: z.string().optional(), model: z.object({ providerID: z.string(), id: z.string(), variant: z.string().optional() }).optional() }),
      output: result,
    },
    snapshot: { input: z.object({ sessionID, selector: z.string().optional(), captureSessionID: z.string().min(1).max(4096).optional(), useSynced: z.boolean().optional() }), output: snapshot },
    cancel: { input: z.object({ runID: z.string() }), output: z.object({ cancelled: z.boolean() }) },
  },
  events: { changed: { schema: z.object({ sessionID }) }, progress: { schema: z.object({ runID: z.string(), text: z.string() }) } },
})
