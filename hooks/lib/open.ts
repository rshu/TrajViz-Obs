// Opening a file or URL in the person's browser: `open` on macOS, `xdg-open`
// on Linux, `cmd /c start "" <target>` on Windows; never over SSH or when
// `openBrowser` is off (the caller prints the path or URL then). Every helper
// taking `$` is a function declaration (validate follows `$` into one).
import type { EngineInterface, PluginOptions } from 'claude-code'
import { isSsh, isWindows } from './paths'

export type OpenResult = 'opened' | 'skipped-ssh' | 'disabled' | 'failed'

/** How long the opener may take; the browser detaches at once, so this only bounds a stuck one. */
const OPEN_TIMEOUT_MS = 15_000
const UNAME_TIMEOUT_MS = 10_000

/** `uname -s` of this host, asked once per module load; "" when it cannot answer. */
let kernelName: Promise<string> | null = null

/** Forgets the cached `uname -s` (for tests that change platform). */
export function forgetPlatform(): void {
  kernelName = null
}

async function kernel($: EngineInterface): Promise<string> {
  kernelName ??= $.process.run(['uname', '-s'], { timeoutMs: UNAME_TIMEOUT_MS }).then(
    r => (r.exitCode === 0 ? r.stdout.trim() : ''),
    () => '',
  )
  return kernelName
}

/** The command that opens `target` on this host. */
export async function openArgv($: EngineInterface, target: string): Promise<string[]> {
  if (await isWindows($)) return ['cmd', '/c', 'start', '', target]
  return (await kernel($)) === 'Darwin' ? ['open', target] : ['xdg-open', target]
}

/**
 * Opens `target` (a file path or URL) in the browser, unless `openBrowser`
 * is off (`disabled`) or the session runs over SSH (`skipped-ssh`); `failed`
 * when the opener is missing or exits non-zero.
 */
export async function openTarget($: EngineInterface, options: PluginOptions, target: string): Promise<OpenResult> {
  if (options['openBrowser'] === false) return 'disabled'
  if (await isSsh($)) return 'skipped-ssh'
  try {
    const r = await $.process.run(await openArgv($, target), { timeoutMs: OPEN_TIMEOUT_MS })
    return r.exitCode === 0 ? 'opened' : 'failed'
  } catch {
    return 'failed'
  }
}
