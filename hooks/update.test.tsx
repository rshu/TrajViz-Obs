import { expect, test } from 'claude-code/testing'
import type { EngineInterface, ProcessRunInit, ProcessRunResult } from 'claude-code'
import { parseArgs } from './lib/args'
import { updatePlugin } from './commands/update'
import type { CommandContext } from './commands/index'

const ROOT = '/config/plugins/cache/fixture/trajviz-obs/1.0.0'
const CWD = '/projects/current/nested'
const PROJECT = '/projects/current'
const OTHER = '/projects/other'
const ID = 'trajviz-obs@fixture'
const MARKET = '/config/plugins/marketplaces/fixture'
const row = (extra: Record<string, unknown> = {}) => ({ id: ID, scope: 'user', version: '1.0.0', installPath: ROOT, ...extra })
const catalog = (version = '1.1.0') => ({ name: 'fixture', plugins: [{ name: 'trajviz-obs', version }] })
const ok = (doc: unknown): ProcessRunResult => ({ exitCode: 0, stdout: JSON.stringify(doc), stderr: '', isStdoutTruncated: false, isStderrTruncated: false })
type Settings = {
  rows?: unknown; root?: string; manifest?: unknown; metadata?: unknown; cached?: unknown; remote?: unknown;
  directories?: Record<string, string | undefined>; windows?: boolean; noCanonical?: boolean;
  run?: (argv: readonly string[], init: ProcessRunInit | undefined) => Promise<ProcessRunResult>;
  fetch?: (url: string) => Promise<{ ok: boolean; text: string }>;
  configuration?: string;
}

function host(settings: Settings = {}) {
  const ran: { argv: string[]; init?: ProcessRunInit }[] = [], urls: string[] = [], statuses: (string | undefined)[] = []
  let applied = false
  const root = settings.root ?? ROOT
  const directories: Record<string, string | undefined> = { '/config': '/config', [root]: ROOT, [ROOT]: ROOT, [CWD]: CWD, [PROJECT]: PROJECT, [OTHER]: OTHER, ...settings.directories }
  const files: Record<string, unknown> = {
    [`${root}/.claude-plugin/plugin.json`]: settings.manifest ?? { name: 'trajviz-obs', version: '1.0.0' },
    '/config/plugins/known_marketplaces.json': settings.metadata ?? { fixture: { source: { source: 'github', repo: 'owner/repo' }, installLocation: MARKET } },
    [`${MARKET}/.claude-plugin/marketplace.json`]: settings.cached === undefined ? catalog() : settings.cached,
  }
  const $ = {
    plugin: { name: 'trajviz-obs', root },
    session: { cwd: async () => CWD },
    env: { get: async (key: string) => key === 'CLAUDE_CONFIG_DIR' ? settings.configuration ?? '/config' : key === 'OS' && settings.windows ? 'Windows_NT' : undefined },
    fs: {
      stat: async (path: string) => {
        if (!directories[path]) throw new Error('No directory')
        return { kind: 'dir', realPath: settings.noCanonical ? undefined : directories[path], size: 0, mtimeMs: 0, isLink: path !== directories[path] }
      },
      read: async (path: string) => {
        if (!(path in files)) throw new Error('No file')
        return JSON.stringify(files[path])
      },
    },
    clock: { after: () => ({ cancel: () => {} }) },
    ui: { status: (text: string | undefined) => { statuses.push(text) }, log: () => {} },
    state: { get: async () => ({ value: null, version: 0 }) },
    http: { fetch: async (url: string) => {
      urls.push(url)
      if (settings.fetch) return settings.fetch(url)
      return { ok: true, text: JSON.stringify(settings.remote === undefined ? catalog() : settings.remote) }
    } },
    process: { run: async (argv: readonly string[], init?: ProcessRunInit) => {
      ran.push({ argv: [...argv], init })
      if (settings.run) return settings.run(argv, init)
      if (argv[2] === 'list') {
        const rows = settings.rows ?? [row()]
        return ok(applied && Array.isArray(rows) ? rows.map(item => ({ ...item, version: '1.1.0' })) : rows)
      }
      if (argv[2] === 'update') applied = true
      return ok({ outcome: 'ok', message: argv[2] === 'marketplace' ? 'Refreshed' : 'Updated to 1.1.0.', ...(argv[2] === 'update' ? { newVersion: '1.1.0' } : {}) })
    } },
  } as unknown as EngineInterface
  return { $, ran, urls, statuses }
}

