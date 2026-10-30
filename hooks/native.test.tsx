import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'
import { NATIVE_SNAPSHOT } from './testdata/native-snapshot'
import { patternRows } from './render/pane'

const PYTHON = '/cache/trajviz-obs/envs/0123456789abcdef01234567/0123456789abcdef0123456789abcdef/bin/python'
function host(on: On) {
  const live = { percent: 16, usd: 0.42 }
  mock.env(on, { XDG_CACHE_HOME: '/cache', HOME: '/home/test' })
  mock.clock(on, { now: 1_000_000 })
  on('command.register', () => ({ value: { command: 'obs' } }))
  on('tool.register', () => ({ value: { tool: 'mcp__trajviz-obs__obs' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: 'native' }))
  on('session.cwd', () => ({ value: '/project' }))
  on('session.usage', () => ({ value: { startedAt: 0, context: { window: 200_000, percent: live.percent }, rateLimits: [], cost: { usd: live.usd } } }))
  on('fs.exists', () => ({ value: true }))
  on('fs.read', () => ({ value: JSON.stringify({ revision: 5, python: PYTHON, trajviz: '0.3.1', extras: ['llm', 'charts', 'zstd'], source: 'agent-trajviz==0.3.1', createdAt: 1 }) }))
  on('process.run', () => ({ value: { exitCode: 0, stdout: JSON.stringify(NATIVE_SNAPSHOT), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }))
  return live
}

// Unlike the synthetic layout fixture, this document comes from real bridge
// output. In particular, several pattern views are objects containing lists.
test('native pattern groups preserve clusters, chains and every hotspot group', () => {
  expect(patternRows(NATIVE_SNAPSHOT.patterns.error_clusters)).toHaveLength(1)
  expect(patternRows(NATIVE_SNAPSHOT.patterns.failure_chains)).toHaveLength(1)
  const hotspots = patternRows(NATIVE_SNAPSHOT.patterns.hotspots)
  expect(hotspots).toHaveLength(15)
  expect([...new Set(hotspots.map(r => r.group))]).toEqual(['slowest', 'heaviest', 'lowest_cache_read'])
})

test('real bridge snapshot renders every pane tab on terminal and desktop', async ($, on) => {
  host(on)
  await $.session.start({ cwd: '/project', surface: 'terminal', isInteractive: false })
  await $.command.run({ command: 'obs', args: '', origin: { kind: 'composer' }, presentation: { columns: 80, isFullscreen: false } })
  for (const surface of ['terminal', 'desktop'] as const) {
    for (let tab = 1; tab <= 7; tab++) {
      const ui = await $.ui.mount({ plugin: 'trajviz-obs', surface, component: 'Pane', requestId: 'obs', props: { title: 'obs', isFocused: true, bodyColumns: 80, placement: 'dock', scroll: { offset: 0, bodyRows: 50 }, view: {} } })
      await ui.press({ key: `tab:${tab}` })
      expect(await ui.find({ key: 'refresh' })).toBeDefined()
      if (tab === 4) {
        expect(await ui.find({ type: 'Text', text: 'Error clusters' })).toBeDefined()
        expect(await ui.find({ type: 'Text', text: 'Failure chains' })).toBeDefined()
        expect(await ui.find({ type: 'Text', text: 'Hotspots' })).toBeDefined()
      }
      if (surface === 'desktop') expect(await ui.findAll({ type: 'Raster' })).toHaveLength(0)
      await ui.unmount()
    }
  }
})

test('historical card keeps captured context and cost after engine usage changes', async ($, on) => {
  const live = host(on)
  await $.session.start({ cwd: '/project', surface: 'terminal', isInteractive: false })
  const { text } = await $.command.run({ command: 'obs', args: '', origin: { kind: 'composer' }, presentation: { columns: 80, isFullscreen: false } })
  live.percent = 99
  live.usd = 50
  const ui = await $.ui.mount({ plugin: 'trajviz-obs', surface: 'terminal', component: 'CommandOutput', requestId: 'native-card', props: { command: 'obs', args: '', text: text ?? '', isErrored: false }, viewport: { columns: 120, rows: 50, isFullscreen: true } })
  expect(await ui.find({ type: 'Text', text: /^16%$/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^\$0\.42$/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^99%$/ })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: /^\$50\.00$/ })).toBeUndefined()
  await ui.unmount()
})
