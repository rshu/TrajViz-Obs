import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process'
import { mkdir, readFile, writeFile, rename, access, unlink } from 'node:fs/promises'
import { constants } from 'node:fs'
import { homedir } from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { randomUUID } from 'node:crypto'

export type Options = {
  python?: string; trajvizSource?: string; cacheRoot?: string; serverUrl?: string; serverPasswordEnv?: string
  llm?: 'session' | 'env'; analysisModel?: string; labelModel?: string; labelMaxSteps?: number
  openBrowser?: boolean; watch?: boolean; statusline?: boolean
}
type Environment = { python: string; source: string; revision: number; extras: string[]; pythonOption?: string; sourceCwd?: string }
export const exists = async (p: string) => access(p, constants.F_OK).then(() => true, () => false)
export async function atomicJSON(file: string, value: unknown): Promise<void> {
  return atomicFile(file, JSON.stringify(value))
}
export async function atomicFile(file: string, data: string | Uint8Array): Promise<void> {
  await mkdir(path.dirname(file), { recursive: true, mode: 0o700 })
  const temp = `${file}.${randomUUID()}.tmp`
  try {
    await writeFile(temp, data, { mode: 0o600, flag: 'wx' })
    await rename(temp, file)
  } finally { await unlink(temp).catch(() => {}) }
}

export function stopChild(child: ChildProcessWithoutNullStreams): void {
  if (child.exitCode !== null || child.signalCode !== null) return
  child.stdin.end()
  if (process.platform === 'win32') {
    if (child.pid) spawn('taskkill', ['/pid', String(child.pid), '/t', '/f'], { windowsHide: true, stdio: 'ignore' }).unref()
  } else if (child.pid) {
    try { process.kill(-child.pid, 'SIGTERM') } catch { child.kill('SIGTERM') }
    const timer = setTimeout(() => { if (child.exitCode === null && child.signalCode === null) { try { process.kill(-child.pid!, 'SIGKILL') } catch { child.kill('SIGKILL') } } }, 2000)
    timer.unref()
    child.once('close', () => clearTimeout(timer))
  }
}

