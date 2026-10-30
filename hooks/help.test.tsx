import { expect, test } from 'claude-code/testing'
import { analyzeSelection, argvValue, parseArgs, quoteOption, selectionError, suggest, tokenize, valueArgv } from './lib/args'

test('parseArgs splits sub, flags, k=v and passthrough', async () => {
  expect(parseArgs('view tool_usage agent=main --limit 10 --json -- --x')).toEqual({
    sub: 'view',
    positional: ['tool_usage', 'agent=main'],
    flags: { limit: '10', json: true },
    passthrough: ['--x'],
  })
  expect(parseArgs('').sub).toBe('overview')
  expect(parseArgs('--session abc').sub).toBe('overview')
  expect(parseArgs('--session abc').flags).toEqual({ session: 'abc' })
  expect(parseArgs('json --with-trajectory -o out').flags).toEqual({ 'with-trajectory': true, o: 'out' })
  expect(parseArgs('json --view steps --view tools').flags).toEqual({ view: 'tools' })
  expect(parseArgs('view --json steps')).toEqual({ sub: 'view', positional: ['steps'], flags: { json: true }, passthrough: [] })
  expect(parseArgs('scorecard --no-self def --fuzzy-commands ghi').positional).toEqual(['def', 'ghi'])
  expect(parseArgs('label --assistant-only --max-steps 2').flags).toEqual({ 'assistant-only': true, 'max-steps': '2' })
  expect(tokenize(`analyze "why so slow?" it's`)).toEqual(['analyze', 'why so slow?', 'its'])
  expect(tokenize(`a 'b c' d\\ e`)).toEqual(['a', 'b c', 'd e'])
  expect(suggest('isues')).toBe('issues')
  expect(suggest('zzzzzz')).toBeUndefined()
})

test('analysis selection preserves apostrophes, quoted questions and literal flag text after --', () => {
  expect(analyzeSelection(`analyze what's slow --session "/capture folder/manifest.json" --session-id "user's A"`)).toEqual({
    question: "what's slow", session: '/capture folder/manifest.json', captureSessionId: "user's A",
  })
  expect(analyzeSelection('analyze --session capture --session-id A -- Why did --session-id fail?')).toEqual({
    question: 'Why did --session-id fail?', session: 'capture', captureSessionId: 'A',
  })
  const literal = 'analyze -- Why did --session-id and --model fail?'
  expect(analyzeSelection(literal)).toEqual({ question: 'Why did --session-id and --model fail?', session: undefined })
  expect(selectionError(parseArgs(literal).flags, literal)).toBeUndefined()
  expect(analyzeSelection('analyze "Why did --session-id fail?" --session capture')).toEqual({
    question: '"Why did --session-id fail?"', session: 'capture',
  })
})

test('action arguments roundtrip exact captured IDs including real whitespace and quotes', () => {
  for (const id of ['A\nB', 'A\tB', 'A"B', 'A\\B', "user's A", '用户 A', '-A', '--A\nB']) {
    expect(parseArgs(`dashboard --session source ${quoteOption('session-id', id)}`).flags['session-id']).toBe(id)
    expect(argvValue(valueArgv('session-id', id), 'session-id')).toBe(id)
    expect(analyzeSelection(`analyze What's missing --session source ${quoteOption('session-id', id)}`).captureSessionId).toBe(id)
  }
  expect(valueArgv('session-id', '-A')).toEqual(['--session-id=-A'])
})

test('/obs help answers the grouped table', async ($, on) => {
  on('command.register', () => ({ value: { command: 'obs' } }))
  on('tool.register', () => ({ value: { tool: 'mcp__trajviz-obs__obs' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  await $.session.start({ cwd: '/p', surface: 'terminal', isInteractive: true })
  const out = await $.command.run({
    command: 'obs',
    args: 'help',
    origin: { kind: 'composer' },
    presentation: { isFullscreen: false, columns: 80 },
  } as never)
  expect(out.text).toContain('Look')
  for (const group of ['Export', 'Browser', 'Compare', 'LLM', 'Setup']) expect(out.text).toContain(group)
  expect(out.text!.split('\n').length).toBeLessThan(30)
  expect((await $.command.run({ command: 'obs', args: 'isues' } as never)).text).toContain("Did you mean 'issues'")
  expect((await $.command.run({ command: 'obs', args: 'isues' } as never)).text).toBe(
    "Unknown subcommand 'isues'. Did you mean 'issues'? /obs help lists everything.",
  )
  expect((await $.command.run({ command: 'obs', args: 'qqqq' } as never)).text).toBe(
    "Unknown subcommand 'qqqq'. /obs help lists everything.",
  )
  expect((await $.command.run({ command: 'obs', args: 'dashboard --offlne' } as never)).text).toBe("Unknown option '--offlne' for /obs dashboard. /obs help lists the supported options.")
  expect((await $.command.run({ command: 'obs', args: 'json --with-trajectory=false' } as never)).text).toBe('--with-trajectory does not take a value.')
  expect(out.text).not.toContain('report [')
  expect((await $.command.run({ command: 'obs', args: 'report' } as never)).text).toBe('HTML reports have been removed. Run /obs dashboard for the full interactive view.')
})

test('extra arguments and unsupported session selectors fail before any external work', async ($, on) => {
  on('command.register', () => ({ value: { command: 'obs' } }))
  on('tool.register', () => ({ value: { tool: 'mcp__trajviz-obs__obs' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  await $.session.start({ cwd: '/p', surface: 'terminal', isInteractive: true })
  for (const args of ['overview extra', 'help extra', 'doctor extra', 'setup extra', 'json extra', 'bundle extra', 'fixes extra', 'label extra', 'attribution extra', 'issues extra', 'health extra', 'patterns extra', 'agents extra', 'context extra', 'timeline extra', 'pane unknown', 'pane close extra', 'schemas first second', 'view steps junk', 'json -- ignored']) {
    const result = await $.command.run({ command: 'obs', args } as never)
    expect(result.text).toContain('Unexpected arguments')
  }
  expect((await $.command.run({ command: 'obs', args: 'overview --session-id A' } as never)).text).toBe('--session-id requires --session <capture path>.')
  expect((await $.command.run({ command: 'obs', args: 'overview --session x --session-id' } as never)).text).toBe('--session-id needs an exact session id inside the capture.')
  for (const sub of ['help', 'doctor', 'setup', 'schemas', 'converge', 'labels', 'watch', 'statusline']) {
    expect((await $.command.run({ command: 'obs', args: `${sub} --session other` } as never)).text).toBe(`--session is not supported by /obs ${sub}.`)
  }
})
