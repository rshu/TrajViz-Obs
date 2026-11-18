import { VERSION, type ExtensionAPI, type ExtensionCommandContext, type ExtensionContext } from '@earendil-works/pi-coding-agent'
import { Key, isKeyRelease, matchesKey, Text, truncateToWidth, stripTerminalSequences, type Component } from '@earendil-works/pi-tui'
import { parseArgs, quoteOption, SUBCOMMANDS } from '../../hooks/lib/args.js'
import { Backend } from './backend.js'
import { loadOptions } from './config.js'
import { runCommand, type CommandResult } from './commands.js'
import { Usage, usageFeature, type Outcome } from './telemetry.js'
import { FramedComponent, ObsPane, renderOverview, type PaneState } from './ui.js'

export type RefreshOutcome = 'success' | 'error' | 'cancelled'

/** Read-only command output. It never enters Pi's conversation or model context. */
class ResultPane implements Component {
  private offset = 0
  constructor(readonly content: () => string, readonly close: () => void, readonly rows: () => number, readonly changed: () => void = () => {}, readonly title = 'TrajViz Obs · ↑↓ scroll · Esc close') {}
  invalidate(): void {}
  handleInput(data: string): void {
    if (isKeyRelease(data)) return
    if (matchesKey(data, Key.escape) || data === 'q') this.close()
    else if (matchesKey(data, Key.up)) this.offset = Math.max(0, this.offset - 1)
    else if (matchesKey(data, Key.down)) this.offset++
    else if (matchesKey(data, Key.pageDown)) this.offset += Math.max(1, this.rows() - 3)
    else if (matchesKey(data, Key.pageUp)) this.offset = Math.max(0, this.offset - Math.max(1, this.rows() - 3))
    else if (matchesKey(data, Key.home)) this.offset = 0
    else if (matchesKey(data, Key.end)) this.offset = Number.MAX_SAFE_INTEGER
    this.changed()
  }
  render(width: number): string[] {
    if (width < 1) return []
    const safe = stripTerminalSequences(this.content().replace(/(?:\u001b[PX^_]|[\u0090\u0098\u009e\u009f])[\s\S]*?(?:\u001b\\|\u009c|$)/g, ''))
      .replace(/\r\n?/g, '\n').replace(/\t/g, '  ')
      .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/g, '')
    const lines = new Text(safe, 0, 0).render(Math.max(1, width))
    const rows = Math.max(1, Math.floor(this.rows()))
    const available = Math.max(1, rows - 2)
    this.offset = Math.min(Math.max(0, lines.length - available), this.offset)
    const title = truncateToWidth(this.title, width, '…')
    if (rows === 1) return [title]
    const body = lines.slice(this.offset, this.offset + available).map(line => truncateToWidth(line, width, '…'))
    return rows === 2 ? [title, ...body] : [title, '', ...body]
  }
}

