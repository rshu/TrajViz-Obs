import { expect, mock, test } from 'claude-code/testing'
import type { EngineInterface, On, TurnStepInput, TurnStepResult } from 'claude-code'
import { capturePromptObservation, createPromptCapture, finishPromptCapture } from './lib/prompt-capture'

const PYTHON = '/cache/trajviz-obs/envs/0123456789abcdef01234567/0123456789abcdef0123456789abcdef/bin/python'
const STATE = JSON.stringify({ revision: 6, python: PYTHON, trajviz: '0.5.1', extras: ['llm', 'charts', 'zstd'], source: 'agent-trajviz==0.5.1', createdAt: 1 })
const PRIVATE = 'PRIVATE-SYNTHETIC-PROMPT'
const DONE = { exitCode: 0, stdout: '{"saved":true}', stderr: '', isStdoutTruncated: false, isStderrTruncated: false }
const STEP: TurnStepInput = { turnId: 'turn-one', index: 0, model: 'test-model', messageCount: 1 }
const RESULT: TurnStepResult = { turnId: STEP.turnId, index: STEP.index, answer: 'unchanged', toolUses: [], stopReason: 'end_turn', usage: null }
type Saved = { stage: string; data: Record<string, unknown>; correlation?: Record<string, unknown>; model?: unknown; session_id: string; instance_id: string; record_id: string; sequence: number; capture_health?: { dropped: number; failed: number } }
type World = { flag?: string | null; root?: string | null; settingsEnv?: Record<string, string>; noPython?: boolean; writeFails?: boolean; contextFails?: boolean; agentDenied?: boolean }

async function consume<C, R>(stream: AsyncGenerator<C, R>): Promise<{ chunks: C[]; result: R }> {
  const chunks: C[] = []
  for (;;) {
    const item = await stream.next()
    if (item.done) return { chunks, result: item.value }
    chunks.push(item.value)
  }
}

function host(on: On, world: World = {}) {
  const clock = mock.clock(on)
  const writes: { argv: readonly string[]; stdin: string; env?: Record<string, string>; timeoutMs?: number }[] = []
  const logs: string[] = []
  const requested: (string | undefined)[] = []
  let sessionId = 'synthetic-session'
  const flags: Record<string, string | undefined> = { XDG_CACHE_HOME: '/cache', TRAJVIZ_OBS_TELEMETRY: '0',
    TRAJVIZ_OBS_PROMPT_CAPTURE: world.flag === null ? undefined : world.flag ?? '1',
    TRAJVIZ_OBS_PROMPT_CAPTURE_DIR: world.root === null ? undefined : world.root ?? '/private/capture' }
  on('env.get', (_, e) => ({ value: flags[e.name] }))
  mock.store(on)
  on('settings.read', () => ({ value: world.settingsEnv ? { env: world.settingsEnv } : {} }))
  on('session.id', () => ({ value: sessionId }))
  on('session.cwd', () => ({ value: '/synthetic-project' }))
  on('session.messages', (_, e) => {
    requested.push(e.agentId)
    if (world.contextFails) throw new Error(PRIVATE)
    if (world.agentDenied && e.agentId) return { value: { deny: PRIVATE } }
    return { value: [{ role: 'user', content: [{ type: 'text', text: e.agentId ? 'AGENT-' + PRIVATE : PRIVATE }] }] }
  })
  on('fs.exists', (_, e) => ({ value: !world.noPython && e.path === PYTHON }))
  on('fs.read', (_, e) => e.path === '/cache/trajviz-obs/state.json' && !world.noPython ? { value: STATE } : { deny: 'missing' })
  on('process.run', (_, e) => {
    if (!e.argv[2]?.endsWith('/bridge/prompt_capture.py')) return { value: { ...DONE, exitCode: 1, stdout: '' } }
    writes.push({ argv: e.argv, stdin: e.init?.stdin ?? '', env: e.init?.env, timeoutMs: e.init?.timeoutMs })
    if (world.writeFails) throw new Error(PRIVATE)
    return { value: DONE }
  })
  on('ui.log', (_, e) => { logs.push(e.text); return { value: undefined } })
  return { clock, writes, logs, requested, setSessionId: (value: string) => { sessionId = value }, records: () => writes.map(write => JSON.parse(write.stdin) as Saved) }
}

