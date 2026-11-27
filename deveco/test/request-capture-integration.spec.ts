import { expect, test } from 'bun:test'
import { createHash } from 'node:crypto'
import { mkdtemp, readFile, readdir, realpath, rm } from 'node:fs/promises'
import http from 'node:http'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { Backend, requestCaptureArgs } from '../src/backend.js'
import plugin from '../src/index.js'
import type { HostClient } from '../src/host.js'

const keys = ['TRAJVIZ_OBS_REQUEST_CAPTURE', 'TRAJVIZ_OBS_REQUEST_CAPTURE_DIR', 'TRAJVIZ_OBS_REQUEST_CAPTURE_PROVIDERS', 'TRAJVIZ_NATIVE_TRACE_CAPTURE', 'DEVECO_EXPERIMENTAL_NATIVE_LLM'] as const
async function withCaptureEnvironment<T>(root: string, work: () => Promise<T>): Promise<T> {
  const old = keys.map(key => [key, process.env[key]] as const)
  process.env.TRAJVIZ_OBS_REQUEST_CAPTURE = '1'
  process.env.TRAJVIZ_OBS_REQUEST_CAPTURE_DIR = root
  process.env.TRAJVIZ_OBS_REQUEST_CAPTURE_PROVIDERS = 'fixture'
  delete process.env.TRAJVIZ_NATIVE_TRACE_CAPTURE
  delete process.env.DEVECO_EXPERIMENTAL_NATIVE_LLM
  try { return await work() } finally { for (const [key, value] of old) if (value === undefined) delete process.env[key]; else process.env[key] = value }
}
const hash = (value: string) => createHash('sha256').update(value).digest('hex')
function nativeClient(directory: string): HostClient {
  return { session: {
    async get() { return { data: { id: 'ses_fixture', directory } } },
    async messages() { return { data: [] } },
  } }
}

test('request capture bridge capability requires explicit enabled absolute scope and a safe session', () => {
  const env = { TRAJVIZ_OBS_REQUEST_CAPTURE: '1', TRAJVIZ_OBS_REQUEST_CAPTURE_DIR: '/private/captures with spaces' }
  expect(requestCaptureArgs('ses_fixture', env)).toEqual(['--request-capture-root', '/private/captures with spaces', '--request-session-id', 'ses_fixture'])
  for (const invalid of [ {}, { ...env, TRAJVIZ_OBS_REQUEST_CAPTURE: '0' }, { ...env, TRAJVIZ_OBS_REQUEST_CAPTURE_DIR: '' }, { ...env, TRAJVIZ_OBS_REQUEST_CAPTURE_DIR: 'relative' } ]) expect(requestCaptureArgs('ses_fixture', invalid)).toEqual([])
  for (const session of ['', 'a\0b', 'x'.repeat(4097)]) expect(requestCaptureArgs(session, env)).toEqual([])
})

test('backend grants private request access only to prompt construction and the owned dashboard', async () => {
  const root = await realpath(await mkdtemp(path.join(tmpdir(), 'deveco-request-bridge-')))
  const backend = new Backend(nativeClient(root), root, { cacheRoot: path.join(root, 'cache') })
  try {
    await withCaptureEnvironment(path.join(root, 'private requests'), async () => {
      const calls: string[][] = []
      backend.runtime.run = async <T>(args: readonly string[]) => { calls.push([...args]); return {} as T }
      await backend.inspect('ses_fixture', 'view', ['prompt_construction', '--json'])
      await backend.inspect('ses_fixture', 'view', ['issues', '--json'])
      await backend.inspect('ses_fixture', 'snapshot')
      expect(calls[0]!.slice(-4)).toEqual(['--request-capture-root', path.join(root, 'private requests'), '--request-session-id', 'ses_fixture'])
      expect(calls[1]).not.toContain('--request-capture-root')
      expect(calls[2]).not.toContain('--request-capture-root')
      const launched: string[][] = []
      backend.runtime.argv = async args => { launched.push([...args]); return [process.execPath, '-e', 'console.log(JSON.stringify({url:"http://127.0.0.1:54325/"})); setInterval(()=>{},1000)'] }
      await backend.dashboard('ses_fixture', 54325)
      expect(launched[0]).toContain('--exit-on-stdin')
      expect(launched[0]).toContain('--request-capture-root')
      expect(launched[0]![launched[0]!.indexOf('--request-session-id') + 1]).toBe('ses_fixture')
    })
  } finally { await backend.close(); await rm(root, { recursive: true, force: true }) }
})

