import { expect, test } from 'claude-code/testing'
import { parseArgs, suggest, tokenize } from './lib/args'

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
  expect(parseArgs('report --dark -o out.html').flags).toEqual({ dark: true, o: 'out.html' })
  expect(parseArgs('json --view steps --view tools').flags).toEqual({ view: 'tools' })
  expect(parseArgs('view --json steps')).toEqual({ sub: 'view', positional: ['steps'], flags: { json: true }, passthrough: [] })
  expect(parseArgs('scorecard --no-self def --fuzzy-commands ghi').positional).toEqual(['def', 'ghi'])
  expect(parseArgs('label --assistant-only --max-steps 2').flags).toEqual({ 'assistant-only': true, 'max-steps': '2' })
  expect(tokenize(`analyze "why so slow?" it's`)).toEqual(['analyze', 'why so slow?', 'its'])
  expect(tokenize(`a 'b c' d\\ e`)).toEqual(['a', 'b c', 'd e'])
  expect(suggest('isues')).toBe('issues')
  expect(suggest('zzzzzz')).toBeUndefined()
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
  expect((await $.command.run({ command: 'obs', args: 'report --offlne' } as never)).text).toBe("Unknown option '--offlne' for /obs report. /obs help lists the supported options.")
  expect((await $.command.run({ command: 'obs', args: 'report --dark=false' } as never)).text).toBe('--dark does not take a value.')
})

test('extra arguments and unsupported session selectors fail before any external work', async ($, on) => {
  on('command.register', () => ({ value: { command: 'obs' } }))
  on('tool.register', () => ({ value: { tool: 'mcp__trajviz-obs__obs' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  await $.session.start({ cwd: '/p', surface: 'terminal', isInteractive: true })
  for (const args of ['overview extra', 'help extra', 'doctor extra', 'setup extra', 'report extra', 'json extra', 'bundle extra', 'fixes extra', 'label extra', 'attribution extra', 'issues extra', 'health extra', 'patterns extra', 'agents extra', 'context extra', 'timeline extra', 'pane unknown', 'pane close extra', 'schemas first second', 'view steps junk', 'report -- ignored']) {
    const result = await $.command.run({ command: 'obs', args } as never)
    expect(result.text).toContain('Unexpected arguments')
  }
  for (const sub of ['help', 'doctor', 'setup', 'schemas', 'converge', 'labels', 'sessions', 'watch', 'statusline']) {
    expect((await $.command.run({ command: 'obs', args: `${sub} --session other` } as never)).text).toBe(`--session is not supported by /obs ${sub}.`)
  }
})
