import { expect, mock, test, type MockClock } from 'claude-code/testing'
import type { On } from 'claude-code'
import { SESSION_DELAY_TEXT, planLine, usageLine, type PromptPlan, type Usage } from './lib/llm'
import { analyzeArgs, LIMIT_TEXT, MAX_STEPS_TEXT } from './commands/llm'
import { SNAPSHOT } from './testdata/snapshot'

const HOME = '/Users/me'
const CWD = '/Users/me/p'
const CACHE = `${HOME}/.cache/trajviz-obs`
const VENV_PY = `${CACHE}/envs/0123456789abcdef01234567/0123456789abcdef0123456789abcdef/bin/python`
const STATE_PATH = `${CACHE}/state.json`
const STATE_JSON = JSON.stringify({ revision: 5, python: VENV_PY, trajviz: '0.3.1', extras: ['llm', 'charts', 'zstd'], source: 'agent-trajviz==0.3.1', createdAt: 1 })
const DERIVED = `${HOME}/.claude/projects/-Users-me-p/abc.jsonl`
const ARTIFACTS = `${CACHE}/sessions/abc`
const USAGE = { startedAt: 0, context: { window: 1_000_000, percent: 12 }, rateLimits: [] }
const NOW = 1_761_150_000_000

/** The argv every session-bound bridge subcommand gets for the current session. */
const SESSION_ARGV = ['--transcript', DERIVED, '--window', '1000000', '--artifacts', ARTIFACTS, '--kind', 'current']

type RunAnswer = { exitCode: number; stdout: string; stderr?: string }

/** `process.run` answers for the bridge by subcommand (a list answers call by call, its last repeating); anything else cannot start. */
const bridgeOf = (by: Record<string, RunAnswer | RunAnswer[]>) => (argv: readonly string[]): RunAnswer | undefined => {
  if (argv[1] !== '-I' || !argv[2]?.endsWith('/bridge/trajviz_obs_cli.py')) return undefined
  const a = by[argv[3] ?? '']
  if (!Array.isArray(a)) return a
  return a.length > 1 ? a.shift() : a[0]
}

/** What one `model.complete` answers: a reply's text, or a failed request. */
type Reply = string | { reason: 'api-error'; status: number | null } | { reason: 'empty-reply' | 'aborted' }

/** What the mod sent to `$.model.complete`, as the kit hands it to a hook. */
type Completion = { model: string; system?: string; prompt: string; maxTokens?: number; effort?: string; timeoutMs?: number }

type World = {
  bridge?: Record<string, RunAnswer | RunAnswer[]>
  /** `model.complete` answers, call by call (the last repeating; `'ok'` when none). */
  replies?: Reply[]
  /** The session's main-loop model. */
  model?: string
  /** What the person answers an ask; `dismiss` closes the dialog. */
  answer?: string
  onCompletion?: () => void
  apply?: (argv: readonly string[], files: Record<string, string>) => Promise<RunAnswer>
}

type Host = {
  clock: MockClock
  ran: string[][]
  /** Every `process.spawn` argv (the env-mode `label`, which has no deadline). */
  spawned: string[][]
  files: Record<string, string>
  completions: Completion[]
  asked: string[]
  timeline: string[]
  rows: string[]
}

/** What every answered completion costs. */
const CALL_USAGE = { input_tokens: 1_000, output_tokens: 50, cache_read_input_tokens: 200, cache_creation_input_tokens: 300 }
const NO_USAGE = { input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 }

