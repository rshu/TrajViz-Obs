import path from 'node:path'
import { readFile, realpath, stat, mkdir } from 'node:fs/promises'
import { homedir } from 'node:os'
import type { ExtensionContext } from '@earendil-works/pi-coding-agent'
import { parseArgs, flagValues, passArgs, valueArgv, dashboardPortValue } from '../../hooks/lib/args.js'
import { validateArguments, isDashboardPortCommand, pageArguments, parseExtras } from '../../shared/command-args.js'
import { atomicFile, Runtime } from '../../opencode/src/runtime.js'
import { Backend } from './backend.js'
import { dashboardPort } from './config.js'
import { runModel, type ModelConfirmation, type Result } from './model.js'
import type { Usage } from './telemetry.js'
import type { Snapshot, EmptySnapshot } from './types.js'
import { protectNativeOutput } from './outputs.js'
import type { UpdateResult } from './update.js'
import { promptExplorerArgs } from '../../shared/prompt-capture.js'

export type CommandResult = Result & Pick<UpdateResult, 'reload' | 'applyUpdate'> & { snapshot?: Snapshot | EmptySnapshot; pane?: boolean; url?: string; dashboardSessionID?: string }
export type Actions = { watch(value?: boolean): boolean; statusline(value?: boolean): boolean; closePane(): void; progress(text: string): void; confirm?: ModelConfirmation; update?(check: boolean, signal: AbortSignal): Promise<CommandResult> }
export const HELP = `/obs [command] [options] · TrajViz for Pi
Diagnostics run locally. Session commands accept --session <id|prefix|path>.

Look
  /obs · compact live overview    pane | pane close · interactive seven-tab overlay
  issues | health | patterns | agents | context | timeline · diagnostic views
  view [ID] [key=value] [--limit 1–500] [--offset N] [--json] · all 37 views
  brief    attribution [--reference-root DIR] [--agent A] [--instance I]

Browser and exports
  dashboard [--port N] [--dark] [--inline-plotly] | dashboard stop
  dashboard --html PATH · standalone HTML
  dashboard port [auto|default|N] · save a port during the session
  json [-o DIR] [--view ID] [--views-only]    bundle [-o PATH]    schemas [DIR]
  converge [--data-root DIR] [--port N] | converge stop
  compare <session|path>    scorecard <b> [<c>...]    labels <sidecar> [<other>]
  converge-cli [--] <arguments>    collect pi [output-file|-] [--session ID|PATH]
  collect <cursor|codearts> ...

AI · uses your selected Pi model, with a call preview
  analyze [question]    fixes [--limit N]    label [--max-steps N] [--assistant-only]
  label options: -o PATH --model provider/id --taxonomy PATH --max-content-chars N
    --max-tokens N --temperature N --delay SEC --user-phase TAG --user-action TAG
  More than four calls require confirmation. Escape cancels a running command.

Setup and preferences
  setup [--extras all|none|charts,app] [--source S]    doctor    sessions [1–500]
  update [--check] · update a Pi-managed Git install or review a clean local main checkout
  watch [on|off]    statusline [on|off]    cancel
  enable_telemetry | disable_telemetry    telemetry
  telemetry_destination [URL [--use-launch-token] | default]
  help

The overlay supports 1–7 tabs, arrows, scrolling, r refresh, d dashboard, w watch, Esc close.
Pi subagent extensions have no universal session relationship format; nested tool evidence is retained.`
const TABS: Record<string, string> = { issues: 'issues', health: 'health', patterns: 'hotspots', agents: 'agents', context: 'context_pressure', timeline: 'workflow' }
const asFlag = (value: string | true | undefined) => typeof value === 'string' ? value : undefined
const pretty = (value: unknown) => JSON.stringify(value, null, 2)
const rejected = (text: string): CommandResult => ({ text, outcome: 'rejected' })
const written = (doc: Record<string, unknown>): CommandResult => doc.empty
  ? { text: String(doc.reason ?? 'Nothing to analyze yet. Complete an assistant response first.'), outcome: 'empty', data: doc }
  : { text: String(doc.text ?? doc.reply ?? doc.brief ?? (doc.path ? `Written: ${doc.path}` : pretty(doc))), data: doc,
    ...(typeof doc.path === 'string' ? { path: doc.path } : {}) }

