import { cp, mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const root = fileURLToPath(new URL('../..', import.meta.url))
const output = path.join(root, 'opencode/runtime/bridge')
await rm(path.dirname(output), { recursive: true, force: true })
await mkdir(output, { recursive: true })
const python = input => { const p = input.split(path.sep).join('/'); return !/(?:^|\/)(?:\.venv|tests|__pycache__|\.pytest_cache|\.ruff_cache)(?:\/|$)/.test(p) && (p.endsWith('.py') || !path.extname(p)) }
await cp(path.join(root, 'bridge/trajviz_obs'), path.join(output, 'trajviz_obs'), { recursive: true, filter: python })
for (const name of ['bootstrap.py', 'trajviz_obs_cli.py', 'telemetry_client.py', 'telemetry_protocol.py', 'prompt_capture.py']) {
  await cp(path.join(root, 'bridge', name), path.join(output, name))
}
await cp(path.join(root, 'LICENSE'), path.join(root, 'opencode/LICENSE'))
const types = await readFile(path.join(root, 'types/index.d.ts'), 'utf8')
await writeFile(path.join(root, 'opencode/src/types.ts'), '// Shared bridge snapshot contract. Generated from types/index.d.ts by the package build.\n' + types.slice(types.indexOf('export type TranscriptInfo'), types.indexOf("export type SessionState")))

await mkdir(path.join(root, 'opencode/src/shared'), { recursive: true })
for (const name of ['args.ts', 'delivery-status.ts', 'telemetry-version.ts']) {
  await cp(path.join(root, 'hooks/lib', name), path.join(root, 'opencode/src/shared', name))
}

await cp(path.join(root, 'shared/command-args.ts'), path.join(root, 'opencode/src/shared/command-args.ts'))
await cp(path.join(root, 'shared/local-update.ts'), path.join(root, 'opencode/src/shared/local-update.ts'))
await cp(path.join(root, 'shared/prompt-capture.ts'), path.join(root, 'opencode/src/shared/prompt-capture.ts'))
const grammar = path.join(root, 'opencode/src/shared/command-args.ts')
await writeFile(grammar, (await readFile(grammar, 'utf8')).replace("'../hooks/lib/args.js'", "'./args.js'"))
