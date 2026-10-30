import { afterEach, expect, test } from 'bun:test'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { Runtime } from '../src/runtime.js'

const owned: { runtime: Runtime; folder: string }[] = []
afterEach(async () => {
  for (const { runtime, folder } of owned.splice(0)) { runtime.close(); await rm(folder, { recursive: true, force: true }) }
})
async function runtime(): Promise<Runtime> {
  const folder = await mkdtemp(path.join(tmpdir(), 'trajviz-runtime-test-'))
  const instance = new Runtime({ cacheRoot: path.join(folder, 'cache') }, folder)
  owned.push({ runtime: instance, folder })
  return instance
}

test('runtime executes argument arrays literally and keeps stdout as one JSON result', async () => {
  const instance = await runtime()
  const literal = 'path with spaces; $(no-shell) `no-shell`'
  const result = await instance.execute<{ value: string }>([process.execPath, '-e', 'console.log(JSON.stringify({value: process.argv.at(-1)}))', '--', literal])
  expect(result.value).toBe(literal)
  expect(instance.children.size).toBe(0)
})

test('runtime preserves bridge errors and rejects invalid or excessive output', async () => {
  const instance = await runtime()
  await expect(instance.execute([process.execPath, '-e', 'console.log(JSON.stringify({error:{message:"Controlled failure"}}));process.exit(1)'])).rejects.toThrow('Controlled failure')
  await expect(instance.execute([process.execPath, '-e', 'console.log("not JSON")'])).rejects.toThrow('Bridge returned no valid result')
  await expect(instance.execute([process.execPath, '-e', 'process.stdout.write("x".repeat(4096))'], undefined, 5000, 256)).rejects.toThrow('size limit')
  expect(instance.children.size).toBe(0)
})

test('runtime cancellation and timeout terminate owned processes', async () => {
  const instance = await runtime()
  const controller = new AbortController()
  const pending = instance.execute([process.execPath, '-e', 'setInterval(()=>{},1000)'], controller.signal)
  const timer = setTimeout(() => controller.abort(), 30)
  try { await expect(pending).rejects.toThrow('Cancelled') } finally { clearTimeout(timer) }
  expect(instance.children.size).toBe(0)
  await expect(instance.execute([process.execPath, '-e', 'setInterval(()=>{},1000)'], undefined, 40)).rejects.toThrow('Operation exceeded')
  expect(instance.children.size).toBe(0)
})

test('a pre-cancelled operation starts no child process', async () => {
  const instance = await runtime()
  const controller = new AbortController(); controller.abort()
  await expect(instance.execute([process.execPath, '-e', 'console.log("unexpected")'], controller.signal)).rejects.toThrow()
  expect(instance.children.size).toBe(0)
})
