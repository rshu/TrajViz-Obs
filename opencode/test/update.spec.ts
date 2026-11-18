import { describe, expect, mock, test } from 'bun:test'
import type { OpenCodeClient, PluginInfo } from '@opencode/client'
import manifest from '../package.json'
import { pinnedTarget, updatePlugin } from '../src/update.js'

function harness() {
  const info: PluginInfo = { id: 'trajviz-obs', source: { type: 'package', target: 'trajviz-obs-opencode@latest', version: '0.5.4', outdated: true }, features: { server: true, tui: true, gui: true, rpc: true }, state: { status: 'active' } }
  const plugin = {
    list: mock(async (_input: unknown, _options?: unknown) => ({ data: [structuredClone(info)] })),
    check: mock(async (_input: unknown, _options?: unknown) => ({ data: [structuredClone(info)] })),
    update: mock(async (_input: unknown, _options?: unknown) => { if (info.source.type === 'package') { info.source.version = 'controlled-updated'; delete info.source.outdated } }),
  }
  const server = { info: mock(async (_options?: unknown) => ({ version: '2.2.5', urls: ['https://private:secret@server.example:4096/path?token=hidden'] })) }
  const client = { plugin, server } as unknown as OpenCodeClient
  const controller = new AbortController(), progress = mock((_text: string) => {}), confirm = mock(async (_question: { title: string; message: string }, _signal: AbortSignal) => true)
  const options = { check: false, location: { directory: '/remote/project' }, signal: controller.signal, confirm, progress, current: () => true }
  return { info, plugin, server, client, controller, progress, confirm, options, run: () => updatePlugin(client, options) }
}