function context(args = 'update', personRequested = true): CommandContext {
  return { args, parsed: parseArgs(args), options: {}, personRequested }
}

test('update --check reads the running installation and remote catalog without modifying anything', async () => {
  const h = host()
  const result = await updatePlugin(h.$, context('update --check', false))
  expect(result.text).toContain('Installed: 1.0.0 · user · trajviz-obs@fixture')
  expect(result.text).toContain('Available: 1.1.0 · remote catalog (HEAD)')
  expect(result.text).toContain('No installation was changed.')
  expect(h.urls).toEqual(['https://raw.githubusercontent.com/owner/repo/HEAD/.claude-plugin/marketplace.json'])
  expect(h.ran.map(call => call.argv)).toEqual([['claude', 'plugin', 'list', '--json']])
  expect(h.statuses.at(-1)).toBe(undefined)
})

test('version checks preserve pinned refs and identify offline cached evidence without claiming freshness', async () => {
  const metadata = { fixture: { source: { source: 'github', repo: 'owner/repo', ref: 'release/v1' }, installLocation: MARKET } }
  const pinned = host({ metadata })
  expect((await updatePlugin(pinned.$, context('update --check'))).text).toContain('remote catalog (release/v1)')
  expect(pinned.urls[0]).toContain('/release%2Fv1/')
  const offline = host({ metadata, fetch: async () => { throw new Error('Offline') }, cached: catalog('1.0.0') })
  const result = await updatePlugin(offline.$, context('update --check'))
  expect(result.text).toContain('Available: 1.0.0 · cached catalog; latest remote version unknown')
  expect(result.text).not.toContain('up to date')
  expect(offline.urls).toHaveLength(1)
})

test('invalid, ambiguous, wrong-marketplace and missing catalog versions stay unknown', async () => {
  for (const malformed of [null, { name: 'other', plugins: [{ name: 'trajviz-obs', version: '1.1.0' }] }, { ...catalog(), plugins: [catalog().plugins[0], catalog().plugins[0]] }, { ...catalog(), plugins: [{ name: 'trajviz-obs' }] }]) {
    const h = host({ remote: malformed, cached: malformed })
    const result = await updatePlugin(h.$, context('update --check'))
    expect(result.text).toContain('Available: unknown')
    expect(h.ran).toHaveLength(1)
  }
})

test('native apply refreshes only this marketplace and updates the exact user scope without auto-approval', async () => {
  const h = host()
  const result = await updatePlugin(h.$, context())
  expect(h.ran.map(call => call.argv)).toEqual([
    ['claude', 'plugin', 'list', '--json'],
    ['claude', 'plugin', 'marketplace', 'update', 'fixture', '--json'],
    ['claude', 'plugin', 'update', ID, '--scope', 'user', '--json'],
    ['claude', 'plugin', 'list', '--json'],
  ])
  expect(h.urls).toHaveLength(0)
  expect(h.ran.slice(1, 3).every(call => call.init?.stdin === '' && call.init.timeoutMs === 120000)).toBe(true)
  expect(h.ran.every(call => call.init?.env?.CLAUDE_CONFIG_DIR === '/config')).toBe(true)
  expect(result.text).toContain('Verified installed: 1.1.0')
  expect(result.text).toContain('/reload-plugins')
  expect(h.statuses.at(-1)).toBe(undefined)
})

