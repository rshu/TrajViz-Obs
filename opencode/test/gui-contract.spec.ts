import { describe, expect, mock, test } from 'bun:test'
import { readFileSync } from 'node:fs'
import { createContext, runInContext } from 'node:vm'
import type { OpenCodeClient } from '@opencode/client'
import { SNAPSHOT } from '../../hooks/testdata/snapshot.js'
import { GuiController, type GuiOrigin } from '../src/gui-controller.js'
import type { CommandResult, RunInput } from '../src/rpc.js'

type Options = { signal?: AbortSignal; location?: { directory: string } }
const tick = () => new Promise<void>(resolve => setTimeout(resolve, 0))

/** Only the authenticated SDK boundary is stubbed; command coordination is production code. */
function connection(server = 'server-a', id = 'ses_shared') {
  const snapshot = structuredClone(SNAPSHOT)
  snapshot.title = server
  const rpc = {
    sync: mock(async (_input: unknown, _options?: Options) => ({ sessionID: id })),
    resolve: mock(async (_input: unknown, _options?: Options): Promise<{ sessionID: string; path?: string }> => ({ sessionID: id })),
    run: mock(async (_input: RunInput, _options?: Options): Promise<CommandResult> => ({ text: 'Done', snapshot: structuredClone(snapshot), sessionID: id })),
    snapshot: mock(async (_input: unknown, _options?: Options) => structuredClone(snapshot)),
    cancel: mock(async (_input: unknown, _options?: Options) => ({ cancelled: true })),
    events: { on: mock((_name: string, _listener: unknown) => () => {}) },
  }
  const messages = mock(async (_input: unknown, _options?: Options): Promise<{ data: Record<string, unknown>[]; cursor: { next?: string } }> => ({ data: [], cursor: {} }))
  const client = {
    rpc: (contract: { id: string }) => { if (contract.id !== 'trajviz-obs') throw new Error('Unexpected RPC contract'); return rpc },
    session: { get: mock(async (input: { sessionID: string }, _options?: Options) => ({ id: input.sessionID, location: { directory: `/${server}/project` } })) },
    message: { list: messages },
    location: { get: mock(async (_input?: unknown, _options?: Options) => ({ directory: `/${server}/project` })) },
  }
  let live = client as unknown as OpenCodeClient
  const origin: GuiOrigin = { key: `${server}\n${id}`, id, client: () => live, location: () => ({ directory: `/${server}/project` }) }
  return { rpc, client, origin, messages, snapshot, reconnect: (next: OpenCodeClient) => { live = next } }
}

function controller() {
  const hooks = { changed: mock(() => {}), notice: mock((_text: string, _error?: boolean) => {}), confirm: mock(async (_question: unknown, _signal: AbortSignal) => true) }
  return { hooks, ui: new GuiController(hooks) }
}

