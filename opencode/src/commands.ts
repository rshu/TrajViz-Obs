import path from 'node:path'
import { mkdir, readFile, realpath, stat } from 'node:fs/promises'
import { homedir } from 'node:os'
import type { Backend } from './backend.js'
import type { CommandResult, RunInput } from './rpc.js'
import type { EmptySnapshot } from './types.js'
import { Runtime, atomicFile, exists } from './runtime.js'
import { flagValues, parseArgs, passArgs } from './shared/args.js'
import { pageArguments, parseExtras, validateArguments } from './command-args.js'
import { runModel } from './llm.js'

const LONG_TIMEOUT = 600_000
const EMPTY = 'Nothing to analyze yet: this session has no assistant steps. Run /obs after the first turn.'
const TABS: Record<string, string> = { issues: 'issues', health: 'health', patterns: 'hotspots', agents: 'agents', context: 'context_pressure', timeline: 'workflow' }

export const HELP_TEXT = [
  '/obs [subcommand] [options] · TrajViz for OpenCode 2',
  'Session commands accept --session <id|prefix|path>. Diagnostics run locally without model calls.',
  'Look',
  '  overview (default) · quick health summary    pane | pane close · live seven-tab panel',
  '  issues | health | patterns | agents | context | timeline · diagnostic views',
  '  view [<id>] [k=v]... [--limit 1–500] [--offset N] [--json] · catalog or one view',
  'Export',
  '  json [-o DIR] [--view ID]... [--with-trajectory]    bundle [-o PATH]    schemas [DIR]',
  'Browser',
  '  dashboard [--port N] | dashboard stop · session dashboard',
  '  converge [--data-root DIR] [--port N] | converge stop · comparison dashboard',
  'Compare',
  '  compare <session|path> [--anchor-patch P] [--fuzzy-commands] [--reverse]',
  '  scorecard <b> [<c>...] [--fuzzy-commands] [--labels a,b] [--no-self]',
  '  labels <sidecar> [<other>]    converge-cli [--] <arguments>',
  'Models · on request, with a call preview and cancellation',
  '  analyze [question]    fixes [--limit N]',
  '  label [-o PATH] [--assistant-only] [--model M] [--max-steps N]',
  '    --taxonomy PATH --user-phase TAG --user-action TAG --max-content-chars N --max-tokens N',
  '    --delay SEC spaces label calls. --max-tokens and --temperature require llm=env.',
  'Setup',
  '  setup [--extras all|none|charts,app] [--source S]    doctor    sessions [1–500]',
  '  watch [on|off]    statusline [on|off]    cancel · cancel the active obs command',
  '  attribution [--reference-root DIR] [--agent A] [--instance I]',
  '  collect opencode <id|prefix> [PATH|-] · native session and subagents',
  '  collect <cursor|codearts> ...    help',
].join('\n')

type Doc = Record<string, unknown>
type Written = { path: string; bytes?: number; files?: string[]; text?: string }
type Catalog = { views: { id: string; kind: string; title: string; options: Record<string, string> }[] }
type View = { view: { id: string; title: string }; path: string; rows_total: number; inline: { table?: string; series?: string; data?: unknown } }
const string = (value: unknown) => typeof value === 'string' ? value : String(value ?? '')
const json = (value: unknown) => JSON.stringify(value, null, 2)
const empty = (doc: object): doc is EmptySnapshot => 'empty' in doc && doc.empty === true
const flag = (value: string | true | undefined): string | undefined => typeof value === 'string' ? value : undefined

function valueOptions(flags: Record<string, string | true>, names: readonly string[], usage: string): string[] | string {
  const args: string[] = []
  for (const name of names) {
    const value = flags[name]
    if (value === true || (typeof value === 'string' && !value.trim())) return usage
    if (typeof value === 'string') args.push(`--${name}`, value)
  }
  return args
}

function attributionText(doc: Doc): string {
  // Availability messages are plugin prose; source exports retain native content.
  const publicText = (value: unknown) => (typeof value === 'string' ? value : json(value) ?? '').replace(/\bdecaf(?:[-_]attribution)?\b/gi, 'attribution')
  if (!doc.available) return publicText(doc.reason || 'Attribution is not available for this session.')
  const lines = [`Attribution (${string(doc.mode)}): ${string(doc.blame_status || 'no verdict')}`]
  if (doc.primary) lines.push(`Primary failure: ${publicText(doc.primary)}`)
  if (Array.isArray(doc.scorecard)) lines.push(...doc.scorecard.filter(row => row?.blamed).map(row => `${row.capability}: weight ${row.weight}`))
  if (Array.isArray(doc.faults) && doc.faults.length) lines.push('Faults:', ...doc.faults.map(publicText))
  if (Array.isArray(doc.notes)) lines.push(...doc.notes.map(publicText))
  return lines.join('\n')
}

