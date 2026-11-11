import type { LocationRef, OpenCodeClient } from '@opencode/client'
import { Obs, type UsageInteraction } from './rpc.js'

/** Get consent from the authenticated server, then send only allowlisted action fields. */
export function createInteractionReporter(surface: UsageInteraction['surface']) {
  const consent = new WeakMap<OpenCodeClient, Promise<boolean>>()
  async function report(client: OpenCodeClient, location: LocationRef | undefined,
    feature: UsageInteraction['feature'], outcome: UsageInteraction['outcome'], started: number): Promise<void> {
    const durationMs = Math.max(0, Math.min(3_600_000, Math.round(performance.now() - started)))
    try {
      const rpc = client.rpc(Obs)
      let enabled = consent.get(client)
      if (!enabled) {
        enabled = rpc.usageStatus({}, { location, signal: AbortSignal.timeout(3000) }).then(result => result.enabled)
        consent.set(client, enabled)
        void enabled.catch(() => { if (consent.get(client) === enabled) consent.delete(client) })
      }
      if (await enabled && consent.get(client) === enabled) {
        await rpc.usageInteraction({ feature, surface, outcome, durationMs }, { location, signal: AbortSignal.timeout(3000) })
      }
    } catch { /* Analytics must never affect a pane action. */ }
  }
  return Object.assign(report, { reset(client: OpenCodeClient) { consent.delete(client) } })
}