test('real plugin hooks capture the serialized body and unload their listener without a model call', async () => {
  const root = await realpath(await mkdtemp(path.join(tmpdir(), 'deveco-request-hooks-')))
  const captureRoot = path.join(root, 'private-requests')
  const observed: { body: string; headers: http.IncomingHttpHeaders }[] = []
  const upstream = http.createServer(async (request, response) => {
    const chunks: Buffer[] = []; for await (const chunk of request) chunks.push(Buffer.from(chunk))
    observed.push({ body: Buffer.concat(chunks).toString('utf8'), headers: request.headers })
    response.writeHead(200, { 'content-type': 'text/event-stream' }); response.end('data: [DONE]\n\n')
  })
  await new Promise<void>(resolve => upstream.listen(0, '127.0.0.1', resolve))
  try {
    await withCaptureEnvironment(captureRoot, async () => {
      const hooks = await plugin({ client: nativeClient(root), directory: root }, { cacheRoot: path.join(root, 'cache') })
      try {
        expect(typeof hooks.config).toBe('function'); expect(typeof hooks['chat.headers']).toBe('function')
        const original = `http://127.0.0.1:${(upstream.address() as { port: number }).port}/v1`
        const config = { model: 'fixture/main', small_model: 'fixture/small', provider: { fixture: { options: { baseURL: original, apiKey: 'DUMMY' } } } }
        await hooks.config!(config)
        expect(config.provider.fixture.options.baseURL).not.toBe(original)
        const output = { headers: { authorization: 'Bearer DUMMY' } }
        await hooks['chat.headers']!({ sessionID: 'ses_fixture', message: { id: 'msg_user', sessionID: 'ses_fixture' }, agent: 'build', model: { providerID: 'fixture', id: 'main' } }, output)
        const body = '{"model":"main","messages":[{"role":"system","content":"System constructed by provider"},{"role":"user","content":"Synthetic plugin integration"}],"tools":[],"stream":true}'
        const response = await fetch(config.provider.fixture.options.baseURL + '/chat/completions', { method: 'POST', headers: { ...output.headers, 'content-type': 'application/json' }, body })
        expect(response.status).toBe(200); await response.text()
        expect(observed).toHaveLength(1); expect(observed[0]!.body).toBe(body)
        expect(Object.keys(observed[0]!.headers).some(name => name.startsWith('x-trajviz-'))).toBe(false)
        const folder = path.join(captureRoot, hash('ses_fixture'))
        const metadataName = (await readdir(folder)).find(name => name.endsWith('.json'))!
        const metadata = JSON.parse(await readFile(path.join(folder, metadataName), 'utf8'))
        expect(metadata.session_id).toBe('ses_fixture'); expect(metadata.user_message_id).toBe('msg_user')
        expect(metadata.mapping.status).toBe('unlinked') // Native recording span is deliberately absent in this fixture.
        expect(await readFile(path.join(folder, metadata.body_ref), 'utf8')).toBe(body)
        expect(config.small_model).toBe('fixture/small')
        await hooks.dispose!(); await hooks.dispose!()
        await expect(fetch(config.provider.fixture.options.baseURL + '/chat/completions', { method: 'POST', headers: output.headers, body })).rejects.toThrow()
      } finally { await hooks.dispose!() }
    })
  } finally {
    upstream.closeAllConnections()
    await new Promise<void>(resolve => upstream.close(() => resolve()))
    await rm(root, { recursive: true, force: true })
  }
})
