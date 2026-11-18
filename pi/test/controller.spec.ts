import { afterEach, describe, expect, spyOn, test } from 'bun:test'
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { createEventBus, discoverAndLoadExtensions, SessionManager, type ExtensionCommandContext, type ExtensionContext, type Theme } from '@earendil-works/pi-coding-agent'
import { stripTerminalSequences, visibleWidth, type Component } from '@earendil-works/pi-tui'
import { Controller } from '../src/index.js'
import { Backend } from '../src/backend.js'
import { SNAPSHOT } from '../../hooks/testdata/snapshot.js'
import type { Snapshot } from '../src/types.js'

const folders: string[] = []
const controllers: Controller[] = []
const environment = new Map<string, string | undefined>()
function setenv(key: string, value: string): void { if (!environment.has(key)) environment.set(key, process.env[key]); process.env[key] = value }
afterEach(async () => {
  for (const controller of controllers.splice(0)) await controller.close()
  for (const folder of folders.splice(0)) await rm(folder, { recursive: true, force: true })
  for (const [key, value] of environment) { if (value === undefined) delete process.env[key]; else process.env[key] = value }
  environment.clear()
})
function deferred<T>() { let resolve!: (value: T) => void, reject!: (error: unknown) => void; const promise = new Promise<T>((a, b) => { resolve = a; reject = b }); return { promise, resolve, reject } }
const tick = async () => { for (let i = 0; i < 5; i++) await Promise.resolve() }
const frame = (component: Component, width = 80) => component.render(width).map(stripTerminalSequences).join('\n')
const theme = { fg: (_name: string, value: string) => value } as Theme
type Overlay = { component?: Component; options?: any; closed: boolean; renders: number; mount(): void; close(): void }

async function fixture(mode: ExtensionContext['mode'] = 'tui', deferredUI = false) {
  const directory = await mkdtemp(path.join(tmpdir(), 'trajviz-pi-controller-')); folders.push(directory)
  setenv('TRAJVIZ_OBS_TELEMETRY_STATE_DIR', path.join(directory, 'telemetry'))
  setenv('TRAJVIZ_OBS_TELEMETRY', '0')
  setenv('PI_CODING_AGENT_DIR', path.join(directory, 'agent'))
  const manager = SessionManager.inMemory(directory, { id: 'controller-test' })
  const widgets: unknown[] = [], statuses: (string | undefined)[] = [], notifications: string[] = [], overlays: Overlay[] = [], tracked: { feature: string; outcome: string }[] = []
  const context = { cwd: directory, mode, hasUI: mode === 'tui' || mode === 'rpc', sessionManager: manager, getContextUsage: () => undefined,
    ui: {
      setWidget: (_name: string, value: unknown) => { widgets.push(value) },
      setStatus: (_name: string, value: string | undefined) => { statuses.push(value) },
      notify: (value: string) => { notifications.push(value) },
      custom: (factory: any, options?: any) => {
        const result = deferred<void>()
        const overlay: Overlay = { options, closed: false, renders: 0, close() { if (!overlay.closed) { overlay.closed = true; result.resolve() } }, mount() {
          overlay.component = factory({ terminal: { rows: 30, columns: 80 }, requestRender() { overlay.renders++ } }, theme, {}, () => overlay.close())
        } }
        overlays.push(overlay)
        if (!deferredUI) overlay.mount()
        return result.promise
      },
    },
  } as unknown as ExtensionCommandContext
  const backend = new Backend(directory, { cacheRoot: path.join(directory, 'cache') })
  backend.snapshot = async () => structuredClone(SNAPSHOT)
  const controller = new Controller(backend, context, '1.5.2'); controllers.push(controller)
  controller.usage.report = event => { tracked.push(event) }
  return { controller, backend, context, manager, directory, widgets, statuses, notifications, overlays, tracked }
}

