import { expect, test } from 'bun:test'
import { parseArgs } from '../../hooks/lib/args.js'
import { isSessionlessCommand, validateArguments } from '../../shared/command-args.js'
import { usageFeature, USAGE_FEATURES } from '../src/usage-features.js'

test('update accepts only its check switch and works before a session exists', () => {
  for (const input of ['update', 'update --check']) {
    expect(validateArguments(input)).toBeUndefined()
    expect(isSessionlessCommand(parseArgs(input))).toBe(true)
  }
  expect(parseArgs('update --check extra')).toMatchObject({ flags: { check: true }, positional: ['extra'] })
  for (const input of [
    'update extra', 'update --check extra', 'update --check=false', 'update --check=true',
    'update --yes', 'update --force', 'update --session current',
    'update --session source --session-id selected', 'update -- arbitrary-command',
  ]) expect(validateArguments(input)).toBeDefined()
  expect(validateArguments('view health --check')).toContain('Unknown option')
})

test('update administration stays out of usage events and collector feature contracts', () => {
  expect(usageFeature('update')).toBeUndefined()
  expect(usageFeature('update --check')).toBeUndefined()
  expect(USAGE_FEATURES.includes('update' as never)).toBe(false)
})
