import { expect, test } from 'bun:test'
import { LocalUpdateJobs } from '../src/local-update-jobs.js'
import type { LocalCheckoutUpdater, LocalUpdateResult } from '../../shared/local-update.js'

const runID = '927e1d45-c880-4778-9215-6867cf34267e'
const input = { sourcePath: '/repo/opencode', token: 'dbe5fa40-f57d-4903-bb1e-c2d45def40a3', runID }
const tick = () => new Promise(resolve => setTimeout(resolve, 0))
const result: LocalUpdateResult = { state: 'updated', text: 'Built the reviewed revision.', restartRequired: true }

function fixture() {
  let applies = 0, prepared = 0
  let finish!: (result: LocalUpdateResult) => void
  const work = new Promise<LocalUpdateResult>(resolve => { finish = resolve })
  const updater: Pick<LocalCheckoutUpdater, 'apply' | 'poll'> = {
    async apply(_token, options) { applies++; await options?.beforeApply?.(); options?.progress?.('Building…'); return work },
    async poll() { return { state: 'expired' } },
  }
  const jobs = new LocalUpdateJobs(updater, async () => { prepared++ })
  return { jobs, finish, applies: () => applies, prepared: () => prepared }
}

test('duplicate start acknowledgements dispatch once and reject changed bindings', async () => {
  const f = fixture()
  expect(f.jobs.start(input)).toEqual({ accepted: true })
  expect(f.jobs.start(input)).toEqual({ accepted: true })
  expect(() => f.jobs.start({ ...input, token: 'other' })).toThrow('another request')
  expect(() => f.jobs.start({ ...input, sourcePath: '/another' })).toThrow('another request')
  await tick()
  expect(f.applies()).toBe(1); expect(f.prepared()).toBe(1)
  expect(await f.jobs.poll(input)).toEqual({ state: 'pending', progress: 'Building…' })
  await expect(f.jobs.poll({ ...input, sourcePath: '/another' })).rejects.toThrow('another installation')
  f.finish(result); await tick()
  expect(await f.jobs.poll(input)).toEqual({ state: 'finished', result })
})

test('UI/plugin disposal does not cancel accepted file updates and durable polling survives a new instance', async () => {
  const f = fixture()
  f.jobs.start(input); await tick(); f.jobs.close()
  expect(() => f.jobs.start({ ...input, runID: crypto.randomUUID() })).toThrow('reloading')
  f.finish(result); await tick()
  expect(await f.jobs.poll(input)).toEqual({ state: 'finished', result })
  const restored = new LocalUpdateJobs({ apply: async () => { throw new Error('Must not apply on poll') }, poll: async () => ({ state: 'finished', result }) }, async () => {})
  expect(await restored.poll(input)).toEqual({ state: 'finished', result })
})

test('invalid confirmation tokens cannot quiesce dashboards and failures finish without retries', async () => {
  let stopped = 0, attempts = 0
  const jobs = new LocalUpdateJobs({
    async apply() { attempts++; return { state: 'error', text: 'The reviewed plan expired.' } },
    async poll() { return { state: 'expired' } },
  }, async () => { stopped++ })
  jobs.start(input); await tick()
  expect(stopped).toBe(0); expect(attempts).toBe(1)
  expect(await jobs.poll(input)).toEqual({ state: 'finished', result: { state: 'error', text: 'The reviewed plan expired.' } })
  jobs.start(input); await tick(); expect(attempts).toBe(1)
})

test('a retried start after server reload observes the retained worker without reapplying its consumed plan', async () => {
  let applied = 0, prepared = 0
  const jobs = new LocalUpdateJobs({
    async apply() { applied++; throw new Error('The consumed token must not be reused') },
    async poll() { return { state: 'finished', result } },
  }, async () => { prepared++ })
  expect(jobs.start(input)).toEqual({ accepted: true }); await tick()
  expect(await jobs.poll(input)).toEqual({ state: 'finished', result })
  expect(applied).toBe(0); expect(prepared).toBe(0)
})