describe('Pi controller state and cancellation', () => {
  test('SDK execute returns read-only results and interactive overview updates only plugin UI', async () => {
    const h = await fixture()
    h.manager.appendMessage({ role: 'user', content: 'Keep this conversation unchanged.', timestamp: Date.now() })
    const before = JSON.stringify(h.manager.getEntries())
    const result = await h.controller.execute('')
    expect(result.snapshot).toEqual(SNAPSHOT)
    expect(h.widgets.length).toBeGreaterThan(0)
    expect(h.statuses.at(-1)).toContain('311 steps')
    await h.controller.present(result, h.context)
    expect(h.overlays).toHaveLength(0)
    expect(h.notifications).toHaveLength(0)
    const help = await h.controller.execute('help')
    expect(help.text).toContain('TrajViz for Pi')
    expect(JSON.stringify(h.manager.getEntries())).toBe(before)
    expect(h.tracked.map(({ feature, outcome }) => ({ feature, outcome }))).toEqual([{ feature: 'overview', outcome: 'success' }, { feature: 'help', outcome: 'success' }])
  })

  test('overlapping commands reject, cancellation propagates, and a later command recovers', async () => {
    const h = await fixture()
    const started = deferred<void>()
    let abort!: AbortSignal
    h.backend.snapshot = async (_ctx, signal) => {
      abort = signal!; started.resolve()
      return new Promise((_resolve, reject) => signal!.addEventListener('abort', () => reject(new Error('cancelled by test')), { once: true }))
    }
    const running = h.controller.execute('')
    await started.promise
    expect(h.controller.state.busy).toBe(true)
    expect((await h.controller.execute('help')).outcome).toBe('rejected')
    expect((await h.controller.execute('cancel')).text).toContain('cancelled')
    expect(abort.aborted).toBe(true)
    expect((await running).outcome).toBe('cancelled')
    expect(h.controller.state.busy).toBe(false)
    expect(h.controller.state.error).toBeUndefined()
    expect((await h.controller.execute('help')).text).toContain('TrajViz for Pi')
    expect(h.tracked.filter(event => event.feature === 'overview').map(({ feature, outcome }) => ({ feature, outcome }))).toEqual([{ feature: 'overview', outcome: 'cancelled' }])
    expect(h.tracked.filter(event => event.feature === 'help').map(event => event.outcome)).toEqual(['rejected', 'success'])
  })

  test('a newer refresh wins and stale errors never replace the newest data', async () => {
    const h = await fixture()
    const replies = [deferred<Snapshot>(), deferred<Snapshot>(), deferred<Snapshot>()]
    const signals: AbortSignal[] = []
    h.backend.snapshot = async (_ctx, signal) => { signals.push(signal!); return replies[signals.length - 1]!.promise }
    const first = h.controller.refresh(), second = h.controller.refresh()
    expect(signals[0]!.aborted).toBe(true)
    replies[1]!.resolve({ ...SNAPSHOT, title: 'newest' }); expect(await second).toBe('success')
    replies[0]!.reject(new Error('stale failure')); expect(await first).toBe('cancelled')
    expect(h.controller.state.snapshot).toHaveProperty('title', 'newest')
    expect(h.controller.state.error).toBeUndefined()
    const third = h.controller.refresh()
    replies[2]!.reject(new Error('latest failure')); expect(await third).toBe('error')
    expect(h.controller.state.error).toBe('latest failure')
    expect(h.controller.state.snapshot).toHaveProperty('title', 'newest')
  })

  test('explicit commands invalidate an older automatic refresh', async () => {
    const h = await fixture()
    const delayed = deferred<Snapshot>()
    let calls = 0, oldSignal!: AbortSignal
    h.backend.snapshot = async (_ctx, signal) => { if (++calls === 1) { oldSignal = signal!; return delayed.promise } return { ...SNAPSHOT, title: 'selected session' } }
    const refresh = h.controller.refresh()
    await h.controller.execute('--session selected')
    delayed.resolve({ ...SNAPSHOT, title: 'old current session' }); await refresh
    expect(oldSignal.aborted).toBe(true)
    expect(h.controller.state.snapshot).toHaveProperty('title', 'selected session')
  })

  test('manual refresh retains a selected capture and automatic watch stays paused until returning current', async () => {
    const h = await fixture()
    const captures: { selector?: string; capturedID?: string }[] = []
    h.backend.snapshot = async (_ctx, _signal, selector, capturedID) => { captures.push({ selector, capturedID }); return structuredClone(SNAPSHOT) }
    const scheduled: unknown[] = []
    const original = globalThis.setTimeout
    const timer = spyOn(globalThis, 'setTimeout').mockImplementation(((callback: () => void, ms: number) => {
      const handle = original(callback, ms === 200 ? 60_000 : ms)
      if (ms === 200) scheduled.push(handle)
      return handle
    }) as typeof setTimeout)
    try {
      await h.controller.execute('pane --session "saved capture.zip" --session-id selected-run')
      expect(h.controller.state.watchPaused).toBe(true)
      expect(await h.controller.refresh()).toBe('success')
      expect(captures).toEqual([{ selector: 'saved capture.zip', capturedID: 'selected-run' }, { selector: 'saved capture.zip', capturedID: 'selected-run' }])
      h.controller.schedule(h.context)
      expect(scheduled).toEqual([])
      h.controller.state.dashboardUrl = 'http://127.0.0.1:7891/'
      await h.controller.execute('pane')
      expect(h.controller.state.watchPaused).toBe(false)
      expect(h.controller.state.dashboardUrl).toBeUndefined()
      await h.controller.refresh()
      expect(captures.at(-1)).toEqual({ selector: undefined, capturedID: undefined })
      h.controller.schedule(h.context)
      expect(scheduled).toHaveLength(1)
    } finally { timer.mockRestore() }
  })

  test('watch schedules only after activation and off cancels queued refresh work', async () => {
    const h = await fixture()
    let calls = 0
    h.backend.snapshot = async () => { calls++; return structuredClone(SNAPSHOT) }
    const callbacks: (() => void)[] = []
    const original = globalThis.setTimeout
    const timer = spyOn(globalThis, 'setTimeout').mockImplementation(((callback: () => void, ms: number) => {
      if (ms === 200) callbacks.push(callback)
      return original(callback, 60_000)
    }) as typeof setTimeout)
    try {
      h.controller.schedule(h.context)
      expect(callbacks).toHaveLength(0)
      await h.controller.execute('')
      h.controller.schedule(h.context); h.controller.schedule(h.context)
      expect(callbacks).toHaveLength(2)
      await h.controller.execute('watch off')
      expect(h.controller.state.watch).toBe(false)
      h.controller.schedule(h.context)
      expect(callbacks).toHaveLength(2)
      expect(calls).toBe(1)
    } finally { timer.mockRestore() }
  })

  test('pane watch-off cancels the queued timer and active refresh just like the command', async () => {
    const h = await fixture()
    await h.controller.execute('')
    h.controller.openPane()
    const callbacks: ReturnType<typeof setTimeout>[] = []
    const original = globalThis.setTimeout
    const timer = spyOn(globalThis, 'setTimeout').mockImplementation(((callback: () => void, ms: number) => {
      const handle = original(callback, ms === 200 ? 60_000 : ms)
      if (ms === 200) callbacks.push(handle)
      return handle
    }) as typeof setTimeout)
    const clear = spyOn(globalThis, 'clearTimeout')
    try {
      h.controller.schedule(h.context)
      const delayed = deferred<Snapshot>()
      let incoming!: AbortSignal
      h.backend.snapshot = async (_ctx, signal) => { incoming = signal!; return delayed.promise }
      const refreshing = h.controller.refresh()
      h.overlays[0]!.component!.handleInput!('w')
      expect(h.controller.state.watch).toBe(false)
      expect(clear).toHaveBeenCalledWith(callbacks[0])
      expect(incoming.aborted).toBe(true)
      delayed.resolve(SNAPSHOT)
      expect(await refreshing).toBe('cancelled')
      h.controller.schedule(h.context)
      expect(callbacks).toHaveLength(1)
      h.overlays[0]!.component!.handleInput!('w')
      h.controller.schedule(h.context)
      expect(callbacks).toHaveLength(2)
    } finally { timer.mockRestore(); clear.mockRestore() }
  })

  test('pane refresh telemetry records cancellation, success and error from the operation outcome', async () => {
    const h = await fixture()
    h.controller.openPane()
    const delayed = deferred<Snapshot>()
    let incoming!: AbortSignal
    h.backend.snapshot = async (_ctx, signal) => { incoming = signal!; return delayed.promise }
    const pane = h.overlays[0]!.component!
    pane.handleInput!('r')
    expect((await h.controller.execute('cancel')).text).toContain('command cancelled')
    expect(incoming.aborted).toBe(true)
    delayed.resolve(SNAPSHOT); await tick()
    expect(h.tracked.filter(event => event.feature === 'pane.refresh').map(event => event.outcome)).toEqual(['cancelled'])
    h.backend.snapshot = async () => structuredClone(SNAPSHOT)
    pane.handleInput!('r'); await tick()
    h.backend.snapshot = async () => { throw new Error('refresh failed') }
    pane.handleInput!('r'); await tick()
    expect(h.tracked.filter(event => event.feature === 'pane.refresh').map(event => event.outcome)).toEqual(['cancelled', 'success', 'error'])
    expect(h.controller.state.error).toBe('refresh failed')
  })

  test('shutdown aborts work, clears UI once and prevents stale completion from writing UI', async () => {
    const h = await fixture()
    const delayed = deferred<Snapshot>()
    let signal!: AbortSignal
    h.backend.snapshot = async (_ctx, incoming) => { signal = incoming!; return delayed.promise }
    const running = h.controller.execute('')
    await tick()
    const closing = h.controller.close()
    delayed.resolve(SNAPSHOT)
    await closing
    expect(signal.aborted).toBe(true)
    expect((await running).outcome).toBe('cancelled')
    expect(h.widgets.at(-1)).toBeUndefined()
    expect(h.statuses.at(-1)).toBeUndefined()
    const size = [h.widgets.length, h.statuses.length, h.overlays.length, h.notifications.length]
    await h.controller.present({ text: 'late output' }, h.context)
    h.controller.openPane()
    expect([h.widgets.length, h.statuses.length, h.overlays.length, h.notifications.length]).toEqual(size)
    expect((await h.controller.execute('help')).outcome).toBe('cancelled')
  })
})

