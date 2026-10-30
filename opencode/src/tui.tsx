import { Plugin } from '@opencode/plugin/tui'
import { ErrorBoundary, For, Show, createEffect, onMount } from 'solid-js'
import { useTerminalDimensions } from '@opentui/solid'
import type { ScrollBoxRenderable } from '@opentui/core'
import { captureSession } from './collect.js'
import { Obs, type CommandResult } from './rpc.js'
import { analyzeSelection, parseArgs, passArgs, quoteOption } from './shared/args.js'
import { validateArguments } from './command-args.js'
import { TABS, commandText, metrics, safeText, sections, statusText } from './tui-model.js'
import type { EmptySnapshot, Snapshot } from './types.js'

type SnapshotDoc = Snapshot | EmptySnapshot
type Job = { args: string; sessionID: string; targetID?: string; text: string; pending: boolean; url?: string; path?: string }
type UIState = {
  tab: number
  selected: Record<string, string>
  files: Record<string, { origin: string; selector: string; captureSessionID?: string }>
  snapshots: Record<string, SnapshotDoc>
  errors: Record<string, string>
  jobs: Record<string, Job>
  running: Record<string, string>
}
type Panel = { name: string; sessionID: string; width: number; focused: boolean; focus(): void; close(): void; toggleFullscreen(): void }
const PANEL = 'trajviz-obs'
const SESSIONLESS = new Set(['help', 'setup', 'doctor', 'sessions', 'schemas', 'converge', 'labels', 'collect', 'converge-cli'])
const errorText = (error: unknown): string => safeText(error instanceof Error ? error.message : String(error))
const displayResult = (text: string): string => {
  const clean = safeText(text)
  return clean.length > 64_000 ? `${clean.slice(0, 64_000)}\n\nDisplay clipped. Use /obs json for full session data.` : clean
}
const localTime = (stamp: string): string => {
  const date = new Date(stamp)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleTimeString(undefined, { hour12: false })
}

