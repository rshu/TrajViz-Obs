import { afterEach, expect, test } from 'bun:test'
import { execFileSync } from 'node:child_process'
import { mkdtemp, readFile, readdir, rm, utimes, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { Revisions } from '../src/revisions.js'
import { exists } from '../src/runtime.js'

const owned: {store:Revisions;folder:string}[] = []
afterEach(async () => { for (const {store,folder} of owned.splice(0)) { await store.close(); await rm(folder,{recursive:true,force:true}) } })
async function fixture() {
  const folder = await mkdtemp(path.join(tmpdir(),'trajviz-revisions-'))
  const store = new Revisions(); owned.push({store,folder})
  return {store,folder}
}

// Filesystem interception stays in a child process so other tests keep their real filesystem bindings.
function isolated(folder: string, body: string): unknown {
  const script = `
    import { mock } from 'bun:test'
    import * as fs from 'node:fs/promises'
    import path from 'node:path'
    const original = {...fs}
    const directory = ${JSON.stringify(folder)}
    const moduleURL = ${JSON.stringify(new URL('../src/revisions.ts', import.meta.url).href)}
    ${body}
  `
  return JSON.parse(execFileSync(process.execPath, ['--eval', script], {encoding:'utf8',timeout:10_000}))
}

test('frequent live refreshes retain eight immutable recent revisions and preserve unrelated files', async () => {
  const {store,folder} = await fixture()
  let latest = ''
  for (let i=0;i<24;i++) latest = await store.write(folder,JSON.stringify({version:i}))
  const directory = path.dirname(latest)
  expect((await readdir(directory)).filter(p=>p.endsWith('.json'))).toHaveLength(8)
  expect(JSON.parse(await readFile(latest,'utf8'))).toEqual({version:23})
  const unrelated = path.join(directory,'user-output.txt')
  await writeFile(unrelated,'Keep this export')
  await store.close()
  expect(await readFile(unrelated,'utf8')).toBe('Keep this export')
  expect(await exists(latest)).toBe(false)
})

test('active readers remain byte-correct while newer watch captures are pruned', async () => {
  const {store,folder} = await fixture()
  let ready!: () => void, release!: () => void, held = ''
  const began = new Promise<void>(resolve=>{ready=resolve})
  const resume = new Promise<void>(resolve=>{release=resolve})
  const reading = store.run(async () => {
    held = await store.write(folder,'{"reviewed":"original"}')
    await utimes(held,1,1)
    ready(); await resume
    expect(await readFile(held,'utf8')).toBe('{"reviewed":"original"}')
  })
  await began
  for (let i=0;i<20;i++) await store.write(folder,JSON.stringify({newer:i}))
  expect(await exists(held)).toBe(true)
  release(); await reading
  expect(await exists(held)).toBe(false)
  expect((await readdir(path.dirname(held))).filter(p=>p.endsWith('.json'))).toHaveLength(8)
})

test('a dashboard lease survives refresh pruning and unload waits until the owner releases it', async () => {
  const {store,folder} = await fixture()
  const file = await store.write(folder,'{"dashboard":"original"}')
  const release = store.retain(file)
  await utimes(file,1,1)
  for (let i=0;i<15;i++) await store.write(folder,JSON.stringify({newer:i}))
  let closed = false
  const closing = store.close().then(()=>{closed=true})
  await Promise.resolve()
  expect(closed).toBe(false)
  expect(await readFile(file,'utf8')).toBe('{"dashboard":"original"}')
  release(); await closing
  expect(closed).toBe(true)
  expect(await exists(file)).toBe(false)
})

test('separate plugin instances cannot remove each other’s live captures', async () => {
  const {store,folder} = await fixture(), other = new Revisions()
  const first = await store.write(folder,'{"instance":1}')
  const second = await other.write(folder,'{"instance":2}')
  try {
    expect(path.dirname(first)).not.toBe(path.dirname(second))
    await store.close()
    expect(await exists(first)).toBe(false)
    expect(await readFile(second,'utf8')).toBe('{"instance":2}')
  } finally { await other.close() }
})

test('rewriting a revision during pruning preserves its later dashboard lease', async () => {
  const {folder} = await fixture()
  const result = isolated(folder, `
    let target = '', began, finish
    const deleting = new Promise(resolve => { began = resolve })
    const resumed = new Promise(resolve => { finish = resolve })
    mock.module('node:fs/promises', () => ({...original, unlink: async file => {
      if (file === target) { target = ''; began(); await resumed }
      return original.unlink(file)
    }}))
    const {Revisions} = await import(moduleURL)
    const store = new Revisions()
    let release = () => {}
    try {
      const text = '{"reviewed":"original"}'
      const oldest = await store.write(directory, text)
      await original.utimes(oldest, 1, 1)
      for (let i = 0; i < 7; i++) await store.write(directory, JSON.stringify({newer:i}))
      target = oldest
      const pruning = store.write(directory, '{"newest":true}')
      await deleting
      const rewriting = store.write(directory, text)
      finish()
      await Promise.all([pruning, rewriting])
      release = store.retain(oldest)
      await original.utimes(oldest, 1, 1)
      for (let i = 0; i < 12; i++) await store.write(directory, JSON.stringify({latest:i}))
      console.log(JSON.stringify({contents:await original.readFile(oldest, 'utf8').catch(() => null)}))
    } finally { release(); await store.close() }
  `)
  expect(result).toEqual({contents:'{"reviewed":"original"}'})
})

test('a queued write cannot create a revision after unloading begins', async () => {
  const {folder} = await fixture()
  const result = isolated(folder, `
    let target = '', began, finish
    const creating = new Promise(resolve => { began = resolve })
    const resumed = new Promise(resolve => { finish = resolve })
    mock.module('node:fs/promises', () => ({...original, mkdir: async (folder, options) => {
      if (folder === target) { target = ''; began(); await resumed }
      return original.mkdir(folder, options)
    }}))
    const {Revisions} = await import(moduleURL)
    const store = new Revisions()
    target = path.join(directory, 'captures', store.instance)
    const first = store.write(directory, '{"active":true}')
    await creating
    const queued = store.write(directory, '{"queued":true}').then(() => false, error => error.message.includes('unloading'))
    const closing = store.close()
    finish()
    await first
    await closing
    console.log(JSON.stringify({rejected:await queued}))
  `)
  expect(result).toEqual({rejected:true})
})
