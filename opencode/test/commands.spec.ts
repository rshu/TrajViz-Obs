import { describe, expect, test } from 'bun:test'
import type { Backend, SourceRef } from '../src/backend.js'
import { runCommand } from '../src/commands.js'
import { pageArguments, parseExtras, validateArguments } from '../src/command-args.js'

function fixture(responses: Record<string, unknown> = {}) {
  const calls: { argv: string[]; timeout?: number }[] = []
  const served: { kind: string; argv: string[] }[] = []
  const stopped: unknown[][] = []
  const selections: string[] = []
  const source = (selector?: string): SourceRef => ({ sessionID: selector ?? 'ses_current',
    transcript: `/tmp/controlled project/${selector ?? 'ses_current'}.json`, artifacts: '/tmp/controlled obs/artifacts',
    argv: ['--transcript', `/tmp/controlled project/${selector ?? 'ses_current'}.json`, '--artifacts', '/tmp/controlled obs/artifacts', '--window', '200000'] })
  const backend = {
    root: '/tmp/controlled obs/sessions', options: {}, ctx: { location: { directory: '/tmp/controlled project' } },
    runtime: { cache: '/tmp/controlled obs', async run<T>(argv: string[], _signal: AbortSignal, timeout?: number): Promise<T> {
      calls.push({ argv, timeout })
      return (responses[argv[0]!] ?? { path: '/tmp/output.json', bytes: 10, text: 'Bridge result' }) as T
    } },
    async session(_id: string, selector?: string) { selections.push(selector ?? 'ses_current'); return source(selector) },
    async resolve(_id: string, selector?: string) { return { sessionID: selector ?? 'ses_current' } },
    async snapshot() { return { schema_version: 1, empty: true as const, reason: 'No assistant steps', transcript: {} } },
    async sessions() { return [{ id: 'ses_current', title: 'Controlled session' }] },
    async serve(kind: string, argv: string[]) { served.push({ kind, argv }); return { url: 'http://127.0.0.1:7860/', preloaded: true } },
    async stop(...args: unknown[]) { stopped.push(args); return true },
  } as unknown as Backend
  const execute = (args: string) => runCommand(backend, { sessionID: 'ses_current', args, runID: 'run_test' }, new AbortController().signal)
  return { backend, calls, served, stopped, selections, execute }
}

