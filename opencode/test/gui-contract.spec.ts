import { describe, expect, mock, test } from 'bun:test'
import { readFileSync } from 'node:fs'
import { createContext, runInContext } from 'node:vm'
import type { OpenCodeClient } from '@opencode/client'
import { SNAPSHOT } from '../../hooks/testdata/snapshot.js'
import { GuiController, type GuiOrigin } from '../src/gui-controller.js'
import type { CommandResult, RunInput, UsageInteraction } from '../src/rpc.js'

type Options = { signal?: AbortSignal; location?: { directory: string } }
const tick = () => new Promise<void>(resolve => setTimeout(resolve, 0))

/** Only the authenticated SDK boundary is stubbed; command coordination is production code. */
function connection(server = 'server-a', id = 'ses_shared') {
  const snapshot = structuredClone(SNAPSHOT)
  snapshot.title = server
  const completed = new Map<string, CommandResult>()
  const phase = (input: { runID: string; confirmationID?: string }) => JSON.stringify([input.runID, input.confirmationID ?? ''])
  const rpc = {
    sync: mock(async (_input: unknown, _options?: Options) => ({ sessionID: id })),
    resolve: mock(async (_input: unknown, _options?: Options): Promise<{ sessionID: string; path?: string }> => ({ sessionID: id })),
    run: mock(async (_input: RunInput, _options?: Options): Promise<CommandResult> => ({ text: 'Done', snapshot: structuredClone(snapshot), sessionID: id })),
    start: mock(async (input: RunInput, options?: Options) => { completed.set(phase(input), await rpc.run(input, options)); return { accepted: true as const } }),
    poll: mock(async (input: { sessionID: string; runID: string; confirmationID?: string }, _options?: Options): Promise<
      { state: 'finished'; result: CommandResult } | { state: 'pending'; progress?: string } | { state: 'expired' }
    > => { const result = completed.get(phase(input)); return result ? { state: 'finished', result } : { state: 'expired' } }),
    snapshot: mock(async (_input: unknown, _options?: Options) => structuredClone(snapshot)),
    cancel: mock(async (_input: unknown, _options?: Options) => ({ cancelled: true })),
    usageStatus: mock(async (_input: object, _options?: Options) => ({ enabled: false })),
    usageInteraction: mock(async (_input: UsageInteraction, _options?: Options) => ({})),
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

async function fastPolling(run: (elapsed: () => number) => Promise<void>) {
  const original = globalThis.setTimeout
  let elapsed = 0
  globalThis.setTimeout = ((callback: (...args: unknown[]) => void, milliseconds?: number, ...args: unknown[]) => milliseconds === 1000
    ? original(() => { elapsed += milliseconds; callback(...args) }, 0)
    : original(callback, milliseconds, ...args)) as typeof setTimeout
  try { await run(() => elapsed) }
  finally { globalThis.setTimeout = original }
}

describe('Desktop command coordination', () => {
  test('known commands that fail before Desktop dispatch report fixed outcomes once', async () => {
    const h = connection(), { ui } = controller()
    h.rpc.usageStatus.mockResolvedValue({ enabled: true })
    try {
      expect(() => ui.start(h.origin, 'help --private-flag value')).toThrow('Unknown option')
      expect(() => ui.start(h.origin, 'private-unknown-command')).toThrow('Unknown subcommand')
      h.rpc.resolve.mockRejectedValueOnce(new Error('private resolve error'))
      await ui.start(h.origin, 'issues --session "/private/missing.json"').done
      expect(h.rpc.start).not.toHaveBeenCalled()
      await ui.start(h.origin, 'help').done
      expect(h.rpc.start).toHaveBeenCalledTimes(1)
      expect(h.rpc.usageInteraction.mock.calls.map(([{ feature, surface, outcome }]) => ({ feature, surface, outcome }))).toEqual([
        { feature: 'help', surface: 'desktop', outcome: 'rejected' },
        { feature: 'issues', surface: 'desktop', outcome: 'error' },
      ])
      expect(JSON.stringify(h.rpc.usageInteraction.mock.calls.map(([input]) => input))).not.toContain('private')
    } finally { ui.dispose() }
  })

  test('pane tab changes and manual refresh outcomes are sent to the consenting session server', async () => {
    const h = connection(), off = connection('off'), { ui } = controller()
    h.rpc.usageStatus.mockResolvedValue({ enabled: true })
    try {
      await ui.refresh(h.origin)
      ui.selectTab(h.origin, 0)
      expect(h.rpc.usageStatus).not.toHaveBeenCalled()
      ui.selectTab(h.origin, 2)
      ui.selectTab(h.origin, 2)
      await ui.refreshPane(h.origin)
      h.rpc.snapshot.mockRejectedValueOnce(new Error('private snapshot error'))
      await ui.refreshPane(h.origin)
      ui.selectTab(off.origin, 3)
      await ui.refreshPane(off.origin)
      await tick()
      expect(h.rpc.usageStatus.mock.calls.map(([input]) => input)).toEqual([{}, {}, {}])
      expect(off.rpc.usageInteraction).not.toHaveBeenCalled()
      expect(h.rpc.usageInteraction.mock.calls.map(([{ durationMs, ...event }, options]) => {
        expect(durationMs).toBeGreaterThanOrEqual(0)
        expect(options?.location).toEqual({ directory: '/server-a/project' })
        return event
      })).toEqual([
        { feature: 'pane.tab', surface: 'desktop', outcome: 'success' },
        { feature: 'pane.refresh', surface: 'desktop', outcome: 'success' },
        { feature: 'pane.refresh', surface: 'desktop', outcome: 'error' },
      ])
    } finally { ui.dispose() }
  })

  test('open Desktop panes recheck changed consent on each connected server', async () => {
    const h = connection(), other = connection('other-server'), { ui } = controller()
    try {
      ui.selectTab(h.origin, 1)
      ui.selectTab(other.origin, 1)
      await tick()
      expect(h.rpc.usageInteraction).not.toHaveBeenCalled()

      h.rpc.usageStatus.mockResolvedValue({ enabled: true })
      ui.selectTab(h.origin, 2)
      ui.selectTab(other.origin, 2)
      await tick()
      expect(h.rpc.usageStatus).toHaveBeenCalledTimes(2)
      expect(h.rpc.usageInteraction).toHaveBeenCalledTimes(1)
      expect(other.rpc.usageStatus).toHaveBeenCalledTimes(2)
      expect(other.rpc.usageInteraction).not.toHaveBeenCalled()
      h.rpc.usageStatus.mockResolvedValue({ enabled: false })
      ui.selectTab(h.origin, 3)
      await tick()
      expect(h.rpc.usageInteraction).toHaveBeenCalledTimes(1)
    } finally { ui.dispose() }
  })

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

  test('an older result restores its own captured session before reopening the live pane', async () => {
    const h = connection(), other = connection('other-server'), { ui } = controller()
    h.rpc.resolve.mockResolvedValueOnce({ sessionID: 'capture_A', path: '/server/capture.json' })
    h.rpc.run.mockResolvedValueOnce({ text: 'Captured A', sessionID: 'capture_A', snapshot: h.snapshot })
    try {
      const first = ui.start(h.origin, 'overview --session /server/capture.json --session-id A')
      await first.done
      h.rpc.resolve.mockResolvedValueOnce({ sessionID: 'capture_B', path: '/server/capture.json' })
      h.rpc.run.mockResolvedValueOnce({ text: 'Captured B', sessionID: 'capture_B', snapshot: h.snapshot })
      await ui.start(h.origin, 'overview --session /server/capture.json --session-id B').done
      expect(ui.target(h.origin).id).toBe('capture_B')
      expect(ui.restoreResult(other.origin, first.id)).toBe(false)
      expect(ui.target(h.origin).id).toBe('capture_B')
      expect(ui.restoreResult(h.origin, first.id)).toBe(true)
      expect(ui.target(h.origin)).toEqual({ id: 'capture_A', selector: '/server/capture.json', captureSessionID: 'A' })
      expect(ui.target(h.origin)).not.toBe(ui.jobs.get(first.id)?.target)
      await ui.refresh(h.origin)
      expect(h.rpc.snapshot.mock.calls.at(-1)?.[0]).toEqual({ sessionID: h.origin.id, selector: '/server/capture.json', captureSessionID: 'A', useSynced: true })
      expect(ui.jobs.get(first.id)?.text).toBe('Captured A')
      const help = ui.start(h.origin, 'help')
      expect(ui.restoreResult(h.origin, help.id)).toBe(false)
      await help.done
      expect(ui.restoreResult(h.origin, help.id)).toBe(false)
      expect(ui.target(h.origin).id).toBe('capture_A')
      expect(ui.restoreResult(h.origin, 'expired-result')).toBe(false)
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

  test('declining confirmation completes locally when remote cleanup never responds', async () => {
    const h = connection(), { ui, hooks } = controller()
    h.rpc.run.mockResolvedValueOnce({ text: 'Plan', confirmation: { id: 'five', title: 'Run?', message: 'Five calls' } })
    h.rpc.cancel.mockImplementationOnce(async () => new Promise(() => {}))
    hooks.confirm.mockResolvedValueOnce(false)
    try {
      const job = ui.start(h.origin, 'label --max-steps 5')
      const outcome = await Promise.race([job.done.then(() => 'finished'), new Promise(resolve => setTimeout(() => resolve('blocked'), 100))])
      expect(outcome).toBe('finished')
      expect(ui.jobs.get(job.id)?.pending).toBe(false)
      expect(ui.jobs.get(job.id)?.text).toContain('Cancelled: no model call was made.')
      expect(ui.view(h.origin).running).toBeUndefined()
      expect(h.rpc.start).toHaveBeenCalledTimes(1)
      expect(h.rpc.cancel.mock.calls[0]?.[1]?.signal).toBeInstanceOf(AbortSignal)
      expect(h.rpc.cancel.mock.calls[0]?.[1]?.location?.directory).toBe('/server-a/project')
    } finally { ui.dispose() }
  })

  test('short Desktop requests can observe generation beyond the host sixty-second deadline', async () => {
    await fastPolling(async elapsed => {
      const h = connection(), { ui } = controller()
      h.rpc.start.mockResolvedValue({ accepted: true })
      h.rpc.poll.mockImplementation(async () => elapsed() >= 61_000
        ? { state: 'finished', result: { text: 'Long analysis completed' } }
        : { state: 'pending', progress: 'Generating analysis…' })
      try {
        const job = ui.start(h.origin, 'analyze what changed')
        await job.done
        expect(elapsed()).toBeGreaterThan(60_000)
        expect(h.rpc.start).toHaveBeenCalledTimes(1)
        expect(h.rpc.run).toHaveBeenCalledTimes(0)
        expect(h.rpc.poll.mock.calls.length).toBeGreaterThan(60)
        expect(h.rpc.poll.mock.calls.every(([input, options]) => input.sessionID === h.origin.id
          && options?.location?.directory === '/server-a/project' && options.signal instanceof AbortSignal)).toBe(true)
        expect(ui.jobs.get(job.id)?.text).toBe('Long analysis completed')
        expect(h.rpc.cancel).toHaveBeenCalledTimes(0)
      } finally { ui.dispose() }
    })
  })

  test('a lost start acknowledgement recovers by polling without restarting paid work', async () => {
    const h = connection(), { ui } = controller()
    h.rpc.start.mockRejectedValueOnce(new Error('Transport response was lost'))
    h.rpc.poll.mockResolvedValueOnce({ state: 'finished', result: { text: 'Already completed' } })
    try {
      const job = ui.start(h.origin, 'analyze recovery')
      await job.done
      expect(h.rpc.start).toHaveBeenCalledTimes(1)
      expect(h.rpc.poll.mock.calls[0]?.[0]).toEqual({ sessionID: h.origin.id, runID: job.id })
      expect(h.rpc.run).toHaveBeenCalledTimes(0)
      expect(h.rpc.cancel).toHaveBeenCalledTimes(0)
      expect(ui.jobs.get(job.id)?.text).toBe('Already completed')
    } finally { ui.dispose() }
  })

  test('expired server work never triggers a new start or a model replay', async () => {
    const h = connection(), { ui } = controller()
    h.rpc.start.mockResolvedValueOnce({ accepted: true })
    h.rpc.poll.mockResolvedValueOnce({ state: 'expired' })
    try {
      const job = ui.start(h.origin, 'fixes --limit 1')
      await job.done
      expect(h.rpc.start).toHaveBeenCalledTimes(1)
      expect(h.rpc.poll).toHaveBeenCalledTimes(1)
      expect(h.rpc.run).toHaveBeenCalledTimes(0)
      expect(ui.jobs.get(job.id)?.text).toContain('result expired')
      expect(ui.view(h.origin).running).toBeUndefined()
    } finally { ui.dispose() }
  })

  test('confirmation resumes one separate server phase with the captured identity and model', async () => {
    const h = connection(), { ui, hooks } = controller()
    h.rpc.start.mockResolvedValue({ accepted: true })
    h.rpc.poll.mockImplementation(async input => input.confirmationID
      ? { state: 'finished', result: { text: 'Confirmed result' } }
      : { state: 'finished', result: { text: 'Plan', confirmation: { id: 'plan-five', title: 'Run?', message: 'Five calls' } } })
    try {
      const model = { providerID: 'provider', id: 'selected-model' }
      const job = ui.start(h.origin, 'label --max-steps 5', model)
      await job.done
      expect(hooks.confirm).toHaveBeenCalledTimes(1)
      expect(h.rpc.start).toHaveBeenCalledTimes(2)
      expect(h.rpc.start.mock.calls[0]?.[0]).toEqual({ sessionID: h.origin.id, args: 'label --max-steps 5', runID: job.id, model, useSynced: true })
      expect(h.rpc.start.mock.calls[1]?.[0]).toEqual({ ...h.rpc.start.mock.calls[0]![0], confirmationID: 'plan-five' })
      expect(h.rpc.poll.mock.calls.map(([input]) => input.confirmationID)).toEqual([undefined, 'plan-five'])
      expect(h.rpc.run).toHaveBeenCalledTimes(0)
      expect(ui.jobs.get(job.id)?.text).toBe('Confirmed result')
    } finally { ui.dispose() }
  })

  test('reconnect while a confirmation is open cannot start its paid continuation', async () => {
    const h = connection(), newer = connection('new-server'), { ui, hooks } = controller()
    h.rpc.start.mockResolvedValue({ accepted: true })
    h.rpc.poll.mockResolvedValueOnce({ state: 'finished', result: { text: 'Plan',
      confirmation: { id: 'stale-plan', title: 'Run?', message: 'Five calls' } } })
    hooks.confirm.mockImplementationOnce(async () => { h.reconnect(newer.client as unknown as OpenCodeClient); return true })
    try {
      const job = ui.start(h.origin, 'label --max-steps 5')
      await job.done
      expect(hooks.confirm).toHaveBeenCalledTimes(1)
      expect(h.rpc.start).toHaveBeenCalledTimes(1)
      expect(h.rpc.run).toHaveBeenCalledTimes(0)
      expect(h.rpc.cancel.mock.calls[0]?.[0]).toEqual({ runID: job.id })
      expect(newer.rpc.start).toHaveBeenCalledTimes(0)
      expect(newer.rpc.cancel).toHaveBeenCalledTimes(0)
      expect(ui.jobs.get(job.id)?.text).toContain('server reconnected')
    } finally { ui.dispose() }
  })

  test('a late result from a reconnected server never opens a stale confirmation', async () => {
    const h = connection(), newer = connection('new-server'), { ui, hooks } = controller()
    h.rpc.start.mockResolvedValueOnce({ accepted: true })
    h.rpc.poll.mockImplementationOnce(async () => {
      h.reconnect(newer.client as unknown as OpenCodeClient)
      return { state: 'finished', result: { text: 'Old plan', confirmation: { id: 'old', title: 'Run?', message: 'Five calls' } } }
    })
    try {
      const job = ui.start(h.origin, 'label --max-steps 5')
      await job.done
      expect(hooks.confirm).toHaveBeenCalledTimes(0)
      expect(h.rpc.start).toHaveBeenCalledTimes(1)
      expect(h.rpc.cancel).toHaveBeenCalledTimes(1)
      expect(newer.rpc.start).toHaveBeenCalledTimes(0)
      expect(ui.jobs.get(job.id)?.text).toContain('server reconnected')
    } finally { ui.dispose() }
  })

  test('poll failure cancels only its original server and immutable location', async () => {
    const h = connection(), newer = connection('new-server'), { ui } = controller()
    const location = { directory: '/original/project' }
    h.origin.location = () => location
    let ready!: () => void, fail!: (error: Error) => void
    const waiting = new Promise<void>(resolve => { ready = resolve })
    h.rpc.start.mockResolvedValueOnce({ accepted: true })
    h.rpc.poll.mockImplementationOnce(async () => { ready(); return new Promise((_resolve, reject) => { fail = reject }) })
    try {
      const job = ui.start(h.origin, 'analyze failure')
      await waiting
      location.directory = '/moved/project'
      h.reconnect(newer.client as unknown as OpenCodeClient)
      fail(new Error('Server unavailable'))
      await job.done
      expect(h.rpc.cancel).toHaveBeenCalledTimes(1)
      expect(h.rpc.cancel.mock.calls[0]?.[1]?.location?.directory).toBe('/original/project')
      expect(newer.rpc.poll).toHaveBeenCalledTimes(0)
      expect(newer.rpc.cancel).toHaveBeenCalledTimes(0)
      expect(h.rpc.start).toHaveBeenCalledTimes(1)
      expect(ui.jobs.get(job.id)?.text).toBe('Server unavailable')
    } finally { ui.dispose() }
  })

  test('cancel during the polling interval stops future polls immediately', async () => {
    const h = connection(), { ui } = controller()
    let ready!: () => void
    const waiting = new Promise<void>(resolve => { ready = resolve })
    h.rpc.start.mockResolvedValueOnce({ accepted: true })
    h.rpc.poll.mockImplementation(async () => { ready(); return { state: 'pending', progress: 'Working' } })
    h.rpc.cancel.mockImplementationOnce(async () => new Promise(() => {}))
    try {
      const job = ui.start(h.origin, 'analyze cancellation')
      await waiting; await tick()
      const outcome = await Promise.race([ui.cancel(h.origin).then(() => 'cancelled'), new Promise(resolve => setTimeout(() => resolve('blocked'), 100))])
      expect(outcome).toBe('cancelled')
      await job.done; await tick()
      expect(h.rpc.start).toHaveBeenCalledTimes(1)
      expect(h.rpc.poll).toHaveBeenCalledTimes(1)
      expect(h.rpc.cancel).toHaveBeenCalledTimes(1)
      expect(h.rpc.cancel.mock.calls[0]?.[1]?.signal).toBeInstanceOf(AbortSignal)
      expect(ui.jobs.get(job.id)?.text).toBe('Obs command cancelled.')
      expect(ui.view(h.origin).running).toBeUndefined()
    } finally { ui.dispose() }
  })

  test('disposing pending work stops polling and cancels its original server job', async () => {
    const h = connection(), { ui } = controller()
    let ready!: () => void
    const waiting = new Promise<void>(resolve => { ready = resolve })
    h.rpc.start.mockResolvedValueOnce({ accepted: true })
    h.rpc.poll.mockImplementation(async () => { ready(); return { state: 'pending' } })
    h.rpc.cancel.mockImplementationOnce(async () => new Promise(() => {}))
    const job = ui.start(h.origin, 'analyze cleanup')
    await waiting; await tick()
    ui.dispose(); await job.done; await tick()
    expect(h.rpc.start).toHaveBeenCalledTimes(1)
    expect(h.rpc.poll).toHaveBeenCalledTimes(1)
    expect(h.rpc.cancel.mock.calls[0]?.[0]).toEqual({ runID: job.id })
    expect(h.rpc.cancel.mock.calls[0]?.[1]?.location?.directory).toBe('/server-a/project')
    expect(ui.jobs.get(job.id)?.pending).toBe(false)
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
      await ui.start(h.origin, 'telemetry').done
      expect(h.rpc.run.mock.calls[1]?.[0]).toMatchObject({ sessionID: 'home', args: 'telemetry' })
      await ui.start(h.origin, 'enable_telemetry').done
      await ui.start(h.origin, 'disable_telemetry').done
      expect(h.rpc.run.mock.calls[2]?.[0]).toMatchObject({ sessionID: 'home', args: 'enable_telemetry' })
      expect(h.rpc.run.mock.calls[3]?.[0]).toMatchObject({ sessionID: 'home', args: 'disable_telemetry' })
      expect(h.rpc.sync).not.toHaveBeenCalled()
      const job = ui.start(h.origin, 'issues')
      await job.done
      expect(ui.jobs.get(job.id)?.text).toContain('Open a session first')
      expect(h.rpc.run).toHaveBeenCalledTimes(4)
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
  const close = mock((key: string, target: typeof sessionA) => {
    if (!visible.delete(target.key + key)) return
    const panel = contributions.get('panel')?.[0] as unknown as { close?(input: { session: typeof sessionA }): void }
    panel?.close?.({ session: target })
  })
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
  const realm = createContext({ module, exports: module.exports, require: (name: string) => { if (!(name in allowed)) throw new Error(`GUI requires unsupported renderer module: ${name}`); return allowed[name] }, TextEncoder, TextDecoder, AbortController, AbortSignal, URL, crypto, performance, setTimeout, clearTimeout })
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
  test('explicit pane opens, native closes and restores report one Desktop event each', async () => {
    const h = desktop()
    h.a.rpc.usageStatus.mockResolvedValue({ enabled: true })
    try {
      await h.command('pane --session /private/capture.json')
      expect(h.a.rpc.start.mock.calls[0]?.[0]).toMatchObject({ args: 'overview --session /private/capture.json', paneSnapshot: true })
      await h.command('pane close')
      const panel = h.contributions.get('panel')?.[0] as unknown as { restore(input: { session: typeof h.sessionA }): void }
      panel.restore({ session: h.sessionA })
      await h.command('watch off')
      await h.command('statusline on')
      await tick()
      expect(h.a.rpc.usageInteraction.mock.calls.map(([{ durationMs, ...event }]) => {
        expect(durationMs).toBeGreaterThanOrEqual(0)
        return event
      })).toEqual((['pane', 'pane.close', 'pane', 'watch', 'statusline'] as const).map(feature => ({ feature, surface: 'desktop', outcome: 'success' })))
      expect(h.b.rpc.usageInteraction).not.toHaveBeenCalled()
    } finally { h.dispose() }
  })

  test('loads without Node globals and registers native slash arguments, side panel and mobile view', async () => {
    const h = desktop()
    try {
      expect(runInContext('[typeof Buffer, typeof process, typeof Bun]', h.realm)).toEqual(['undefined', 'undefined', 'undefined'])
      expect(h.contributions.get('command')?.[0]?.slash).toEqual({ name: 'obs', arguments: true })
      expect(h.contributions.get('panel')?.[0]).toMatchObject({ id: 'main', region: 'side', mobile: { kind: 'menu' } })
      await h.command('/obs issues --limit 3')
      expect(h.a.rpc.start).toHaveBeenCalledTimes(1)
      expect(h.a.rpc.poll).toHaveBeenCalledTimes(1)
      expect(h.a.rpc.run.mock.calls[0]?.[0]).toMatchObject({ args: 'issues --limit 3', model: { providerID: 'composer-provider', id: 'composer-model', variant: 'high' } })
      expect(h.dialogs).toHaveBeenCalledTimes(1)
      const render = h.dialogs.mock.calls[0]?.[0] as (dialog: { close(): void }) => NativeNode
      expect(render({ close() {} }).component).toBe(h.context.ui.Dialog)
    } finally { h.dispose() }
  })

  test('the native slash command exposes every backend feature without rewriting its arguments', async () => {
    const h = desktop()
    const commands = [
      '', 'overview', 'help', 'setup --extras none', 'doctor', 'telemetry', 'enable_telemetry', 'disable_telemetry', 'sessions 20', 'schemas /server/schemas',
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
