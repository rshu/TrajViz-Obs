import { expect, test } from 'claude-code/testing'
import type { EngineInterface, ProcessSpawnChunk, ProcessSpawnRequest, ProcessSpawnResult, StateAddress } from 'claude-code'
import { LineBuffer, START_TIMEOUT_MS, hasServer, parseUrlLine, portOf, resetServers, running, startServer, startServerFrom, stopAll, stopServer, type ServerStream } from './lib/servers'

type Step = IteratorResult<ProcessSpawnChunk, ProcessSpawnResult>

/** A stream the test feeds: `out`/`err` deliver a chunk, `exit` ends it; `return()` ends it as the engine's kill does. */
function fakeStream() {
  const queue: Step[] = []
  let waiting: ((r: Step) => void) | null = null
  let returned = false
  const deliver = (r: Step) => {
    if (waiting) {
      const w = waiting
      waiting = null
      w(r)
    } else queue.push(r)
  }
  const killed: ProcessSpawnResult = { code: null, signal: 'SIGTERM' }
  const stream: ServerStream = {
    next: () =>
      new Promise<Step>(resolve => {
        const q = queue.shift()
        if (q) resolve(q)
        else waiting = resolve
      }),
    return: async () => {
      returned = true
      deliver({ value: killed, done: true })
      return { value: killed, done: true }
    },
    throw: async (e: unknown) => {
      throw e
    },
    [Symbol.asyncIterator]() {
      return this
    },
  } as ServerStream
  return {
    stream,
    out: (text: string) => deliver({ value: { stream: 'stdout', text }, done: false }),
    err: (text: string) => deliver({ value: { stream: 'stderr', text }, done: false }),
    exit: (code: number) => deliver({ value: { code, signal: null }, done: true }),
    get returned() {
      return returned
    },
  }
}

type Fake = { $: EngineInterface; spawned: ProcessSpawnRequest[]; state: Map<string, { value: unknown; version: number }>; timers: { ms: number; cancelled: boolean; fire: () => void }[] }

const CWD = '/Users/me/p'
const stateKey = (ref: StateAddress): string => (ref.id === undefined ? ref.key : `${ref.key}/${ref.id}`)

/** A `$` over an in-memory host: state, a session, and `process.spawn` answering the streams of `streams` in turn. */
function fake(streams: ServerStream[] = []): Fake {
  const spawned: ProcessSpawnRequest[] = []
  const state = new Map<string, { value: unknown; version: number }>()
  const timers: Fake['timers'] = []
  const $ = {
    clock: {
      after: (ms: number, fn: () => void) => {
        const timer = { ms, cancelled: false, fire: () => { if (!timer.cancelled) fn() } }
        timers.push(timer)
        return { cancel: () => { timer.cancelled = true } }
      },
    },
    session: { cwd: async () => CWD },
    process: {
      spawn: (request: ProcessSpawnRequest) => {
        spawned.push(request)
        const s = streams.shift()
        if (!s) throw new Error(`no stream for ${request.argv.join(' ')}`)
        return s
      },
    },
    state: {
      get: async (ref: StateAddress) => {
        const e = state.get(stateKey(ref))
        return { value: e?.value, version: e?.version ?? 0 }
      },
      set: async (ref: StateAddress, value: unknown, options?: { ifVersion?: number }) => {
        const k = stateKey(ref)
        const current = state.get(k)?.version ?? 0
        if (options?.ifVersion !== undefined && options.ifVersion !== current) return { isSet: false, version: current }
        state.set(k, { value, version: current + 1 })
        return { isSet: true, version: current + 1 }
      },
    },
  }
  return { $: $ as unknown as EngineInterface, spawned, state, timers }
}

const URL_LINE = '{"url": "http://127.0.0.1:7860"}\n'
const dashboards = (f: Fake) => f.state.get('dashboards')?.value

