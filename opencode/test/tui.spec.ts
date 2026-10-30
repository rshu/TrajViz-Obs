import { describe, expect, mock, test } from 'bun:test'
import type { Plugin } from '@opencode/plugin/tui'
import { SNAPSHOT } from '../../hooks/testdata/snapshot.js'
import type { CommandResult } from '../src/rpc.js'
import { setup } from '../src/tui.js'

function host(options: Record<string, unknown> = {}, onSession = true) {
  let panel: { name: string; sessionID: string } | undefined
  let command: ((input?: string) => void | Promise<void>) | undefined
  const listeners = new Map<string, (event: { data: Record<string, string> }) => void>()
  const unsubscribed: string[] = []
  const memory = new Map<string, object>()
  const persisted = new Map<string, object>()
  const rpc = {
    sync: mock(async (_input: unknown, _options?: unknown) => ({ sessionID: 'ses_current' })),
    resolve: mock(async (_input: unknown, _options?: unknown): Promise<{ sessionID: string; path?: string }> => ({ sessionID: 'ses_other' })),
    snapshot: mock(async (_input: unknown, _options?: unknown) => structuredClone(SNAPSHOT)),
    run: mock(async (_input: { sessionID: string; args: string; runID: string; confirmationID?: string; model?: unknown }, _options?: unknown): Promise<CommandResult> => ({ text: 'Done', snapshot: structuredClone(SNAPSHOT), sessionID: 'ses_current' })),
    cancel: mock(async (_input: unknown, _options?: unknown) => ({ cancelled: true })),
    events: { on: (name: string, callback: (event: { data: Record<string, string> }) => void) => { listeners.set(name, callback); return () => { unsubscribed.push(name); listeners.delete(name) } } },
  }
  const sessionGet = mock(async (input: { sessionID: string }, _options?: unknown) => ({ id: input.sessionID, location: { directory: '/test-project' } }))
  const messageList = mock(async (_input: unknown, _options?: { signal?: AbortSignal }): Promise<{ data: Record<string, unknown>[]; cursor: { next?: string } }> => ({ data: [], cursor: {} }))
  const toast = mock((_input: unknown) => {})
  const confirm = mock(async (_input: unknown) => true as boolean | undefined)
  const context = {
    options,
    location: { directory: '/test-project' },
    renderer: { height: 40 },
    client: { rpc: () => rpc, session: { get: sessionGet }, message: { list: messageList } },
    storage: {
      store: (key: string, input: { initial: object }) => { persisted.set(key, input.initial); return [input.initial, async (fn: (state: object) => void) => fn(input.initial)] },
      memory: (key: string, input: { initial: object }) => { memory.set(key, input.initial); return [input.initial, (fn: (state: object) => void) => fn(input.initial)] },
    },
    data: {
      session: { get: () => ({ location: { directory: '/test-project' } }), family: (id: string) => [id] },
      location: { default: () => ({ directory: '/test-project' }) },
      listen: (_fn: unknown) => () => { unsubscribed.push('native') },
    },
    theme: { text: { base: 'white', muted: 'gray' } },
    keymap: { layer: (factory: () => { commands?: { id?: string; run(input?: string): void | Promise<void> }[] }) => { const commands = factory().commands ?? []; command = commands.find(row => row.id === 'trajviz-obs.command')?.run ?? command } },
    ui: {
      toast: { show: toast },
      router: { current: () => onSession ? { type: 'session', sessionID: 'ses_current' } : { type: 'home' } },
      panel: { current: () => panel, open: (name: string) => { panel = { name, sessionID: 'ses_current' }; return true }, close: () => { panel = undefined } },
      dialog: { set: (_input: unknown) => {}, show: (_fn: unknown) => {}, clear: () => {}, confirm },
      model: { current: () => ({ providerID: 'test-provider', modelID: 'test-model', variant: 'high' }) },
      slot: (_input: unknown) => () => { unsubscribed.push('slot') },
    },
  }
  const cleanup = setup(context as unknown as Plugin.Context)
  return { rpc, sessionGet, messageList, toast, confirm, cleanup, persisted, memory, listeners, unsubscribed, command: (args: string) => Promise.resolve(command!(args)) }
}

