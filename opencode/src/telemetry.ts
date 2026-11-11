import { execFile, spawn } from 'node:child_process'
import { homedir } from 'node:os'
import path from 'node:path'
import manifest from '../package.json' with { type: 'json' }
import type { Runtime } from './runtime.js'
import type { CommandResult } from './rpc.js'
import { DELIVERY_STATUS_UNAVAILABLE, deliveryStatusText } from './shared/delivery-status.js'

export type UsageSurface = 'terminal' | 'desktop' | 'headless' | 'unknown'
export type UsageOutcome = 'success' | 'error' | 'cancelled' | 'empty' | 'rejected'
type Usage = { surface: UsageSurface; feature: string; outcome: UsageOutcome; durationMs: number }
type UsageReporter = ((event: Usage) => void) & { flush(): Promise<void> }
const outcomes = new WeakMap<CommandResult, UsageOutcome>()
const LIMIT = 32
const MAX_CHILDREN = 4
const SHUTDOWN_DRAIN_MS = 1500

/** Command results keep their public shape. */
export function usageOutcome<T extends CommandResult>(result: T, outcome: UsageOutcome): T {
  outcomes.set(result, outcome)
  return result
}

export function commandUsageOutcome(result: CommandResult): UsageOutcome {
  return outcomes.get(result) ?? (result.snapshot?.empty ? 'empty' : 'success')
}

function usageEndpoint(): string | undefined {
  return process.env.TRAJVIZ_OBS_TELEMETRY === '1' ? process.env.TRAJVIZ_OBS_TELEMETRY_URL || undefined : undefined
}

function usageEnvironment(): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {}
  for (const name of ['HOME', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH', 'LOCALAPPDATA', 'XDG_STATE_HOME', 'SystemRoot',
    'SSL_CERT_FILE', 'SSL_CERT_DIR',
    'TRAJVIZ_OBS_TELEMETRY', 'TRAJVIZ_OBS_TELEMETRY_URL', 'TRAJVIZ_OBS_TELEMETRY_TOKEN', 'TRAJVIZ_OBS_TELEMETRY_STATE_DIR']) {
    if (process.env[name] !== undefined) env[name] = process.env[name]
  }
  return env
}

/** Read only local delivery metadata. The helper neither installs Python nor sends events. */
export async function usageDeliveryStatus(runtime: Pick<Runtime, 'bridge' | 'telemetryPython'>, signal: AbortSignal): Promise<string> {
  const env = usageEnvironment()
  if (!env.TRAJVIZ_OBS_TELEMETRY_URL) return `Usage telemetry · ${env.TRAJVIZ_OBS_TELEMETRY === '1' ? 'Endpoint missing' : 'Off'}`
  try {
    const python = runtime.telemetryPython()
    if (!python) return `${DELIVERY_STATUS_UNAVAILABLE} Python could not be located.`
    const args = ['-I', path.join(runtime.bridge, 'telemetry_client.py'), '--status', '--adapter', 'opencode']
    const stdout = await new Promise<string>((resolve, reject) => {
      execFile(python, args, { cwd: homedir(), env, signal, timeout: 5_000, maxBuffer: 8_192, windowsHide: true }, (error, stdout) => {
        if (error) reject(error)
        else resolve(stdout)
      })
    })
    return deliveryStatusText(JSON.parse(stdout))
  } catch {
    return DELIVERY_STATUS_UNAVAILABLE
  }
}

/** The dashboard sender applies the same launch opt-in as the OpenCode server. */
export function dashboardUsageArgs(): string[] {
  return usageEndpoint() ? ['--usage-adapter', 'opencode', '--usage-version', manifest.version] : []
}

/** Detached children persist independently, while the SQLite lease allows one uploader. */
export function createUsageReporter(runtime: Pick<Runtime, 'bridge' | 'telemetryPython'>): UsageReporter | undefined {
  if (!usageEndpoint()) return undefined
  const env = usageEnvironment()
  const queue: Usage[] = []
  let children = 0
  let scheduled = false
  let dispatchComplete: (() => void) | undefined
  let flushing: Promise<void> | undefined
  function schedule() {
    if (scheduled || !queue.length || children >= MAX_CHILDREN) return
    scheduled = true
    setImmediate(drain).unref()
  }
  function drain() {
    scheduled = false
    if (!queue.length) { dispatchComplete?.(); return }
    if (children >= MAX_CHILDREN) return
    let python: string | undefined
    try { python = runtime.telemetryPython() } catch { /* No usable interpreter yet. */ }
    if (!python) { dispatchComplete?.(); return }
    while (queue.length && children < MAX_CHILDREN) {
      const { surface, feature, outcome, durationMs } = queue.shift()!
      try {
        const argv = ['-I', path.join(runtime.bridge, 'telemetry_client.py'), '--adapter', 'opencode',
          '--surface', surface, '--feature', feature, '--outcome', outcome, '--duration-ms',
          String(Math.max(0, Math.round(durationMs))), '--version', manifest.version]
        const child = spawn(python, argv, { cwd: homedir(), env, detached: process.platform !== 'win32',
          windowsHide: true, stdio: 'ignore' })
        children++
        const timer = setTimeout(() => child.kill(), 10_000)
        timer.unref()
        child.on('error', () => {})
        child.once('close', () => { clearTimeout(timer); children--; schedule() })
        child.unref()
      } catch { /* Telemetry must stay best effort. */ }
    }
    if (!queue.length) dispatchComplete?.()
  }
  function report(event: Usage) {
    if (queue.length === LIMIT) queue.shift()
    queue.push(event)
    schedule()
  }
  // Graceful unload waits for the remaining children to start, never for network completion.
  function flush(): Promise<void> {
    if (!queue.length) return Promise.resolve()
    return flushing ??= new Promise<void>(resolve => {
      const timer = setTimeout(finish, SHUTDOWN_DRAIN_MS)
      function finish() { clearTimeout(timer); dispatchComplete = undefined; resolve() }
      dispatchComplete = finish
      drain()
    })
  }
  return Object.assign(report, { flush })
}