for (const flag of [null, '', '0', 'true', ' 1', '1 ']) {
  test(`prompt capture stays disabled without the exact launch opt-in ${JSON.stringify(flag)}`, async ($, on) => {
    const world = host(on, { flag })
    on('turn.step', async function* () { yield { kind: 'text', index: 0, text: PRIVATE }; return RESULT })
    const stream = $.turn.step(STEP)
    expect(await consume(stream)).toEqual({ result: RESULT, chunks: [{ kind: 'text', index: 0, text: PRIVATE }] })
    await world.clock.settle()
    expect(world.requested).toEqual([])
    expect(world.writes).toEqual([])
    expect(world.logs).toEqual([])
  })
}

for (const root of [null, '', 'relative/capture', '~/.capture', '//server/capture', '\\\\server\\capture', '/private/\0capture']) {
  test(`prompt capture refuses an absent or unsafe destination ${JSON.stringify(root)}`, async ($, on) => {
    const world = host(on, { root })
    on('prompt.section', (_, e) => ({ text: e.text }))
    expect(await $.prompt.section({ name: 'memory', text: PRIVATE })).toEqual({ text: PRIVATE })
    await world.clock.settle()
    expect(world.requested).toEqual([])
    expect(world.writes).toEqual([])
  })
}

for (const key of ['TRAJVIZ_OBS_PROMPT_CAPTURE', 'TRAJVIZ_OBS_PROMPT_CAPTURE_DIR', 'trajviz_obs_prompt_capture']) {
  test(`settings.env cannot opt in to or redirect prompt capture through ${key}`, async ($, on) => {
    const world = host(on, { settingsEnv: { [key]: '1' } })
    on('prompt.section', (_, e) => ({ text: e.text }))
    expect(await $.prompt.section({ name: 'memory', text: PRIVATE })).toEqual({ text: PRIVATE })
    await world.clock.settle()
    expect(world.writes).toEqual([])
  })
}

test('component hooks retain observed outputs and provenance without rewriting or inventing request links', async ($, on) => {
  const world = host(on)
  on('prompt.compose', () => ({ sections: [{ id: 'memory', text: PRIVATE, scope: 'session' }] }))
  on('prompt.section', (_, e) => ({ text: e.text }))
  on('prompt.context', (_, e) => ({ blocks: e.blocks, instructionFiles: e.instructionFiles }))
  on('prompt.attachment', (_, e) => ({ text: e.text }))
  on('skill.prompt', (_, e) => ({ text: e.text }))
  on('tool.describe', (_, e) => ({ description: e.description, isDeferred: e.isDeferred }))
  expect(await $.prompt.compose({ model: 'test-model', promptModel: 'test-model', surfaces: [], tools: [], outputStyle: null, traits: [] }))
    .toEqual({ sections: [{ id: 'memory', text: PRIVATE, scope: 'session' }] })
  expect(await $.prompt.section({ name: 'memory', text: PRIVATE })).toEqual({ text: PRIVATE })
  const context = { blocks: [{ name: 'claudeMd', text: PRIVATE }], instructionFiles: [{ path: '/synthetic/CLAUDE.md', kind: 'project' as const, content: PRIVATE }] }
  expect(await $.prompt.context(context)).toEqual(context)
  expect(await $.prompt.attachment({ type: 'instructions', text: PRIVATE, origin: { kind: 'engine' }, agentId: 'child-one' })).toEqual({ text: PRIVATE })
  expect(await $.skill.prompt({ skill: 'synthetic', text: PRIVATE, source: 'projectSettings', path: '/synthetic/SKILL.md', frontmatter: {} })).toEqual({ text: PRIVATE })
  expect(await $.tool.describe({ tool: 'mcp__synthetic__lookup', description: PRIVATE, isDeferred: true, provider: { plugin: 'mcp:synthetic', tier: 'user' } }))
    .toEqual({ description: PRIVATE, isDeferred: true })
  await world.clock.settle()
  const records = world.records()
  expect(records.map(record => record.stage)).toEqual(['component.compose', 'component.section', 'component.context', 'component.attachment', 'component.skill', 'component.tool'])
  for (const record of records) {
    expect(record.session_id).toBe('synthetic-session')
    expect(record.correlation?.request_id).toBeUndefined()
    expect(JSON.stringify(record.data)).toContain(PRIVATE)
  }
  expect(records[2]?.data.output).toEqual(context)
  expect(records[3]?.correlation).toEqual({ agent_id: 'child-one', scope: 'agent' })
  expect(new Set(records.map(record => record.record_id)).size).toBe(6)
  expect(new Set(records.map(record => record.instance_id)).size).toBe(1)
  expect(records.map(record => record.sequence)).toEqual([1, 2, 3, 4, 5, 6])
  for (const write of world.writes) {
    expect(write.argv.slice(-3)).toEqual(['write', '--root', '/private/capture'])
    expect(write.env).toEqual({ TRAJVIZ_OBS_PROMPT_CAPTURE: '1' })
    expect(write.timeoutMs).toBe(2_000)
    expect(JSON.stringify(write.argv)).not.toContain(PRIVATE)
  }
})