test('scope resolution excludes other projects and uses the matching native project path as cwd', async () => {
  const h = host({ rows: [row(), row({ scope: 'local', projectPath: OTHER }), row({ scope: 'project', projectPath: PROJECT })] })
  await updatePlugin(h.$, context())
  const update = h.ran.find(call => call.argv[2] === 'update')
  expect(update?.argv).toEqual(['claude', 'plugin', 'update', ID, '--scope', 'project', '--json'])
  expect(update?.init?.cwd).toBe(PROJECT)
  const local = host({ rows: [row(), row({ scope: 'project', projectPath: PROJECT }), row({ scope: 'local', projectPath: PROJECT })] })
  await updatePlugin(local.$, context())
  expect(local.ran.find(call => call.argv[2] === 'update')?.argv).toContain('local')
})

test('canonical cached aliases retain their installation identity', async () => {
  const h = host({ root: '/running/alias', directories: { '/running/alias': ROOT } })
  await updatePlugin(h.$, context())
  expect(h.ran.find(call => call.argv[2] === 'update')?.argv).toEqual(['claude', 'plugin', 'update', ID, '--scope', 'user', '--json'])
})

test('native local directory marketplaces and direct source folders stay manual', async () => {
  for (const settings of [
    { rows: [row({ readFromFolder: '/market/plugin' })], root: '/market/plugin' },
    { metadata: { fixture: { source: { source: 'directory', path: '/market' }, installLocation: MARKET } } },
  ]) {
    const h = host(settings)
    expect((await updatePlugin(h.$, context())).text).toContain('pull --ff-only')
    expect(h.ran).toHaveLength(1)
    expect(h.urls).toHaveLength(0)
  }
})

test('unmatched local checkout gives manual fast-forward and reload instructions without mutation', async () => {
  const h = host({ rows: [] })
  const result = await updatePlugin(h.$, context())
  expect(result.text).toContain(`git -C '${ROOT}' pull --ff-only`)
  expect(result.text).toContain('Current files: 1.0.0')
  expect(result.text).toContain('Available: unknown')
  expect(result.text).toContain('/reload-plugins')
  expect(h.ran).toHaveLength(1)
  expect(h.urls).toHaveLength(0)
})

test('a stale running cache asks for reload rather than suggesting git pull inside the cache', async () => {
  const h = host({ rows: [], directories: { '/config/plugins/cache': '/config/plugins/cache' } })
  const result = await updatePlugin(h.$, context())
  expect(result.text).toContain('no longer matches a current installation')
  expect(result.text).toContain('/reload-plugins')
  expect(result.text).not.toContain('git pull')
  expect(h.ran).toHaveLength(1)
})

test('ambiguous identities, missing canonical paths, missing marketplaces and manifest mismatch fail closed', async () => {
  for (const settings of [{ rows: [row(), row()] }, { noCanonical: true }, { metadata: {} }, { manifest: { name: 'other', version: '1.0.0' } }]) {
    const h = host(settings)
    expect((await updatePlugin(h.$, context())).exitCode).toBe(1)
    expect(h.ran).toHaveLength('noCanonical' in settings ? 0 : 1)
    expect(h.statuses.at(-1)).toBe(undefined)
  }
})

test('managed installations can be inspected but never modified', async () => {
  const h = host({ rows: [row({ scope: 'managed' })] })
  expect((await updatePlugin(h.$, context())).text).toContain('managed by your administrator')
  expect(h.ran).toHaveLength(1)
  expect((await updatePlugin(h.$, context('update --check'))).text).toContain('Available: 1.1.0')
  expect(h.ran).toHaveLength(2)
})

test('model-origin updates and unsupported arguments are rejected before subprocesses', async () => {
  const h = host()
  expect((await updatePlugin(h.$, context('update', false))).text).toContain('directly in the Claude Code composer')
  for (const args of ['update extra', 'update --check=no', 'update --yes', 'update --session abc']) {
    expect((await updatePlugin(h.$, context(args))).text).toContain('Usage: /obs update [--check]')
  }
  expect(h.ran).toHaveLength(0)
})

test('native approval requirements never gain --yes or --accept-command and stop subsequent work', async () => {
  const h = host({ run: async argv => argv[2] === 'list' ? ok([row()]) : { ...ok({ outcome: 'needs_confirmation', message: 'Confirm the source command in your shell.', shownCommand: { sha256: 'controlled' } }), exitCode: 1 } })
  const result = await updatePlugin(h.$, context())
  expect(result.exitCode).toBe(1)
  expect(result.text).toContain('No approval was accepted automatically.')
  expect(h.ran).toHaveLength(2)
  expect(h.ran.flatMap(call => call.argv).some(arg => arg === '--yes' || arg === '--accept-command')).toBe(false)
})

