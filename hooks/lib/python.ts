// The managed Python environment: immutable virtual environments built by uv or
// by the newest Python 3.11+ on PATH, `agent-trajviz[llm,charts,zstd]` installed from
// `options.trajvizSource`, and `state.json` beside the venv recording what was
// installed so a setup is decided without running pip. Every helper taking
// `$` is a function declaration (validate follows `$` into one); the
// environment is read only through paths.ts, whose env names are literals.
import type { EngineInterface, PluginOptions } from 'claude-code'
import { cacheRoot, isWindows } from './paths'
import { readSession, updateSession } from './state'

/** The manifest's default `trajvizSource`. */
export const DEFAULT_SOURCE = 'agent-trajviz==0.3.1'

/** The interpreters tried on PATH, in order, when no override is set. */
export const PYTHON_CANDIDATES: readonly string[] = ['python3.13', 'python3.12', 'python3.11', 'python3', 'python']

const MIN_MAJOR = 3
const MIN_MINOR = 11

/** `--version` probes, and the version read-back after an install. */
const PROBE_TIMEOUT_MS = 10_000
const BOOTSTRAP_TIMEOUT_MS = 600_000
/** Includes host Python discovery/download, lock waiting, installation and verification. */
export const SETUP_TIMEOUT_MS = BOOTSTRAP_TIMEOUT_MS + PROBE_TIMEOUT_MS * (PYTHON_CANDIDATES.length + 2)
/** Rebuild older environments that omitted the chart imports required by TrajViz's LLM renderer. */
export const ENV_REVISION = 5
const BASE_EXTRAS: readonly string[] = ['llm', 'charts', 'zstd']

export type EnvState = {
  revision?: number
  pythonOption?: string
  sourceCwd?: string
  /** The venv's interpreter. */
  python: string
  /** The installed agent-trajviz version, null when it could not be read. */
  trajviz: string | null
  /** The pip extras installed so far (`llm`, `charts`, and `zstd` on a fresh build). */
  extras: string[]
  /** The `trajvizSource` the environment was built from. */
  source: string
  createdAt: number
}

export type Extra = 'charts' | 'app'

/** Neither `uv` nor a Python 3.11+ was found; `message` is the row's text. */
export class NoPythonError extends Error {
  /** What was looked for, each with what came back. */
  readonly looked: readonly string[]
  constructor(looked: readonly string[]) {
    super(
      `No Python 3.11+ found to set TrajViz up with. Looked for ${looked.join(', ')}. ` +
        'Install uv (https://docs.astral.sh/uv/) or Python 3.11+, or set python in /config to an interpreter, then run /obs again.',
    )
    this.name = 'NoPythonError'
    this.looked = looked
  }
}

/** `options.trajvizSource` as a string, the manifest's default when unset. */
export function trajvizSource(options: PluginOptions): string {
  const s = options['trajvizSource']
  return typeof s === 'string' && s.trim() ? s.trim() : DEFAULT_SOURCE
}

/** `options.python` as a string; "" when unset. */
export function pythonOverride(options: PluginOptions): string {
  const p = options['python']
  return typeof p === 'string' ? p.trim() : ''
}

/** The package every source installs; the name a bare URL source is given. */
const PACKAGE = 'agent-trajviz'

/**
 * A PEP 508 named requirement: the name grammar (`[A-Za-z0-9._-]`, alnum at
 * both ends), optional `[extras]`, then nothing or a version specifier, a
 * `; marker`, or an `@ url`. Anything else is a URL or a local path.
 */
const NAMED = /^([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)(?:\s*\[([^\]]*)\])?((?:\s*[<>=!~(;@].*)?)$/s

/** A bare URL requirement: `scheme://…` such as `git+https://` or a wheel's `https://`. */
const URL_SOURCE = /^[A-Za-z][A-Za-z0-9+.-]*:\/\//

/** A local path with its trailing `[extras]`, if any, split off. */
const PATH_EXTRAS = /^(.*?)(?:\[([^\]]*)\])?$/s

/** Whether `source` is a local checkout path rather than a named requirement or a URL. */
export function isLocalPath(source: string): boolean {
  const s = source.trim()
  return !NAMED.test(s) && !URL_SOURCE.test(s)
}

const mergeExtras = (own: string | undefined, extras: readonly string[]): string[] => {
  const all = (own ?? '')
    .split(',')
    .map(s => s.trim())
    .filter(Boolean)
  for (const e of extras) if (!all.includes(e)) all.push(e)
  return all
}

