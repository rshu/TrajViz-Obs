import type { OpenCodeClient } from '@opencode/client'

export type NativeSource = {
  source_format: 'opencode_v2'
  schema_version: 1
  directory: string
  session: Record<string, unknown>
  messages: Record<string, unknown>[]
  children: NativeSource[]
  window?: number
}

type Reader = Pick<OpenCodeClient, 'session' | 'message'>
export const record = (v: unknown): Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v) ? v as Record<string, unknown> : {}

/** Read every projected message, including pre-compaction history. Never use model context as a transcript. */
export async function captureSession(client: Reader, sessionID: string, signal?: AbortSignal, seen = new Set<string>(), budget = {bytes:0}): Promise<NativeSource> {
  if (seen.has(sessionID)) throw new Error('Cyclic subagent session relationship')
  if (seen.size >= 64) throw new Error('Session family exceeds 64 sessions; export the source explicitly instead.')
  seen.add(sessionID)
  const session = record(await client.session.get({ sessionID }, { signal }))
  budget.bytes += Buffer.byteLength(JSON.stringify(session))
  const messages: Record<string, unknown>[] = []
  let cursor: string | undefined
  const cursors = new Set<string>()
  do {
    const page = await client.message.list({ sessionID, limit: 200, ...(cursor ? { cursor } : { order: 'asc' as const }) }, { signal })
    budget.bytes += Buffer.byteLength(JSON.stringify(page.data))
    if (budget.bytes > 32 * 1024 * 1024) throw new Error('Session family exceeds 32 MiB; use an explicit source export.')
    messages.push(...page.data.map(record))
    cursor = page.cursor.next ?? undefined
    if (cursor && cursors.has(cursor)) throw new Error('OpenCode repeated a message cursor; retry after refreshing the session.')
    if (cursor) cursors.add(cursor)
    if (messages.length > 200_000) throw new Error('Session exceeds 200,000 messages; export the source explicitly instead.')
  } while (cursor)
  const children: NativeSource[] = []
  const ids = new Set<string>()
  for (const message of messages) {
    if (!Array.isArray(message.content)) continue
    for (const raw of message.content) {
      const part = record(raw), state = record(part.state), metadata = record(state.metadata)
      if (part.type === 'tool' && (part.name === 'subagent' || part.name === 'task') && typeof metadata.sessionID === 'string') ids.add(metadata.sessionID)
    }
  }
  for (const id of ids) {
    if (seen.has(id)) continue
    const info = record(await client.session.get({ sessionID: id }, { signal }))
    if (info.parentID !== sessionID || info.fork) continue
    children.push(await captureSession(client, id, signal, seen, budget))
  }
  const location = record(session.location)
  return { source_format: 'opencode_v2', schema_version: 1, directory: String(location.directory ?? ''), session, messages, children }
}

export function validateSource(value: unknown): NativeSource {
  const source = record(value)
  const pending = [source], ids = new Set<string>()
  while (pending.length) {
    const item = pending.pop()!, id = record(item.session).id
    if (item.source_format !== 'opencode_v2' || item.schema_version !== 1 || typeof id !== 'string' || !id || id.length > 256 || ids.has(id) || typeof item.directory !== 'string' || !Array.isArray(item.messages) || !Array.isArray(item.children) || item.messages.some(m => !m || typeof m !== 'object' || Array.isArray(m)) || (item.window !== undefined && (!Number.isSafeInteger(item.window) || Number(item.window) <= 0))) throw new Error('Invalid OpenCode 2 session capture.')
    ids.add(id)
    if (ids.size > 64) throw new Error('Session family exceeds 64 sessions.')
    pending.push(...item.children.map(record))
  }
  if (Buffer.byteLength(JSON.stringify(source)) > 32 * 1024 * 1024) throw new Error('Session capture exceeds 32 MiB; use an explicit source export.')
  return source as NativeSource
}
