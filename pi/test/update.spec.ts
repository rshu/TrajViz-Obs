import { afterEach, describe, expect, test } from 'bun:test'
import { execFile } from 'node:child_process'
import { writeFileSync } from 'node:fs'
import { cp, mkdtemp, mkdir, readFile, realpath, rename, rm, symlink, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import { DefaultPackageManager, DefaultResourceLoader, createAgentSession, SessionManager, SettingsManager, type ExtensionCommandContext } from '@earendil-works/pi-coding-agent'
import { prepareUpdate, manualUpdateCommand, type UpdateOptions } from '../src/update.js'
import { Controller } from '../src/index.js'
import { Backend } from '../src/backend.js'

const SOURCE = 'git:github.com/rshu/TrajViz-Obs'
const NEXT = 'b'.repeat(40)
const folders: string[] = []
const controllers: Controller[] = []
const before = new Map<string, string | undefined>()
function env(key: string, value: string | undefined) { if (!before.has(key)) before.set(key, process.env[key]); if (value === undefined) delete process.env[key]; else process.env[key] = value }
afterEach(async () => {
  for (const controller of controllers.splice(0)) await controller.close()
  for (const folder of folders.splice(0)) await rm(folder, { recursive: true, force: true })
  for (const [key, value] of before) { if (value === undefined) delete process.env[key]; else process.env[key] = value }
  before.clear()
})
const signal = () => new AbortController().signal
const git = (args: string[], cwd: string): Promise<string> => new Promise((resolve, reject) => execFile('git', args, {
  cwd, env: { ...process.env, GIT_CONFIG_NOSYSTEM: '1', GIT_CONFIG_GLOBAL: path.join(cwd, 'nonexistent-global-config'), GIT_OPTIONAL_LOCKS: '0' },
}, (error, out) => error ? reject(error) : resolve(out)))

async function fixture(scope: 'user' | 'project' = 'user', source = SOURCE) {
  const folder = await mkdtemp(path.join(tmpdir(), 'trajviz-pi-update-')); folders.push(folder)
  const agent = path.join(folder, 'agent'), project = path.join(folder, 'project')
  const root = path.join(scope === 'user' ? agent : path.join(project, '.pi'), 'git/github.com/rshu/TrajViz-Obs')
  const entry = path.join(root, 'pi/trajviz-obs.ts')
  await mkdir(path.dirname(entry), { recursive: true }); await mkdir(project, { recursive: true }); await mkdir(agent, { recursive: true })
  await writeFile(path.join(root, 'package.json'), JSON.stringify({ name: 'trajviz-obs', version: '0.1.0' }))
  await writeFile(path.join(root, '.gitignore'), '.env\nnode_modules/\n__pycache__/\n')
  await writeFile(entry, 'export default function() {}\n')
  await git(['init', '--initial-branch=main'], root); await git(['add', '.'], root)
  await git(['-c', 'user.name=Controlled Test', '-c', 'user.email=test@invalid.example', 'commit', '-m', 'Controlled fixture'], root)
  await git(['remote', 'add', 'origin', 'https://github.com/rshu/TrajViz-Obs.git'], root)
  const current = (await git(['rev-parse', 'HEAD'], root)).trim()
  await git(['update-ref', 'refs/remotes/origin/main', current], root)
  await git(['config', 'branch.main.remote', 'origin'], root); await git(['config', 'branch.main.merge', 'refs/heads/main'], root)
  const globalFile = path.join(agent, 'settings.json'), projectFile = path.join(project, '.pi/settings.json')
  await mkdir(path.dirname(projectFile), { recursive: true })
  await writeFile(globalFile, JSON.stringify({ packages: scope === 'user' ? [source] : ['git:github.com/example/unrelated'] }))
  await writeFile(projectFile, JSON.stringify({ packages: scope === 'project' ? [source] : [] }))
  env('PI_CODING_AGENT_DIR', agent); env('TRAJVIZ_OBS_TELEMETRY', '0'); env('TRAJVIZ_OBS_TELEMETRY_STATE_DIR', path.join(folder, 'usage')); env('PI_OFFLINE', undefined)
  let trusted = true, applied = false, available = NEXT, remoteFailure = false, updates = 0
  const inspected: string[][] = [], selectedConfigurations: { source: string; scope: string }[][] = []
  let duringUpdate = () => {}
  const context = { cwd: project, isProjectTrusted: () => trusted, isIdle: () => true, hasPendingMessages: () => false }
  const options: UpdateOptions = {
    agentDir: agent, sourceInfo: { path: entry, source, scope, origin: 'package' },
    git: async (args, cwd, abort) => {
      abort?.throwIfAborted(); inspected.push(args)
      if (args[0] === 'ls-remote') { if (remoteFailure) throw new Error('Controlled network failure'); return `${available}\trefs/heads/main\n` }
      if (applied && ['rev-parse --verify HEAD', 'rev-parse --verify @{upstream}'].includes(args.join(' '))) return `${available}\n`
      return git(args, cwd)
    },
    manager: settings => {
      const manager = new DefaultPackageManager({ cwd: project, agentDir: agent, settingsManager: settings })
      return { listConfiguredPackages: () => manager.listConfiguredPackages(), update: async source => {
        updates++; selectedConfigurations.push(manager.listConfiguredPackages().map(row => ({ source: row.source, scope: row.scope })))
        expect(source).toBe(SOURCE); duringUpdate(); applied = true
      } }
    },
  }
  return { folder, root, agent, project, entry, globalFile, projectFile, current, context, options, inspected, selectedConfigurations,
    state: () => ({ applied, updates }), trust: (value: boolean) => { trusted = value }, next: (value: string) => { available = value }, failRemote: () => { remoteFailure = true },
    updating: (callback: () => void) => { duringUpdate = callback } }
}

async function localFixture(h: Awaited<ReturnType<typeof fixture>>) {
  const root = await realpath(h.root)
  await writeFile(path.join(h.root, 'pi/package.json'), JSON.stringify({ name: 'trajviz-obs-pi', version: '0.1.0' }))
  await writeFile(h.globalFile, JSON.stringify({ packages: [h.root] })); await writeFile(h.projectFile, '{}')
  h.options.sourceInfo = { path: h.entry, source: h.root, scope: 'user', origin: 'package' }
  const local = { checks: 0, updates: 0, accepted: true, transport: 'https://github.com/rshu/TrajViz-Obs.git', state: 'available' as 'available' | 'current' | 'blocked' | 'error',
    applied: 'updated' as 'updated' | 'error' | 'cancelled', questions: [] as { title: string; message: string }[],
    loadedAt: [] as (number | undefined)[],
    checking: () => {}, applying: () => {}, confirming: () => {} }
  h.options.confirm = async question => { local.questions.push(question); local.confirming(); return local.accepted }
  h.options.localUpdater = options => {
    local.loadedAt.push(options?.loadedAt)
    expect(options?.adapter).toBe('pi'); expect(options?.entryURL).toBe(pathToFileURL(h.entry).href)
    expect(options?.workerSource).toContain('local-update.ts')
    return {
      check: async abort => {
        abort?.throwIfAborted(); await options?.validateSource?.(path.join(root, 'pi')); local.checks++; local.checking()
        return { state: local.state, text: 'Local TrajViz Obs 0.1.0\nInstalled: current-revision\nAvailable: reviewed-revision\nVersion: 0.1.0 → 0.1.1',
          ...(local.state === 'available' ? { plan: { token: 'controlled-local-plan', root, branch: 'main', remote: 'https://github.com/rshu/TrajViz-Obs.git', effectiveRemote: local.transport, current: h.current, available: NEXT,
            version: '0.1.0', currentVersion: '0.1.0', availableVersion: '0.1.1', expiresAt: Date.now() + 300_000, steps: ['Fast-forward the reviewed revision', 'Reload Pi when idle'] } } : {}) }
      },
      apply: async (token, optionsApply) => {
        expect(token).toBe('controlled-local-plan'); optionsApply?.signal?.throwIfAborted()
        await options?.validateSource?.(path.join(root, 'pi')); local.updates++; local.applying()
        return { state: local.applied, text: local.applied === 'updated' ? 'Obs checkout updated · 0.1.1 · reviewed-revision.' : 'Local update failed; repair the checkout manually.' }
      },
    }
  }
  return local
}

describe('Pi plugin update inspection and native management', () => {
  test('read-only check reports version and available commit without touching settings or Git state', async () => {
    const h = await fixture(), settings = await readFile(h.globalFile), config = await readFile(path.join(h.root, '.git/config'))
    const result = await prepareUpdate(h.context, true, signal(), h.options)
    expect(result.text).toContain('TrajViz Obs 0.1.0'); expect(result.text).toContain('update available')
    expect(result.text).toContain('Available version: unknown until installed'); expect(result.applyUpdate).toBeUndefined()
    expect(h.state().updates).toBe(0); expect(await readFile(h.globalFile)).toEqual(settings); expect(await readFile(path.join(h.root, '.git/config'))).toEqual(config)
    expect((await git(['rev-parse', 'HEAD'], h.root)).trim()).toBe(h.current)
    expect(h.inspected.some(args => ['fetch', 'reset', 'clean', 'pull'].includes(args[0]!))).toBe(false)
  })

  for (const scope of ['user', 'project'] as const) test(`apply uses only the exact ${scope} package through the real SDK resolver`, async () => {
    const h = await fixture(scope), globalBefore = await readFile(h.globalFile), projectBefore = await readFile(h.projectFile)
    const result = await prepareUpdate(h.context, false, signal(), h.options)
    expect(h.state().updates).toBe(0); expect(result.applyUpdate).toBeDefined()
    const applied = await result.applyUpdate!()
    expect(applied.reload).toBe(true); expect(h.state().updates).toBe(1)
    expect(h.selectedConfigurations).toEqual([[{ source: SOURCE, scope }]])
    expect(await readFile(h.globalFile)).toEqual(globalBefore); expect(await readFile(h.projectFile)).toEqual(projectBefore)
  })

  for (const scope of ['user', 'project'] as const) test(`the actual native updater applies a ${scope} fixture commit without changing other packages or consent`, async () => {
    const h = await fixture(scope), remote = path.join(h.folder, 'remote.git'), publisher = path.join(h.folder, 'publisher')
    await git(['clone', '--bare', h.root, remote], h.folder)
    await git(['clone', remote, publisher], h.folder)
    await writeFile(path.join(publisher, 'package.json'), JSON.stringify({ name: 'trajviz-obs', version: '0.1.1', private: true }))
    await git(['add', 'package.json'], publisher)
    await git(['-c', 'user.name=Controlled Test', '-c', 'user.email=test@invalid.example', 'commit', '-m', 'Fixture update'], publisher)
    await git(['push', 'origin', 'main'], publisher)
    h.next((await git(['rev-parse', 'HEAD'], publisher)).trim())
    const other = path.join(h.agent, 'git/github.com/example/unrelated/sentinel.txt')
    await mkdir(path.dirname(other), { recursive: true }); await writeFile(other, 'Unrelated installation stays intact')
    const consent = path.join(h.folder, 'usage/pi-consent'); await mkdir(path.dirname(consent), { recursive: true }); await writeFile(consent, 'off\n')
    const globalBefore = await readFile(h.globalFile), projectBefore = await readFile(h.projectFile)
    let updates = 0
    h.options.manager = settings => {
      const native = new DefaultPackageManager({ cwd: h.project, agentDir: h.agent, settingsManager: settings })
      return { listConfiguredPackages: () => native.listConfiguredPackages(), update: async source => {
        updates++; expect(native.listConfiguredPackages().map(row => ({ source: row.source, scope: row.scope }))).toEqual([{ source: SOURCE, scope }])
        const values: Record<string, string> = {
          GIT_CONFIG_COUNT: '2', GIT_CONFIG_KEY_0: `url.file://${remote}.insteadOf`, GIT_CONFIG_VALUE_0: 'https://github.com/rshu/TrajViz-Obs.git',
          GIT_CONFIG_KEY_1: 'protocol.file.allow', GIT_CONFIG_VALUE_1: 'always', GIT_CONFIG_GLOBAL: path.join(h.folder, 'empty-gitconfig'),
          npm_config_offline: 'true', npm_config_audit: 'false', npm_config_fund: 'false', npm_config_ignore_scripts: 'true',
          npm_config_cache: path.join(h.folder, 'npm-cache'), npm_config_userconfig: path.join(h.folder, 'empty-npmrc'),
        }
        const previous = Object.fromEntries(Object.keys(values).map(key => [key, process.env[key]]))
        try { Object.assign(process.env, values); await native.update(source) }
        finally { for (const key of Object.keys(values)) { if (previous[key] === undefined) delete process.env[key]; else process.env[key] = previous[key] } }
      } }
    }
    const result = await (await prepareUpdate(h.context, false, signal(), h.options)).applyUpdate!()
    expect(result.reload).toBe(true); expect(updates).toBe(1)
    expect(JSON.parse(await readFile(path.join(h.root, 'package.json'), 'utf8')).version).toBe('0.1.1')
    expect((await git(['rev-parse', 'HEAD'], h.root)).trim()).toBe((await git(['rev-parse', 'HEAD'], publisher)).trim())
    expect((await git(['symbolic-ref', '--short', 'HEAD'], h.root)).trim()).toBe('main')
    expect(await readFile(h.globalFile)).toEqual(globalBefore); expect(await readFile(h.projectFile)).toEqual(projectBefore)
    expect(await readFile(consent, 'utf8')).toBe('off\n'); expect(await readFile(other, 'utf8')).toBe('Unrelated installation stays intact')
  }, 15_000)

  test('an unidentified checkout returns quoted manual guidance without mutating a package installation', async () => {
    const h = await fixture()
    const options = { ...h.options, sourceInfo: undefined, entryPath: h.entry, manager: () => { throw new Error('Must not construct native manager') } }
    const result = await prepareUpdate(h.context, false, signal(), options)
    expect(result.outcome).toBe('rejected'); expect(result.applyUpdate).toBeUndefined()
    expect(result.text).toContain('could not be matched to a configured installation')
    expect(result.text).toContain('pull --ff-only'); expect(result.text).toContain('/reload'); expect(h.state().updates).toBe(0)
  })

  test('offline, pinned and up-to-date states never start an update', async () => {
    const pinned = await fixture('user', `${SOURCE}@v0.1.0`)
    expect((await prepareUpdate(pinned.context, false, signal(), pinned.options)).text).toContain('preserves explicitly selected refs')
    expect(pinned.inspected.some(args => args[0] === 'ls-remote')).toBe(false)
    const h = await fixture()
    env('PI_OFFLINE', '1'); expect((await prepareUpdate(h.context, true, signal(), h.options)).text).toContain('unknown (Pi offline mode)')
    env('PI_OFFLINE', undefined); h.next(h.current)
    expect((await prepareUpdate(h.context, false, signal(), h.options)).text).toContain('up to date')
    expect(h.state().updates).toBe(0)
  })

  test('remote failures remain unknown and cancellation cannot become an up-to-date claim', async () => {
    const h = await fixture(); h.failRemote()
    const result = await prepareUpdate(h.context, true, signal(), h.options)
    expect(result.outcome).toBe('error'); expect(result.text).toContain('unknown (remote check failed)'); expect(result.text).not.toContain('up to date')
    const abort = new AbortController(); abort.abort(); await expect(prepareUpdate(h.context, true, abort.signal, h.options)).rejects.toThrow()
    expect(h.state().updates).toBe(0)
  })

  test('project trust, multiple scopes, source mismatch and unrelated loaded files fail closed', async () => {
    const h = await fixture('project'); h.trust(false)
    expect((await prepareUpdate(h.context, false, signal(), h.options)).text).toContain('Approve this project')
    h.trust(true); await writeFile(h.globalFile, JSON.stringify({ packages: ['https://github.com/rshu/TrajViz-Obs.git'] }))
    expect((await prepareUpdate(h.context, false, signal(), h.options)).text).toContain('configured more than once')
    await writeFile(h.globalFile, '{}')
    const other = path.join(h.folder, 'outside.ts'); await writeFile(other, '')
    expect((await prepareUpdate(h.context, false, signal(), { ...h.options, entryPath: other })).outcome).toBe('rejected')
    expect(h.state().updates).toBe(0)
  })

  for (const file of ['.env', 'private.txt', 'package.json']) test(`locally changed ${file} is protected from native reset/clean`, async () => {
    const h = await fixture(); await writeFile(path.join(h.root, file), 'controlled local data')
    if (file === 'package.json') await writeFile(path.join(h.root, file), JSON.stringify({ name: 'trajviz-obs', version: 'local' }))
    await expect(prepareUpdate(h.context, false, signal(), h.options)).rejects.toThrow('contains local changes or files')
    expect(h.state().updates).toBe(0)
  })

  test('Pi-generated dependency locks and Python bytecode do not block a clean managed installation', async () => {
    const h = await fixture()
    await writeFile(path.join(h.root, 'package-lock.json'), '{"lockfileVersion":3}')
    await mkdir(path.join(h.root, '__pycache__')); await writeFile(path.join(h.root, '__pycache__/cache.pyc'), '')
    await mkdir(path.join(h.root, 'node_modules')); await writeFile(path.join(h.root, 'node_modules/.package-lock.json'), '{}')
    expect((await prepareUpdate(h.context, false, signal(), h.options)).applyUpdate).toBeDefined()
  })

  test('local commits, changed origin, symlinked managed roots and registration changes are rejected', async () => {
    const h = await fixture()
    await writeFile(path.join(h.root, 'extra'), 'controlled'); await git(['add', '.'], h.root)
    await git(['-c', 'user.name=Controlled Test', '-c', 'user.email=test@invalid.example', 'commit', '-m', 'Local work'], h.root)
    expect((await prepareUpdate(h.context, false, signal(), h.options)).text).toContain('local commits')
    const changed = await fixture(); await git(['remote', 'set-url', 'origin', 'https://github.com/other/repo'], changed.root)
    expect((await prepareUpdate(changed.context, false, signal(), changed.options)).text).toContain('origin differs')
    const linked = await fixture(), moved = path.join(linked.folder, 'developer-checkout')
    const { rename } = await import('node:fs/promises'); await rename(linked.root, moved); await symlink(moved, linked.root, 'dir')
    expect((await prepareUpdate(linked.context, false, signal(), linked.options)).text).toContain('symlink')
    const registration = await fixture(), prepared = await prepareUpdate(registration.context, false, signal(), registration.options)
    await writeFile(registration.globalFile, '{}')
    expect((await prepared.applyUpdate!()).text).toContain('No update was started'); expect(registration.state().updates).toBe(0)
  })

  test('a custom clean branch is not reset by the host updater', async () => {
    const h = await fixture()
    await git(['checkout', '-b', 'custom-work'], h.root)
    await git(['branch', '--set-upstream-to=origin/main'], h.root)
    expect((await prepareUpdate(h.context, false, signal(), h.options)).text).toContain('custom local branch')
    expect(h.state().updates).toBe(0)
  })

  for (const change of ['branch', 'upstream', 'root', 'git-directory', 'ignored-file', 'trust', 'offline', 'busy'] as const) test(`a ${change} change after preparation cannot reach the native mutation`, async () => {
    const h = await fixture(), result = await prepareUpdate(h.context, false, signal(), h.options)
    if (change === 'branch') {
      await git(['checkout', '-b', 'new-work'], h.root); await git(['branch', '--set-upstream-to=origin/main'], h.root)
    } else if (change === 'upstream') {
      await git(['update-ref', 'refs/remotes/origin/other', h.current], h.root)
      await git(['branch', '--set-upstream-to=origin/other'], h.root)
    } else if (change === 'root') {
      const replacement = path.join(h.folder, 'replacement')
      await git(['clone', '--no-hardlinks', h.root, replacement], h.folder)
      await git(['remote', 'set-url', 'origin', 'https://github.com/rshu/TrajViz-Obs.git'], replacement)
      await rename(h.root, path.join(h.folder, 'previous-root')); await rename(replacement, h.root)
    } else if (change === 'git-directory') {
      const saved = path.join(h.folder, 'previous-git-directory')
      await rename(path.join(h.root, '.git'), saved); await cp(saved, path.join(h.root, '.git'), { recursive: true })
    } else if (change === 'ignored-file') await writeFile(path.join(h.root, '.env'), 'controlled local setting')
    else if (change === 'trust') h.trust(false)
    else if (change === 'offline') env('PI_OFFLINE', '1')
    else h.context.isIdle = () => false
    const applied = await result.applyUpdate!()
    expect(applied.outcome).toBe('error'); expect(applied.text).toContain('No update was started')
    expect(applied.reload).toBeUndefined(); expect(h.state().updates).toBe(0)
    expect((await git(['rev-parse', 'HEAD'], h.root)).trim()).toBe(h.current)
    if (change === 'branch') expect((await git(['symbolic-ref', '--short', 'HEAD'], h.root)).trim()).toBe('new-work')
    if (change === 'upstream') expect((await git(['rev-parse', '--abbrev-ref', '@{upstream}'], h.root)).trim()).toBe('origin/other')
    if (change === 'ignored-file') expect(await readFile(path.join(h.root, '.env'), 'utf8')).toBe('controlled local setting')
  })

  test('origin identity retains custom ports while preserving standard transport aliases', async () => {
    for (const origin of ['https://github.com:8443/rshu/TrajViz-Obs.git', 'ssh://git@github.com:8443/rshu/TrajViz-Obs.git']) {
      const h = await fixture(); await git(['remote', 'set-url', 'origin', origin], h.root)
      expect((await prepareUpdate(h.context, false, signal(), h.options)).text).toContain('origin differs')
      expect(h.state().updates).toBe(0)
    }
    for (const source of ['git:https://github.com:8443/rshu/TrajViz-Obs', 'git:github.com:8443/rshu/TrajViz-Obs']) {
      const h = await fixture('user', source)
      await git(['remote', 'set-url', 'origin', 'https://github.com:8443/rshu/TrajViz-Obs.git'], h.root)
      expect((await prepareUpdate(h.context, true, signal(), h.options)).text).toContain('update available')
    }
    for (const origin of ['https://github.com:443/rshu/TrajViz-Obs.git', 'ssh://git@github.com:22/rshu/TrajViz-Obs.git', 'git@github.com:rshu/TrajViz-Obs.git']) {
      const h = await fixture(); await git(['remote', 'set-url', 'origin', origin], h.root)
      expect((await prepareUpdate(h.context, true, signal(), h.options)).text).toContain('update available')
    }
  })

  test('a port change after preparation is rejected before mutation', async () => {
    const h = await fixture(), result = await prepareUpdate(h.context, false, signal(), h.options)
    await git(['remote', 'set-url', 'origin', 'https://github.com:8443/rshu/TrajViz-Obs.git'], h.root)
    expect((await result.applyUpdate!()).text).toContain('No update was started'); expect(h.state().updates).toBe(0)
  })

  test('a native no-op cannot claim an update or trigger reload', async () => {
    const h = await fixture()
    h.options.manager = settings => {
      const manager = new DefaultPackageManager({ cwd: h.project, agentDir: h.agent, settingsManager: settings })
      return { listConfiguredPackages: () => manager.listConfiguredPackages(), update: async () => {} }
    }
    const applied = await (await prepareUpdate(h.context, false, signal(), h.options)).applyUpdate!()
    expect(applied.outcome).toBe('error'); expect(applied.text).toContain('did not advance')
    expect(applied.reload).toBeUndefined(); expect(applied.text).not.toContain('TrajViz Obs updated')
  })

  test('native incomplete markers require manual repair before checking, dispatching, or claiming success', async () => {
    const initial = await fixture(), initialMarker = path.join(path.dirname(initial.root), `.${path.basename(initial.root)}.pi-update-incomplete`)
    await writeFile(initialMarker, '')
    for (const check of [true, false]) await expect(prepareUpdate(initial.context, check, signal(), initial.options)).rejects.toThrow('Repair it with Pi’s native package manager')
    expect(initial.state().updates).toBe(0)
    const late = await fixture(), prepared = await prepareUpdate(late.context, false, signal(), late.options)
    await writeFile(path.join(path.dirname(late.root), `.${path.basename(late.root)}.pi-update-incomplete`), '')
    const refused = await prepared.applyUpdate!()
    expect(refused.text).toContain('No update was started'); expect(late.state().updates).toBe(0)
    const partial = await fixture()
    partial.updating(() => writeFileSync(path.join(path.dirname(partial.root), `.${path.basename(partial.root)}.pi-update-incomplete`), ''))
    const failed = await (await prepareUpdate(partial.context, false, signal(), partial.options)).applyUpdate!()
    expect(partial.state().updates).toBe(1); expect(failed.outcome).toBe('error'); expect(failed.reload).toBeUndefined()
    expect(failed.text).toContain('update as incomplete'); expect(failed.text).toContain('may be incomplete')
    expect(failed.text).not.toContain('TrajViz Obs updated')
  })

  test('native errors cannot expose source credentials, query tokens, or terminal controls', async () => {
    const h = await fixture()
    h.updating(() => { throw new Error('git clone https://private-user:secret-password@host.example/repo?token=private-token\x1b]2;private-title\x07 failed') })
    const failed = await (await prepareUpdate(h.context, false, signal(), h.options)).applyUpdate!()
    expect(failed.outcome).toBe('error'); expect(failed.text).toContain('native package update failed')
    for (const secret of ['private-user', 'secret-password', 'private-token', 'private-title', 'https://', '\x1b', '\x07']) expect(failed.text).not.toContain(secret)
  })

  test('manual checkout commands quote POSIX and PowerShell paths safely', () => {
    expect(manualUpdateCommand("/tmp/owner's repo", 'darwin')).toBe("git -C '/tmp/owner'\\''s repo' pull --ff-only")
    expect(manualUpdateCommand("C:\\dev\\owner's repo", 'win32')).toBe("git -C 'C:\\dev\\owner''s repo' pull --ff-only")
    expect(manualUpdateCommand("C:\\dev\\owner's repo", 'darwin')).toBe("git -C 'C:\\dev\\owner''s repo' pull --ff-only")
  })

  test('control characters never become a displayed manual update command or a changed copyable target', async () => {
    for (const character of ['\n', '\r', '\t', '\x1b', '\x7f', '\x85', '\x9b']) {
      expect(manualUpdateCommand(`/tmp/unsafe${character}checkout`, 'darwin')).toBeUndefined()
      expect(manualUpdateCommand(`C:\\unsafe${character}checkout`, 'win32')).toBeUndefined()
    }
    const h = await fixture(), unsafe = path.join(h.folder, 'unsafe\ncheckout')
    const { rename } = await import('node:fs/promises'); await rename(h.root, unsafe)
    const result = await prepareUpdate(h.context, false, signal(), { ...h.options, sourceInfo: undefined, entryPath: path.join(unsafe, 'pi/trajviz-obs.ts') })
    expect(result.text).toContain('Open the plugin’s local checkout in a terminal')
    expect(result.text).not.toContain('git -C'); expect(result.text).not.toContain(unsafe)
    expect(result.text).not.toContain('unsafecheckout'); expect(result.applyUpdate).toBeUndefined()
  })

  test('a native apply failure reports possible partial update without reload or rollback promises', async () => {
    const h = await fixture(); h.updating(() => { throw new Error('Controlled installation failure') })
    const result = await (await prepareUpdate(h.context, false, signal(), h.options)).applyUpdate!()
    expect(result.outcome).toBe('error'); expect(result.reload).toBeUndefined(); expect(result.text).toContain('may be incomplete'); expect(result.text).toContain('No rollback was attempted')
  })
})

describe('Pi local checkout update review', () => {
  test('check shows installed and available versions without confirmation or native package mutations', async () => {
    const h = await fixture(), local = await localFixture(h)
    const global = await readFile(h.globalFile), project = await readFile(h.projectFile)
    const result = await prepareUpdate(h.context, true, signal(), h.options)
    expect(result.text).toContain('0.1.0 → 0.1.1'); expect(result.text).toContain('reviewed-revision')
    expect(result.applyUpdate).toBeUndefined(); expect(local.questions).toEqual([]); expect(local.updates).toBe(0)
    expect(h.state().updates).toBe(0); expect(await readFile(h.globalFile)).toEqual(global); expect(await readFile(h.projectFile)).toEqual(project)
  })

  test('apply confirms the exact shared repository and reviewed revision before returning a mutation', async () => {
    const h = await fixture(), local = await localFixture(h)
    const result = await prepareUpdate(h.context, false, signal(), h.options)
    expect(local.questions).toHaveLength(1)
    for (const value of [h.root, 'main', 'https://github.com/rshu/TrajViz-Obs.git', NEXT, 'other coding-agent adapters']) expect(local.questions[0]!.message).toContain(value)
    expect(local.updates).toBe(0); expect(result.applyUpdate).toBeDefined()
    const applied = await result.applyUpdate!()
    expect(local.updates).toBe(1); expect(applied.reload).toBe(true); expect(h.state().updates).toBe(0)
  })

  test('confirmation exposes a rewritten transport endpoint without repeating the configured origin', async () => {
    const h = await fixture(), local = await localFixture(h)
    local.transport = 'https://[redacted]@mirror.example.invalid/repo?[redacted]'
    await prepareUpdate(h.context, false, signal(), h.options)
    expect(local.questions[0]!.message).toContain(`Transport: ${local.transport}`)
    expect(local.questions[0]!.message.split('\n').length).toBeLessThan(12)
  })

  test('declining or cancelling confirmation leaves the checkout and active controller unchanged', async () => {
    const h = await fixture(), local = await localFixture(h); local.accepted = false
    const declined = await prepareUpdate(h.context, false, signal(), h.options)
    expect(declined.outcome).toBe('cancelled'); expect(declined.applyUpdate).toBeUndefined(); expect(local.updates).toBe(0)
    const abort = new AbortController(); local.confirming = () => abort.abort()
    await expect(prepareUpdate(h.context, false, abort.signal, h.options)).rejects.toThrow()
    expect(local.updates).toBe(0); expect((await git(['rev-parse', 'HEAD'], h.root)).trim()).toBe(h.current)
  })

  test('an available plan needs a native UI confirmation, while current and blocked states never prompt', async () => {
    const h = await fixture(), local = await localFixture(h); delete h.options.confirm
    expect((await prepareUpdate(h.context, false, signal(), h.options)).outcome).toBe('rejected')
    for (const state of ['current', 'blocked', 'error'] as const) {
      local.state = state
      const result = await prepareUpdate(h.context, false, signal(), h.options)
      expect(result.applyUpdate).toBeUndefined()
      expect(result.outcome).toBe(state === 'current' ? 'success' : state === 'blocked' ? 'rejected' : 'error')
    }
    expect(local.questions).toEqual([]); expect(local.updates).toBe(0)
  })

  for (const changed of ['registration', 'source', 'busy', 'queued', 'trust', 'offline'] as const) test(`a local ${changed} change after confirmation blocks apply`, async () => {
    const h = await fixture(), local = await localFixture(h)
    h.options.currentSourceInfo = () => h.options.sourceInfo
    const prepared = await prepareUpdate(h.context, false, signal(), h.options)
    if (changed === 'registration') await writeFile(h.globalFile, '{}')
    else if (changed === 'source') h.options.sourceInfo = { ...h.options.sourceInfo!, source: '/different/checkout' }
    else if (changed === 'busy') h.context.isIdle = () => false
    else if (changed === 'queued') h.context.hasPendingMessages = () => true
    else if (changed === 'trust') h.trust(false)
    else env('PI_OFFLINE', '1')
    if (changed === 'busy' || changed === 'queued') expect((await prepared.applyUpdate!()).outcome).toBe('error')
    else await expect(prepared.applyUpdate!()).rejects.toThrow()
    expect(local.updates).toBe(0); expect(h.state().updates).toBe(0)
  })

  test('new agent work during the final native registration read cannot dispatch a local worker', async () => {
    const h = await fixture(), local = await localFixture(h), create = h.options.manager!
    let preparing = true
    h.options.manager = settings => {
      const native = create(settings)
      return { ...native, listConfiguredPackages: () => { const rows = native.listConfiguredPackages(); if (!preparing) h.context.isIdle = () => false; return rows } }
    }
    const prepared = await prepareUpdate(h.context, false, signal(), h.options); preparing = false
    await expect(prepared.applyUpdate!()).rejects.toThrow('Pi started new work')
    expect(local.updates).toBe(0); expect(h.state().updates).toBe(0)
  })

  test('built local bundles remain manual and do not run a source-only update', async () => {
    const h = await fixture(), local = await localFixture(h), built = path.join(h.root, 'pi/dist/trajviz-obs.js')
    await mkdir(path.dirname(built)); await writeFile(built, 'export default function() {}')
    h.options.sourceInfo = { ...h.options.sourceInfo!, path: await realpath(built) }; delete h.options.localUpdater
    const result = await prepareUpdate(h.context, false, signal(), h.options)
    expect(result.text).toContain('built package'); expect(result.applyUpdate).toBeUndefined()
    expect(local.checks).toBe(0); expect(local.questions).toEqual([]); expect(local.updates).toBe(0)
  })

  test('a symlinked native source path remains visible to the checkout provenance guard', async () => {
    const h = await fixture(); await localFixture(h); delete h.options.localUpdater
    const link = path.join(await realpath(h.folder), 'linked-extension.ts'); await symlink(h.entry, link)
    h.options.sourceInfo = { path: link, source: 'local', scope: 'temporary', origin: 'top-level' }
    const result = await prepareUpdate(h.context, false, signal(), h.options)
    expect(result.outcome).toBe('rejected'); expect(result.text).toContain('independent local source checkout')
    expect(result.applyUpdate).toBeUndefined(); expect(h.state().updates).toBe(0)
  })
})

describe('Pi direct update command lifecycle', () => {
  async function controllerFixture(useLocal = false) {
    const h = await fixture(), notifications: string[] = []
    const local = useLocal ? await localFixture(h) : undefined
    const backend = new Backend(h.project, { cacheRoot: path.join(h.folder, 'cache') })
    let activeRevisions = 0, closed = false, reloads = 0
    const run = backend.revisions.run.bind(backend.revisions)
    backend.revisions.run = async request => { activeRevisions++; try { return await run(request) } finally { activeRevisions-- } }
    const close = backend.close.bind(backend)
    backend.close = async () => { await close(); closed = true }
    const context = { ...h.context, mode: 'rpc', hasUI: true, isIdle: () => true, hasPendingMessages: () => false,
      sessionManager: SessionManager.inMemory(h.project), getContextUsage: () => undefined,
      ui: { notify: (value: string) => notifications.push(value), confirm: async (title: string, message: string) => { local?.questions.push({ title, message }); return local?.accepted ?? true } },
      reload: async () => { expect(closed).toBe(true); expect(activeRevisions).toBe(0); reloads++ },
    } as unknown as ExtensionCommandContext
    const controller = new Controller(backend, context, '1.5.2', () => h.options); controllers.push(controller)
    h.updating(() => { expect(closed).toBe(true); expect(activeRevisions).toBe(0) })
    return { ...h, local, backend, controller, context, notifications, lifecycle: () => ({ closed, activeRevisions, reloads }) }
  }

  test('SDK execution cannot apply updates, while check remains read-only', async () => {
    const h = await controllerFixture()
    expect((await h.controller.execute('update')).outcome).toBe('rejected')
    expect((await h.controller.execute('update --check')).text).toContain('update available')
    expect(h.state().updates).toBe(0); expect(h.lifecycle().closed).toBe(false)
  })

  test('a direct RPC command quiesces after its transaction and reloads only after native apply', async () => {
    const h = await controllerFixture()
    await h.controller.command('update', h.context)
    expect(h.state().updates).toBe(1); expect(h.lifecycle()).toEqual({ closed: true, activeRevisions: 0, reloads: 1 })
    expect(h.notifications.at(-1)).toContain('Reloading Pi extensions')
  })

  test('a confirmed local source update uses the same idle quiescence and native reload lifecycle', async () => {
    const h = await controllerFixture(true)
    h.local!.applying = () => { expect(h.lifecycle().closed).toBe(true); expect(h.lifecycle().activeRevisions).toBe(0) }
    await h.controller.command('update --check', h.context)
    expect(h.local!.questions).toEqual([]); expect(h.lifecycle().closed).toBe(false)
    await h.controller.command('update', h.context)
    expect(h.local!.questions).toHaveLength(1); expect(h.local!.updates).toBe(1)
    expect(h.local!.loadedAt).toHaveLength(2); expect(h.local!.loadedAt[0]).toBeNumber(); expect(h.local!.loadedAt[1]).toBe(h.local!.loadedAt[0])
    expect(h.state().updates).toBe(0); expect(h.lifecycle()).toEqual({ closed: true, activeRevisions: 0, reloads: 1 })
  })

  test('declining a local update keeps Obs loaded; SDK execution cannot bypass confirmation', async () => {
    const h = await controllerFixture(true); h.local!.accepted = false
    expect((await h.controller.execute('update')).outcome).toBe('rejected')
    await h.controller.command('update', h.context)
    expect(h.local!.updates).toBe(0); expect(h.lifecycle().closed).toBe(false); expect(h.lifecycle().reloads).toBe(0)
    expect(h.notifications.at(-1)).toContain('cancelled'); expect((await h.controller.execute('help')).text).toContain('/obs')
  })

  test('local confirmation dismisses the progress overlay before the native dialog opens', async () => {
    const h = await controllerFixture(true); let overlayOpen = false, confirmations = 0
    h.context.mode = 'tui'
    Object.assign(h.context.ui, {
      setWidget() {}, setStatus() {},
      custom: (factory: any) => new Promise(resolve => {
        overlayOpen = true
        factory({ terminal: { rows: 24, columns: 80 }, requestRender() {} }, { fg: (_name: string, text: string) => text }, {}, (value: unknown) => { overlayOpen = false; resolve(value) })
      }),
      confirm: async (_title: string, message: string, options: { signal: AbortSignal }) => {
        expect(overlayOpen).toBe(false); expect(options.signal.aborted).toBe(false)
        expect(message.split('\n').length).toBeLessThan(12); expect(message).toContain(NEXT)
        confirmations++; return false
      },
    })
    await h.controller.command('update', h.context)
    expect(confirmations).toBe(1); expect(h.local!.updates).toBe(0); expect(h.lifecycle().closed).toBe(false)
  })

  test('a failed local update never reloads or claims success', async () => {
    const h = await controllerFixture(true); h.local!.applied = 'error'
    await h.controller.command('update', h.context)
    expect(h.local!.updates).toBe(1); expect(h.lifecycle().reloads).toBe(0)
    expect(h.notifications.at(-1)).toContain('repair the checkout manually')
  })

  test('check never reloads and running or queued agent work blocks apply', async () => {
    const h = await controllerFixture()
    await h.controller.command('update --check', h.context)
    expect(h.lifecycle().reloads).toBe(0)
    h.context.isIdle = () => false; await h.controller.command('update', h.context)
    expect(h.notifications.at(-1)).toContain('Wait for Pi')
    h.context.isIdle = () => true; h.context.hasPendingMessages = () => true; await h.controller.command('update', h.context)
    expect(h.state().updates).toBe(0); expect(h.lifecycle().closed).toBe(false)
  })

  test('new agent work arriving during preflight also blocks mutation without aborting the agent', async () => {
    const h = await controllerFixture(), inspect = h.options.git!
    h.options.git = async (args, cwd, abort) => { const result = await inspect(args, cwd, abort); if (args[0] === 'ls-remote') h.context.isIdle = () => false; return result }
    await h.controller.command('update', h.context)
    expect(h.notifications.at(-1)).toContain('Pi started new work')
    expect(h.state().updates).toBe(0); expect(h.lifecycle().closed).toBe(false); expect(h.lifecycle().reloads).toBe(0)
  })

  test('new work during quiescence is checked again before the native mutation', async () => {
    const h = await controllerFixture(), close = h.backend.close.bind(h.backend)
    h.backend.close = async () => { await close(); h.context.isIdle = () => false }
    await h.controller.command('update', h.context)
    expect(h.state().updates).toBe(0); expect(h.lifecycle().reloads).toBe(0)
    expect(h.notifications.at(-1)).toContain('Pi started new work')
    expect(h.notifications.at(-1)).toContain('No update was started')
  })

  test('new work during native apply defers reload and never aborts the coding task', async () => {
    const h = await controllerFixture(); let aborted = 0
    h.context.abort = () => { aborted++ }
    h.updating(() => { h.context.isIdle = () => false })
    await h.controller.command('update', h.context)
    expect(h.state().updates).toBe(1); expect(h.lifecycle().reloads).toBe(0); expect(aborted).toBe(0)
    expect(h.notifications.at(-1)).toContain('Run /reload after it finishes')
    expect(h.notifications.at(-1)).not.toContain('Reloading Pi extensions')
  })

  test('native failures and reload failures leave explicit recovery instructions for subsequent commands', async () => {
    const failed = await controllerFixture(); failed.updating(() => { throw new Error('Controlled update failure') })
    await failed.controller.command('update', failed.context)
    expect(failed.notifications.at(-1)).toContain('may be incomplete')
    await failed.controller.command('help', failed.context)
    expect(failed.notifications.at(-1)).toContain('Obs is unloaded. Run /reload')
    const reload = await controllerFixture(); reload.context.reload = async () => { throw new Error('Controlled reload failure') }
    await reload.controller.command('update', reload.context)
    expect(reload.notifications.at(-1)).toContain('plugin files were updated, but Pi could not reload')
    await reload.controller.command('help', reload.context)
    expect(reload.notifications.at(-1)).toContain('Obs is unloaded. Run /reload')
  })

  for (const failure of [false, true]) test(`native TUI output is isolated from live rendering and input is drained on ${failure ? 'failure' : 'success'}`, async () => {
    const h = await controllerFixture(), events: string[] = []
    let input: ((data: string) => unknown) | undefined
    h.context.mode = 'tui'
    const tui = {
      terminal: { rows: 30, columns: 100, drainInput: async () => { events.push('drain'); expect(input?.('queued prompt\r')).toEqual({ consume: true }) } },
      stop: () => { events.push('stop') }, start: () => { events.push('start') }, requestRender: (force?: boolean) => { if (force) events.push('redraw') },
    }
    Object.assign(h.context.ui, {
      setWidget() {}, setStatus() {},
      onTerminalInput: (listener: (data: string) => unknown) => { input = listener; events.push('guard'); return () => { input = undefined; events.push('unguard') } },
      custom: (factory: any) => new Promise((resolve, reject) => { Promise.resolve(factory(tui, { fg: (_name: string, text: string) => text }, {}, resolve)).catch(reject) }),
    })
    h.updating(() => { events.push('native'); expect(input?.('typed while updating')).toEqual({ consume: true }); if (failure) throw new Error('Controlled native failure') })
    await h.controller.command('update', h.context)
    expect(events).toEqual(['guard', 'stop', 'native', 'start', 'drain', 'redraw', 'unguard'])
    expect(h.lifecycle().reloads).toBe(failure ? 0 : 1)
  })

  test('the actual Pi SDK reload lifecycle completes after the update command without modifying conversation history', async () => {
    const h = await controllerFixture(), lifecycle: string[] = [], notifications: string[] = []
    const settings = SettingsManager.inMemory({ packages: [] })
    let factories = 0
    const loader = new DefaultResourceLoader({ cwd: h.project, agentDir: h.agent, settingsManager: settings,
      noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true,
      extensionFactories: [pi => {
        factories++
        pi.on('session_start', event => { lifecycle.push(`start:${event.reason}`) })
        pi.on('session_shutdown', async event => { lifecycle.push(`shutdown:${event.reason}`); await h.controller.close() })
        pi.registerCommand('controlled-update', { handler: (_args, ctx) => h.controller.command('update', ctx) })
      }],
    })
    await loader.reload()
    const manager = SessionManager.inMemory(h.project)
    const { session } = await createAgentSession({ cwd: h.project, agentDir: h.agent, settingsManager: settings, resourceLoader: loader, sessionManager: manager, tools: [] })
    try {
      await session.bindExtensions({ mode: 'rpc', uiContext: { notify: (value: string) => notifications.push(value) } as any,
        commandContextActions: { reload: () => session.reload() } as any })
      const history = JSON.stringify(manager.getEntries())
      await session.prompt('/controlled-update')
      expect(h.state().updates).toBe(1); expect(factories).toBe(2)
      expect(lifecycle).toEqual(['start:startup', 'shutdown:reload', 'start:reload'])
      expect(notifications.at(-1)).toContain('Reloading Pi extensions')
      expect(JSON.stringify(manager.getEntries())).toBe(history)
      expect(session.isStreaming).toBe(false)
    } finally { session.dispose() }
  }, 15_000)
})
