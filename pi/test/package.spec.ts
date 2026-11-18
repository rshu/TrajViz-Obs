import { afterAll, beforeAll, describe, expect, test } from 'bun:test'
import { execFileSync } from 'node:child_process'
import { cp, mkdir, mkdtemp, readFile, readdir, realpath, rm, stat, symlink } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const repository = fileURLToPath(new URL('../..', import.meta.url))
const node = Bun.which('node')
const npm = Bun.which('npm')
let root: string, source: string, standalone: string, sdk: string, archive: string
let files: { path: string; size: number }[]
let distribution: Promise<void> | undefined

beforeAll(async () => {
  if (!node || !npm) throw new Error('Package tests require Node.js and npm, as well as Bun.')
  root = await mkdtemp(path.join(tmpdir(), 'trajviz-pi-package-'))
  source = path.join(root, 'source')
  standalone = path.join(root, 'standalone')
  sdk = await realpath(fileURLToPath(import.meta.resolve('@earendil-works/pi-coding-agent')))
  await mkdir(source)
  const copySource = (input: string) => {
    const name = input.split(path.sep).join('/')
    return !/(?:^|\/)(?:node_modules|dist|runtime|tests|test|\.venv|__pycache__|\.pytest_cache|\.ruff_cache)(?:\/|$)/.test(name)
      && (path.extname(input) === '' || /\.(?:ts|py|json|mjs|md)$/.test(input))
  }
  for (const dir of ['pi/src', 'pi/scripts', 'hooks/lib', 'opencode/src', 'shared', 'types', 'bridge']) {
    await cp(path.join(repository, dir), path.join(source, dir), { recursive: true, filter: copySource })
  }
  for (const file of ['package.json', 'LICENSE', 'pi/package.json', 'pi/README.md', 'pi/trajviz-obs.ts']) {
    await cp(path.join(repository, file), path.join(source, file))
  }
}, 30_000)
afterAll(async () => { if (root) await rm(root, { recursive: true, force: true }) })

type Loaded = { node: string; version: string; commands: string[]; completion: string[]; tools: number; errors: unknown[]; notifications: string[]; entries: number; sessionFile?: string }
function loadWithNode(packageRoot: string, options: { direct?: boolean; telemetry?: boolean } = {}): Loaded {
  const code = `
    import fs from 'node:fs/promises';
    import path from 'node:path';
    import {pathToFileURL} from 'node:url';
    const sdk = await import(${JSON.stringify(pathToFileURL(sdk).href)});
    const root = ${JSON.stringify(packageRoot)};
    const agent = path.join(${JSON.stringify(root)}, 'agent-' + (root.endsWith('standalone') ? 'package' : 'source'));
    await fs.mkdir(agent,{recursive:true});
    process.env.PI_CODING_AGENT_DIR=agent;
    process.env.TRAJVIZ_OBS_TELEMETRY='0';
    process.env.TRAJVIZ_OBS_TELEMETRY_STATE_DIR=path.join(agent,'telemetry');
    ${options.direct ? `const module = await import(pathToFileURL(path.join(root, 'dist/trajviz-obs.js')).href);if(typeof module.default!=='function')throw new Error('Missing default extension export');` : ''}
    const loaded=await sdk.discoverAndLoadExtensions([root],agent,agent,sdk.createEventBus());
    const extension=loaded.extensions[0];
    const notifications=[];
    const manager=sdk.SessionManager.inMemory(agent,{id:'package-smoke'});
    const ctx={cwd:agent,mode:'rpc',hasUI:true,sessionManager:manager,getContextUsage:()=>undefined,ui:{notify:(value)=>notifications.push(value)}};
    if(extension){
      for(const handler of extension.handlers.get('session_start')??[])await handler({type:'session_start',reason:'startup'},ctx);
      const command=extension.commands.get('obs');
      await command.handler('help',ctx);
      ${options.telemetry ? `await command.handler('telemetry',ctx);` : ''}
      for(const handler of extension.handlers.get('session_shutdown')??[])await handler({type:'session_shutdown',reason:'quit'},ctx);
    }
    console.log(JSON.stringify({node:process.release.name,version:process.versions.node,errors:loaded.errors,
      commands:extension?[...extension.commands.keys()]:[],completion:extension?(await extension.commands.get('obs').getArgumentCompletions('dash')).map(row=>row.value):[],
      tools:extension?.tools.size??0,notifications,entries:manager.getEntries().length,sessionFile:manager.getSessionFile()}));
  `
  return JSON.parse(execFileSync(node!, ['--input-type=module', '--eval', code], {
    cwd: root, encoding: 'utf8', timeout: 30_000, maxBuffer: 1024 * 1024,
    env: { ...process.env, NO_COLOR: '1' },
  }))
}

