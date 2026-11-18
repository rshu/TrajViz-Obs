import { realpath, stat } from 'node:fs/promises'
import path from 'node:path'
import { homedir } from 'node:os'
import type { ExtensionContext } from '@earendil-works/pi-coding-agent'

/** The bridge reads private captures, so also protect Pi's actual live file. */
export async function protectNativeOutput(ctx: ExtensionContext, outputs: unknown[]): Promise<void> {
  if (!outputs.some(value => typeof value === 'string' && value.trim())) return
  const original = ctx.sessionManager?.getSessionFile?.()
  if (!original) return
  const source = path.resolve(ctx.cwd, original)
  const sourcePath = await realpath(source).catch(() => source)
  const sourceStat = await stat(source).catch(() => undefined)
  for (const value of outputs) {
    if (typeof value !== 'string' || !value.trim()) continue
    const output = path.resolve(ctx.cwd, value.replace(/^~(?=[/\\])/, homedir()))
    const outputPath = await realpath(output).catch(() => output)
    const metadata = await stat(output).catch(() => undefined)
    if (outputPath === sourcePath || (metadata && sourceStat && metadata.dev === sourceStat.dev && metadata.ino === sourceStat.ino)) {
      throw new Error(`Output path would overwrite input session file: ${source}`)
    }
  }
}
