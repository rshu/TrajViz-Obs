import { expect, test } from 'bun:test'
import { execFileSync } from 'node:child_process'
import { mkdtemp, rm } from 'node:fs/promises'
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
    expect(names).toContain('runtime/bridge/trajviz_obs/opencode.py')
    expect(names).toContain('LICENSE')
    for (const name of names) expect(name).toMatch(/^(?:src\/.+\.(?:ts|tsx)|runtime\/bridge\/.+\.py|README\.md|LICENSE|package\.json)$/)
  } finally { await rm(cache,{recursive:true,force:true}) }
})
