import type { OpenCodeClient, LocationRef } from '@opencode/client'
import { captureSession } from './collect.js'
import { createInteractionReporter } from './interaction.js'
import { Obs, type CommandResult, type RunInput, type UsageInteraction } from './rpc.js'
import { analyzeSelection, parseArgs, passArgs, quoteOption } from './shared/args.js'
import { validateArguments } from './command-args.js'
import { usageFeature } from './usage-features.js'
import { commandText, errorMessage as message, safeText, TABS } from './tui-model.js'
import type { Snapshot, EmptySnapshot } from './types.js'

/** An origin never changes servers while an operation is in flight. */
export type GuiOrigin = {
  key: string
  id: string
  client(): OpenCodeClient
  location(): LocationRef | undefined
}
export type GuiTarget = { id: string; selector?: string; captureSessionID?: string }
export type GuiJob = { id: string; args: string; origin: GuiOrigin; target?: GuiTarget; text: string; pending: boolean; url?: string; path?: string; data?: unknown; snapshot?: Snapshot | EmptySnapshot }
export type GuiView = { selected?: GuiTarget; tab: number; snapshots: Record<string, Snapshot | EmptySnapshot>; errors: Record<string, string>; running?: string; latest?: string }
export type GuiHooks = {
  changed(): void
  confirm(confirmation: NonNullable<CommandResult['confirmation']>, signal: AbortSignal): Promise<boolean>
  notice(text: string, error?: boolean): void
}
const SESSIONLESS = new Set(['help', 'setup', 'doctor', 'telemetry', 'enable_telemetry', 'disable_telemetry', 'sessions', 'schemas', 'converge', 'labels', 'collect', 'converge-cli'])
const display = (text: string) => { const clean = safeText(text); return clean.length > 64_000 ? clean.slice(0, 64_000) + '\n\nDisplay clipped. Export the full result with /obs json.' : clean }
const pause = (signal: AbortSignal) => new Promise<void>((resolve, reject) => {
  signal.throwIfAborted()
  const abort = () => { clearTimeout(timer); reject(signal.reason) }
  const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve() }, 1000)
  signal.addEventListener('abort', abort, { once: true })
})