/** The host beneath `/obs`: env, files (writes kept), session, canned bridge answers and model replies; every run, completion and ask recorded. */
function host(on: On, world: World = {}): Host {
  const ran: string[][] = []
  const spawned: string[][] = []
  const completions: Completion[] = []
  const asked: string[] = []
  const timeline: string[] = []
  const rows: string[] = []
  const files: Record<string, string> = { [STATE_PATH]: STATE_JSON }
  const answers = bridgeOf(world.bridge ?? {})
  const replies = [...(world.replies ?? ['ok'])]
  on('command.register', () => ({ value: { command: 'obs' } }))
  on('tool.register', () => ({ value: { tool: 'mcp__trajviz-obs__obs' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: 'abc' }))
  on('session.cwd', () => ({ value: CWD }))
  on('session.usage', () => ({ value: USAGE }))
  on('session.model', () => ({ value: world.model ?? 'claude-opus-4-1-20250805' }))
  mock.env(on, { HOME })
  const clock = mock.clock(on, { now: NOW })
  on('fs.read', (_, e) => (e.path in files ? { value: files[e.path] as string } : { deny: `ENOENT: ${e.path}` }))
  on('fs.write', (_, e) => {
    files[e.path] = e.text
    return { value: undefined }
  })
  on('fs.exists', (_, e) => ({ value: e.path === VENV_PY || e.path === DERIVED }))
  on('ui.toast', () => ({ value: undefined }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.log', (_, e) => {
    if (e.to === 'transcript') rows.push(e.text)
    timeline.push(`plan:${e.text}`)
    return { value: undefined }
  })
  on('tool.call', { tool: 'AskUserQuestion' }, (_, e) => {
    const questions = (e as { questions?: { question: string }[] }).questions ?? []
    for (const q of questions) asked.push(q.question)
    timeline.push('ask')
    const answer = world.answer ?? 'Run'
    if (answer === 'dismiss') return { deny: 'the dialog was dismissed' }
    return { result: { questions, answers: Object.fromEntries(questions.map(q => [q.question, answer])) } }
  })
  on('model.complete', (_, e) => {
    timeline.push('completion')
    completions.push({ model: e.model, system: e.system, prompt: e.prompt, maxTokens: e.maxTokens, effort: e.effort, timeoutMs: e.timeoutMs })
    world.onCompletion?.()
    const r = (replies.length > 1 ? replies.shift() : replies[0]) ?? 'ok'
    if (typeof r === 'string') return { value: { isAnswered: true as const, text: r, usage: CALL_USAGE } }
    if (r.reason === 'api-error') return { value: { isAnswered: false as const, reason: 'api-error' as const, status: r.status, error: 'overloaded' as const, usage: NO_USAGE } }
    return { value: { isAnswered: false as const, reason: r.reason, usage: NO_USAGE } }
  })
  on('process.run', async (_, e) => {
    timeline.push(`bridge:${e.argv[3]}`)
    ran.push([...e.argv])
    const a = e.argv[3] === 'llm-apply' && world.apply ? await world.apply(e.argv, files) : answers(e.argv)
    if (!a) return { deny: `cannot start ${e.argv[0]}` }
    return { value: { exitCode: a.exitCode, stdout: a.stdout, stderr: a.stderr ?? '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('process.spawn', async function* (_, e) {
    timeline.push(`spawn:${e.argv[3]}`)
    spawned.push([...e.argv])
    const a = answers(e.argv)
    if (!a) return { deny: `cannot start ${e.argv[0]}` }
    if (a.stderr) yield { stream: 'stderr' as const, text: a.stderr }
    // In pieces, as a long run's output arrives.
    const cut = Math.floor(a.stdout.length / 2)
    yield { stream: 'stdout' as const, text: a.stdout.slice(0, cut) }
    yield { stream: 'stdout' as const, text: a.stdout.slice(cut) }
    return { value: { code: a.exitCode, signal: null } }
  })
  return { clock, ran, spawned, files, completions, asked, timeline, rows }
}

const json = (doc: unknown): RunAnswer => ({ exitCode: 0, stdout: `${JSON.stringify(doc)}\n` })

const obs = ($: { command: { run: (args: never) => Promise<{ text?: string; context?: readonly string[] }> } }, args: string) =>
  $.command.run({ command: 'obs', args, origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 80 } } as never)

/** The argv of every bridge call, after the launcher. */
const bridgeCalls = (ran: string[][]): string[][] => ran.filter(r => r[1] === '-I').map(r => r.slice(3))

/** The bridge's `llm-prompt` document for `feature` with `calls` prompts of ~7k tokens each. */
const planOf = (feature: PromptPlan['feature'], calls: number, extra: Partial<PromptPlan> = {}): PromptPlan => ({
  feature,
  hash: 'h1',
  plan: { calls, approx_input_tokens: 7_000 * calls },
  prompts: Array.from({ length: calls }, (_, i) => ({ index: i, system: 'SYS', prompt: `P${i}`, max_tokens: feature === 'label' ? 1024 : 2048, timeout_s: 120 })),
  path: `${ARTIFACTS}/prompts-${feature}.json`,
  bytes: 1_234,
  ...extra,
})

for (const suffix of ['.jsonl.zst', '.jsonl.zstd']) test(`compressed ${suffix} source remains intact across Claude model planning and apply`, async ($, on) => {
  const source = `/captures/session${suffix}`
  const { ran, completions } = host(on, { bridge: {
    resolve: json({ path: source, session_id: 'capture', kind: 'file' }),
    'llm-prompt': json(planOf('analyze', 1)), 'llm-apply': json({ reply: 'Controlled analysis', history_path: null }),
  } })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  expect((await obs($, `analyze --session ${source}`)).text).toContain('Controlled analysis')
  const calls = bridgeCalls(ran).filter(argv => ['llm-prompt', 'llm-apply'].includes(argv[0]!))
  expect(calls).toHaveLength(2)
  for (const argv of calls) expect(argv[argv.indexOf('--transcript') + 1]).toBe(source)
  expect(completions).toHaveLength(1)
})

const EMPTY_TEXT = 'Nothing to analyze yet: this session has no assistant steps. Run /obs after the first turn.'
const ONE_CALL_USAGE = 'usage: 1,000 input · 200 cache read · 300 cache write · 50 output tokens'

const replyFile = (files: Record<string, string>, feature: string): string => Object.keys(files).find(path => path.startsWith(`${ARTIFACTS}/replies-${feature}-`)) ?? ''

test('native AI card buttons reach the configured provider without command re-entry and print multiline results', { options: { llm: 'env' } }, async ($, on) => {
  const { ran, completions, rows } = host(on, { bridge: {
    'llm-prompt': [json(planOf('fixes', 1)), json(planOf('analyze', 1))],
    fixes: json({ issues: [], errors: [], text: 'First fix.\nSecond fix.' }),
    ask: json({ reply: 'Analysis answer.', history_path: null }),
  } })
  on('command.run', (_, e) => {
    if (e.origin.kind === 'plugin') throw new Error('native command.run re-entry')
    return { text: 'unexpected command fallback' }
  })
  on('state.get', { plugin: 'trajviz-obs', key: 'snapshot', id: '1' }, () => ({ value: { value: SNAPSHOT, version: 1 } }))
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const card = await $.ui.mount({ plugin: 'trajviz-obs', surface: 'terminal', component: 'CommandOutput', requestId: 'ai-card', props: { command: 'obs', args: '', text: 'trajviz-obs: obs snapshot 1 · now', isErrored: false }, viewport: { columns: 80, rows: 40, isFullscreen: true } })
  await card.press({ key: 'fixes' })
  await card.press({ key: 'analyze' })
  expect(bridgeCalls(ran).map(call => call[0])).toEqual(['llm-prompt', 'fixes', 'llm-prompt', 'ask'])
  expect(completions).toEqual([])
  expect(rows).toContain('First fix.')
  expect(rows).toContain('Second fix.')
  expect(rows).toContain('Analysis answer.')
  expect(rows.every(line => !line.includes('\n'))).toBe(true)
  await card.unmount()
})

test('analyze asks the question as typed, runs one completion, writes the replies and applies them', async ($, on) => {
  const { ran, files, completions, asked } = host(on, {
    bridge: {
      'llm-prompt': json(planOf('analyze', 1)),
      'llm-apply': json({ reply: 'The session spent 40% of its tokens re-reading files.', history_path: `${ARTIFACTS}/analyze.json` }),
    },
    replies: ['The session spent 40% of its tokens re-reading files.'],
  })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const out = await obs($, "analyze why's it slow?")
  // The apostrophe reaches the bridge: the question is the raw text after the subcommand, not re-joined tokens.
  expect(bridgeCalls(ran)[0]).toEqual(['llm-prompt', ...SESSION_ARGV, '--feature', 'analyze', '--question', "why's it slow?"])
  expect(completions).toEqual([{ model: 'sonnet', system: 'SYS', prompt: 'P0', maxTokens: 2048, effort: undefined, timeoutMs: 120_000 }])
  expect(asked).toEqual([])
  const repliesPath = replyFile(files, 'analyze')
  expect(repliesPath).toMatch(/replies-analyze-\d+-\d+\.json$/)
  expect(JSON.parse(files[repliesPath] as string)).toEqual({ hash: 'h1', replies: [{ index: 0, text: 'The session spent 40% of its tokens re-reading files.' }] })
  expect(bridgeCalls(ran)[1]).toEqual(['llm-apply', ...SESSION_ARGV, '--feature', 'analyze', '--replies', repliesPath, '--question', "why's it slow?"])
  expect(out.text).toBe(['analyze: 1 call · ~7k input tokens · model sonnet', 'The session spent 40% of its tokens re-reading files.', ONE_CALL_USAGE].join('\n'))
  // No question: TrajViz's first pass, nothing passed; `--session` is read off the raw text too.
  await obs($, 'analyze')
  expect(bridgeCalls(ran)[2]).toEqual(['llm-prompt', ...SESSION_ARGV, '--feature', 'analyze'])
})

test('analyzeArgs keeps apostrophes and lifts --session out of the question', () => {
  expect(analyzeArgs("analyze why's it slow?")).toEqual({ question: "why's it slow?", session: undefined })
  expect(analyzeArgs('analyze')).toEqual({ question: undefined, session: undefined })
  expect(analyzeArgs('analyze   ')).toEqual({ question: undefined, session: undefined })
  expect(analyzeArgs("analyze --session abc what's next")).toEqual({ question: "what's next", session: 'abc' })
  expect(analyzeArgs('analyze is it done? --session=abc')).toEqual({ question: 'is it done?', session: 'abc' })
  expect(analyzeArgs('analyze --session "/tmp/my run.jsonl" why is it slow?')).toEqual({ question: 'why is it slow?', session: '/tmp/my run.jsonl' })
  expect(analyzeArgs("analyze why's it slow? --session='/tmp/my run.jsonl'")).toEqual({ question: "why's it slow?", session: '/tmp/my run.jsonl' })
})

test('concurrent model plans never read each other\'s replies during apply', async ($, on) => {
  let release: () => void = () => {}
  const gate = new Promise<void>(resolve => { release = resolve })
  let firstApply: () => void = () => {}
  const applying = new Promise<void>(resolve => { firstApply = resolve })
  let calls = 0
  const paths: string[] = []
  host(on, { bridge: { 'llm-prompt': json(planOf('analyze', 1)) }, replies: ['first reply', 'second reply'], apply: async (argv, files) => {
    const path = argv[argv.indexOf('--replies') + 1] as string
    paths.push(path)
    if (++calls === 1) { firstApply(); await gate }
    const reply = JSON.parse(files[path] as string).replies[0].text
    return json({ reply, history_path: null })
  } })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: false })
  const first = obs($, 'analyze')
  await applying
  const second = await obs($, 'analyze')
  release()
  const completedFirst = await first
  expect(paths).toHaveLength(2)
  expect(paths[0]).not.toBe(paths[1])
  expect(completedFirst.text).toContain('first reply')
  expect(completedFirst.text).not.toContain('second reply')
  expect(second.text).toContain('second reply')
})

