import { expect, test } from 'bun:test'
import { execFileSync } from 'node:child_process'
import { cp, mkdir, mkdtemp, readFile, rm, symlink } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

test('published package contains only the adapter, shared runtime, license and focused README', async () => {
  const cache = await mkdtemp(path.join(tmpdir(),'trajviz-package-'))
  try {
    const cwd = fileURLToPath(new URL('..', import.meta.url))
    const [packed] = JSON.parse(execFileSync('npm',['--cache',cache,'pack','--dry-run','--json','--ignore-scripts'],{cwd,encoding:'utf8'})) as {files:{path:string}[]}[]
    const names = packed!.files.map(f=>f.path)
    expect(names).toContain('src/server.ts')
    expect(names).toContain('src/tui.tsx')
    expect(names).toContain('src/rpc.ts')
    expect(names).toContain('dist/gui-experimental.cjs')
    expect(names).toContain('dist/tui.js')
    expect(names).toContain('runtime/bridge/trajviz_obs/opencode.py')
    expect(names).toContain('runtime/bridge/telemetry_client.py')
    expect(names).toContain('runtime/bridge/telemetry_protocol.py')
    expect(names).toContain('LICENSE')
    for (const name of names) expect(name).toMatch(/^(?:src\/.+\.(?:ts|tsx)|dist\/(?:gui-experimental\.cjs|tui\.js)|runtime\/bridge\/.+\.py|README\.md|LICENSE|package\.json)$/)
  } finally { await rm(cache,{recursive:true,force:true}) }
})

test('packaged terminal entry loads from node_modules without a consumer JSX configuration', async () => {
  const folder = await mkdtemp(path.join(tmpdir(), 'trajviz-installed-tui-'))
  try {
    const root = fileURLToPath(new URL('..', import.meta.url))
    const installed = path.join(folder, 'node_modules/trajviz-obs-opencode')
    await mkdir(path.join(installed, 'dist'), { recursive: true })
    await cp(path.join(root, 'package.json'), path.join(installed, 'package.json'))
    await cp(path.join(root, 'dist/tui.js'), path.join(installed, 'dist/tui.js'))
    // Use the same host dependencies; bundling another Solid renderer breaks
    // native contexts even when a standalone import appears to succeed.
    for (const name of ['@opencode', '@opentui', 'solid-js', 'zod']) await symlink(path.join(root, 'node_modules', name), path.join(folder, 'node_modules', name))
    const entry = JSON.parse(await readFile(path.join(installed, 'package.json'), 'utf8')).exports['./tui']
    expect(entry).toBe('./dist/tui.js')
    const output = execFileSync(process.execPath, ['--eval', 'const plugin = await import("trajviz-obs-opencode/tui"); if (typeof plugin.default?.setup !== "function") throw new Error("Missing terminal plugin setup"); console.log("installed-tui-ok")'], {
      cwd: folder, encoding: 'utf8', env: { ...process.env, BUN_OPTIONS: '' },
    })
    expect(output.trim()).toBe('installed-tui-ok')
  } finally { await rm(folder, { recursive: true, force: true }) }
})