test('each request snapshots normalized main or agent messages and passes streaming results unchanged', async ($, on) => {
  const world = host(on)
  on('turn.step', async function* (_, e) { yield { kind: 'text', index: 0, text: PRIVATE }; return { ...RESULT, turnId: e.turnId, index: e.index } })
  for (const step of [STEP, { ...STEP, turnId: 'child-turn', index: 2, agentId: 'child-one' }]) {
    const stream = $.turn.step(step)
    expect(await consume(stream)).toEqual({ chunks: [{ kind: 'text', index: 0, text: PRIVATE }], result: { ...RESULT, turnId: step.turnId, index: step.index } })
  }
  await world.clock.settle()
  expect(world.requested).toEqual([undefined, 'child-one'])
  const records = world.records()
  expect(records.map(record => record.stage)).toEqual(['request.context', 'request.complete', 'request.context', 'request.complete'])
  expect(records[0]?.data).toEqual({ messages: [{ role: 'user', content: [{ type: 'text', text: PRIVATE }] }], available: true,
    source: 'session.messages.api', message_count: 1, normalized: true,
    provenance: { hook: 'turn.step', boundary: 'engine_normalized_before_provider_serialization', later_handlers_may_modify: true,
      observed_message_count: 1, message_limit: 4096, message_limit_reached: false },
    omissions: ['system_prompt', 'tool_schemas', 'provider_transformations'] })
  expect(records[2]?.correlation).toEqual({ request_id: 'child-turn:2', turn_id: 'child-turn', step_index: 2, agent_id: 'child-one', scope: 'agent' })
  expect(records[3]?.correlation).toEqual(records[2]?.correlation)
  expect(records[3]?.data).toEqual({ usage: null, stop_reason: 'end_turn', outcome: 'success' })
})

test('denied agent context is recorded as unavailable without falling back to the main conversation', async ($, on) => {
  const world = host(on, { agentDenied: true })
  on('turn.step', async function* (_, e) { return { ...RESULT, turnId: e.turnId } })
  const stream = $.turn.step({ ...STEP, agentId: 'denied-child' })
  for await (const _chunk of stream) { /* No output. */ }
  await world.clock.settle()
  expect(world.requested).toEqual(['denied-child'])
  expect(world.records()[0]?.data.available).toBe(false)
  expect(world.records()[0]?.data.messages).toBeNull()
  expect(world.records()[0]?.data.omitted_reason).toBe('agent_context_unavailable')
  expect(JSON.stringify(world.records())).not.toContain(PRIVATE)
})