/** Direct UI commands never submit an agent prompt or alter conversation history. */
export function setup(context: Plugin.Context): () => void {
  const [settings, updateSettings] = context.storage.store('settings', { initial: { watch: context.options.watch !== false, statusline: context.options.statusline === true } })
  const [state, update] = context.storage.memory<UIState>('ui-v1', {
    initial: { tab: 0, selected: {}, files: {}, snapshots: {}, errors: {}, jobs: {}, running: {} },
  })
  let disposed = false
  const aborts = new Set<AbortController>()
  const jobs = new Map<string, AbortController>()
  const timers = new Map<string, ReturnType<typeof setTimeout>>()
  const refreshing = new Set<string>()
  const dirty = new Set<string>()
  const generations = new Map<string, number>()
  const claim = (id: string): number => {
    const generation = (generations.get(id) ?? 0) + 1
    generations.set(id, generation)
    return generation
  }
  const selected = (base: string): string => state.selected[base] ?? base
  const location = (id: string) => context.data.session.get(id)?.location ?? context.location ?? context.data.location.default()
  const active = (): string | undefined => {
    const route = context.ui.router.current()
    return route.type === 'session' ? route.sessionID : context.ui.panel.current()?.sessionID
  }
  const notice = (message: string, error = false) => context.ui.toast.show({ title: 'TrajViz Obs', message: safeText(message), variant: error ? 'error' : 'info' })
  const rpc = () => context.client.rpc(Obs)
  // Memory survives reload, but the old generation's asynchronous work does not.
  for (const [base, runID] of Object.entries(state.running)) {
    void rpc().cancel({ runID }, { location: location(base) }).catch(() => {})
    update(draft => {
      delete draft.running[base]
      if (draft.jobs[runID]) { draft.jobs[runID]!.pending = false; draft.jobs[runID]!.text = 'Obs command interrupted by plugin reload.' }
    })
  }

  async function sync(id: string, signal: AbortSignal): Promise<void> {
    const source = await captureSession(context.client, id, signal)
    await rpc().sync({ source }, { location: location(id), signal })
  }

  async function refresh(id: string): Promise<void> {
    if (disposed || id === 'home') return
    if (refreshing.has(id)) { dirty.add(id); return }
    const generation = claim(id)
    refreshing.add(id)
    const controller = new AbortController()
    aborts.add(controller)
    try {
      const file = state.files[id]
      if (!file) await sync(id, controller.signal)
      const origin = file?.origin ?? id
      const snapshot = await rpc().snapshot({ sessionID: origin, useSynced: true, ...(file ? { selector: file.selector, ...(file.captureSessionID ? { captureSessionID: file.captureSessionID } : {}) } : {}) }, { location: location(origin), signal: controller.signal })
      if (!disposed && generations.get(id) === generation) update(draft => { draft.snapshots[id] = snapshot; delete draft.errors[id] })
    } catch (error) {
      if (!disposed && !controller.signal.aborted && generations.get(id) === generation) update(draft => { draft.errors[id] = errorText(error) })
    } finally {
      aborts.delete(controller)
      refreshing.delete(id)
      if (dirty.delete(id) && !disposed) schedule(id)
    }
  }

  function schedule(id: string): void {
    if (disposed || timers.has(id)) return
    timers.set(id, setTimeout(() => { timers.delete(id); void refresh(id) }, 350))
  }

  function changed(id: string): void {
    if (!settings.watch) return
    const base = active()
    if (!base) return
    const target = selected(base)
    if (state.files[target]) return
    const visible = context.ui.panel.current()?.name === PANEL || settings.statusline
    if (visible && (target === id || context.data.session.family(target).includes(id))) schedule(target)
  }

  let stopChanged = () => {}
  let stopProgress = () => {}
  function subscribe(): void {
    stopChanged()
    stopProgress()
    stopChanged = rpc().events.on('changed', event => changed(event.data.sessionID))
    stopProgress = rpc().events.on('progress', event => {
      const job = state.jobs[event.data.runID]
      if (job?.pending) update(draft => { draft.jobs[event.data.runID]!.text = displayResult(event.data.text) })
    })
  }
  subscribe()
  const stopEvents = context.data.listen(({ details }) => {
    if (details.type === 'server.connected') {
      subscribe()
      const base = active()
      if (base && (settings.statusline || context.ui.panel.current()?.name === PANEL)) schedule(selected(base))
    } else if (/^session\.(?:execution\.|compaction\.|tool\.|usage\.|revert\.|deleted|moved)/.test(details.type)) {
      const data = details.data as unknown as { sessionID?: unknown }
      if (typeof data.sessionID === 'string') changed(data.sessionID)
    }
  })

  async function cancel(base?: string): Promise<void> {
    const id = base ?? active() ?? 'home'
    const runID = state.running[id]
    if (!runID) { notice('No obs command is running for this session.'); return }
    jobs.get(runID)?.abort()
    try {
      const result = await rpc().cancel({ runID }, { location: location(id) })
      notice(result.cancelled ? 'Cancelling obs command…' : 'The obs command has already finished.')
    } catch (error) { notice(errorText(error), true) }
  }

  async function openURL(value: string): Promise<void> {
    try {
      const url = new URL(value)
      if (url.protocol !== 'http:' && url.protocol !== 'https:') throw new Error('Only HTTP dashboard URLs can be opened.')
      const command = process.platform === 'darwin' ? ['open', url.href] : process.platform === 'win32' ? ['rundll32.exe', 'url.dll,FileProtocolHandler', url.href] : ['xdg-open', url.href]
      const child = Bun.spawn(command, { stdin: 'ignore', stdout: 'ignore', stderr: 'ignore' })
      if (await child.exited) notice(`Open this URL in your browser: ${url.href}`)
    } catch (error) { notice(`Browser unavailable: ${errorText(error)}. URL: ${value}`, true) }
  }

  const Action = (props: { label: string; run(): void }): ReturnType<typeof Text> => <text fg={context.theme.text.base} onMouseDown={props.run}>{`[${props.label}]`}</text>
  const Text = (props: { children: string; muted?: boolean }) => <text fg={props.muted ? context.theme.text.muted : context.theme.text.base} wrapMode="word">{safeText(props.children)}</text>

  function showResult(runID: string): void {
    context.ui.dialog.set({ size: 'large', centered: true })
    context.ui.dialog.show(() => <Result runID={runID} />)
  }

  function Result(props: { runID: string }) {
    const job = () => state.jobs[props.runID]
    const terminal = useTerminalDimensions()
    let scroll: ScrollBoxRenderable | undefined
    const livePane = () => {
      const current = job()
      const base = active() ?? current?.sessionID
      if (current?.targetID && base) update(draft => { draft.selected[base] = current.targetID! })
      context.ui.dialog.clear(); void command('pane')
    }
    const browser = () => { const url = job()?.url; if (url) void openURL(url) }
    context.keymap.layer(() => ({ mode: 'global', commands: [
      { bind: 'c', title: 'Cancel obs command', enabled: () => !!job()?.pending, run: () => cancel(job()?.sessionID) },
      { bind: 'p', title: 'Open live pane', run: livePane },
      { bind: 'o', title: 'Open dashboard URL', enabled: () => !!job()?.url, run: browser },
      { bind: 'x', title: 'Close result', run: () => context.ui.dialog.clear() },
      { bind: 'down', title: 'Scroll down', run: () => scroll?.scrollBy(1) },
      { bind: 'up', title: 'Scroll up', run: () => scroll?.scrollBy(-1) },
      { bind: 'pagedown', title: 'Next page', run: () => scroll?.scrollBy(Math.max(1, terminal().height - 16)) },
      { bind: 'pageup', title: 'Previous page', run: () => scroll?.scrollBy(-Math.max(1, terminal().height - 16)) },
      { bind: 'home', title: 'Start of result', run: () => scroll?.scrollTo(0) },
      { bind: 'end', title: 'End of result', run: () => scroll?.scrollTo(scroll.scrollHeight) },
    ] }))
    return <box flexDirection="column" gap={1} padding={1}>
      <Text>{`TrajViz Obs · ${(job()?.args || 'overview').slice(0, 100)}`}</Text>
      <scrollbox ref={scroll} height={Math.max(3, Math.min(24, terminal().height - 14))} scrollX={false}>
        <Text>{job()?.text || 'Loading…'}</Text>
      </scrollbox>
      <box flexDirection="row" flexWrap="wrap" gap={2}>
        <Show when={job()?.pending}><Action label="c Cancel" run={() => { void cancel(job()?.sessionID) }} /></Show>
        <Show when={job()?.url}><Action label="o Open URL" run={browser} /></Show>
        <Action label="p Live pane" run={livePane} />
        <Action label="x Close" run={() => context.ui.dialog.clear()} />
      </box>
      <Text muted>↑↓ or PgUp/PgDn to scroll · Esc to close</Text>
    </box>
  }

  async function execute(args: string, base: string, presentation: 'dialog' | 'pane'): Promise<void> {
    if (state.running[base]) { notice('An obs command is running. Use /obs cancel to stop it.'); return }
    const runID = crypto.randomUUID()
    const controller = new AbortController()
    aborts.add(controller)
    jobs.set(runID, controller)
    update(draft => {
      draft.running[base] = runID
      draft.jobs[runID] = { args, sessionID: base, text: `Running /obs ${args || 'overview'}…`, pending: true }
    })
    if (presentation === 'dialog') showResult(runID)
    let target = base
    let generation: number | undefined
    try {
      const parsed = parseArgs(args)
      if (!SESSIONLESS.has(parsed.sub)) {
        // Questions preserve apostrophes; the generic shell lexer can treat one as an open quote.
        const analyze = parsed.sub === 'analyze' ? analyzeSelection(args) : undefined
        const selector = analyze ? analyze.session : parsed.flags.session
        const captureSessionID = analyze ? analyze.captureSessionId : parsed.flags['session-id']
        if (base === 'home' && typeof selector !== 'string') throw new Error('Open a session before using this command, or select a saved session with --session. /obs help and /obs doctor are available here.')
        if (typeof selector === 'string') {
          const resolved = await rpc().resolve({ sessionID: base, selector, ...(typeof captureSessionID === 'string' ? { captureSessionID } : {}) }, { location: location(base), signal: controller.signal })
          target = resolved.sessionID
          generation = claim(target)
          if (resolved.path) update(draft => { draft.files[target] = { origin: base, selector: resolved.path!, ...(typeof captureSessionID === 'string' ? { captureSessionID } : {}) } })
          else await sync(target, controller.signal)
        } else { generation = claim(target); await sync(target, controller.signal) }
        if (parsed.sub === 'compare' || parsed.sub === 'scorecard') for (const other of parsed.positional) {
          const resolved = await rpc().resolve({ sessionID: base, selector: other }, { location: location(base), signal: controller.signal })
          if (!resolved.path) await sync(resolved.sessionID, controller.signal)
        }
      }
      if (parsed.sub === 'collect') {
        const collect = passArgs(args)
        const selector = collect[1]
        if (collect[0] === 'opencode' && collect.length >= 2 && collect.length <= 3 && selector && selector !== 'help' && !selector.startsWith('-')) {
          const resolved = await rpc().resolve({ sessionID: base, selector }, { location: location(base), signal: controller.signal })
          if (!resolved.path) await sync(resolved.sessionID, controller.signal)
        }
      }
      const selectedModel = context.ui.model.current()
      const model = selectedModel ? { providerID: selectedModel.providerID, id: selectedModel.modelID, ...(selectedModel.variant === undefined ? {} : { variant: selectedModel.variant }) } : undefined
      let result: CommandResult = await rpc().run({ sessionID: base, args, runID, model, useSynced: true }, { location: location(base), signal: controller.signal })
      if (result.confirmation) {
        update(draft => { draft.jobs[runID]!.text = result.text })
        const confirmed = await context.ui.dialog.confirm({ title: safeText(result.confirmation.title), message: safeText(result.confirmation.message), label: { confirm: 'Run', cancel: 'Cancel' } })
        if (confirmed) result = await rpc().run({ sessionID: base, args, runID, model, useSynced: true, confirmationID: result.confirmation.id }, { location: location(base), signal: controller.signal })
        else {
          await rpc().cancel({ runID }, { location: location(base) })
          result = { text: `${result.text}\nCancelled: no model call was made.` }
        }
        if (presentation === 'dialog') showResult(runID)
      }
      target = result.sessionID ?? target
      if (disposed) return
      update(draft => {
        if (base !== 'home' && !SESSIONLESS.has(parsed.sub)) draft.selected[base] = target
        if (result.snapshot && generations.get(target) === generation) { draft.snapshots[target] = result.snapshot; delete draft.errors[target] }
        draft.jobs[runID] = { args, sessionID: base, ...(!SESSIONLESS.has(parsed.sub) ? { targetID: target } : {}), text: displayResult(commandText(result)), pending: false, url: result.url, path: result.path }
        if (result.tab) { const tab = TABS.findIndex(name => name.toLowerCase() === result.tab?.toLowerCase()); if (tab >= 0) draft.tab = tab }
      })
      if (presentation === 'pane' && !result.snapshot) await refresh(target)
      if (parsed.sub === 'label' && result.path && presentation !== 'pane') await refresh(target)
      if (presentation === 'pane' && result.text && !result.snapshot) notice(result.text)
      if (result.url && context.options.openBrowser === true) void openURL(result.url)
    } catch (error) {
      if (!disposed) {
        const message = controller.signal.aborted ? 'Obs command cancelled.' : errorText(error)
        update(draft => {
          draft.jobs[runID]!.text = message; draft.jobs[runID]!.pending = false
          if (presentation === 'pane' && generations.get(target) === generation) draft.errors[target] = message
        })
      }
    } finally {
      aborts.delete(controller)
      jobs.delete(runID)
      if (!disposed) update(draft => {
        delete draft.running[base]
        if (draft.jobs[runID]) draft.jobs[runID]!.pending = false
        for (const id of Object.keys(draft.jobs).slice(0, -8)) if (!draft.jobs[id]?.pending) delete draft.jobs[id]
      })
    }
  }

  async function command(raw = '', followSelection = false): Promise<void> {
    let args = raw.trim().replace(/^\/obs(?:\s+|$)/, '')
    const base = active() ?? 'home'
    if (followSelection) {
      const target = selected(base)
      const file = state.files[target]
      if (file) args += ` ${quoteOption('session', file.selector)}${file.captureSessionID ? ` ${quoteOption('session-id', file.captureSessionID)}` : ''}`
      else if (target !== base) args += ` ${quoteOption('session', target)}`
    }
    const invalid = validateArguments(args)
    if (invalid) { notice(invalid, true); return }
    const parsed = parseArgs(args)
    if (parsed.sub === 'cancel') { await cancel(base); return }
    if (parsed.sub === 'watch' || parsed.sub === 'statusline') {
      const option = parsed.positional[0]
      if (option && option !== 'on' && option !== 'off') { notice(`Usage: /obs ${parsed.sub} [on|off]`, true); return }
      const key = parsed.sub
      const enabled = option ? option === 'on' : !settings[key]
      await updateSettings(draft => { draft[key] = enabled })
      notice(`${key} ${enabled ? 'on' : 'off'}`)
      if (enabled && base !== 'home') await refresh(selected(base))
      return
    }
    if (parsed.sub === 'pane') {
      if (parsed.positional[0] === 'close') { context.ui.panel.close(); notice('Pane closed.'); return }
      if (base === 'home') { notice('Open a session before opening the obs pane.', true); return }
      if (!context.ui.panel.open(PANEL)) { notice('This surface cannot open a session panel.', true); return }
      if (parsed.flags.session) await execute(args.replace(/^pane\b/, 'overview'), base, 'pane')
      else await refresh(selected(base))
      return
    }
    if (!args) update(draft => { delete draft.selected[base] })
    await execute(args, base, 'dialog')
  }

  function Pane(props: { panel: Panel }) {
    const id = () => selected(props.panel.sessionID)
    const snapshot = () => state.snapshots[id()]
    const terminal = useTerminalDimensions()
    const compact = () => terminal().height < 34
    onMount(() => props.panel.focus())
    createEffect(() => { const target = id(); if (!state.snapshots[target] && !state.errors[target]) void refresh(target) })
    context.keymap.layer(() => ({
      mode: 'global', enabled: () => props.panel.focused,
      commands: [
        ...TABS.map((name, index) => ({ bind: String(index + 1), title: name, run: () => { update(draft => { draft.tab = index }) } })),
        { bind: 'r', title: 'Refresh', run: () => refresh(id()) },
        { bind: 'w', title: 'Toggle watch', run: () => command('watch') },
        { bind: 'd', title: 'Dashboard', run: () => command('dashboard', true) },
        { bind: 'f', title: 'Toggle fullscreen', run: () => props.panel.toggleFullscreen() },
        { bind: 'x', title: 'Close pane', run: () => props.panel.close() },
      ],
    }))
    return <box flexDirection="column" flexGrow={1} minHeight={0} padding={1} gap={1} onMouseDown={() => props.panel.focus()}>
      <box flexDirection="row" flexWrap="wrap" gap={1}>
        <For each={TABS}>{(name, index) => <text fg={context.theme.text.base} onMouseDown={() => update(draft => { draft.tab = index() })}>{`${state.tab === index() ? '▸' : ''}${index() + 1} ${name}`}</text>}</For>
      </box>
      <box flexDirection="row" flexWrap="wrap" gap={2}>
        <Action label="r Refresh" run={() => { void refresh(id()) }} />
        <Action label={`w Watch ${settings.watch ? 'on' : 'off'}`} run={() => { void command('watch') }} />
        <Action label="d Dashboard" run={() => { void command('dashboard', true) }} />
        <Action label="x Close" run={() => props.panel.close()} />
      </box>
      <Text muted>{`${id() === props.panel.sessionID ? 'Current session' : 'Saved session'}${state.files[id()]?.captureSessionID ? ` · ${state.files[id()]!.captureSessionID}` : ''}${snapshot() && !snapshot()!.empty && (snapshot() as Snapshot).title ? ` · ${(snapshot() as Snapshot).title}` : ''}`}</Text>
      <scrollbox flexGrow={1} minHeight={0} scrollX={false} contentOptions={{ gap: 1 }}>
        <Show when={state.errors[id()]}><Text>{`Unable to refresh: ${state.errors[id()]}. Press r to retry.`}</Text></Show>
        <Show when={state.tab === 0 && snapshot() && !snapshot()!.empty}>
          <box flexDirection="row" flexWrap="wrap" columnGap={1} rowGap={compact() ? 0 : 1}>
            <For each={metrics(snapshot() as Snapshot)}>{metric => <box flexDirection="column" width={Math.max(16, Math.floor((props.panel.width - 6) / 2))}>
              <Show when={!compact()} fallback={<Text>{`${metric.label}: ${metric.value}`}</Text>}>
                <Text muted>{metric.label}</Text><Text>{metric.value}</Text><Show when={metric.detail}><Text muted>{metric.detail!}</Text></Show>
              </Show>
            </box>}</For>
          </box>
        </Show>
        <For each={sections(snapshot(), state.tab, props.panel.width - 4)}>{section => <box flexDirection="column">
          <Text>{section.title}</Text><Text muted={section.title === 'Notes'}>{section.lines.join('\n')}</Text>
        </box>}</For>
      </scrollbox>
      <Text muted>{snapshot() && !snapshot()!.empty ? `Updated ${localTime((snapshot() as Snapshot).generated_at)} · ${(snapshot() as Snapshot).counts.assistant_steps} steps · watch ${settings.watch ? 'on' : 'off'}` : 'Dashboard provides the full interactive workflow.'}</Text>
    </box>
  }

  const stopPanel = context.ui.slot({ append: 'session.panel', render: panel => <Show when={panel.name === PANEL}>
    <ErrorBoundary fallback={() => <box padding={1} flexDirection="column" gap={1}><Text>Unable to draw the obs pane.</Text><Action label="Retry" run={() => { context.ui.panel.close(); void command('pane') }} /><Action label="Close" run={() => panel.close()} /></box>}>
      <Pane panel={panel} />
    </ErrorBoundary>
  </Show> })
  const stopFooter = context.ui.slot({ append: 'prompt.footer.status', render: input => {
    const id = () => input.sessionID ? selected(input.sessionID) : undefined
    createEffect(() => { const target = id(); if (settings.statusline && target && !state.snapshots[target] && !state.errors[target]) void refresh(target) })
    return <Show when={settings.statusline}><text fg={context.theme.text.muted}>{safeText(id() ? statusText(state.snapshots[id()!]) : 'obs: open a session')}</text></Show>
  } })
  context.keymap.layer(() => ({ mode: 'global', commands: [{ id: 'trajviz-obs.command', title: 'TrajViz Obs', description: 'Visualize and diagnose session activity', group: 'TrajViz Obs', palette: true, slash: { name: 'obs', arguments: true }, run: input => command(input) }] }))

  return () => {
    disposed = true
    stopChanged(); stopProgress(); stopEvents(); stopPanel(); stopFooter()
    for (const timer of timers.values()) clearTimeout(timer)
    for (const controller of aborts) controller.abort()
    for (const [base, runID] of Object.entries(state.running)) if (jobs.has(runID)) {
      void rpc().cancel({ runID }, { location: location(base) }).catch(() => {})
      update(draft => {
        if (draft.running[base] === runID) delete draft.running[base]
        if (draft.jobs[runID]) { draft.jobs[runID]!.pending = false; draft.jobs[runID]!.text = 'Obs command interrupted by plugin reload.' }
      })
    }
    context.ui.panel.close()
  }
}

export default Plugin.define({ id: 'trajviz-obs.tui', setup })
