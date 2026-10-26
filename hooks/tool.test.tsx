import { expect, mock, test } from 'claude-code/testing'
import type { On, ToolSpec } from 'claude-code'
import { SNAPSHOT } from './testdata/snapshot'

const HOME = '/Users/me'
const CWD = '/Users/me/p'
const CACHE = `${HOME}/.cache/trajviz-obs`
const VENV_PY = `${CACHE}/envs/0123456789abcdef01234567/0123456789abcdef0123456789abcdef/bin/python`
const STATE_PATH = `${CACHE}/state.json`
const DERIVED = `${HOME}/.claude/projects/-Users-me-p/abc.jsonl`
const ARTIFACTS = `${CACHE}/sessions/abc`
const USAGE = { startedAt: 0, context: { window: 1_000_000, percent: 12 }, rateLimits: [] }
const NOW = 1_761_150_000_000
const STATE_JSON = JSON.stringify({ revision: 4, python: VENV_PY, trajviz: '0.2.1', extras: ['llm', 'charts'], source: 'agent-trajviz>=0.2.0,<0.3', createdAt: NOW })

/** The argv every session-bound bridge subcommand gets for the current session. */
const SESSION_ARGV = ['--transcript', DERIVED, '--window', '1000000', '--artifacts', ARTIFACTS, '--kind', 'current']

const TOOL = 'mcp__trajviz-obs__obs'

type RunAnswer = { exitCode: number; stdout: string; stderr?: string }

const json = (doc: unknown): RunAnswer => ({ exitCode: 0, stdout: `${JSON.stringify(doc)}\n` })

type Host = { ran: string[][]; registered: ToolSpec[] }

