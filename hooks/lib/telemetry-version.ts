const releaseVersion = /^(?:0|[1-9][0-9]{0,3})(?:\.(?:0|[1-9][0-9]{0,3})){2}(?:-(?:alpha|beta|rc)\.[0-9]{1,3})?$/

// The sender may spend up to 3 seconds on each of three batches and one v1 retry.
export const TELEMETRY_WORKER_TIMEOUT_MS = 14_000

/** Keep the same bounded release syntax as the Python telemetry protocol. */
export function telemetryAgentVersion(value: unknown): string | undefined {
  return typeof value === 'string' && releaseVersion.exec(value)?.[0] === value ? value : undefined
}