describe('Desktop command coordination', () => {
  test('arguments, quoted capture identity and selected model reach the same authenticated server', async () => {
    const h = connection(), { ui } = controller()
    const args = "analyze what's missing --session '/server/shared capture.json' --session-id 'session A'"
    h.rpc.resolve.mockResolvedValueOnce({ sessionID: 'capture_A', path: '/server/shared capture.json' })
    h.rpc.run.mockResolvedValueOnce({ text: 'Analysis', sessionID: 'capture_A', snapshot: h.snapshot })
    try {
      await ui.start(h.origin, args, { providerID: 'provider', id: 'selected-model', variant: 'high' }).done
      expect(h.rpc.resolve.mock.calls[0]?.[0]).toEqual({ sessionID: 'ses_shared', selector: '/server/shared capture.json', captureSessionID: 'session A' })
      expect(h.rpc.run.mock.calls[0]?.[0]).toMatchObject({ sessionID: 'ses_shared', args, model: { providerID: 'provider', id: 'selected-model', variant: 'high' }, useSynced: true })
      expect(h.client.session.get).toHaveBeenCalledTimes(0)
      expect(ui.target(h.origin)).toEqual({ id: 'capture_A', selector: '/server/shared capture.json', captureSessionID: 'session A' })
      await ui.refresh(h.origin)
      expect(h.rpc.snapshot.mock.calls[0]?.[0]).toEqual({ sessionID: 'ses_shared', selector: '/server/shared capture.json', captureSessionID: 'session A', useSynced: true })
      expect(ui.follow(h.origin, 'view health')).toContain('--session-id')
    } finally { ui.dispose() }
  })

  test('identical session IDs on different servers keep snapshots, tabs and jobs separate', async () => {
    const a = connection('server-a'), b = connection('server-b'), { ui } = controller()
    try {
      const first = ui.start(a.origin, 'overview'), second = ui.start(b.origin, 'overview')
      await Promise.all([first.done, second.done])
      ui.selectTab(a.origin, 4)
      expect(ui.snapshot(a.origin)?.empty).not.toBe(true)
      expect((ui.snapshot(a.origin) as typeof SNAPSHOT).title).toBe('server-a')
      expect((ui.snapshot(b.origin) as typeof SNAPSHOT).title).toBe('server-b')
      expect(ui.view(a.origin).tab).toBe(4)
      expect(ui.view(b.origin).tab).toBe(0)
      expect(ui.view(a.origin).latest).toBe(first.id)
      expect(ui.view(b.origin).latest).toBe(second.id)
      expect(a.rpc.run.mock.calls[0]?.[1]?.location?.directory).toBe('/server-a/project')
      expect(b.rpc.run.mock.calls[0]?.[1]?.location?.directory).toBe('/server-b/project')
    } finally { ui.dispose() }
  })

  test('following a saved capture preserves explicit selectors and leaves setup commands sessionless', async () => {
    const h = connection(), { ui } = controller()
    h.rpc.resolve.mockResolvedValueOnce({ sessionID: 'capture_A', path: '/server/capture.json' })
    h.rpc.run.mockResolvedValueOnce({ text: 'Saved', sessionID: 'capture_A', snapshot: h.snapshot })
    try {
      await ui.start(h.origin, 'overview --session /server/capture.json --session-id A').done
      expect(ui.follow(h.origin, 'issues')).toContain('--session-id')
      for (const args of ['help', 'setup --extras app', 'doctor', 'schemas /server/schemas', 'sessions 20', 'watch off', 'statusline on', 'cancel']) expect(ui.follow(h.origin, args)).toBe(args)
      for (const args of ['issues --session ses_other', "analyze what's missing --session ses_other", 'overview --session /server/capture.json --session-id B']) expect(ui.follow(h.origin, args)).toBe(args)
    } finally { ui.dispose() }
  })

  test('a late refresh cannot overwrite a newer explicit overview', async () => {
    const h = connection(), { ui } = controller()
    let started!: () => void, finish!: (snapshot: typeof SNAPSHOT) => void
    const ready = new Promise<void>(resolve => { started = resolve })
    h.rpc.snapshot.mockImplementationOnce(async () => { started(); return new Promise(resolve => { finish = resolve }) })
    try {
      const older = ui.refresh(h.origin)
      await ready
      const fresh = structuredClone(h.snapshot)
      fresh.counts.assistant_steps = 999
      h.rpc.run.mockResolvedValueOnce({ text: 'Fresh', snapshot: fresh, sessionID: h.origin.id })
      await ui.start(h.origin, 'overview').done
      finish(h.snapshot)
      await older
      expect((ui.snapshot(h.origin) as typeof SNAPSHOT).counts.assistant_steps).toBe(999)
    } finally { ui.dispose() }
  })

  test('cancelling after reconnect targets the client that owns the pending run', async () => {
    const old = connection('server-a'), newer = connection('server-a-new'), { ui } = controller()
    let started!: () => void
    const ready = new Promise<void>(resolve => { started = resolve })
    old.rpc.run.mockImplementationOnce(async (_input, options) => {
      started()
      return new Promise((_resolve, reject) => options?.signal?.addEventListener('abort', () => reject(new Error('Aborted')), { once: true }))
    })
    try {
      const job = ui.start(old.origin, 'analyze why')
      await ready
      old.reconnect(newer.client as unknown as OpenCodeClient)
      old.origin.location = () => ({ directory: '/moved/project' })
      await ui.cancel(old.origin)
      await job.done
      expect(old.rpc.cancel).toHaveBeenCalledTimes(1)
      expect(newer.rpc.cancel).toHaveBeenCalledTimes(0)
      expect(old.rpc.cancel.mock.calls[0]?.[1]?.location?.directory).toBe('/server-a/project')
      expect(ui.jobs.get(job.id)?.text).toBe('Obs command cancelled.')
      expect(ui.view(old.origin).running).toBeUndefined()
    } finally { ui.dispose() }
  })

  test('declining a model plan cancels without a confirmed rerun', async () => {
    const h = connection(), { ui, hooks } = controller()
    hooks.confirm.mockResolvedValueOnce(false)
    h.rpc.run.mockResolvedValueOnce({ text: 'Plan: five calls', confirmation: { id: 'plan', title: 'Run five calls?', message: 'Five model calls' } })
    try {
      const job = ui.start(h.origin, 'label --max-steps 5')
      await job.done
      expect(hooks.confirm).toHaveBeenCalledTimes(1)
      expect(h.rpc.run).toHaveBeenCalledTimes(1)
      expect(h.rpc.cancel.mock.calls[0]?.[0]).toEqual({ runID: job.id })
      expect(ui.jobs.get(job.id)?.text).toContain('Cancelled: no model call was made.')
    } finally { ui.dispose() }
  })

  test('accepting a model plan reuses its run, selection and model', async () => {
    const h = connection(), { ui } = controller()
    h.rpc.run.mockResolvedValueOnce({ text: 'Plan', confirmation: { id: 'plan', title: 'Run?', message: 'Five calls' } })
    try {
      const job = ui.start(h.origin, 'fixes --limit 5', { providerID: 'p', id: 'm' })
      await job.done
      expect(h.rpc.run.mock.calls[1]?.[0].confirmationID).toBe('plan')
      expect(h.rpc.run.mock.calls[1]?.[0].runID).toBe(job.id)
      expect(h.rpc.run.mock.calls[1]?.[0].args).toBe(h.rpc.run.mock.calls[0]?.[0].args)
      expect(h.rpc.run.mock.calls[1]?.[0].model).toEqual(h.rpc.run.mock.calls[0]?.[0].model)
      expect(h.rpc.cancel).toHaveBeenCalledTimes(0)
    } finally { ui.dispose() }
  })

  test('cancellation during source capture prevents the backend command', async () => {
    const h = connection(), { ui } = controller()
    let started!: () => void
    const ready = new Promise<void>(resolve => { started = resolve })
    h.messages.mockImplementationOnce(async (_input, options) => {
      started()
      return new Promise((_resolve, reject) => options?.signal?.addEventListener('abort', () => reject(new Error('Aborted')), { once: true }))
    })
    try {
      const job = ui.start(h.origin, 'analyze why')
      await ready
      await ui.cancel(h.origin)
      await job.done
      expect(h.rpc.run).toHaveBeenCalledTimes(0)
      expect(h.rpc.cancel).toHaveBeenCalledTimes(1)
    } finally { ui.dispose() }
  })

  test('home setup works while missing sessions and invalid flags remain actionable', async () => {
    const h = connection('server-a', 'home'), { ui } = controller()
    h.origin.location = () => undefined
    try {
      await ui.start(h.origin, 'help').done
      expect(h.client.location.get).toHaveBeenCalledTimes(1)
      expect(h.rpc.run.mock.calls[0]?.[0].sessionID).toBe('home')
      const job = ui.start(h.origin, 'issues')
      await job.done
      expect(ui.jobs.get(job.id)?.text).toContain('Open a session first')
      expect(h.rpc.run).toHaveBeenCalledTimes(1)
      expect(() => ui.start(h.origin, 'health --unexpected')).toThrow('Unknown option')
    } finally { ui.dispose() }
  })

  test('label completion refreshes the phase snapshot and control text is sanitized', async () => {
    const h = connection(), { ui } = controller()
    h.rpc.run.mockResolvedValueOnce({ text: '\u001b[31mLabels written\u001b[0m', path: '/server/labels.json' })
    try {
      const job = ui.start(h.origin, 'label --max-steps 1')
      await job.done
      await tick()
      expect(ui.jobs.get(job.id)?.text).toBe('Labels written')
      expect(h.rpc.snapshot).toHaveBeenCalledTimes(1)
      expect(h.rpc.sync).toHaveBeenCalledTimes(2)
    } finally { ui.dispose() }
  })

  test('dispose stops queued refreshes and refuses new commands', async () => {
    const h = connection(), { ui } = controller()
    ui.schedule(h.origin)
    ui.dispose()
    await new Promise(resolve => setTimeout(resolve, 380))
    expect(h.rpc.snapshot).toHaveBeenCalledTimes(0)
    expect(() => ui.start(h.origin, 'help')).toThrow('reloading')
  })
})

