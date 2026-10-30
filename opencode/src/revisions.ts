import { AsyncLocalStorage } from 'node:async_hooks'
import { createHash, randomUUID } from 'node:crypto'
import { mkdir, readdir, rmdir, stat, unlink } from 'node:fs/promises'
import path from 'node:path'
import { atomicFile, exists } from './runtime.js'

const SOURCE = /^source-[a-f0-9]{64}\.json$/
const INSTANCE = /^([1-9]\d*)-[a-f0-9-]{36}$/
const KEEP = 8

/** Each backend owns its capture directory. Active request/dashboard leases protect immutable inputs. */
export class Revisions {
  readonly instance = `${process.pid}-${randomUUID()}`
  private readonly scopes = new AsyncLocalStorage<Map<string, () => void>>()
  private readonly holds = new Map<string, number>()
  private readonly files = new Set<string>()
  private readonly folders = new Set<string>()
  private readonly queues = new Map<string, Promise<void>>()
  private readonly scanned = new Set<string>()
  private closing = false
  private closed?: Promise<void>
  private finishClose?: () => void

  private async locked<T>(folder: string, task: () => Promise<T>): Promise<T> {
    const previous = this.queues.get(folder) ?? Promise.resolve()
    let release!: () => void
    const gate = new Promise<void>(resolve => { release = resolve })
    this.queues.set(folder, gate)
    await previous
    try { return await task() }
    finally { release(); if (this.queues.get(folder) === gate) this.queues.delete(folder) }
  }

  async run<T>(task: () => Promise<T>): Promise<T> {
    if (this.closing) throw new Error('The obs plugin is unloading.')
    const leases = new Map<string, () => void>()
    return this.scopes.run(leases, async () => {
      try { return await task() }
      finally {
        for (const release of leases.values()) release()
        await Promise.all([...new Set([...leases.keys()].map(p => path.dirname(p)))].map(folder => this.prune(folder)))
      }
    })
  }

  retain(file: string): () => void {
    if (!this.files.has(file)) return () => {}
    this.holds.set(file, (this.holds.get(file) ?? 0) + 1)
    let released = false
    return () => {
      if (released) return
      released = true
      const count = (this.holds.get(file) ?? 1) - 1
      if (count > 0) this.holds.set(file, count); else this.holds.delete(file)
      void this.prune(path.dirname(file))
      this.maybeClose()
    }
  }

  async write(artifacts: string, text: string): Promise<string> {
    if (this.closing) throw new Error('The obs plugin is unloading.')
    const parent = path.join(artifacts, 'captures'), folder = path.join(parent, this.instance)
    const file = path.join(folder, `source-${createHash('sha256').update(text).digest('hex')}.json`)
    const scope = this.scopes.getStore()
    let temporary = () => {}
    try {
      await this.locked(folder, async () => {
        if (this.closing) throw new Error('The obs plugin is unloading.')
        this.files.add(file); this.folders.add(folder)
        temporary = this.retain(file)
        if (scope && !scope.has(file)) scope.set(file, this.retain(file))
        await mkdir(folder, {recursive:true,mode:0o700})
        if (!await exists(file)) await atomicFile(file, text)
        await this.pruneUnlocked(folder)
      })
      if (!this.scanned.has(parent)) { this.scanned.add(parent); await this.scavenge(parent) }
      return file
    } finally { temporary(); await this.prune(folder) }
  }

  private async pruneUnlocked(folder: string): Promise<void> {
    const entries = await readdir(folder, {withFileTypes:true}).catch(() => [])
    const files = await Promise.all(entries.filter(e => e.isFile() && SOURCE.test(e.name)).map(async e => {
      const file = path.join(folder,e.name)
      return {file,modified:(await stat(file).catch(() => ({mtimeMs:0}))).mtimeMs}
    }))
    files.sort((a,b) => b.modified-a.modified || a.file.localeCompare(b.file))
    let retained = 0
    for (const {file} of files) {
      if (this.holds.has(file)) continue
      if (!this.closing && retained++ < KEEP) continue
      await unlink(file).catch(() => {})
      this.files.delete(file)
    }
    if (this.closing) await rmdir(folder).catch(() => {})
  }

  private async prune(folder: string): Promise<void> {
    await this.locked(folder, () => this.pruneUnlocked(folder)).catch(() => {})
  }

  private async scavenge(parent: string): Promise<void> {
    for (const entry of await readdir(parent, {withFileTypes:true}).catch(() => [])) {
      const match = INSTANCE.exec(entry.name)
      if (!entry.isDirectory() || !match || entry.name === this.instance) continue
      try { process.kill(Number(match[1]), 0); continue }
      catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ESRCH') continue }
      const folder = path.join(parent,entry.name)
      // Never remove arbitrary files or follow links in a cache directory.
      for (const file of await readdir(folder, {withFileTypes:true}).catch(() => [])) if (file.isFile() && SOURCE.test(file.name)) await unlink(path.join(folder,file.name)).catch(() => {})
      await rmdir(folder).catch(() => {})
    }
  }

  close(): Promise<void> {
    if (!this.closed) {
      this.closing = true
      this.closed = new Promise<void>(resolve => { this.finishClose = resolve })
      this.maybeClose()
    }
    return this.closed
  }

  private maybeClose(): void {
    if (this.closing && this.holds.size === 0 && this.finishClose) {
      const finish = this.finishClose; this.finishClose = undefined
      void Promise.all([...this.folders].map(folder => this.prune(folder))).finally(finish)
    }
  }
}
