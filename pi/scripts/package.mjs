import { cp, mkdir, rm } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const root = fileURLToPath(new URL('../..', import.meta.url))
const output = path.join(root, 'pi/runtime/bridge')
await rm(path.dirname(output), { recursive: true, force: true })
await mkdir(output, { recursive: true })
const python = input => {
  const p = input.split(path.sep).join('/')
  return !/(?:^|\/)(?:\.venv|tests|__pycache__|\.pytest_cache|\.ruff_cache)(?:\/|$)/.test(p)
    && (p.endsWith('.py') || !path.extname(p))
}
await cp(path.join(root, 'bridge/trajviz_obs'), path.join(output, 'trajviz_obs'), { recursive: true, filter: python })
for (const name of ['bootstrap.py', 'trajviz_obs_cli.py', 'telemetry_client.py', 'telemetry_protocol.py']) {
  await cp(path.join(root, 'bridge', name), path.join(output, name))
}
await cp(path.join(root, 'LICENSE'), path.join(root, 'pi/LICENSE'))
