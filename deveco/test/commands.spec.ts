import { expect, test } from 'bun:test'
import { quoteOption } from '../../hooks/lib/args.js'
import type { Backend } from '../src/backend.js'
import { runCommand } from '../src/commands.js'

function fixture() {
  const calls: Parameters<Backend['inspect']>[] = []
  const backend = {
    inspect: async (...args: Parameters<Backend['inspect']>) => {
      calls.push(args)
      return { text: 'Prompt evidence' }
    },
  } as unknown as Backend
  return { calls, backend }
}

test('prompt request selection forwards one literal view option with pagination and cancellation', async () => {
  const { backend, calls } = fixture()
  const signal = new AbortController().signal
  expect(await runCommand(backend, 'ses_current', 'view prompt_construction --request-id req-123 --limit 10 --offset 2 --json', signal)).toBe('Prompt evidence')
  expect(calls).toEqual([['ses_current', 'view', [
    'prompt_construction', '--option', 'request_id=req-123', '--limit', '10', '--offset', '2', '--json',
  ], signal]])
})

test.each([
  'request with spaces',
  'quotes "double" and \'single\' with \\backslash',
  'literal; $(no-shell) `no-shell` & | > < = value',
  '-leading-dash',
  'line one\nline two',
])('opaque request ID is preserved as one structured argument: %s', async (id) => {
  const { backend, calls } = fixture()
  await runCommand(backend, 'ses_current', `view prompt_construction ${quoteOption('request-id', id)}`)
  expect(calls[0]![2]).toEqual([
    'prompt_construction', '--option', `request_id=${id}`, '--limit', '25', '--offset', '0', '--json',
  ])
})

test('the prompt view keeps its default selection when no request ID is supplied', async () => {
  const { backend, calls } = fixture()
  await runCommand(backend, 'ses_current', 'view prompt_construction')
  expect(calls[0]![2]).toEqual(['prompt_construction', '--limit', '25', '--offset', '0', '--json'])
})

test('request IDs allow 4096 characters and reject larger values before inspecting a session', async () => {
  const { backend, calls } = fixture()
  await runCommand(backend, 'ses_current', `view prompt_construction --request-id ${'x'.repeat(4096)}`)
  expect(calls[0]![2]![2]).toBe(`request_id=${'x'.repeat(4096)}`)
  await expect(runCommand(backend, 'ses_current', `view prompt_construction --request-id ${'x'.repeat(4097)}`)).rejects.toThrow('up to 4096 characters')
  expect(calls).toHaveLength(1)
})

test.each([
  'view prompt_construction --request-id',
  'view prompt_construction --request-id=""',
  'view prompt_construction --request-id="   "',
])('missing or blank request selectors fail before inspecting: %s', async (input) => {
  const { backend, calls } = fixture()
  await expect(runCommand(backend, 'ses_current', input)).rejects.toThrow('--request-id needs an exact request ID')
  expect(calls).toEqual([])
})

test('NUL is rejected before interpreting any request selector', async () => {
  const { backend, calls } = fixture()
  await expect(runCommand(backend, 'ses_current', 'view prompt_construction --request-id="a\0b"')).rejects.toThrow('contains NUL')
  expect(calls).toEqual([])
})

test.each([
  'view --request-id req',
  'view issues --request-id req',
  'issues --request-id req',
  'overview --request-id req',
  'json --request-id req',
  'dashboard --request-id req',
  'doctor --request-id req',
])('request selection is restricted to the prompt view: %s', async (input) => {
  const { backend, calls } = fixture()
  await expect(runCommand(backend, 'ses_current', input)).rejects.toThrow()
  expect(calls).toEqual([])
})
