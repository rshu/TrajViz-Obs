import type { LocationRef, OpenCodeClient } from '@opencode/client'
import { Obs, type UsageInteraction } from './rpc.js'

/** Get consent from the authenticated server, then send only allowlisted action fields. */
export function createInteractionReporter(surface: UsageInteraction['surface']) {
  const connections = new WeakMap<OpenCodeClient, { pending?: Promise<boolean> }>()
  function begin(client: OpenCodeClient, location: LocationRef | undefined, started: number) {
    try {
      const rpc = client.rpc(Obs)
      let connection = connections.get(client)
      if (!connection) {
        connection = {}
        connections.set(client, connection)
      }
      const current = connection
      // Check before awaiting work. The server rechecks consent before accepting an event.
      const enabled = current.pending ??= rpc.usageStatus({}, { location, signal: AbortSignal.timeout(3000) })
        .then(result => result.enabled).catch(() => false).finally(() => { delete current.pending })
      return async (feature: UsageInteraction['feature'], outcome: UsageInteraction['outcome']): Promise<void> => {
        const durationMs = Math.max(0, Math.min(3_600_000, Math.round(performance.now() - started)))
        try {
          if (await enabled && connections.get(client) === current) {
            await rpc.usageInteraction({ feature, surface, outcome, durationMs }, { location, signal: AbortSignal.timeout(3000) })
          }
        } catch { /* Analytics must never affect a pane action. */ }
      }
    } catch { return undefined }
  }
  return Object.assign(begin, { reset(client: OpenCodeClient) { connections.delete(client) } })
}