export class Runtime {
  readonly cache: string
  readonly children = new Set<ChildProcessWithoutNullStreams>()
  private environment?: Environment
  private preparing?: Promise<Environment>
  private closed = false
  readonly bridge: string
  constructor(readonly options: Options, readonly cwd: string) {
    this.cache = path.resolve(cwd, options.cacheRoot ?? path.join(process.env.XDG_CACHE_HOME ?? path.join(homedir(), '.cache'), 'trajviz-obs', 'opencode2'))
    this.bridge = fileURLToPath(new URL('../runtime/bridge/', import.meta.url))
  }
  spawn(argv: readonly string[]): ChildProcessWithoutNullStreams {
    if (this.closed) throw new Error('The obs plugin is unloading.')
    if (!argv[0]) throw new Error('Missing executable')
    const child = spawn(argv[0], argv.slice(1), { cwd: this.cwd, env: process.env, detached: process.platform !== 'win32', windowsHide: true, stdio: 'pipe' })
    this.children.add(child)
    child.once('close', () => this.children.delete(child))
    return child
  }
  async execute<T>(argv: readonly string[], signal?: AbortSignal, timeout = 120_000, maxBytes = 4 * 1024 * 1024, raw = false): Promise<T> {
    signal?.throwIfAborted()
    return new Promise<T>((resolve, reject) => {
      const child = this.spawn(argv)
      child.stdout.setEncoding('utf8')
      child.stderr.setEncoding('utf8')
      let stdout = '', stderr = '', bytes = 0, failure: Error | undefined
      const fail = (error: Error) => { failure ??= error; stopChild(child) }
      const abort = () => fail(new Error('Cancelled.'))
      signal?.addEventListener('abort', abort, { once: true })
      const timer = setTimeout(() => fail(new Error(`Operation exceeded ${Math.round(timeout / 1000)} seconds.`)), timeout)
      child.stdout.on('data', chunk => { bytes += Buffer.byteLength(chunk); if (bytes > maxBytes) fail(new Error('Bridge output exceeded its size limit. Export a smaller view.')); else stdout += chunk })
      child.stderr.on('data', chunk => { stderr = (stderr + chunk).slice(-65536) })
      child.once('error', error => { failure = error })
      child.once('close', code => {
        clearTimeout(timer); signal?.removeEventListener('abort', abort)
        if (failure) { reject(failure); return }
        try {
          if (raw) { if (code !== 0) throw new Error(stderr || 'Command failed'); resolve(stdout as T); return }
          const doc = JSON.parse(stdout)
          if (doc?.error) throw new Error(doc.error.message ?? 'Bridge operation failed.')
          if (code !== 0) throw new Error(`Bridge exited ${code}: ${stderr.trim().split('\n').slice(-1).join('')}`)
          resolve(doc as T)
        } catch (error) { reject(error instanceof SyntaxError ? new Error(`Bridge returned no valid result: ${stderr.trim().split('\n').slice(-1).join('')}`) : error) }
      })
    })
  }
  async ready(extras: string[] = [], signal?: AbortSignal): Promise<Environment> {
    signal?.throwIfAborted()
    const source = this.options.trajvizSource ?? 'agent-trajviz>=0.2.0,<0.3'
    if (this.environment?.source === source && extras.every(e => this.environment!.extras.includes(e))) return this.environment
    if (this.preparing) { await this.preparing; return this.ready(extras, signal) }
    this.preparing = (async () => {
      try {
        const prior: Environment = JSON.parse(await readFile(path.join(this.cache, 'state.json'), 'utf8'))
        const relativeSource = /^(?:\.\.?[/\\]|~[/\\])/.test(source) && !source.startsWith('~')
        const relative = typeof prior.python === 'string' ? path.relative(this.cache, prior.python).split(path.sep).join('/') : ''
        const managed = /^envs\/[a-f0-9]{24}\/[a-f0-9]{32}\/(?:bin\/python|Scripts\/python\.exe)$/.test(relative)
        if (prior.revision === 4 && prior.source === source && (prior.pythonOption ?? '') === (this.options.python ?? '') && Array.isArray(prior.extras) && ['llm','charts',...extras].every(e => prior.extras.includes(e)) && managed && (!relativeSource || prior.sourceCwd === this.cwd) && await exists(prior.python)) return prior
      } catch { /* Bootstrap validates and repairs a missing or stale generation. */ }
      let host: string[] | undefined
      if (!this.options.python) {
        try { await this.execute(['uv', '--version'], signal, 10_000, 65536, true); host = ['uv', 'run', '--no-project', '--python', '>=3.11', '--', 'python', '-I'] } catch { signal?.throwIfAborted() }
      }
      if (!host) {
        for (const candidate of this.options.python ? [this.options.python] : ['python3.13', 'python3.12', 'python3.11', 'python3', 'python']) {
          try {
            await this.execute([candidate, '-c', 'import json,sys; print(json.dumps({"ok": sys.version_info >= (3,11)}))'], signal, 10_000, 65536).then(v => { if (!(v as {ok:boolean}).ok) throw new Error('Old Python') })
            host = [candidate, '-I']; break
          } catch { signal?.throwIfAborted() }
        }
      }
      if (!host) throw new Error('Install uv or Python 3.11+, or set the plugin python option.')
      const local = !/^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[^\]]*\])?(?:[<>=!~; @].*)?$/.test(source) && !/^[a-z+]+:\/\//i.test(source)
      const request = { cache: this.cache, source, installSource: source.replace(/^~(?=[/\\])/, homedir()), local, pythonOption: this.options.python ?? '', uv: host[0] === 'uv' ? 'uv' : null, extras }
      return this.execute<Environment>([...host, path.join(this.bridge, 'bootstrap.py'), '--request', JSON.stringify(request)], signal, 600_000, 65536)
    })()
    try { this.environment = await this.preparing; return this.environment } finally { this.preparing = undefined }
  }
  async argv(args: readonly string[], signal?: AbortSignal, extras: string[] = []): Promise<string[]> {
    const env = await this.ready(extras, signal)
    return [env.python, '-I', path.join(this.bridge, 'trajviz_obs_cli.py'), ...(process.platform === 'win32' ? [] : ['--host-parent',String(process.pid)]), ...args]
  }
  async run<T>(args: readonly string[], signal?: AbortSignal, timeout = 120_000): Promise<T> { return this.execute<T>(await this.argv(args, signal), signal, timeout) }
  close(): void { this.closed = true; for (const child of this.children) stopChild(child) }
}
