import { Command, ComposerPill, MenuItem, Panel, Plugin, Store, Style, createKeyed, type SessionRef, type PanelProps } from '@opencode/plugin/gui'
import { Schema } from 'effect'
import { ErrorBoundary, For, Show, createEffect, createSignal, onCleanup, onMount } from 'solid-js'
import { DesktopCharts } from './gui-charts.js'
import { captureSession } from './collect.js'
import { GuiController, type GuiOrigin } from './gui-controller.js'
import { Obs } from './rpc.js'
import { parseArgs, tokenize, quoteArgument } from './shared/args.js'
import { validateArguments } from './command-args.js'
import { usageFeature } from './usage-features.js'
import { errorMessage as errorText, safeText, TABS, compact, duration, failures, metrics, sections, statusText } from './tui-model.js'
import { agentFailureTone, healthRowTone, healthStatusLabel, metricTone, resultFragments } from './presentation.js'

const PANEL = 'main'
const declaration = Plugin.define({ stores: { settings: Store.global(Schema.Struct({ watch: Schema.NullOr(Schema.Boolean), statusline: Schema.NullOr(Schema.Boolean) }), { watch: null, statusline: null }) } })
type GuiContext = Parameters<Plugin.Setup<typeof declaration>>[0]
const css = `
.obs-ui{--obs-danger:color-mix(in srgb,var(--v2-state-fg-danger,var(--text-diff-delete-base,var(--text-on-critical-base,var(--icon-critical-base,light-dark(#b82d35,#f17471))))) 70%,var(--text-strong));--obs-success:color-mix(in srgb,var(--v2-state-fg-success,var(--text-diff-add-base,var(--text-on-success-base,var(--icon-success-base,light-dark(#198b43,#6bd586))))) 55%,var(--text-strong));--obs-warning:color-mix(in srgb,var(--v2-state-fg-warning,var(--icon-warning-base,light-dark(#cb9f34,#f2cf76))) 45%,var(--text-strong));--obs-agent:color-mix(in srgb,var(--icon-agent-plan-base) 85%,var(--text-strong));--obs-info:color-mix(in srgb,var(--text-interactive-base) 90%,var(--text-strong));--obs-neutral:color-mix(in srgb,var(--text-base) 85%,var(--text-strong));font:inherit;font-size:13px;line-height:1.5;color:inherit;min-width:0;box-sizing:border-box}
.obs-ui *{box-sizing:border-box}.obs-ui button,.obs-ui input{font:inherit;color:inherit}
.obs-pane{height:100%;min-height:0;display:flex;flex-direction:column;overflow:hidden}
.obs-header{padding:14px 16px 10px;flex-shrink:0;border-bottom:1px solid color-mix(in srgb,currentColor 14%,transparent)}
.obs-heading{font-size:15px;font-weight:600;margin:0;overflow-wrap:anywhere}.obs-muted{color:var(--obs-neutral)}.obs-small{font-size:12px}
.obs-row{display:flex;align-items:center;flex-wrap:wrap;gap:8px}.obs-actions{margin-top:10px}.obs-button{appearance:none;border:1px solid color-mix(in srgb,currentColor 20%,transparent);border-radius:6px;padding:5px 9px;background:transparent;cursor:pointer;min-height:30px;white-space:normal;text-align:center}
.obs-button:hover{background:color-mix(in srgb,currentColor 7%,transparent)}.obs-button:focus-visible,.obs-input:focus-visible,.obs-scroll:focus-visible{outline:2px solid var(--border-interactive-focus,var(--text-interactive-base));outline-offset:2px}
.obs-button:disabled{opacity:.4;cursor:default}.obs-tabs{display:flex;gap:5px;flex-wrap:wrap;margin-top:12px}.obs-tab[aria-selected=true]{color:var(--obs-info);font-weight:600;background:color-mix(in srgb,currentColor 10%,transparent);border-color:currentColor}
.obs-scroll{container:obs-content / inline-size;overflow:auto;min-height:0;flex:1;padding:16px;overscroll-behavior:contain;overflow-wrap:anywhere}
.obs-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-bottom:18px}.obs-card{padding:11px 12px;border:1px solid color-mix(in srgb,currentColor 14%,transparent);border-radius:8px;min-width:0}.obs-value{font-size:20px;font-weight:600;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.obs-tone-neutral{color:var(--obs-neutral)}.obs-tone-info{color:var(--obs-info)}.obs-tone-success{color:var(--obs-success)}.obs-tone-warning{color:var(--obs-warning)}.obs-tone-danger{color:var(--obs-danger)}.obs-tone-agent{color:var(--obs-agent)}.obs-key,.obs-emphasis{font-weight:600}.obs-label{font-weight:500;color:var(--obs-neutral)}.obs-chips{display:flex;flex-wrap:wrap;gap:7px}.obs-chip{border:1px solid color-mix(in srgb,currentColor 28%,transparent);border-radius:6px;padding:4px 7px;background:color-mix(in srgb,currentColor 5%,transparent);font-size:12px}.obs-chip-status{font-weight:600}.obs-agent-name{font-weight:600}.obs-button[aria-pressed=true]{color:var(--obs-info);border-color:var(--obs-info)}
.obs-section{margin:0 0 20px}.obs-section h3{font-size:13px;font-weight:600;margin:0 0 7px;text-transform:capitalize}.obs-line{margin:0 0 6px;white-space:pre-wrap;overflow-wrap:anywhere}.obs-section[data-trend=true] .obs-line{font-family:ui-monospace,monospace;font-size:12px}
.obs-charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,310px),1fr));gap:12px;margin-bottom:18px}.obs-charts>.obs-chart:last-child:nth-child(odd){grid-column:1/-1}.obs-chart{border:1px solid color-mix(in srgb,currentColor 14%,transparent);border-radius:8px;padding:10px;min-width:0}.obs-chart h4{font-size:13px;font-weight:600;color:var(--obs-info);margin:0 0 6px}.obs-chart-plot{min-width:0;width:100%}.obs-chart svg{display:block;fill:var(--obs-neutral);font-size:10px;overflow:visible}.obs-chart-grid{stroke:color-mix(in srgb,currentColor 15%,transparent);stroke-width:1}.obs-chart-legend{display:flex;flex-wrap:wrap;gap:4px 10px;font-size:11px;margin-top:5px}.obs-chart-legend>span{display:inline-flex;align-items:center;gap:4px}.obs-chart-swatch{width:9px;height:9px;border-radius:2px;display:inline-block;flex-shrink:0}.obs-chart-caption{font-size:11px;color:var(--obs-neutral);margin:7px 0 0}.obs-chart-empty{font-size:12px;color:var(--obs-neutral);padding:20px 0}
.obs-footer{padding:9px 16px;border-top:1px solid color-mix(in srgb,currentColor 14%,transparent);flex-shrink:0;overflow-wrap:anywhere}.obs-error{padding:12px;color:var(--obs-danger);border:1px solid color-mix(in srgb,var(--obs-danger) 60%,transparent);border-radius:6px;margin-bottom:12px}
.obs-result{width:100%;max-height:min(65vh,480px);display:flex;flex-direction:column;min-height:180px}.obs-result .obs-scroll{max-height:62vh}.obs-result pre{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;white-space:pre-wrap;overflow-wrap:anywhere;margin:0}
.obs-input{border:1px solid color-mix(in srgb,currentColor 25%,transparent);background:transparent;border-radius:6px;padding:6px 9px;min-width:0;flex:1;width:100%}.obs-command{display:flex;gap:8px;margin-top:10px}.obs-path{font-family:ui-monospace,monospace;font-size:11px;overflow-wrap:anywhere}.obs-pending{margin-bottom:12px;white-space:pre-wrap}.obs-expanded{height:min(65vh,480px);width:100%}.obs-result-actions{padding:12px 0;border-top:1px solid color-mix(in srgb,currentColor 14%,transparent);flex-shrink:0}
@container obs-content (min-width:650px){.obs-grid{grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}.obs-card{padding:10px}.obs-value{font-size:18px}}
@media(max-width:480px){.obs-header,.obs-scroll{padding:12px}.obs-grid{gap:7px}.obs-result{width:100%;max-height:65vh}.obs-command{flex-wrap:wrap}.obs-value{font-size:18px}}
`
const asRecord = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const stamp = (value: string) => { const date = new Date(value); return Number.isNaN(date.getTime()) ? '—' : date.toLocaleTimeString() }