/** The host beneath the mod: env, `state.json`, the session, the bridge answered by subcommand; every run recorded. */
function host(on: On, bridge: Record<string, RunAnswer>, toolError?: string): Host {
  const ran: string[][] = []
  const registered: ToolSpec[] = []
  on('command.register', () => ({ value: { command: 'obs' } }))
  on('tool.register', (_, e) => {
    registered.push(e as ToolSpec)
    if (toolError) throw new Error(toolError)
    return { value: { tool: TOOL } }
  })
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: 'abc' }))
  on('session.cwd', () => ({ value: CWD }))
  on('session.usage', () => ({ value: USAGE }))
  mock.env(on, { HOME })
  mock.clock(on, { now: NOW })
  on('fs.read', (_, e) => (e.path === STATE_PATH ? { value: STATE_JSON } : { deny: `ENOENT: ${e.path}` }))
  on('fs.write', () => ({ value: undefined }))
  on('fs.exists', (_, e) => ({ value: e.path === VENV_PY || e.path === DERIVED }))
  on('ui.toast', () => ({ value: undefined }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.log', () => ({ value: undefined }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('process.run', (_, e) => {
    ran.push([...e.argv])
    const a = e.argv[1] === '-I' ? bridge[e.argv[3] ?? ''] : undefined
    if (!a) return { deny: `cannot start ${e.argv[0]}` }
    return { value: { exitCode: a.exitCode, stdout: a.stdout, stderr: a.stderr ?? '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  return { ran, registered }
}

/** The argv of every bridge call, after the launcher. */
const bridgeCalls = (ran: string[][]): string[][] => ran.filter(r => r[1] === '-I').map(r => r.slice(3))

type Answer = { result?: string; deny?: string }

/** One call of the obs tool as the model makes it, answered by the mod's own `tool.call` hook. */
const callObs = async ($: { tool: { call: (input: never) => Promise<unknown> } }, input: Record<string, unknown>): Promise<Answer> => {
  const out = (await $.tool.call({ tool: TOOL, ...input } as never)) as Answer
  // The kit's generic MCP result does not enforce the real MCP output schema.
  // Match core's native string boundary on every branch exercised below.
  if ('result' in out) {
    expect(typeof out.result).toBe('string')
    expect(new TextEncoder().encode(out.result).length).toBeLessThanOrEqual(65_536)
  }
  return out
}

const start = ($: { session: { start: (e: never) => Promise<unknown> } }) =>
  $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true } as never)

const VIEW = { id: 'steps', title: 'Steps', kind: 'table', description: 'One row per step' }

test('the obs tool is registered at session start, deferred, with its input schema', async ($, on) => {
  const { registered } = host(on, {})
  await start($)
  expect(registered).toHaveLength(1)
  const spec = registered[0] as ToolSpec
  expect(spec.name).toBe('obs')
  expect(spec.isDeferred).toBe(true)
  const schema = spec.inputSchema as { properties: { what: { enum: string[] } }; required: string[] }
  expect(schema.properties.what.enum).toEqual(['brief', 'issues', 'health', 'view'])
  expect(schema.required).toEqual(['what'])
})

test('disabled model tools preserve slash commands and initialize configured UI state', { options: { watch: false, statusline: true } }, async ($, on) => {
  host(on, {}, 'this session has no built-in tools (--tools "")')
  const flags: unknown[] = []
  const dashboards: unknown[] = []
  on('state.set', { plugin: 'trajviz-obs', key: 'flags' }, (_, e, next) => {
    flags.push(e.value)
    return next(e)
  })
  on('state.set', { plugin: 'trajviz-obs', key: 'dashboards' }, (_, e, next) => {
    dashboards.push(e.value)
    return next(e)
  })
  await start($)
  expect(flags).toContainEqual({ watch: false, statusline: true, isInteractive: true, envReady: false })
  expect(dashboards).toContainEqual({ insight: null, converge: null })
  const result = await $.command.run({ command: 'obs', args: 'help', origin: { kind: 'composer' }, presentation: { columns: 80, isFullscreen: false } })
  expect(result.text).toContain('Start here: /obs')
})

test('registered MCP output uses native strings for data, brief, view and validation errors', async ($, on) => {
  const path = `${ARTIFACTS}/views/steps.json`
  host(on, {
    snapshot: json(SNAPSHOT), brief: json({ brief: 'The session has one failed call.' }),
    view: json({ view: VIEW, path, bytes: 100, rows_total: 1, inline: { data: [{ step: 1 }] } }),
  })
  await start($)
  for (const input of [{ what: 'health' }, { what: 'issues' }, { what: 'brief' },
    { what: 'view', view: 'steps' }, { what: 'invalid' }]) {
    const response = await callObs($, input)
    expect(typeof response.result).toBe('string')
    expect(response.deny).toBeUndefined()
  }
})

test('what: "issues" answers the issue titles from a fresh snapshot, then from the stored one', async ($, on) => {
  const { ran } = host(on, { snapshot: json(SNAPSHOT) })
  await start($)
  const first = await callObs($, { what: 'issues' })
  for (const issue of SNAPSHOT.issues) expect(first.result).toContain(issue.title)
  expect(JSON.parse(first.result ?? '')).toEqual({ issues: SNAPSHOT.issues, total: SNAPSHOT.issues.length })
  const second = await callObs($, { what: 'issues', limit: 1 })
  expect(JSON.parse(second.result ?? '')).toEqual({ issues: SNAPSHOT.issues.slice(0, 1), total: SNAPSHOT.issues.length })
  expect(bridgeCalls(ran)).toEqual([['snapshot', ...SESSION_ARGV]])
})

test('what: "health" answers the health rows of the snapshot', async ($, on) => {
  host(on, { snapshot: json(SNAPSHOT) })
  await start($)
  const r = await callObs($, { what: 'health' })
  expect(JSON.parse(r.result ?? '')).toEqual({ health: SNAPSHOT.health, total: SNAPSHOT.health.length })
})

test('issues reports the full count when the snapshot carries only its top preview', async ($, on) => {
  host(on, { snapshot: json({ ...SNAPSHOT, counts: { ...SNAPSHOT.counts, issues: 37 } }) })
  await start($)
  const result = await callObs($, { what: 'issues', limit: 1 })
  expect(JSON.parse(result.result ?? '')).toEqual({ issues: SNAPSHOT.issues.slice(0, 1), total: 37 })
})

test('what: "brief" answers the bridge brief of the current session', async ($, on) => {
  const { ran } = host(on, { brief: json({ brief: 'Session: 311 assistant steps, 196 tool calls.' }) })
  await start($)
  const r = await callObs($, { what: 'brief' })
  expect(r.result).toBe('Session: 311 assistant steps, 196 tool calls.')
  expect(bridgeCalls(ran)).toEqual([['brief', ...SESSION_ARGV]])
})

test('what: "view" over 64 KB answers the path, not the data', async ($, on) => {
  const path = `${ARTIFACTS}/views/steps.json`
  const { ran } = host(on, { view: json({ view: VIEW, path, bytes: 65_537, rows_total: 311, inline: { data: [{ step: 1 }] } }) })
  await start($)
  const r = await callObs($, { what: 'view', view: 'steps' })
  expect(r.result).toContain(path)
  expect(r.result).not.toContain('"step"')
  expect(bridgeCalls(ran)).toEqual([['view', ...SESSION_ARGV, 'steps', '--limit', '25', '--json']])
})

test('what: "view" at most 64 KB inlines the top rows of its data and names the path', async ($, on) => {
  const path = `${ARTIFACTS}/views/steps.json`
  const data = [{ step: 1 }, { step: 2 }, { step: 3 }]
  const { ran } = host(on, { view: json({ view: VIEW, path, bytes: 65_536, rows_total: 3, inline: { data } }) })
  await start($)
  const r = await callObs($, { what: 'view', view: 'steps', options: { agent: 'main', top: 5 }, limit: 2 })
  expect(JSON.parse(r.result ?? '')).toEqual({ view: VIEW, rows_total: 3, data: data.slice(0, 2), path })
  expect(bridgeCalls(ran)).toEqual([
    ['view', ...SESSION_ARGV, 'steps', '--option', 'agent=main', '--option', 'top=5', '--limit', '2', '--json'],
  ])
})

test('UTF-8 metadata and path overhead fall back to the full view path at the final response boundary', async ($, on) => {
  const path = `${ARTIFACTS}/views/steps.json`
  const encoder = new TextEncoder()
  const overhead = encoder.encode(JSON.stringify({ view: VIEW, rows_total: 1, data: [{ preview: '' }], path }, null, 2)).length
  const data = [{ preview: '界'.repeat(Math.floor((65_536 - overhead) / 3) + 1) }]
  const bytes = encoder.encode(JSON.stringify(data)).length
  expect(bytes).toBeLessThanOrEqual(65_536)
  expect(encoder.encode(JSON.stringify({ view: VIEW, rows_total: 1, data, path }, null, 2)).length).toBeGreaterThan(65_536)
  host(on, { view: json({ view: VIEW, path, bytes, rows_total: 1, inline: { data } }) })
  await start($)
  const r = await callObs($, { what: 'view', view: 'steps' })
  expect(r.result).toContain(path)
  expect(r.result).toContain('full response exceeds')
  expect(r.result).not.toContain('preview')
})

test('oversized rendered view text also falls back to its path, and a huge brief stays bounded', async ($, on) => {
  const path = `${ARTIFACTS}/views/steps.json`
  host(on, {
    view: json({ view: VIEW, path, bytes: 100, rows_total: 1, inline: { table: '界'.repeat(22_000) } }),
    brief: json({ brief: '界'.repeat(22_000) }),
  })
  await start($)
  expect((await callObs($, { what: 'view', view: 'steps' })).result).toContain(path)
  expect((await callObs($, { what: 'brief' })).result).toContain('output exceeds the 64 KB tool limit')
})

test('a limit out of range and an unknown what are answered without a bridge run', async ($, on) => {
  const { ran } = host(on, {})
  await start($)
  expect((await callObs($, { what: 'issues', limit: 0 })).result).toBe('limit must be a whole number from 1 to 500.')
  expect((await callObs($, { what: 'labels' })).result).toBe('what must be one of brief, issues, health, view.')
  expect(bridgeCalls(ran)).toEqual([])
})

test('the tool reads the session --session selected', async ($, on) => {
  const other = '/x/def.jsonl'
  const { ran } = host(on, {
    resolve: json({ path: other, session_id: 'def', kind: 'file' }),
    snapshot: json(SNAPSHOT),
    brief: json({ brief: 'the other session' }),
  })
  await start($)
  await $.command.run({ command: 'obs', args: '--session def', origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 80 } } as never)
  ran.length = 0
  expect((await callObs($, { what: 'brief' })).result).toBe('the other session')
  const brief = bridgeCalls(ran).find(c => c[0] === 'brief')
  expect(brief).toEqual(['brief', '--transcript', other, '--artifacts', `${CACHE}/sessions/def`, '--kind', 'other'])
})

