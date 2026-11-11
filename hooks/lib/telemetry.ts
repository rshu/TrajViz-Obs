import type { EngineInterface, PluginOptions } from 'claude-code'
import { SUBCOMMANDS, type Parsed } from './args'
import { DELIVERY_STATUS_UNAVAILABLE, deliveryStatusText } from './delivery-status'
import { findPython, pythonOverride, readEnvState } from './python'

export type TelemetryOutcome = 'success' | 'error' | 'cancelled' | 'empty' | 'rejected' | 'confirmation'
export type OutcomeTracker = { outcome: TelemetryOutcome }

let surface: 'terminal' | 'desktop' | 'headless' | 'unknown' = 'unknown'
const pending = new Set<Promise<void>>()
const SHUTDOWN_GRACE_MS = 750
const CONFIG_KEYS = ['TRAJVIZ_OBS_TELEMETRY', 'TRAJVIZ_OBS_TELEMETRY_URL', 'TRAJVIZ_OBS_TELEMETRY_TOKEN', 'TRAJVIZ_OBS_TELEMETRY_STATE_DIR']

export function setTelemetrySurface(value: string | null, interactive: boolean): void {
  surface = !interactive ? 'headless' : value === 'terminal' || value === 'desktop' ? value : 'unknown'
}

/** Only command names and fixed variants may leave the host. */
export function commandFeature({ sub, positional, flags }: Parsed): string | undefined {
  if (sub === 'telemetry' || !SUBCOMMANDS.includes(sub)) return undefined
  if (positional.length === 1) {
    if (sub === 'pane' && positional[0] === 'close') return 'pane.close'
    if (sub === 'dashboard' && positional[0] === 'stop') return 'dashboard.stop'
    if (sub === 'converge' && positional[0] === 'stop') return 'converge.stop'
  }
  return sub === 'dashboard' && flags.html !== undefined ? 'dashboard.html' : sub
}

/** Preserve the native result while recording a structured local outcome. */
export function markOutcome<T>(tracker: OutcomeTracker | undefined, outcome: TelemetryOutcome, value: T): T {
  if (tracker) tracker.outcome = outcome
  return value
}

async function hasLaunchConsent($: EngineInterface): Promise<boolean> {
  // Claude applies settings.env to its process before the hooks run. Ignore
  // telemetry configuration present in the merged settings.
  const env = await $.settings.read({ key: 'env' })
  return env === null || typeof env !== 'object' || !Object.keys(env).some(key => CONFIG_KEYS.includes(key.toUpperCase()))
}

async function launchTelemetry($: EngineInterface): Promise<{ endpoint: string; version: string } | undefined> {
  if (await $.env.get('TRAJVIZ_OBS_TELEMETRY') !== '1') return
  const endpoint = await $.env.get('TRAJVIZ_OBS_TELEMETRY_URL')
  if (!endpoint?.trim() || !(await hasLaunchConsent($))) return
  const manifest = JSON.parse(await $.fs.read(`${$.plugin.root}/.claude-plugin/plugin.json`)) as { version?: unknown }
  const version = manifest.version
  if (typeof version === 'string' && /^\d+\.\d+\.\d+(?:[-+][\w.+-]+)?$/.test(version)) return { endpoint, version }
}

async function telemetryPython($: EngineInterface, options: PluginOptions): Promise<string | undefined> {
  const managed = await readEnvState($, false)
  return managed?.python ?? (await findPython($, pythonOverride(options)))?.path
}

/** Read the sender state without attempting delivery or preparing a runtime. */
export async function telemetryStatus($: EngineInterface, options: PluginOptions): Promise<{ text: string }> {
  try {
    if (!(await hasLaunchConsent($))) return { text: 'Usage telemetry · Off. Use the Claude Code launch environment for telemetry settings.' }
    const enabled = await $.env.get('TRAJVIZ_OBS_TELEMETRY') === '1'
    const endpoint = await $.env.get('TRAJVIZ_OBS_TELEMETRY_URL')
    if (!endpoint) return { text: `Usage telemetry · ${enabled ? 'Endpoint missing' : 'Off'}` }
    const python = await telemetryPython($, options)
    if (!python) return { text: `${DELIVERY_STATUS_UNAVAILABLE} Python could not be located.` }
    const token = await $.env.get('TRAJVIZ_OBS_TELEMETRY_TOKEN')
    const result = await $.process.run([python, '-I', `${$.plugin.root}/bridge/telemetry_client.py`, '--status', '--adapter', 'claude-code'], {
      cwd: $.plugin.root, timeoutMs: 5_000,
      env: { TRAJVIZ_OBS_TELEMETRY: enabled ? '1' : '0', TRAJVIZ_OBS_TELEMETRY_URL: endpoint,
        ...(token === undefined ? {} : { TRAJVIZ_OBS_TELEMETRY_TOKEN: token }) },
    })
    if (result.exitCode !== 0 || result.isStdoutTruncated) return { text: DELIVERY_STATUS_UNAVAILABLE }
    return { text: deliveryStatusText(JSON.parse(result.stdout)) }
  } catch {
    return { text: DELIVERY_STATUS_UNAVAILABLE }
  }
}

/** Live dashboards inherit the launch environment and need only the adapter and version. */
export async function dashboardUsageArgs($: EngineInterface): Promise<string[]> {
  try {
    const launch = await launchTelemetry($)
    if (launch) return ['--usage-adapter', 'claude-code', '--usage-version', launch.version]
  } catch {
    // Browser telemetry must never prevent launching a dashboard.
  }
  return []
}

async function sendTelemetry($: EngineInterface, options: PluginOptions, feature: string, outcome: TelemetryOutcome, elapsedMs: number, hostSurface: typeof surface): Promise<void> {
  try {
    const launch = await launchTelemetry($)
    if (!launch) return
    const root = $.plugin.root
    const python = await telemetryPython($, options)
    if (!python) return
    const token = await $.env.get('TRAJVIZ_OBS_TELEMETRY_TOKEN')
    await $.process.run([
      python, '-I', `${root}/bridge/telemetry_client.py`, '--adapter', 'claude-code', '--surface', hostSurface,
      '--feature', feature, '--outcome', outcome, '--duration-ms', String(Math.max(0, elapsedMs)), '--version', launch.version,
    ], {
      cwd: root, timeoutMs: 8_000,
      env: { TRAJVIZ_OBS_TELEMETRY: '1', TRAJVIZ_OBS_TELEMETRY_URL: launch.endpoint,
        ...(token === undefined ? {} : { TRAJVIZ_OBS_TELEMETRY_TOKEN: token }) },
    })
  } catch {
    // Telemetry should stay silent when the host denies a capability or lacks a runtime.
  }
}

/** Best effort, without installing Python or blocking the observed operation. */
export async function reportTelemetry($: EngineInterface, options: PluginOptions, feature: string, outcome: TelemetryOutcome, elapsedMs: number): Promise<void> {
  if (pending.size >= 8) return
  const send = sendTelemetry($, options, feature, outcome, elapsedMs, surface)
  pending.add(send)
  try { await send } finally { pending.delete(send) }
}

/** Give finished commands a chance to reach the local queue during graceful shutdown. */
export async function finishTelemetry($: EngineInterface): Promise<void> {
  if (!pending.size) return
  let expire = () => {}
  const deadline = new Promise<void>(resolve => { expire = resolve })
  try {
    const timer = $.clock.after(SHUTDOWN_GRACE_MS, expire)
    try {
      await Promise.race([Promise.allSettled([...pending]), deadline])
    } finally {
      timer.cancel()
    }
  } catch { /* A missing clock capability never delays shutdown. */ }
}