test('the cost plan is visible before a completion or confirmation', async ($, on) => {
  const { timeline } = host(on, { bridge: { 'llm-prompt': json(planOf('fixes', 5)), 'llm-apply': json({ issues: [], errors: [], text: 'Fixes' }) } })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  await obs($, 'fixes')
  expect(timeline.slice(0, 4)).toEqual(['bridge:llm-prompt', 'plan:fixes: 5 calls · ~35k input tokens · model sonnet', 'ask', 'completion'])
})

test('a plan over 4 calls is asked in an interactive session; Cancel and a dismissed ask run nothing', async ($, on) => {
  const { ran, completions, asked } = host(on, { bridge: { 'llm-prompt': json(planOf('fixes', 6)) }, answer: 'Cancel' })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const out = await obs($, 'fixes --limit 6')
  expect(bridgeCalls(ran)).toEqual([['llm-prompt', ...SESSION_ARGV, '--feature', 'fixes', '--limit', '6']])
  expect(asked).toEqual(['Run 6 model calls?'])
  expect(completions).toEqual([])
  expect(out.text).toBe('fixes: 6 calls · ~42k input tokens · model sonnet\nCancelled: no model call was made.')
})

test('a dismissed ask is no', async ($, on) => {
  const { ran, completions, asked } = host(on, { bridge: { 'llm-prompt': json(planOf('fixes', 5)) }, answer: 'dismiss' })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const out = await obs($, 'fixes')
  expect(asked).toEqual(['Run 5 model calls?'])
  expect(completions).toEqual([])
  expect(bridgeCalls(ran)).toHaveLength(1)
  expect(out.text).toContain('Cancelled')
})