/**
 * The pip requirement for `source` with `extras` added. A named requirement
 * gets `name[extras]` before its version specifier, marker or ` @ url`; a
 * bare URL becomes `agent-trajviz[extras] @ url`; a local checkout path gets
 * `path[extras]` (install it with `-e`: see `requirementArgv`). Extras the
 * source already names are kept, in their order, the new ones after them.
 */
export function withExtras(source: string, extras: readonly string[]): string {
  const s = source.trim()
  const named = NAMED.exec(s)
  if (named) {
    const all = mergeExtras(named[2], extras)
    return `${named[1]}${all.length ? `[${all.join(',')}]` : ''}${named[3] ?? ''}`
  }
  if (URL_SOURCE.test(s)) {
    return `${PACKAGE}${extras.length ? `[${extras.join(',')}]` : ''} @ ${s}`
  }
  const m = PATH_EXTRAS.exec(s)
  const all = mergeExtras(m?.[2], extras)
  return `${m?.[1] ?? s}${all.length ? `[${all.join(',')}]` : ''}`
}

/** A leading `~` replaced by `$HOME` (pip gets no shell to expand it). */
async function expandHome($: EngineInterface, path: string): Promise<string> {
  if (path !== '~' && !path.startsWith('~/') && !path.startsWith('~\\')) return path
  const home = (await $.env.get('HOME'))?.replace(/[\\/]+$/, '')
  return home ? `${home}${path.slice(1)}` : path
}

/**
 * The install arguments for `source` with `extras`: the requirement from
 * `withExtras`, preceded by `-e` for a local checkout (installed editable,
 * with `~` expanded).
 */
export async function requirementArgv($: EngineInterface, source: string, extras: readonly string[]): Promise<string[]> {
  const requirement = withExtras(source, extras)
  return isLocalPath(source) ? ['-e', await expandHome($, requirement)] : [requirement]
}

/** Where `state.json` lives: beside the venv under the cache root. */
export async function statePath($: EngineInterface): Promise<string> {
  return `${await cacheRoot($)}/state.json`
}

/** The recorded environment, null when `state.json` is missing or not an EnvState. */
export async function readEnvState($: EngineInterface): Promise<EnvState | null> {
  const pinned = (await readSession($)).environment
  if (pinned && isEnvState(pinned) && await usableState($, pinned)) return pinned
  let text: string
  try {
    const path = await statePath($)
    for (let attempt = 0; ; attempt++) {
      try { text = await $.fs.read(path); break } catch (e) {
        if (attempt >= 10 || !(e instanceof Error) || !/EACCES|EPERM|EBUSY|permission denied|sharing violation|access is denied|being used by another process/i.test(e.message)) throw e
        await $.clock.sleep(20)
      }
    }
  } catch {
    return null
  }
  try {
    const v: unknown = JSON.parse(text)
    if (!isEnvState(v) || !await usableState($, v)) return null
    return pinEnv($, v)
  } catch {
    return null
  }
}

/** Fold path spellings for comparison without resolving the venv's Python symlink. */
function normalizedPath(path: string): string {
  const slash = path.replace(/\\/g, '/')
  const parts: string[] = []
  for (const part of slash.split('/')) {
    if (!part || part === '.') continue
    if (part === '..' && parts.length && parts[parts.length - 1] !== '..') parts.pop()
    else if (part !== '.') parts.push(part)
  }
  return `${slash.startsWith('/') ? '/' : ''}${parts.join('/')}`
}

