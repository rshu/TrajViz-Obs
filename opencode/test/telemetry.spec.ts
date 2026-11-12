import { expect, test } from 'bun:test'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { runCommand } from '../src/commands.js'
import type { Backend } from '../src/backend.js'
import { createUsageReporter, dashboardUsageArgs, usageDeliveryStatus } from '../src/telemetry.js'
import { USAGE_FEATURES, usageFeature } from '../src/usage-features.js'

test('analytics selects only known features and command variants', () => {
  expect(usageFeature('')).toBe('overview')
  expect(usageFeature('dashboard --html "/private/report.html"')).toBe('dashboard.html')
  expect(usageFeature('dashboard stop --session "secret"')).toBe('dashboard.stop')
  expect(usageFeature('converge stop')).toBe('converge.stop')
  expect(usageFeature('pane close')).toBe('pane.close')
  expect(usageFeature('analyze private model prompt')).toBe('analyze')
  expect(usageFeature('telemetry')).toBeUndefined()
  expect(usageFeature('telemetry --bad')).toBeUndefined()
  expect(USAGE_FEATURES).not.toContain('telemetry')
  expect(usageFeature('enable_telemetry')).toBeUndefined()
  expect(usageFeature('disable_telemetry --bad')).toBeUndefined()
  expect(USAGE_FEATURES).not.toContain('enable_telemetry')
  expect(USAGE_FEATURES).not.toContain('disable_telemetry')
  expect(usageFeature('private-secret-unknown-command')).toBeUndefined()
})

const python = Bun.which('python3') ?? Bun.which('python')
const TOKEN = 'A'.repeat(43)

test.skipIf(!python)('the reporter requires launch opt-in and spawns with only the analytics fields and environment', async () => {
  const bridge = await mkdtemp(path.join(tmpdir(), 'trajviz-opencode-telemetry-'))
  const capture = path.join(bridge, 'events.jsonl')
  const endpoint = 'http://127.0.0.1:1/v1/events'
  const caFile = path.join(bridge, 'collector-ca.pem')
  const caDir = path.join(bridge, 'trust')
  const environment = {
    TRAJVIZ_OBS_TELEMETRY: '0',
    TRAJVIZ_OBS_TELEMETRY_URL: endpoint,
    TRAJVIZ_OBS_TELEMETRY_TOKEN: TOKEN,
    TRAJVIZ_OBS_TELEMETRY_STATE_DIR: bridge,
    SSL_CERT_FILE: caFile,
    SSL_CERT_DIR: caDir,
    OPENAI_API_KEY: 'test-marker',
  }
  const entries = Object.entries(environment)
  const previous = entries.map(([name]) => [name, process.env[name]] as const)
  let interpreter: string | undefined
  let probes = 0
  const runtime = { bridge, telemetryPython: () => { probes++; return interpreter } }
  try {
    for (const [name, value] of entries) process.env[name] = value
    const report = createUsageReporter(runtime)
    const activation = { feature: 'activation', surface: 'unknown', outcome: 'success', durationMs: 0 } as const
    expect(report.enabled()).toBe(false)
    report(activation)
    await report.flush()
    expect(dashboardUsageArgs()).toEqual([])
    process.env.TRAJVIZ_OBS_TELEMETRY = '1'
    delete process.env.TRAJVIZ_OBS_TELEMETRY_URL
    expect(report.enabled()).toBe(false)
    report(activation)
    await report.flush()
    expect(dashboardUsageArgs()).toEqual([])
    expect(probes).toBe(0)
    process.env.TRAJVIZ_OBS_TELEMETRY_URL = endpoint
    expect(dashboardUsageArgs()).toEqual(['--usage-adapter', 'opencode', '--usage-version', expect.stringMatching(/^\d+\.\d+\.\d+/)])

    await writeFile(path.join(bridge, 'telemetry_client.py'), [
      'import json, os, sys',
      `with open(${JSON.stringify(capture)}, "a") as output:`,
      '    event = {',
      '        "argv": sys.argv[1:], "isolated": sys.flags.isolated,',
      '        "enabled": os.environ.get("TRAJVIZ_OBS_TELEMETRY"),',
      '        "endpoint": os.environ.get("TRAJVIZ_OBS_TELEMETRY_URL"),',
      '        "credential": os.environ.get("TRAJVIZ_OBS_TELEMETRY_TOKEN"),',
      '        "caFile": os.environ.get("SSL_CERT_FILE"),',
      '        "caDir": os.environ.get("SSL_CERT_DIR"),',
      '        "key": os.environ.get("OPENAI_API_KEY"),',
      '    }',
      '    output.write(json.dumps(event) + "\\n")',
    ].join('\n'))
    report(activation)
    await new Promise<void>(resolve => setImmediate(resolve))
    expect(probes).toBe(1)
    interpreter = python ?? undefined
    report({ feature: 'dashboard.html', surface: 'desktop', outcome: 'success', durationMs: 12.7 })

    let captured: unknown[] = []
    for (let attempt = 0; attempt < 150; attempt++) {
      const contents = await readFile(capture, 'utf8').catch(() => '')
      const lines = contents.split('\n').slice(0, -1)
      if (lines.length === 2) {
        captured = lines.map(line => JSON.parse(line))
        break
      }
      await Bun.sleep(20)
    }
    const common = { isolated: 1, enabled: '1', endpoint, credential: TOKEN, caFile, caDir, key: null }
    const version = expect.stringMatching(/^\d+\.\d+\.\d+/)
    expect(captured).toHaveLength(2)
    expect(captured).toEqual(expect.arrayContaining([
      { ...common, argv: ['--adapter', 'opencode', '--surface', 'unknown', '--feature', 'activation', '--outcome', 'success', '--duration-ms', '0', '--version', version] },
      { ...common, argv: ['--adapter', 'opencode', '--surface', 'desktop', '--feature', 'dashboard.html', '--outcome', 'success', '--duration-ms', '13', '--version', version] },
    ]))
  } finally {
    for (const [name, value] of previous) {
      if (value === undefined) delete process.env[name]
      else process.env[name] = value
    }
    await rm(bridge, { recursive: true, force: true })
  }
})