describe('Pi output surfaces and overlays', () => {
  test('RPC notifications carry read-only results; print/JSON preserve protocol stdout', async () => {
    const rpc = await fixture('rpc')
    await rpc.controller.command('help', rpc.context)
    expect(rpc.notifications[0]).toContain('TrajViz for Pi')
    expect(rpc.overlays).toHaveLength(0)
    expect((await rpc.controller.execute('pane')).outcome).toBe('rejected')
    expect((await rpc.controller.execute('pane')).text).toContain('requires interactive Pi')
    expect(rpc.tracked.filter(event => event.feature === 'pane').map(event => event.outcome)).toEqual(['rejected', 'rejected'])
    for (const mode of ['print', 'json'] as const) {
      const h = await fixture(mode)
      const stdout = spyOn(process.stdout, 'write').mockImplementation(() => true)
      const stderr = spyOn(process.stderr, 'write').mockImplementation(() => true)
      try {
        await h.controller.command('help', h.context)
        expect(stdout).not.toHaveBeenCalled()
        expect(stderr).toHaveBeenCalledTimes(1)
        expect(h.notifications).toHaveLength(0)
        expect(h.manager.getEntries()).toEqual([])
        expect((await h.controller.execute('help')).text).toContain('TrajViz for Pi')
      } finally { stdout.mockRestore(); stderr.mockRestore() }
    }
  })

  test('replacing a mounted pane retains the new close/render handlers', async () => {
    const h = await fixture()
    await h.controller.execute('')
    h.controller.openPane(); h.controller.openPane()
    await tick()
    expect(h.overlays).toHaveLength(2)
    expect(h.overlays[0]!.closed).toBe(true)
    const current = h.overlays[1]!
    expect(current.options.overlayOptions).toEqual({ width: '100%', maxHeight: '100%', anchor: 'top-left' })
    const rendered = current.component!.render(80)
    expect(rendered).toHaveLength(30)
    for (const line of rendered) expect(visibleWidth(line)).toBe(80)
    expect(current.closed).toBe(false)
    await h.controller.execute('statusline off')
    expect(current.renders).toBeGreaterThan(0)
    await h.controller.execute('pane close')
    expect(current.closed).toBe(true)
  })

  test('pane dashboard action keeps the selected capture and displays its URL inside the full-screen pane', async () => {
    const h = await fixture()
    const imported = path.join(h.directory, 'saved capture.json')
    await writeFile(imported, '[]')
    await h.controller.execute(`pane --session "${imported}" --session-id selected-run`)
    const served: string[][] = []
    h.backend.serve = async (_key, argv) => { served.push(argv); return { url: 'http://127.0.0.1:7891/' } }
    let launched: ReturnType<Controller['execute']> | undefined
    const execute = h.controller.execute.bind(h.controller)
    h.controller.execute = (input, ctx) => {
      const promise = execute(input, ctx)
      if (input.startsWith('dashboard')) launched = promise
      return promise
    }
    h.controller.openPane()
    const pane = h.overlays[0]!.component!
    pane.handleInput!('d')
    expect(launched).toBeDefined()
    await launched
    expect(served[0]).toContain(imported)
    expect(served[0]).toContain('selected-run')
    expect(frame(pane)).toContain('Dashboard: http://127.0.0.1:7891/')
    expect(h.controller.state.dashboardUrl).toBe('http://127.0.0.1:7891/')
  })

  test('late pane/progress factories cannot reopen a closed or replaced overlay', async () => {
    const h = await fixture('tui', true)
    h.controller.openPane()
    await h.controller.execute('pane close')
    h.overlays[0]!.mount()
    expect(h.overlays[0]!.closed).toBe(true)
    h.controller.openPane()
    await h.controller.close()
    h.overlays[1]!.mount()
    expect(h.overlays[1]!.closed).toBe(true)
    const next = await fixture('tui', true)
    await next.controller.command('', next.context)
    expect(next.overlays).toHaveLength(1)
    const laterCommand = next.controller.execute('help')
    next.overlays[0]!.mount()
    expect(next.overlays[0]!.closed).toBe(true)
    await laterCommand
  })

  test('result screens scroll without source mutations or terminal injection', async () => {
    const h = await fixture()
    const text = '\x1b]52;c;c2VjcmV0\x07\x1bPsecret payload\x1b\\' + Array.from({ length: 80 }, (_, i) => `line ${i}: 中文🧪`).join('\r\n')
    const showing = h.controller.present({ text }, h.context)
    const screen = h.overlays[0]!
    expect(screen.options.overlayOptions).toEqual({ width: '100%', maxHeight: '100%', anchor: 'top-left' })
    const first = frame(screen.component!, 40)
    screen.component!.handleInput!('\x1b[6~')
    expect(screen.renders).toBeGreaterThan(0)
    expect(frame(screen.component!, 40)).not.toBe(first)
    screen.component!.handleInput!('\x1b[F')
    expect(frame(screen.component!, 40)).toContain('line 79')
    screen.component!.handleInput!('\x1b[H')
    expect(frame(screen.component!, 40)).toBe(first)
    for (const width of [1, 2, 8, 24, 80]) for (const line of screen.component!.render(width)) {
      expect(visibleWidth(line)).toBeLessThanOrEqual(width)
      expect(line).not.toContain('\r')
    }
    expect(first).not.toContain('secret')
    expect(first).not.toContain('52;c;')
    expect(h.manager.getEntries()).toEqual([])
    screen.component!.handleInput!('\x1b')
    await showing
  })
})