/** Native Desktop commands do not submit prompts or alter the conversation. */
export function setup(ctx: GuiContext): () => void {
  const [revision, setRevision] = createSignal(0)
  const settings = () => ({ watch: ctx.stores.settings.value.watch ?? (ctx.options.watch !== false), statusline: ctx.stores.settings.value.statusline ?? (ctx.options.statusline === true) })
  const notice = (text: string, error = false) => { ctx.toast.show({ title: 'TrajViz Obs', description: safeText(text), variant: error ? 'error' : 'default' }) }
  const controller = new GuiController({
    changed: () => setRevision(value => value + 1), notice,
    confirm: (plan, signal) => signal.aborted ? Promise.resolve(false) : ctx.dialogs.confirm({ title: safeText(plan.title), description: safeText(plan.message), confirm: 'Run', cancel: 'Cancel' }),
  }, ctx.signal)
  const key = `${ctx.id}:${PANEL}`
  const origin = (session: SessionRef): GuiOrigin => ({ key: session.key, id: session.id, client: () => session.server.client, location: () => session.location })
  const read = <T,>(get: () => T): T => { revision(); return get() }
  const active = () => ctx.sessions.current()
  const currentOrigin = async (): Promise<GuiOrigin> => {
    const session = active()
    if (session) return origin(session)
    const server = ctx.servers.list().map(id => ctx.servers.get(id)).find(server => server?.builtin)
    if (!server) throw new Error('Open a project or connect a server first.')
    return { key: `${server.id}\nhome`, id: 'home', client: () => server.client, location: () => undefined }
  }
  const openPane = (session?: SessionRef) => {
    const target = session ?? active()
    if (!target) { notice('Open a session before opening the live pane.', true); return }
    const started = performance.now()
    let outcome: 'success' | 'error' = 'error'
    try { ctx.layout.open(key, target); outcome = 'success' }
    finally { controller.interaction(origin(target), 'pane', outcome, started) }
  }
  const openURL = (value: string) => {
    try {
      const url = new URL(value)
      if (!['http:', 'https:'].includes(url.protocol)) throw new Error('Only HTTP dashboard URLs can be opened.')
      ctx.system.openExternal(url.href)
    } catch (error) { notice(errorText(error), true) }
  }
  const selectedModel = () => {
    const model = ctx.screen.current()?.composer.model()
    return model ? { providerID: model.providerID, id: model.modelID, ...(model.variant === undefined ? {} : { variant: model.variant }) } : undefined
  }
  function showResult(id: string) { ctx.dialogs.open(dialog => <ctx.ui.Dialog title={`TrajViz Obs · ${read(() => controller.jobs.get(id)?.args) || 'Overview'}`} size="x-large"><Result id={id} close={() => dialog.close()} /></ctx.ui.Dialog>) }
  async function command(raw = '', session?: SessionRef, follow = false): Promise<void> {
    try {
      let args = raw.trim().replace(/^\/obs(?:\s+|$)/, '')
      const invalid = validateArguments(args)
      if (invalid) {
        const feature = usageFeature(args), started = performance.now()
        if (feature) void (session ? Promise.resolve(origin(session)) : currentOrigin())
          .then(source => controller.interaction(source, feature, 'rejected', started)).catch(() => {})
        throw new Error(invalid)
      }
      const source = session ? origin(session) : await currentOrigin(), parsed = parseArgs(args)
      if (parsed.sub === 'cancel') { await controller.cancel(source); return }
      if (parsed.sub === 'watch' || parsed.sub === 'statusline') {
        const option = parsed.positional[0]
        if (parsed.positional.length > 1 || option && !['on', 'off'].includes(option) || Object.keys(parsed.flags).length || parsed.passthrough.length) throw new Error(`Usage: /obs ${parsed.sub} [on|off]`)
        const enabled = option ? option === 'on' : !settings()[parsed.sub]
        const started = performance.now()
        let outcome: 'success' | 'error' = 'error'
        try { ctx.stores.settings.set({ ...ctx.stores.settings.value, [parsed.sub]: enabled }); outcome = 'success' }
        finally { controller.interaction(source, parsed.sub, outcome, started) }
        notice(`${parsed.sub} ${enabled ? 'on' : 'off'}`)
        if (enabled && source.id !== 'home' && !controller.refreshing(source)) void controller.refresh(source)
        return
      }
      if (follow) args = controller.follow(source, args)
      if (parsed.sub === 'pane') {
        if (parsed.positional[0] === 'close') { const target = session ?? active(); if (target) ctx.layout.close(key, target); return }
        const target = session ?? active()
        if (!target) {
          controller.interaction(source, 'pane', 'rejected', performance.now())
          throw new Error('Open a session before opening the live pane.')
        }
        openPane(target)
        if (parsed.flags.session) { const run = controller.start(source, args.replace(/^pane\b/, 'overview'), selectedModel(), true); await run.done }
        else await controller.refresh(source)
        return
      }
      const run = controller.start(source, args, selectedModel())
      showResult(run.id)
      await run.done
      const job = controller.jobs.get(run.id)
      if (job?.url && ctx.options.openBrowser === true) openURL(job.url)
    } catch (error) { notice(errorText(error), true) }
  }

  function Result(props: { id: string; close(): void }) {
    const job = () => read(() => controller.jobs.get(props.id))
    const rows = () => asRecord(job()?.data)
    const parsed = () => parseArgs(job()?.args ?? '')
    const offset = () => Number(parsed().flags.offset ?? 0)
    const limit = () => Number(parsed().flags.limit ?? 25)
    const paged = () => typeof rows().rows_total === 'number'
    const page = (direction: number) => {
      const current = job()
      if (!current) return
      const tokens = tokenize(current.args).filter((token, index, all) => token !== '--offset' && !token.startsWith('--offset=') && all[index - 1] !== '--offset')
      const args = tokens.map(token => /[\s"'\\]/.test(token) ? quoteArgument(token) : token).join(' ') + ` --offset ${Math.max(0, offset() + direction * limit())}`
      try { const run = controller.start(current.origin, args, selectedModel()); props.close(); showResult(run.id) } catch (error) { notice(errorText(error), true) }
    }
    const livePane = () => {
      const current = job(), session = active()
      if (!current || !session || current.origin.key !== session.key) { notice('Return to the result’s session to open its live pane.'); return }
      if (current.target && !controller.restoreResult(origin(session), current.id)) { notice('This result is no longer available. Run its command again.'); return }
      props.close(); openPane(session); void controller.refresh(origin(session))
    }
    return <div class="obs-ui obs-result">
      <Show when={job()?.pending}><div class="obs-muted obs-small" role="status">Working…</div></Show>
      <div class="obs-scroll" tabIndex={0} aria-label="Command result"><pre aria-live="polite"><For each={resultFragments(job()?.text || 'Loading…', job()?.snapshot, job()?.data)}>{fragment => <span class={`obs-tone-${fragment.tone ?? 'neutral'}${fragment.emphasis === 'key' ? ' obs-key' : fragment.emphasis === 'value' ? ' obs-emphasis' : ''}`}>{fragment.text}</span>}</For></pre><Show when={job()?.path && !job()?.text.includes(job()!.path!)}><p class="obs-path obs-muted">Saved on the server: {job()?.path}</p></Show></div>
      <footer class="obs-result-actions obs-row">
        <Show when={job()?.pending}><ctx.ui.Button onClick={() => { const current = job(); if (current) void controller.cancel(current.origin) }}>Cancel</ctx.ui.Button></Show>
        <Show when={job()?.url}><ctx.ui.Button onClick={() => openURL(job()!.url!)}>Open dashboard</ctx.ui.Button><ctx.ui.Button onClick={() => { void ctx.system.copy(job()!.url!).catch(error => notice(errorText(error), true)) }}>Copy URL</ctx.ui.Button></Show>
        <Show when={paged()}><ctx.ui.Button disabled={job()?.pending || offset() === 0} onClick={() => page(-1)}>Previous</ctx.ui.Button><span class="obs-small obs-muted">Rows {Math.min(offset() + 1, Number(rows().rows_total))}–{Math.min(offset() + limit(), Number(rows().rows_total))} of {String(rows().rows_total)}</span><ctx.ui.Button disabled={job()?.pending || offset() + limit() >= Number(rows().rows_total)} onClick={() => page(1)}>Next</ctx.ui.Button></Show>
        <ctx.ui.Button onClick={livePane}>Live pane</ctx.ui.Button><ctx.ui.Button onClick={() => { void ctx.system.copy(job()?.text ?? '').catch(error => notice(errorText(error), true)) }}>Copy result</ctx.ui.Button><ctx.ui.Button onClick={props.close}>Close</ctx.ui.Button>
      </footer>
    </div>
  }

  function PaneView(props: { session: SessionRef; expanded?: boolean; close?(): void }) {
    const source = () => origin(props.session)
    const view = () => read(() => controller.view(source()))
    const snapshot = () => read(() => controller.snapshot(source()))
    const error = () => read(() => controller.error(source()))
    const refreshing = () => read(() => controller.refreshing(source()))
    const job = () => read(() => { const id = controller.view(source()).running; return id ? controller.jobs.get(id) : undefined })
    const known = () => { const snap = snapshot(); return snap && !snap.empty ? snap : undefined }
    const measured = () => { const snap = known(); return snap ? metrics(snap) : [] }
    const displayedSections = () => sections(snapshot(), view().tab, columns()).filter(section => !(known() && (view().tab === 0 && (['Health', 'Top issues'].includes(section.title) || section.title.startsWith('Trends')) || view().tab === 2 && section.title === 'Issues' || view().tab === 4 && section.title === 'Agents')))
    const shownIssues = () => (known()?.issues ?? []).slice(0, view().tab === 0 ? 3 : undefined)
    const moreIssues = () => Math.max(0, (known()?.counts.issues ?? known()?.issues.length ?? 0) - shownIssues().length)
    const updated = () => { const snap = snapshot(); return snap && !snap.empty ? `Updated ${stamp(snap.generated_at)} · watch ${settings().watch ? 'on' : 'off'}` : 'Waiting for session data' }
    const [input, setInput] = createSignal('')
    const [columns, setColumns] = createSignal(40)
    let scroll: HTMLDivElement | undefined
    onMount(() => {
      if (!scroll) return
      const resize = () => setColumns(Math.max(16, Math.min(100, Math.floor((scroll?.clientWidth ?? 320) / 8))))
      resize()
      if (typeof ResizeObserver === 'undefined') return
      const observer = new ResizeObserver(resize)
      observer.observe(scroll)
      onCleanup(() => observer.disconnect())
    })
    const selectTab = (index: number) => { controller.selectTab(source(), index); scroll?.scrollTo({ top: 0 }) }
    const expand = () => {
      const session = props.session, started = performance.now()
      let outcome: 'success' | 'error' = 'error'
      try {
        ctx.dialogs.open(dialog => <ctx.ui.Dialog title="TrajViz Obs" size="x-large"><div class="obs-expanded"><PaneView session={session} expanded close={() => dialog.close()} /></div></ctx.ui.Dialog>)
        outcome = 'success'
      } finally { controller.interaction(origin(session), 'pane.expand', outcome, started) }
    }
    createEffect(() => {
      const current = source()
      if (!snapshot() && !error() && !refreshing()) void controller.refresh(current)
    })
    const close = () => props.close ? props.close() : ctx.layout.close(key, props.session)
    const title = () => { const snap = snapshot(); return snap && !snap.empty ? snap.title || 'Current session' : 'Current session' }
    const targetLabel = () => { const selected = view().selected; return selected?.selector ? 'Saved capture' : selected?.id && selected.id !== props.session.id ? 'Selected session' : 'Current session' }
    const focusTab = (event: KeyboardEvent, index: number) => {
      selectTab(index)
      ;(event.currentTarget as HTMLElement).querySelectorAll<HTMLButtonElement>('[role="tab"]')[index]?.focus()
    }
    const selectKey = (event: KeyboardEvent) => {
      if (event.ctrlKey || event.metaKey || event.altKey || (event.target as HTMLElement)?.closest('input,textarea,[contenteditable=true]')) return
      if (/^[1-7]$/.test(event.key)) { event.preventDefault(); selectTab(Number(event.key) - 1) }
      else if (event.key === 'ArrowRight' && (event.target as HTMLElement)?.getAttribute('role') === 'tab') { event.preventDefault(); focusTab(event, (view().tab + 1) % TABS.length) }
      else if (event.key === 'ArrowLeft' && (event.target as HTMLElement)?.getAttribute('role') === 'tab') { event.preventDefault(); focusTab(event, (view().tab + TABS.length - 1) % TABS.length) }
      else if ((event.key === 'Home' || event.key === 'End') && (event.target as HTMLElement)?.getAttribute('role') === 'tab') { event.preventDefault(); focusTab(event, event.key === 'Home' ? 0 : TABS.length - 1) }
      else if (event.key === 'f' && !props.expanded) { event.preventDefault(); expand() }
    }
    return <div class="obs-ui obs-pane" onKeyDown={selectKey}>
      <header class="obs-header"><h2 class="obs-heading">TrajViz Obs</h2><div class="obs-muted obs-small">{targetLabel()} · {safeText(title())}</div>
        <div class="obs-row obs-actions"><ctx.ui.Button disabled={refreshing()} onClick={() => { void controller.refreshPane(source()) }}>{refreshing() ? 'Refreshing…' : 'Refresh'}</ctx.ui.Button><button class="obs-button" aria-pressed={settings().watch} onClick={() => { void command('watch', props.session) }}>Watch {settings().watch ? 'on' : 'off'}</button><ctx.ui.Button onClick={() => { void command('dashboard', props.session, true) }}>Dashboard</ctx.ui.Button><Show when={!props.expanded}><ctx.ui.Button onClick={expand}>Expand</ctx.ui.Button></Show><ctx.ui.Button onClick={close}>Close</ctx.ui.Button></div>
        <nav class="obs-tabs" role="tablist" aria-label="Diagnostic views"><For each={TABS}>{(tab, index) => <button class="obs-button obs-tab" role="tab" aria-selected={view().tab === index()} tabIndex={view().tab === index() ? 0 : -1} aria-controls={`obs-${props.expanded ? 'expanded-' : ''}${props.session.id}-content`} onClick={() => selectTab(index())}>{tab}</button>}</For></nav>
      </header>
      <div class="obs-scroll" ref={scroll} tabIndex={0} role="tabpanel" id={`obs-${props.expanded ? 'expanded-' : ''}${props.session.id}-content`} aria-label={TABS[view().tab]}>
        <Show when={error()}><div class="obs-error" role="alert">{error()} <ctx.ui.Button onClick={() => { void controller.refreshPane(source()) }}>Retry</ctx.ui.Button></div></Show>
        <Show when={job()}><div class="obs-pending" role="status">{job()?.text}<div><ctx.ui.Button onClick={() => { void controller.cancel(source()) }}>Cancel</ctx.ui.Button></div></div></Show>
        <Show when={view().tab === 0 && snapshot() && !snapshot()!.empty}><div class="obs-grid"><For each={measured()}>{metric => <div class="obs-card"><div class="obs-small obs-label">{metric.label}</div><div class={`obs-value obs-tone-${metricTone(known()!, metric.label)}`}>{metric.value}</div><Show when={metric.detail}><div class="obs-small obs-muted">{metric.detail}</div></Show></div>}</For></div></Show>
        <Show when={view().tab === 0 && known()}><section class="obs-section"><h3 class="obs-tone-info">Health</h3><div class="obs-chips"><For each={known()?.health ?? []}>{row => <span class={`obs-chip obs-tone-${healthRowTone(known()!, row)}`} title={safeText(row.detail)}>{safeText(row.metric).replaceAll('_', ' ')}: {safeText(row.label)} · <span class="obs-chip-status">{healthStatusLabel(row.status)}</span></span>}</For></div></section></Show>
        <Show when={known() && [0, 1, 4, 5].includes(view().tab)}><DesktopCharts snapshot={known()!} tab={view().tab} /></Show>
        <Show when={(view().tab === 0 || view().tab === 2) && known()}><section class="obs-section"><h3 class="obs-tone-info">{view().tab === 0 ? 'Top issues' : 'Issues'}</h3><For each={shownIssues()} fallback={<p class="obs-line">{known()?.availability?.issues?.status === 'unavailable' ? 'Issues unavailable: required evidence was not recorded.' : 'No observed issues.'}</p>}>{(issue, index) => <div><p class={`obs-line obs-emphasis obs-tone-${issue.kind === 'error' ? 'danger' : 'warning'}`}>{index() + 1}. {safeText(issue.title)}<Show when={view().tab === 2}> · steps {issue.steps.join(', ') || '—'}</Show></p><Show when={view().tab === 2}><For each={[issue.evidence, issue.detail, issue.why].filter(Boolean)}>{line => <p class="obs-line">{safeText(line)}</p>}</For></Show></div>}</For><Show when={moreIssues() > 0}><p class="obs-line obs-small obs-muted">{moreIssues()} more · {view().tab === 0 ? 'open Issues for details.' : '/obs issues for details.'}</p></Show><Show when={view().tab === 0 && shownIssues().length > 0}><p class="obs-line obs-small obs-muted">Open Issues for evidence and affected steps.</p></Show></section></Show>
        <Show when={view().tab === 4 && known()}><section class="obs-section"><h3 class="obs-tone-info">Agents</h3><For each={known()?.agents ?? []} fallback={<p class="obs-line">No agent activity recorded.</p>}>{agent => <section class="obs-section"><p class="obs-line"><span class="obs-agent-name obs-tone-agent">{safeText(agent.label)}</span><Show when={agent.type}> <span class="obs-muted">({safeText(agent.type!)})</span></Show></p><Show when={typeof agent.spawned_by_step === 'number' && Number.isSafeInteger(agent.spawned_by_step) && agent.spawned_by_step >= 0}><p class="obs-line obs-small obs-tone-info">Spawned at step #{agent.spawned_by_step}</p></Show><p class="obs-line">{agent.steps} steps · {compact(agent.tokens)} tokens · {agent.tool_calls} tools · <span class={`obs-emphasis obs-tone-${agentFailureTone(agent)}`}>{failures(agent.unknown_outcomes ? agent.observed_errors ?? agent.errors : agent.errors, agent.unknown_outcomes)}</span> · {duration(agent.duration_s)}</p><Show when={agent.description}><p class="obs-line obs-muted">{safeText(agent.description!)}</p></Show></section>}</For></section></Show>
        <For each={displayedSections()}>{section => <section class="obs-section" data-trend={section.title.startsWith('Trends')}><h3 class="obs-tone-info">{section.title}</h3><For each={section.lines}>{line => <p class="obs-line">{line === 'Press 3 for evidence and affected steps.' ? 'Open Issues for evidence and affected steps.' : line}</p>}</For></section>}</For>
      </div>
      <footer class="obs-footer"><div class="obs-muted obs-small" role="status">{updated()}{view().selected?.selector ? ' · refresh saved captures manually' : ''}</div><form class="obs-command" onSubmit={event => { event.preventDefault(); const args = input().trim(); if (args) { setInput(''); void command(args, props.session, true) } }}><input class="obs-input" aria-label="Obs command" placeholder="Command, e.g. issues or label --max-steps 10" value={input()} onInput={event => setInput(event.currentTarget.value)} /><ctx.ui.Button type="submit">Run</ctx.ui.Button><ctx.ui.Button type="button" onClick={() => { void command('help', props.session) }}>Help</ctx.ui.Button></form></footer>
    </div>
  }

  ctx.add(Style, css)
  ctx.add(Command, { id: 'obs', title: 'TrajViz Obs', description: 'Inspect session health, tools, agents, and context', group: 'TrajViz Obs', slash: { name: 'obs', arguments: true }, run: args => command(args) })
  const panelTab = { id: PANEL, title: 'TrajViz Obs', closable: 'compact' as const, tabbable: true }
  ctx.add(Panel, { id: PANEL, region: 'side', wide: true, mobile: { title: 'TrajViz Obs', order: 50, kind: 'menu', icon: 'window-analytics' }, list: input => input.open.includes(PANEL) ? [panelTab] : [],
    close: ({ session }) => controller.interaction(origin(session), 'pane.close', 'success', performance.now()),
    restore: ({ session }) => openPane(session),
    render: (props: PanelProps) => <ErrorBoundary fallback={(_, reset) => <div class="obs-ui obs-scroll" role="alert"><p>Unable to draw the pane.</p><ctx.ui.Button onClick={reset}>Retry</ctx.ui.Button><ctx.ui.Button onClick={() => ctx.layout.close(key, props.session)}>Close</ctx.ui.Button></div>}><PaneView session={props.session} /></ErrorBoundary> })
  ctx.add(MenuItem, { menu: 'session.panel', id: 'open', title: 'TrajViz Obs', icon: 'window-analytics', run: () => openPane() })
  ctx.add(ComposerPill, { id: 'status', show: input => settings().statusline ? { label: read(() => statusText(controller.snapshot(origin(input.session)))), title: 'TrajViz Obs', icon: 'window-analytics', run: () => openPane(input.session) } : undefined })

  // Keep a responder on every authenticated host, including while the pane/watch is closed.
  createKeyed(() => ctx.servers.list().map(id => ctx.servers.get(id)).filter((server): server is NonNullable<typeof server> => !!server).map(server => ({ server, client: server.client })), hosts => {
    for (const { server, client } of hosts) {
      const rpc = client.rpc(Obs)
      const changed = (id: string) => {
        const session = active()
        if (!settings().watch || !session || session.server.client !== client) return
        const source = origin(session), target = controller.target(source)
        if (target.selector) return
        if ((ctx.layout.state(key, session) === 'visible' || settings().statusline) && (target.id === id || server.data.session.family(target.id).includes(id))) controller.schedule(source)
      }
      const stopChanged = rpc.events.on('changed', event => changed(event.data.sessionID))
      const stopProgress = rpc.events.on('progress', event => controller.progress(event.data.runID, event.data.text))
      const requests = new Map<string, AbortController>()
      const stopCapture = rpc.events.on('capture', event => {
        if (server.client !== client || requests.has(event.data.requestID)) return
        const request = new AbortController(); requests.set(event.data.requestID, request)
        const signal = AbortSignal.any([ctx.signal, request.signal, AbortSignal.timeout(15_000)])
        void (async () => {
          try {
            const source = await captureSession(client, event.data.sessionID, signal)
            if (server.client !== client) return
            await rpc.sync({ source, requestID: event.data.requestID }, { location: { directory: source.directory }, signal })
          } catch { /* A closed session or another window can finish this request first. */ }
          finally { requests.delete(event.data.requestID) }
        })()
      })
      const stopNative = server.data.listen(({ details }) => {
        if (details.type === 'server.connected') {
          controller.resetInteractionConsent(client)
        } else if (/^session\.(?:step\.|execution\.|compaction\.|tool\.|usage\.|revert\.|deleted|moved)/.test(details.type)) {
          const data = details.data as { sessionID?: unknown }
          if (typeof data.sessionID === 'string') changed(data.sessionID)
        }
      })
      onCleanup(() => { stopChanged(); stopProgress(); stopCapture(); stopNative(); for (const request of requests.values()) request.abort() })
    }
    const session = active()
    if (session && (settings().statusline || ctx.layout.state(key, session) === 'visible')) controller.schedule(origin(session))
  }, { equals: (before, after) => before.length === after.length && before.every((host, index) => host.client === after[index]?.client) })
  createEffect(() => {
    const session = active()
    if (settings().statusline && session) {
      const source = origin(session)
      if (!read(() => controller.snapshot(source)) && !read(() => controller.error(source)) && !controller.refreshing(source)) void controller.refresh(source)
    }
  })
  return () => controller.dispose()
}

export default Plugin.define({ ...declaration, setup })