test('Run proceeds; up to 4 calls are never asked', async ($, on) => {
  const fixes = { issues: [{ title: 'Re-reads', judgment: { where: 'CLAUDE.md', fix: 'x', also: '', confidence: 'high' } }], errors: [], text: 'Fixes for the top 1 issue:\n1. Re-reads (confidence high)' }
  const { completions, asked } = host(on, { bridge: { 'llm-prompt': [json(planOf('fixes', 6)), json(planOf('fixes', 4))], 'llm-apply': json(fixes) } })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const out = await obs($, 'fixes')
  expect(asked).toEqual(['Run 6 model calls?'])
  expect(completions).toHaveLength(6)
  expect(out.text).toBe(['fixes: 6 calls · ~42k input tokens · model sonnet', fixes.text, 'usage: 6,000 input · 1,200 cache read · 1,800 cache write · 300 output tokens'].join('\n'))
  await obs($, 'fixes')
  expect(asked).toHaveLength(1)
  expect(completions).toHaveLength(10)
})

test('a headless session runs 6 calls without asking', async ($, on) => {
  const fixes = { issues: [], errors: [], text: 'Fixes for the top 6 issues:' }
  const { ran, completions, asked, files } = host(on, { bridge: { 'llm-prompt': json(planOf('fixes', 6)), 'llm-apply': json(fixes) } })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: false })
  const out = await obs($, 'fixes')
  expect(asked).toEqual([])
  expect(completions.map(c => c.prompt)).toEqual(['P0', 'P1', 'P2', 'P3', 'P4', 'P5'])
  const repliesPath = replyFile(files, 'fixes')
  expect(JSON.parse(files[repliesPath] as string).replies).toHaveLength(6)
  expect(bridgeCalls(ran)[1]).toEqual(['llm-apply', ...SESSION_ARGV, '--feature', 'fixes', '--replies', repliesPath])
  expect(out.text).toContain(fixes.text)
})