/** One extension instance owns its processes, UI, cancellation and immutable captures. */
export class Controller {
  readonly usage: Usage
  readonly state: PaneState
  private status: boolean
  private ctx: ExtensionContext
  private active?: AbortController
  private refreshAbort?: AbortController
  private refreshTimer?: ReturnType<typeof setTimeout>
  private selection?: { selector: string; captureSessionID?: string }
  private paneRender?: () => void
  private progressRender?: () => void
  private paneToken?: symbol
  private progressToken?: symbol
  private closePane?: () => void
  private closeProgress?: () => void
  private progressText = ''
  private widget = false
  private closed = false
  private closing?: Promise<void>
  constructor(readonly backend: Backend, ctx: ExtensionContext, hostVersion = VERSION) {
    this.ctx = ctx
    this.state = { tab: 0, watch: backend.options.watch !== false }
    this.status = backend.options.statusline !== false
    this.usage = new Usage(backend.runtime, hostVersion)
    this.track('activation', 'success', 0)
  }
  private track(feature: string, outcome: Outcome = 'success', durationMs = 0): void {
    this.usage.report({ feature, outcome, durationMs, surface: this.ctx.mode === 'tui' ? 'terminal' : 'headless' })
  }
  private draw(): void {
    if (this.closed || this.ctx.mode !== 'tui') return
    if (this.widget) {
      this.ctx.ui.setWidget('trajviz-obs', (_tui, theme) => ({
        render: width => renderOverview(this.state.snapshot, width, theme), invalidate() {},
      }))
    }
    const snap = this.state.snapshot
    this.ctx.ui.setStatus('trajviz-obs', this.status && snap && !snap.empty
      ? `Obs · ${snap.counts.assistant_steps} steps · ${snap.tokens.total === null ? 'tokens —' : `${(snap.tokens.total / 1000).toFixed(1)}k tok`} · ${snap.counts.issues ?? snap.issues.length} issues${this.state.busy ? ' · working' : ''}` : undefined)
    this.paneRender?.(); this.progressRender?.()
  }
  /** Only refresh after the user activates Obs, or while a dashboard is open. */
  schedule(ctx: ExtensionContext): void {
    this.ctx = ctx
    if (this.closed || !this.state.watch || this.selection || (!this.state.snapshot && !this.backend.servers.size)) return
    clearTimeout(this.refreshTimer)
    this.refreshTimer = setTimeout(() => { this.refreshTimer = undefined; void this.refresh() }, 200)
    this.refreshTimer.unref()
  }
  async refresh(): Promise<RefreshOutcome> {
    if (this.closed) return 'cancelled'
    this.refreshAbort?.abort()
    const controller = new AbortController(); this.refreshAbort = controller
    try {
      const snapshot = await this.backend.snapshot(this.ctx, controller.signal, this.selection?.selector, this.selection?.captureSessionID)
      if (this.closed || controller.signal.aborted) return 'cancelled'
      this.state.snapshot = snapshot; this.state.error = undefined; this.draw()
      return 'success'
    } catch (error) {
      if (this.closed || controller.signal.aborted) return 'cancelled'
      this.state.error = error instanceof Error ? error.message : String(error); this.draw()
      return 'error'
    } finally { if (this.refreshAbort === controller) this.refreshAbort = undefined }
  }
  private setWatch(value?: boolean): boolean {
    if (value !== undefined) this.state.watch = value
    if (!this.state.watch) {
      clearTimeout(this.refreshTimer); this.refreshTimer = undefined
      this.refreshAbort?.abort()
    }
    this.draw()
    return this.state.watch
  }
  async execute(input: string, ctx: ExtensionContext = this.ctx): Promise<CommandResult> {
    this.ctx = ctx
    if (this.closed) return { text: 'Obs is unloading. Restart Pi or /reload.', outcome: 'cancelled' }
    const parsed = parseArgs(input)
    const started = performance.now(), feature = usageFeature(input)
    const rejected = (text: string): CommandResult => {
      if (feature) this.track(feature, 'rejected', performance.now() - started)
      return { text, outcome: 'rejected' }
    }
    if (parsed.sub === 'pane' && ctx.mode !== 'tui') return rejected('The obs pane requires interactive Pi. Use /obs or /obs dashboard instead.')
    if (parsed.sub === 'cancel' && !parsed.positional.length && !Object.keys(parsed.flags).length) {
      const running = Boolean(this.active || this.refreshAbort || this.refreshTimer)
      clearTimeout(this.refreshTimer); this.refreshTimer = undefined
      this.active?.abort(); this.refreshAbort?.abort()
      return { text: running ? 'Obs command cancelled.' : 'No obs command is running.' }
    }
    if (this.active) return rejected('An obs command is running. Press Escape in its progress panel or use /obs cancel.')
    clearTimeout(this.refreshTimer); this.refreshTimer = undefined
    this.refreshAbort?.abort()
    const active = new AbortController(); this.active = active
    this.state.busy = true; this.state.error = undefined; this.draw()
    const progress = (text: string) => {
      this.progressText = text
      if (ctx.mode === 'rpc' && ctx.hasUI) ctx.ui.notify(text, 'info')
      this.draw()
    }
    try {
      const result = await this.backend.revisions.run(() => runCommand(this.backend, ctx, input, active.signal, {
        watch: value => this.setWatch(value),
        statusline: value => { if (value !== undefined) this.status = value; this.draw(); return this.status },
        closePane: () => this.dismissPane(), progress,
      }, this.usage))
      active.signal.throwIfAborted()
      if (result.snapshot) {
        const selected = typeof parsed.flags.session === 'string' && parsed.flags.session !== ctx.sessionManager.getSessionId()
          ? { selector: parsed.flags.session, ...(typeof parsed.flags['session-id'] === 'string' ? { captureSessionID: parsed.flags['session-id'] } : {}) }
          : undefined
        if (JSON.stringify(selected) !== JSON.stringify(this.selection)) this.state.dashboardUrl = undefined
        this.selection = selected; this.state.watchPaused = Boolean(selected); this.state.snapshot = result.snapshot; this.widget = true
      }
      if (result.url) this.state.dashboardUrl = result.url
      if (parsed.sub === 'enable_telemetry' && this.usage.enabled()) this.track('activation')
      if (feature) this.track(feature, result.outcome ?? (result.snapshot?.empty ? 'empty' : 'success'), performance.now() - started)
      return result
    } catch (error) {
      const outcome = active.signal.aborted ? 'cancelled' : 'error'
      const text = active.signal.aborted ? 'Obs command cancelled.' : error instanceof Error ? error.message : String(error)
      this.state.error = outcome === 'error' ? text : undefined
      if (feature) this.track(feature, outcome, performance.now() - started)
      return { text, outcome }
    } finally {
      if (this.active === active) this.active = undefined
      this.state.busy = false; this.closeProgress?.(); this.closeProgress = undefined
      this.progressToken = undefined; this.progressRender = undefined; this.draw()
    }
  }
  private openProgress(ctx: ExtensionContext): void {
    if (ctx.mode !== 'tui') return
    const token = Symbol('progress'); this.progressToken = token
    this.progressText = 'Preparing TrajViz…\nEscape cancels this Obs command.'
    void ctx.ui.custom<void>((tui, theme, _bindings, done) => {
      if (!this.active || this.progressToken !== token) { done(); return new ResultPane(() => '', () => {}, () => 1) }
      this.closeProgress = () => done()
      const rows = () => Math.max(1, Math.min(10, Math.floor(tui.terminal.rows * .4)))
      const pane = new ResultPane(() => this.progressText, () => { this.active?.abort(); done() }, () => Math.max(1, rows() - 2), () => tui.requestRender(), 'TrajViz Obs · Running command')
      this.progressRender = () => tui.requestRender()
      return new FramedComponent(pane, theme, rows, 'Esc cancels')
    }, { overlay: true, overlayOptions: { width: '85%', maxHeight: '40%', anchor: 'center' } }).catch(() => {}).finally(() => {
      if (this.progressToken === token) { this.closeProgress = undefined; this.progressRender = undefined; this.progressToken = undefined }
    })
  }
  async present(result: CommandResult, ctx: ExtensionContext): Promise<void> {
    if (this.closed) return
    if (ctx.mode === 'tui') {
      if (result.pane) { this.openPane(ctx); return }
      if (result.snapshot) return
      if (result.text.split('\n').length <= 4) { ctx.ui.notify(result.text, result.outcome === 'error' ? 'error' : 'info'); return }
      await ctx.ui.custom<void>((tui, theme, _keys, done) => new FramedComponent(
        new ResultPane(() => result.text, () => done(), () => Math.max(1, tui.terminal.rows - 2), () => tui.requestRender()), theme, () => tui.terminal.rows),
        { overlay: true, overlayOptions: { width: '100%', maxHeight: '100%', anchor: 'top-left' } })
    } else if (ctx.hasUI) ctx.ui.notify(result.text, result.outcome === 'error' ? 'error' : 'info')
    // Print/JSON have no command-result UI. Keep their protocol stdout untouched.
  }
  openPane(ctx: ExtensionContext = this.ctx): void {
    if (this.closed || ctx.mode !== 'tui') return
    this.closePane?.()
    const token = Symbol('pane'); this.paneToken = token
    void ctx.ui.custom<void>((tui, theme, _bindings, done) => {
      if (this.closed || this.paneToken !== token) { done(); return new ResultPane(() => '', () => {}, () => 1) }
      const close = (record = true) => { if (this.paneToken === token) this.closePane = undefined; if (record) this.track('pane.close'); done() }
      this.closePane = () => close(false)
      this.paneRender = () => tui.requestRender()
      const pane = new ObsPane(() => this.state, {
        close: () => close(), changed: () => tui.requestRender(),
        selectTab: tab => { this.state.tab = tab; this.track('pane.tab') },
        refresh: () => { const start = performance.now(); void this.refresh().then(outcome => this.track('pane.refresh', outcome, performance.now() - start)) },
        dashboard: () => {
          const selection = this.selection
          const command = ['dashboard', ...(selection ? [quoteOption('session', selection.selector), ...(selection.captureSessionID === undefined ? [] : [quoteOption('session-id', selection.captureSessionID)])] : [])].join(' ')
          void this.execute(command, ctx).then(result => this.present(result, ctx))
        },
        toggleWatch: () => { this.setWatch(!this.state.watch); this.track('watch') },
      }, theme, { rows: () => Math.max(1, tui.terminal.rows - 2) })
      return new FramedComponent(pane, theme, () => tui.terminal.rows)
    }, { overlay: true, overlayOptions: { width: '100%', maxHeight: '100%', anchor: 'top-left' } }).catch(error => {
      if (!this.closed && this.paneToken === token) { this.state.error = error instanceof Error ? error.message : String(error); this.draw() }
    }).finally(() => { if (this.paneToken === token) { this.closePane = undefined; this.paneRender = undefined; this.paneToken = undefined } })
  }
  async command(input: string, ctx: ExtensionCommandContext): Promise<void> {
    if (this.closed) return
    if (ctx.mode !== 'tui' && !ctx.hasUI) {
      // Pi exposes no slash-command output transport in print/JSON mode.
      // The backend/controller API remains usable by SDK integrations.
      process.stderr.write('TrajViz /obs commands require Pi interactive mode or an RPC UI.\n')
      return
    }
    const alreadyActive = Boolean(this.active)
    const pending = this.execute(input, ctx)
    if (!['cancel', 'help', 'telemetry', 'telemetry_destination', 'enable_telemetry', 'disable_telemetry', 'watch', 'statusline'].includes(parseArgs(input).sub) && !alreadyActive) this.openProgress(ctx)
    const result = await pending
    await this.present(result, ctx)
  }
  private dismissPane(): void {
    this.paneToken = undefined; this.closePane?.(); this.closePane = undefined; this.paneRender = undefined
  }
  close(): Promise<void> {
    if (this.closing) return this.closing
    this.closed = true; clearTimeout(this.refreshTimer)
    this.active?.abort(); this.refreshAbort?.abort(); this.dismissPane(); this.closeProgress?.()
    this.progressToken = undefined; this.closeProgress = undefined; this.progressRender = undefined
    if (this.ctx.mode === 'tui') { this.ctx.ui.setWidget('trajviz-obs', undefined); this.ctx.ui.setStatus('trajviz-obs', undefined) }
    this.closing = (async () => { await this.usage.flush(); await this.backend.close() })()
    return this.closing
  }
}

