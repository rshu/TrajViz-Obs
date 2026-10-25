// Where the mod keeps things, from the environment alone. Every `$.env.get`
// name is a string literal (validate reads them off this source), and every
// helper taking `$` is a function declaration (validate follows `$` into one).
import type { EngineInterface } from 'claude-code'

const PLUGIN_DIR = 'trajviz-obs'

/** A path without its trailing separators; undefined when unset or empty. */
const dir = (v: string | undefined): string | undefined => {
  const t = v?.replace(/[\\/]+$/, '')
  return t ? t : undefined
}

/** Claude Code's project slug: the working directory with every character outside `[A-Za-z0-9]` as `-`. */
export function projectSlug(cwd: string): string {
  return cwd.replace(/[^A-Za-z0-9]/g, '-')
}

/** Whether the host is Windows (`OS=Windows_NT`). */
export async function isWindows($: EngineInterface): Promise<boolean> {
  return /^windows/i.test((await $.env.get('OS')) ?? '')
}

/** Whether the session runs over SSH (`SSH_CONNECTION` or `SSH_TTY` set): then nothing opens a browser. */
export async function isSsh($: EngineInterface): Promise<boolean> {
  return Boolean((await $.env.get('SSH_CONNECTION')) || (await $.env.get('SSH_TTY')))
}

/**
 * The mod's own cache folder, `<cache>/trajviz-obs`: `<cache>` is
 * `$XDG_CACHE_HOME`, else `$HOME/.cache`, else `%LOCALAPPDATA%` when
 * `OS=Windows_NT` (the plan's global constraint; a Windows shell with `HOME`
 * set, such as Git Bash, therefore uses `~/.cache` too). Holds `venv/`,
 * `state.json` and `sessions/`.
 */
export async function cacheRoot($: EngineInterface): Promise<string> {
  const xdg = dir(await $.env.get('XDG_CACHE_HOME'))
  if (xdg) return `${xdg}/${PLUGIN_DIR}`
  const home = dir(await $.env.get('HOME'))
  if (home) return `${home}/.cache/${PLUGIN_DIR}`
  if (await isWindows($)) {
    const local = dir(await $.env.get('LOCALAPPDATA'))
    if (local) return `${local}/${PLUGIN_DIR}`
  }
  return `./.cache/${PLUGIN_DIR}`
}

/** Claude Code's config folder: `$CLAUDE_CONFIG_DIR`, else `~/.claude`. */
export async function configDir($: EngineInterface): Promise<string> {
  const cfg = dir(await $.env.get('CLAUDE_CONFIG_DIR'))
  if (cfg) return cfg
  const home = dir(await $.env.get('HOME'))
  return `${home ?? '.'}/.claude`
}

/** Where a session's artifacts go: `<cacheRoot>/sessions/<session-id>`. */
export async function artifactsDir($: EngineInterface, sessionId: string): Promise<string> {
  return `${await cacheRoot($)}/sessions/${sessionId}`
}

/** The TrajViz virtual environment, `<cacheRoot>/venv`. */
export async function venvDir($: EngineInterface): Promise<string> {
  return `${await cacheRoot($)}/venv`
}

/** The venv's interpreter: `bin/python`, or `Scripts/python.exe` on Windows. */
export async function venvPython($: EngineInterface): Promise<string> {
  const venv = await venvDir($)
  return (await isWindows($)) ? `${venv}/Scripts/python.exe` : `${venv}/bin/python`
}