test('a failed request is forwarded to llm-apply as a reply error', async ($, on) => {
  const { files, completions } = host(on, {
    bridge: { 'llm-prompt': json(planOf('fixes', 4)), 'llm-apply': json({ issues: [], errors: ['api-error 529'], text: 'Fixes…' }) },
    replies: ['{"where":"a"}', { reason: 'api-error', status: 529 }, { reason: 'empty-reply' }, { reason: 'api-error', status: null }],
  })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const out = await obs($, 'fixes --limit 4')
  expect(completions).toHaveLength(4)
  expect(JSON.parse(files[replyFile(files, 'fixes')] as string)).toEqual({
    hash: 'h1',
    replies: [
      { index: 0, text: '{"where":"a"}' },
      { index: 1, error: 'api-error 529' },
      { index: 2, error: 'empty-reply' },
      { index: 3, error: 'api-error' },
    ],
  })
  // A failed call costs nothing: one answered call's usage.
  expect(out.text).toContain(ONE_CALL_USAGE)
})

test("analysisModel 'session' uses the session's model", { options: { analysisModel: 'session' } }, async ($, on) => {
  const { completions } = host(on, {
    bridge: { 'llm-prompt': json(planOf('analyze', 1)), 'llm-apply': json({ reply: 'r', history_path: null }) },
    model: 'claude-sonnet-4-5-20250929',
  })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const out = await obs($, 'analyze')
  expect(completions.map(c => c.model)).toEqual(['claude-sonnet-4-5-20250929'])
  expect(out.text!.split('\n')[0]).toBe('analyze: 1 call · ~7k input tokens · model claude-sonnet-4-5-20250929')
})