test.skipIf(!python)('local status uses only the read-only helper and omits credential, URL and opaque fields', async () => {
  const bridge = await mkdtemp(path.join(tmpdir(), 'trajviz-opencode-telemetry-status-'))
  const endpoint = 'https://private-collector.example/v1/events'
  const environment = { TRAJVIZ_OBS_TELEMETRY: '1', TRAJVIZ_OBS_TELEMETRY_URL: endpoint,
    TRAJVIZ_OBS_TELEMETRY_TOKEN: TOKEN, OPENAI_API_KEY: 'must-not-be-inherited' }
  const entries = Object.entries(environment)
  const previous = entries.map(([name]) => [name, process.env[name]] as const)
  const runtime = { bridge, telemetryPython: () => python ?? undefined }
  try {
    for (const [name, value] of entries) process.env[name] = value
    await writeFile(path.join(bridge, 'telemetry_client.py'), [
      'import json, os, sys',
      'assert sys.argv[1:] == ["--status", "--adapter", "opencode"]',
      'assert sys.flags.isolated',
      'assert "OPENAI_API_KEY" not in os.environ',
      'print(json.dumps({"configuration": "ready", "credential_configured": "TRAJVIZ_OBS_TELEMETRY_TOKEN" in os.environ, "queue_state": "present", "queued": 2, "queue_capacity": 512, "last_success_at": "2026-11-11T08:15:02Z", "last_failure": "authentication", "last_http_status": 401, "last_failure_at": "private-path", "token": os.environ.get("TRAJVIZ_OBS_TELEMETRY_TOKEN"), "url": os.environ.get("TRAJVIZ_OBS_TELEMETRY_URL")}))',
    ].join('\n'))
    const result = await usageDeliveryStatus(runtime, new AbortController().signal)
    expect(result).toContain('Usage telemetry · On')
    expect(result).toContain('Collector credential · Configured')
    expect(result).toContain('Queued events · 2 / 512')
    expect(result).toContain('Last successful upload · 2026-11-11T08:15:02Z')
    expect(result).toContain('Collector rejected the credential (HTTP 401)')
    for (const privateValue of [endpoint, TOKEN, 'private-path']) expect(result).not.toContain(privateValue)
  } finally {
    for (const [name, value] of previous) {
      if (value === undefined) delete process.env[name]
      else process.env[name] = value
    }
    await rm(bridge, { recursive: true, force: true })
  }
})

