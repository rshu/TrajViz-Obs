import { expect, test } from 'bun:test'
import { access, readFile, readdir } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('..', import.meta.url))

test('package exposes a built legacy plugin and includes its shared Python workers', async () => {
  const pkg = JSON.parse(await readFile(path.join(root, 'package.json'), 'utf8'))
  expect(pkg.exports['.']).toBe('./dist/index.js')
  expect(pkg.main).toBe(pkg.exports['.'])
  expect(pkg.dependencies.zod).toBe('4.1.12')
  expect(pkg.files).toEqual(['dist/index.js', 'runtime/bridge/**/*.py', 'README.md', 'LICENSE', 'scripts/capture-run.mjs'])
  expect(pkg.dependencies['@opentelemetry/api']).toBe('1.9.0')
  expect(pkg.bin['trajviz-deveco-capture']).toBe('./scripts/capture-run.mjs')
  for (const file of ['dist/index.js', 'runtime/bridge/bootstrap.py', 'runtime/bridge/trajviz_obs_cli.py',
    'scripts/capture-run.mjs', 'runtime/bridge/trajviz_obs/deveco_requests.py',
    'runtime/bridge/trajviz_obs/deveco_request_scope.py', 'runtime/bridge/prompt_capture.py', 'runtime/bridge/trajviz_obs/prompt_explorer.py', 'LICENSE']) {
    await access(path.join(root, file))
  }
  const visit = async (folder: string): Promise<void> => {
    for (const entry of await readdir(folder, { withFileTypes: true })) {
      expect(['tests', '__pycache__', '.venv', 'captures']).not.toContain(entry.name)
      const file = path.join(folder, entry.name)
      if (entry.isDirectory()) await visit(file)
      else expect(path.extname(entry.name)).toBe('.py')
    }
  }
  await visit(path.join(root, 'runtime/bridge'))
})
