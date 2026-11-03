import { expect, test } from 'bun:test'
import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

test('native wrapped pane navigation keeps every control above the session body', () => {
  // Use the production Solid transform in a fresh process, so logic-only tests
  // cannot cache a JSX module before the renderer's compiler is installed.
  const code = `
    import { testRender } from '@opentui/solid'
    import { createStore, produce } from 'solid-js/store'
    import { setup } from './src/tui.tsx'
    import { SNAPSHOT } from '../hooks/testdata/snapshot.ts'
    const frames = []
    for (const width of [120, 80, 60, 32]) {
      let renderPane
      const noop = () => {}
      const panel = { name: 'trajviz-obs', sessionID: 'render-test', width, focused: true,
        focus: noop, close: noop, toggleFullscreen: noop }
      const storage = (key, { initial }) => {
        const value = structuredClone(initial)
        if (key === 'ui-v1') value.snapshots['render-test'] = {
          ...structuredClone(SNAPSHOT), title: 'A long controlled session title stays readable when navigation and titles wrap.',
        }
        const [state, set] = createStore(value)
        return [state, change => set(produce(change))]
      }
      const context = {
        options: { watch: false, statusline: false }, storage: { store: storage, memory: storage },
        client: { rpc: () => ({ events: { on: () => noop } }) },
        data: { listen: () => noop }, theme: { text: { base: 'white', muted: 'gray' } },
        keymap: { layer: () => noop },
        ui: { panel: { close: noop }, slot: input => {
          if (input.append === 'session.panel') renderPane = input.render
          return noop
        } },
      }
      const cleanup = setup(context)
      const view = await testRender(() => renderPane(panel), {
        width, height: 28, screenMode: 'alternate-screen', consoleMode: 'disabled',
      })
      try {
        await view.renderOnce()
        await view.flush()
        const body = view.renderer.root.getChildren()[0]
        const rows = body.getChildren().filter(child => ['obs-pane-tabs', 'obs-pane-actions'].includes(child.id))
        frames.push({ width, frame: view.captureCharFrame(), rows: rows.map(row => ({
          id: row.id, top: row.y, bottom: row.y + row.height,
          children: row.getChildren().map(child => ({ top: child.y, bottom: child.y + child.height })),
        })) })
      } finally { cleanup(); await view.dispose() }
    }
    console.log(JSON.stringify(frames))
  `
  const cwd = fileURLToPath(new URL('..', import.meta.url))
  const output = execFileSync(process.execPath, ['--preload', '@opentui/solid/preload', '--eval', code], {
    cwd, encoding: 'utf8', timeout: 30_000,
  })
  const frames = JSON.parse(output) as { width: number; frame: string; rows: {
    id: string; top: number; bottom: number; children: { top: number; bottom: number }[];
  }[] }[]
  expect(frames.map(frame => frame.width)).toEqual([120, 80, 60, 32])
  for (const frame of frames) {
    expect(frame.rows.map(row => row.id)).toEqual(['obs-pane-tabs', 'obs-pane-actions'])
    for (const row of frame.rows) for (const child of row.children) {
      expect(child.top).toBeGreaterThanOrEqual(row.top)
      expect(child.bottom).toBeLessThanOrEqual(row.bottom)
    }
    expect(frame.rows[0]!.bottom).toBeLessThanOrEqual(frame.rows[1]!.top)
    expect(frame.frame).toContain('[x Close]')
    expect(frame.frame).toContain('Current session')
    expect(frame.frame.replace(/\s/g, '')).toContain('A long controlled session title stays readable when navigation and titles wrap.'.replace(/\s/g, ''))
    expect(frame.frame).toContain('↑↓ scroll')
    expect(frame.frame).not.toContain('Unable to draw')
  }
})
