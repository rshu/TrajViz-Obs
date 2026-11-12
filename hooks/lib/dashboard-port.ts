import type { EngineInterface, PluginOptions } from 'claude-code'
import { dashboardPortValue, DASHBOARD_PORT_USAGE } from './args'

type Preference = { port: number; saved: boolean }

async function readPreference($: EngineInterface, options: PluginOptions): Promise<Preference> {
  try {
    const value = await $.store.get('dashboardPort')
    const port = value === 0 ? 0 : dashboardPortValue(value)
    if (port !== undefined) return { port, saved: true }
  } catch { /* A missing preference never prevents a dashboard from starting. */ }
  return { port: dashboardPortValue(options.dashboardPort) ?? 0, saved: false }
}

export async function preferredDashboardPort($: EngineInterface, options: PluginOptions): Promise<number | undefined> {
  return (await readPreference($, options)).port || undefined
}

/** Read or update the host preference without touching a running dashboard. */
export async function dashboardPortSetting($: EngineInterface, options: PluginOptions, requested: string | undefined, personRequested: boolean): Promise<string> {
  if (requested !== undefined) {
    const port = requested === 'auto' ? 0 : dashboardPortValue(requested)
    if (requested !== 'default' && port === undefined) return DASHBOARD_PORT_USAGE
    if (!personRequested) return 'Run /obs dashboard port directly in the Claude Code composer to change the dashboard port.'
    try {
      if (port === undefined) await $.store.delete('dashboardPort')
      else await $.store.set('dashboardPort', port)
    } catch { return 'Dashboard port preference could not be saved.' }
  }
  const { port, saved } = await readPreference($, options)
  const result = `Dashboard port · ${port || 'Automatic'} (${saved ? 'saved preference' : 'configuration default'})`
  return requested === undefined ? result : `${result}\nApplies to the next dashboard launch. An open dashboard keeps its current port.`
}