/** Metadata may name only an immutable generation belonging to this cache. */
async function usableState($: EngineInterface, state: EnvState): Promise<boolean> {
  const insensitive = await isWindows($)
  const localBase = PATH_EXTRAS.exec(state.source)?.[1] ?? state.source
  const relative = isLocalPath(state.source) && !/^~(?:[\\/]|$)/.test(localBase) && !(insensitive ? /^(?:[A-Za-z]:[\\/]|[\\/]{2})/ : /^\//).test(localBase)
  if (relative && state.sourceCwd === undefined) return false
  if (state.sourceCwd !== undefined && state.sourceCwd !== await $.session.cwd()) {
    const cwd = await $.fs.stat(await $.session.cwd(), { resolve: true }).catch(() => undefined)
    if (cwd?.realPath !== state.sourceCwd) return false
  }
  const python = state.python.replace(/\\/g, '/')
  const match = /^(.*)\/envs\/[0-9a-f]{24}\/[0-9a-f]{32}\/(?:bin\/python|Scripts\/python\.exe)$/.exec(python)
  if (!match || /(?:^|\/)\.\.(?:\/|$)/.test(python) || !(insensitive ? python.endsWith('/Scripts/python.exe') : python.endsWith('/bin/python'))) return false
  let root = await cacheRoot($)
  if (!/^(?:[A-Za-z]:[\\/]|[\\/])/.test(root)) root = `${await $.session.cwd()}/${root}`
  const same = (a: string, b: string): boolean => insensitive ? normalizedPath(a).toLowerCase() === normalizedPath(b).toLowerCase() : normalizedPath(a) === normalizedPath(b)
  if (!same(match[1]!, root)) {
    // macOS /tmp, symlinked HOME/cache roots, and Windows aliases can resolve
    // differently in pathlib; only ask for canonicalization when needed.
    const canonical = await $.fs.stat(root, { resolve: true }).catch(() => undefined)
    if (!canonical?.realPath || !same(match[1]!, canonical.realPath)) return false
  }
  return $.fs.exists(state.python)
}

function isEnvState(v: unknown): v is EnvState {
  if (typeof v !== 'object' || v === null) return false
  const o = v as Record<string, unknown>
  return (
    typeof o['python'] === 'string' && !!o['python'].trim() &&
    (typeof o['trajviz'] === 'string' || o['trajviz'] === null) &&
    Array.isArray(o['extras']) &&
    o['extras'].every(e => typeof e === 'string') && o['extras'].includes('llm') &&
    typeof o['source'] === 'string' && !!o['source'].trim() &&
    typeof o['createdAt'] === 'number' && Number.isFinite(o['createdAt']) && o['createdAt'] >= 0 && o['createdAt'] <= 8.64e15 &&
    (o['revision'] === undefined || Number.isInteger(o['revision'])) &&
    (o['pythonOption'] === undefined || typeof o['pythonOption'] === 'string') &&
    (o['sourceCwd'] === undefined || typeof o['sourceCwd'] === 'string')
  )
}

/** Whether the source, interpreter option or installer layout changed. */
export function needsSetup(state: EnvState | null, options: PluginOptions): boolean {
  return state === null || state.revision !== ENV_REVISION || !state.trajviz?.trim() || !BASE_EXTRAS.every(extra => state.extras.includes(extra)) || state.source !== trajvizSource(options) || (state.pythonOption ?? '') !== pythonOverride(options)
}

/** Pins the immutable interpreter to this Claude session, independent of other sessions' latest pointer. */
async function pinEnv($: EngineInterface, state: EnvState): Promise<EnvState> {
  await updateSession($, current => ({ ...current, environment: state }))
  return state
}

/**
 * Whether `e` is the engine refusing the `process` noun itself, as a host
 * that withholds it does (its message names the op: `$.process.run: …`),
 * rather than a command that could not start. Such a refusal is never
 * swallowed as "not found": `/obs` answers one line for it.
 */
export function isProcessWithheld(e: unknown): boolean {
  return e instanceof Error && /^\$\.process\.\w+: /.test(e.message)
}

/** The output of `<cmd> --version`, both streams; null when it cannot start or fails. */
async function probeVersion($: EngineInterface, cmd: string): Promise<string | null> {
  try {
    const r = await $.process.run([cmd, '--version'], { timeoutMs: PROBE_TIMEOUT_MS })
    return r.exitCode === 0 ? `${r.stdout}\n${r.stderr}` : null
  } catch (e) {
    if (isProcessWithheld(e)) throw e
    return null
  }
}

/** The `uv` executable when `uv --version` answers, else null. */
export async function findUv($: EngineInterface): Promise<string | null> {
  return (await probeVersion($, 'uv')) === null ? null : 'uv'
}

const parseVersion = (text: string): { major: number; minor: number; version: string } | null => {
  const m = /(\d+)\.(\d+)(?:\.(\d+))?/.exec(text)
  if (!m) return null
  return { major: Number(m[1]), minor: Number(m[2]), version: m[0] }
}

/**
 * The first interpreter whose `--version` is 3.11 or newer: the override when
 * non-empty, then `python3.13`, `python3.12`, `python3.11`, `python3`,
 * `python`. `looked`, when given, receives one entry per candidate tried.
 */
export async function findPython(
  $: EngineInterface,
  override: string,
  looked?: string[],
): Promise<{ path: string; version: string } | null> {
  const candidates = override ? [override, ...PYTHON_CANDIDATES] : [...PYTHON_CANDIDATES]
  for (const cmd of candidates) {
    const out = await probeVersion($, cmd)
    const v = out === null ? null : parseVersion(out)
    if (v === null) {
      looked?.push(`${cmd} (not found)`)
      continue
    }
    if (v.major > MIN_MAJOR || (v.major === MIN_MAJOR && v.minor >= MIN_MINOR)) return { path: cmd, version: v.version }
    looked?.push(`${cmd} (${v.version}, too old)`)
  }
  return null
}

/** The bootstrap emits progress on stderr and one bounded state document on stdout. */
async function runBootstrap($: EngineInterface, argv: readonly string[], options: PluginOptions, extras: readonly string[], report: (line: string) => void): Promise<EnvState> {
  let stdout = '', pending = '', last = ''
  const emit = (line: string): void => {
    const text = line.slice(-4096).replace(/\r$/, '')
    if (text.trim()) { last = text; report(text) }
  }
  const child = $.process.spawn({ argv, cwd: await $.session.cwd() })
  const timer = new AbortController()
  const expired = $.clock.sleep(BOOTSTRAP_TIMEOUT_MS, { signal: timer.signal }).then(() => { throw new Error('obs setup: installation exceeded 10 minutes; try /obs again.') })
  let code: number | null = null
  let done = false
  try {
    for (;;) {
      const chunk = await Promise.race([child.next(), expired])
      if (chunk.done) { code = chunk.value.code; done = true; break }
      if (chunk.value.stream === 'stdout') {
        if (stdout.length + chunk.value.text.length > 65_536) throw new Error('obs setup: bootstrap output exceeded 64 KB.')
        stdout += chunk.value.text
      } else {
        const lines = (pending + chunk.value.text).split('\n')
        pending = (lines.pop() ?? '').slice(-4096)
        for (const line of lines) emit(line)
      }
    }
  } finally {
    timer.abort()
    if (!done) {
      try { await child.return({ code: null, signal: null }) } catch { /* Preserve the setup failure. */ }
    }
  }
  emit(pending)
  let result: unknown
  try { result = JSON.parse(stdout) } catch { /* Report malformed output below. */ }
  const error = typeof result === 'object' && result !== null && 'error' in result ? (result as { error?: { message?: unknown } }).error : undefined
  if (code !== 0 || error) throw new Error(`obs setup: ${typeof error?.message === 'string' ? error.message : `bootstrap exited with ${code ?? 'a signal'}${last ? `: ${last}` : ''}`}`)
  if (!isEnvState(result) || needsSetup(result, options) || !extras.every(extra => result.extras.includes(extra)) || !await usableState($, result)) throw new Error('obs setup: bootstrap returned an invalid environment document.')
  return pinEnv($, result)
}

/** Host Python runs the stdlib worker; the target venv is never used as its bootstrap. */
async function setupEnvironment($: EngineInterface, options: PluginOptions, extras: readonly string[], report: (line: string) => void): Promise<EnvState> {
  const uv = await findUv($)
  const override = pythonOverride(options)
  let command: string[]
  if (uv) command = [uv, 'run', '--no-project', '--python', override || '>=3.11', '--', 'python', '-I']
  else {
    const looked = ['uv (not found)']
    const found = await findPython($, override, looked)
    if (!found) throw new NoPythonError(looked)
    command = [found.path, '-I']
  }
  const source = trajvizSource(options)
  const request = { cache: await cacheRoot($), source, installSource: await expandHome($, source), local: isLocalPath(source), pythonOption: override, uv, extras }
  command.push(`${$.plugin.root}/bridge/bootstrap.py`, '--request', JSON.stringify(request))
  return runBootstrap($, command, options, extras, report)
}

/** Reuses a ready immutable interpreter without spawning, otherwise asks the locked worker to build or find one. */
export async function ensureEnv($: EngineInterface, options: PluginOptions, report: (line: string) => void): Promise<EnvState> {
  const existing = await readEnvState($)
  if (existing && !needsSetup(existing, options)) return pinEnv($, existing)
  return setupEnvironment($, options, BASE_EXTRAS, report)
}

/** Extra installs publish a new generation; running analyses keep their existing interpreter. */
export async function ensureExtra($: EngineInterface, options: PluginOptions, extra: Extra, report: (line: string) => void): Promise<void> {
  const existing = await readEnvState($)
  if (existing && !needsSetup(existing, options) && existing.extras.includes(extra)) { await pinEnv($, existing); return }
  await setupEnvironment($, options, [...BASE_EXTRAS, ...(existing && !needsSetup(existing, options) ? existing.extras : []), extra], report)
}