const COLLECT_HELP = [
  'Usage: /obs collect opencode <session-id|prefix> [output-file|-]',
  'Exports complete OpenCode 2 history and confirmed subagents through the connected server API.',
  'The default file is native-export.json in this session’s obs artifact folder.',
  'Use - for inline JSON (display capped at 64 KiB; the complete source remains available as a file).',
  'Other collectors: /obs collect cursor --help; /obs collect codearts --help.',
].join('\n')

async function collectNative(backend: Backend, currentID: string, args: string[], signal: AbortSignal): Promise<CommandResult> {
  if (args.length === 0 || (args.length === 1 && ['--help', '-h', 'help'].includes(args[0]!))) return { text: COLLECT_HELP }
  if (args.length > 2 || !args[0]!.trim() || args[0]!.startsWith('-')
      || (args.length === 2 && !args[1]!.trim())) return { text: COLLECT_HELP }
  const source = await backend.session(currentID, args[0], signal)
  const text = await readFile(source.transcript, 'utf8')
  const native = JSON.parse(text)
  if (native.source_format !== 'opencode_v2' || native.schema_version !== 1 || native.session?.id !== source.sessionID) throw new Error('OpenCode collection needs a saved native session id or unique prefix.')
  signal.throwIfAborted()
  if (args[1] === '-') {
    const bytes = Buffer.byteLength(text)
    return { text: bytes <= 60_000 ? text : `${Buffer.from(text).subarray(0, 60_000).toString('utf8')}\n\nInline JSON clipped; complete source: ${source.transcript}`,
      path: source.transcript, data: { sessionID: source.sessionID, bytes } }
  }
  const destination = args[1]
    ? path.resolve(backend.ctx.location.directory, args[1].replace(/^~(?=[/\\])/, homedir()))
    : path.join(source.artifacts, 'native-export.json')
  const input = await realpath(source.transcript)
  const target = await realpath(destination).catch(() => path.resolve(destination))
  const [inputStat, outputStat] = await Promise.all([stat(source.transcript), stat(destination).catch(() => undefined)])
  if (input === target || (outputStat && outputStat.dev === inputStat.dev && outputStat.ino === inputStat.ino)) throw new Error('Collection output would overwrite its input session source. Choose another output path.')
  signal.throwIfAborted()
  await atomicFile(destination, text)
  return { text: `Native OpenCode session and subagents exported: ${destination} (${Buffer.byteLength(text).toLocaleString()} bytes)`, path: destination,
    data: { sessionID: source.sessionID, bytes: Buffer.byteLength(text) } }
}

