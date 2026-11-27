import { describe, expect, test } from 'bun:test'
import { captureSession, validateSource, type NativeSource } from '../src/collect.js'
import type { HostClient, HostSessionOptions } from '../src/host.js'
import { mkdtemp, rm, symlink } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'

const info = (id: string, parentID?: string, extra = {}) => ({ id, directory: '/demo', ...(parentID ? { parentID } : {}), ...extra })
const row = (sessionID: string, id: string, parts: Record<string, unknown>[] = []) => ({ info: { id, sessionID, role: 'assistant' }, parts })
const task = (id: string, field = 'sessionId', tool = 'task') => ({ type: 'tool', tool, state: { status: 'completed', metadata: { [field]: id } } })
function reader(sessions: Record<string, Record<string, unknown>>, history: Record<string, unknown[]>): { client: HostClient; calls: { method: string; options: HostSessionOptions }[] } {
  const calls: { method: string; options: HostSessionOptions }[] = []
  return { calls, client: { session: {
    async get(options) { calls.push({ method: 'get', options }); return { data: sessions[options.path.id] } },
    async messages(options) { calls.push({ method: 'messages', options }); return { data: history[options.path.id] } },
  } } }
}
const source = (id = 'root', parentID?: string): NativeSource => ({ source_format: 'deveco_v1', schema_version: 1, directory: '/demo', session: info(id, parentID), messages: [], children: [] })

describe('DevEco legacy native history', () => {
  test('uses verified SDK v1 options without a truncating limit and keeps pre-compaction messages', async () => {
    const original = [row('root', 'before'), row('root', 'summary', [{ type: 'compaction' }]), row('root', 'after')]
    const h = reader({ root: info('root') }, { root: original })
    const signal = new AbortController().signal
    const captured = await captureSession(h.client, 'root', '/demo', signal)
    expect(captured.messages.map(message => message.info.id)).toEqual(['before', 'summary', 'after'])
    expect(h.calls).toHaveLength(2)
    expect(h.calls[1]).toEqual({ method: 'messages', options: { path: { id: 'root' }, query: { directory: '/demo' }, signal, throwOnError: true } })
    expect(h.calls[1]!.options.query).not.toHaveProperty('limit')
    original[0]!.info.id = 'changed-after-read'
    expect(captured.messages[0]!.info.id).toBe('before')
    expect(validateSource(captured)).toBe(captured)
  })

  test('includes only task-linked and parent-confirmed children, excluding explicit forks and foreign sessions', async () => {
    const h = reader({ root: info('root'), child: info('child', 'root', { directory: '/child-worktree' }), fork: info('fork', 'root', { fork: { sessionID: 'root' } }), foreign: info('foreign', 'another') }, {
      root: [row('root', 'spawn', [task('child'), task('child', 'sessionID'), task('fork'), task('foreign'), task('not-a-child', 'sessionId', 'read')])],
      child: [row('child', 'work')],
    })
    const captured = await captureSession(h.client, 'root', '/demo')
    expect(captured.children.map(child => child.session.id)).toEqual(['child'])
    expect(captured.children[0]!.directory).toBe('/child-worktree')
    expect(h.calls.filter(call => call.method === 'messages').map(call => call.options.path.id)).toEqual(['root', 'child'])
    expect(h.calls.some(call => call.options.path.id === 'not-a-child')).toBe(false)
  })

  test('does not silently replace missing child history or SDK errors with an empty session', async () => {
    const h = reader({ root: info('root'), child: info('child', 'root') }, { root: [row('root', 'spawn', [task('child')])] })
    await expect(captureSession(h.client, 'root', '/demo')).rejects.toThrow('message read failed')
    h.client.session.get = async () => ({ error: { message: 'private upstream detail', headers: { authorization: 'secret' } } })
    await expect(captureSession(h.client, 'root', '/demo')).rejects.toThrow('session read failed')
  })

  test('rejects foreign message identities and invalid legacy envelope shapes', async () => {
    const h = reader({ root: info('root') }, { root: [row('foreign', 'bad')] })
    await expect(captureSession(h.client, 'root', '/demo')).rejects.toThrow('another session identity')
    for (const value of [null, {}, { ...source(), messages: [{ info: {}, parts: [null] }] }, { ...source(), source_format: 'opencode_v2' }, { ...source(), window: 0 }, { ...source(), directory: '/wrong-envelope-directory' }]) {
      expect(() => validateSource(value)).toThrow()
    }
  })

  test('rejects a root from another project before reading its message content', async () => {
    const h = reader({ root: info('root', undefined, { directory: '/another-project' }) }, { root: [row('root', 'private')] })
    await expect(captureSession(h.client, 'root', '/demo')).rejects.toThrow('another project directory')
    expect(h.calls.map(call => call.method)).toEqual(['get'])
    h.client.session.get = async () => ({ data: { id: 'root' } })
    await expect(captureSession(h.client, 'root', '/demo')).rejects.toThrow('missing its native project directory')
  })

  test('accepts filesystem aliases of the same native project', async () => {
    const temporary = await mkdtemp(path.join(os.tmpdir(), 'deveco-project-alias-'))
    try {
      const alias = path.join(temporary, 'alias')
      await symlink(temporary, alias)
      const h = reader({ root: info('root', undefined, { directory: temporary }) }, { root: [] })
      const captured = await captureSession(h.client, 'root', alias)
      expect(captured.directory).toBe(temporary)
    } finally { await rm(temporary, { recursive: true, force: true }) }
  })

  test('rejects cycles, duplicate sources, and unconfirmed parent links', async () => {
    const h = reader({ root: info('root') }, { root: [row('root', 'cycle', [task('root')])] })
    await expect(captureSession(h.client, 'root', '/demo')).rejects.toThrow('Cyclic')
    expect(() => validateSource({ ...source(), children: [source('child', 'wrong')] })).toThrow('parent')
    expect(() => validateSource({ ...source(), children: [source('child', 'root')] })).toThrow('confirming native task')
    expect(() => validateSource({ ...source(), messages: [row('root', 'spawn', [task('child')])], children: [source('child', 'root'), source('child', 'root')] })).toThrow('Invalid')
  })

  test('honors cancellation and UTF-8 byte limits', async () => {
    const h = reader({ root: info('root') }, { root: [] })
    const controller = new AbortController()
    controller.abort(new Error('user cancelled'))
    await expect(captureSession(h.client, 'root', '/demo', controller.signal)).rejects.toThrow('user cancelled')
    expect(h.calls).toHaveLength(0)
    const huge = { ...source(), session: { ...info('root'), text: '🧪'.repeat(8 * 1024 * 1024) } }
    expect(() => validateSource(huge)).toThrow('32 MiB')
  })
})
