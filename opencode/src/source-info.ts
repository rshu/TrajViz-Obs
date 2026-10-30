import { readFile, stat } from 'node:fs/promises'
import path from 'node:path'

/** Compound sources must keep their companion files when frozen or refreshed. */
export async function inspectSource(file: string): Promise<{ compound: boolean; bytes?: Buffer }> {
  if ((await stat(file)).isDirectory() || /\.(?:jsonl|zst|zstd)$/i.test(file)) return { compound: true }
  const bytes = await readFile(file)
  let compound = false
  // Published manifests are UTF-8 (an optional BOM is valid), at most 1 MiB,
  // and need not use a .json suffix. Binary ZIP bytes never enter text sniffing.
  if (bytes.length <= 1024 * 1024 && path.extname(file).toLowerCase() !== '.zip' && !(bytes[0] === 0x50 && bytes[1] === 0x4b)) {
    try { compound = JSON.parse(bytes.toString('utf8').replace(/^\uFEFF/, ''))?.type === 'agent-trajviz-otlp-bundle' } catch { /* The loader reports invalid input. */ }
  }
  return { compound, bytes }
}