/** Lets the detached loop run its pending steps (the runtime has no timers; microtask turns are enough). */
async function settle(): Promise<void> {
  for (let i = 0; i < 20; i++) await Promise.resolve()
}

/** A host with no dashboard left over from an earlier test (the map is the module's). */
async function fresh(streams: ServerStream[] = []): Promise<Fake> {
  const f = fake(streams)
  await stopAll(f.$)
  return f
}

test('LineBuffer answers whole lines across split chunks', () => {
  const buf = new LineBuffer()
  expect(buf.push('{"ur')).toEqual([])
  expect(buf.push('l": "x"}\nwarn')).toEqual(['{"url": "x"}'])
  expect(buf.push('ing\r\nsecond\nthird')).toEqual(['warning', 'second'])
  expect(buf.flush()).toBe('third')
  expect(buf.flush()).toBeNull()
  expect(buf.push('\n')).toEqual([''])
  expect(buf.push('x'.repeat(100_000))).toEqual([])
  expect(buf.flush()?.length).toBe(65_536)
})

test('concurrent starts share one child and stopping before its URL cancels it', async () => {
  const a = fakeStream()
  const f = await fresh([a.stream])
  const first = startServer(f.$, 'insight', ['x'])
  const second = startServer(f.$, 'insight', ['x'])
  await settle()
  expect(f.spawned).toHaveLength(1)
  await stopServer(f.$, 'insight')
  expect(await first).toEqual({ error: 'the dashboard start was cancelled' })
  expect(await second).toEqual({ error: 'the dashboard start was cancelled' })
  expect(a.returned).toBe(true)
  expect(running('insight')).toBeNull()
})

test('stopping while the host is resolving cwd cancels a start before it spawns a child', async () => {
  const s = fakeStream()
  const f = await fresh([s.stream])
  let release: () => void = () => {}
  const gate = new Promise<void>(resolve => { release = resolve })
  f.$.session.cwd = async () => { await gate; return CWD }
  const pending = startServer(f.$, 'insight', ['x'])
  expect(hasServer('insight')).toBe(true)
  await stopServer(f.$, 'insight')
  release()
  expect(await pending).toEqual({ error: 'the dashboard start was cancelled' })
  expect(f.spawned).toEqual([])
  expect(hasServer('insight')).toBe(false)
})

test('a startup timeout during dashboard state publication cannot leave a dead URL in state', async () => {
  const s = fakeStream()
  const f = await fresh([s.stream])
  const set = f.$.state.set.bind(f.$.state)
  let release: () => void = () => {}
  const gate = new Promise<void>(resolve => { release = resolve })
  let storageStarted: () => void = () => {}
  const storage = new Promise<void>(resolve => { storageStarted = resolve })
  let first = true
  f.$.state.set = (async (ref: StateAddress, value: unknown, options?: { ifVersion?: number }) => {
    if (ref.key === 'dashboards' && first) { first = false; storageStarted(); await gate }
    return set(ref as never, value as never, options)
  }) as typeof f.$.state.set
  const pending = startServer(f.$, 'insight', ['x'])
  s.out(URL_LINE)
  await storage
  f.timers[0]?.fire()
  const outcome = await pending
  release()
  await settle()
  expect(outcome).toMatchObject({ error: 'the dashboard did not print its URL within 120 seconds: {"url": "http://127.0.0.1:7860"}' })
  expect(running('insight')).toBeNull()
  expect(dashboards(f)).toEqual({ insight: null, converge: null })
  expect(s.returned).toBe(true)
})

test('a silent dashboard startup times out and closes its child; a ready child cancels the timer', async () => {
  const s = fakeStream()
  const f = await fresh([s.stream])
  const pending = startServer(f.$, 'insight', ['x'])
  await settle()
  expect(f.timers[0]?.ms).toBe(START_TIMEOUT_MS)
  f.timers[0]?.fire()
  expect(await pending).toEqual({ error: 'the dashboard did not print its URL within 120 seconds' })
  await settle()
  expect(s.returned).toBe(true)
  expect(running('insight')).toBeNull()
  const ready = fakeStream()
  const next = startServerFrom(f.$, 'converge', ready.stream)
  ready.out(URL_LINE)
  await next
  expect(f.timers[1]?.cancelled).toBe(true)
  f.timers[1]?.fire()
  expect(running('converge')).toBe('http://127.0.0.1:7860')
  await stopAll(f.$)
})