type Contribution = { id?: string; slash?: { name: string; arguments?: boolean }; run?: (args?: string) => Promise<void>; [key: string]: unknown }
type NativeNode = { component: unknown; props: Record<string, unknown> }
type Preferences = { value: { watch: boolean | null; statusline: boolean | null } }
// Use Solid's actual browser reactivity, independently of Bun's server import conditions.
const solid = require('solid-js/dist/solid.cjs') as typeof import('solid-js')

/** The app's contribution/RPC boundaries, without Node globals inside the plugin's realm. */
function desktop(options: Record<string, unknown> = {}, preferences: Preferences = { value: { watch: null, statusline: null } }) {
  const a = connection('desktop-a'), b = connection('desktop-b')
  const contributions = new Map<string, Contribution[]>()
  const observers = new Map<string, Map<string, (event: { data: Record<string, string> }) => void>>()
  const removed: string[] = []
  const lifetime = new AbortController()
  const keys = ['command', 'panel', 'menu-item', 'composer-pill', 'style', 'settings-page', 'slot']
  const sdk: Record<string, unknown> = Object.fromEntries(keys.map(key => [key.split('-').map(part => part[0]!.toUpperCase() + part.slice(1)).join(''), { id: key }]))
  sdk.Plugin = { define: (definition: unknown) => definition }
  sdk.Store = { global: (schema: unknown, initial: unknown) => ({ scope: 'global', schema, initial }) }
  sdk.createKeyed = (read: () => unknown, run: (value: unknown) => void, settings?: { equals?: (a: unknown, b: unknown) => boolean }) => {
    const value = solid.createMemo(read, undefined, { equals: settings?.equals })
    let child: (() => void) | undefined
    solid.createRenderEffect(() => {
      const next = value()
      child?.()
      solid.createRoot(dispose => { child = dispose; solid.untrack(() => run(next)) })
    })
    solid.onCleanup(() => child?.())
  }
  function server(h: ReturnType<typeof connection>) {
    const [client, setClient] = solid.createSignal(h.client as unknown as OpenCodeClient)
    const events = new Map<string, (event: { data: Record<string, string> }) => void>()
    observers.set(h.origin.key, events)
    h.rpc.events.on.mockImplementation((name, callback) => {
      events.set(name, callback as (event: { data: Record<string, string> }) => void)
      return () => { events.delete(name); removed.push(name) }
    })
    return {
      ref: { id: h.origin.key.split('\n')[0]!, builtin: h === a, connected: true, compatible: true, get client() { return client() }, data: { session: { family: (id: string) => [id] }, listen: mock((_handler: unknown) => () => { removed.push('native') }) } },
      setClient,
    }
  }
  const sa = server(a), sb = server(b)
  const session = (h: typeof a, host: typeof sa) => ({ key: h.origin.key, id: h.origin.id, tab: h.origin.key, server: host.ref, location: h.origin.location(), pending: false })
  const sessionA = session(a, sa), sessionB = session(b, sb)
  const [current, setCurrent] = solid.createSignal<typeof sessionA | undefined>(sessionA)
  const [stored, setStored] = solid.createSignal(preferences.value)
  const visible = new Set<string>()
  const open = mock((key: string, target: typeof sessionA) => { visible.add(target.key + key) })
  const close = mock((key: string, target: typeof sessionA) => { visible.delete(target.key + key) })
  const confirm = mock(async (_question: unknown) => true)
  const notify = mock((_message: unknown) => {})
  const openExternal = mock((_url: string) => {})
  const dialogs = mock((_render: unknown) => ({ close: () => {} }))
  const model = { providerID: 'composer-provider', modelID: 'composer-model', variant: 'high' }
  const context = {
    id: 'trajviz-obs', signal: lifetime.signal, options,
    stores: { settings: { get value() { return stored() }, ready: () => true, set: (value: Preferences['value']) => { preferences.value = { ...value }; setStored(value) } } },
    add: (registry: { id: string }, item: Contribution) => { contributions.set(registry.id, [...contributions.get(registry.id) ?? [], item]); return () => {} },
    toast: { show: notify }, dialogs: { open: dialogs, confirm },
    sessions: { current }, screen: { current: () => current() ? { composer: { model: () => model, submit: () => { throw new Error('Obs must never submit an agent prompt') } } } : undefined },
    servers: { list: () => [sa.ref.id, sb.ref.id], get: (id: string) => id === sa.ref.id ? sa.ref : id === sb.ref.id ? sb.ref : undefined },
    layout: { open, close, state: (key: string, target: typeof sessionA) => visible.has(target.key + key) ? 'visible' : 'closed' },
    system: { openExternal, copy: mock(async (_text: string) => {}) },
    ui: { Dialog: () => null, Button: () => null },
  }
  const module = { exports: {} as { default: { setup(ctx: unknown): () => void }; setup?: (ctx: unknown) => () => void } }
  const web = { template: () => () => ({}), delegateEvents: () => {}, createComponent: (component: unknown, props: Record<string, unknown>): NativeNode => ({ component, props }) }
  const allowed: Record<string, unknown> = { effect: require('effect'), 'solid-js': solid, 'solid-js/web': web, '@opencode/plugin/gui': sdk, '@opencode/schema/rpc': { Rpc: { define: (value: unknown) => value } } }
  const realm = createContext({ module, exports: module.exports, require: (name: string) => { if (!(name in allowed)) throw new Error(`GUI requires unsupported renderer module: ${name}`); return allowed[name] }, TextEncoder, TextDecoder, AbortController, AbortSignal, URL, crypto, setTimeout, clearTimeout })
  runInContext(readFileSync(new URL('../dist/gui-experimental.cjs', import.meta.url), 'utf8'), realm, { filename: 'gui-experimental.cjs' })
  let cleanup: (() => void) | undefined, root: (() => void) | undefined
  solid.createRoot(dispose => { root = dispose; cleanup = module.exports.default.setup(context) })
  const command = contributions.get('command')?.find(item => item.slash?.name === 'obs')
  if (!command?.run) throw new Error('Built Desktop bundle did not register /obs')
  return {
    a, b, sa, sb, sessionA, sessionB, contributions, context, realm, observers, removed, open, close, confirm, notify, dialogs, openExternal, preferences,
    route: (next: typeof sessionA | undefined) => setCurrent(next),
    command: (args = '') => command.run!(args),
    dispose: () => { cleanup?.(); lifetime.abort(); root?.() },
  }
}

