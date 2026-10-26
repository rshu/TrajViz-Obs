import { expect, test } from 'claude-code/testing'
import { commandFailure } from './commands/index'

test('host timeout explains the failed command instead of reporting a missing handler', () => {
  const result = commandFailure('report --offline', { kind: 'timeout', budget: 1000 })
  expect(result.exitCode).toBe(1)
  expect(result.text).toContain('obs report: Claude Code stopped the command handler')
  expect(result.text).toContain('hook time budget expired')
  expect(result.text).toContain('/reload-plugins')
  expect(result.text).not.toContain('no command.run hook answered')
})

test('host failure preserves a bounded cause without echoing command arguments', () => {
  const result = commandFailure('analyze private question', { kind: 'throw', message: 'worker disconnected', budget: 1000 })
  expect(result.text).toContain('obs analyze: The command handler failed: worker disconnected')
  expect(result.text).not.toContain('private question')
  const long = commandFailure('dashboard', { kind: 'throw', message: 'x'.repeat(3000), budget: 1000 })
  expect(long.text!.length).toBeLessThan(2200)
})

test('host failure without details names the default overview command', () => {
  expect(commandFailure('', { kind: 'throw', budget: 1000 }).text).toContain('obs overview: The command handler failed in Claude Code.')
  expect(commandFailure('--session private-id', { kind: 'throw', budget: 1000 }).text).toContain('obs overview:')
})