test('installed Pi loader recognizes the native default extension and command lifecycle', async () => {
  const h = await fixture('rpc')
  const agentDir = path.join(h.directory, 'empty-agent')
  await mkdir(agentDir)
  const manifest = JSON.parse(await readFile(new URL('../package.json', import.meta.url), 'utf8'))
  expect(manifest.pi.extensions).toEqual(['./dist/trajviz-obs.js'])
  const result = await discoverAndLoadExtensions([path.resolve(import.meta.dir, '../src/index.ts')], h.directory, agentDir, createEventBus())
  expect(result.errors).toEqual([])
  expect(result.extensions).toHaveLength(1)
  const extension = result.extensions[0]!
  expect([...extension.commands.keys()]).toEqual(['obs'])
  expect(extension.tools.size).toBe(0)
  expect(extension.handlers.has('session_shutdown')).toBe(true)
  expect(extension.handlers.has('turn_end')).toBe(true)
  expect(extension.handlers.has('session_switch')).toBe(false)
  const native = extension.commands.get('obs')!
  expect((await native.getArgumentCompletions!('dash'))?.map(row => row.value)).toEqual(['dashboard'])
  const before = JSON.stringify(h.manager.getEntries())
  for (const handler of extension.handlers.get('session_start')!) await handler({ type: 'session_start', reason: 'startup' }, h.context)
  await native.handler('help', h.context)
  expect(h.notifications.at(-1)).toContain('TrajViz for Pi')
  for (const handler of extension.handlers.get('session_shutdown')!) await handler({ type: 'session_shutdown', reason: 'reload' }, h.context)
  expect(JSON.stringify(h.manager.getEntries())).toBe(before)
})

test('shutdown during native initialization cannot create a controller after the session ends', async () => {
  const h = await fixture('rpc')
  const agentDir = path.join(h.directory, 'empty-agent')
  await mkdir(agentDir)
  const result = await discoverAndLoadExtensions([path.resolve(import.meta.dir, '../src/index.ts')], h.directory, agentDir, createEventBus())
  expect(result.errors).toEqual([])
  const extension = result.extensions[0]!
  const startup = Promise.resolve(extension.handlers.get('session_start')![0]!({ type: 'session_start', reason: 'startup' }, h.context)).catch(error => error as Error)
  await extension.handlers.get('session_shutdown')![0]!({ type: 'session_shutdown', reason: 'quit' }, h.context)
  expect(String(await startup)).toContain('unloading')
  expect(h.notifications).toEqual([])
  expect(h.widgets).toEqual([])
  expect(h.manager.getEntries()).toEqual([])
})