test('label takes its flags, labels with labelModel (or --model), and names the sidecar', async ($, on) => {
  const labelled = { path: '/tmp/l.json', summary: { classification_rate: 0.9 }, text: 'Labels: 3 steps\nphases: understand 2, build 1', errors: [] }
  const { ran, completions, asked, files } = host(on, { bridge: { 'llm-prompt': json(planOf('label', 3)), 'llm-apply': json(labelled) } })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  const out = await obs($, 'label -o /tmp/l.json --assistant-only --max-steps 3')
  expect(bridgeCalls(ran)[0]).toEqual(['llm-prompt', ...SESSION_ARGV, '--feature', 'label', '--max-steps', '3', '--assistant-only', '--out', '/tmp/l.json'])
  expect(completions.map(c => c.model)).toEqual(['haiku', 'haiku', 'haiku'])
  expect(completions[0]).toEqual({ model: 'haiku', system: 'SYS', prompt: 'P0', maxTokens: 1024, effort: undefined, timeoutMs: 120_000 })
  expect(asked).toEqual([])
  expect(bridgeCalls(ran)[1]).toEqual([
    'llm-apply', ...SESSION_ARGV, '--feature', 'label', '--replies', replyFile(files, 'label'),
    '--max-steps', '3', '--assistant-only', '--out', '/tmp/l.json', '--model', 'haiku',
  ])
  expect(out.text).toBe(['label: 3 calls · ~21k input tokens · model haiku', labelled.text, 'Sidecar written to /tmp/l.json', 'usage: 3,000 input · 600 cache read · 900 cache write · 150 output tokens'].join('\n'))
  // `--model M` is the completion model and the sidecar's.
  await obs($, 'label --model opus --max-steps 3')
  expect(completions.slice(3).map(c => c.model)).toEqual(['opus', 'opus', 'opus'])
  expect(bridgeCalls(ran)[3]?.slice(-2)).toEqual(['--model', 'opus'])
})

test('a headless label is capped at labelMaxSteps, an interactive one asks', { options: { labelMaxSteps: 2 } }, async ($, on) => {
  const labelled = { path: `${ARTIFACTS}/labels.json`, summary: {}, text: 'Labels', errors: [] }
  const { ran, completions, asked } = host(on, { bridge: { 'llm-prompt': [json(planOf('label', 2)), json(planOf('label', 50))], 'llm-apply': json(labelled) } })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: false })
  await obs($, 'label')
  // The bridge plans only the cap, so it expects no more replies than were made.
  expect(bridgeCalls(ran)[0]).toEqual(['llm-prompt', ...SESSION_ARGV, '--feature', 'label', '--max-steps', '2'])
  expect(completions).toHaveLength(2)
  expect(asked).toEqual([])
  // No `-o`: the sidecar's path is the bridge's default (<artifacts>/labels.json), nothing passed.
  expect(bridgeCalls(ran)[1]?.slice(-4)).toEqual(['--max-steps', '2', '--model', 'haiku'])
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  await obs($, 'label')
  // Interactive: the bridge's default of 50, asked.
  expect(bridgeCalls(ran)[2]).toEqual(['llm-prompt', ...SESSION_ARGV, '--feature', 'label'])
  expect(asked).toEqual(['Run 50 model calls?'])
  expect(completions).toHaveLength(52)
})

test('custom label options reach capture and replay and use the requested reply limit', async ($, on) => {
  const plan = planOf('label', 1)
  plan.prompts![0]!.max_tokens = 256
  const { ran, files, completions } = host(on, { bridge: { 'llm-prompt': json(plan),
    'llm-apply': json({ path: '/tmp/labels.json', summary: {}, text: 'Labels', errors: [] }) } })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: false })
  await obs($, 'label --max-steps 1 --taxonomy "/tmp/my taxonomy.md" --user-phase request --user-action question --max-content-chars 400 --max-tokens 256 --delay 0')
  const flags = ['--max-steps', '1', '--taxonomy', '/tmp/my taxonomy.md', '--user-phase', 'request', '--user-action', 'question', '--max-content-chars', '400', '--max-tokens', '256', '--delay', '0']
  expect(bridgeCalls(ran)[0]).toEqual(['llm-prompt', ...SESSION_ARGV, '--feature', 'label', ...flags])
  expect(bridgeCalls(ran)[1]).toEqual(['llm-apply', ...SESSION_ARGV, '--feature', 'label', '--replies', replyFile(files, 'label'), ...flags, '--model', 'haiku'])
  expect(completions[0]?.maxTokens).toBe(256)
})