test('missing native CLI, truncated metadata, native update failures and timeout restore progress', async () => {
  for (const failAt of ['list', 'update', 'timeout', 'truncated']) {
    const h = host({ run: async argv => {
      if (failAt === 'timeout') throw new Error('Native operation timed out')
      if (argv[2] === 'list') return failAt === 'list' ? { ...ok(null), exitCode: 1 } : { ...ok([row()]), isStdoutTruncated: failAt === 'truncated' }
      if (argv[2] === 'marketplace') return ok({ outcome: 'ok' })
      return { ...ok({ outcome: 'error', message: 'Native update failed' }), exitCode: 1 }
    } })
    expect((await updatePlugin(h.$, context())).exitCode).toBe(1)
    expect(h.statuses.at(-1)).toBe(undefined)
  }
})

test('overlapping updates reject and a completed failure releases the update lock', async () => {
  let release = () => {}
  const gate = new Promise<void>(resolve => { release = resolve })
  const h = host({ run: async () => { await gate; return { ...ok(null), exitCode: 1 } } })
  const first = updatePlugin(h.$, context())
  expect((await updatePlugin(h.$, context())).text).toContain('already running')
  release(); await first
  const next = host()
  expect((await updatePlugin(next.$, context())).text).toContain('Verified installed: 1.1.0')
})

test('native success requires a unique resulting row and consistent installed version', async () => {
  for (const rows of [[], [row(), row()], [row({ version: '1.0.0' })]]) {
    let lists = 0
    const h = host({ run: async argv => {
      if (argv[2] === 'list') return ok(++lists === 1 ? [row()] : rows)
      return ok({ outcome: 'ok', newVersion: '1.1.0' })
    } })
    const result = await updatePlugin(h.$, context())
    expect(result.exitCode).toBe(1)
    expect(result.text).not.toContain('Verified installed:')
    expect(result.text).not.toContain('No installation was changed.')
  }
})

test('native diagnostics and configured refs cannot inject OSC, C1 or terminal controls', async () => {
  const h = host({ run: async argv => argv[2] === 'list' ? ok([row()]) : { ...ok({ outcome: 'error', message: '\u001b]2;secret\u0007\u009b31mNative failure\u001b[0m' }), exitCode: 1 } })
  const result = await updatePlugin(h.$, context())
  expect(result.text).toContain('Native failure')
  expect(result.text).not.toContain('secret')
  expect(result.text).not.toContain('\u001b')
  expect(result.text).not.toContain('\u009b')
})

test('native child configuration is frozen canonically when selecting a project cwd', async () => {
  const h = host({ configuration: '../profile', directories: { '../profile': '/config' }, rows: [row({ scope: 'project', projectPath: PROJECT })] })
  expect((await updatePlugin(h.$, context())).text).toContain('Verified installed: 1.1.0')
  expect(h.ran.every(call => call.init?.env?.CLAUDE_CONFIG_DIR === '/config')).toBe(true)
  expect(h.ran.find(call => call.argv[2] === 'update')?.init?.cwd).toBe(PROJECT)
})

test('manual checkout commands quote literal POSIX and PowerShell paths and omit control-character commands', async () => {
  const root = "/local/owner's $(project)"
  const posix = host({ root, rows: [] })
  expect((await updatePlugin(posix.$, context('update --check'))).text).toContain(`git -C '/local/owner'"'"'s $(project)' pull --ff-only`)
  const windows = host({ root, rows: [], windows: true })
  expect((await updatePlugin(windows.$, context())).text).toContain(`git -C '/local/owner''s $(project)' pull --ff-only`)
  const controls = host({ root: '/local/line\nfeed', rows: [] })
  expect((await updatePlugin(controls.$, context())).text).not.toContain('git -C')
})