test.skipIf(!python)('the native commands save adapter consent and update a running reporter and browser launcher', async () => {
  const directory = await mkdtemp(path.join(tmpdir(), 'trajviz-opencode-consent-'))
  const marker = path.join(directory, 'opencode-consent')
  const env = process.env
  const names = ['TRAJVIZ_OBS_TELEMETRY', 'TRAJVIZ_OBS_TELEMETRY_URL', 'TRAJVIZ_OBS_TELEMETRY_TOKEN', 'TRAJVIZ_OBS_TELEMETRY_STATE_DIR']
  const previous = names.map(name => [name, env[name]] as const)
  const runtime = { bridge: path.resolve(import.meta.dir, '../runtime/bridge'), telemetryPython: () => python ?? undefined }
  const backend = { runtime } as unknown as Backend
  const signal = new AbortController().signal
  const command = async (args: string) => (await runCommand(backend, { args, sessionID: 'home', runID: args }, signal)).text
  try {
    env.TRAJVIZ_OBS_TELEMETRY_STATE_DIR = directory
    env.TRAJVIZ_OBS_TELEMETRY_TOKEN = TOKEN
    delete env.TRAJVIZ_OBS_TELEMETRY
    delete env.TRAJVIZ_OBS_TELEMETRY_URL
    await writeFile(path.join(directory, 'claude-code-consent'), 'on\n')
    const reporter = createUsageReporter(runtime)
    expect(reporter.enabled()).toBe(false)
    expect(await command('telemetry')).toBe('Usage telemetry · Off')
    expect(await command('enable_telemetry')).toContain('On. Preference saved')
    expect(await readFile(marker, 'utf8')).toBe('on\n')
    expect(reporter.enabled()).toBe(true)
    expect(dashboardUsageArgs()).toEqual(['--usage-adapter', 'opencode', '--usage-version', expect.any(String)])
    expect(await command('telemetry')).toContain('Collector credential · Not configured')
    env.TRAJVIZ_OBS_TELEMETRY = '1'
    env.TRAJVIZ_OBS_TELEMETRY_URL = 'http://127.0.0.1:1/v1/events'
    expect(await command('disable_telemetry')).toContain('Off. Preference saved')
    expect(await readFile(marker, 'utf8')).toBe('off\n')
    expect(reporter.enabled()).toBe(false)
    expect(dashboardUsageArgs()).toEqual([])
    expect(await command('telemetry')).toContain('Usage telemetry · Off')
    env.TRAJVIZ_OBS_TELEMETRY_URL = 'http://private-collector.example/v1/events'
    expect(await command('enable_telemetry')).toContain('Endpoint invalid')
    expect(await readFile(marker, 'utf8')).toBe('off\n')
    expect(await readFile(path.join(directory, 'claude-code-consent'), 'utf8')).toBe('on\n')
  } finally {
    for (const [name, value] of previous) {
      if (value === undefined) delete env[name]
      else env[name] = value
    }
    await rm(directory, { recursive: true, force: true })
  }
})

test.skipIf(!python)('graceful unload launches queued senders while the first uploader is stalled', async () => {
  const bridge = await mkdtemp(path.join(tmpdir(), 'trajviz-opencode-telemetry-stalled-'))
  const consent = process.env.TRAJVIZ_OBS_TELEMETRY
  const endpoint = process.env.TRAJVIZ_OBS_TELEMETRY_URL
  const capture = path.join(bridge, 'events')
  const release = path.join(bridge, 'release')
  const features = ['help', 'view', 'doctor', 'pane', 'pane.close']
  try {
    process.env.TRAJVIZ_OBS_TELEMETRY = '1'
    process.env.TRAJVIZ_OBS_TELEMETRY_URL = 'http://127.0.0.1:1/v1/events'
    await writeFile(path.join(bridge, 'telemetry_client.py'), [
      'import pathlib, sys, time',
      'feature = sys.argv[sys.argv.index("--feature") + 1]',
      `with open(${JSON.stringify(capture)}, "a") as output: output.write(feature + "\\n")`,
      'deadline = time.monotonic() + 5',
      `while feature == "help" and time.monotonic() < deadline and not pathlib.Path(${JSON.stringify(release)}).exists():`,
      '    time.sleep(0.01)',
    ].join('\n'))
    const report = createUsageReporter({ bridge, telemetryPython: () => python ?? undefined })!
    for (const feature of features) report({ feature, surface: 'terminal', outcome: 'success', durationMs: 1 })
    await report.flush()
    let observed: string[] = []
    for (let attempt = 0; attempt < 100; attempt++) {
      observed = (await readFile(capture, 'utf8').catch(() => '')).trim().split('\n')
      if (observed.length === features.length) break
      await Bun.sleep(10)
    }
    expect(observed.sort()).toEqual(features.sort())
  } finally {
    if (consent === undefined) delete process.env.TRAJVIZ_OBS_TELEMETRY
    else process.env.TRAJVIZ_OBS_TELEMETRY = consent
    if (endpoint === undefined) delete process.env.TRAJVIZ_OBS_TELEMETRY_URL
    else process.env.TRAJVIZ_OBS_TELEMETRY_URL = endpoint
    await writeFile(release, '')
    await rm(bridge, { recursive: true, force: true })
  }
})