describe('OpenCode obs command contract', () => {
  test('rejects ignored flags, values, extra positionals and malformed paging before work', async () => {
    for (const args of ['overview extra', 'view health --unused', 'json --view', 'bundle -o', 'pane close extra', 'watch on extra', 'statusline off --session s', 'cancel now', 'view health --json=false', 'view health --limit 501', 'view health --offset -1', 'sessions 0', 'dashboard --port 0', 'dashboard stop --port 7860', 'converge --data-root', 'label --model']) {
      const { execute, calls, served, selections } = fixture()
      const result = await execute(args)
      expect(result.text).not.toBe('Bridge result')
      expect(calls).toEqual([]); expect(served).toEqual([]); expect(selections).toEqual([])
    }
    expect(validateArguments('cancel')).toBeUndefined()
    expect(validateArguments('watch off')).toBeUndefined()
    expect(validateArguments('pane --session ses_other')).toBeUndefined()
    expect(validateArguments('isssues')).toContain("Did you mean 'issues'")
    expect(pageArguments({ limit: '500', offset: '0' })).toEqual(['--limit', '500', '--offset', '0'])
    expect(parseExtras('app,charts,app,llm')).toEqual(['app', 'charts'])
    expect(parseExtras('typo')).toBeUndefined()
  })

  test('help and SDK session listing need no Python runtime', async () => {
    const { execute, calls } = fixture()
    expect((await execute('help')).text).toContain('OpenCode 2')
    expect((await execute('sessions 20')).text).toContain('* ses_current')
    expect(calls).toEqual([])
  })

  test('diagnostic shortcuts and full views preserve paging, options and selection', async () => {
    for (const [sub, view] of Object.entries({ issues: 'issues', health: 'health', patterns: 'hotspots', agents: 'agents', context: 'context_pressure', timeline: 'workflow' })) {
      const { execute, calls } = fixture({ view: { view: { id: view, title: 'View title' }, path: '/tmp/view.json', rows_total: 3, inline: { table: 'Full evidence' } } })
      const result = await execute(`${sub} --session ses_other --limit 2 --offset 1 --json`)
      expect(result.sessionID).toBe('ses_other')
      expect(result.text).toContain('Full evidence')
      expect(calls[0]!.argv).toEqual(['view', '--transcript', '/tmp/controlled project/ses_other.json', '--artifacts', '/tmp/controlled obs/artifacts', '--window', '200000', view, '--limit', '2', '--offset', '1', '--json'])
    }
    const { execute, calls } = fixture({ view: { view: { id: 'context_pressure', title: 'Context' }, path: '/tmp/view.json', rows_total: 1, inline: { data: { occupancy: 100 } } } })
    expect((await execute('view context_pressure agent=main --json')).text).toContain('"occupancy": 100')
    expect(calls[0]!.argv).toContain('agent=main')
  })

  test('export arguments remain literal and bundle excludes unsupported window flags', async () => {
    const { execute, calls } = fixture()
    await execute('json -o "/tmp/export $(literal)" --view health --view issues --with-trajectory')
    expect(calls[0]!.argv.slice(-7)).toEqual(['--out', '/tmp/export $(literal)', '--view', 'health', '--view', 'issues', '--with-trajectory'])
    await execute('bundle -o "/tmp/export bundle.zip"')
    expect(calls[1]!.argv).toEqual(['bundle', '--transcript', '/tmp/controlled project/ses_current.json', '--artifacts', '/tmp/controlled obs/artifacts', '--out', '/tmp/export bundle.zip'])
  })

  test('dashboard receives exact session and context; stop stays scoped to that session', async () => {
    const { execute, served, stopped } = fixture()
    const result = await execute('dashboard --session ses_other --port 7860')
    expect(result.url).toBe('http://127.0.0.1:7860/')
    expect(served[0]!.argv).toEqual(['dashboard', '--transcript', '/tmp/controlled project/ses_other.json', '--window', '200000', '--port', '7860'])
    await execute('dashboard stop')
    expect(stopped).toEqual([['dashboard', 'ses_current']])
  })

  test('comparison and scorecard resolve each OpenCode source through the backend', async () => {
    const { execute, calls, selections } = fixture()
    await execute('compare ses_other --anchor-patch "/tmp/patch file.diff" --fuzzy-commands --reverse')
    expect(selections).toEqual(['ses_current', 'ses_other'])
    expect(calls[0]!.argv.slice(-6)).toEqual(['--other', '/tmp/controlled project/ses_other.json', '--anchor-patch', '/tmp/patch file.diff', '--fuzzy-commands', '--reverse'])
    await execute('scorecard a b --labels first,second --no-self')
    expect(calls[1]!.argv.slice(-6)).toEqual(['--others', '/tmp/controlled project/a.json', '/tmp/controlled project/b.json', '--labels', 'first,second', '--no-self'])
  })

  test('passthrough commands retain arguments and labels use the OpenCode artifact namespace', async () => {
    const { execute, calls } = fixture({ collect: { exit: 0, stdout: 'Collector help', stderr: '' }, 'converge-cli': { exit: 0, text: 'Converge help', path: null } })
    expect((await execute('collect -- cursor --help')).text).toBe('Collector help')
    expect(calls[0]!.argv).toEqual(['collect', '--', 'cursor', '--help'])
    expect((await execute('converge-cli -- --help')).text).toBe('Converge help')
    expect(calls[1]!.argv).toEqual(['converge-cli', '--artifacts', '/tmp/controlled obs/converge', '--', '--help'])
    await execute('labels "/tmp/one labels.json" "/tmp/two labels.json"')
    expect(calls[2]!.argv).toEqual(['labels', '--sidecar', '/tmp/one labels.json', '--artifacts', '/tmp/controlled obs/label-views', '--other', '/tmp/two labels.json'])
  })
})
