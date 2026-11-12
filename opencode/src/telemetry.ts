import { execFile, spawn } from 'node:child_process'
import { lstatSync, readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import path from 'node:path'
import manifest from '../package.json' with { type: 'json' }
import type { Runtime } from './runtime.js'
import type { CommandResult } from './rpc.js'
import { DELIVERY_STATUS_UNAVAILABLE, deliveryStatusText, destinationStatusText } from './shared/delivery-status.js'
import { userStateRoot } from './user-state.js'

export type UsageSurface = 'terminal' | 'desktop' | 'headless' | 'unknown'
export type UsageOutcome = 'success' | 'error' | 'cancelled' | 'empty' | 'rejected'
type Usage = { surface: UsageSurface; feature: string; outcome: UsageOutcome; durationMs: number }
type UsageReporter = ((event: Usage) => void) & { enabled(): boolean; flush(): Promise<void> }
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

function telemetryFile(name: string): string | undefined {
  const directory = process.env.TRAJVIZ_OBS_TELEMETRY_STATE_DIR || path.join(userStateRoot(), 'trajviz-obs', 'telemetry')
  return path.isAbsolute(directory) ? path.join(directory, name) : undefined
}

function savedConsent(): 'on' | 'off' | undefined {
  // Match the helper's adapter-specific marker without starting Python for disabled installs.
  const marker = telemetryFile('opencode-consent')
  if (!marker) return 'off'
  try {
    const metadata = lstatSync(marker, { throwIfNoEntry: false })
    if (!metadata) return undefined
    if (!metadata.isFile() || metadata.size > 3) return 'off'
    const value = readFileSync(marker, 'utf8')
    return value === 'on' || value === 'on\n' ? 'on' : 'off'
  } catch { return 'off' }
}

function hasSavedDestination(): boolean {
  const marker = telemetryFile('opencode-destination.json')
  try { return Boolean(marker && lstatSync(marker, { throwIfNoEntry: false })?.isFile()) }
  catch { return false }
}

function usageEnabled(): boolean {
  const consent = savedConsent()
  if (consent) return consent === 'on'
  return process.env.TRAJVIZ_OBS_TELEMETRY === '1' && (Boolean(process.env.TRAJVIZ_OBS_TELEMETRY_URL) || hasSavedDestination())
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

async function usageControl(runtime: Pick<Runtime, 'bridge' | 'telemetryPython'>, signal: AbortSignal,
  action: string[], unavailable: string, render: (value: unknown) => string): Promise<string> {
  try {
    const python = runtime.telemetryPython()
    if (!python) return `${unavailable} Python could not be located.`
    const args = ['-I', path.join(runtime.bridge, 'telemetry_client.py'), ...action, '--adapter', 'opencode']
    const stdout = await new Promise<string>((resolve, reject) => {
      execFile(python, args, { cwd: homedir(), env: usageEnvironment(), signal, timeout: 5_000, maxBuffer: 8_192, windowsHide: true }, (error, stdout) => {
        if (error) reject(error)
        else resolve(stdout)
      })
    })
    return render(JSON.parse(stdout))
  } catch {
    return unavailable
  }
}

/** Read only local delivery metadata. The helper neither installs Python nor sends events. */
export async function usageDeliveryStatus(runtime: Pick<Runtime, 'bridge' | 'telemetryPython'>, signal: AbortSignal): Promise<string> {
  if (!process.env.TRAJVIZ_OBS_TELEMETRY_URL && !savedConsent() && !hasSavedDestination()) {
    return `Usage telemetry · ${process.env.TRAJVIZ_OBS_TELEMETRY === '1' ? 'Endpoint missing' : 'Off'}`
  }
  return usageControl(runtime, signal, ['--status'], DELIVERY_STATUS_UNAVAILABLE, deliveryStatusText)
}

export async function setUsageConsent(runtime: Pick<Runtime, 'bridge' | 'telemetryPython'>, signal: AbortSignal, mode: 'on' | 'off'): Promise<string> {
  const unavailable = 'Usage telemetry · Could not save the preference.'
  return usageControl(runtime, signal, ['--set-consent', mode], unavailable, value => {
    const status = deliveryStatusText(value)
    if (status === DELIVERY_STATUS_UNAVAILABLE) return unavailable
    const configuration = (value as { configuration: string }).configuration
    if (mode === 'on' && configuration === 'ready') return 'Usage telemetry · On. Preference saved. Reopen any existing dashboards to track browser actions. Run /obs telemetry for delivery status.'
    if (mode === 'off' && configuration === 'disabled') return 'Usage telemetry · Off. Preference saved. Queued uploads are paused.'
    return status
  })
}

export async function usageDestination(runtime: Pick<Runtime, 'bridge' | 'telemetryPython'>, signal: AbortSignal,
  destination: string | undefined, useLaunchToken: boolean): Promise<string> {
  const action = destination === undefined ? ['--destination'] : ['--set-destination', destination]
  if (useLaunchToken) action.push('--use-launch-token')
  const operation = destination === undefined ? 'read' : destination === 'default' ? 'reset' : 'set'
  const unavailable = operation === 'read' ? 'Telemetry destination could not be read.' : 'Telemetry destination could not be saved.'
  return usageControl(runtime, signal, action, unavailable, value => destinationStatusText(value, operation))
}

export function dashboardUsageArgs(): string[] {
  return usageEnabled() ? ['--usage-adapter', 'opencode', '--usage-version', manifest.version] : []
}

/** Detached children persist independently, while the SQLite lease allows one uploader. */
export function createUsageReporter(runtime: Pick<Runtime, 'bridge' | 'telemetryPython'>): UsageReporter {
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
    if (!usageEnabled()) queue.length = 0
    if (!queue.length) { dispatchComplete?.(); return }
    if (children >= MAX_CHILDREN) return
    let python: string | undefined
    try { python = runtime.telemetryPython() } catch { /* No usable interpreter yet. */ }
    if (!python) { dispatchComplete?.(); return }
    const env = usageEnvironment()
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
    if (!usageEnabled()) return
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
  return Object.assign(report, { enabled: usageEnabled, flush })
}