for (const reason of ['clear', 'resume'] as const) {
  test(`capture follows the new session identity after ${reason} without a new session.start`, async ($, on) => {
    const world = host(on)
    on('prompt.section', (_, e) => ({ text: e.text }))
    on('session.end', (_, e) => {
      world.setSessionId('next-synthetic-session')
      return { sessionId: e.sessionId }
    })
    await $.prompt.section({ name: 'memory', text: 'before' })
    await world.clock.settle()
    await $.session.end({ reason, sessionId: 'synthetic-session', resume: { id: 'synthetic-session' } })
    await $.prompt.section({ name: 'memory', text: 'after' })
    await world.clock.settle()
    expect(world.records().map(record => record.session_id)).toEqual(['synthetic-session', 'next-synthetic-session'])
    expect(world.records()[0]?.instance_id === world.records()[1]?.instance_id).toBe(false)
  })
}

for (const failure of [{ noPython: true }, { writeFails: true }, { contextFails: true }]) {
  test(`capture failures never change the model stream or expose diagnostics ${JSON.stringify(failure)}`, async ($, on) => {
    const world = host(on, failure)
    on('turn.step', async function* () { yield { kind: 'text', index: 0, text: 'OK' }; return RESULT })
    const stream = $.turn.step(STEP)
    expect(await consume(stream)).toEqual({ result: RESULT, chunks: [{ kind: 'text', index: 0, text: 'OK' }] })
    await world.clock.settle()
    expect(world.logs.join('')).not.toContain(PRIVATE)
    expect(world.logs.length).toBeLessThanOrEqual(1)
    if (failure.noPython) expect(world.writes).toEqual([])
    if (failure.contextFails) expect(world.records()[0]?.stage).toBe('capture.gap')
  })
}

test('a model stream failure is still rejected and records only a bounded error outcome', async ($, on) => {
  const world = host(on)
  const failure = new Error(PRIVATE)
  on('turn.step', async function* () { throw failure })
  const stream = $.turn.step(STEP)
  let caught: unknown
  try { for await (const _chunk of stream) { /* No output. */ } } catch (error) { caught = error }
  expect(caught instanceof Error).toBe(true)
  await world.clock.settle()
  expect(world.records().at(-1)?.data).toEqual({ outcome: 'error' })
  expect(world.logs.join('')).not.toContain(PRIVATE)
})

test('closing a model stream preserves its cancellation and records the matching request', async ($, on) => {
  const world = host(on)
  let closed = false
  on('turn.step', async function* () {
    try { yield { kind: 'text', index: 0, text: 'first' }; yield { kind: 'text', index: 0, text: 'second' }; return RESULT }
    finally { closed = true }
  })
  const stream = $.turn.step(STEP)
  expect(await stream.next()).toEqual({ done: false, value: { kind: 'text', index: 0, text: 'first' } })
  await stream.return(RESULT)
  await world.clock.settle()
  expect(closed).toBe(true)
  expect(world.records().at(-1)?.data).toEqual({ outcome: 'cancelled' })
  expect(world.records().at(-1)?.correlation?.request_id).toBe('turn-one:0')
})

function localSender(wait?: Promise<void>, fails?: (attempt: number) => boolean, waitAt = 1) {
  const state = createPromptCapture({})
  const bodies: string[] = []
  const logs: string[] = []
  let expire: (() => void) | undefined
  let concurrent = 0
  let maximum = 0
  let started = () => {}
  const begun = new Promise<void>(resolve => { started = resolve })
  const $ = {
    plugin: { root: '/plugin' },
    env: { get: async (name: string) => ({ TRAJVIZ_OBS_PROMPT_CAPTURE: '1', TRAJVIZ_OBS_PROMPT_CAPTURE_DIR: '/private/capture', XDG_CACHE_HOME: '/cache' })[name] },
    settings: { read: async () => ({}) }, session: { id: async () => 'synthetic-session' },
    state: { get: async () => ({ value: undefined, version: 0 }) },
    fs: { read: async () => STATE, exists: async () => true },
    process: { run: async (_argv: unknown, init: { stdin: string }) => {
      concurrent++
      maximum = Math.max(maximum, concurrent)
      bodies.push(init.stdin)
      if (bodies.length === 1) started()
      const failed = fails?.(bodies.length)
      if (bodies.length === waitAt) await wait
      concurrent--
      return failed ? { ...DONE, exitCode: 1 } : DONE
    } },
    ui: { log: async (message: string) => { logs.push(message) } },
    clock: { after: (_milliseconds: number, callback: () => void) => { expire = callback; return { cancel: () => {} } } },
  } as unknown as EngineInterface
  return { $, state, bodies, logs, started: begun, maximum: () => maximum, expire: () => expire?.() }
}

