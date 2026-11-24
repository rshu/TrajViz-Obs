import { expect, test } from 'bun:test'
import { mkdtemp, readFile, rm, symlink, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { Usage, usageFeature, PLUGIN_VERSION } from '../src/telemetry.js'

const python = Bun.which('python3') ?? Bun.which('python')
const TOKEN = 'A'.repeat(43)
const ROOT_BRIDGE = path.resolve(import.meta.dir, '../../bridge')

test('Pi telemetry version matches both installation manifests', async () => {
  for (const file of ['../package.json', '../../package.json']) {
    const manifest = JSON.parse(await readFile(path.resolve(import.meta.dir, file), 'utf8'))
    expect(PLUGIN_VERSION).toBe(manifest.version)
  }
})
async function environment(values: Record<string, string | undefined>, run: () => Promise<void>) {
  const previous = Object.keys(values).map(key => [key, process.env[key]] as const)
  try {
    for (const [key, value] of Object.entries(values)) if (value === undefined) delete process.env[key]; else process.env[key] = value
    await run()
  } finally {
    for (const [key, value] of previous) if (value === undefined) delete process.env[key]; else process.env[key] = value
  }
}
async function captured(file: string, count: number): Promise<{ argv: string[]; env: Record<string, string>; isolated: number }[]> {
  for (let attempt = 0; attempt < 150; attempt++) {
    const lines = (await readFile(file, 'utf8').catch(() => '')).split('\n').slice(0, -1)
    if (lines.length >= count) return lines.map(line => JSON.parse(line))
    await Bun.sleep(20)
  }
  return []
}

test('Pi feature telemetry only names allowlisted commands and variants', () => {
  for (const [args, expected] of [
    ['', 'overview'], ['pane close', 'pane.close'], ['pane', 'pane'], ['analyze why private code failed', 'analyze'],
    ['dashboard --html "/private/report.html"', 'dashboard.html'], ['dashboard stop', 'dashboard.stop'],
    ['converge stop', 'converge.stop'], ['brief', 'brief'], ['telemetry', undefined], ['enable_telemetry', undefined],
    ['update', undefined], ['update --check', undefined],
    ['disable_telemetry', undefined], ['telemetry_destination https://private.example --use-launch-token', undefined],
    ['dashboard port 8001', undefined], ['private-secret-unknown-command', undefined],
  ] as const) expect(usageFeature(args)).toBe(expected)
})

test('Pi defaults on without an endpoint and preserves saved and malformed opt-outs', async () => {
  const state = await mkdtemp(path.join(tmpdir(), 'trajviz-pi-default-'))
  const marker = path.join(state, 'pi-consent')
  try {
    await environment({ TRAJVIZ_OBS_TELEMETRY: undefined, TRAJVIZ_OBS_TELEMETRY_URL: undefined,
      TRAJVIZ_OBS_TELEMETRY_STATE_DIR: state }, async () => {
      const runtime = { bridge: state, telemetryPython: () => undefined }
      const usage = new Usage(runtime, '1.5.2')
      for (const [value, expected] of [[undefined, true], ['1', true], ['0', false], ['', false], ['true', false], [' 1', false]] as const) {
        if (value === undefined) delete process.env.TRAJVIZ_OBS_TELEMETRY
        else process.env.TRAJVIZ_OBS_TELEMETRY = value
        expect(usage.enabled()).toBe(expected)
        expect(usage.dashboardArgs().length > 0).toBe(expected)
      }
      await expect(readFile(marker, 'utf8')).rejects.toThrow('ENOENT')
      await writeFile(marker, 'off\n')
      delete process.env.TRAJVIZ_OBS_TELEMETRY
      expect(usage.enabled()).toBe(false)
      process.env.TRAJVIZ_OBS_TELEMETRY = '1'
      expect(new Usage(runtime, '1.5.2').enabled()).toBe(false)
      await writeFile(marker, 'on\n')
      process.env.TRAJVIZ_OBS_TELEMETRY = '0'
      expect(usage.enabled()).toBe(true)
      process.env.TRAJVIZ_OBS_TELEMETRY = '1'
      for (const invalid of ['', 'yes', 'on\nextra']) {
        await writeFile(marker, invalid)
        expect(usage.enabled()).toBe(false)
      }
      await rm(marker)
      const target = path.join(state, 'linked-consent')
      await writeFile(target, 'on\n')
      await symlink(target, marker)
      expect(usage.enabled()).toBe(false)
    })
  } finally { await rm(state, { recursive: true, force: true }) }
})

test.skipIf(!python)('fresh Pi consent and destination changes work during a session and stay adapter scoped', async () => {
  const state = await mkdtemp(path.join(tmpdir(), 'trajviz-pi-consent-'))
  try {
    await environment({ TRAJVIZ_OBS_TELEMETRY: undefined, TRAJVIZ_OBS_TELEMETRY_URL: undefined,
      TRAJVIZ_OBS_TELEMETRY_STATE_DIR: state, TRAJVIZ_OBS_TELEMETRY_TOKEN: TOKEN }, async () => {
      const usage = new Usage({ bridge: ROOT_BRIDGE, telemetryPython: () => python ?? undefined }, '1.4.0')
      const signal = new AbortController().signal
      await writeFile(path.join(state, 'claude-code-consent'), 'on\n')
      expect(usage.enabled()).toBe(true)
      expect(await usage.control('status', signal)).toContain('Usage telemetry · On')
      expect(await usage.control('destination', signal)).toContain('http://127.0.0.1:8765/v1/events')
      await expect(readFile(path.join(state, 'pi-consent'), 'utf8')).rejects.toThrow('ENOENT')
      expect(await usage.control('on', signal)).toContain('On. Preference saved')
      expect(usage.enabled()).toBe(true)
      expect(await readFile(path.join(state, 'pi-consent'), 'utf8')).toBe('on\n')
      expect(usage.dashboardArgs()).toEqual(['--usage-adapter', 'pi', '--usage-version', PLUGIN_VERSION, '--usage-agent-version', '1.4.0'])
      expect(await usage.control('status', signal)).toContain('Collector credential · Not configured')
      expect(await usage.control('destination', signal, 'https://collector.example')).toContain('https://collector.example/v1/events')
      expect(usage.enabled()).toBe(true)
      expect(await usage.control('destination', signal, 'https://collector.example', true)).toContain('Configured')
      const stored = await readFile(path.join(state, 'pi-destination.json'), 'utf8')
      expect(stored).not.toContain(TOKEN)
      const rejected = await usage.control('destination', signal, 'http://public.example/v1/events')
      expect(rejected).not.toContain(TOKEN)
      expect(rejected).not.toContain('public.example')
      expect(await readFile(path.join(state, 'pi-destination.json'), 'utf8')).toBe(stored)
      expect(usage.enabled()).toBe(true)
      expect(await usage.control('off', signal)).toContain('Off. Preference saved')
      expect(usage.enabled()).toBe(false)
      process.env.TRAJVIZ_OBS_TELEMETRY = '1'
      expect(new Usage({ bridge: ROOT_BRIDGE, telemetryPython: () => python ?? undefined }, '1.4.0').enabled()).toBe(false)
      expect(usage.dashboardArgs()).toEqual([])
      expect(await readFile(path.join(state, 'claude-code-consent'), 'utf8')).toBe('on\n')
      expect(await usage.control('destination', signal, 'default')).toContain('http://127.0.0.1:8765/v1/events')
      expect(usage.enabled()).toBe(false)
    })
  } finally { await rm(state, { recursive: true, force: true }) }
})

test.skipIf(!python)('worker arguments omit prompts, paths and provider secrets, and preserve only measured fields', async () => {
  const directory = await mkdtemp(path.join(tmpdir(), 'trajviz-pi-events-'))
  const capture = path.join(directory, 'events.jsonl')
  try {
    await writeFile(path.join(directory, 'telemetry_client.py'), [
      'import json, os, sys',
      `with open(${JSON.stringify(capture)}, 'a') as target:`,
      '    target.write(json.dumps({"argv": sys.argv[1:], "env": dict(os.environ), "isolated": sys.flags.isolated}) + "\\n")',
    ].join('\n'))
    await environment({ TRAJVIZ_OBS_TELEMETRY: '0', TRAJVIZ_OBS_TELEMETRY_URL: undefined,
      TRAJVIZ_OBS_TELEMETRY_STATE_DIR: directory, TRAJVIZ_OBS_TELEMETRY_TOKEN: TOKEN,
      OPENAI_API_KEY: 'private-provider-key', ANTHROPIC_API_KEY: 'private-other-key', PRIVATE_SESSION_PATH: '/private/secret.jsonl',
      SSL_CERT_FILE: '/private/collector-ca.pem' }, async () => {
      let interpreter: string | undefined
      const usage = new Usage({ bridge: directory, telemetryPython: () => interpreter }, '1.4.0')
      usage.report({ feature: 'analyze', surface: 'terminal', outcome: 'success', durationMs: 12.7 })
      await usage.flush(); expect(await captured(capture, 0)).toEqual([])
      delete process.env.TRAJVIZ_OBS_TELEMETRY
      expect(usage.enabled()).toBe(true)
      expect(usage.dashboardArgs()).toContain('--usage-adapter')
      usage.report({ feature: 'analyze', surface: 'terminal', outcome: 'cancelled', durationMs: 12.7 })
      await new Promise<void>(resolve => setImmediate(resolve))
      interpreter = python ?? undefined
      usage.report({ feature: 'pane.tab', surface: 'terminal', outcome: 'success', durationMs: 0 })
      usage.report({ feature: 'label', surface: 'headless', outcome: 'error', durationMs: 1500 })
      await usage.flush()
      const events = await captured(capture, 3)
      expect(events).toHaveLength(3)
      for (const event of events) {
        expect(event.isolated).toBe(1)
        expect(event.argv.slice(0, 2)).toEqual(['--adapter', 'pi'])
        expect(event.argv.slice(-4)).toEqual(['--version', PLUGIN_VERSION, '--agent-version', '1.4.0'])
        expect(event.env.TRAJVIZ_OBS_TELEMETRY_TOKEN).toBe(TOKEN)
        expect(event.env.TRAJVIZ_OBS_TELEMETRY).toBeUndefined()
        expect(event.env.TRAJVIZ_OBS_TELEMETRY_URL).toBeUndefined()
        expect(event.env.SSL_CERT_FILE).toBe('/private/collector-ca.pem')
        for (const name of ['OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'PRIVATE_SESSION_PATH']) expect(event.env[name]).toBeUndefined()
      }
      expect(events.find(event => event.argv.includes('analyze'))!.argv).toContain('cancelled')
      expect(events.find(event => event.argv.includes('analyze'))!.argv).toContain('13')
      expect(events.find(event => event.argv.includes('label'))!.argv).toContain('headless')
    })
  } finally { await rm(directory, { recursive: true, force: true }) }
})

test.skipIf(!python)('turning telemetry off drops unsent Node events and host versions cannot inject private strings', async () => {
  const state = await mkdtemp(path.join(tmpdir(), 'trajviz-pi-pause-'))
  try {
    await environment({ TRAJVIZ_OBS_TELEMETRY: undefined, TRAJVIZ_OBS_TELEMETRY_URL: undefined,
      TRAJVIZ_OBS_TELEMETRY_STATE_DIR: state, TRAJVIZ_OBS_TELEMETRY_TOKEN: undefined }, async () => {
      let interpreter: string | undefined
      const usage = new Usage({ bridge: ROOT_BRIDGE, telemetryPython: () => interpreter }, '1.4.0-private-user-build')
      expect(usage.dashboardArgs()).toEqual(['--usage-adapter', 'pi', '--usage-version', PLUGIN_VERSION])
      usage.report({ feature: 'overview', surface: 'terminal', outcome: 'success', durationMs: 0 })
      await new Promise<void>(resolve => setImmediate(resolve))
      interpreter = python ?? undefined
      expect(await usage.control('off', new AbortController().signal)).toContain('Off. Preference saved')
      await usage.flush()
      expect(await usage.control('status', new AbortController().signal)).toContain('Queued events · 0 / 512')
      expect(usage.enabled()).toBe(false)
      expect(usage.dashboardArgs()).toEqual([])
    })
  } finally { await rm(state, { recursive: true, force: true }) }
})

test.skipIf(!python)('an unavailable interpreter bounds queued gestures and later delivers the most recent ones', async () => {
  const directory = await mkdtemp(path.join(tmpdir(), 'trajviz-pi-bounded-'))
  const capture = path.join(directory, 'events.jsonl')
  try {
    await writeFile(path.join(directory, 'telemetry_client.py'), [
      'import json, os, sys',
      `with open(${JSON.stringify(capture)}, 'a') as target:`,
      '    target.write(json.dumps({"argv": sys.argv[1:], "env": {}, "isolated": sys.flags.isolated}) + "\\n")',
    ].join('\n'))
    await environment({ TRAJVIZ_OBS_TELEMETRY: '1', TRAJVIZ_OBS_TELEMETRY_URL: 'http://127.0.0.1:1/v1/events',
      TRAJVIZ_OBS_TELEMETRY_STATE_DIR: directory }, async () => {
      let interpreter: string | undefined
      const usage = new Usage({ bridge: directory, telemetryPython: () => interpreter }, '1.4.0')
      for (let index = 0; index < 40; index++) usage.report({ feature: 'help', surface: 'terminal', outcome: 'success', durationMs: index })
      await new Promise<void>(resolve => setImmediate(resolve))
      interpreter = python ?? undefined
      await usage.flush()
      const events = await captured(capture, 32)
      expect(events).toHaveLength(32)
      const durations = events.map(({ argv }) => Number(argv[argv.indexOf('--duration-ms') + 1])).sort((a, b) => a - b)
      expect(durations).toEqual(Array.from({ length: 32 }, (_, index) => index + 8))
    })
  } finally { await rm(directory, { recursive: true, force: true }) }
})
