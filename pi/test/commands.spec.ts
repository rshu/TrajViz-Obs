import { afterAll, beforeAll, describe, expect, test } from 'bun:test'
import { execFile } from 'node:child_process'
import { existsSync } from 'node:fs'
import { link, mkdtemp, mkdir, readFile, rm, symlink, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import path from 'node:path'
import type { ExtensionContext } from '@earendil-works/pi-coding-agent'
import type { Backend, Source } from '../src/backend.js'
import { runCommand, type Actions } from '../src/commands.js'
import { Usage, usageFeature, PLUGIN_VERSION } from '../src/telemetry.js'
import { SNAPSHOT } from '../../hooks/testdata/snapshot.js'

let root: string
beforeAll(async () => { root = await mkdtemp(path.join(tmpdir(), 'trajviz-pi-commands-')) })
afterAll(async () => { await rm(root, { recursive: true, force: true }) })
const signal = () => new AbortController().signal
const bridge = path.resolve(import.meta.dir, '../../bridge')
const python = process.env.TRAJVIZ_OBS_TEST_PYTHON ?? path.join(bridge, '.venv/bin/python')
if (process.env.TRAJVIZ_OBS_TEST_PYTHON && !existsSync(python)) throw new Error('TRAJVIZ_OBS_TEST_PYTHON must name an installed bridge interpreter.')
const bridgeTest = existsSync(python) ? test : test.skip
const nativeRows = () => [
  { type: 'session', version: 3, id: 'pi-controlled', cwd: '/controlled', timestamp: '2026-11-18T00:00:00.000Z',
    _trajviz_obs_pi: { schema_version: 1, leaf_id: 'answer', context_window: 200000, context_tokens: 102 } },
  { type: 'message', id: 'user', parentId: null, timestamp: '2026-11-18T00:00:01.000Z',
    message: { role: 'user', content: 'Controlled coding task', timestamp: 1794960001000 } },
  { type: 'message', id: 'answer', parentId: 'user', timestamp: '2026-11-18T00:00:02.000Z', message: {
    role: 'assistant', api: 'openai-completions', provider: 'openai', model: 'controlled', timestamp: 1794960001500,
    durationMs: 500, content: [{ type: 'text', text: 'Controlled answer' }], stopReason: 'stop',
    usage: { input: 100, output: 2, cacheRead: 0, cacheWrite: 0, reasoning: 0, totalTokens: 102,
      cost: { input: .01, output: .002, cacheRead: 0, cacheWrite: 0, total: .012 } },
  } },
]

type Run = (argv: string[], abort?: AbortSignal) => Promise<Record<string, unknown>>
async function harness(execute?: Run) {
  const dir = await mkdtemp(path.join(root, 'case-'))
  const transcript = path.join(dir, 'session.json')
  await writeFile(transcript, JSON.stringify(nativeRows()))
  const artifact = path.join(dir, 'artifacts')
  await mkdir(artifact)
  const calls: string[][] = [], controls: unknown[][] = [], sources: (string | undefined)[] = [], served: unknown[][] = [], stopped: (string | undefined)[] = [], progress: string[] = []
  let watch = true, statusline = true, closed = 0, streams = 0, confirmations = 0, reply: unknown
  let planCalls = 1, confirm = true, modelError: string | undefined
  const source: Source = { sessionID: 'pi-controlled', transcript, artifacts: artifact,
    argv: ['--transcript', transcript, '--artifacts', artifact, '--kind', 'current'] }
  const context = {
    cwd: dir, hasUI: true, model: { provider: 'controlled', id: 'model', contextWindow: 200000 },
    sessionManager: { getSessionId: () => 'pi-controlled', getSessionFile: () => undefined },
    modelRegistry: { find: (provider: string, id: string) => provider === 'controlled' && id === 'other' ? { provider, id } : undefined,
      streamSimple: (_model: unknown, _context: unknown, options: { signal: AbortSignal }) => {
        streams++; options.signal.throwIfAborted()
        return { result: async () => ({ stopReason: modelError ? 'error' : 'stop', errorMessage: modelError,
          content: [{ type: 'text', text: 'Controlled model reply' }], usage: { input: 4, output: 2 } }) }
      } },
    ui: { confirm: async () => { confirmations++; return confirm } },
  } as unknown as ExtensionContext
  const backend = {
    cwd: dir, root: path.join(dir, 'sessions'), options: { llm: 'session' },
    runtime: { cache: dir, bridge,
      ready: async (extras: string[]) => ({ python, source: 'agent-trajviz==0.7.1', extras }),
      run: async (argv: string[], abort?: AbortSignal) => {
        abort?.throwIfAborted(); calls.push(argv)
        if (execute) return execute(argv, abort)
        if (argv[0] === 'llm-prompt') return { hash: 'controlled-hash', plan: { calls: planCalls, approx_input_tokens: 123 },
          prompts: Array.from({ length: planCalls }, (_, index) => ({ index, system: 'Controlled system', prompt: 'Controlled prompt', max_tokens: 20, timeout_s: 2 })), path: path.join(dir, 'plan.json') }
        if (argv[0] === 'llm-apply') {
          reply = JSON.parse(await readFile(argv[argv.indexOf('--replies') + 1]!, 'utf8'))
          return modelError ? { reply: 'Analysis incomplete', errors: ['controlled provider failure'] } : { reply: 'Controlled diagnosis', path: path.join(dir, 'analysis.json') }
        }
        if (argv[0] === 'view') return { view: { title: 'Controlled view' }, rows_total: 2,
          inline: { table: 'Evidence: controlled steps' }, path: path.join(artifact, 'views/controlled.json') }
        return { text: 'Controlled complete', path: path.join(artifact, 'result.json') }
      } },
    source: async (_ctx: unknown, selector?: string) => { sources.push(selector); return source },
    snapshot: async () => structuredClone(SNAPSHOT),
    sessions: async () => [{ id: 'pi-controlled', name: 'Coding task' }, { id: 'pi-other', firstMessage: 'Another task' }],
    stop: async (key?: string) => { stopped.push(key); return true },
    serve: async (...args: unknown[]) => { served.push(args); return { url: 'http://127.0.0.1:7891/' } },
  } as unknown as Backend
  const actions: Actions = {
    watch(value) { if (value !== undefined) watch = value; return watch },
    statusline(value) { if (value !== undefined) statusline = value; return statusline },
    closePane() { closed++ }, progress(text) { progress.push(text) },
  }
  const usage = { control: async (...args: unknown[]) => { controls.push(args); return 'Telemetry preference saved.' },
    dashboardArgs: () => ['--usage-adapter', 'pi', '--usage-version', '0.1.0'] } as unknown as Usage
  return { dir, source, backend, context, calls, sources, controls, served, stopped, progress, actions, usage,
    state: () => ({ watch, statusline, closed, streams, confirmations, reply }),
    modelPlan(calls: number, accepted = true) { planCalls = calls; confirm = accepted },
    modelFailure(error: string) { modelError = error },
    run: (input: string, abort = signal()) => runCommand(backend, context, input, abort, actions, usage) }
}

const invokeBridge: Run = (argv, abort) => new Promise((resolve, reject) => {
  execFile(python, ['-I', path.join(bridge, 'trajviz_obs_cli.py'), ...argv], { signal: abort, maxBuffer: 16 * 1024 * 1024 }, (error, stdout, stderr) => {
    let doc: Record<string, unknown>
    try { doc = JSON.parse(stdout) } catch { reject(new Error(stderr || stdout || String(error))); return }
    if (error || doc.error) reject(new Error(JSON.stringify(doc.error ?? stderr))); else resolve(doc)
  })
})

describe('Pi obs commands', () => {
  test('only native live dashboards receive trusted Prompt Explorer arguments, excluding imports, exports and comparisons', async () => {
    const keys = ['TRAJVIZ_OBS_PROMPT_CAPTURE', 'TRAJVIZ_OBS_PROMPT_CAPTURE_DIR'] as const
    const previous = keys.map(key => [key, process.env[key]] as const)
    try {
      process.env.TRAJVIZ_OBS_PROMPT_CAPTURE = '1'
      process.env.TRAJVIZ_OBS_PROMPT_CAPTURE_DIR = '/private/prompt-captures'
      const h = await harness()
      h.source.liveTranscript = path.join(h.dir, 'live.json')
      await h.run('dashboard')
      expect((h.served[0]![1] as string[])).toContain('--prompt-capture-root')
      expect((h.served[0]![1] as string[])).toContain('pi-controlled')
      await h.run('dashboard --html /tmp/pi-dashboard.html')
      await h.run('json')
      await h.run('bundle')
      await h.run('converge --data-root /tmp/pi-comparison')
      delete h.source.liveTranscript
      h.source.captureSessionID = 'imported-session'
      await h.run('dashboard --session /tmp/imported.json')
      for (const call of h.calls) expect(call).not.toContain('--prompt-capture-root')
      for (const served of h.served.slice(1)) expect((served[1] as string[])).not.toContain('--prompt-capture-root')
    } finally {
      for (const [key, value] of previous) if (value === undefined) delete process.env[key]; else process.env[key] = value
    }
  })

  test('private captures cannot let exports overwrite the original live Pi file or its aliases', async () => {
    const h = await harness(), original = path.join(h.dir, 'actual-pi-session.jsonl')
    await writeFile(original, 'Original native Pi session bytes')
    h.context.sessionManager.getSessionFile = () => original
    const aliases = [original, path.join(h.dir, 'native-hardlink'), path.join(h.dir, 'native-symlink')]
    await link(original, aliases[1]!); await symlink(original, aliases[2]!)
    for (const output of aliases) for (const command of [`bundle -o "${output}"`, `dashboard --html "${output}"`, `label -o "${output}"`, `collect pi "${output}"`]) {
      await expect(h.run(command)).rejects.toThrow('overwrite input session file')
      expect(await readFile(original, 'utf8')).toBe('Original native Pi session bytes')
    }
    expect(h.calls).toEqual([]); expect(h.served).toEqual([]); expect(h.state().streams).toBe(0)
  })
  test('the optional browser opener uses a literal URL and retains it when opening fails', async () => {
    const h = await harness(), opened: string[][] = []
    h.backend.options.openBrowser = true
    h.backend.runtime.execute = async <T>(argv: readonly string[]) => { opened.push([...argv]); return '' as T }
    expect((await h.run('dashboard')).url).toBe('http://127.0.0.1:7891/')
    expect(opened).toHaveLength(1)
    expect(opened[0]!.at(-1)).toBe('http://127.0.0.1:7891/')
    h.backend.runtime.execute = async () => { throw new Error('No browser available') }
    const result = await h.run('dashboard')
    expect(result.url).toBe('http://127.0.0.1:7891/')
    expect(result.text).toContain('could not be opened automatically')
  })
  test('overview and pane return their snapshot without starting a browser or model', async () => {
    const h = await harness()
    expect((await h.run('')).snapshot).toEqual(SNAPSHOT)
    expect((await h.run('pane')).pane).toBe(true)
    expect((await h.run('pane close')).text).toContain('closed')
    expect(h.state().closed).toBe(1)
    expect(h.calls).toEqual([]); expect(h.served).toEqual([]); expect(h.state().streams).toBe(0)
    expect((await h.run('help')).text).toContain('all 37 views')
    expect((await h.run('watch off')).text).toBe('watch off.')
    expect((await h.run('statusline off')).text).toBe('statusline off.')
    expect((await h.run('watch')).text).toBe('watch off.')
    expect((await h.run('cancel')).text).toContain('No other')
  })

  test('all diagnostic shortcuts show evidence with pagination and explicit source selection', async () => {
    const h = await harness()
    for (const command of ['issues', 'health', 'patterns', 'agents', 'context', 'timeline']) {
      const result = await h.run(`${command} --session recorded --limit 4 --offset 1 --json`)
      expect(result.text).toContain('Controlled view · 2 rows')
      expect(result.text).toContain('Evidence: controlled steps')
      const args = h.calls.at(-1)!
      expect(args).toContain('--json'); expect(args).toContain('--limit'); expect(args).toContain('4')
      expect(h.sources.at(-1)).toBe('recorded')
    }
    expect((await h.run('view hotspots limit=3')).path).toContain('/views/')
    expect(h.calls.at(-1)).toContain('--option'); expect(h.calls.at(-1)).toContain('limit=3')
  })

  test('session listing marks the current session, limits results, and supports imported captures', async () => {
    const h = await harness()
    const list = await h.run('sessions 1')
    expect(list.text).toContain('* pi-controlled · Coding task')
    expect(list.text).not.toContain('pi-other')
    await h.run('sessions 2 --session imported.json')
    expect(h.calls.at(-1)?.[0]).toBe('capture-sessions')
    expect(h.calls.at(-1)).toContain('--limit')
  })

  test('exports, schemas, comparison, labels and attribution preserve their selected input', async () => {
    const h = await harness()
    for (const command of ['json --view summary --view metrics --views-only -o exports', 'bundle -o portable.zip', 'schemas schemas',
      'compare other', 'scorecard first second --no-self', 'labels one.json two.json', 'brief',
      'attribution --reference-root reference --agent main --instance controlled', 'doctor', 'setup --extras charts,app']) {
      expect((await h.run(command)).text).not.toBeEmpty()
    }
    const json = h.calls.find(row => row[0] === 'json')!
    expect(json.filter(value => value === '--view')).toHaveLength(2)
    expect(h.calls.find(row => row[0] === 'bundle')).not.toContain('--kind')
    expect(h.sources).toContain('other'); expect(h.sources).toContain('first'); expect(h.sources).toContain('second')
    expect(h.calls.find(row => row[0] === 'scorecard')).toContain('--no-self')
    expect(h.calls.find(row => row[0] === 'labels')).toContain('--other')
    expect(h.calls.find(row => row[0] === 'attribution')).toContain('--reference-root')
  })

  test('dashboards receive telemetry options, stop matching servers and export HTML locally', async () => {
    const h = await harness()
    expect((await h.run('dashboard --port 7891 --dark --inline-plotly')).url).toBe('http://127.0.0.1:7891/')
    const [key, argv, telemetry] = h.served[0] as [string, string[], string[]]
    expect(key).toBe('dashboard:pi-controlled')
    expect(argv).toContain('--dark'); expect(argv).toContain('--inline-plotly'); expect(argv).toContain('7891')
    expect(telemetry).toContain('pi')
    expect((await h.run('dashboard stop')).text).toBe('Dashboard stopped.')
    expect(h.stopped).toContain('dashboard:pi-controlled')
    await h.run('dashboard --html offline.html')
    expect(h.calls.at(-1)).toContain('--html'); expect(h.served).toHaveLength(1)
    expect((await h.run('converge')).url).toContain('127.0.0.1')
    await h.run('converge stop')
    expect(h.stopped).toContain('converge')
  })

  test('invalid requests never invoke a model, backend command, or browser', async () => {
    const h = await harness()
    for (const input of ['nonsense', 'report', 'pane unsupported', 'view metrics invalid', 'issues --limit 0', 'issues --offset -1',
      'view --limit 501', 'sessions 0', 'sessions 501', 'compare', 'compare one two', 'scorecard', 'labels', 'labels one two three',
      'json --views-only --with-trajectory', 'json -o', 'dashboard --html copy.html --port 7891', 'dashboard --port 65536',
      'dashboard port 0', 'telemetry_destination default --use-launch-token', 'watch maybe', 'statusline off extra', 'help --session other']) {
      expect((await h.run(input)).outcome).toBe('rejected')
    }
    expect(h.calls).toHaveLength(0); expect(h.served).toHaveLength(0); expect(h.state().streams).toBe(0)
    const cancelled = new AbortController(); cancelled.abort()
    await expect(h.run('issues', cancelled.signal)).rejects.toThrow()
  })

  test('collection supports native JSONL, preserves exact source, and refuses aliases of source outputs', async () => {
    const h = await harness()
    const text = nativeRows().map(row => JSON.stringify(row)).join('\n') + '\n'
    await writeFile(h.source.transcript, text)
    const before = await readFile(h.source.transcript)
    const result = await h.run('collect pi controlled.jsonl')
    expect(result.path).toBe(path.join(h.dir, 'controlled.jsonl'))
    expect(await readFile(result.path!, 'utf8')).toBe(text)
    expect((await h.run('collect pi -')).text).toBe(text)
    await expect(h.run(`collect pi ${h.source.transcript}`)).rejects.toThrow('overwrite')
    for (const [name, create] of [['hardlink', link], ['symlink', symlink]] as const) {
      const destination = path.join(h.dir, name)
      await create(h.source.transcript, destination)
      await expect(h.run(`collect pi ${destination}`)).rejects.toThrow('overwrite')
    }
    expect(await readFile(h.source.transcript)).toEqual(before)
  })

  test('external collector and comparison CLI failures remain visible', async () => {
    const h = await harness(async () => ({ stdout: 'Controlled collector failure', exit: 2 }))
    expect((await h.run('collect cursor output.jsonl')).outcome).toBe('error')
    expect((await h.run('converge-cli -- --help')).text).toBe('Controlled collector failure')
    expect(h.calls.at(-1)).toContain('--artifacts')
    expect((await h.run('collect')).text).toContain('collect pi')
  })
})

describe('Pi native model calls', () => {
  test('analyze, fixes and label apply replies against the same plan and keep accounting separate', async () => {
    const h = await harness()
    for (const input of ['analyze Why is this slow?', 'fixes --limit 2', 'label --max-steps 3 --assistant-only --model controlled/other']) {
      const result = await h.run(input)
      expect(result.text).toContain('Controlled diagnosis')
      expect(result.path).toContain('analysis.json')
      const replies = h.state().reply as { hash: string; replies: unknown[]; usage: unknown[] }
      expect(replies.hash).toBe('controlled-hash')
      expect(replies.replies).toEqual([{ index: 0, text: 'Controlled model reply' }])
      expect(replies.usage).toEqual([{ input: 4, output: 2 }])
    }
    expect(h.state().streams).toBe(3)
    expect(h.calls.some(row => row.includes('Why is this slow?'))).toBe(true)
    expect(h.calls.find(row => row.includes('--assistant-only'))).toContain('--max-steps')
  })

  test('large plans require consent and cancelled plans make no provider request', async () => {
    const h = await harness(); h.modelPlan(5, false)
    expect((await h.run('analyze')).outcome).toBe('cancelled')
    expect(h.state().confirmations).toBe(1); expect(h.state().streams).toBe(0)
    ;(h.context as unknown as { hasUI: boolean }).hasUI = false
    expect((await h.run('fixes')).outcome).toBe('confirmation')
    expect(h.state().streams).toBe(0)
  })

  test('provider errors are applied as errors instead of invented successful diagnoses', async () => {
    const h = await harness(); h.modelFailure('Controlled provider error')
    expect((await h.run('analyze')).outcome).toBe('error')
    const replies = h.state().reply as { replies: { error: string }[] }
    expect(replies.replies[0]?.error).toBe('Controlled provider error')
    expect(h.progress.at(-1)).toContain('Running call 1 of 1')
  })

  test('a changed plan or missing selected model is rejected before provider calls', async () => {
    const h = await harness()
    ;(h.context as unknown as { model: undefined }).model = undefined
    await expect(h.run('analyze')).rejects.toThrow('Select a Pi model')
    expect(h.state().streams).toBe(0)
    const altered = await harness(async () => ({ hash: 'hash', plan: { calls: 2, approx_input_tokens: 10 }, prompts: [], path: '' }))
    await expect(altered.run('analyze')).rejects.toThrow('plan changed')
    expect(altered.state().streams).toBe(0)
  })

  test('cancelling generation never applies partial replies or starts a second provider call', async () => {
    const h = await harness(), controller = new AbortController()
    h.modelPlan(2)
    const registry = h.context.modelRegistry as unknown as { streamSimple: () => { result(): Promise<never> } }
    let calls = 0
    registry.streamSimple = () => ({ result: async () => { calls++; controller.abort(); throw new Error('Cancelled') } })
    await expect(h.run('analyze', controller.signal)).rejects.toThrow()
    expect(calls).toBe(1)
    expect(h.calls.some(row => row[0] === 'llm-apply')).toBe(false)
  })

  test('environment mode delegates to the Python provider without using Pi model credentials', async () => {
    const h = await harness()
    h.backend.options.llm = 'env'
    ;(h.context as unknown as { model: undefined }).model = undefined
    expect((await h.run('analyze')).text).toContain('Controlled complete')
    expect(h.calls.at(-1)?.[0]).toBe('ask')
    expect(h.state().streams).toBe(0)
  })
})

describe('Pi telemetry preferences and delivery', () => {
  test('feature names exclude administration and user arguments', () => {
    expect(usageFeature('analyze Private prompt /Users/person/repo')).toBe('analyze')
    expect(usageFeature('dashboard --html private-name.html')).toBe('dashboard.html')
    expect(usageFeature('dashboard stop')).toBe('dashboard.stop')
    expect(usageFeature('pane close')).toBe('pane.close')
    for (const input of ['enable_telemetry', 'disable_telemetry', 'telemetry', 'telemetry_destination https://private.example', 'dashboard port 7891', 'unknown']) expect(usageFeature(input)).toBeUndefined()
  })

  test('commands call consent and destination controls without touching session data', async () => {
    const h = await harness()
    for (const input of ['telemetry', 'enable_telemetry', 'disable_telemetry', 'telemetry_destination', 'telemetry_destination https://telemetry.example.com --use-launch-token']) await h.run(input)
    expect(h.controls.map(row => row[0])).toEqual(['status', 'on', 'off', 'destination', 'destination'])
    expect(h.controls.at(-1)?.slice(2)).toEqual(['https://telemetry.example.com', true])
    expect(h.sources).toHaveLength(0); expect(h.calls).toHaveLength(0)
  })

  bridgeTest('opt-in sends allowlisted Pi usage to a local collector and opt-out stops recording', async () => {
    const state = await mkdtemp(path.join(root, 'telemetry-'))
    const names = ['TRAJVIZ_OBS_TELEMETRY_STATE_DIR', 'TRAJVIZ_OBS_TELEMETRY_URL', 'TRAJVIZ_OBS_TELEMETRY', 'TRAJVIZ_OBS_TELEMETRY_TOKEN'] as const
    const old = Object.fromEntries(names.map(name => [name, process.env[name]]))
    const batches: { adapter: string; events: { feature: string; outcome: string; version: string; agent_version?: string }[] }[] = []
    const server = createServer((request, response) => {
      let body = ''; request.on('data', chunk => { body += String(chunk) }); request.on('end', () => {
        batches.push(JSON.parse(body)); response.writeHead(204, { 'TrajViz-Obs-Telemetry-Schema': '2' }); response.end()
      })
    })
    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
    const port = (server.address() as { port: number }).port
    try {
      process.env.TRAJVIZ_OBS_TELEMETRY_STATE_DIR = state
      process.env.TRAJVIZ_OBS_TELEMETRY_URL = `http://127.0.0.1:${port}/v1/events`
      process.env.TRAJVIZ_OBS_TELEMETRY = '0'; delete process.env.TRAJVIZ_OBS_TELEMETRY_TOKEN
      const usage = new Usage({ bridge, telemetryPython: () => python }, '1.5.2')
      expect(usage.enabled()).toBe(false); expect(usage.dashboardArgs()).toEqual([])
      expect(await usage.control('on', signal())).toContain('On')
      expect(usage.dashboardArgs()).toContain('pi')
      usage.report({ feature: 'activation', surface: 'terminal', outcome: 'success', durationMs: 0 })
      await usage.flush()
      const waitForDelivery = async (count: number) => {
        const deadline = Date.now() + 8000
        while (Date.now() < deadline) {
          const received = batches.flatMap(batch => batch.events).filter(event => event.feature === 'view').length
          if (batches.length && received >= count && (await usage.control('status', signal())).includes('Queued events · 0 / 512')) return
          await new Promise(resolve => setTimeout(resolve, 25))
        }
        throw new Error(`Collector delivery was not acknowledged for ${count} view events`)
      }
      // Receipt precedes the durable ACK. Wait for both before starting the next
      // bounded sender, including the activation that negotiates agent-version support.
      await waitForDelivery(0)
      let count = 0
      for (const outcome of ['success', 'error', 'cancelled', 'empty', 'rejected', 'confirmation'] as const) {
        usage.report({ feature: 'view', surface: 'terminal', outcome, durationMs: 125 })
        await usage.flush()
        await waitForDelivery(++count)
      }
      const events = batches.flatMap(batch => batch.events).filter(event => event.feature === 'view')
      expect(new Set(events.map(event => event.outcome))).toEqual(new Set(['success', 'error', 'cancelled', 'empty', 'rejected', 'confirmation']))
      expect(batches.every(batch => batch.adapter === 'pi')).toBe(true)
      expect(events.every(event => event.version === PLUGIN_VERSION && event.agent_version === '1.5.2')).toBe(true)
      expect(await usage.control('off', signal())).toContain('Off')
      const delivered = batches.length
      usage.report({ feature: 'overview', surface: 'terminal', outcome: 'success', durationMs: 1 })
      await usage.flush(); await new Promise(resolve => setTimeout(resolve, 100))
      expect(usage.enabled()).toBe(false); expect(batches).toHaveLength(delivered)
      expect(JSON.stringify(batches)).not.toContain(root)
    } finally {
      await new Promise<void>(resolve => server.close(() => resolve()))
      for (const name of names) if (old[name] === undefined) delete process.env[name]; else process.env[name] = old[name]
    }
  }, 30000)
})

describe('Pi commands through the real shared bridge', () => {
  bridgeTest('catalog, views, export/reload, portable bundle and comparison use the native Pi capture', async () => {
    const h = await harness(invokeBridge)
    const before = await readFile(h.source.transcript)
    const catalog = await h.run('view')
    expect(catalog.text).toContain('37 TrajViz views')
    for (const input of ['issues', 'health', 'patterns', 'agents', 'context', 'timeline', 'brief', 'doctor', 'attribution']) {
      const result = await h.run(input)
      expect(result.text).not.toBeEmpty(); expect(result.outcome).not.toBe('error')
    }
    const exported = await h.run(`json -o ${path.join(h.dir, 'export')}`)
    const doc = exported.data as { path?: string; files?: Record<string, string> }
    expect(doc).toBeDefined()
    const bundle = await h.run(`bundle -o ${path.join(h.dir, 'session.zip')}`)
    const loaded = await invokeBridge(['snapshot', '--transcript', bundle.path!])
    expect((loaded.tokens as { total: number }).total).toBe(102)
    expect((loaded.context as { window: number; current_occupancy: number }).window).toBe(200000)
    expect((loaded.timing as { avg_step_s: number }).avg_step_s).toBe(.5)
    const comparison = await h.run(`compare ${h.source.transcript}`)
    expect(comparison.data).toBeDefined()
    expect(await readFile(h.source.transcript)).toEqual(before)
    await expect(h.run(`bundle -o ${h.source.transcript}`)).rejects.toThrow()
    await expect(h.run(`dashboard --html ${h.source.transcript}`)).rejects.toThrow()
    expect(await readFile(h.source.transcript)).toEqual(before)
  }, 30000)
})
