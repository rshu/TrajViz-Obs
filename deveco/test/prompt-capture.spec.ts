import { expect, test } from 'bun:test'
import { registerPromptCapture } from '../src/prompt-capture.js'

type Capture = Parameters<typeof registerPromptCapture>[0]
type Observation = Parameters<Capture['record']>[0]
function fixture(enabled = true) {
  const observations: Observation[] = []
  let closed = 0
  const capture = { enabled, record(observation: Observation) { observations.push(observation) }, async close() { closed++ } }
  return { capture, observations, closed: () => closed, ...registerPromptCapture(capture) }
}
function freeze<T>(value: T): T {
  if (value && typeof value === 'object') { Object.values(value).forEach(freeze); Object.freeze(value) }
  return value
}
const input = (sessionID = 'root') => ({ sessionID, agent: 'build', messageID: 'user-message', model: { providerID: 'example', id: 'model', api: { url: 'https://secret.example/?apiKey=private' } } })
const message = (sessionID = 'root') => ({ info: { id: 'native-message', sessionID, role: 'user' }, parts: [{ type: 'text', sessionID, text: 'Inspect this project' }] })

test('disabled capture exposes no hooks, performs no work, and closes once', async () => {
  const h = fixture(false)
  expect(h.hooks).toEqual({})
  expect(h.observations).toEqual([])
  await Promise.all([h.close(), h.close()])
  expect(h.closed()).toBe(1)
})

test('captures native components without mutating or retaining live hook objects', async () => {
  const h = fixture()
  const identity = freeze(input())
  const system = freeze({ system: ['Project instructions'] })
  const messages = { messages: [message()] }
  await h.hooks['experimental.chat.system.transform']!(identity, system)
  await h.hooks['experimental.chat.messages.transform']!({}, freeze(messages))
  expect(h.observations.map(row => row.stage)).toEqual(['component.system', 'component.messages'])
  expect(h.observations[1]!.session_id).toBe('root')
  expect(h.observations[1]!.data).toMatchObject({ messages: [{ id: 'native-message', role: 'user', content: [{ text: 'Inspect this project' }] }] })
  expect(h.observations[0]!.model).toEqual({ providerID: 'example', id: 'model' })
  expect(JSON.stringify(h.observations)).not.toContain('secret.example')
  expect(h.observations[0]!.correlation).toMatchObject({ pairing: 'independent', agent_id: 'build' })
  expect(h.observations[0]!.data).toMatchObject({ provenance: { assembled_request: 'not_observed', provider_lowering: 'not_observed', later_hooks: 'not_observed' } })
  expect(h.hooks).not.toHaveProperty('http.request')
  expect(h.hooks).not.toHaveProperty('experimental.ws.send')
  await h.close()
})

test('does not guess attribution for empty, missing-ID, mixed-session, or contradictory hooks', async () => {
  const h = fixture()
  // A previous, valid session observation never supplies identity to later hooks.
  await h.hooks['experimental.chat.system.transform']!(input('previous'), { system: ['Known'] })
  await h.hooks['experimental.chat.system.transform']!({}, { system: ['Unlinked without a native session'] })
  await h.hooks['experimental.chat.messages.transform']!({}, { messages: [] })
  await h.hooks['experimental.chat.messages.transform']!({}, { messages: [{ info: { id: 'no-session' }, parts: [] }] })
  await h.hooks['experimental.chat.messages.transform']!({}, { messages: [message('one'), message('two')] })
  await h.hooks['experimental.chat.messages.transform']!(input('wrong'), { messages: [message('one')] })
  await h.hooks['experimental.chat.messages.transform']!({}, { messages: [{ ...message('one'), parts: [{ type: 'text', sessionID: 'two' }] }] })
  await h.hooks['chat.message']!(input('one'), { message: { id: 'one', sessionID: 'one', role: 'user' }, parts: [{ type: 'text', sessionID: 'two', text: 'Not session one' }] })
  expect(h.observations).toHaveLength(1)
  expect(h.observations[0]!.session_id).toBe('previous')
  await h.close()
})

test('parameters exclude headers, credentials, provider options and non-finite measurements', async () => {
  const h = fixture()
  const params = freeze({ temperature: 0.2, topP: 0.9, topK: Infinity, maxOutputTokens: 1024,
    options: { apiKey: 'private-token', headers: { authorization: 'private-token' } } })
  await h.hooks['chat.params']!(input(), params)
  expect(h.observations[0]!.data).toMatchObject({ parameters: { temperature: 0.2, topP: 0.9, maxOutputTokens: 1024 } })
  expect(JSON.stringify(h.observations)).not.toContain('private-token')
  expect(JSON.stringify(h.observations)).not.toContain('topK')
  await h.close()
})

test('compaction only observes its exposed overrides, not a fabricated default prompt', async () => {
  const h = fixture()
  await h.hooks['experimental.session.compacting']!(input(), freeze({ context: ['Keep pending work'] }))
  expect(h.observations[0]).toMatchObject({ stage: 'component.compaction', session_id: 'root', data: { context: ['Keep pending work'], default_prompt: 'not_exposed' } })
  expect(h.observations[0]!.data).not.toHaveProperty('prompt')
  await h.close()
})

test('observations keep independent session identity and snapshots across concurrent callbacks', async () => {
  const h = fixture()
  const current = { message: { id: 'user', role: 'user', sessionID: 'first' }, parts: [{ type: 'text', text: 'First content' }] }
  await h.hooks['chat.message']!(input('first'), current)
  current.parts[0]!.text = 'Later mutation'
  await h.hooks['chat.params']!(input('second'), { temperature: 0.5 })
  expect(h.observations.map(row => row.session_id)).toEqual(['first', 'second'])
  expect(JSON.stringify(h.observations[0]!.data)).toContain('First content')
  expect(new Set(h.observations.map(row => (row.correlation as any).capture_id)).size).toBe(2)
  expect(h.observations.map(row => (row.correlation as any).observation_sequence)).toEqual([1, 2])
  for (const row of h.observations) expect(row.correlation).not.toHaveProperty('step_id')
  await h.close()
  await h.hooks['chat.params']!(input('after-close'), {})
  expect(h.observations).toHaveLength(2)
})

test('capture failure and unserializable observations cannot interrupt host dispatch', async () => {
  const capture: Capture = { enabled: true, record() { throw new Error('writer failed') }, async close() { throw new Error('worker failed') } }
  const registered = registerPromptCapture(capture)
  await expect(registered.hooks['chat.params']!(input(), { temperature: 0.2 })).resolves.toBeUndefined()
  const cyclic: any = {}; cyclic.self = cyclic
  await expect(registered.hooks['experimental.chat.system.transform']!(input(), { system: [cyclic] })).resolves.toBeUndefined()
  await expect(registered.close()).resolves.toBeUndefined()
})
