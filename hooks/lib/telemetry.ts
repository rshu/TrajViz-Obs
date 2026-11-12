import type { EngineInterface, PluginOptions } from 'claude-code'
import { SUBCOMMANDS, type Parsed } from './args'
import { DELIVERY_STATUS_UNAVAILABLE, deliveryStatusText } from './delivery-status'
import { isWindows } from './paths'
import { findPython, pythonOverride, readEnvState } from './python'

export type TelemetryOutcome = 'success' | 'error' | 'cancelled' | 'empty' | 'rejected' | 'confirmation'
export type OutcomeTracker = { outcome: TelemetryOutcome }

let surface: 'terminal' | 'desktop' | 'headless' | 'unknown' = 'unknown'
const pending = new Set<Promise<void>>()
const SHUTDOWN_GRACE_MS = 750
const CONFIG_KEYS = [
  'TRAJVIZ_OBS_TELEMETRY', 'TRAJVIZ_OBS_TELEMETRY_URL', 'TRAJVIZ_OBS_TELEMETRY_TOKEN', 'TRAJVIZ_OBS_TELEMETRY_STATE_DIR',
  'XDG_STATE_HOME', 'HOME', 'LOCALAPPDATA', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH', 'OS',
]
type Consent = 'on' | 'off'
type LaunchSettings = { enabled: boolean; endpoint: string }
const CONSENT_FILE = 'claude-code-consent'

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

async function hasTrustedTelemetrySettings($: EngineInterface): Promise<boolean> {
  // Claude applies settings.env to its process before the hooks run. Ignore
  // telemetry configuration present in the merged settings.
  const env = await $.settings.read({ key: 'env' })
  return env === null || typeof env !== 'object' || !Object.keys(env).some(key => CONFIG_KEYS.includes(key.toUpperCase()))
}

/** Match the sender's state path without starting Python just to check consent. */
async function consentDirectory($: EngineInterface): Promise<string | undefined> {
  const windows = await isWindows($)
  const absolute = (value: string) => windows ? /^(?:[A-Za-z]:[\\/]|[\\/]{2}[^\\/])/.test(value) : value.startsWith('/')
  const override = await $.env.get('TRAJVIZ_OBS_TELEMETRY_STATE_DIR')
  if (override) return absolute(override) ? override : undefined
  let root = windows ? await $.env.get('LOCALAPPDATA') : undefined
  if (!root) root = await $.env.get('XDG_STATE_HOME')
  if (!root) {
    const home = windows
      ? (await $.env.get('USERPROFILE')) || `${(await $.env.get('HOMEDRIVE')) ?? ''}${(await $.env.get('HOMEPATH')) ?? ''}`
      : await $.env.get('HOME')
    if (home) root = `${home}/.local/state`
  }
  return root && absolute(root) ? `${root}/trajviz-obs/telemetry` : undefined
}

async function readConsent($: EngineInterface): Promise<Consent | undefined> {
  const directory = await consentDirectory($)
  if (!directory) return
  const marker = `${directory}/${CONSENT_FILE}`
  if (!(await $.fs.exists(marker))) return
  const stat = await $.fs.stat(marker)
  if (stat.isLink || stat.kind !== 'file' || stat.size > 3) return 'off'
  const value = await $.fs.read(marker)
  return value === 'on' || value === 'on\n' ? 'on' : 'off'
}

async function launchSettings($: EngineInterface): Promise<LaunchSettings | undefined> {
  const preference = await readConsent($)
  if (preference === 'off') return
  const enabled = await $.env.get('TRAJVIZ_OBS_TELEMETRY') === '1'
  if (preference !== 'on' && !enabled) return
  const endpoint = (await $.env.get('TRAJVIZ_OBS_TELEMETRY_URL')) ?? ''
  if ((preference !== 'on' && !endpoint.trim()) || !(await hasTrustedTelemetrySettings($))) return
  return { enabled, endpoint }
}

/** Begin a non-blocking consent check before starting an observed action. */
export async function telemetryConsentAtStart($: EngineInterface): Promise<boolean> {
  try { return (await launchSettings($)) !== undefined }
  catch { return false }
}

async function launchTelemetry($: EngineInterface): Promise<(LaunchSettings & { version: string }) | undefined> {
  const settings = await launchSettings($)
  if (!settings) return
  const manifest = JSON.parse(await $.fs.read(`${$.plugin.root}/.claude-plugin/plugin.json`)) as { version?: unknown }
  const version = manifest.version
  if (typeof version === 'string' && /^\d+\.\d+\.\d+(?:[-+][\w.+-]+)?$/.test(version)) return { ...settings, version }
}

async function telemetryPython($: EngineInterface, options: PluginOptions): Promise<string | undefined> {
  const managed = await readEnvState($, false)
  return managed?.python ?? (await findPython($, pythonOverride(options)))?.path
}

