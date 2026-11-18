import type { LocalCheckoutUpdater, LocalUpdatePoll, LocalUpdateResult } from './shared/local-update.js'

type Input = { sourcePath: string; token: string; runID: string }
type Record = { identity: string; progress?: string; result?: LocalUpdateResult; finished?: number }

/** Short RPC requests observe an update without tying its worker to a UI connection. */
export class LocalUpdateJobs {
  private readonly records = new Map<string, Record>()
  private closed = false
  constructor(private readonly updater: Pick<LocalCheckoutUpdater, 'apply' | 'poll'>,
    private readonly beforeApply: () => Promise<void>, private readonly now = Date.now) {}
  get busy(): boolean { return [...this.records.values()].some(record => record.finished === undefined) }
  start(input: Input): { accepted: true } {
    if (this.closed) throw new Error('Obs is reloading. Reconnect and check the update state.')
    for (const [id, record] of this.records) if (record.finished !== undefined && record.finished + 900_000 < this.now()) this.records.delete(id)
    const identity = JSON.stringify([input.sourcePath, input.token]), existing = this.records.get(input.runID)
    if (existing) {
      if (existing.identity !== identity) throw new Error('This update job belongs to another request.')
      return { accepted: true }
    }
    if (this.busy || this.records.size >= 32) throw new Error('An update is already running or its recent history is full. Check its state before retrying.')
    const record: Record = { identity }
    this.records.set(input.runID, record)
    const finish = (result: LocalUpdateResult) => { record.result = result; record.finished = this.now(); delete record.progress }
    void Promise.resolve().then(async () => {
      let retained = await this.updater.poll(input.runID)
      if (retained.state !== 'expired') {
        for (;;) {
          if (retained.state === 'finished') return retained.result
          if (retained.state === 'expired') return { state: 'error' as const, text: 'The existing update outcome could not be read. Check the checkout before retrying.' }
          record.progress = retained.progress
          await new Promise(resolve => setTimeout(resolve, 100))
          retained = await this.updater.poll(input.runID)
        }
      }
      return this.updater.apply(input.token, { jobID: input.runID, beforeApply: async () => {
        if (this.closed) throw new Error('Obs unloaded before its update was dispatched.')
        await this.beforeApply()
        if (this.closed) throw new Error('Obs unloaded before its update was dispatched.')
      }, progress: text => { record.progress = text } })
    }).then(finish, () => finish({ state: 'error', text: 'The local update could not start safely. Run /obs update --check before retrying.' }))
    return { accepted: true }
  }
  async poll(input: { sourcePath: string; runID: string }): Promise<LocalUpdatePoll> {
    const record = this.records.get(input.runID)
    if (record) {
      if (JSON.parse(record.identity)[0] !== input.sourcePath) throw new Error('This update job belongs to another installation.')
      if (record.result) return { state: 'finished', result: record.result }
      return { state: 'pending', ...(record.progress ? { progress: record.progress } : {}) }
    }
    return this.updater.poll(input.runID)
  }
  close(): void { this.closed = true }
}
