import { expect, test } from 'bun:test'
import { CommandJobs } from '../src/jobs.js'
import type { CommandResult, RunInput } from '../src/rpc.js'

const input = (runID = 'job'): RunInput => ({ sessionID: 'session', args: 'analyze', runID, useSynced: true })
const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve() }
const deferred = () => { let resolve!: (result: CommandResult) => void; const promise = new Promise<CommandResult>(done => { resolve = done }); return { promise, resolve } }

test('start acknowledges immediately and repeated polls never repeat long work', async () => {
  let now = 0, calls = 0
  const jobs = new CommandJobs({ now: () => now }), work = deferred(), request = input()
  expect(jobs.start(request, async () => { calls++; return work.promise })).toEqual({ accepted: true })
  expect(jobs.poll(request)).toEqual({ state: 'pending' })
  await flush(); now = 90_000
  jobs.progress(request.runID, 'Running call 1 of 1…')
  expect(jobs.poll(request)).toEqual({ state: 'pending', progress: 'Running call 1 of 1…' })
  expect(jobs.start(request, async () => { calls++; return { text: 'duplicate' } })).toEqual({ accepted: true })
  expect(calls).toBe(1)
  work.resolve({ text: 'Finished after the host header deadline.', path: '/tmp/result' }); await flush()
  expect(jobs.poll(request)).toEqual({ state: 'finished', result: { text: 'Finished after the host header deadline.', path: '/tmp/result' } })
  jobs.start(request, async () => { calls++; return { text: 'duplicate' } }); await flush()
  expect(calls).toBe(1)
})

test('job identity binds session, arguments and model without retaining mutable caller input', async () => {
  const jobs = new CommandJobs(), request = { ...input(), model: { providerID: 'configured', id: 'selected' } }
  let captured: RunInput | undefined
  jobs.start(request, async fixed => { captured = fixed; return { text: 'done' } })
  expect(jobs.poll({ ...request, sessionID: 'other' })).toEqual({ state: 'expired' })
  expect(() => jobs.start({ ...request, args: 'fixes' }, async () => ({ text: 'wrong' }))).toThrow('another request')
  request.args = 'changed'; request.model.id = 'changed'; await flush()
  expect(captured?.args).toBe('analyze'); expect(captured?.model?.id).toBe('selected')
})

test('confirmation is a separate idempotent phase and cannot overlap the initial phase', async () => {
  const jobs = new CommandJobs(), initial = input(), first = deferred(), continuation = { ...initial, confirmationID: 'confirmed' }
  jobs.start(initial, async () => first.promise)
  expect(() => jobs.start(continuation, async () => ({ text: 'premature' }))).toThrow('already running')
  first.resolve({ text: '5 model calls', confirmation: { id: 'confirmed', title: 'Run?', message: 'Uses your configured model.' } }); await flush()
  let calls = 0
  jobs.start(continuation, async () => { calls++; return { text: 'confirmed result' } }); await flush()
  jobs.start(continuation, async () => { calls++; return { text: 'duplicate' } }); await flush()
  expect(calls).toBe(1)
  expect(jobs.poll(initial).state).toBe('finished')
  expect(jobs.poll(continuation)).toEqual({ state: 'finished', result: { text: 'confirmed result' } })
})

test('cancellation prevents delayed starts, queued execution and late completion from reviving a run', async () => {
  const jobs = new CommandJobs(), waiting = deferred(); let calls = 0
  jobs.cancel('before-start')
  expect(() => jobs.start(input('before-start'), async () => { calls++; return { text: 'wrong' } })).toThrow('cancelled')
  jobs.start(input('queued'), async () => { calls++; return { text: 'wrong' } }); jobs.cancel('queued'); await flush()
  expect(calls).toBe(0)
  jobs.start(input('active'), async () => waiting.promise); await flush(); jobs.cancel('active')
  waiting.resolve({ text: 'late result', confirmation: { id: 'late', title: 'Run?', message: 'wrong' } }); await flush()
  expect(jobs.poll(input('active'))).toEqual({ state: 'expired' })
  expect(() => jobs.start({ ...input('active'), confirmationID: 'late' }, async () => ({ text: 'wrong' }))).toThrow('cancelled')
  const closing = new CommandJobs()
  closing.start(input(), async () => { calls++; return { text: 'wrong' } }); closing.close(); await flush()
  expect(calls).toBe(0)
})

test('bounded result retention allows more than a complete view walkthrough and blocks replay of evicted results', async () => {
  const jobs = new CommandJobs({ resultLimit: 2, historyLimit: 100 }); let calls = 0
  for (let index = 0; index < 64; index++) { jobs.start(input(String(index)), async () => { calls++; return { text: String(index) } }); await flush() }
  expect(calls).toBe(64)
  expect(jobs.poll(input('0'))).toEqual({ state: 'expired' })
  expect(() => jobs.start(input('0'), async () => { calls++; return { text: 'duplicate' } })).toThrow('expired')
  expect(jobs.poll(input('63'))).toEqual({ state: 'finished', result: { text: '63' } })
  expect(calls).toBe(64)
})