test('an error envelope closes the failed child and releases registration before the start returns', async () => {
  const failed = fakeStream()
  const retry = fakeStream()
  const f = await fresh([failed.stream, retry.stream])
  const pending = startServer(f.$, 'insight', ['x'])
  failed.out('{"error":{"code":"port_busy","message":"busy"}}\n')
  expect(await pending).toEqual({ error: 'busy' })
  expect(failed.returned).toBe(true)
  expect(hasServer('insight')).toBe(false)
  const next = startServer(f.$, 'insight', ['x'])
  retry.out(URL_LINE)
  expect(await next).toEqual({ url: 'http://127.0.0.1:7860', preloaded: true })
  await stopAll(f.$)
})

test('parseUrlLine reads {"url": …} with its preloaded flag, and nothing else', () => {
  expect(parseUrlLine('{"url": "http://127.0.0.1:7860"}')).toEqual({ url: 'http://127.0.0.1:7860', preloaded: true })
  expect(parseUrlLine('  {"url":"http://127.0.0.1:7861/"}  ')).toEqual({ url: 'http://127.0.0.1:7861/', preloaded: true })
  expect(parseUrlLine('{"url": "http://127.0.0.1:7860", "preloaded": false}')).toEqual({ url: 'http://127.0.0.1:7860', preloaded: false })
  expect(parseUrlLine('{"url": "http://127.0.0.1:7860", "preloaded": true}')).toEqual({ url: 'http://127.0.0.1:7860', preloaded: true })
  expect(parseUrlLine('{"error": {"code": "missing_extra", "message": "no app"}}')).toBeNull()
  expect(parseUrlLine('{"url": 5}')).toBeNull()
  expect(parseUrlLine('Running on local URL: http://127.0.0.1:7860')).toBeNull()
  expect(parseUrlLine('')).toBeNull()
})

test('portOf reads the port of a URL', () => {
  expect(portOf('http://127.0.0.1:7860')).toBe(7860)
  expect(portOf('http://[::1]:7861/')).toBe(7861)
  expect(portOf('http://localhost/')).toBe(0)
  expect(portOf('')).toBe(0)
})

test('startServerFrom resolves on the URL line, records the dashboard, and stopServer ends it', async () => {
  const f = await fresh()
  const s = fakeStream()
  const started = startServerFrom(f.$, 'insight', s.stream)
  s.out('{"ur')
  s.out('l": "http://127.0.0.1:7860"}\n')
  expect(await started).toEqual({ url: 'http://127.0.0.1:7860', preloaded: true })
  expect(running('insight')).toBe('http://127.0.0.1:7860')
  expect(running('converge')).toBeNull()
  expect(dashboards(f)).toEqual({ insight: { url: 'http://127.0.0.1:7860', port: 7860 }, converge: null })
  // Later output (request logs, warnings) changes nothing.
  s.err('INFO: GET / 200\n')
  await settle()
  expect(running('insight')).toBe('http://127.0.0.1:7860')

  await stopServer(f.$, 'insight')
  expect(s.returned).toBe(true)
  expect(running('insight')).toBeNull()
  expect(dashboards(f)).toEqual({ insight: null, converge: null })
  // A second stop is nothing.
  await stopServer(f.$, 'insight')
  expect(dashboards(f)).toEqual({ insight: null, converge: null })
})

