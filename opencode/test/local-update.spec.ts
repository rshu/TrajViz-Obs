import { afterEach, describe, expect, test } from 'bun:test'
import { mkdtemp, mkdir, readFile, readdir, realpath, rm, symlink, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import { randomUUID } from 'node:crypto'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { LocalCheckoutUpdater, runLocalUpdateCommand, updateProcessTreeCommand } from '../../shared/local-update.js'

const directories: string[] = []
afterEach(async () => { for (const directory of directories.splice(0)) await rm(directory, { recursive: true, force: true }) })
const origin = 'https://github.com/rshu/TrajViz-Obs.git'
const git = (cwd: string, ...args: string[]) => runLocalUpdateCommand('git', args, cwd)
async function fixture(options: { advance?: boolean } = {}) {
  const directory = await realpath(await mkdtemp(path.join(tmpdir(), 'trajviz-local-update-'))); directories.push(directory)
  const publisher = path.join(directory, 'publisher'), root = path.join(directory, 'checkout'), adapter = path.join(root, 'opencode')
  await mkdir(path.join(publisher, 'opencode/src'), { recursive: true })
  await writeFile(path.join(publisher, 'package.json'), JSON.stringify({ name: 'trajviz-obs', private: true }))
  await writeFile(path.join(publisher, '.gitignore'), 'node_modules/\nopencode/dist/\nopencode/runtime/\nopencode/src/shared/\nopencode/src/types.ts\nopencode/LICENSE\n.env\n.env.*\n.pytest_cache/\n.venv/\n.ruff_cache/\n.DS_Store\n*.log\ndocs/\n.claude/\n.claude-plugin/types/\n.playwright-mcp/\nprivate.ignore\n')
  await mkdir(path.join(publisher, 'bridge'), { recursive: true })
  await writeFile(path.join(publisher, 'bridge/trajviz_obs_cli.py'), '# Fixture Python entry')
  await writeFile(path.join(publisher, 'opencode/src/local-update.ts'), '// Loaded fixture extension\n')
  await writeFile(path.join(publisher, 'opencode/build.cjs'), `const fs=require('node:fs');for(const f of ['dist/tui.js','dist/gui-experimental.cjs','runtime/bridge/trajviz_obs_cli.py']){fs.mkdirSync(require('node:path').dirname(f),{recursive:true});fs.writeFileSync(f,'built fixture')}\n`)
  const manifest = (version: string) => JSON.stringify({ name: 'trajviz-obs-opencode', version, private: true, scripts: { build: 'node build.cjs' } })
  await writeFile(path.join(publisher, 'opencode/package.json'), manifest('0.5.5'))
  await mkdir(path.join(publisher, 'pi/src'), { recursive: true })
  await writeFile(path.join(publisher, 'pi/package.json'), JSON.stringify({ name: 'trajviz-obs-pi', version: '0.1.1' }))
  await writeFile(path.join(publisher, 'pi/trajviz-obs.ts'), '// Pi source fixture')
  await writeFile(path.join(publisher, 'pi/src/index.ts'), '// Pi fixture implementation')
  await runLocalUpdateCommand('bun', ['install', '--ignore-scripts'], path.join(publisher, 'opencode'))
  await git(publisher, 'init', '-b', 'main'); await git(publisher, 'config', 'user.email', 'fixture@example.invalid'); await git(publisher, 'config', 'user.name', 'Fixture')
  await git(publisher, 'add', '.'); await git(publisher, 'commit', '-m', 'Base fixture')
  await git(directory, 'clone', publisher, root); await git(root, 'remote', 'set-url', 'origin', origin)
  await git(root, 'config', `url.${publisher}.insteadOf`, origin)
  await git(root, 'config', 'user.email', 'fixture@example.invalid'); await git(root, 'config', 'user.name', 'Fixture')
  const current = (await git(root, 'rev-parse', 'HEAD')).trim()
  const advance = async () => { await writeFile(path.join(publisher, 'opencode/package.json'), manifest('0.5.6')); await writeFile(path.join(publisher, 'pi/package.json'), JSON.stringify({ name: 'trajviz-obs-pi', version: '0.1.2' })); await git(publisher, 'add', '.'); await git(publisher, 'commit', '-m', 'Update fixture'); return (await git(publisher, 'rev-parse', 'HEAD')).trim() }
  const available = options.advance === false ? current : await advance()
  const calls: { command: string; args: string[]; signal?: AbortSignal }[] = []
  let intercept: undefined | ((command: string, args: string[]) => void | Promise<void>)
  const run: typeof runLocalUpdateCommand = async (command, args, cwd, opts) => {
    calls.push({ command, args: [...args], signal: opts?.signal })
    await intercept?.(command, args)
    const argv = [...args]
    // Network operations alone use the temporary publisher; all Git inspection,
    // checkout mutation, Bun installation, and build use actual subprocesses.
    if (command === 'git' && (argv.includes('ls-remote') || argv.includes('fetch'))) argv[argv.indexOf('origin')] = publisher
    return runLocalUpdateCommand(command, argv, cwd, opts)
  }
  const entryURL = pathToFileURL(path.join(adapter, 'src/local-update.ts')).href
  const updater = new LocalCheckoutUpdater({ entryURL, run, inlineWorker: true, remoteVersion: async commit => commit === current ? '0.5.5' : '0.5.6' })
  const plan = async () => { const checked = await updater.check(); expect(checked.state).toBe('available'); expect(checked.plan).toBeDefined(); return checked.plan! }
  return { root, adapter, publisher, directory, current, available, updater, calls, run, entryURL, advance, plan,
    intercept(value: typeof intercept) { intercept = value } }
}

describe('server-owned local checkout updates', () => {
  test('check is read-only and exposes the exact shared checkout revision and actions', async () => {
    const h = await fixture(), before = await readdir(path.join(h.root, '.git'))
    const plan = await h.plan()
    expect(plan).toMatchObject({ root: h.root, branch: 'main', remote: origin, effectiveRemote: h.publisher, current: h.current, available: h.available, version: '0.5.5' })
    expect(plan.steps).toContain('bun install --frozen-lockfile')
    expect(await readdir(path.join(h.root, '.git'))).toEqual(before)
    expect(h.calls.every(call => call.command === 'git' && !call.args.includes('fetch') && !call.args.includes('merge'))).toBe(true)
    expect((await git(h.root, 'rev-parse', 'HEAD')).trim()).toBe(h.current)
  })

  test('real Git, frozen Bun install, and build update only the reviewed checkout and retain a restart receipt', async () => {
    const h = await fixture(), plan = await h.plan(), progress: string[] = []
    const preference = path.join(h.directory, 'consent.json'); await writeFile(preference, '{"enabled":false}')
    const result = await h.updater.apply(plan.token, { progress: text => progress.push(text) })
    expect(result.state).toBe('updated'); expect(result.restartRequired).toBe(true)
    expect(result.text).toContain('0.5.6'); expect(result.text).toContain('running plugin still uses its loaded code')
    expect((await git(h.root, 'rev-parse', 'HEAD')).trim()).toBe(h.available)
    expect((await git(h.root, 'rev-parse', '@{upstream}')).trim()).toBe(h.available)
    expect((await git(h.root, 'status', '--porcelain')).trim()).toBe('')
    expect(await readFile(preference, 'utf8')).toBe('{"enabled":false}')
    expect(progress).toHaveLength(4)
    const receipt = JSON.parse(await readFile(result.receipt!, 'utf8'))
    expect(receipt).toMatchObject({ state: 'updated', installed: h.available, version: '0.5.6', restartRequired: true })
    expect(await readdir(path.dirname(result.receipt!))).not.toContain('incomplete.json')
    expect(await readdir(path.dirname(result.receipt!))).not.toContain('lock.json')
    const followup = await h.updater.check(); expect(followup.state).toBe('current'); expect(followup.restartRequired).toBe(true)
    const reloaded = new LocalCheckoutUpdater({ entryURL: h.entryURL, run: h.run, loadedAt: receipt.finishedAt + 1, remoteVersion: async () => '0.5.6' })
    const afterReload = await reloaded.check()
    expect(afterReload.state).toBe('current'); expect(afterReload.restartRequired).toBeUndefined()
    expect(afterReload.text).not.toContain('A completed local update was recorded')
    expect((await h.updater.apply(plan.token)).state).toBe('blocked')
  })

  test('current checkout has no mutation plan', async () => {
    const h = await fixture({ advance: false })
    let metadataCalls = 0
    const updater = new LocalCheckoutUpdater({ entryURL: h.entryURL, run: h.run, remoteVersion: async () => { metadataCalls++; throw new Error('Version metadata unavailable') } })
    const result = await updater.check()
    expect(result.state).toBe('current'); expect(result.plan).toBeUndefined(); expect(result.text).toContain('Version: 0.5.5 → 0.5.5')
    expect(metadataCalls).toBe(0)
  })

  for (const name of ['tracked', 'untracked', 'ignored-secret']) test(`local ${name} work is protected`, async () => {
    const h = await fixture()
    const file = name === 'tracked' ? 'opencode/build.cjs' : name === 'ignored-secret' ? 'private.ignore' : 'private.txt'
    await writeFile(path.join(h.root, file), 'Keep local work')
    const result = await h.updater.check(); expect(result.state).toBe('blocked'); expect(result.text).toContain('local changes or files')
    expect(await readFile(path.join(h.root, file), 'utf8')).toBe('Keep local work')
  })

  test('expected generated artifacts do not block an otherwise clean checkout', async () => {
    const h = await fixture()
    for (const dir of ['node_modules', 'opencode/node_modules', 'opencode/src/shared']) { await mkdir(path.join(h.root, dir), { recursive: true }); await writeFile(path.join(h.root, dir, 'generated'), 'fixture') }
    await mkdir(path.join(h.adapter, 'runtime/bridge'), { recursive: true }); await writeFile(path.join(h.adapter, 'runtime/bridge/trajviz_obs_cli.py'), '# packaged fixture')
    await mkdir(path.join(h.adapter, 'dist'), { recursive: true }); await writeFile(path.join(h.adapter, 'dist/tui.js'), '// packaged fixture')
    await writeFile(path.join(h.adapter, 'src/types.ts'), '// generated')
    expect((await h.updater.check()).state).toBe('available')
  })

  test('ignored provider configuration, host settings, and caches survive a real update', async () => {
    const h = await fixture()
    const files = ['.env', 'opencode/.env.local', '.pytest_cache/state', 'bridge/.venv/state', '.ruff_cache/state', '.DS_Store', 'session.log', 'docs/private.md', '.claude/settings.local.json', '.claude-plugin/types/index.d.ts', '.playwright-mcp/state']
    for (const file of files) { await mkdir(path.dirname(path.join(h.root, file)), { recursive: true }); await writeFile(path.join(h.root, file), `Preserve ${file}`) }
    const plan = await h.plan(), result = await h.updater.apply(plan.token)
    expect(result.state).toBe('updated')
    for (const file of files) expect(await readFile(path.join(h.root, file), 'utf8')).toBe(`Preserve ${file}`)
  })

  test('Git refuses an incoming tracked collision with an ignored provider configuration', async () => {
    const h = await fixture()
    await writeFile(path.join(h.root, '.env'), 'Preserve local credentials')
    await writeFile(path.join(h.publisher, '.env'), 'Remote example')
    await git(h.publisher, 'add', '-f', '.env'); await git(h.publisher, 'commit', '-m', 'Conflicting tracked fixture')
    const plan = await h.plan(), result = await h.updater.apply(plan.token)
    expect(result.state).toBe('error'); expect(result.text).toContain('No rollback was attempted')
    expect(await readFile(path.join(h.root, '.env'), 'utf8')).toBe('Preserve local credentials')
    expect((await git(h.root, 'rev-parse', 'HEAD')).trim()).toBe(h.current)
  })

  for (const folder of ['runtime', 'dist']) test(`private files inside generated ${folder} are protected from the build`, async () => {
    const h = await fixture(), file = path.join(h.adapter, folder, '.env')
    await mkdir(path.dirname(file), { recursive: true }); await writeFile(file, 'Private output-directory config')
    const result = await h.updater.check()
    expect(result.state).toBe('blocked'); expect(result.text).toContain('cannot safely replace')
    expect(await readFile(file, 'utf8')).toBe('Private output-directory config')
  })

  for (const changed of ['branch', 'upstream', 'origin', 'head', 'dirty']) test(`confirmation revalidates ${changed} before mutation`, async () => {
    const h = await fixture(), plan = await h.plan()
    if (changed === 'branch') await git(h.root, 'checkout', '-b', 'coding-work')
    if (changed === 'upstream') await git(h.root, 'branch', '--unset-upstream')
    if (changed === 'origin') await git(h.root, 'remote', 'set-url', 'origin', 'https://github.com:8443/rshu/TrajViz-Obs.git')
    if (changed === 'head') { await writeFile(path.join(h.root, 'work'), 'local commit'); await git(h.root, 'add', '.'); await git(h.root, 'commit', '-m', 'Local work') }
    if (changed === 'dirty') await writeFile(path.join(h.root, 'work'), 'untracked work')
    const result = await h.updater.apply(plan.token)
    expect(result.state).toBe('error'); expect(result.text).toContain('No checkout update was applied')
    expect(h.calls.some(call => call.args.includes('merge'))).toBe(false)
  })

  test('remote advancement invalidates a confirmed target rather than applying an unseen revision', async () => {
    const h = await fixture(), plan = await h.plan()
    await writeFile(path.join(h.publisher, 'readme'), 'New remote work'); await git(h.publisher, 'add', '.'); await git(h.publisher, 'commit', '-m', 'Advance again')
    expect((await h.updater.apply(plan.token)).text).toContain('Remote main changed')
    expect((await git(h.root, 'rev-parse', 'HEAD')).trim()).toBe(h.current)
  })

  test('a remote rewrite is never reset into the checkout', async () => {
    const h = await fixture(), plan = await h.plan()
    await git(h.publisher, 'checkout', '--orphan', 'replacement'); await git(h.publisher, 'add', '.'); await git(h.publisher, 'commit', '-m', 'Unrelated history'); await git(h.publisher, 'branch', '-M', 'main')
    const result = await h.updater.apply(plan.token)
    expect(result.state).toBe('error'); expect((await git(h.root, 'rev-parse', 'HEAD')).trim()).toBe(h.current)
    const refreshed = await h.updater.check()
    expect(refreshed.state).toBe('available')
    expect((await h.updater.apply(refreshed.plan!.token)).state).toBe('error')
    expect((await git(h.root, 'rev-parse', 'HEAD')).trim()).toBe(h.current)
    expect(h.calls.some(call => call.args.includes('reset'))).toBe(false)
  })

  test('linked metadata and symlinked entrypoints are rejected', async () => {
    const h = await fixture(), alias = path.join(h.directory, 'alias.ts')
    await symlink(path.join(h.adapter, 'src/local-update.ts'), alias)
    expect((await new LocalCheckoutUpdater({ entryURL: pathToFileURL(alias).href, run: h.run }).check()).state).toBe('blocked')
    await rm(path.join(h.root, '.git'), { recursive: true }); await writeFile(path.join(h.root, '.git'), 'gitdir: /unrelated')
    expect((await h.updater.check()).text).toContain('Linked worktrees')
  })

  test('plans expire and are consumed even when apply is cancelled', async () => {
    let clock = 1000
    const h = await fixture(), expired = new LocalCheckoutUpdater({ entryURL: h.entryURL, run: h.run, now: () => clock, inlineWorker: true, remoteVersion: async () => 'unknown' })
    const checked = await expired.check(); clock += 300_001
    expect((await expired.apply(checked.plan!.token)).state).toBe('blocked')
    const plan = await h.plan(), controller = new AbortController(); controller.abort()
    expect((await h.updater.apply(plan.token, { signal: controller.signal })).state).toBe('cancelled')
    expect((await h.updater.apply(plan.token)).state).toBe('blocked')
    expect(h.calls.some(call => call.args.includes('merge'))).toBe(false)
  })

  test('cross-instance updates lock the shared checkout until the build completes', async () => {
    const h = await fixture(), second = new LocalCheckoutUpdater({ entryURL: h.entryURL, run: h.run, inlineWorker: true, remoteVersion: async () => '0.5.6' }), firstPlan = await h.plan(), secondPlan = (await second.check()).plan!
    const entered = Promise.withResolvers<void>(), finish = Promise.withResolvers<void>()
    h.intercept(async (command, args) => { if (command === 'bun' && args[0] === 'install') { entered.resolve(); await finish.promise } })
    const first = h.updater.apply(firstPlan.token); await entered.promise
    const concurrent = await second.apply(secondPlan.token)
    expect(concurrent.state).toBe('error'); expect(concurrent.text).toMatch(/incomplete|lock/)
    finish.resolve(); expect((await first).state).toBe('updated')
    expect(h.calls.filter(call => call.command === 'bun' && call.args[0] === 'install')).toHaveLength(1)
  })

  test('cancellation after checkout application does not interrupt installation or build', async () => {
    const h = await fixture(), plan = await h.plan(), controller = new AbortController()
    h.intercept((command, args) => { if (command === 'bun' && args[0] === 'install') controller.abort() })
    const result = await h.updater.apply(plan.token, { signal: controller.signal })
    expect(result.state).toBe('updated')
    for (const call of h.calls.filter(call => call.command === 'bun' && ['install', 'run'].includes(call.args[0]!))) expect(call.signal).toBeUndefined()
  })

  for (const change of ['cancel', 'dirty', 'git-config']) test(`${change} during fetch is checked again before worktree mutation`, async () => {
    const h = await fixture(), plan = await h.plan(), controller = new AbortController()
    h.intercept(async (command, args) => {
      if (command !== 'git' || !args.includes('fetch')) return
      if (change === 'cancel') controller.abort()
      if (change === 'dirty') await writeFile(path.join(h.root, 'coding-work.txt'), 'Do not overwrite')
      if (change === 'git-config') await git(h.root, 'config', 'fixture.changed', 'true')
    })
    const result = await h.updater.apply(plan.token, { signal: controller.signal })
    expect(result.state).toBe(change === 'cancel' ? 'cancelled' : 'error')
    expect((await git(h.root, 'rev-parse', 'HEAD')).trim()).toBe(h.current)
    expect(h.calls.some(call => call.args.includes('merge'))).toBe(false)
    expect(await readdir(path.join(h.root, '.git/trajviz-obs-update'))).not.toContain('incomplete.json')
  })

  test('partial build failure leaves durable recovery state and cannot later report current', async () => {
    const h = await fixture(), plan = await h.plan()
    h.intercept((command, args) => { if (command === 'bun' && args[0] === 'run') throw new Error('Failed https://user:secret@example.test/build?token=private') })
    const result = await h.updater.apply(plan.token)
    expect(result.state).toBe('error'); expect(result.text).toContain('No rollback was attempted')
    expect(result.text).not.toContain('secret'); expect(result.text).not.toContain('private')
    expect(await readdir(path.join(h.root, '.git/trajviz-obs-update'))).toContain('incomplete.json')
    expect((await h.updater.check()).state).toBe('blocked')
    expect(JSON.parse(await readFile(result.receipt!, 'utf8')).state).toBe('error')
    expect((await git(h.root, 'rev-parse', 'HEAD')).trim()).toBe(h.available)
  })

  test('missing build outputs cannot become success', async () => {
    const h = await fixture(), plan = await h.plan()
    h.intercept(async (command, args) => { if (command === 'bun' && args[0] === 'run') await writeFile(path.join(h.adapter, 'build.cjs'), '// intentionally no outputs') })
    const result = await h.updater.apply(plan.token)
    expect(result.state).toBe('error'); expect(result.text).toContain('incomplete')
  })

  test('the process runner captures output, redacts credentials, bounds output, and supports preflight cancellation', async () => {
    const directory = await mkdtemp(path.join(tmpdir(), 'trajviz-update-runner-')); directories.push(directory)
    await expect(runLocalUpdateCommand('git', ['not-a-real-command'], directory)).rejects.toThrow('git failed')
    const controller = new AbortController(); controller.abort()
    await expect(runLocalUpdateCommand('git', ['--version'], directory, { signal: controller.signal })).rejects.toThrow()
    expect(await runLocalUpdateCommand('git', ['--version'], directory)).toContain('git version')
    await expect(runLocalUpdateCommand('bun', ['--eval', 'process.stdout.write("x".repeat(300000))'], directory)).rejects.toThrow('output exceeded')
    await expect(runLocalUpdateCommand('bun', ['--eval', 'setInterval(()=>{},1000)'], directory, { timeout: 30 })).rejects.toThrow('time limit')
    try {
      await runLocalUpdateCommand('bun', ['--eval', 'console.error("https://user:secret@example.invalid/path?token=private");process.exit(1)'], directory)
      throw new Error('Expected process failure')
    } catch (error) { expect(String(error)).toContain('[redacted]'); expect(String(error)).not.toMatch(/secret|private/) }
  })

  test('Windows timeout cleanup targets the owned process tree with literal arguments', () => {
    expect(updateProcessTreeCommand(12345, 'win32')).toEqual({ command: 'taskkill', args: ['/pid', '12345', '/t', '/f'] })
    expect(updateProcessTreeCommand(12345, 'darwin')).toBeUndefined()
    for (const pid of [undefined, 0, -1, NaN, 1.5]) expect(updateProcessTreeCommand(pid, 'win32')).toBeUndefined()
  })

  for (const adapter of ['opencode', 'pi'] as const) test(`a real detached ${adapter} snapshot worker survives a replacement observer and publishes a durable result`, async () => {
    const h = await fixture(), entryURL = adapter === 'pi' ? pathToFileURL(path.join(h.root, 'pi/trajviz-obs.ts')).href : h.entryURL
    const updater = new LocalCheckoutUpdater({ adapter, entryURL: `${entryURL}?__opencode_reload=original#old`, remoteVersion: async () => adapter === 'pi' ? '0.1.2' : '0.5.6' })
    const checked = await updater.check(); expect(checked.state).toBe('available')
    expect(checked.plan?.currentVersion).toBe(adapter === 'pi' ? '0.1.1' : '0.5.5')
    const id = randomUUID(), updating = updater.apply(checked.plan!.token, { jobID: id })
    const replacement = new LocalCheckoutUpdater({ adapter, entryURL: `${entryURL}?__opencode_reload=reconnected#new` })
    let observed = await replacement.poll(id)
    for (let attempt = 0; observed.state === 'expired' && attempt < 20; attempt++) { await new Promise(resolve => setTimeout(resolve, 50)); observed = await replacement.poll(id) }
    expect(['pending', 'finished']).toContain(observed.state)
    const result = await updating
    expect(result.state).toBe('updated'); expect((await git(h.root, 'rev-parse', 'HEAD')).trim()).toBe(h.available)
    const durable = await replacement.poll(id)
    expect(durable.state).toBe('finished'); if (durable.state === 'finished') expect(durable.result).toEqual(result)
    if (adapter === 'pi') expect(result.text).toContain('Reload Pi'); else expect(result.text).toContain('Quitting Desktop alone can leave its server running')
  }, 20_000)

  test('host source registration is checked on inspection and again before dispatch', async () => {
    const h = await fixture(); let allowed = true; const directories: string[] = []
    const updater = new LocalCheckoutUpdater({ entryURL: h.entryURL, inlineWorker: true, run: h.run, remoteVersion: async () => 'unknown', validateSource: async directory => { directories.push(directory); if (!allowed) throw new Error('The configured source changed') } })
    const checked = await updater.check(); expect(checked.state).toBe('available'); expect(checked.plan?.availableVersion).toBe('unknown')
    allowed = false
    const result = await updater.apply(checked.plan!.token)
    expect(result.state).toBe('error'); expect(result.text).toContain('configured source changed')
    expect(directories).toEqual([h.adapter, h.adapter]); expect(h.calls.some(call => call.args.includes('merge'))).toBe(false)
  })

  test('pre-apply teardown is reserved for a real current plan', async () => {
    const h = await fixture(); let calls = 0
    const beforeApply = async () => { calls++; throw new Error('Busy observer') }
    expect((await h.updater.apply(randomUUID(), { beforeApply })).state).toBe('blocked'); expect(calls).toBe(0)
    const stale = await h.plan(); await writeFile(path.join(h.root, 'local-work'), 'Preserve')
    expect((await h.updater.apply(stale.token, { beforeApply })).state).toBe('error'); expect(calls).toBe(0)
    await rm(path.join(h.root, 'local-work'))
    const valid = await h.plan()
    const result = await h.updater.apply(valid.token, { beforeApply })
    expect(result.state).toBe('error'); expect(result.text).toContain('Busy observer'); expect(calls).toBe(1)
    expect((await git(h.root, 'rev-parse', 'HEAD')).trim()).toBe(h.current)
  })

  test('source registration is rechecked after teardown and snapshot writes before dispatch', async () => {
    const h = await fixture(); let allowed = true, checks = 0
    const updater = new LocalCheckoutUpdater({ entryURL: h.entryURL, remoteVersion: async () => '0.5.6', validateSource: async () => { checks++; if (!allowed) throw new Error('The plugin unloaded before dispatch') } })
    const checked = await updater.check(), id = randomUUID()
    const result = await updater.apply(checked.plan!.token, { jobID: id, beforeApply: async () => { allowed = false } })
    expect(checks).toBe(3); expect(result.state).toBe('error'); expect(result.text).toContain('unloaded before dispatch')
    expect((await git(h.root, 'rev-parse', 'HEAD')).trim()).toBe(h.current)
    expect(await readdir(path.join(h.root, '.git/trajviz-obs-update/jobs', id))).not.toContain('started.json')
    const durable = await updater.poll(id)
    expect(durable.state).toBe('finished'); if (durable.state === 'finished') expect(durable.result).toEqual(result)
  })

  test('effective transport rewrites are shown with redaction and full private identity changes invalidate the plan', async () => {
    const h = await fixture(); let effective = 'https://user:secret@mirror.example/repo?token=private'
    const run: typeof runLocalUpdateCommand = async (command, args, cwd, options) => command === 'git' && args.includes('get-url') ? `${effective}\n` : h.run(command, args, cwd, options)
    const updater = new LocalCheckoutUpdater({ entryURL: h.entryURL, run, inlineWorker: true, remoteVersion: async () => '0.5.6' })
    const checked = await updater.check()
    expect(checked.state).toBe('available'); expect(checked.text).toContain('Configured origin: https://github.com/rshu/TrajViz-Obs.git')
    expect(checked.plan?.effectiveRemote).toBe('https://[redacted]@mirror.example/repo?[redacted]')
    expect(JSON.stringify(checked)).not.toMatch(/user:secret|token=private/)
    // A credential-only change has the same redacted display, but a different fingerprint.
    effective = 'https://user:replacement@mirror.example/repo?token=rotated'
    const result = await updater.apply(checked.plan!.token)
    expect(result.state).toBe('error'); expect(result.text).toContain('changed')
    expect(result.text).not.toMatch(/replacement|rotated/)
    expect(h.calls.some(call => call.args.includes('merge'))).toBe(false)
  })

  test('durable polling rejects unknown IDs and another adapter identity', async () => {
    const h = await fixture(), observer = new LocalCheckoutUpdater({ entryURL: h.entryURL })
    expect((await observer.poll('../escape')).state).toBe('expired')
    expect((await observer.poll(randomUUID())).state).toBe('expired')
  })

  test('Pi source updates run through a real Node host and a standalone Node worker without Bun', async () => {
    const h = await fixture(), driver = path.join(h.directory, 'node-driver.mjs')
    await writeFile(driver, `import { LocalCheckoutUpdater } from ${JSON.stringify(new URL('../../shared/local-update.ts', import.meta.url).href)};
      const updater = new LocalCheckoutUpdater({adapter:'pi',entryURL:${JSON.stringify(pathToFileURL(path.join(h.root, 'pi/trajviz-obs.ts')).href)},remoteVersion:async()=>'0.1.2'});
      const checked=await updater.check(); if(!checked.plan)throw Error(checked.text);
      const result=await updater.apply(checked.plan.token); console.log(JSON.stringify({result,node:process.versions.node,bun:process.versions.bun??null}));`)
    const { stdout } = await promisify(execFile)('node', ['--experimental-strip-types', driver], { timeout: 20_000, maxBuffer: 65536 })
    const native = JSON.parse(stdout)
    expect(native.bun).toBeNull(); expect(native.result.state).toBe('updated'); expect(native.result.text).toContain('0.1.2')
    expect((await git(h.root, 'rev-parse', 'HEAD')).trim()).toBe(h.available)
    expect((await readdir(path.join(h.root, '.git/trajviz-obs-update/jobs'))).length).toBe(1)
  }, 25_000)
})