test('active work and retained identities are bounded without dropping live cancellation tombstones', async () => {
  const jobs = new CommandJobs({ activeLimit: 1, historyLimit: 2, retention: 100 }), work = deferred()
  jobs.start(input('active'), async () => work.promise)
  expect(() => jobs.start(input('second'), async () => ({ text: 'wrong' }))).toThrow('Too many')
  jobs.cancel('active'); jobs.cancel('other')
  expect(() => jobs.start(input('third'), async () => ({ text: 'wrong' }))).toThrow('history is full')
  expect(() => jobs.start(input('active'), async () => ({ text: 'wrong' }))).toThrow('cancelled')
  jobs.cancel('late-start')
  expect(() => jobs.start(input('late-start'), async () => ({ text: 'wrong' }))).toThrow('history is full')
  work.resolve({ text: 'late' }); await flush()
})

test('completed results expire and unload cannot publish later work', async () => {
  let now = 0
  const jobs = new CommandJobs({ now: () => now, retention: 100 }), pending = deferred()
  jobs.start(input(), async () => ({ text: 'done' })); await flush(); now = 101
  expect(jobs.poll(input())).toEqual({ state: 'expired' })
  jobs.start(input('pending'), async () => pending.promise); await flush(); jobs.close()
  pending.resolve({ text: 'late' }); await flush()
  expect(jobs.poll(input('pending'))).toEqual({ state: 'expired' })
  expect(() => jobs.start(input('new'), async () => ({ text: 'wrong' }))).toThrow('reloading')
})

test('unexpected worker rejection becomes a final result and progress remains bounded', async () => {
  const jobs = new CommandJobs(), pending = deferred()
  jobs.start(input(), async () => pending.promise); jobs.progress('job', 'x'.repeat(70_000))
  const state = jobs.poll(input()); expect(state.state === 'pending' && state.progress?.length).toBe(64_000)
  pending.resolve({ text: 'done' }); await flush()
  jobs.start(input('error'), async () => { throw new Error('worker failed') }); await flush()
  expect(jobs.poll(input('error'))).toEqual({ state: 'finished', result: { text: 'obs: worker failed' } })
})

test('poll renews the active lease but progress cannot keep abandoned paid work alive', async () => {
  let now = 0
  const cancelled: string[] = [], pending = deferred()
  const jobs = new CommandJobs({ now: () => now, lease: 100, cancelWork: id => cancelled.push(id) })
  jobs.start(input(), async () => pending.promise); await flush(); now = 90
  expect(jobs.poll(input()).state).toBe('pending')
  now = 150; jobs.progress('job', 'Still thinking'); expect(cancelled).toEqual([])
  now = 191; expect(jobs.poll(input())).toEqual({ state: 'expired' }); expect(cancelled).toEqual(['job'])
  expect(() => jobs.start(input(), async () => ({ text: 'replayed' }))).toThrow('cancelled')
  pending.resolve({ text: 'too late' }); await flush(); jobs.close()
})

test('an actual lease timer cancels unseen work and unload clears the timer', async () => {
  const cancelled: string[] = [], pending = deferred()
  const jobs = new CommandJobs({ lease: 5, cancelWork: id => cancelled.push(id) })
  jobs.start(input(), async () => pending.promise); await new Promise(resolve => setTimeout(resolve, 20))
  expect(cancelled).toEqual(['job']); expect(jobs.poll(input())).toEqual({ state: 'expired' })
  const closed = new CommandJobs({ lease: 5, cancelWork: id => cancelled.push(id) })
  closed.start(input('closed'), async () => pending.promise); await flush(); closed.close()
  await new Promise(resolve => setTimeout(resolve, 20)); expect(cancelled).toEqual(['job'])
  pending.resolve({ text: 'done' }); await flush(); jobs.close()
})

test('retained payload bytes are bounded and the most recently finished result survives', async () => {
  let now = 0
  const jobs = new CommandJobs({ resultBytes: 150, now: () => now }), slow = deferred()
  jobs.start(input('slow'), async () => slow.promise)
  jobs.start(input('fast'), async () => ({ text: 'f'.repeat(80) })); await flush(); now = 10
  slow.resolve({ text: 's'.repeat(80) }); await flush()
  expect(jobs.poll(input('fast'))).toEqual({ state: 'expired' })
  expect(jobs.poll(input('slow'))).toEqual({ state: 'finished', result: { text: 's'.repeat(80) } })
  jobs.start(input('large'), async () => ({ text: 'x'.repeat(1000), path: '/tmp/full-view.json' })); await flush()
  const result = jobs.poll(input('large'))
  expect(result.state === 'finished' && result.result.text).toContain('exceeds retained size')
  expect(result.state === 'finished' && result.result.path).toBe('/tmp/full-view.json')
  jobs.close()
})

test('completion order preserves the newest result with equal or backwards wall-clock timestamps', async () => {
  for (const finishTime of [0, -1]) {
    let now = 0
    const jobs = new CommandJobs({ now: () => now, resultLimit: 1 }), slow = deferred()
    jobs.start(input('slow'), async () => slow.promise)
    jobs.start(input('fast'), async () => ({ text: 'fast' })); await flush(); now = finishTime
    slow.resolve({ text: 'newest completion' }); await flush()
    expect(jobs.poll(input('fast'))).toEqual({ state: 'expired' })
    expect(jobs.poll(input('slow'))).toEqual({ state: 'finished', result: { text: 'newest completion' } })
    jobs.close()
  }
})