test('stderr before the URL is context, not a failure: the URL after it starts, an exit after it names it', async () => {
  const f = await fresh()
  // Gradio's launch prints and Python warnings reach stderr before the bridge's URL line.
  const s = fakeStream()
  const started = startServerFrom(f.$, 'insight', s.stream)
  s.err('DeprecationWarning: something in pydantic\n')
  s.err('Running on local URL:  http://127.0.0.1:7860\n')
  await settle()
  expect(s.returned).toBe(false)
  s.out('{"url": "http://127.0.0.1:7860", "preloaded": false}\n')
  expect(await started).toEqual({ url: 'http://127.0.0.1:7860', preloaded: false })
  expect(running('insight')).toBe('http://127.0.0.1:7860')
  expect(dashboards(f)).toEqual({ insight: { url: 'http://127.0.0.1:7860', port: 7860 }, converge: null })
  await stopServer(f.$, 'insight')

  // A child that dies after writing to stderr alone: the exit row carries its last line.
  const dead = fakeStream()
  const d = startServerFrom(f.$, 'insight', dead.stream)
  dead.err('Traceback (most recent call last):\n  File "x"\nOSError: [Errno 48] Address already in use\n')
  dead.exit(1)
  expect(await d).toEqual({ error: 'the dashboard exited with 1 before printing its URL: OSError: [Errno 48] Address already in use' })
  expect(running('insight')).toBeNull()
  expect(dashboards(f)).toEqual({ insight: null, converge: null })
})

test("the bridge's error envelope on stdout is the error, whatever stderr said before it", async () => {
  const f = await fresh()
  const s = fakeStream()
  const started = startServerFrom(f.$, 'converge', s.stream)
  s.out('{"error": {"code": "missing_extra", "message": "TrajViz\'s app extra is not installed", "extra": "app"}}\n')
  expect(await started).toEqual({ error: "TrajViz's app extra is not installed" })
  await settle()
  expect(s.returned).toBe(true)
  expect(running('converge')).toBeNull()

  // A busy --port: Gradio's complaint on stderr, then the bridge's port_busy envelope.
  const busy = fakeStream()
  const b = startServerFrom(f.$, 'insight', busy.stream)
  busy.err('OSError: [Errno 48] Address already in use\n')
  busy.out('{"error": {"code": "port_busy", "message": "Port 7860 on 127.0.0.1 is in use or cannot be bound; pass another --port, or none to take a free one.", "port": 7860}}\n')
  expect(await b).toEqual({ error: 'Port 7860 on 127.0.0.1 is in use or cannot be bound; pass another --port, or none to take a free one.' })
  await settle()
  expect(busy.returned).toBe(true)
  expect(running('insight')).toBeNull()
  expect(dashboards(f)).toBeUndefined()
})

test('an exit before the URL is the error, with the last line', async () => {
  const f = await fresh()
  const s = fakeStream()
  const started = startServerFrom(f.$, 'insight', s.stream)
  s.out('Traceback (most recent call last):\n  File "x"\nModuleNotFoundError: No module named gradio')
  s.exit(1)
  expect(await started).toEqual({ error: 'the dashboard exited with 1 before printing its URL: ModuleNotFoundError: No module named gradio' })
  expect(running('insight')).toBeNull()

  const quiet = fakeStream()
  const q = startServerFrom(f.$, 'insight', quiet.stream)
  quiet.exit(0)
  expect(await q).toEqual({ error: 'the dashboard exited with 0 before printing its URL' })
})

test('a stream that cannot start is the error', async () => {
  const f = await fresh()
  const stream = {
    next: async () => {
      throw new Error('spawn ENOENT')
    },
    return: async () => ({ value: { code: null, signal: null }, done: true as const }),
    throw: async (e: unknown) => {
      throw e
    },
    [Symbol.asyncIterator]() {
      return this
    },
  } as unknown as ServerStream
  expect(await startServerFrom(f.$, 'insight', stream)).toEqual({ error: 'the dashboard could not start: spawn ENOENT' })
  expect(running('insight')).toBeNull()
})