test('bounded local capture clones data before later edits and replaces oversized records with a gap', async () => {
  const { $, state, bodies } = localSender()
  const data = { text: PRIVATE }
  await capturePromptObservation($, state, { stage: 'component.section', data })
  data.text = 'changed-later'
  await state.active
  await capturePromptObservation($, state, { stage: 'request.context', data: { text: 'x'.repeat(2 * 1024 * 1024) } })
  await state.active
  expect((JSON.parse(bodies[0]!) as Saved).data).toEqual({ text: PRIVATE })
  expect((JSON.parse(bodies[1]!) as Saved).stage).toBe('capture.gap')
  expect((JSON.parse(bodies[1]!) as Saved).data.reason).toBe('record_too_large')
  expect(bodies[1]!.length).toBeLessThan(1_000)
  expect(state.queuedBytes).toBe(0)
})

test('a saturated capture queue remains bounded, serializes writes, and reports omitted records', async () => {
  let release = () => {}
  const gate = new Promise<void>(resolve => { release = resolve })
  const sender = localSender(gate)
  for (let index = 0; index < 132; index++) await capturePromptObservation(sender.$, sender.state, { stage: 'component.section', data: { index } })
  expect(sender.state.queue.length).toBeLessThanOrEqual(127)
  expect(sender.state.queuedBytes).toBeLessThanOrEqual(8 * 1024 * 1024)
  expect(sender.logs).toHaveLength(1)
  release()
  await sender.state.active
  expect(sender.maximum()).toBe(1)
  expect(sender.bodies).toHaveLength(129)
  expect((JSON.parse(sender.bodies.at(-1)!) as Saved).data).toEqual({ reason: 'queue_full', omitted_records: 4 })
  expect(sender.state.queue).toEqual([])
  expect(sender.state.queuedBytes).toBe(0)
})

test('a normal startup burst keeps component, request and completion observations without gaps', async () => {
  let release = () => {}
  const gate = new Promise<void>(resolve => { release = resolve })
  const sender = localSender(gate)
  for (let index = 0; index < 60; index++) await capturePromptObservation(sender.$, sender.state, { stage: 'component.section', data: { index } })
  const correlation = { request_id: 'startup-request' }
  await capturePromptObservation(sender.$, sender.state, { stage: 'request.context', correlation, data: { messages: [] } })
  await capturePromptObservation(sender.$, sender.state, { stage: 'request.complete', correlation, data: { outcome: 'success' } })
  release()
  await sender.state.active
  const records = sender.bodies.map(body => JSON.parse(body) as Saved)
  expect(records).toHaveLength(62)
  expect(records.filter(record => record.stage.startsWith('capture.'))).toEqual([])
  expect(records.slice(-2).map(record => record.stage)).toEqual(['request.context', 'request.complete'])
  expect(sender.state.totalDropped).toBe(0)
  expect(sender.logs).toEqual([])
})