test('env label options and sampling settings reach both planning and the actual provider command', { options: { llm: 'env' } }, async ($, on) => {
  const { ran, spawned, completions } = host(on, { bridge: { 'llm-prompt': json(planOf('label', 1)),
    label: json({ path: '/tmp/labels.json', summary: {}, text: 'Labels', errors: [] }) } })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: false })
  await obs($, 'label --max-steps 1 --taxonomy "/tmp/my taxonomy.md" --user-phase request --user-action question --max-content-chars 400 --max-tokens 256 --delay 0.25 --temperature 0.37')
  const flags = ['--max-steps', '1', '--taxonomy', '/tmp/my taxonomy.md', '--user-phase', 'request', '--user-action', 'question', '--max-content-chars', '400', '--max-tokens', '256', '--delay', '0.25', '--temperature', '0.37']
  expect(bridgeCalls(ran)[0]).toEqual(['llm-prompt', ...SESSION_ARGV, '--feature', 'label', ...flags])
  expect(bridgeCalls(spawned)).toEqual([['label', ...SESSION_ARGV, ...flags]])
  expect(completions).toEqual([])
})

test('invalid label options stop before setup, planning or model calls', async ($, on) => {
  const { ran, completions } = host(on)
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: false })
  for (const flag of ['--taxonomy', '--user-phase=""', '--user-action', '--max-content-chars 0', '--max-tokens 64001', '--delay Infinity', '--temperature NaN']) {
    expect((await obs($, `label ${flag}`)).text).toMatch(/needs|must be/)
  }
  expect((await obs($, 'label --temperature 0.3')).text).toContain('--temperature requires llm=env')
  for (const delay of ['0.25', '3', '3600']) expect((await obs($, `label --delay ${delay}`)).text).toBe(SESSION_DELAY_TEXT)
  expect((await obs($, 'label --delay 3601')).text).toBe('--delay must be from 0 to 3600 seconds.')
  expect(bridgeCalls(ran)).toEqual([])
  expect(completions).toEqual([])
})

test('a paced session plan is refused before confirmation or model calls', { options: { labelMaxSteps: 2 } }, async ($, on) => {
  const plan = planOf('label', 3)
  for (const prompt of plan.prompts!) prompt.delay_s = 0.25
  const h = host(on, { bridge: { 'llm-prompt': json(plan),
    'llm-apply': json({ path: '/tmp/labels.json', summary: {}, text: 'Labels', errors: [] }) } })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: false })
  expect((await obs($, 'label --max-steps 2')).text).toContain(SESSION_DELAY_TEXT)
  expect(h.completions).toEqual([])
  expect(h.asked).toEqual([])
  expect(bridgeCalls(h.ran).map(call => call[0])).toEqual(['llm-prompt'])
})

test('a plan too large to inline is read from its file', async ($, on) => {
  const { completions, files } = host(on, { bridge: { 'llm-prompt': json(planOf('analyze', 1, { prompts: undefined })), 'llm-apply': json({ reply: 'r', history_path: null }) } })
  files[`${ARTIFACTS}/prompts-analyze.json`] = JSON.stringify(planOf('analyze', 1))
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  await obs($, 'analyze')
  expect(completions.map(c => c.prompt)).toEqual(['P0'])
})

test('a changed prompts file is refused before sending model calls', async ($, on) => {
  const { completions, files } = host(on, { bridge: { 'llm-prompt': json(planOf('analyze', 1, { prompts: undefined })) } })
  files[`${ARTIFACTS}/prompts-analyze.json`] = JSON.stringify(planOf('analyze', 1, { hash: 'different-session' }))
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  expect((await obs($, 'analyze')).text).toContain('prompt plan changed')
  expect(completions).toEqual([])
})