test('a child that dies after starting clears its entry and the state', async () => {
  const f = await fresh()
  const s = fakeStream()
  const started = startServerFrom(f.$, 'converge', s.stream)
  s.out(URL_LINE)
  expect(await started).toEqual({ url: 'http://127.0.0.1:7860', preloaded: true })
  expect(dashboards(f)).toEqual({ insight: null, converge: { url: 'http://127.0.0.1:7860', port: 7860 } })
  s.exit(0)
  await settle()
  expect(running('converge')).toBeNull()
  expect(dashboards(f)).toEqual({ insight: null, converge: null })
})

test('startServer spawns in the session directory, once per kind', async () => {
  const a = fakeStream()
  const b = fakeStream()
  const f = await fresh([a.stream, b.stream])
  const argv = ['/envs/0123456789abcdef01234567/0123456789abcdef0123456789abcdef/bin/python', '-I', '/plugin/bridge/trajviz_obs_cli.py', 'dashboard', '--transcript', '/t.jsonl']
  const started = startServer(f.$, 'insight', argv)
  a.out(URL_LINE)
  expect(await started).toEqual({ url: 'http://127.0.0.1:7860', preloaded: true })
  expect(f.spawned).toEqual([{ argv, cwd: CWD }])
  // Already running: the same URL, no second child.
  expect(await startServer(f.$, 'insight', argv)).toEqual({ url: 'http://127.0.0.1:7860', preloaded: true })
  expect(f.spawned).toHaveLength(1)
  // The other kind is its own child.
  const other = startServer(f.$, 'converge', ['x', 'converge'])
  b.out('{"url": "http://127.0.0.1:7861/"}\n')
  expect(await other).toEqual({ url: 'http://127.0.0.1:7861/', preloaded: true })
  expect(f.spawned).toHaveLength(2)
  expect(dashboards(f)).toEqual({ insight: { url: 'http://127.0.0.1:7860', port: 7860 }, converge: { url: 'http://127.0.0.1:7861/', port: 7861 } })

  await stopAll(f.$)
  expect(a.returned).toBe(true)
  expect(b.returned).toBe(true)
  expect(running('insight')).toBeNull()
  expect(running('converge')).toBeNull()
  expect(dashboards(f)).toEqual({ insight: null, converge: null })
  expect(f.spawned).toHaveLength(2)
})

test('a stopped loop never clears the entry of the one started after it', async () => {
  const a = fakeStream()
  const b = fakeStream()
  const f = await fresh([a.stream, b.stream])
  const first = startServer(f.$, 'insight', ['x'])
  a.out(URL_LINE)
  await first
  await stopServer(f.$, 'insight')
  const second = startServer(f.$, 'insight', ['x'])
  b.out('{"url": "http://127.0.0.1:7870"}\n')
  expect(await second).toEqual({ url: 'http://127.0.0.1:7870', preloaded: true })
  await settle()
  expect(running('insight')).toBe('http://127.0.0.1:7870')
  expect(dashboards(f)).toEqual({ insight: { url: 'http://127.0.0.1:7870', port: 7870 }, converge: null })
  await stopAll(f.$)
})

test('resetServers (a load) ends what the map holds and writes that none runs', async () => {
  const a = fakeStream()
  const f = await fresh([a.stream])
  // Nothing running, state left by a previous environment: the state is cleared anyway.
  f.state.set('dashboards', { value: { insight: { url: 'http://127.0.0.1:7860', port: 7860 }, converge: null }, version: 3 })
  await resetServers(f.$)
  expect(dashboards(f)).toEqual({ insight: null, converge: null })
  // A running one is ended too.
  const started = startServer(f.$, 'insight', ['x'])
  a.out(URL_LINE)
  await started
  await resetServers(f.$)
  expect(a.returned).toBe(true)
  expect(running('insight')).toBeNull()
  expect(dashboards(f)).toEqual({ insight: null, converge: null })
})
