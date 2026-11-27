import type { HostClient, HostResponse } from './host.js'
import { realpath } from 'node:fs/promises'
import path from 'node:path'

export type NativeMessage = { info: Record<string, unknown>; parts: Record<string, unknown>[] }
export type NativeSource = {
  source_format: 'deveco_v1'
  schema_version: 1
  directory: string
  session: Record<string, unknown>
  messages: NativeMessage[]
  children: NativeSource[]
  window?: number
}

const MAX_BYTES = 32 * 1024 * 1024
const MAX_SESSIONS = 64
const MAX_MESSAGES = 200_000
const encoder = new TextEncoder()
export const record = (value: unknown): Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const object = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value)
const validID = (id: unknown): id is string => typeof id === 'string' && id.length > 0 && id.length <= 4096 && !id.includes('\0')
const bytes = (value: unknown) => encoder.encode(JSON.stringify(value)).byteLength
const validDirectory = (value: unknown): value is string => typeof value === 'string' && value.length > 0 && !value.includes('\0')
const canonicalDirectory = async (value: string) => realpath(value).catch(() => path.resolve(value))

function data<T>(response: HostResponse<T>, label: string): T {
  if (!response || response.error !== undefined || response.response && !response.response.ok || response.data === undefined) {
    throw new Error(`DevEco ${label} failed; no complete native history was returned.`)
  }
  return response.data
}

function messages(value: unknown, sessionID: string): NativeMessage[] {
  if (!Array.isArray(value) || value.length > MAX_MESSAGES) throw new Error('Invalid or oversized DevEco message history.')
  for (const row of value) {
    if (!object(row) || !object(row.info) || !Array.isArray(row.parts) || row.parts.some(part => !object(part))) {
      throw new Error('Invalid DevEco native message (expected info and parts).')
    }
    if (row.info.sessionID !== undefined && row.info.sessionID !== sessionID || row.parts.some(part => part.sessionID !== undefined && part.sessionID !== sessionID)) {
      throw new Error('DevEco message history contains another session identity.')
    }
  }
  return value as NativeMessage[]
}

function childReferences(rows: NativeMessage[]): string[] {
  const refs = new Set<string>()
  for (const row of rows) for (const part of row.parts) {
    if (part.type !== 'tool' || !['task', 'subagent'].includes(String(part.tool))) continue
    const metadata = record(record(part.state).metadata)
    const lower = metadata.sessionId, upper = metadata.sessionID
    if (lower !== undefined && upper !== undefined && lower !== upper) continue
    const id = lower ?? upper
    if (validID(id)) refs.add(id)
  }
  return [...refs]
}

/** SDK v1 has an optional limit but no cursor. Omit limit to read all durable
 * messages, including pre-compaction history. Only task-linked, parent-confirmed
 * children are attached; the children listing alone cannot distinguish forks.
 */
export async function captureSession(client: HostClient, sessionID: string, directory: string, signal?: AbortSignal): Promise<NativeSource> {
  if (!validID(sessionID) || typeof directory !== 'string' || !directory || directory.includes('\0')) throw new Error('Invalid DevEco session selection.')
  const seen = new Set<string>()
  let total = 0
  const account = (value: unknown) => {
    total += bytes(value)
    if (total > MAX_BYTES) throw new Error('DevEco session family exceeds 32 MiB; export a smaller source explicitly.')
  }
  const options = (id: string) => ({ path: { id }, query: { directory }, signal, throwOnError: true })
  const read = async (id: string, known?: Record<string, unknown>): Promise<NativeSource> => {
    signal?.throwIfAborted()
    if (seen.has(id)) throw new Error('Cyclic or duplicate DevEco child session relationship.')
    if (seen.size >= MAX_SESSIONS) throw new Error('DevEco session family exceeds 64 sessions.')
    seen.add(id)
    const info = known ?? data(await client.session.get(options(id)), 'session read')
    if (!object(info) || info.id !== id) throw new Error('DevEco session response does not match the requested session.')
    if (!validDirectory(info.directory)) throw new Error('DevEco session response is missing its native project directory.')
    if (id === sessionID && await canonicalDirectory(info.directory) !== await canonicalDirectory(directory)) {
      throw new Error('DevEco session belongs to another project directory.')
    }
    account(info)
    const rows = messages(data(await client.session.messages(options(id)), 'message read'), id)
    signal?.throwIfAborted()
    account(rows)
    const children: NativeSource[] = []
    for (const childID of childReferences(rows)) {
      signal?.throwIfAborted()
      if (seen.has(childID)) throw new Error('Cyclic or duplicate DevEco child session relationship.')
      const child = data(await client.session.get(options(childID)), 'child session read')
      if (!object(child) || child.id !== childID) throw new Error('DevEco child response does not match its task reference.')
      if (child.parentID !== id || child.fork || child.forkedFrom || child.forkedFromID) continue
      children.push(await read(childID, child))
    }
    return { source_format: 'deveco_v1', schema_version: 1,
      directory: info.directory,
      session: info, messages: rows, children }
  }
  // Snapshot before returning: subsequent host updates must not rewrite a saved revision.
  const source = JSON.parse(JSON.stringify(await read(sessionID))) as NativeSource
  signal?.throwIfAborted()
  return validateSource(source)
}

export function validateSource(value: unknown): NativeSource {
  const pending = [value], seen = new Set<string>()
  while (pending.length) {
    const item = pending.pop()
    if (!object(item) || item.source_format !== 'deveco_v1' || item.schema_version !== 1 || typeof item.directory !== 'string'
      || !item.directory || item.directory.includes('\0') || !object(item.session) || !validID(item.session.id) || item.session.directory !== item.directory
      || seen.has(item.session.id) || !Array.isArray(item.children)
      || item.window !== undefined && (!Number.isSafeInteger(item.window) || Number(item.window) <= 0)) throw new Error('Invalid DevEco native session capture.')
    seen.add(item.session.id)
    if (seen.size > MAX_SESSIONS) throw new Error('DevEco session family exceeds 64 sessions.')
    const rows = messages(item.messages, item.session.id)
    const linkedChildren = new Set(childReferences(rows))
    for (const child of item.children) {
      if (!object(child) || !object(child.session) || child.session.parentID !== item.session.id || child.session.fork || child.session.forkedFrom || child.session.forkedFromID) {
        throw new Error('Invalid DevEco child parent or fork relationship.')
      }
      if (!validID(child.session.id) || !linkedChildren.has(child.session.id)) throw new Error('DevEco child has no confirming native task reference.')
      pending.push(child)
    }
  }
  if (bytes(value) > MAX_BYTES) throw new Error('DevEco session capture exceeds 32 MiB.')
  return value as NativeSource
}
