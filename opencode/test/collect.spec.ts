import { describe, expect, test } from 'bun:test'
import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { captureSession, validateSource } from '../src/collect.js'

type Reader = Parameters<typeof captureSession>[0]
const session = (id: string, parentID?: string, fork?: object) => ({ id, parentID, fork, location: {directory:'/demo'} })

describe('native OpenCode history collection', () => {
  test('Desktop capture and sync validation work without Node globals and retain UTF-8 limits', () => {
    const code = `
      delete globalThis.Buffer
      const { captureSession, validateSource } = await import('./src/collect.ts')
      const { Obs } = await import('./src/rpc.ts')
      const session = { id: 'ses_browser', location: { directory: '/demo' } }
      const messages = [{ id: 'unicode', type: 'assistant', text: '🧪' }]
      const reader = { session: { get: async () => session },
        message: { list: async () => ({ data: messages, cursor: {} }) } }
      const source = await captureSession(reader, session.id)
      validateSource(source)
      Obs.methods.sync.input.parse({ source })
      const sessionBytes = new TextEncoder().encode(JSON.stringify(session)).byteLength
      const remaining = JSON.stringify(messages).length
      let limited = false
      try {
        await captureSession(reader, session.id, undefined, new Set(), { bytes: 32 * 1024 * 1024 - sessionBytes - remaining })
      } catch (error) { limited = error.message.includes('32 MiB') }
      if (!limited) throw new Error('Unicode payload bypassed the UTF-8 byte limit')
      console.log(JSON.stringify({ id: source.session.id, text: source.messages[0].text, limited }))
    `
    const output = execFileSync(process.execPath, ['--eval', code], {
      cwd: fileURLToPath(new URL('..', import.meta.url)), encoding: 'utf8', timeout: 10_000,
    })
    expect(JSON.parse(output)).toEqual({ id: 'ses_browser', text: '🧪', limited: true })
  })

  test('follows opaque cursors and confirmed subagents, retaining pre-compaction history', async () => {
    const requests: object[] = []
    const child = session('ses_child','ses_root'), fork = session('ses_fork','ses_root',{sessionID:'ses_root'})
    const reader = {
      session: { get: async ({sessionID}: {sessionID:string}) => sessionID === 'ses_root' ? session(sessionID) : sessionID === 'ses_child' ? child : fork },
      message: {list: async (input: {sessionID:string;cursor?:string}) => {
        requests.push(input)
        if (input.sessionID === 'ses_child') return {data:[{id:'child_work',type:'assistant',content:[]}],cursor:{}}
        if (!input.cursor) return { data:[{id:'before_compaction',type:'user',text:'Earlier task'},{id:'compact',type:'compaction',status:'completed'}], cursor:{next:'opaque+/next='} }
        return {data:[{id:'spawn',type:'assistant',content:[{type:'tool',name:'subagent',state:{metadata:{sessionID:'ses_child'}}},{type:'tool',name:'subagent',state:{metadata:{sessionID:'ses_fork'}}},{type:'tool',name:'read',state:{metadata:{sessionID:'ses_wrong'}}}]}],cursor:{}}
      }},
    } as unknown as Reader
    const source = await captureSession(reader,'ses_root')
    expect(source.messages.map(m=>m.id)).toEqual(['before_compaction','compact','spawn'])
    expect(source.children.map(c=>c.session.id)).toEqual(['ses_child'])
    expect(requests[1]).toEqual({sessionID:'ses_root',limit:200,cursor:'opaque+/next='})
    expect(validateSource(source)).toBe(source)
  })

  test('rejects repeated cursors rather than returning silently truncated history', async () => {
    const reader = {session:{get:async()=>session('ses_root')},message:{list:async()=>({data:[],cursor:{next:'same'}})}} as unknown as Reader
    await expect(captureSession(reader,'ses_root')).rejects.toThrow('repeated a message cursor')
  })

  test('missing child history is an explicit collection failure', async () => {
    const reader = {session:{get:async({sessionID}:{sessionID:string})=>{if(sessionID!=='ses_root')throw new Error('child inaccessible');return session(sessionID)}},message:{list:async()=>({data:[{type:'assistant',content:[{type:'tool',name:'subagent',state:{metadata:{sessionID:'ses_child'}}}]}],cursor:{}})}} as unknown as Reader
    await expect(captureSession(reader,'ses_root')).rejects.toThrow('child inaccessible')
  })

  test('validates recursive source contracts and duplicate session identities', () => {
    const root = {source_format:'opencode_v2',schema_version:1,directory:'/demo',session:{id:'ses_root'},messages:[],children:[]} as const
    expect(()=>validateSource({...root,children:[{...root}]})).toThrow('Invalid')
    expect(()=>validateSource({...root,children:[{}]})).toThrow('Invalid')
    expect(()=>validateSource({...root,messages:[null]})).toThrow('Invalid')
    expect(()=>validateSource({...root,schema_version:2})).toThrow('Invalid')
  })
})
