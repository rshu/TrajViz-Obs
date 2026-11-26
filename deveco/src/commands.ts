import { parseArgs } from '../../hooks/lib/args.js'
import { Backend } from './backend.js'

export const HELP = `TrajViz Obs for DevEco Code
Invoke the obs tool with {"command":"dashboard"}, or ask DevEco Code to invoke it.
This host has no verified native local /obs command or OpenCode 2 pane API.

Commands
  overview                 Current session diagnostics (default)
  brief                    Concise diagnostic brief
  issues | health | agents | context | timeline
  view [ID] [--limit 1–500] [--offset N] [--json]
  view prompt_construction [--request-id ID]
  dashboard [--port N]      Local browser dashboard and opt-in Captured components
  dashboard stop           Stop this session's dashboard
  sessions [1–500]          Native sessions in this project
  json                     Export trajectory and views into private session artifacts
  doctor                   Analysis-runtime health
  help

No command calls an analysis model or enables prompt capture. The host may use its
coding model to route a natural-language request to this tool. Prompt capture needs
trusted launch-time opt-in and records components, not verified provider payloads.
An open dashboard receives coalesced native copies; use its Load action to reload
trajectory views, and Refresh captures in Captured components for prompt observations.`

const views: Record<string, string> = { issues: 'issues', health: 'health', agents: 'agents', context: 'context_pressure', timeline: 'workflow' }
const pretty = (value: unknown) => JSON.stringify(value, null, 2)
const rendered = (value: Record<string, unknown>) => {
  if (value.empty) return String(value.reason ?? 'Complete an assistant response first.')
  if (typeof value.text === 'string') return value.text
  if (typeof value.brief === 'string') return value.brief
  return pretty(value)
}

export async function runCommand(backend: Backend, sessionID: string, input = '', signal?: AbortSignal): Promise<string> {
  if (input.length > 8192 || input.includes('\0')) throw new Error('The obs command is too long or contains NUL.')
  const { sub, positional, flags } = parseArgs(input)
  const allowed = sub === 'view' ? ['limit', 'offset', 'json', 'request-id'] : sub === 'dashboard' ? ['port'] : []
  if (Object.keys(flags).some(key => !allowed.includes(key))) throw new Error(`Unsupported option. ${HELP}`)
  if (sub === 'help') return HELP
  if (sub === 'doctor') {
    if (positional.length) throw new Error('Usage: doctor')
    return rendered(await backend.runtime.run(['doctor'], signal))
  }
  if (sub === 'sessions') {
    const count = positional.length ? Number(positional[0]) : 20
    if (positional.length > 1 || !Number.isSafeInteger(count) || count < 1 || count > 500) throw new Error('Usage: sessions [1–500]')
    return pretty(await backend.sessions(count, signal))
  }
  if (!sessionID) throw new Error('Open a DevEco Code session before using session diagnostics.')
  if (sub === 'dashboard') {
    if (positional[0] === 'stop' && positional.length === 1 && !Object.keys(flags).length) return await backend.stop(sessionID) ? 'Dashboard stopped.' : 'No dashboard is running for this session.'
    if (positional.length) throw new Error('Usage: dashboard [--port N] | dashboard stop')
    const port = flags.port === undefined ? backend.options.dashboardPort : Number(flags.port)
    if (flags.port === true || port !== undefined && (!Number.isSafeInteger(port) || port < 1 || port > 65535)) throw new Error('Dashboard port must be an integer from 1 to 65535.')
    const result = await backend.dashboard(sessionID, port, signal)
    return `TrajViz dashboard: ${result.url}\nUse Load to reload the current trajectory; the Captured components tab has a separate Refresh captures action.`
  }
  if (sub === 'overview' || sub === 'brief' || sub === 'json') {
    if (positional.length) throw new Error(`Usage: ${sub}`)
    return rendered(await backend.inspect(sessionID, sub === 'overview' ? 'snapshot' : sub, [], signal))
  }
  if (sub === 'view' || views[sub]) {
    if (sub !== 'view' && positional.length || sub === 'view' && positional.length > 1) throw new Error('Usage: view [ID] [--limit N] [--offset N] [--json]; view prompt_construction [--request-id ID]')
    const limit = flags.limit === undefined ? 25 : Number(flags.limit)
    const offset = flags.offset === undefined ? 0 : Number(flags.offset)
    if (flags.limit === true || !Number.isSafeInteger(limit) || limit < 1 || limit > 500 || flags.offset === true || !Number.isSafeInteger(offset) || offset < 0) throw new Error('View limit must be 1–500 and offset a nonnegative integer.')
    const id = sub === 'view' ? positional[0] : views[sub]
    if (id !== undefined && !/^[a-zA-Z0-9_-]+$/.test(id)) throw new Error('Invalid view ID.')
    const requestID = flags['request-id']
    if (requestID !== undefined && (id !== 'prompt_construction' || sub !== 'view')) throw new Error('--request-id is only supported by view prompt_construction.')
    if (requestID !== undefined && (typeof requestID !== 'string' || !requestID.trim() || requestID.length > 4096 || requestID.includes('\0'))) throw new Error('--request-id needs an exact request ID up to 4096 characters, without NUL.')
    const options = typeof requestID === 'string' ? ['--option', `request_id=${requestID}`] : []
    return rendered(await backend.inspect(sessionID, 'view', [...(id ? [id] : []), ...options, '--limit', String(limit), '--offset', String(offset), '--json'], signal))
  }
  throw new Error(`Unsupported DevEco obs command '${sub}'. Use help for the supported read-only commands.`)
}