test('a bridge error is answered as its row', async ($, on) => {
  host(on, { brief: { exitCode: 1, stdout: `${JSON.stringify({ error: { code: 'load_error', message: 'cannot read the transcript' } })}\n` } })
  await start($)
  expect((await callObs($, { what: 'brief' })).result).toBe('cannot read the transcript')
})

test('issues refresh after the live transcript changes even with the pane closed', async ($, on) => {
  const first = { ...SNAPSHOT, transcript: { ...SNAPSHOT.transcript, path: DERIVED, size: 10, mtime_ms: 1 } }
  const bridge = { snapshot: json(first) }
  const { ran } = host(on, bridge)
  on('fs.stat', () => ({ value: { size: 20, mtimeMs: 2, kind: 'file', isLink: false } }))
  await start($)
  await callObs($, { what: 'issues' })
  bridge.snapshot = json({ ...first, issues: [{ ...SNAPSHOT.issues[0], title: 'new failure in the next turn' }] })
  const next = await callObs($, { what: 'issues' })
  expect(next.result).toContain('new failure in the next turn')
  expect(bridgeCalls(ran).filter(c => c[0] === 'snapshot')).toHaveLength(2)
})

test('fractional host mtimes reuse an unchanged millisecond snapshot', async ($, on) => {
  const snapshot = { ...SNAPSHOT, transcript: { ...SNAPSHOT.transcript, path: DERIVED, size: 10, mtime_ms: 1 } }
  const { ran } = host(on, { snapshot: json(snapshot) })
  on('fs.stat', () => ({ value: { size: 10, mtimeMs: 1.75, kind: 'file', isLink: false } }))
  await start($)
  await callObs($, { what: 'health' })
  await callObs($, { what: 'health' })
  expect(bridgeCalls(ran).filter(c => c[0] === 'snapshot')).toHaveLength(1)
})