/** Browser-only coordinator; source reads and RPC always use the same authenticated server client. */
export class GuiController {
  readonly views = new Map<string, GuiView>()
  readonly jobs = new Map<string, GuiJob>()
  private readonly active = new Map<string, { controller: AbortController; client: OpenCodeClient; location?: LocationRef }>()
  private readonly refreshes = new Map<string, AbortController>()
  private readonly generations = new Map<string, number>()
  private readonly timers = new Map<string, ReturnType<typeof setTimeout>>()
  private readonly dirty = new Set<string>()
  private readonly report = createInteractionReporter('desktop')
  private closed = false
  constructor(readonly hooks: GuiHooks, readonly lifetime?: AbortSignal) {}
  view(origin: GuiOrigin): GuiView {
    let view = this.views.get(origin.key)
    if (!view) { view = { tab: 0, snapshots: {}, errors: {} }; this.views.set(origin.key, view) }
    return view
  }
  target(origin: GuiOrigin): GuiTarget { return this.view(origin).selected ?? { id: origin.id } }
  snapshot(origin: GuiOrigin) { return this.view(origin).snapshots[this.target(origin).id] }
  error(origin: GuiOrigin) { return this.view(origin).errors[this.target(origin).id] }
  resetInteractionConsent(client: OpenCodeClient): void { this.report.reset(client) }
  interaction(origin: GuiOrigin, feature: UsageInteraction['feature'], outcome: UsageInteraction['outcome'], started: number): void {
    if (this.closed || this.lifetime?.aborted) return
    try { void this.report(origin.client(), origin.location(), feature, outcome, started) }
    catch { /* The UI can be reloading or between connections. */ }
  }
  selectTab(origin: GuiOrigin, tab: number) {
    const view = this.view(origin), next = Math.max(0, Math.min(TABS.length - 1, tab))
    if (view.tab === next) return
    const started = performance.now()
    view.tab = next
    this.hooks.changed()
    this.interaction(origin, 'pane.tab', 'success', started)
  }
  restoreResult(origin: GuiOrigin, id: string): boolean {
    const job = this.jobs.get(id)
    if (this.closed || !job || job.pending || !job.target || job.origin.key !== origin.key) return false
    this.view(origin).selected = { ...job.target }
    this.hooks.changed()
    return true
  }
  follow(origin: GuiOrigin, args: string): string {
    const parsed = parseArgs(args)
    if (SESSIONLESS.has(parsed.sub) || ['watch', 'statusline', 'cancel'].includes(parsed.sub) || parsed.flags.session !== undefined || parsed.sub === 'analyze' && analyzeSelection(args).session !== undefined) return args
    const target = this.target(origin)
    return target.selector ? `${args} ${quoteOption('session', target.selector)}${target.captureSessionID ? ` ${quoteOption('session-id', target.captureSessionID)}` : ''}`
      : target.id !== origin.id ? `${args} ${quoteOption('session', target.id)}` : args
  }
  private key(origin: GuiOrigin, target: GuiTarget) { return JSON.stringify([origin.key, target.id]) }
  private claim(key: string) { const next = (this.generations.get(key) ?? 0) + 1; this.generations.set(key, next); return next }
  private controller() {
    const controller = new AbortController()
    if (this.lifetime?.aborted) controller.abort()
    return controller
  }
  private signal(controller: AbortController) { return this.lifetime ? AbortSignal.any([controller.signal, this.lifetime]) : controller.signal }
  private async location(origin: GuiOrigin, client: OpenCodeClient, signal: AbortSignal): Promise<LocationRef> {
    const location = origin.location()
    if (location) return { ...location }
    if (origin.id !== 'home') throw new Error('Session location is still loading. Try again when the session is ready.')
    return { directory: (await client.location.get(undefined, { signal })).directory }
  }
  private async sync(client: OpenCodeClient, id: string, signal: AbortSignal): Promise<void> {
    const source = await captureSession(client, id, signal)
    await client.rpc(Obs).sync({ source }, { location: { directory: source.directory }, signal })
  }
  private cancelRemote(client: OpenCodeClient, runID: string, location?: LocationRef): void {
    try {
      void client.rpc(Obs).cancel({ runID }, { location, signal: AbortSignal.timeout(5000) }).catch(() => {})
    } catch { /* A disposed client cannot delay local cancellation or unload. */ }
  }
  private async remote(job: GuiJob, client: OpenCodeClient, input: RunInput,
                       options: { location: LocationRef; signal: AbortSignal }): Promise<CommandResult> {
    const rpc = client.rpc(Obs), { signal, location } = options
    const check = () => {
      signal.throwIfAborted()
      if (job.origin.client() !== client) throw new Error('The server reconnected. Run the command again to refresh its result.')
    }
    let startError: unknown
    try {
      // Every phase starts once. An ambiguous acknowledgement can be recovered
      // by polling its existing identity, never by repeating paid work.
      check()
      try { await rpc.start(input, options) }
      catch (error) { check(); startError = error }
      for (;;) {
        check()
        const state = await rpc.poll({ sessionID: input.sessionID, runID: input.runID,
          ...(input.confirmationID === undefined ? {} : { confirmationID: input.confirmationID }) }, options)
        check()
        if (state.state === 'finished') return state.result
        if (state.state === 'expired') throw startError ?? new Error('The obs result expired or the server restarted. Start a new command to retry.')
        startError = undefined
        if (state.progress) { job.text = display(state.progress); this.hooks.changed() }
        await pause(signal)
      }
    } catch (error) {
      if (!signal.aborted) this.cancelRemote(client, input.runID, location)
      throw error
    }
  }
  start(origin: GuiOrigin, raw: string, model?: RunInput['model'], paneSnapshot?: true): { id: string; done: Promise<void> } {
    if (this.closed) throw new Error('TrajViz Obs is reloading.')
    const started = performance.now(), args = raw.trim(), invalid = validateArguments(args)
    const view = this.view(origin)
    const refusal = invalid ?? (view.running ? 'An obs command is running. Cancel it before starting another.' : undefined)
    if (refusal) {
      const feature = paneSnapshot ? undefined : usageFeature(args)
      if (feature) this.interaction(origin, feature, 'rejected', started)
      throw new Error(refusal)
    }
    const id = crypto.randomUUID(), controller = this.controller(), client = origin.client()
    const job: GuiJob = { id, args, origin, text: `Running /obs ${args || 'overview'}…`, pending: true }
    this.jobs.set(id, job); this.active.set(id, { controller, client }); view.running = id; view.latest = id
    if (!args) delete view.selected
    this.hooks.changed()
    return { id, done: this.execute(job, controller, client, model, paneSnapshot) }
  }
  private async execute(job: GuiJob, controller: AbortController, client: OpenCodeClient, model?: RunInput['model'], paneSnapshot?: true): Promise<void> {
    const { origin, args } = job, view = this.view(origin), signal = this.signal(controller), rpc = client.rpc(Obs)
    const started = performance.now(), feature = paneSnapshot ? undefined : usageFeature(args)
    let target: GuiTarget = { id: origin.id }, key: string | undefined, generation: number | undefined
    let dispatched = false
    let preflightOutcome: 'error' | 'rejected' = 'error'
    try {
      const location = await this.location(origin, client, signal), options = { location, signal }, parsed = parseArgs(args)
      const active = this.active.get(job.id)
      if (active) active.location = location
      if (!SESSIONLESS.has(parsed.sub)) {
        const analyze = parsed.sub === 'analyze' ? analyzeSelection(args) : undefined
        const selector = analyze ? analyze.session : parsed.flags.session, captureID = analyze ? analyze.captureSessionId : parsed.flags['session-id']
        if (origin.id === 'home' && typeof selector !== 'string') {
          preflightOutcome = 'rejected'
          throw new Error('Open a session first, or select a saved session with --session.')
        }
        if (typeof selector === 'string') {
          const resolved = await rpc.resolve({ sessionID: origin.id, selector, ...(typeof captureID === 'string' ? { captureSessionID: captureID } : {}) }, options)
          target = { id: resolved.sessionID, ...(resolved.path ? { selector: resolved.path, ...(typeof captureID === 'string' ? { captureSessionID: captureID } : {}) } : {}) }
        }
        key = this.key(origin, target); generation = this.claim(key)
        if (!target.selector) await this.sync(client, target.id, signal)
        if (parsed.sub === 'compare' || parsed.sub === 'scorecard') for (const other of parsed.positional) {
          const resolved = await rpc.resolve({ sessionID: origin.id, selector: other }, options)
          if (!resolved.path) await this.sync(client, resolved.sessionID, signal)
        }
      }
      if (parsed.sub === 'collect') {
        const collect = passArgs(args), selector = collect[1]
        if (collect[0] === 'opencode' && collect.length >= 2 && collect.length <= 3 && selector && selector !== 'help' && !selector.startsWith('-')) {
          const resolved = await rpc.resolve({ sessionID: origin.id, selector }, options)
          if (!resolved.path) await this.sync(client, resolved.sessionID, signal)
        }
      }
      const input = { sessionID: origin.id, args, runID: job.id, model, useSynced: true, ...(paneSnapshot ? { paneSnapshot } : {}) }
      dispatched = true
      let result = await this.remote(job, client, input, options)
      if (result.confirmation) {
        job.text = display(result.text); this.hooks.changed()
        let accepted = false
        try { accepted = await Promise.race([this.hooks.confirm(result.confirmation, signal), new Promise<false>(resolve => signal.addEventListener('abort', () => resolve(false), { once: true }))]) }
        finally { if (!accepted || signal.aborted) this.cancelRemote(client, job.id, location) }
        signal.throwIfAborted()
        result = accepted ? await this.remote(job, client, { ...input, confirmationID: result.confirmation.id }, options) : { text: `${result.text}\nCancelled: no model call was made.` }
      }
      signal.throwIfAborted()
      if (this.closed) return
      if (origin.client() !== client) throw new Error('The server reconnected. Run the command again to refresh its result.')
      if (result.sessionID) target = { ...target, id: result.sessionID }
      if (!SESSIONLESS.has(parsed.sub)) { view.selected = target; job.target = target }
      if (result.snapshot && key && this.generations.get(key) === generation) { view.snapshots[target.id] = result.snapshot; delete view.errors[target.id] }
      job.text = display(commandText(result)); job.url = result.url; job.path = result.path; job.data = result.data; job.snapshot = result.snapshot
      if (result.tab) { const tab = TABS.findIndex(name => name.toLowerCase() === result.tab?.toLowerCase()); if (tab >= 0) view.tab = tab }
      if (parsed.sub === 'label' && result.path) void this.refresh(origin)
    } catch (error) {
      if (!dispatched && feature && origin.client() === client) {
        this.interaction(origin, feature, signal.aborted ? 'cancelled' : preflightOutcome, started)
      }
      if (!this.closed) job.text = signal.aborted ? 'Obs command cancelled.' : message(error)
    } finally {
      this.active.delete(job.id); job.pending = false
      if (view.running === job.id) delete view.running
      if (!this.closed) {
        // Keep bounded result history without dropping pending jobs.
        for (const [id, old] of [...this.jobs].slice(0, -16)) if (!old.pending) this.jobs.delete(id)
        this.hooks.changed()
      }
    }
  }
  async refreshPane(origin: GuiOrigin): Promise<void> {
    const client = origin.client(), started = performance.now()
    const outcome = await this.refresh(origin)
    if (origin.client() === client) this.interaction(origin, 'pane.refresh', outcome, started)
  }
  async refresh(origin: GuiOrigin): Promise<UsageInteraction['outcome']> {
    if (this.closed || origin.id === 'home') return 'cancelled'
    const target = { ...this.target(origin) }, key = this.key(origin, target)
    if (this.refreshes.has(key)) { this.dirty.add(key); return 'cancelled' }
    const generation = this.claim(key), controller = this.controller(), signal = this.signal(controller), client = origin.client(), view = this.view(origin)
    this.refreshes.set(key, controller); this.hooks.changed()
    try {
      const location = await this.location(origin, client, signal)
      if (!target.selector) await this.sync(client, target.id, signal)
      const snapshot = await client.rpc(Obs).snapshot({ sessionID: target.selector ? origin.id : target.id, useSynced: true, ...(target.selector ? { selector: target.selector, ...(target.captureSessionID ? { captureSessionID: target.captureSessionID } : {}) } : {}) }, { location, signal })
      if (this.closed || signal.aborted || origin.client() !== client || this.generations.get(key) !== generation) return 'cancelled'
      view.snapshots[target.id] = snapshot
      delete view.errors[target.id]
      return snapshot.empty ? 'empty' : 'success'
    } catch (error) {
      if (!this.closed && !signal.aborted && this.generations.get(key) === generation) view.errors[target.id] = message(error)
      return this.closed || signal.aborted ? 'cancelled' : 'error'
    } finally {
      this.refreshes.delete(key)
      if (!this.closed) { this.hooks.changed(); if (this.dirty.delete(key)) this.schedule(origin) }
    }
  }
  refreshing(origin: GuiOrigin) { return this.refreshes.has(this.key(origin, this.target(origin))) }
  schedule(origin: GuiOrigin) {
    if (this.closed || this.target(origin).selector || this.timers.has(origin.key)) return
    this.timers.set(origin.key, setTimeout(() => { this.timers.delete(origin.key); void this.refresh(origin) }, 350))
  }
  progress(runID: string, text: string) { const job = this.jobs.get(runID); if (job?.pending) { job.text = display(text); this.hooks.changed() } }
  async cancel(origin: GuiOrigin): Promise<void> {
    const id = this.view(origin).running, job = id ? this.active.get(id) : undefined
    if (!id || !job) { this.hooks.notice('No obs command is running.'); return }
    job.controller.abort()
    this.cancelRemote(job.client, id, job.location)
  }
  dispose() {
    this.closed = true
    for (const timer of this.timers.values()) clearTimeout(timer)
    for (const controller of this.refreshes.values()) controller.abort()
    for (const [id, job] of this.active) { job.controller.abort(); this.cancelRemote(job.client, id, job.location) }
    this.timers.clear(); this.refreshes.clear(); this.active.clear()
  }
}
