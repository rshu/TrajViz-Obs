import { expect, spyOn, test } from 'bun:test'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import plugin from '../src/server.js'
import { Backend } from '../src/backend.js'
import { Runtime } from '../src/runtime.js'

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