function prepareDistribution(): Promise<void> {
  return distribution ??= (async () => {
    const build = path.join(root, 'build')
    await cp(source, build, { recursive: true })
    const packageRoot = path.join(build, 'pi')
    execFileSync(node!, ['scripts/package.mjs'], { cwd: packageRoot, encoding: 'utf8', timeout: 30_000 })
    execFileSync(process.execPath, ['build', 'src/index.ts', '--target=node', '--external', '@earendil-works/*', '--outfile', 'dist/trajviz-obs.js'], { cwd: packageRoot, encoding: 'utf8', timeout: 30_000 })
    const archives = path.join(root, 'archives')
    await mkdir(archives)
    const env = { ...process.env, npm_config_cache: path.join(root, 'npm-cache'), npm_config_update_notifier: 'false', npm_config_ignore_scripts: 'true' }
    const packed = JSON.parse(execFileSync(npm!, ['pack', '--json', '--ignore-scripts', '--pack-destination', archives], { cwd: packageRoot, env, encoding: 'utf8', timeout: 30_000 }))
    files = packed[0].files
    archive = path.join(archives, packed[0].filename)
    await mkdir(standalone)
    execFileSync('tar', ['-xzf', archive, '-C', standalone, '--strip-components=1'], { encoding: 'utf8', timeout: 30_000 })
  })()
}

describe('Pi distribution', () => {
  test('a clean root source install loads through the real Node Pi host without generated output or local dependencies', () => {
    expect(existsSync(path.join(source, 'node_modules'))).toBe(false)
    expect(existsSync(path.join(source, 'pi/node_modules'))).toBe(false)
    expect(existsSync(path.join(source, 'pi/dist'))).toBe(false)
    expect(existsSync(path.join(source, 'pi/runtime'))).toBe(false)
    const loaded = loadWithNode(source, { telemetry: true })
    expect(loaded.node).toBe('node')
    expect(Number(loaded.version.split('.')[0])).toBeGreaterThanOrEqual(22)
    expect(loaded.errors).toEqual([])
    expect(loaded.commands).toEqual(['obs'])
    expect(loaded.completion).toEqual(['dashboard'])
    expect(loaded.tools).toBe(0)
    expect(loaded.notifications[0]).toContain('TrajViz for Pi')
    expect(loaded.notifications[1]).toContain('Usage telemetry')
    expect(loaded.entries).toBe(0)
    expect(loaded.sessionFile).toBeUndefined()
  }, 30_000)

  test('the actual npm tarball contains only the extension, bridge and focused package metadata', async () => {
    await prepareDistribution()
    expect((await stat(archive)).size).toBeGreaterThan(0)
    expect(files.map(file => file.path)).toContain('dist/trajviz-obs.js')
    expect(files.map(file => file.path)).toContain('runtime/bridge/trajviz_obs_cli.py')
    expect(files.map(file => file.path)).toContain('runtime/bridge/trajviz_obs/pi.py')
    expect(files.map(file => file.path)).toContain('runtime/bridge/telemetry_client.py')
    for (const file of files) {
      expect(file.path === 'package.json' || file.path === 'README.md' || file.path === 'LICENSE' || file.path === 'dist/trajviz-obs.js' || /^runtime\/bridge\/.+\.py$/.test(file.path)).toBe(true)
      expect(file.path).not.toMatch(/(?:^|\/)(?:node_modules|docs|tests?|src|scripts|__pycache__|\.venv|\.env)(?:\/|$)/)
      expect(file.path).not.toMatch(/(?:^|\/)(?:\.claude|\.claude-plugin|opencode|telemetry)\//)
    }
    expect((await readdir(standalone)).sort()).toEqual(['LICENSE', 'README.md', 'dist', 'package.json', 'runtime'])
    const manifest = JSON.parse(await readFile(path.join(standalone, 'package.json'), 'utf8'))
    expect(manifest.pi.extensions).toEqual(['./dist/trajviz-obs.js'])
    expect(Object.keys(manifest.peerDependencies).sort()).toEqual(['@earendil-works/pi-coding-agent', '@earendil-works/pi-tui'])
    expect(manifest.dependencies).toBeUndefined()
    const built = await readFile(path.join(standalone, 'dist/trajviz-obs.js'), 'utf8')
    expect(built).not.toMatch(/from\s*["'](?:@opencode\/|@opentui\/|claude-code)/)
    expect(built).not.toContain('from "../../')
  }, 60_000)

  test('the extracted standalone tarball loads and executes using only Node and host peer libraries', async () => {
    await prepareDistribution()
    const peerDir = path.join(standalone, 'node_modules/@earendil-works')
    await mkdir(peerDir, { recursive: true })
    await symlink(path.dirname(path.dirname(sdk)), path.join(peerDir, 'pi-coding-agent'), 'dir')
    const tui = fileURLToPath(import.meta.resolve('@earendil-works/pi-tui'))
    await symlink(path.dirname(path.dirname(await realpath(tui))), path.join(peerDir, 'pi-tui'), 'dir')
    const loaded = loadWithNode(standalone, { direct: true, telemetry: true })
    expect(loaded.node).toBe('node')
    expect(loaded.errors).toEqual([])
    expect(loaded.commands).toEqual(['obs'])
    expect(loaded.notifications[0]).toContain('TrajViz for Pi')
    expect(loaded.notifications[1]).toContain('Usage telemetry')
    expect(loaded.entries).toBe(0)
    expect(loaded.sessionFile).toBeUndefined()
  }, 30_000)
})