describe('built Desktop GUI contract', () => {
  test('loads without Node globals and registers native slash arguments, side panel and mobile view', async () => {
    const h = desktop()
    try {
      expect(runInContext('[typeof Buffer, typeof process, typeof Bun]', h.realm)).toEqual(['undefined', 'undefined', 'undefined'])
      expect(h.contributions.get('command')?.[0]?.slash).toEqual({ name: 'obs', arguments: true })
      expect(h.contributions.get('panel')?.[0]).toMatchObject({ id: 'main', region: 'side', mobile: { kind: 'menu' } })
      await h.command('/obs issues --limit 3')
      expect(h.a.rpc.run.mock.calls[0]?.[0]).toMatchObject({ args: 'issues --limit 3', model: { providerID: 'composer-provider', id: 'composer-model', variant: 'high' } })
      expect(h.dialogs).toHaveBeenCalledTimes(1)
      const render = h.dialogs.mock.calls[0]?.[0] as (dialog: { close(): void }) => NativeNode
      expect(render({ close() {} }).component).toBe(h.context.ui.Dialog)
    } finally { h.dispose() }
  })

  test('the native slash command exposes every backend feature without rewriting its arguments', async () => {
    const h = desktop()
    const commands = [
      '', 'overview', 'help', 'setup --extras none', 'doctor', 'sessions 20', 'schemas /server/schemas',
      'view', 'view data_quality --json', 'issues --limit 2 --offset 1', 'health', 'patterns', 'agents', 'context', 'timeline', 'brief',
      'json --with-trajectory --view health --view issues -o "/server/output dir"', 'bundle -o /server/source.zip',
      'compare /server/other.json', 'scorecard /server/other.json --labels current,other', 'labels /server/labels.json',
      "analyze what's missing?", 'fixes --limit 1', 'label --assistant-only --max-steps 1',
      'attribution --reference-root /server/references --agent native --instance task',
      'dashboard --port 9000', 'dashboard stop', 'converge --data-root /server/sessions', 'converge stop',
      'collect cursor --help', 'collect opencode --help', 'converge-cli --help',
    ]
    try {
      for (const args of commands) {
        await h.command(args)
        expect(h.a.rpc.run.mock.calls.at(-1)?.[0].args).toBe(args)
      }
      expect(h.a.rpc.run).toHaveBeenCalledTimes(commands.length)
      expect(h.b.rpc.run).toHaveBeenCalledTimes(0)
    } finally { h.dispose() }
  })

  test('native model confirmation and browser opening use host APIs with safe URLs', async () => {
    const h = desktop({ openBrowser: true })
    try {
      h.confirm.mockResolvedValueOnce(false)
      h.a.rpc.run.mockResolvedValueOnce({ text: 'Five calls', confirmation: { id: 'five', title: 'Run five calls?', message: 'Uses your configured model' } })
      await h.command('label --max-steps 5')
      expect(h.confirm).toHaveBeenCalledTimes(1)
      expect(h.a.rpc.run).toHaveBeenCalledTimes(1)
      expect(h.a.rpc.cancel).toHaveBeenCalledTimes(1)
      h.a.rpc.run.mockResolvedValueOnce({ text: 'Dashboard', url: 'http://127.0.0.1:7777' })
      await h.command('dashboard')
      expect(h.openExternal.mock.calls[0]?.[0]).toBe('http://127.0.0.1:7777/')
      h.a.rpc.run.mockResolvedValueOnce({ text: 'Invalid URL', url: 'javascript:alert(1)' })
      await h.command('dashboard')
      expect(h.openExternal).toHaveBeenCalledTimes(1)
      expect(h.notify.mock.calls.some(call => JSON.stringify(call).includes('Only HTTP dashboard URLs'))).toBe(true)
    } finally { h.dispose() }
  })

  test('panel actions and reactive render props follow the routed session across servers', async () => {
    const h = desktop()
    try {
      await h.command('pane')
      expect(h.open.mock.calls[0]).toEqual(['trajviz-obs:main', h.sessionA])
      const panel = h.contributions.get('panel')?.[0] as unknown as { render(props: object): NativeNode; list(props: object): unknown[] }
      const props = { get session() { return h.context.sessions.current() }, tab: { id: 'main', title: 'TrajViz Obs' }, screen: h.context.screen.current() }
      const frame = panel.render(props)
      const view = frame.props.children as NativeNode
      expect(view.props.session).toBe(h.sessionA)
      h.route(h.sessionB)
      expect(view.props.session).toBe(h.sessionB)
      await h.command('pane close')
      expect(h.close.mock.calls[0]).toEqual(['trajviz-obs:main', h.sessionB])
      expect(panel.list({ ...props, open: ['main'] })).toHaveLength(1)
    } finally { h.dispose() }
  })

  test('native statusline and watch toggles work without backend commands and dispose subscriptions', async () => {
    const h = desktop()
    const pill = h.contributions.get('composer-pill')?.[0] as unknown as { show(input: { session: typeof h.sessionA }): unknown }
    try {
      expect(pill.show({ session: h.sessionA })).toBeUndefined()
      await h.command('statusline on')
      await tick()
      expect(pill.show({ session: h.sessionA })).toMatchObject({ title: 'TrajViz Obs' })
      await h.command('watch off')
      const initial = h.a.rpc.snapshot.mock.calls.length
      h.observers.get(h.sessionA.key)?.get('changed')?.({ data: { sessionID: h.sessionA.id } })
      await new Promise(resolve => setTimeout(resolve, 380))
      expect(h.a.rpc.snapshot).toHaveBeenCalledTimes(initial)
      await h.command('statusline off')
      expect(pill.show({ session: h.sessionA })).toBeUndefined()
      expect(h.a.rpc.run).toHaveBeenCalledTimes(0)
    } finally { h.dispose() }
    expect(h.removed).toEqual(expect.arrayContaining(['changed', 'progress', 'capture', 'native']))
  })

  test('native preferences fall back to config and retain explicit toggles after reload', async () => {
    const preferences: Preferences = { value: { watch: null, statusline: null } }
    const first = desktop({ watch: false, statusline: false }, preferences)
    try {
      const pill = first.contributions.get('composer-pill')?.[0] as unknown as { show(input: { session: typeof first.sessionA }): unknown }
      expect(pill.show({ session: first.sessionA })).toBeUndefined()
      await first.command('statusline on')
      await first.command('watch on')
      await tick()
      expect(preferences.value).toEqual({ watch: true, statusline: true })
    } finally { first.dispose() }
    const reloaded = desktop({ watch: false, statusline: false }, preferences)
    try {
      const pill = reloaded.contributions.get('composer-pill')?.[0] as unknown as { show(input: { session: typeof reloaded.sessionA }): unknown }
      expect(pill.show({ session: reloaded.sessionA })).toMatchObject({ title: 'TrajViz Obs' })
      await reloaded.command('statusline off')
      await reloaded.command('watch off')
      expect(preferences.value).toEqual({ watch: false, statusline: false })
      expect(reloaded.context.options).toEqual({ watch: false, statusline: false })
    } finally { reloaded.dispose() }
  })
})