describe('OpenCode command adapter', () => {
  test('slash diagnostics synchronize the exact connected session without prompting the agent', async () => {
    const h = host()
    try {
      await h.command('/obs issues --limit 3')
      expect(h.sessionGet.mock.calls[0]?.[0]).toEqual({ sessionID: 'ses_current' })
      expect(h.rpc.sync).toHaveBeenCalledTimes(1)
      expect(h.rpc.run.mock.calls[0]?.[0]).toMatchObject({ sessionID: 'ses_current', args: 'issues --limit 3', model: { providerID: 'test-provider', id: 'test-model', variant: 'high' }, useSynced: true })
      expect(h.rpc.run.mock.calls[0]?.[0].runID.length).toBeGreaterThan(0)
    } finally { h.cleanup() }
  })

  test('setup commands work before a session exists and diagnostics explain the missing session', async () => {
    const h = host({}, false)
    try {
      await h.command('help')
      expect(h.rpc.run.mock.calls[0]?.[0].sessionID).toBe('home')
      expect(h.rpc.sync).toHaveBeenCalledTimes(0)
      await h.command('issues')
      expect(h.rpc.run).toHaveBeenCalledTimes(1)
      const state = [...h.memory.values()][0] as { jobs: Record<string, { text: string }> }
      expect(Object.values(state.jobs).some(job => job.text.includes('Open a session'))).toBe(true)
    } finally { h.cleanup() }
  })

  test('saved session selection captures the resolved session, while file selectors stay on the server', async () => {
    const h = host()
    try {
      await h.command('overview --session ses_other')
      expect(h.rpc.resolve.mock.calls[0]?.[0]).toEqual({ sessionID: 'ses_current', selector: 'ses_other' })
      expect(h.sessionGet.mock.calls[0]?.[0]).toEqual({ sessionID: 'ses_other' })
      h.rpc.resolve.mockResolvedValueOnce({ sessionID: 'source_file', path: '/server/saved.json' })
      h.rpc.run.mockResolvedValueOnce({ text: 'File', snapshot: structuredClone(SNAPSHOT), sessionID: 'source_file' })
      await h.command('overview --session /server/saved.json')
      expect(h.sessionGet).toHaveBeenCalledTimes(1)
      await h.command('pane')
      expect(h.rpc.snapshot.mock.calls.at(-1)?.[0]).toEqual({ sessionID: 'ses_current', selector: '/server/saved.json', useSynced: true })
      expect(h.sessionGet).toHaveBeenCalledTimes(1)
    } finally { h.cleanup() }
  })

  test('analysis questions with apostrophes still synchronize an explicit saved session', async () => {
    const h = host()
    try {
      await h.command("analyze what's wrong --session ses_other")
      expect(h.rpc.resolve.mock.calls[0]?.[0]).toEqual({ sessionID: 'ses_current', selector: 'ses_other' })
      expect(h.sessionGet.mock.calls[0]?.[0]).toEqual({ sessionID: 'ses_other' })
    } finally { h.cleanup() }
  })

  test('capture session selection survives refresh and isolates late snapshots from another session in the same file', async () => {
    const h = host()
    const source = '/server/shared capture.json'
    let started!: () => void
    let finish!: (snapshot: typeof SNAPSHOT) => void
    const ready = new Promise<void>(resolve => { started = resolve })
    try {
      h.rpc.resolve.mockResolvedValueOnce({ sessionID: 'capture_A', path: source })
      h.rpc.run.mockResolvedValueOnce({ text: 'A', snapshot: structuredClone(SNAPSHOT), sessionID: 'capture_A' })
      await h.command(`overview --session "${source}" --session-id A`)
      expect(h.rpc.resolve.mock.calls.at(-1)?.[0]).toEqual({ sessionID: 'ses_current', selector: source, captureSessionID: 'A' })
      h.rpc.snapshot.mockImplementationOnce(async () => { started(); return new Promise(resolve => { finish = resolve }) })
      const oldRefresh = h.command('watch on')
      await ready
      expect(h.rpc.snapshot.mock.calls.at(-1)?.[0]).toEqual({ sessionID: 'ses_current', selector: source, captureSessionID: 'A', useSynced: true })
      const newer = structuredClone(SNAPSHOT)
      newer.counts.assistant_steps = 999
      h.rpc.resolve.mockResolvedValueOnce({ sessionID: 'capture_B', path: source })
      h.rpc.run.mockResolvedValueOnce({ text: 'B', snapshot: newer, sessionID: 'capture_B' })
      await h.command(`overview --session "${source}" --session-id B`)
      finish(structuredClone(SNAPSHOT))
      await oldRefresh
      const state = [...h.memory.values()][0] as {
        selected: Record<string, string>
        files: Record<string, { selector: string; captureSessionID?: string }>
        snapshots: Record<string, typeof SNAPSHOT>
      }
      expect(state.selected.ses_current).toBe('capture_B')
      expect(state.files.capture_A?.captureSessionID).toBe('A')
      expect(state.files.capture_B?.captureSessionID).toBe('B')
      expect(state.snapshots.capture_B?.counts.assistant_steps).toBe(999)
      await h.command('pane')
      expect(h.rpc.snapshot.mock.calls.at(-1)?.[0]).toEqual({ sessionID: 'ses_current', selector: source, captureSessionID: 'B', useSynced: true })
      expect(h.sessionGet).toHaveBeenCalledTimes(0)
    } finally { h.cleanup() }
  })

  test('analysis questions preserve an explicit capture session ID after an apostrophe', async () => {
    const h = host()
    try {
      h.rpc.resolve.mockResolvedValueOnce({ sessionID: 'capture_A', path: '/server/capture.json' })
      h.rpc.run.mockResolvedValueOnce({ text: 'A', snapshot: structuredClone(SNAPSHOT), sessionID: 'capture_A' })
      await h.command("analyze what's missing --session /server/capture.json --session-id 'session A'")
      expect(h.rpc.resolve.mock.calls[0]?.[0]).toEqual({ sessionID: 'ses_current', selector: '/server/capture.json', captureSessionID: 'session A' })
    } finally { h.cleanup() }
  })

  test('native collection works as the first command on a remote client without an active session', async () => {
    const h = host({}, false)
    try {
      h.rpc.run.mockResolvedValueOnce({ text: 'Native export written', path: '/tmp/native.json', data: { sessionID: 'ses_other' } })
      await h.command('collect opencode ses_other /tmp/native.json')
      expect(h.rpc.resolve.mock.calls[0]?.[0]).toEqual({ sessionID: 'home', selector: 'ses_other' })
      expect(h.sessionGet.mock.calls[0]?.[0]).toEqual({ sessionID: 'ses_other' })
      expect(h.rpc.sync).toHaveBeenCalledTimes(1)
      const state = [...h.memory.values()][0] as { selected: Record<string, string> }
      expect(state.selected).toEqual({})
    } finally { h.cleanup() }
  })

  test('native collection captures fresh growth even after an old overview with watch off', async () => {
    const h = host({ watch: false })
    try {
      h.messageList.mockResolvedValueOnce({ data: [{ type: 'user', id: 'old' }], cursor: {} })
      await h.command('overview')
      h.rpc.resolve.mockResolvedValueOnce({ sessionID: 'ses_current' })
      h.messageList.mockResolvedValueOnce({ data: [{ type: 'user', id: 'old' }, { type: 'assistant', id: 'fresh' }], cursor: {} })
      h.rpc.run.mockResolvedValueOnce({ text: 'Native export written', path: '/tmp/native.json' })
      await h.command('collect -- opencode ses_current /tmp/native.json')
      expect(h.rpc.sync).toHaveBeenCalledTimes(2)
      const input = h.rpc.sync.mock.calls.at(-1)?.[0] as { source: { messages: { id: string }[] } }
      expect(input.source.messages.map(message => message.id)).toEqual(['old', 'fresh'])
    } finally { h.cleanup() }
  })

  test('collection help and other collectors do not synchronize unrelated sessions', async () => {
    const h = host({}, false)
    try {
      await h.command('collect opencode --help')
      await h.command('collect cursor --help')
      expect(h.rpc.sync).toHaveBeenCalledTimes(0)
      expect(h.sessionGet).toHaveBeenCalledTimes(0)
    } finally { h.cleanup() }
  })

  test('model confirmation is explicit and a declined plan never executes', async () => {
    const h = host()
    try {
      const plan = { id: 'plan-1', title: 'Label five steps?', message: '5 model calls' }
      h.rpc.run.mockResolvedValueOnce({ text: 'Plan: 5 calls', confirmation: plan })
      await h.command('label --max-steps 5')
      expect(h.confirm).toHaveBeenCalledTimes(1)
      expect(h.rpc.run.mock.calls[1]?.[0].confirmationID).toBe('plan-1')
      h.rpc.run.mockResolvedValueOnce({ text: 'Plan: 5 calls', confirmation: plan })
      h.confirm.mockResolvedValueOnce(false)
      await h.command('label --max-steps 5')
      expect(h.rpc.run).toHaveBeenCalledTimes(3)
      expect(h.rpc.cancel).toHaveBeenCalledTimes(1)
    } finally { h.cleanup() }
  })

  test('completed labeling refreshes native phase data even with watch off', async () => {
    const h = host({ watch: false })
    try {
      h.rpc.run.mockResolvedValueOnce({ text: 'Labels written', path: '/tmp/labels.json' })
      await h.command('label --max-steps 2')
      expect(h.rpc.snapshot).toHaveBeenCalledTimes(1)
      expect(h.rpc.sync).toHaveBeenCalledTimes(2)
    } finally { h.cleanup() }
  })

  test('an older watch snapshot cannot overwrite a newer explicit overview', async () => {
    const h = host()
    let started!: () => void
    let finish!: (snapshot: typeof SNAPSHOT) => void
    const ready = new Promise<void>(resolve => { started = resolve })
    h.rpc.snapshot.mockImplementationOnce(async () => {
      started()
      return new Promise(resolve => { finish = resolve })
    })
    try {
      const watch = h.command('pane')
      await ready
      const newer = structuredClone(SNAPSHOT)
      newer.counts.assistant_steps = 999
      h.rpc.run.mockResolvedValueOnce({ text: 'Fresh overview', snapshot: newer, sessionID: 'ses_current' })
      await h.command('overview')
      finish(structuredClone(SNAPSHOT))
      await watch
      const state = [...h.memory.values()][0] as { snapshots: Record<string, typeof SNAPSHOT>; errors: Record<string, string> }
      expect(state.snapshots.ses_current?.counts.assistant_steps).toBe(999)
      expect(state.errors.ses_current).toBeUndefined()
    } finally { h.cleanup() }
  })

  test('watch activity during a slow refresh queues work without starving the current result', async () => {
    const h = host()
    let started!: () => void
    let finish!: (snapshot: typeof SNAPSHOT) => void
    const ready = new Promise<void>(resolve => { started = resolve })
    h.rpc.snapshot.mockImplementationOnce(async () => {
      started()
      return new Promise(resolve => { finish = resolve })
    })
    try {
      const watch = h.command('pane')
      await ready
      for (let i = 0; i < 10; i++) h.listeners.get('changed')!({ data: { sessionID: 'ses_current' } })
      await Bun.sleep(450)
      const snapshot = structuredClone(SNAPSHOT)
      snapshot.counts.assistant_steps = 42
      finish(snapshot)
      await watch
      const state = [...h.memory.values()][0] as { snapshots: Record<string, typeof SNAPSHOT> }
      expect(state.snapshots.ses_current?.counts.assistant_steps).toBe(42)
      expect(h.rpc.snapshot).toHaveBeenCalledTimes(1)
    } finally { h.cleanup() }
  })

  test('cancellation during collection stops before any backend model request', async () => {
    const h = host()
    let started!: () => void
    const ready = new Promise<void>(resolve => { started = resolve })
    h.messageList.mockImplementationOnce(async (_input, options) => {
      started()
      return new Promise((_resolve, reject) => options?.signal?.addEventListener('abort', () => reject(new Error('Aborted')), { once: true }))
    })
    try {
      const running = h.command('analyze why did this fail?')
      await ready
      await h.command('cancel')
      await running
      expect(h.rpc.cancel).toHaveBeenCalledTimes(1)
      expect(h.rpc.run).toHaveBeenCalledTimes(0)
    } finally { h.cleanup() }
  })

  test('watch events debounce, watch off stops automatic refresh, and cleanup detaches resources', async () => {
    const h = host({ statusline: true })
    try {
      await h.command('pane')
      const initial = h.rpc.snapshot.mock.calls.length
      for (let i = 0; i < 20; i++) h.listeners.get('changed')!({ data: { sessionID: 'ses_current' } })
      await Bun.sleep(450)
      expect(h.rpc.snapshot).toHaveBeenCalledTimes(initial + 1)
      await h.command('watch off')
      h.listeners.get('changed')!({ data: { sessionID: 'ses_current' } })
      await Bun.sleep(450)
      expect(h.rpc.snapshot).toHaveBeenCalledTimes(initial + 1)
      expect(h.persisted.get('settings')).toEqual({ watch: false, statusline: true })
      await h.command('pane close')
    } finally { h.cleanup() }
    expect(h.unsubscribed).toEqual(expect.arrayContaining(['changed', 'progress', 'native', 'slot']))
  })

  test('progress is safe to render and reload clears persisted busy claims while cancelling work', async () => {
    const h = host()
    let started!: () => void
    const ready = new Promise<void>(resolve => { started = resolve })
    h.rpc.run.mockImplementationOnce(async (_input, options) => {
      started()
      return new Promise((_resolve, reject) => (options as { signal: AbortSignal }).signal.addEventListener('abort', () => reject(new Error('Aborted')), { once: true }))
    })
    const running = h.command('analyze check timing')
    await ready
    const state = [...h.memory.values()][0] as { running: Record<string, string>; jobs: Record<string, { pending: boolean; text: string }> }
    const runID = state.running.ses_current!
    h.listeners.get('progress')!({ data: { runID, text: '\u001b[31mPlan: one call\u001b[0m' } })
    expect(state.jobs[runID]?.text).toBe('Plan: one call')
    h.cleanup()
    await running
    expect(state.running).toEqual({})
    expect(state.jobs[runID]?.pending).toBe(false)
    expect(state.jobs[runID]?.text).toContain('reload')
    expect(h.rpc.cancel).toHaveBeenCalledTimes(1)
  })
})