test('the queue byte bound counts multibyte content and the active writer', async () => {
  let release = () => {}
  const gate = new Promise<void>(resolve => { release = resolve })
  const sender = localSender(gate)
  const text = 'é'.repeat(600_000)
  for (let index = 0; index < 7; index++) await capturePromptObservation(sender.$, sender.state, { stage: 'component.section', data: { text, index } })
  expect(sender.state.queuedBytes).toBeLessThanOrEqual(8 * 1024 * 1024)
  expect(sender.state.totalDropped).toBe(1)
  release()
  await sender.state.active
  expect(sender.bodies).toHaveLength(7)
  expect((JSON.parse(sender.bodies.at(-1)!) as Saved).data).toEqual({ reason: 'queue_full', omitted_records: 1 })
})

test('a transient writer failure gets a content-free health record and persists in later metadata', async () => {
  const sender = localSender(undefined, attempt => attempt === 1)
  await capturePromptObservation(sender.$, sender.state, { stage: 'component.section', data: { text: PRIVATE } })
  await sender.state.active
  let records = sender.bodies.map(body => JSON.parse(body) as Saved)
  expect(records).toHaveLength(2)
  expect(records[1]?.stage).toBe('capture.status')
  expect(records[1]?.data).toEqual({ dropped: 0, failed: 1 })
  expect(JSON.stringify(records[1])).not.toContain(PRIVATE)
  await capturePromptObservation(sender.$, sender.state, { stage: 'request.complete', data: { outcome: 'success' } })
  await sender.state.active
  records = sender.bodies.map(body => JSON.parse(body) as Saved)
  expect(records).toHaveLength(3)
  expect(records[2]?.capture_health).toEqual({ dropped: 0, failed: 1 })
  expect(sender.state.reportedFailed).toBe(1)
  expect(sender.logs).toHaveLength(1)
})

test('a permanently failing writer attempts health once without generating recursive records', async () => {
  const sender = localSender(undefined, () => true)
  await capturePromptObservation(sender.$, sender.state, { stage: 'component.section', data: { text: PRIVATE } })
  await sender.state.active
  expect(sender.bodies).toHaveLength(2)
  expect((JSON.parse(sender.bodies[1]!) as Saved).stage).toBe('capture.status')
  expect(sender.state.failed).toBe(1)
  expect(sender.state.reportedFailed).toBe(0)
  expect(sender.state.queue).toEqual([])
  expect(sender.state.queuedBytes).toBe(0)
  expect(sender.logs).toHaveLength(1)
})

test('new observations arriving during health publication are retained', async () => {
  let release = () => {}
  let healthStarted = () => {}
  const gate = new Promise<void>(resolve => { release = resolve })
  const ready = new Promise<void>(resolve => { healthStarted = resolve })
  const sender = localSender(gate, attempt => { if (attempt === 2) healthStarted(); return attempt === 1 }, 2)
  await capturePromptObservation(sender.$, sender.state, { stage: 'component.section', data: { text: PRIVATE } })
  await ready
  await capturePromptObservation(sender.$, sender.state, { stage: 'request.complete', data: { outcome: 'success' } })
  release()
  await sender.state.active
  expect(sender.bodies.map(body => (JSON.parse(body) as Saved).stage)).toEqual(['component.section', 'capture.status', 'request.complete'])
  expect(sender.state.queue).toEqual([])
  expect(sender.state.queuedBytes).toBe(0)
})

test('shutdown has a bounded grace period and drops pending data when it expires', async () => {
  let release = () => {}
  const gate = new Promise<void>(resolve => { release = resolve })
  const sender = localSender(gate)
  await capturePromptObservation(sender.$, sender.state, { stage: 'component.section', data: { text: PRIVATE } })
  await sender.started
  await capturePromptObservation(sender.$, sender.state, { stage: 'component.section', data: { text: PRIVATE } })
  const finish = finishPromptCapture(sender.$, sender.state)
  sender.expire()
  await finish
  expect(sender.state.stopped).toBe(true)
  expect(sender.state.queue).toEqual([])
  await capturePromptObservation(sender.$, sender.state, { stage: 'component.section', data: { text: PRIVATE } })
  release()
  await sender.state.active
  expect(sender.bodies).toHaveLength(1)
  expect(sender.state.queuedBytes).toBe(0)
})
