import { lstat, readFile } from 'node:fs/promises'
import path from 'node:path'
import { homedir } from 'node:os'
import { getAgentDir } from '@earendil-works/pi-coding-agent'
import { atomicFile, type Options } from '../../opencode/src/runtime.js'
import { dashboardPortValue } from '../../hooks/lib/args.js'

const strings = ['python', 'trajvizSource', 'cacheRoot', 'analysisModel', 'labelModel']
const booleans = ['openBrowser', 'watch', 'statusline']
export async function loadOptions(cwd: string): Promise<Options> {
  const merged: Record<string, unknown> = {}
  for (const file of [path.join(getAgentDir(), 'trajviz-obs.json'), path.join(cwd, '.pi', 'trajviz-obs.json')]) {
    let raw: string
    try { raw = await readFile(file, 'utf8') } catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') continue; throw error }
    const doc = JSON.parse(raw)
    if (!doc || Array.isArray(doc) || typeof doc !== 'object') throw new Error(`Expected a JSON object in ${file}.`)
    for (const [key, value] of Object.entries(doc)) {
      if (strings.includes(key) && typeof value === 'string' && value.trim()) merged[key] = value
      else if (booleans.includes(key) && typeof value === 'boolean') merged[key] = value
      else if (key === 'llm' && (value === 'session' || value === 'env')) merged[key] = value
      else if (key === 'dashboardPort' && (value === 0 || dashboardPortValue(value) === value)) merged[key] = value
      else if (key === 'labelMaxSteps' && typeof value === 'number' && Number.isSafeInteger(value) && value >= 1 && value <= 2000) merged[key] = value
      else throw new Error(`Invalid TrajViz option '${key}' in ${file}.`)
    }
  }
  return { watch: true, statusline: true, openBrowser: false, ...merged }
}
function portFile(): string {
  const root = process.platform === 'win32' ? process.env.LOCALAPPDATA : process.env.XDG_STATE_HOME
  const state = root || path.join(homedir(), '.local', 'state')
  if (!path.isAbsolute(state)) throw new Error('Use an absolute state directory for dashboard preferences.')
  return path.join(state, 'trajviz-obs', 'pi-dashboard-port')
}
export async function dashboardPort(configured = 0, preference?: string): Promise<{ port: number; text: string }> {
  const file = portFile()
  if (preference !== undefined) await atomicFile(file, `${preference === 'auto' ? '0' : preference}\n`)
  let saved: number | undefined
  try {
    const metadata = await lstat(file)
    if (metadata.isFile() && metadata.size <= 8) {
      const value = (await readFile(file, 'utf8')).trim()
      saved = value === '0' ? 0 : dashboardPortValue(value)
    }
  } catch { /* A missing preference uses the configured default. */ }
  const port = saved ?? configured
  return { port, text: `Dashboard port · ${port || 'Automatic'} (${saved === undefined ? 'configuration' : 'saved preference'}).${preference === undefined ? '' : '\nApplies to the next dashboard launch.'}` }
}
