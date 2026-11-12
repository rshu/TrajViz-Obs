import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'
import { SNAPSHOT } from './testdata/snapshot'

const CACHE = '/cache/trajviz-obs'
const PYTHON = `${CACHE}/envs/0123456789abcdef01234567/0123456789abcdef0123456789abcdef/bin/python`
const CUSTOM_PYTHON = '/opt/custom-python/bin/python'
const ENDPOINT = 'http://127.0.0.1:8765/v1/events'
const TOKEN = 'A'.repeat(43)
const CONSENT_DIRECTORY = '/usage-state'
const CONSENT_PATH = `${CONSENT_DIRECTORY}/claude-code-consent`
const DESTINATION_PATH = `${CONSENT_DIRECTORY}/claude-code-destination.json`
const PRIVATE_VALUE = 'PRIVATE-SESSION-PATH-OR-CONTENT'
const TOOL = 'mcp__trajviz-obs__obs'
const STATE = JSON.stringify({ revision: 6, python: PYTHON, trajviz: '0.5.1', extras: ['llm', 'charts', 'zstd'], source: 'agent-trajviz==0.5.1', createdAt: 1 })
const DONE = { value: { exitCode: 0, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }

type Settings = {
  consent?: string; endpoint?: string; token?: string; settingsEnv?: Record<string, string>;
  status?: unknown; withoutRuntime?: boolean; stateDirectory?: string; savedConsent?: 'on' | 'off';
  waitForSnapshot?: Promise<void>; savedDestination?: string; destinationError?: string; destinationCredentialInvalid?: boolean;
}

function host(on: On, settings: Settings, sender?: Promise<void>) {
  const calls: { argv: string[]; env?: Record<string, string> }[] = []
  const envReads: string[] = []
  const fileReads: string[] = []
  let snapshots = 0
  let settingsRead = false
  let notify = () => {}
  on('env.get', (_, e) => {
    envReads.push(e.name)
    notify()
    return { value: { TRAJVIZ_OBS_TELEMETRY: settings.consent, TRAJVIZ_OBS_TELEMETRY_URL: settings.endpoint,
      TRAJVIZ_OBS_TELEMETRY_TOKEN: settings.token, TRAJVIZ_OBS_TELEMETRY_STATE_DIR: settings.stateDirectory,
      XDG_CACHE_HOME: '/cache' }[e.name] }
  })
  on('command.register', () => ({ value: { command: 'obs' } }))
  mock.store(on)
  on('settings.read', () => {
    settingsRead = true
    notify()
    return { value: settings.settingsEnv ? { env: settings.settingsEnv } : {} }
  })
  on('tool.register', () => ({ value: { tool: TOOL } }))
  on('tool.call', { tool: 'AskUserQuestion' }, () => ({ deny: 'dismissed' }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('classic.SessionStart', () => ({}))
  on('session.id', () => ({ value: 'test-session' }))
  on('session.cwd', () => ({ value: '/workspace' }))
  on('session.usage', () => ({ value: { startedAt: 0, context: { window: 100_000, percent: 0 }, rateLimits: [] } }))
  on('ui.toast', () => ({ value: undefined }))
  on('ui.log', () => ({ value: undefined }))
  on('ui.close', () => ({ value: undefined }))
  on('fs.stat', (_, e) => ({ value: { kind: 'file', size: e.path === CONSENT_PATH ? (settings.savedConsent?.length ?? 0) + 1 : 1_000, mtimeMs: 10, isLink: false } }))
  on('fs.exists', (_, e) => ({ value: e.path === PYTHON || e.path === CONSENT_PATH && settings.savedConsent !== undefined
    || e.path === DESTINATION_PATH && settings.savedDestination !== undefined }))
  on('fs.read', (_, e) => {
    fileReads.push(e.path)
    if (e.path.endsWith('/.claude-plugin/plugin.json')) return { value: '{"version":"1.2.3"}' }
    if (e.path === `${CACHE}/state.json` && !settings.withoutRuntime) return { value: STATE }
    if (e.path === CONSENT_PATH && settings.savedConsent) return { value: settings.savedConsent + '\n' }
    return { deny: 'not found' }
  })
  on('process.run', async (_, e) => {
    if (e.argv[0] === CUSTOM_PYTHON && e.argv[1] === '--version') {
      return { value: { ...DONE.value, stdout: 'Python 3.11.9' } }
    }
    if (e.argv[3] === 'snapshot') {
      snapshots++
      await settings.waitForSnapshot
      return { value: { ...DONE.value, stdout: JSON.stringify(SNAPSHOT) } }
    }
    if (!e.argv[2]?.endsWith('/bridge/telemetry_client.py')) return { deny: 'unexpected process' }
    calls.push({ argv: [...e.argv], env: e.init?.env })
    notify()
    if (e.argv[3] === '--set-consent') {
      settings.savedConsent = e.argv[4] === 'on' ? 'on' : 'off'
      return { value: { ...DONE.value, stdout: JSON.stringify({ configuration: settings.savedConsent === 'on' ? 'ready' : 'disabled' }) } }
    }
    if (e.argv[3] === '--status') {
      const status = settings.status ?? { configuration: settings.savedConsent === 'on' ? 'ready' : 'disabled' }
      return { value: { ...DONE.value, stdout: JSON.stringify(status) } }
    }
    if (e.argv[3] === '--destination' || e.argv[3] === '--set-destination') {
      if (e.argv[3] === '--set-destination' && e.argv[4] && !settings.destinationError) {
        const target = e.argv[4].replace(/\/$/, '')
        settings.savedDestination = target === 'default' ? undefined : target.endsWith('/v1/events') ? target : `${target}/v1/events`
      }
      const status = { destination: settings.savedDestination ?? settings.endpoint ?? ENDPOINT,
        destination_source: settings.savedDestination ? 'saved' : settings.endpoint ? 'launch' : 'default',
        destination_error: settings.destinationError, credential_invalid: settings.destinationCredentialInvalid,
        credential_configured: e.argv.includes('--use-launch-token') || Boolean(settings.token && settings.destinationCredentialInvalid) }
      return { value: { ...DONE.value, stdout: JSON.stringify(status) } }
    }
    await sender
    return DONE
  })
  async function call(n: number) {
    while (calls.length < n) await new Promise<void>(resolve => { notify = resolve })
    return calls[n - 1]!
  }
  async function envRead(name: string) {
    while (!envReads.includes(name)) await new Promise<void>(resolve => { notify = resolve })
  }
  async function readSettings() {
    while (!settingsRead) await new Promise<void>(resolve => { notify = resolve })
  }
  return { calls, envReads, fileReads, call, envRead, readSettings, snapshots: () => snapshots }
}

const command = (args: string) => ({
  command: 'obs', args, origin: { kind: 'composer' as const }, presentation: { columns: 80, isFullscreen: false },
})
const end = { reason: 'prompt_input_exit' as const, sessionId: 'test-session', resume: { id: 'test-session' } }

const pane = {
  plugin: 'trajviz-obs', surface: 'terminal', component: 'Pane', requestId: 'obs',
  props: { title: 'obs', isFocused: true, bodyColumns: 80, placement: 'dock', scroll: { offset: 0, bodyRows: 50 }, view: {} },
} as const

test('launch consent requires the exact opt-in and an endpoint before any runtime work', async ($, on) => {
  const settings = { consent: 'true', endpoint: ENDPOINT }
  const world = host(on, settings)
  await $.session.start({ cwd: '/workspace', surface: 'terminal', isInteractive: true })
  const help = await $.command.run(command('help'))
  expect(help.text).toContain('Start here:')
  await world.envRead('TRAJVIZ_OBS_TELEMETRY')
  expect(world.envReads).toContain('TRAJVIZ_OBS_TELEMETRY')
  expect(world.envReads).not.toContain('TRAJVIZ_OBS_TELEMETRY_URL')

  settings.consent = '1'
  settings.endpoint = ''
  await $.command.run(command('help'))
  await world.envRead('TRAJVIZ_OBS_TELEMETRY_URL')
  expect(world.fileReads).toEqual([])
  expect(world.calls).toEqual([])
})

test('Claude records fixed feature names and outcomes without waiting for the sender', async ($, on) => {
  let release: () => void = () => {}
  const sender = new Promise<void>(resolve => { release = resolve })
  const world = host(on, { consent: '1', endpoint: ENDPOINT, token: TOKEN }, sender)
  try {
    await $.session.start({ cwd: '/workspace', surface: 'desktop', isInteractive: true })
    const start = await world.call(1)
    expect(start.argv[0]).toBe(PYTHON)
    expect(start.argv.slice(3)).toEqual([
      '--adapter', 'claude-code', '--surface', 'desktop', '--feature', 'activation',
      '--outcome', 'success', '--duration-ms', '0', '--version', '1.2.3',
    ])
    expect(start.env).toEqual({ TRAJVIZ_OBS_TELEMETRY: '1', TRAJVIZ_OBS_TELEMETRY_URL: ENDPOINT, TRAJVIZ_OBS_TELEMETRY_TOKEN: TOKEN })
    expect(JSON.stringify(start.argv)).not.toContain(TOKEN)

    const help = await $.command.run(command('help'))
    expect(help.text).toContain('Start here:')
    expect((await world.call(2)).argv.slice(7, 11)).toEqual(['--feature', 'help', '--outcome', 'success'])

    const rejected = await $.command.run(command(`dashboard --html ${PRIVATE_VALUE} --port 7000`))
    expect(rejected.text).toContain('cannot be combined')
    expect((await world.call(3)).argv.slice(7, 11)).toEqual(['--feature', 'dashboard.html', '--outcome', 'rejected'])

    const declined = await $.command.run(command('dashboard'))
    expect(declined.text).toContain('extra is not installed')
    expect((await world.call(4)).argv.slice(7, 11)).toEqual(['--feature', 'dashboard', '--outcome', 'cancelled'])

    const unknown = await $.command.run(command(PRIVATE_VALUE))
    expect(unknown.text).toContain('Unknown subcommand')
    const result = await $.tool.call({ tool: TOOL, what: PRIVATE_VALUE } as never)
    expect(result).toEqual({ result: 'what must be one of brief, issues, health, view.' })
    expect((await world.call(5)).argv.slice(7, 11)).toEqual(['--feature', 'tool', '--outcome', 'rejected'])
    expect(world.calls).toHaveLength(5)
    expect(JSON.stringify(world.calls)).not.toContain(PRIVATE_VALUE)
  } finally {
    release()
  }
})

test('Claude records cold-start usage and reads status with the configured interpreter', { options: { python: CUSTOM_PYTHON } }, async ($, on) => {
  const world = host(on, { consent: '1', endpoint: ENDPOINT, withoutRuntime: true, status: { configuration: 'ready' } })
  await $.session.start({ cwd: '/workspace', surface: 'terminal', isInteractive: true })
  expect((await world.call(1)).argv[0]).toBe(CUSTOM_PYTHON)
  expect((await $.command.run(command('help'))).text).toContain('Start here:')
  expect((await world.call(2)).argv[0]).toBe(CUSTOM_PYTHON)
  expect((await $.command.run(command('telemetry'))).text).toContain('Usage telemetry · On')
  expect(world.calls[2]?.argv).toEqual([CUSTOM_PYTHON, '-I', world.calls[0]?.argv[2], '--status', '--adapter', 'claude-code'])
})

test('native Claude consent commands persist, take effect immediately, and are never reported as events', async ($, on) => {
  const settings: Settings = { stateDirectory: CONSENT_DIRECTORY }
  const world = host(on, settings)
  const start = { cwd: '/workspace', surface: 'terminal' as const, isInteractive: true }
  await $.session.start(start)
  expect((await $.command.run(command('enable_telemetry'))).text).toContain('Usage telemetry · On. Preference saved.')
  expect(settings.savedConsent).toBe('on')
  expect(world.calls.map(call => call.argv[3])).toEqual(['--set-consent'])
  await $.command.run(command('help'))
  const action = await world.call(2)
  expect(action.argv.slice(7, 11)).toEqual(['--feature', 'help', '--outcome', 'success'])
  expect(action.env?.TRAJVIZ_OBS_TELEMETRY).toBe('0')
  expect((await $.command.run(command('telemetry'))).text).toContain('Usage telemetry · On')
  await $.session.start(start)
  expect((await world.call(4)).argv.slice(7, 11)).toEqual(['--feature', 'activation', '--outcome', 'success'])

  settings.consent = '1'
  settings.endpoint = ENDPOINT
  expect((await $.command.run(command('disable_telemetry'))).text).toContain('Usage telemetry · Off. Preference saved.')
  expect(settings.savedConsent).toBe('off')
  await $.command.run(command('help'))
  expect((await $.command.run(command('telemetry'))).text).toContain('Usage telemetry · Off')
  expect(world.calls.map(call => call.argv[3])).toEqual(['--set-consent', '--adapter', '--status', '--adapter', '--set-consent', '--status'])
})

test('a slow Claude command requires telemetry consent at both invocation and completion', async ($, on) => {
  const clock = mock.clock(on, { now: 1_700_000_000_000 })
  const settings: Settings = { stateDirectory: CONSENT_DIRECTORY, savedConsent: 'off' }
  const world = host(on, settings)
  await $.session.start({ cwd: '/workspace', surface: 'terminal', isInteractive: true })
  await $.classic.SessionStart({ source: 'startup', transcript_path: '/tmp/session.jsonl', session_id: 'test-session' })

  for (const [index, preference] of ['enable_telemetry', 'disable_telemetry'].entries()) {
    let finishSnapshot = () => {}
    settings.waitForSnapshot = new Promise<void>(resolve => { finishSnapshot = resolve })
    const running = $.command.run(command('overview'))
    try {
      await clock.settle()
      expect(world.snapshots()).toBe(index + 1)
      expect((await $.command.run(command(preference))).text).toContain('Preference saved.')
    } finally {
      finishSnapshot()
    }
    expect((await running).text).toContain('obs snapshot')
    await clock.settle()
    expect(world.calls.map(call => call.argv[3])).toEqual(Array(index + 1).fill('--set-consent'))
  }

  await $.command.run(command('enable_telemetry'))
  expect((await $.command.run(command('overview'))).text).toContain('obs snapshot')
  expect((await world.call(4)).argv.slice(7, 11)).toEqual(['--feature', 'overview', '--outcome', 'success'])
})

test('Claude telemetry consent requires a user command and trusted configuration', async ($, on) => {
  const settings: Settings = { stateDirectory: CONSENT_DIRECTORY }
  const world = host(on, settings)
  const sdk = { ...command('enable_telemetry'), origin: { kind: 'sdk' as const } }
  const plugin = { ...command('disable_telemetry'), origin: { kind: 'plugin' as const, name: 'other-plugin' } }
  expect((await $.command.run(sdk)).text).toContain('directly in the Claude Code composer')
  expect((await $.command.run(plugin)).text).toContain('directly in the Claude Code composer')
  expect((await $.command.run(command('enable_telemetry unexpected'))).text).toContain('Unexpected arguments')
  expect((await $.command.run(command('disable_telemetry --session saved'))).text).toContain('--session is not supported')
  settings.settingsEnv = { XDG_STATE_HOME: '/project/private' }
  expect((await $.command.run(command('enable_telemetry'))).text).toContain('could not be changed')
  expect(settings.savedConsent).toBeUndefined()
  expect(world.calls).toEqual([])
})

test('Claude can change the destination during a session without sending telemetry or embedding a token in the command', async ($, on) => {
  const settings: Settings = { stateDirectory: CONSENT_DIRECTORY, token: TOKEN }
  const world = host(on, settings)
  const collector = 'https://collector.example.test:8443'
  const endpoint = `${collector}/v1/events`
  await $.session.start({ cwd: '/workspace', surface: 'terminal', isInteractive: true })
  expect((await $.command.run(command('telemetry_destination'))).text).toContain(`${ENDPOINT} (local default)`)
  expect((await $.command.run(command(`telemetry_destination ${collector} --use-launch-token`))).text).toContain(`${endpoint} (saved)`)
  const write = world.calls[1]!
  expect(write.argv.slice(3)).toEqual(['--set-destination', collector, '--use-launch-token', '--adapter', 'claude-code'])
  expect(write.env?.TRAJVIZ_OBS_TELEMETRY_TOKEN).toBe(TOKEN)
  expect(JSON.stringify(write.argv)).not.toContain(TOKEN)
  expect(settings.savedConsent).toBeUndefined()
  expect((await $.command.run(command('telemetry'))).text).toContain('Usage telemetry · Off')

  const sdk = { ...command('telemetry_destination default'), origin: { kind: 'sdk' as const } }
  expect((await $.command.run(sdk)).text).toContain('directly in the Claude Code composer')
  expect((await $.command.run(command('telemetry_destination default --use-launch-token'))).text).toContain('Usage: /obs telemetry_destination')
  expect((await $.command.run(command('telemetry_destination --use-launch-token'))).text).toContain('Usage: /obs telemetry_destination')
  expect((await $.command.run(command('telemetry destination'))).text).toContain('Unexpected arguments')
  settings.destinationError = 'invalid_endpoint'
  const rejected = await $.command.run(command(`telemetry_destination https://${PRIVATE_VALUE}@collector.example.test`))
  expect(rejected.text).toContain('Use an HTTPS collector URL')
  expect(rejected.text).not.toContain(PRIVATE_VALUE)
  expect(settings.savedDestination).toBe(endpoint)
  delete settings.destinationError
  expect((await $.command.run(command('telemetry_destination https://127.0.0.1/'))).text).toContain('https://127.0.0.1/v1/events (saved)')
  expect((await $.command.run(command('telemetry_destination default'))).text).toContain('Destination reset. Usage telemetry consent is unchanged.')
  expect(world.calls.map(call => call.argv[3])).toEqual(['--destination', '--set-destination', '--status', '--set-destination', '--set-destination', '--set-destination'])
})

test('a saved destination supports launch opt-in without a launch URL, and settings controls are never reported', async ($, on) => {
  const settings: Settings = { stateDirectory: CONSENT_DIRECTORY, consent: '1', savedDestination: ENDPOINT }
  const world = host(on, settings)
  await $.session.start({ cwd: '/workspace', surface: 'terminal', isInteractive: true })
  const activation = await world.call(1)
  expect(activation.argv.slice(7, 11)).toEqual(['--feature', 'activation', '--outcome', 'success'])
  expect(activation.env?.TRAJVIZ_OBS_TELEMETRY_URL).toBe('')
  await $.command.run(command('dashboard port 8000'))
  await $.command.run(command('dashboard port'))
  await $.command.run(command('telemetry_destination https://collector.example.test'))
  await $.command.run(command('telemetry_destination'))
  await $.command.run(command('help'))
  expect((await world.call(4)).argv.slice(7, 11)).toEqual(['--feature', 'help', '--outcome', 'success'])
  expect(world.calls.map(call => call.argv[3])).toEqual(['--adapter', '--set-destination', '--destination', '--adapter'])
})

test('the destination command shows an invalid credential binding while telemetry stays off', async ($, on) => {
  host(on, { stateDirectory: CONSENT_DIRECTORY, savedConsent: 'off', savedDestination: ENDPOINT, token: TOKEN,
    destinationCredentialInvalid: true })
  const destination = await $.command.run(command('telemetry_destination'))
  expect(destination.text).toContain('Collector credential · Invalid')
  expect(destination.text).not.toContain(TOKEN)
  expect((await $.command.run(command('telemetry'))).text).toContain('Usage telemetry · Off')
})

test('Claude settings cannot supply the telemetry endpoint', async ($, on) => {
  const world = host(on, { consent: '1', endpoint: ENDPOINT, settingsEnv: { TRAJVIZ_OBS_TELEMETRY_URL: ENDPOINT } })
  await $.session.start({ cwd: '/workspace', surface: 'terminal', isInteractive: true })
  const help = await $.command.run(command('help'))
  expect(help.text).toContain('Start here:')
  await world.readSettings()
  expect(world.fileReads).toEqual([])
  expect(world.calls).toEqual([])
})

test('Claude settings cannot inject a credential or expose it in local status', async ($, on) => {
  const world = host(on, { consent: '1', endpoint: ENDPOINT, token: TOKEN, settingsEnv: { trajviz_obs_telemetry_token: TOKEN } })
  await $.session.start({ cwd: '/workspace', surface: 'terminal', isInteractive: true })
  const status = await $.command.run(command('telemetry'))
  expect(status.text).toContain('Usage telemetry · Off')
  expect(status.text).not.toContain(TOKEN)
  expect(world.envReads).not.toContain('TRAJVIZ_OBS_TELEMETRY_TOKEN')
  expect(world.calls).toEqual([])
})

test('local telemetry status uses a read-only helper and displays only allowed fields', async ($, on) => {
  const world = host(on, { consent: '1', endpoint: ENDPOINT, token: TOKEN, status: {
    configuration: 'ready', credential_configured: true, queue_state: 'present', queued: 3, queue_capacity: 512,
    last_success_at: '2026-11-11T08:15:02Z', last_failure: 'throttled', last_http_status: 429,
    last_failure_at: PRIVATE_VALUE, retry_at: '2026-11-11T08:16:00+00:00', dropped: 1, expired: 0, rejected: 2,
    unknown: PRIVATE_VALUE,
  } })
  await $.session.start({ cwd: '/workspace', surface: 'terminal', isInteractive: true })
  await world.call(1)
  const result = await $.command.run(command('telemetry'))
  expect(result.text).toContain('Usage telemetry · On')
  expect(result.text).toContain('Collector credential · Configured')
  expect(result.text).toContain('Queued events · 3 / 512')
  expect(result.text).toContain('Last successful upload · 2026-11-11T08:15:02Z')
  expect(result.text).toContain('Collector throttled delivery (HTTP 429)')
  expect(result.text).toContain('Retry eligible from · 2026-11-11T08:16:00Z')
  expect(result.text).toContain('Dropped from full queue · 1')
  expect(result.text).toContain('Expired · 0')
  expect(result.text).toContain('Rejected · 2')
  for (const privateValue of [TOKEN, PRIVATE_VALUE, ENDPOINT]) expect(result.text).not.toContain(privateValue)
  expect(world.calls).toHaveLength(2)
  expect(world.calls[1]?.argv.slice(3)).toEqual(['--status', '--adapter', 'claude-code'])
})

test('graceful exit gives pending telemetry a chance to finish without delaying a command', async ($, on) => {
  const clock = mock.clock(on)
  let release = () => {}
  const sender = new Promise<void>(resolve => { release = resolve })
  const world = host(on, { consent: '1', endpoint: ENDPOINT }, sender)
  on('session.end', (_, e) => ({ sessionId: e.sessionId }))
  try {
    await $.session.start({ cwd: '/workspace', surface: 'terminal', isInteractive: true })
    expect((await $.command.run(command('help'))).text).toContain('Start here:')
    const ending = $.session.end(end)
    let finished = false
    void ending.then(() => { finished = true })
    await world.call(2)
    await clock.settle()
    expect(finished).toBe(false)
    release()
    expect(await ending).toEqual({ sessionId: 'test-session' })
  } finally {
    release()
  }
})

test('graceful exit is bounded when telemetry cannot finish', async ($, on) => {
  const clock = mock.clock(on)
  let release = () => {}
  const sender = new Promise<void>(resolve => { release = resolve })
  const world = host(on, { consent: '1', endpoint: ENDPOINT }, sender)
  on('session.end', (_, e) => ({ sessionId: e.sessionId }))
  try {
    await $.session.start({ cwd: '/workspace', surface: 'terminal', isInteractive: true })
    await world.call(1)
    const ending = $.session.end(end)
    await clock.settle()
    await clock.advance(750)
    expect(await ending).toEqual({ sessionId: 'test-session' })
  } finally {
    release()
  }
})

test('native pane controls record explicit actions using fixed features', async ($, on) => {
  const clock = mock.clock(on, { now: 1_700_000_000_000 })
  const world = host(on, { consent: '1', endpoint: ENDPOINT })
  on('ui.panes', () => ({ value: [{ id: 'obs', title: 'obs', isShown: true, isFocused: true, isPlaced: true }] }))
  on('turn.complete', (_, e) => ({ text: e.answer }))
  await $.session.start({ cwd: '/workspace', surface: 'terminal', isInteractive: true })
  await world.call(1)
  await $.classic.SessionStart({ source: 'startup', transcript_path: `/tmp/${PRIVATE_VALUE}.jsonl`, session_id: 'test-session' })
  const ui = await $.ui.mount(pane)
  try {
    await ui.press({ key: 'tab:3' })
    expect((await world.call(2)).argv.slice(5, 11)).toEqual(['--surface', 'terminal', '--feature', 'pane.tab', '--outcome', 'success'])
    await ui.press({ key: 'refresh' })
    expect((await world.call(3)).argv.slice(7, 11)).toEqual(['--feature', 'pane.refresh', '--outcome', 'success'])
    expect(world.snapshots()).toBe(1)

    await $.turn.complete({ answer: 'done', durationMs: 100, isAborted: false, turnId: 'test-turn', reason: 'answer' })
    await clock.settle()
    expect(world.snapshots()).toBe(2)
    expect(world.calls).toHaveLength(3)

    await ui.press({ key: 'watch' })
    expect((await world.call(4)).argv.slice(7, 11)).toEqual(['--feature', 'watch', '--outcome', 'success'])
    expect(JSON.stringify(world.calls)).not.toContain(PRIVATE_VALUE)
  } finally {
    await ui.unmount()
  }
})

test('slash and fallback pane closes each report once', async ($, on) => {
  const clock = mock.clock(on, { now: 1_700_000_000_000 })
  const world = host(on, { consent: '1', endpoint: ENDPOINT })
  on('state.get', { plugin: 'trajviz-obs', key: 'latest' }, () => ({ deny: 'synthetic render failure' }))
  await $.session.start({ cwd: '/workspace', surface: 'terminal', isInteractive: true })
  await world.call(1)

  expect((await $.command.run(command('pane close'))).text).toBe('obs pane closed.')
  expect((await world.call(2)).argv.slice(7, 11)).toEqual(['--feature', 'pane.close', '--outcome', 'success'])

  const ui = await $.ui.mount(pane)
  try {
    await ui.press({ key: 'close' })
    expect((await world.call(3)).argv.slice(7, 11)).toEqual(['--feature', 'pane.close', '--outcome', 'success'])
    await clock.settle()
    expect(world.calls).toHaveLength(3)
  } finally {
    await ui.unmount()
  }
})