test('env mode plans first, then runs ask, fixes and label through the bridge and no completion', { options: { llm: 'env' } }, async ($, on) => {
  const { ran, spawned, completions, asked } = host(on, {
    bridge: {
      'llm-prompt': [json(planOf('analyze', 1)), json(planOf('fixes', 3)), json(planOf('label', 10)), json(planOf('label', 10))],
      ask: json({ reply: 'Answer.', history_path: `${ARTIFACTS}/analyze.json` }),
      fixes: json({ issues: [], errors: [], text: 'Fixes…' }),
      label: json({ path: `${ARTIFACTS}/labels.json`, summary: {}, text: 'Labels', errors: [] }),
    },
    answer: 'Cancel',
  })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  expect((await obs($, "analyze what's up")).text).toBe('analyze: 1 call · ~7k input tokens · model ANALYZE_MODEL (env)\nAnswer.')
  expect(bridgeCalls(ran).slice(0, 2)).toEqual([
    ['llm-prompt', ...SESSION_ARGV, '--feature', 'analyze', '--question', "what's up"],
    ['ask', ...SESSION_ARGV, '--question', "what's up"],
  ])
  expect((await obs($, 'fixes --limit 3')).text).toBe('fixes: 3 calls · ~21k input tokens · model ANALYZE_MODEL (env)\nFixes…')
  expect(bridgeCalls(ran)[3]).toEqual(['fixes', ...SESSION_ARGV, '--limit', '3'])
  // Over 4 calls: asked as in session mode; Cancel runs nothing.
  expect((await obs($, 'label --model gpt-x --max-steps 10')).text).toBe('label: 10 calls · ~70k input tokens · model gpt-x (env)\nCancelled: no model call was made.')
  expect(asked).toEqual(['Run 10 model calls?'])
  expect(bridgeCalls(ran)).toHaveLength(5)
  expect(spawned).toEqual([])
  // Headless: no ask; the direct `label` (one request per step, no deadline) is spawned, not run.
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: false })
  expect((await obs($, 'label --model gpt-x --max-steps 10')).text).toBe(`label: 10 calls · ~70k input tokens · model gpt-x (env)\nLabels\nSidecar written to ${ARTIFACTS}/labels.json`)
  expect(bridgeCalls(spawned)).toEqual([['label', ...SESSION_ARGV, '--max-steps', '10', '--model', 'gpt-x']])
  expect(bridgeCalls(ran)).toHaveLength(6)
  expect(bridgeCalls(ran).map(a => a[0])).not.toContain('label')
  expect(asked).toHaveLength(1)
  expect(completions).toEqual([])
})

test('usage rows, the empty session and a bridge error', async ($, on) => {
  const { ran } = host(on, {
    bridge: {
      'llm-prompt': [json({ empty: true, reason: 'no_steps' }), { exitCode: 1, stdout: '{"error": {"code": "stale_prompts", "message": "These replies were made for other prompts."}}' }],
    },
  })
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  expect((await obs($, 'fixes --limit 9')).text).toBe(LIMIT_TEXT)
  expect((await obs($, 'fixes --limit')).text).toBe(LIMIT_TEXT)
  expect((await obs($, 'label --max-steps 0')).text).toBe(MAX_STEPS_TEXT)
  expect((await obs($, 'label --model')).text).toBe('--model needs a model name.')
  expect((await obs($, 'label --out')).text).toBe('-o/--out needs an output path.')
  expect((await obs($, 'analyze --session')).text).toBe('--session needs a session id, prefix, or transcript path.')
  expect((await obs($, 'analyze --session=""')).text).toBe('--session needs a session id, prefix, or transcript path.')
  expect(bridgeCalls(ran)).toEqual([])
  expect((await obs($, 'fixes')).text).toBe(EMPTY_TEXT)
  expect((await obs($, 'fixes')).text).toBe('These replies were made for other prompts.')
})

test('planLine and usageLine', () => {
  expect(planLine(planOf('analyze', 1), 'sonnet')).toBe('analyze: 1 call · ~7k input tokens · model sonnet')
  expect(planLine(planOf('label', 312, { plan: { calls: 312, approx_input_tokens: 1_234_567 } }), 'haiku')).toBe('label: 312 calls · ~1.2M input tokens · model haiku')
  expect(planLine(planOf('fixes', 0), 'sonnet')).toBe('fixes: 0 calls · ~0 input tokens · model sonnet')
  const usage: Usage = { input: 7_123, cache_read: 0, cache_write: 6_800, output: 412 }
  expect(usageLine(usage)).toBe('usage: 7,123 input · 0 cache read · 6,800 cache write · 412 output tokens')
})
