const CONFIGURATION: Record<string, string> = {
  disabled: 'Off',
  missing_endpoint: 'Endpoint missing',
  invalid_endpoint: 'Endpoint invalid',
  invalid_token: 'Credential invalid',
  invalid_state_directory: 'State directory invalid',
  ready: 'On',
}

const FAILURE: Record<string, string> = {
  network: 'Network unavailable',
  throttled: 'Collector throttled delivery',
  collector_unavailable: 'Collector unavailable',
  authentication: 'Collector rejected the credential',
  endpoint: 'Collector endpoint rejected delivery',
  payload_rejected: 'Collector rejected events',
}

export const DELIVERY_STATUS_UNAVAILABLE = 'Usage telemetry · Local delivery status unavailable.'

const label = (choices: Record<string, string>, value: unknown): string | undefined =>
  typeof value === 'string' && Object.hasOwn(choices, value) ? choices[value] : undefined
const count = (value: unknown): string | undefined =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? String(value) : undefined
const timestamp = (value: unknown): string | undefined => {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(value)) return undefined
  const time = Date.parse(value)
  if (Number.isFinite(time)) return new Date(time).toISOString().replace('.000Z', 'Z')
}

/** Render only known states, counts and UTC timestamps from the local helper. */
export function deliveryStatusText(value: unknown): string {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return DELIVERY_STATUS_UNAVAILABLE
  const status = value as Record<string, unknown>
  const configuration = label(CONFIGURATION, status.configuration)
  if (!configuration) return DELIVERY_STATUS_UNAVAILABLE
  const lines = [`Usage telemetry · ${configuration}`]
  if (typeof status.credential_configured === 'boolean') {
    lines.push(`Collector credential · ${status.credential_configured ? 'Configured' : 'Not configured'}`)
  }
  const queued = count(status.queued), capacity = count(status.queue_capacity)
  if (queued !== undefined) lines.push(`Queued events · ${queued}${capacity === undefined ? '' : ` / ${capacity}`}`)
  else if (status.queue_state === 'unavailable') lines.push('Local queue · Unavailable')
  if (status.queue_state === 'present' || status.queue_state === 'absent') {
    lines.push(`Last successful upload · ${timestamp(status.last_success_at) ?? 'None recorded'}`)
  }
  const failure = label(FAILURE, status.last_failure)
  if (failure) {
    const code = status.last_http_status
    const http = typeof code === 'number' && Number.isInteger(code) && code >= 100 && code <= 599 ? ` (HTTP ${code})` : ''
    const when = timestamp(status.last_failure_at)
    lines.push(`Last delivery failure · ${failure}${http}${when ? ` · ${when}` : ''}`)
  }
  const retry = timestamp(status.retry_at)
  if (retry) lines.push(`Retry eligible from · ${retry}`)
  const counts = [
    ['Dropped from full queue', count(status.dropped)],
    ['Expired', count(status.expired)],
    ['Rejected', count(status.rejected)],
  ]
  for (const [name, total] of counts) if (total !== undefined) lines.push(`${name} · ${total}`)
  return lines.join('\n')
}