describe('OpenCode native package updates', () => {
  test('only the exact configured target and connected-server location are updated', async () => {
    const h = harness()
    const result = await h.run()
    expect(result.state).toBe('updated')
    expect(h.plugin.check.mock.calls[0]?.[0]).toEqual({ target: 'trajviz-obs-opencode@latest', location: { directory: '/remote/project' } })
    expect(h.plugin.update.mock.calls).toHaveLength(1)
    expect(h.plugin.update.mock.calls[0]).toEqual([{ targets: ['trajviz-obs-opencode@latest'], location: { directory: '/remote/project' } }, { signal: h.controller.signal }])
    expect(result.text).toContain('installed controlled-updated')
    expect(h.confirm.mock.calls[0]?.[0].message).toContain('other sessions or clients')
    expect(h.confirm.mock.calls[0]?.[0].message).toContain('https://server.example:4096')
    expect(h.confirm.mock.calls[0]?.[0].message).not.toContain('secret')
    expect(h.confirm.mock.calls[0]?.[0].message).not.toContain('token=')
    expect(h.progress.mock.calls.at(-1)?.[0]).toContain('host may reload')
  })

  test('check-only does not ask for confirmation or mutate the installation', async () => {
    const h = harness(); h.options.check = true
    const result = await h.run()
    expect(result.state).toBe('available'); expect(result.text).toContain('installed 0.5.4')
    expect(h.plugin.check).toHaveBeenCalledTimes(1)
    expect(h.plugin.update).not.toHaveBeenCalled(); expect(h.confirm).not.toHaveBeenCalled()
  })

  test('a successful native check without outdated reports the native result rather than inventing a latest version', async () => {
    const h = harness()
    if (h.info.source.type === 'package') { delete h.info.source.outdated; delete h.info.source.version }
    const result = await h.run()
    expect(result.state).toBe('current')
    expect(result.text).toContain('OpenCode reports no available update')
    expect(result.text).toContain('version unknown')
    expect(h.plugin.update).not.toHaveBeenCalled(); expect(h.confirm).not.toHaveBeenCalled()
  })

  test('local checkout returns safely quoted server-side build instructions without fetching or updating', async () => {
    const h = harness(); h.info.source = { type: 'local', path: "/server/owner's checkout/opencode" }
    const result = await h.run()
    expect(result.state).toBe('manual')
    expect(result.text).toContain(`Loaded UI version: ${manifest.version}`)
    expect(result.text).toContain(`cd '/server/owner'"'"'s checkout/opencode'`)
    expect(result.text).toContain('git pull --ff-only\nbun install --frozen-lockfile\nbun run build')
    expect(result.text).toContain('remote server must be restarted on its own machine')
    expect(h.plugin.check).not.toHaveBeenCalled(); expect(h.plugin.update).not.toHaveBeenCalled(); expect(h.confirm).not.toHaveBeenCalled()
  })

  test('Windows local instructions use a literal PowerShell directory and unsafe path controls never become shell commands', async () => {
    const h = harness(); h.info.source = { type: 'local', path: "C:\\owner's folder\\opencode" }
    expect((await h.run()).text).toContain("Set-Location -LiteralPath 'C:\\owner''s folder\\opencode'")
    h.info.source = { type: 'local', path: '/private/new\nline' }
    const result = await h.run()
    expect(result.text).not.toContain('cd '); expect(result.text).not.toContain('/private/new')
  })

  test('exact versions and complete Git commits stay pinned', async () => {
    const targets = ['trajviz-obs-opencode@0.5.4', '@owner/obs@1.2.3-beta.1', 'npm:@owner/obs@1.2.3', 'github:rshu/TrajViz-Obs#' + 'a'.repeat(40) + '::path:opencode', 'git+https://example.com/obs.git#' + 'b'.repeat(64)]
    for (const target of targets) {
      expect(pinnedTarget(target)).toBe(true)
      const h = harness(); h.info.source = { type: 'package', target, version: 'controlled' }
      expect((await h.run()).state).toBe('pinned')
      expect(h.plugin.check).not.toHaveBeenCalled(); expect(h.plugin.update).not.toHaveBeenCalled()
    }
    for (const target of ['trajviz-obs-opencode', 'trajviz-obs-opencode@latest', '@owner/obs@^1.2.3', 'github:rshu/TrajViz-Obs#main::path:opencode']) expect(pinnedTarget(target)).toBe(false)
  })

  test('declining confirmation leaves the package unchanged', async () => {
    const h = harness(); h.confirm.mockResolvedValue(false)
    const result = await h.run()
    expect(result.state).toBe('cancelled'); expect(result.text).toContain('before dispatch')
    expect(h.plugin.update).not.toHaveBeenCalled()
  })

  test('credentialized package targets remain exact API arguments but cannot leak through progress or confirmation', async () => {
    const h = harness(), target = 'git+https://private:secret@repository.example/obs.git?token=hidden#main'
    h.info.source = { type: 'package', target, version: 'controlled', outdated: true }
    const result = await h.run()
    expect(result.state).toBe('updated')
    expect(h.plugin.update.mock.calls[0]?.[0]).toMatchObject({ targets: [target] })
    const rendered = JSON.stringify([result.text, h.confirm.mock.calls, h.progress.mock.calls])
    expect(rendered).not.toContain('secret'); expect(rendered).not.toContain('hidden')
  })

  test('cancelling an open confirmation cannot dispatch a later update', async () => {
    const h = harness(), opened = Promise.withResolvers<void>(), answer = Promise.withResolvers<boolean>()
    h.confirm.mockImplementation(() => { opened.resolve(); return answer.promise })
    const running = h.run(); await opened.promise; h.controller.abort()
    const result = await running
    expect(result.state).toBe('cancelled'); expect(result.text).toContain('No package was changed')
    answer.resolve(true); await Promise.resolve()
    expect(h.plugin.update).not.toHaveBeenCalled()
  })

  test('cancelling a dispatched update reports an unknown completion instead of claiming rollback', async () => {
    const h = harness(), dispatched = Promise.withResolvers<void>()
    h.plugin.update.mockImplementation(async (_input, options) => {
      dispatched.resolve()
      await new Promise<void>((_resolve, reject) => (options as { signal: AbortSignal }).signal.addEventListener('abort', () => reject(new Error('Request aborted')), { once: true }))
    })
    const running = h.run(); await dispatched.promise; h.controller.abort()
    const result = await running
    expect(result.state).toBe('cancelled'); expect(result.text).toContain('server may still finish')
    expect(result.text).not.toContain('No package was changed')
    expect(h.plugin.update).toHaveBeenCalledTimes(1)
  })

  test('check failure and missing metadata never become an up-to-date or successful update result', async () => {
    const h = harness(); h.plugin.check.mockRejectedValueOnce(new Error('Controlled check failure'))
    expect((await h.run()).text).toContain('Controlled check failure')
    h.plugin.check.mockResolvedValueOnce({ data: [] })
    const result = await h.run()
    expect(result.state).toBe('error'); expect(result.text).toContain('did not return update information')
    expect(h.plugin.update).not.toHaveBeenCalled()
  })

  test('a failed plugin and another updater are surfaced before mutation', async () => {
    const h = harness(); h.info.state = { status: 'failed', error: 'Controlled load failure' }
    expect((await h.run()).text).toContain('Controlled load failure')
    h.info.state = { status: 'active' }
    if (h.info.source.type === 'package') h.info.source.updating = true
    expect((await h.run()).text).toContain('already updating')
    expect(h.plugin.update).not.toHaveBeenCalled()
  })

  test('source changes during check or confirmation cannot update the new target', async () => {
    const h = harness()
    h.confirm.mockImplementation(async () => { h.info.source = { type: 'package', target: 'other-package@latest', version: '0.5.4', outdated: true }; return true })
    expect((await h.run()).text).toContain('changed while confirmation')
    expect(h.plugin.update).not.toHaveBeenCalled()
    const other = harness()
    other.plugin.check.mockImplementation(async () => { const checked = structuredClone(other.info); other.info.source = { type: 'local', path: '/new/source' }; return { data: [checked] } })
    expect((await other.run()).text).toContain('changed during the check')
    expect(other.plugin.update).not.toHaveBeenCalled()
  })

  test('a reconnected client cannot apply a confirmation made for the previous connection', async () => {
    const h = harness(); let current = true
    h.options.current = () => current
    h.confirm.mockImplementation(async () => { current = false; return true })
    expect((await h.run()).text).toContain('server reconnected')
    expect(h.plugin.update).not.toHaveBeenCalled()
  })

  test('a project switch while confirming cannot move the update to a different location', async () => {
    const h = harness()
    h.confirm.mockImplementation(async () => { h.options.location.directory = '/different/project'; return true })
    expect((await h.run()).state).toBe('updated')
    for (const [input] of [...h.plugin.list.mock.calls, ...h.plugin.check.mock.calls, ...h.plugin.update.mock.calls]) expect(input).toMatchObject({ location: { directory: '/remote/project' } })
  })

  test('lost update acknowledgements never trigger an automatic retry', async () => {
    const h = harness(); h.plugin.update.mockRejectedValueOnce(new Error('Connection lost'))
    const result = await h.run()
    expect(result.state).toBe('error'); expect(result.text).toContain('server may still finish')
    expect(h.plugin.update).toHaveBeenCalledTimes(1)
  })

  test('a native no-op or a failed reloaded plugin never claims an update succeeded', async () => {
    const h = harness(); h.plugin.update.mockImplementation(async () => {})
    const noOp = await h.run()
    expect(noOp.state).toBe('error'); expect(noOp.text).toContain('server may still finish')
    h.plugin.update.mockImplementation(async () => { h.info.state = { status: 'failed', error: 'Controlled reload failure' } })
    const failed = await h.run()
    expect(failed.state).toBe('error'); expect(failed.text).toContain('Controlled reload failure')
  }, 15000)

  test('verification waits for a delayed native reload without dispatching an update twice', async () => {
    const h = harness(); let updated = false, observations = 0
    h.plugin.update.mockImplementation(async () => { updated = true })
    h.plugin.list.mockImplementation(async () => {
      if (updated && ++observations >= 3 && h.info.source.type === 'package') { delete h.info.source.outdated; h.info.source.version = 'controlled-reloaded' }
      return { data: [structuredClone(h.info)] }
    })
    expect((await h.run()).text).toContain('installed controlled-reloaded')
    expect(observations).toBe(3); expect(h.plugin.update).toHaveBeenCalledTimes(1)
  })

  test('ambiguous or absent installations and overlapping native requests are refused', async () => {
    const h = harness(); h.plugin.list.mockResolvedValueOnce({ data: [] })
    expect((await h.run()).state).toBe('error')
    h.plugin.list.mockResolvedValueOnce({ data: [h.info, h.info] })
    expect((await h.run()).text).toContain('More than one')
    const opened = Promise.withResolvers<void>(), answer = Promise.withResolvers<boolean>()
    h.confirm.mockImplementation(() => { opened.resolve(); return answer.promise })
    const first = h.run(); await opened.promise
    expect((await h.run()).text).toContain('already running')
    answer.resolve(false); await first
    expect(h.plugin.update).not.toHaveBeenCalled()
  })
})