export default function trajvizObs(pi: ExtensionAPI): void {
  let controller: Controller | undefined
  let initializing: Promise<Controller> | undefined
  let generation = 0
  const get = async (ctx: ExtensionContext): Promise<Controller> => {
    if (controller) return controller
    const current = generation
    return initializing ??= loadOptions(ctx.cwd).then(options => {
      if (generation !== current) throw new Error('Obs is unloading. Restart Pi or /reload.')
      controller = new Controller(new Backend(ctx.cwd, options), ctx); return controller
    }).finally(() => { initializing = undefined })
  }
  pi.registerCommand('obs', {
    description: 'TrajViz session diagnostics, live charts and dashboard',
    getArgumentCompletions: prefix => [...SUBCOMMANDS, 'enable_telemetry', 'disable_telemetry'].filter(name => name.startsWith(prefix)).map(value => ({ value, label: value })),
    handler: async (input, ctx) => {
      try { await (await get(ctx)).command(input, ctx) }
      catch (error) { if (ctx.hasUI) ctx.ui.notify(error instanceof Error ? error.message : String(error), 'error') }
    },
  })
  pi.on('session_start', async (_event, ctx) => { await get(ctx) })
  const refresh = (_event: unknown, ctx: ExtensionContext) => { controller?.schedule(ctx) }
  pi.on('agent_settled', refresh)
  pi.on('turn_end', refresh)
  pi.on('tool_execution_end', refresh)
  pi.on('session_compact', refresh)
  pi.on('session_tree', refresh)
  pi.on('session_shutdown', async () => {
    generation++
    const active = controller; controller = undefined
    await active?.close()
    await initializing?.catch(() => {})
  })
}