async function telemetryEnv($: EngineInterface, launch: LaunchSettings): Promise<Record<string, string>> {
  const token = await $.env.get('TRAJVIZ_OBS_TELEMETRY_TOKEN')
  return {
    TRAJVIZ_OBS_TELEMETRY: launch.enabled ? '1' : '0',
    TRAJVIZ_OBS_TELEMETRY_URL: launch.endpoint,
    ...(token === undefined ? {} : { TRAJVIZ_OBS_TELEMETRY_TOKEN: token }),
  }
}

async function runConsentHelper($: EngineInterface, options: PluginOptions, launch: LaunchSettings, preference?: Consent): Promise<{ text: string }> {
  const python = await telemetryPython($, options)
  if (!python) return { text: `${DELIVERY_STATUS_UNAVAILABLE} Python could not be located. Run /obs setup and try again.` }
  const mode = preference === undefined ? ['--status'] : ['--set-consent', preference]
  const result = await $.process.run([python, '-I', `${$.plugin.root}/bridge/telemetry_client.py`, ...mode, '--adapter', 'claude-code'], {
    cwd: $.plugin.root, timeoutMs: 5_000, env: await telemetryEnv($, launch),
  })
  if (result.exitCode !== 0 || result.isStdoutTruncated) {
    return { text: preference ? 'Usage telemetry preference could not be saved.' : DELIVERY_STATUS_UNAVAILABLE }
  }
  const status = JSON.parse(result.stdout) as { configuration?: unknown }
  const text = deliveryStatusText(status)
  if (preference === 'on' && status?.configuration === 'ready') {
    return { text: 'Usage telemetry · On. Preference saved. Reopen any existing dashboards to track browser actions. Run /obs telemetry for delivery status.' }
  }
  if (preference === 'off' && status?.configuration === 'disabled') {
    return { text: 'Usage telemetry · Off. Preference saved. Queued uploads are paused.' }
  }
  return { text }
}

/** Read the sender state without attempting delivery or preparing a runtime. */
export async function telemetryStatus($: EngineInterface, options: PluginOptions): Promise<{ text: string }> {
  try {
    if (!(await hasTrustedTelemetrySettings($))) return { text: 'Usage telemetry · Off. Use the Claude Code launch environment for telemetry settings.' }
    const preference = await readConsent($)
    const enabled = await $.env.get('TRAJVIZ_OBS_TELEMETRY') === '1'
    const endpoint = (await $.env.get('TRAJVIZ_OBS_TELEMETRY_URL')) ?? ''
    if (!endpoint && !preference) return { text: `Usage telemetry · ${enabled ? 'Endpoint missing' : 'Off'}` }
    return await runConsentHelper($, options, { enabled, endpoint })
  } catch {
    return { text: DELIVERY_STATUS_UNAVAILABLE }
  }
}

/** Set the local preference without recording or uploading an event. */
export async function setTelemetryConsent($: EngineInterface, options: PluginOptions, preference: Consent): Promise<{ text: string }> {
  try {
    if (!(await hasTrustedTelemetrySettings($))) {
      return { text: 'Usage telemetry preference could not be changed. Remove telemetry or state-directory variables from Claude Code settings and use the launch environment.' }
    }
    const enabled = await $.env.get('TRAJVIZ_OBS_TELEMETRY') === '1'
    const endpoint = (await $.env.get('TRAJVIZ_OBS_TELEMETRY_URL')) ?? ''
    return await runConsentHelper($, options, { enabled, endpoint }, preference)
  } catch {
    return { text: 'Usage telemetry preference could not be saved.' }
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

async function sendTelemetry($: EngineInterface, options: PluginOptions, feature: string, outcome: TelemetryOutcome, elapsedMs: number, hostSurface: typeof surface, consentAtStart?: Promise<boolean>): Promise<void> {
  try {
    if (consentAtStart && !(await consentAtStart)) return
    const launch = await launchTelemetry($)
    if (!launch) return
    const root = $.plugin.root
    const python = await telemetryPython($, options)
    if (!python) return
    await $.process.run([
      python, '-I', `${root}/bridge/telemetry_client.py`, '--adapter', 'claude-code', '--surface', hostSurface,
      '--feature', feature, '--outcome', outcome, '--duration-ms', String(Math.max(0, elapsedMs)), '--version', launch.version,
    ], {
      cwd: root, timeoutMs: 8_000,
      env: await telemetryEnv($, launch),
    })
  } catch {
    // Telemetry should stay silent when the host denies a capability or lacks a runtime.
  }
}

/** Best effort, without installing Python or blocking the observed operation. */
export async function reportTelemetry($: EngineInterface, options: PluginOptions, feature: string, outcome: TelemetryOutcome, elapsedMs: number, consentAtStart?: Promise<boolean>): Promise<void> {
  if (pending.size >= 8) return
  const send = sendTelemetry($, options, feature, outcome, elapsedMs, surface, consentAtStart)
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
