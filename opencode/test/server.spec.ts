import { expect, spyOn, test } from 'bun:test'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import plugin from '../src/server.js'
import { Backend } from '../src/backend.js'
import { Runtime } from '../src/runtime.js'
import { Obs, type CommandResult, type JobPoll, type RunInput, type UsageInteraction } from '../src/rpc.js'
import * as telemetry from '../src/telemetry.js'

test('the actual model tool requests fresh Desktop history through authenticated RPC without a service URL', async () => {
  const directory = await mkdtemp(path.join(tmpdir(),'trajviz-desktop-tool-'))
  const source = {source_format:'opencode_v2',schema_version:1,directory,session:{id:'ses_desktop',location:{directory}},
    messages:[{id:'before_compaction',type:'user',text:'Original task'},{id:'new',type:'assistant',text:'Fresh work'}],children:[]}
  let execute!: (input:{what:'brief'},call:{sessionID:string;signal:AbortSignal})=>Promise<{content:string}>
  let handlers!: {sync:(input:{source:unknown;requestID:string})=>Promise<unknown>}
  const events: string[] = []
  const connection = spyOn(Backend.prototype as unknown as {connection:()=>Promise<undefined>},'connection').mockResolvedValue(undefined)
  const run = spyOn(Runtime.prototype,'run').mockImplementation(async <T>(argv:readonly string[]) => {
    expect(argv[0]).toBe('brief')
    const captured = JSON.parse(await readFile(argv[argv.indexOf('--transcript')+1]!,'utf8'))
    expect(captured.messages.map((message:{id:string})=>message.id)).toEqual(['before_compaction','new'])
    return {brief:'Current Desktop history'} as T
  })
  const ctx = {
    options:{cacheRoot:path.join(directory,'cache')},location:{directory},app:{version:'2.2.5'},
    session:{get:async()=>source.session},model:{list:async()=>({data:[]})},
    rpc:{register:async (_definition:unknown,input:typeof handlers) => {
      handlers=input
      return {events:{emit:async (name:string,value:{requestID:string;sessionID:string}) => {
        events.push(name)
        if(name==='capture') await handlers.sync({source,requestID:value.requestID})
      }},dispose:async()=>{}}
    }},
    tool:{transform:async (edit:(editor:{add:(value:{execute:typeof execute})=>void})=>void) => {
      edit({add:value=>{execute=value.execute}});return {dispose:async()=>{}}
    }},
    event:{subscribe:async function*({signal}:{signal:AbortSignal}) {
      if(!signal.aborted) await new Promise<void>(resolve=>signal.addEventListener('abort',()=>resolve(),{once:true}))
    }},
  } as unknown as Parameters<typeof plugin.setup>[0]
  let cleanup:Awaited<ReturnType<typeof plugin.setup>>
  try {
    cleanup=await plugin.setup(ctx)
    expect(await execute({what:'brief'},{sessionID:'ses_desktop',signal:new AbortController().signal})).toEqual({content:'Current Desktop history'})
    expect(events).toEqual(['capture'])
  } finally { await cleanup!?.();connection.mockRestore();run.mockRestore();await rm(directory,{recursive:true,force:true}) }
})

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