/** All server operations use argument arrays; user arguments never become shell code. */
export async function runCommand(backend: Backend, input: RunInput, signal: AbortSignal): Promise<CommandResult> {
  const parsed = parseArgs(input.args), { sub, positional, flags } = parsed
  const invalid = validateArguments(input.args, parsed)
  if (invalid) return { text: invalid }
  signal.throwIfAborted()
  const selector = flag(flags.session)
  const output = flag(flags.o ?? flags.out)
  const session = () => backend.session(input.sessionID, selector, signal)
  const run = <T>(argv: string[], timeout = LONG_TIMEOUT) => backend.runtime.run<T>(argv, signal, timeout)
  const result = (doc: Written | EmptySnapshot, label = 'Written'): CommandResult => empty(doc)
    ? { text: EMPTY } : { text: doc.text ?? `${label}: ${doc.path}${doc.bytes === undefined ? '' : ` (${doc.bytes.toLocaleString()} bytes)`}`, path: doc.path, data: doc }

  if (sub === 'help') return { text: HELP_TEXT }
  if (sub === 'report') return { text: 'HTML reports have been removed. Run /obs dashboard for the full interactive view.' }
  if (sub === 'analyze' || sub === 'fixes' || sub === 'label') return runModel(backend, input, signal)
  if (sub === 'watch' || sub === 'statusline' || sub === 'cancel' || (sub === 'pane' && positional[0] === 'close')) return { text: `Use /obs ${sub}${positional.length ? ` ${positional.join(' ')}` : ''} in the OpenCode terminal UI.` }
  if (sub === 'overview' || sub === 'pane') {
    const snapshot = await backend.snapshot(input.sessionID, signal, selector)
    const target = await backend.resolve(input.sessionID, selector)
    return { text: snapshot.empty ? EMPTY : snapshot.text, snapshot, sessionID: target.sessionID }
  }
  if (sub === 'sessions') {
    const count = positional[0] === undefined ? 20 : Number(positional[0])
    if (positional.length > 1 || (positional[0] !== undefined && !/^\d+$/.test(positional[0])) || !Number.isSafeInteger(count) || count < 1 || count > 500) return { text: 'Usage: /obs sessions [N] (N from 1 to 500).' }
    const records = await backend.sessions(count)
    return { text: records.length ? ['OpenCode sessions, newest first:', ...records.map(row => `${row.id === input.sessionID ? '* ' : '  '}${string(row.id)} · ${string(row.title || 'Untitled')}`)].join('\n') : 'No saved OpenCode sessions for this project.', data: records }
  }
  if (sub === 'view' || TABS[sub]) {
    const page = pageArguments(flags)
    if (typeof page === 'string') return { text: page }
    const source = await session(), id = sub === 'view' ? positional[0] : TABS[sub]
    const args = ['view', ...source.argv]
    if (id) args.push(id)
    for (const option of sub === 'view' ? positional.slice(1) : []) args.push('--option', option)
    args.push(...page)
    if (flags.json !== undefined) args.push('--json')
    const doc = await run<EmptySnapshot | Catalog | View>(args, 120_000)
    if (empty(doc)) return { text: EMPTY, sessionID: source.sessionID }
    if ('views' in doc) return { text: [`${doc.views.length} TrajViz views; /obs view <id> inspects one:`, ...doc.views.map(v => `${v.id} · ${v.title}${Object.keys(v.options).length ? ` (${Object.keys(v.options).join(', ')})` : ''}`)].join('\n'), data: doc, sessionID: source.sessionID }
    return { text: [`${doc.view.title} · ${doc.rows_total} rows`, doc.inline.data === undefined ? doc.inline.table ?? doc.inline.series ?? '' : json(doc.inline.data), `Full view: ${doc.path}`].join('\n'), path: doc.path, data: doc, sessionID: source.sessionID, tab: sub === 'view' || sub === 'health' ? undefined : sub }
  }
  if (sub === 'json' || sub === 'bundle') {
    const views = sub === 'json' ? flagValues(input.args, 'view') : []
    if (views.some(v => v === true || !v.trim())) return { text: '--view needs a view id (/obs view lists them).' }
    const source = await session()
    const args = sub === 'json' ? ['json', ...source.argv] : ['bundle', '--transcript', source.transcript, '--artifacts', source.artifacts]
    if (output) args.push('--out', output)
    for (const id of views) args.push('--view', String(id))
    if (flags['with-trajectory'] !== undefined) args.push('--with-trajectory')
    return { ...result(await run<Written | EmptySnapshot>(args), sub === 'json' ? 'View data written' : 'Session bundle written'), sessionID: source.sessionID }
  }
  if (sub === 'schemas') return result(await run<Written>(['schemas', ...((output ?? positional[0]) ? ['--out', (output ?? positional[0])!] : [])]), 'View schemas written')
  if (sub === 'dashboard' || sub === 'converge') {
    if (positional.length === 1 && positional[0] === 'stop') {
      const target = sub === 'dashboard' ? (await backend.resolve(input.sessionID, selector)).sessionID : undefined
      return { text: await backend.stop(sub, target) ? `${sub === 'dashboard' ? 'Session' : 'Converge'} dashboard stopped.` : 'No matching dashboard is running.' }
    }
    const port = flags.port === undefined ? undefined : Number(flags.port)
    const root = flags['data-root']
    if (positional.length || (flags.port !== undefined && (typeof flags.port !== 'string' || !/^\d+$/.test(flags.port) || !Number.isSafeInteger(port) || port! < 1 || port! > 65535)) || root === true || (typeof root === 'string' && !root.trim())) return { text: `Usage: /obs ${sub} ${sub === 'converge' ? '[--data-root DIR] ' : ''}[--port 1–65535] | ${sub} stop` }
    let args: string[], target: string | undefined
    if (sub === 'dashboard') {
      const source = await session(); target = source.sessionID
      args = ['dashboard', '--transcript', source.transcript]
      const window = source.argv.indexOf('--window')
      if (window >= 0) args.push('--window', source.argv[window + 1]!)
      const labels = path.join(source.artifacts, 'labels.json')
      if (await exists(labels)) args.push('--labels', labels)
    } else {
      const dataRoot = flag(root) ?? backend.root
      if (root === undefined) await mkdir(dataRoot, { recursive: true, mode: 0o700 })
      args = ['converge', '--data-root', dataRoot]
    }
    if (port !== undefined) args.push('--port', String(port))
    const server = await backend.serve(sub, args, signal)
    return { text: `${sub === 'dashboard' ? 'Session' : 'Converge'} dashboard: ${server.url}${server.preloaded === false ? '\nSession preload was unavailable; upload /obs bundle in the dashboard.' : ''}\n/obs ${sub} stop closes it. For a remote server, forward this localhost port to your machine.`, url: server.url, sessionID: target }
  }
  if (sub === 'compare' || sub === 'scorecard') {
    if (!positional.length || (sub === 'compare' && positional.length !== 1)) return { text: `Usage: /obs ${sub} <session|path>${sub === 'scorecard' ? ' [<session|path>...]' : ''}` }
    const extras = valueOptions(flags, sub === 'compare' ? ['anchor-patch'] : ['labels'], `/obs ${sub}: option needs a value.`)
    if (typeof extras === 'string') return { text: extras }
    const source = await session(), others: string[] = []
    for (const other of positional) others.push((await backend.session(input.sessionID, other, signal)).transcript)
    const args = [sub, ...source.argv, sub === 'compare' ? '--other' : '--others', ...others, ...extras]
    for (const name of sub === 'compare' ? ['fuzzy-commands', 'reverse'] : ['fuzzy-commands', 'no-self']) if (flags[name] !== undefined) args.push(`--${name}`)
    return { ...result(await run<Written | EmptySnapshot>(args)), sessionID: source.sessionID }
  }
  if (sub === 'labels') {
    if (!positional.length || positional.length > 2) return { text: 'Usage: /obs labels <sidecar> [<other sidecar>]' }
    const args = ['labels', '--sidecar', positional[0]!, '--artifacts', path.join(backend.runtime.cache, 'label-views')]
    if (positional[1]) args.push('--other', positional[1])
    return result(await run<Written>(args))
  }
  if (sub === 'collect' || sub === 'converge-cli') {
    const pass = passArgs(input.args)
    if (sub === 'collect' && (pass[0] === 'opencode' || pass.length === 0 || (pass.length === 1 && ['--help', '-h', 'help'].includes(pass[0]!)))) return collectNative(backend, input.sessionID, pass[0] === 'opencode' ? pass.slice(1) : [], signal)
    if (!pass.length) return { text: `Usage: /obs ${sub} [--] <arguments>; --help lists the supported options.` }
    const args = sub === 'collect' ? ['collect'] : ['converge-cli', '--artifacts', path.join(backend.runtime.cache, 'converge')]
    const doc = await run<Doc>([...args, '--', ...pass])
    if (typeof doc.path === 'string') return { text: `Converge report written: ${doc.path}`, path: doc.path, data: doc }
    return { text: [doc.text, doc.stdout, doc.stderr, doc.exit ? `${sub} exited with ${doc.exit}.` : ''].filter(Boolean).map(string).join('\n') || 'Command completed without output.', data: doc }
  }
  if (sub === 'attribution' || sub === 'brief') {
    const extras = valueOptions(flags, sub === 'attribution' ? ['reference-root', 'agent', 'instance'] : [], 'Usage: /obs attribution [--reference-root DIR] [--agent A] [--instance I]')
    if (typeof extras === 'string') return { text: extras }
    const source = await session(), doc = await run<Doc>([sub, ...source.argv, ...extras])
    return { text: empty(doc) ? EMPTY : sub === 'attribution' ? attributionText(doc) : string(doc.text ?? json(doc)), data: doc, sessionID: source.sessionID }
  }
  if (sub === 'setup') {
    const extras = parseExtras(flags.extras), source = flags.source
    if (!extras || source === true || (typeof source === 'string' && !source.trim())) return { text: 'Usage: /obs setup [--extras all|none|charts,app] [--source <requirement|path>]' }
    const runtime = source === undefined ? backend.runtime : new Runtime({ ...backend.options, trajvizSource: source }, backend.ctx.location.directory)
    try {
      const env = await runtime.ready(extras, signal)
      return { text: `TrajViz ready: ${env.python}\nSource: ${env.source}\nExtras: ${env.extras.join(', ') || 'none'}${source !== undefined && source !== backend.options.trajvizSource ? '\nThis source applies to setup only; set trajvizSource in plugin options to keep it.' : ''}` }
    } finally { if (runtime !== backend.runtime) runtime.close() }
  }
  if (sub === 'doctor') {
    const doc = await run<Doc>(['doctor', '--cwd', backend.ctx.location.directory], 120_000)
    return { text: `obs doctor\n${json(doc)}\nModel mode: ${backend.options.llm ?? 'session'}; OpenCode 2.2.2 plugin contract`, data: doc }
  }
  return { text: 'Unknown command. /obs help lists the supported commands.' }
}
