import { lstat, readFile } from 'node:fs/promises'
import path from 'node:path'
import { atomicFile } from './runtime.js'
import { dashboardPortValue } from './shared/args.js'
import { userStateRoot } from './user-state.js'

function preferenceFile(): string | undefined {
  const root = userStateRoot()
  return path.isAbsolute(root) ? path.join(root, 'trajviz-obs', 'opencode-dashboard-port') : undefined
}

async function savedDashboardPort(file: string | undefined): Promise<number | undefined> {
  if (!file) return
  try {
    const metadata = await lstat(file)
    if (!metadata.isFile() || metadata.size > 8) return
    const value = (await readFile(file, 'utf8')).trimEnd()
    return value === '0' ? 0 : dashboardPortValue(value)
  } catch { /* An unavailable preference leaves the plugin configuration in effect. */ }
}

export async function defaultDashboardPort(configured = 0): Promise<number> {
  return await savedDashboardPort(preferenceFile()) ?? configured
}

export async function dashboardPortPreference(configured: number | undefined, value: string | undefined, signal: AbortSignal): Promise<string> {
  const file = preferenceFile()
  if (value !== undefined) {
    if (!file) return 'Dashboard port preference could not be saved. Use an absolute OpenCode state directory.'
    signal.throwIfAborted()
    const setting = value === 'auto' ? 0 : value === 'default' ? 'default' : dashboardPortValue(value)
    if (setting === undefined) return 'Choose auto, default, or a dashboard port from 1 to 65535.'
    try { await atomicFile(file, `${setting}\n`) }
    catch { return 'Dashboard port preference could not be saved.' }
  }
  const saved = await savedDashboardPort(file)
  const port = saved ?? configured ?? 0
  const source = saved !== undefined ? 'saved preference' : configured !== undefined ? 'plugin configuration' : 'default'
  const status = `Dashboard port · ${port === 0 ? 'Automatic' : port} (${source}).`
  return value === undefined ? status : `${status}\nApplies to the next dashboard launch.`
}
