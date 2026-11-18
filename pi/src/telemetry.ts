import { execFile, spawn } from 'node:child_process'
import { lstatSync, readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import path from 'node:path'
import { parseArgs, SUBCOMMANDS } from '../../hooks/lib/args.js'
import { deliveryStatusText, destinationStatusText } from '../../hooks/lib/delivery-status.js'
import { telemetryAgentVersion, TELEMETRY_WORKER_TIMEOUT_MS } from '../../hooks/lib/telemetry-version.js'
import type { Runtime } from '../../opencode/src/runtime.js'

export const PLUGIN_VERSION = '0.1.1'
export type Outcome = 'success' | 'error' | 'cancelled' | 'empty' | 'rejected' | 'confirmation'
type Event = { feature: string; surface: 'terminal' | 'headless'; outcome: Outcome; durationMs: number }
const ADMIN = new Set(['update', 'telemetry', 'telemetry_destination', 'enable_telemetry', 'disable_telemetry'])
export function usageFeature(args: string): string | undefined {
  const { sub, positional, flags } = parseArgs(args)
  if (ADMIN.has(sub) || (sub === 'dashboard' && positional[0] === 'port') || (!SUBCOMMANDS.includes(sub) && sub !== 'brief')) return
  if (sub === 'dashboard' && flags.html !== undefined) return 'dashboard.html'
  if ((sub === 'dashboard' || sub === 'converge') && positional[0] === 'stop') return `${sub}.stop`
  if (sub === 'pane' && positional[0] === 'close') return 'pane.close'
  return sub
}
function marker(name: string): string | undefined {
  const state = process.env.TRAJVIZ_OBS_TELEMETRY_STATE_DIR || path.join(
    (process.platform === 'win32' ? process.env.LOCALAPPDATA : undefined) || process.env.XDG_STATE_HOME || path.join(homedir(), '.local', 'state'), 'trajviz-obs', 'telemetry')
  return path.isAbsolute(state) ? path.join(state, name) : undefined
}
function environment(): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {}
  for (const key of ['HOME', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH', 'LOCALAPPDATA', 'XDG_STATE_HOME', 'SystemRoot', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'TRAJVIZ_OBS_TELEMETRY', 'TRAJVIZ_OBS_TELEMETRY_URL', 'TRAJVIZ_OBS_TELEMETRY_TOKEN', 'TRAJVIZ_OBS_TELEMETRY_STATE_DIR']) {
    if (process.env[key] !== undefined) env[key] = process.env[key]
  }
  return env
}
/** Shares the existing bounded, consent-aware Python sender and durable queue. */
export class Usage {
  private queue: Event[] = []
  private children = 0
  private scheduled = false
  private readonly agentVersion: string | undefined
  constructor(readonly runtime: Pick<Runtime, 'bridge' | 'telemetryPython'>, hostVersion: string) { this.agentVersion = telemetryAgentVersion(hostVersion) }
  enabled(): boolean {
    const file = marker('pi-consent')
    if (!file) return false
    try {
      const metadata = lstatSync(file, { throwIfNoEntry: false })
      if (metadata) return metadata.isFile() && metadata.size <= 3 && ['on', 'on\n'].includes(readFileSync(file, 'utf8'))
    } catch { return false }
    const destination = marker('pi-destination.json')
    try {
      return process.env.TRAJVIZ_OBS_TELEMETRY === '1' && (Boolean(process.env.TRAJVIZ_OBS_TELEMETRY_URL) || Boolean(destination && lstatSync(destination, { throwIfNoEntry: false })?.isFile()))
    } catch { return false }
  }
  dashboardArgs(): string[] {
    return this.enabled() ? ['--usage-adapter', 'pi', '--usage-version', PLUGIN_VERSION, ...(this.agentVersion ? ['--usage-agent-version', this.agentVersion] : [])] : []
  }
  report(event: Event): void {
    if (!this.enabled()) return
    if (this.queue.length >= 32) this.queue.shift()
    this.queue.push(event); this.schedule()
  }
  private schedule(): void {
    if (this.scheduled || !this.queue.length || this.children >= 2) return
    this.scheduled = true
    setImmediate(() => { this.scheduled = false; this.drain() }).unref()
  }
  private drain(): void {
    if (!this.enabled()) this.queue.length = 0
    let python: string | undefined
    try { python = this.queue.length ? this.runtime.telemetryPython() : undefined } catch { /* No interpreter is ready yet. */ }
    if (!python) return
    while (this.queue.length && this.children < 2) {
      const event = this.queue.shift()!
      const args = ['-I', path.join(this.runtime.bridge, 'telemetry_client.py'), '--adapter', 'pi',
        '--surface', event.surface, '--feature', event.feature, '--outcome', event.outcome,
        '--duration-ms', String(Math.max(0, Math.round(event.durationMs))), '--version', PLUGIN_VERSION,
        ...(this.agentVersion ? ['--agent-version', this.agentVersion] : [])]
      try {
        const child = spawn(python, args, { cwd: homedir(), env: environment(), detached: process.platform !== 'win32', windowsHide: true, stdio: 'ignore' })
        this.children++
        const timer = setTimeout(() => child.kill(), TELEMETRY_WORKER_TIMEOUT_MS); timer.unref()
        child.once('error', () => {})
        child.once('close', () => { clearTimeout(timer); this.children--; this.schedule() })
        child.unref()
      } catch { /* Optional telemetry cannot fail a coding session. */ }
    }
  }
  async flush(): Promise<void> {
    const deadline = Date.now() + 1500
    do {
      this.drain()
      if (!this.queue.length) return
      await new Promise(resolve => setTimeout(resolve, 20))
    } while (Date.now() < deadline)
  }
  async control(action: 'status' | 'on' | 'off' | 'destination', signal: AbortSignal, value?: string, useLaunchToken = false): Promise<string> {
    const python = this.runtime.telemetryPython()
    if (!python) throw new Error('Telemetry requires Python 3.9+; run /obs setup or configure python.')
    const options = action === 'status' ? ['--status'] : action === 'destination'
      ? [value === undefined ? '--destination' : '--set-destination', ...(value === undefined ? [] : [value]), ...(useLaunchToken ? ['--use-launch-token'] : [])]
      : ['--set-consent', action]
    const args = ['-I', path.join(this.runtime.bridge, 'telemetry_client.py'), '--adapter', 'pi', ...options]
    const text = await new Promise<string>((resolve, reject) => {
      execFile(python, args, { cwd: homedir(), env: environment(), signal, timeout: 5000, maxBuffer: 8192, windowsHide: true }, (error, stdout) => error ? reject(new Error('Telemetry settings could not be read or saved.')) : resolve(stdout))
    })
    const doc = JSON.parse(text)
    if (action === 'destination') return destinationStatusText(doc, value === undefined ? 'read' : value === 'default' ? 'reset' : 'set')
    if (action === 'on' && doc.configuration === 'ready') return 'Usage telemetry · On. Preference saved. Reopen existing dashboards to track browser actions.'
    if (action === 'off' && doc.configuration === 'disabled') { this.queue.length = 0; return 'Usage telemetry · Off. Preference saved. Queued uploads are paused.' }
    return deliveryStatusText(doc)
  }
}