export async function runCommand(backend: Backend, ctx: ExtensionContext, input: string, signal: AbortSignal, actions: Actions, usage: Usage): Promise<CommandResult> {
  const parsed = parseArgs(input), { sub, positional, flags } = parsed
  const invalid = validateArguments(input, parsed)
  if (invalid) return rejected(invalid)
  signal.throwIfAborted()
  if (sub === 'update') return actions.update ? actions.update(flags.check === true, signal) : rejected('Plugin updates require the direct /obs update command.')
  await backend.protect?.(ctx)
  const selector = asFlag(flags.session), captured = asFlag(flags['session-id']), output = asFlag(flags.o ?? flags.out)
  if (sub !== 'analyze') await protectNativeOutput(ctx, sub === 'collect' && output === '-' ? [] : [flags.o, flags.out, flags.html])
  const source = () => backend.source(ctx, selector, signal, captured)
  const run = (argv: string[], timeout = 600_000) => backend.runtime.run<Record<string, unknown>>(argv, signal, timeout)
  if (sub === 'help') return { text: HELP }
  if (sub === 'telemetry') return { text: await usage.control('status', signal) }
  if (sub === 'telemetry_destination') return { text: await usage.control('destination', signal, positional[0], flags['use-launch-token'] === true) }
  if (sub === 'enable_telemetry' || sub === 'disable_telemetry') return { text: await usage.control(sub === 'enable_telemetry' ? 'on' : 'off', signal) }
  if (isDashboardPortCommand(parsed)) return { text: (await dashboardPort(backend.options.dashboardPort, positional[1])).text }
  if (sub === 'report') return rejected('Use /obs dashboard, or /obs dashboard --html PATH for an offline copy.')
  if (sub === 'watch' || sub === 'statusline') {
    const value = positional[0] === undefined ? undefined : positional[0] === 'on'
    return { text: `${sub} ${actions[sub](value) ? 'on' : 'off'}.` }
  }
  if (sub === 'cancel') return { text: 'No other obs command is running.' }
  if (sub === 'pane' && positional[0] === 'close') { actions.closePane(); return { text: 'Obs pane closed.' } }
  if (sub === 'overview' || sub === 'pane') {
    const snapshot = await backend.snapshot(ctx, signal, selector, captured)
    return { text: snapshot.empty ? snapshot.reason : snapshot.text, snapshot,
      ...(sub === 'pane' ? { pane: true } : {}), ...(snapshot.empty ? { outcome: 'empty' as const } : {}) }
  }
  if (sub === 'analyze' || sub === 'fixes' || sub === 'label') return runModel(backend, ctx, input, signal, actions.progress, actions.confirm)
  if (sub === 'sessions') {
    const count = positional[0] === undefined ? 20 : Number(positional[0])
    if (positional.length > 1 || !Number.isSafeInteger(count) || count < 1 || count > 500) return rejected('Usage: /obs sessions [1–500]')
    if (selector) {
      const imported = await source()
      return written(await run(['capture-sessions', '--transcript', imported.transcript, '--limit', String(count)]))
    }
    const rows = (await backend.sessions(ctx, signal)).slice(0, count)
    return { text: rows.length ? ['Pi sessions, newest first:', ...rows.map(row => `${row.id === ctx.sessionManager.getSessionId() ? '* ' : '  '}${row.id} · ${row.name ?? row.firstMessage ?? 'Untitled'}`)].join('\n') : 'No saved Pi sessions for this project.', data: rows }
  }
  if (sub === 'view' || TABS[sub]) {
    const page = pageArguments(flags)
    if (typeof page === 'string') return rejected(page)
    const selected = await source(), id = sub === 'view' ? positional[0] : TABS[sub]
    const argv = ['view', ...selected.argv, ...(id ? [id] : []), ...page]
    for (const option of sub === 'view' ? positional.slice(1) : []) argv.push('--option', option)
    if (flags.json) argv.push('--json')
    const doc = await run(argv)
    if (doc.empty) return written(doc)
    if (Array.isArray(doc.views)) return { text: `${doc.views.length} TrajViz views:\n${doc.views.map(row => `${row.id} · ${row.title}`).join('\n')}`, data: doc }
    const view = doc as { view: { title: string }; rows_total: number; notice?: string; inline: { data?: unknown; table?: string; series?: string }; path: string }
    return { text: `${view.view.title} · ${view.rows_total} rows\n${view.notice ?? ''}\n${view.inline.data === undefined ? view.inline.table ?? view.inline.series ?? '' : pretty(view.inline.data)}\nFull view: ${view.path}`, data: doc, path: view.path }
  }
  if (sub === 'json' || sub === 'bundle') {
    const selected = await source(), argv = [sub, ...selected.argv]
    if (sub === 'bundle') argv.splice(argv.indexOf('--kind'), argv.includes('--kind') ? 2 : 0)
    if (output) argv.push(...valueArgv('out', output))
    if (sub === 'json') {
      for (const id of flagValues(input, 'view')) {
        if (id === true || !id.trim()) return rejected('--view needs a view id.')
        argv.push(...valueArgv('view', id))
      }
      for (const flag of ['with-trajectory', 'views-only']) if (flags[flag]) argv.push(`--${flag}`)
    }
    return written(await run(argv))
  }
  if (sub === 'schemas') return written(await run(['schemas', ...((output ?? positional[0]) ? valueArgv('out', (output ?? positional[0])!) : [])]))
  if (sub === 'dashboard' || sub === 'converge') {
    let key = sub, argv: string[]
    const dashboardSessionID = sub === 'dashboard' ? (await source()).sessionID : undefined
    if (dashboardSessionID !== undefined) key += `:${dashboardSessionID}`
    if (positional[0] === 'stop') return { text: await backend.stop(key) ? 'Dashboard stopped.' : 'No matching dashboard is running.', ...(dashboardSessionID === undefined ? {} : { dashboardSessionID }) }
    if (positional.length) return rejected(`Usage: /obs ${sub} [options] | ${sub} stop`)
    if (flags.port !== undefined && dashboardPortValue(flags.port) === undefined) return rejected('--port must be from 1 to 65535.')
    if (sub === 'dashboard') {
      const selected = await source()
      argv = ['dashboard', '--transcript', typeof flags.html === 'string' ? selected.transcript : selected.liveTranscript ?? selected.transcript]
      if (selected.captureSessionID !== undefined) argv.push(...valueArgv('session-id', selected.captureSessionID))
      const labels = path.join(selected.artifacts, 'labels.json')
      try { await stat(labels); argv.push('--labels', labels) } catch { /* Labels are optional. */ }
      for (const flag of ['dark', 'inline-plotly']) if (flags[flag]) argv.push(`--${flag}`)
      if (typeof flags.html === 'string') return written(await run([...argv, ...valueArgv('html', flags.html)]))
      if (selected.liveTranscript) argv.push(...promptExplorerArgs(selected.sessionID))
    } else {
      const root = asFlag(flags['data-root']) ?? backend.root
      await mkdir(root, { recursive: true, mode: 0o700 })
      argv = ['converge', '--data-root', root]
    }
    const port = dashboardPortValue(flags.port) ?? (sub === 'dashboard' ? (await dashboardPort(backend.options.dashboardPort)).port : 0)
    if (port) argv.push('--port', String(port))
    const server = await backend.serve(key, argv, usage.dashboardArgs(), signal)
    let note = ''
    if (backend.options.openBrowser) {
      const opener = process.platform === 'darwin' ? ['open', server.url] : process.platform === 'win32'
        ? ['rundll32', 'url.dll,FileProtocolHandler', server.url] : ['xdg-open', server.url]
      try { await backend.runtime.execute(opener, signal, 10_000, 16_384, true) }
      catch { signal.throwIfAborted(); note = '\nThe browser could not be opened automatically. Open the URL above.' }
    }
    return { text: `${sub === 'dashboard' ? 'Session' : 'Comparison'} dashboard: ${server.url}\n/obs ${sub} stop closes it.${note}`, url: server.url,
      ...(dashboardSessionID === undefined ? {} : { dashboardSessionID }) }
  }
  if (sub === 'compare' || sub === 'scorecard') {
    if (!positional.length || (sub === 'compare' && positional.length !== 1)) return rejected(`Usage: /obs ${sub} <session|path>${sub === 'scorecard' ? ' [others...]' : ''}`)
    const selected = await source(), others: string[] = []
    for (const other of positional) others.push((await backend.source(ctx, other, signal)).transcript)
    const argv = [sub, ...selected.argv, sub === 'compare' ? '--other' : '--others', ...others]
    for (const flag of ['anchor-patch', 'labels']) if (typeof flags[flag] === 'string') argv.push(...valueArgv(flag, flags[flag]))
    for (const flag of ['fuzzy-commands', 'reverse', 'no-self']) if (flags[flag]) argv.push(`--${flag}`)
    return written(await run(argv))
  }
  if (sub === 'labels') return positional.length < 1 || positional.length > 2 ? rejected('Usage: /obs labels <sidecar> [<other sidecar>]')
    : written(await run(['labels', '--sidecar', positional[0]!, '--artifacts', path.join(backend.runtime.cache, 'label-views'), ...(positional[1] ? ['--other', positional[1]] : [])]))
  if (sub === 'attribution' || sub === 'brief') {
    const selected = await source(), argv = [sub, ...selected.argv]
    for (const flag of ['reference-root', 'agent', 'instance']) if (typeof flags[flag] === 'string') argv.push(...valueArgv(flag, flags[flag]))
    const doc = await run(argv)
    if (sub === 'attribution' && !doc.available && doc.mode !== 'trajectory_only') return { text: String(doc.reason ?? 'Attribution evidence is unavailable.'), data: doc }
    const result = written(doc)
    result.text = result.text.replace(/\bdecaf(?:[-_]attribution)?\b/gi, 'attribution')
    return result
  }
  if (sub === 'collect' || sub === 'converge-cli') {
    const pass = passArgs(input)
    if (sub === 'collect' && pass[0] === 'pi') {
      if (positional.length > 2 || Object.keys(flags).some(flag => !['session', 'o', 'out'].includes(flag))) return rejected('Usage: /obs collect pi [output-file|-] [--session ID|PATH]')
      const selected = await source()
      const original = await readFile(selected.transcript, 'utf8')
      let rows: unknown
      try { rows = JSON.parse(original) } catch { /* Native Pi files are JSONL. */ }
      const first = Array.isArray(rows) ? rows[0] : JSON.parse(original.split('\n').find(line => line.trim()) ?? 'null')
      if (first?.type !== 'session' || ![1, 2, 3].includes(first.version)) throw new Error('Pi collection requires a native Pi session or a captured Pi tree.')
      const text = Array.isArray(rows) ? rows.map(row => JSON.stringify(row)).join('\n') + '\n' : original
      const name = output ?? positional[1]
      if (name === '-') return { text: Buffer.byteLength(text) > 60_000 ? `${text.slice(0, 60_000)}\nClipped; complete capture: ${selected.transcript}` : text, path: selected.transcript }
      const destination = path.resolve(ctx.cwd, (name ?? path.join(selected.artifacts, 'pi-session.jsonl')).replace(/^~(?=[/\\])/, homedir()))
      await protectNativeOutput(ctx, [destination])
      for (const input of [selected.transcript, ctx.sessionManager.getSessionFile()].filter((file): file is string => Boolean(file))) {
        const [left, right] = await Promise.all([stat(input), stat(destination).catch(() => undefined)])
        if (await realpath(input) === await realpath(destination).catch(() => destination) || (right && left.dev === right.dev && left.ino === right.ino)) throw new Error('Collection output would overwrite a session source. Choose another path.')
      }
      await atomicFile(destination, text)
      return { text: `Pi session exported: ${destination}`, path: destination }
    }
    if (!pass.length) return { text: 'Use /obs collect pi [output-file|-] [--session ID|PATH]; other collectors: cursor, codearts.' }
    const doc = await run([sub, ...(sub === 'converge-cli' ? ['--artifacts', path.join(backend.runtime.cache, 'converge')] : []), '--', ...pass])
    return { ...written(doc), text: String(doc.text ?? doc.stdout ?? doc.path ?? pretty(doc)), ...(doc.exit ? { outcome: 'error' as const } : {}) }
  }
  if (sub === 'setup') {
    const extras = parseExtras(flags.extras)
    if (!extras) return rejected('Usage: /obs setup [--extras all|none|charts,app] [--source S]')
    const runtime = typeof flags.source === 'string' ? new Runtime({ ...backend.options, trajvizSource: flags.source }, backend.cwd, { cacheNamespace: 'pi', bridge: backend.runtime.bridge }) : backend.runtime
    try { const environment = await runtime.ready(extras, signal); return { text: `TrajViz ready: ${environment.python}\nSource: ${environment.source}\nExtras: ${environment.extras.join(', ')}` } }
    finally { if (runtime !== backend.runtime) runtime.close() }
  }
  if (sub === 'doctor') return { text: `Obs doctor\n${pretty(await run(['doctor', '--cwd', ctx.cwd]))}\nPi native extension · model mode: ${backend.options.llm ?? 'session'}` }
  return rejected('Unknown command. Run /obs help.')
}