test('usage counts completed terminal, Desktop, and model-tool actions without poll or confirmation duplicates', async () => {
  const directory = await mkdtemp(path.join(tmpdir(), 'trajviz-opencode-usage-'))
  const transcript = path.join(directory, 'private-transcript.json')
  const sessionID = 'private-session'
  const events: Parameters<ReturnType<typeof telemetry.createUsageReporter>>[0][] = []
  const report = spyOn(telemetry, 'createUsageReporter').mockReturnValue(Object.assign(
    (event: typeof events[number]) => { events.push(event) }, { enabled: () => true, flush: async () => {} },
  ))
  const session = spyOn(Backend.prototype, 'session').mockResolvedValue({
    sessionID, transcript, artifacts: directory, argv: ['--transcript', transcript],
  })
  const snapshot = spyOn(Backend.prototype, 'snapshot').mockResolvedValue({ schema_version: 1, empty: true, reason: 'No history yet.', transcript: { path: transcript } })
  const run = spyOn(Runtime.prototype, 'run').mockImplementation(async <T>(argv: readonly string[]) => {
    if (argv[0] === 'llm-prompt') return { feature: 'label', hash: 'test', plan: { calls: 5, approx_input_tokens: 120 } } as T
    if (argv[0] === 'label') return { reply: 'private model reply' } as T
    if (argv[0] === 'dashboard') return { path: argv.at(-1) } as T
    if (argv[0] === 'brief') return { brief: 'private tool output' } as T
    throw new Error('private runtime error')
  })
  const call = { signal: new AbortController().signal }
  let handlers!: {
    run: (input: RunInput, call: { signal: AbortSignal }) => Promise<CommandResult>
    start: (input: RunInput, call: { signal: AbortSignal }) => Promise<{ accepted: true }>
    poll: (input: RunInput) => Promise<JobPoll>
    cancel: (input: Pick<RunInput, 'runID'>) => Promise<{ cancelled: boolean }>
    usageStatus: (input: object) => Promise<{ enabled: boolean }>
    usageInteraction: (input: UsageInteraction) => Promise<object>
  }
  let execute!: (input: { what: 'brief' }, call: { sessionID: string; signal: AbortSignal }) => Promise<unknown>
  const ctx = {
    options: { llm: 'env', cacheRoot: path.join(directory, 'cache') }, location: { directory },
    rpc: { register: async (_definition: unknown, input: typeof handlers) => {
      handlers = input
      return { events: { emit: async () => {} }, dispose: async () => {} }
    } },
    tool: { transform: async (edit: (editor: { add: (value: { execute: typeof execute }) => void }) => void) => {
      edit({ add: value => { execute = value.execute } })
      return { dispose: async () => {} }
    } },
    event: { subscribe: async function* ({ signal }: { signal: AbortSignal }) {
      if (!signal.aborted) await new Promise<void>(resolve => signal.addEventListener('abort', () => resolve(), { once: true }))
    } },
  } as unknown as Parameters<typeof plugin.setup>[0]
  async function desktop(input: RunInput): Promise<CommandResult> {
    expect(await handlers.start(input, call)).toEqual({ accepted: true })
    for (let attempt = 0; attempt < 100; attempt++) {
      const job = await handlers.poll(input)
      if (job.state === 'finished') {
        expect(await handlers.poll(input)).toEqual(job)
        return job.result
      }
      if (job.state === 'expired') break
      await Bun.sleep(5)
    }
    throw new Error('Desktop job did not finish')
  }
  let cleanup: Awaited<ReturnType<typeof plugin.setup>> | undefined
  try {
    await writeFile(transcript, JSON.stringify({ messages: [{ role: 'user', text: 'private transcript content' }] }))
    cleanup = await plugin.setup(ctx)
    await handlers.run({ sessionID, args: 'help', runID: 'help' }, call)
    await handlers.run({ sessionID, args: 'help unexpected', runID: 'bad-help' }, call)
    await handlers.run({ sessionID, args: '', runID: 'empty-overview' }, call)
    expect(await handlers.usageStatus({})).toEqual({ enabled: true })
    const pane = { feature: 'pane', surface: 'desktop', outcome: 'success', durationMs: 5 } as const
    const schema = Obs.methods.usageInteraction.input
    expect(schema.safeParse({ ...pane, sessionID }).success).toBe(false)
    expect(schema.safeParse({ ...pane, feature: 'private-tab' }).success).toBe(false)
    expect(await handlers.usageInteraction(schema.parse(pane))).toEqual({})
    await desktop({ sessionID, args: 'overview', runID: 'pane-internal-overview', paneSnapshot: true })
    expect((await handlers.run({ sessionID, args: 'doctor', runID: 'doctor' }, call)).text).toContain('private runtime error')
    const dashboard = { sessionID, args: 'dashboard --html "/private/dashboard.html"', runID: 'dashboard' }
    expect((await desktop(dashboard)).path).toBe('/private/dashboard.html')
    expect((await desktop(dashboard)).path).toBe('/private/dashboard.html')
    const approve = { sessionID, args: 'label --taxonomy "/private/taxonomy.json"', runID: 'confirm' }
    const pending = await desktop(approve)
    expect(pending.confirmation?.id).toBeString()
    expect(events.some(event => event.feature === 'label')).toBe(false)
    const approved = { ...approve, confirmationID: pending.confirmation!.id }
    expect((await desktop(approved)).text).toContain('private model reply')
    await desktop(approved)
    const cancel = { ...approve, runID: 'cancel' }
    expect((await desktop(cancel)).confirmation?.id).toBeString()
    expect(await handlers.cancel(cancel)).toEqual({ cancelled: true })
    await handlers.cancel(cancel)
    expect(await execute({ what: 'brief' }, { sessionID, ...call })).toEqual({ content: 'private tool output' })

    expect(events.map(({ durationMs, ...event }) => {
      expect(durationMs).toBeGreaterThanOrEqual(0)
      return event
    })).toEqual([
      { feature: 'activation', surface: 'unknown', outcome: 'success' },
      { feature: 'help', surface: 'terminal', outcome: 'success' },
      { feature: 'help', surface: 'terminal', outcome: 'rejected' },
      { feature: 'overview', surface: 'terminal', outcome: 'empty' },
      { feature: 'pane', surface: 'desktop', outcome: 'success' },
      { feature: 'doctor', surface: 'terminal', outcome: 'error' },
      { feature: 'dashboard.html', surface: 'desktop', outcome: 'success' },
      { feature: 'label', surface: 'desktop', outcome: 'success' },
      { feature: 'label', surface: 'desktop', outcome: 'cancelled' },
      { feature: 'tool', surface: 'unknown', outcome: 'success' },
    ])
    expect(JSON.stringify(events)).not.toContain('private')
  } finally {
    await cleanup?.()
    report.mockRestore()
    run.mockRestore()
    session.mockRestore()
    snapshot.mockRestore()
    await rm(directory, { recursive: true, force: true })
  }
})
