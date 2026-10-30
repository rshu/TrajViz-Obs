import { expect, spyOn, test } from 'bun:test'
import plugin from '../src/server.js'
import { Backend } from '../src/backend.js'

test('headless discovery cannot forward a local service password to a non-loopback URL', async () => {
  for (const serverUrl of ['https://example.com', 'file:///tmp/service', 'http://localhost.evil.test:4096', 'http://user:secret@127.0.0.1:4096', 'http://127.0.0.1:4096/?token=value']) {
    const ctx = {options:{serverUrl}} as unknown as Parameters<typeof plugin.setup>[0]
    await expect(plugin.setup(ctx)).rejects.toThrow('loopback HTTP(S)')
  }
})

test('unload cancels an outstanding model-tool history read', async () => {
  let execute!: (input: {what:'brief'}, call: {sessionID:string;signal:AbortSignal}) => Promise<unknown>
  let started!: () => void
  const reading = new Promise<void>(resolve => { started = resolve })
  let captureSignal: AbortSignal | undefined
  const session = spyOn(Backend.prototype, 'session').mockImplementation(async (_id, _selector, signal) => {
    captureSignal = signal
    started()
    await new Promise<void>((resolve, reject) => {
      if (signal?.aborted) reject(signal.reason)
      else signal?.addEventListener('abort', () => reject(signal.reason), {once:true})
    })
    throw new Error('Unreachable')
  })
  const ctx = {
    options: {}, location: {directory:'/tmp'},
    rpc: {register: async () => ({events:{emit:async () => {}},dispose:async () => {}})},
    tool: {transform: async (edit: (editor:{add:(value:{execute:typeof execute}) => void}) => void) => {
      edit({add:value => { execute = value.execute }})
      return {dispose:async () => {}}
    }},
    event: {subscribe: async function* ({signal}:{signal:AbortSignal}) {
      if (!signal.aborted) await new Promise<void>(resolve => signal.addEventListener('abort', () => resolve(), {once:true}))
    }},
  } as unknown as Parameters<typeof plugin.setup>[0]
  const controller = new AbortController()
  const cleanup = await plugin.setup(ctx)
  try {
    const result = execute({what:'brief'}, {sessionID:'test',signal:controller.signal})
    await reading
    const closing = cleanup!()
    expect(captureSignal?.aborted).toBe(true)
    await closing
    expect(await result).toHaveProperty('content')
    expect(controller.signal.aborted).toBe(false)
  } finally { controller.abort(); await cleanup!(); session.mockRestore() }
})
