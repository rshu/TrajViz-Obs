import type { CommandResult, JobPoll, RunInput } from './rpc.js'
import { errorMessage } from './tui-model.js'

type Record = { input: RunInput; signature: string; progress?: string; result?: CommandResult; finished?: number; bytes?: number; leaseUntil: number; timer?: ReturnType<typeof setTimeout> }
type Retired = { runID: string; sessionID: string; expires: number }
const key = (input: { runID: string; confirmationID?: string }) => JSON.stringify([input.runID, input.confirmationID ?? null])
const signature = (input: RunInput) => JSON.stringify([input.sessionID, input.args, input.useSynced === true,
  input.paneSnapshot === true, input.model?.providerID, input.model?.id, input.model?.variant])

/** Short authenticated RPC requests observe server-owned work without replaying it. */
export class CommandJobs {
  private readonly records = new Map<string, Record>()
  private readonly retired = new Map<string, Retired>()
  private readonly cancelled = new Map<string, number>()
  private blockedUntil = 0
  private closed = false
  private readonly now: () => number
  private readonly activeLimit: number
  private readonly resultLimit: number
  private readonly historyLimit: number
  private readonly retention: number
  private readonly lease: number
  private readonly resultBytes: number
  private readonly cancelWork?: (runID: string) => void
  constructor(options: { now?: () => number; activeLimit?: number; resultLimit?: number; historyLimit?: number; retention?: number; lease?: number; resultBytes?: number; cancelWork?: (runID: string) => void } = {}) {
    this.now = options.now ?? Date.now
    this.activeLimit = options.activeLimit ?? 16
    this.resultLimit = options.resultLimit ?? 32
    this.historyLimit = options.historyLimit ?? 256
    this.retention = options.retention ?? 15 * 60_000
    this.lease = options.lease ?? 120_000
    this.resultBytes = options.resultBytes ?? 32 * 1024 * 1024
    this.cancelWork = options.cancelWork
  }
  private renew(record: Record) {
    clearTimeout(record.timer)
    record.leaseUntil = this.now() + this.lease
    const deadline = record.leaseUntil
    record.timer = setTimeout(() => {
      if (this.closed || this.records.get(key(record.input)) !== record || record.finished !== undefined || record.leaseUntil !== deadline) return
      this.stop(record.input.runID); this.cancelWork?.(record.input.runID)
    }, this.lease)
    record.timer.unref()
  }
  private prune() {
    const now = this.now()
    for (const record of [...this.records.values()]) if (record.finished === undefined && record.leaseUntil <= now) { this.stop(record.input.runID); this.cancelWork?.(record.input.runID) }
    for (const [id, expires] of this.cancelled) if (expires <= now) this.cancelled.delete(id)
    for (const [id, record] of this.retired) if (record.expires <= now) this.retired.delete(id)
    for (const [id, record] of this.records) if (record.finished !== undefined && record.finished + this.retention <= now) this.records.delete(id)
  }
  private trim() {
    const finished = [...this.records].filter(([, record]) => record.finished !== undefined)
    let count = finished.length, bytes = finished.reduce((sum, [, record]) => sum + (record.bytes ?? 0), 0)
    for (const [id, record] of finished) {
      if (count <= this.resultLimit && bytes <= this.resultBytes) break
      this.records.delete(id)
      this.retired.set(id, { runID: record.input.runID, sessionID: record.input.sessionID, expires: record.finished! + this.retention })
      count--; bytes -= record.bytes ?? 0
    }
  }
  start(input: RunInput, work: (input: RunInput) => Promise<CommandResult>): { accepted: true } {
    this.prune()
    if (this.closed) throw new Error('TrajViz Obs is reloading.')
    if (this.cancelled.has(input.runID)) throw new Error('This obs command was cancelled. Start a new command.')
    const id = key(input), existing = this.records.get(id), fingerprint = signature(input)
    if (existing) {
      if (existing.signature !== fingerprint) throw new Error('This obs job belongs to another request.')
      return { accepted: true }
    }
    if (this.retired.has(id)) throw new Error('This obs result expired. Start a new command.')
    const active = [...this.records.values()].filter(record => record.finished === undefined)
    if (active.some(record => record.input.runID === input.runID)) throw new Error('This obs command is already running.')
    if (active.length >= this.activeLimit) throw new Error('Too many obs commands are running. Wait or cancel one.')
    if (this.now() < this.blockedUntil || this.records.size + this.retired.size + this.cancelled.size >= this.historyLimit) throw new Error('Obs job history is full. Wait for previous results to expire.')
    const fixed = { ...input, ...(input.model ? { model: { ...input.model } } : {}) }
    const record: Record = { input: fixed, signature: fingerprint, leaseUntil: this.now() + this.lease }
    this.records.set(id, record)
    this.renew(record)
    const finish = (result: CommandResult) => {
      if (this.closed || this.records.get(id) !== record) return
      clearTimeout(record.timer)
      record.bytes = Buffer.byteLength(JSON.stringify(result))
      record.result = record.bytes <= this.resultBytes ? result : { text: 'Obs result exceeds retained size. Use a smaller view limit or open its exported file.', ...(result.path ? { path: result.path } : {}) }
      if (record.bytes > this.resultBytes) record.bytes = Buffer.byteLength(JSON.stringify(record.result))
      record.finished = this.now(); delete record.progress
      this.records.delete(id); this.records.set(id, record)
      this.trim()
    }
    void Promise.resolve().then(() => this.closed || this.records.get(id) !== record ? { text: 'Cancelled.' } : work(fixed))
      .then(finish, error => finish({ text: `obs: ${errorMessage(error)}` }))
    return { accepted: true }
  }
  poll(input: { sessionID: string; runID: string; confirmationID?: string }): JobPoll {
    this.prune()
    const record = this.records.get(key(input))
    if (this.closed || this.cancelled.has(input.runID) || !record || record.input.sessionID !== input.sessionID) return { state: 'expired' }
    if (record.result) return { state: 'finished', result: record.result }
    this.renew(record)
    return { state: 'pending', ...(record.progress === undefined ? {} : { progress: record.progress }) }
  }
  progress(runID: string, text: string) {
    for (const record of this.records.values()) if (record.input.runID === runID && record.finished === undefined) record.progress = text.slice(0, 64_000)
  }
  cancel(runID: string): boolean {
    this.prune()
    return this.stop(runID)
  }
  private stop(runID: string): boolean {
    let found = false
    for (const [id, record] of this.records) if (record.input.runID === runID) { clearTimeout(record.timer); this.records.delete(id); found = true }
    for (const [id, record] of this.retired) if (record.runID === runID) { this.retired.delete(id); found = true }
    // A cancelled request may arrive before its start acknowledgement. Retain a
    // tombstone rather than allowing that delayed start to launch paid work.
    if (this.cancelled.has(runID) || this.records.size + this.retired.size + this.cancelled.size < this.historyLimit) this.cancelled.set(runID, this.now() + this.retention)
    else this.blockedUntil = this.now() + this.retention
    return found
  }
  close() { this.closed = true; for (const record of this.records.values()) clearTimeout(record.timer); this.records.clear(); this.retired.clear(); this.cancelled.clear() }
}
