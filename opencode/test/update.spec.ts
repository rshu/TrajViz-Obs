import { describe, expect, mock, test } from 'bun:test'
import type { OpenCodeClient, PluginInfo } from '@opencode/client'
import manifest from '../package.json'
import type { LocalUpdateResult } from '../../shared/local-update.js'
import { pinnedTarget, updatePlugin } from '../src/update.js'

function harness() {
  const info: PluginInfo = { id: 'trajviz-obs', source: { type: 'package', target: 'trajviz-obs-opencode@latest', version: '0.5.4', outdated: true }, features: { server: true, tui: true, gui: true, rpc: true }, state: { status: 'active' } }
  const plugin = {
    list: mock(async (_input: unknown, _options?: unknown) => ({ data: [structuredClone(info)] })),
    check: mock(async (_input: unknown, _options?: unknown) => ({ data: [structuredClone(info)] })),
    update: mock(async (_input: unknown, _options?: unknown) => { if (info.source.type === 'package') { info.source.version = 'controlled-updated'; delete info.source.outdated } }),
  }
  const server = { info: mock(async (_options?: unknown) => ({ version: '2.2.5', urls: ['https://private:secret@server.example:4096/path?token=hidden'] })) }
  const session = { active: mock(async (_options?: unknown): Promise<Record<string, { type: 'running' }>> => ({})) }
  const local = {
    localUpdateCheck: mock(async (_input: unknown, _options?: unknown): Promise<LocalUpdateResult> => ({
      state: 'available', text: 'Installed: 1.0.0 · aaaa\nAvailable: 1.0.1 · bbbb',
      plan: { token: 'reviewed-local-plan', root: '/server/TrajViz-Obs', branch: 'main', remote: 'https://github.com/rshu/TrajViz-Obs.git', effectiveRemote: '/server/update-mirror', current: 'a'.repeat(40), available: 'b'.repeat(40), version: '1.0.0', currentVersion: '1.0.0', availableVersion: '1.0.1', expiresAt: Date.now() + 60_000, steps: ['Fast-forward the shared checkout', 'bun install --frozen-lockfile', 'bun run build', 'Restart after the build finishes'] },
    })),
    localUpdateStart: mock(async (_input: unknown, _options?: unknown) => ({ accepted: true as const })),
    localUpdatePoll: mock(async (_input: unknown, _options?: unknown): Promise<{ state: 'pending'; progress?: string } | { state: 'finished'; result: LocalUpdateResult } | { state: 'expired' }> => ({ state: 'finished', result: { state: 'updated', restartRequired: true, text: 'Checkout updated and built. Restart the connected server after current work finishes.' } })),
  }
  const client = { plugin, server, session, rpc: () => local } as unknown as OpenCodeClient
  const controller = new AbortController(), progress = mock((_text: string) => {}), confirm = mock(async (_question: { title: string; message: string }, _signal: AbortSignal) => true)
  const options = { check: false, location: { directory: '/remote/project' }, signal: controller.signal, confirm, progress, current: () => true }
  return { info, plugin, server, session, local, client, controller, progress, confirm, options, run: () => updatePlugin(client, options) }
}

describe('OpenCode plugin updates', () => {
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

  test('local check-only uses the server plan without applying or asking for confirmation', async () => {
    const h = harness(); h.info.source = { type: 'local', path: "/server/owner's checkout/opencode" }
    h.options.check = true
    const result = await h.run()
    expect(result.state).toBe('available')
    expect(result.text).toContain(`Loaded UI version: ${manifest.version}`)
    expect(result.text).toContain('Installed: 1.0.0'); expect(result.text).toContain('Available: 1.0.1')
    expect(h.local.localUpdateCheck.mock.calls[0]).toEqual([{ sourcePath: "/server/owner's checkout/opencode" }, { location: { directory: '/remote/project' }, signal: h.controller.signal }])
    expect(h.local.localUpdateStart).not.toHaveBeenCalled(); expect(h.session.active).not.toHaveBeenCalled()
    expect(h.plugin.check).not.toHaveBeenCalled(); expect(h.plugin.update).not.toHaveBeenCalled(); expect(h.confirm).not.toHaveBeenCalled()
  })

  test('local updates run one confirmed server job against the exact captured source and location', async () => {
    const h = harness(); h.info.source = { type: 'local', path: '/server/TrajViz-Obs/opencode' }
    h.confirm.mockImplementation(async () => { h.options.location.directory = '/new/project'; return true })
    h.local.localUpdatePoll.mockResolvedValueOnce({ state: 'pending', progress: 'Building the adapter…' })
    const result = await h.run()
    expect(result.state).toBe('updated'); expect(result.restartRequired).toBe(true)
    const [input, options] = h.local.localUpdateStart.mock.calls[0] as [{ sourcePath: string; token: string; runID: string }, unknown]
    expect(input).toMatchObject({ sourcePath: '/server/TrajViz-Obs/opencode', token: 'reviewed-local-plan' })
    expect(input.runID).toMatch(/^[\da-f-]{36}$/)
    expect(options).toEqual({ location: { directory: '/remote/project' }, signal: h.controller.signal })
    for (const [pollInput, pollOptions] of h.local.localUpdatePoll.mock.calls) {
      expect(pollInput).toEqual({ sourcePath: input.sourcePath, runID: input.runID }); expect(pollOptions).toEqual(options)
    }
    const prompt = h.confirm.mock.calls[0]?.[0].message
    for (const expected of ['https://server.example:4096', 'Checkout: /server/TrajViz-Obs', 'Branch: main', 'https://github.com/rshu/TrajViz-Obs.git', 'Transport: /server/update-mirror', 'a'.repeat(40), 'b'.repeat(40), 'bun install --frozen-lockfile', 'bun run build', 'Restart the server only after']) expect(prompt).toContain(expected)
    expect(h.local.localUpdateStart).toHaveBeenCalledTimes(1); expect(h.session.active).toHaveBeenCalledTimes(2)
    expect(h.plugin.update).not.toHaveBeenCalled(); expect(h.plugin.check).not.toHaveBeenCalled()
    expect(h.progress.mock.calls.some(([text]) => text === 'Building the adapter…')).toBe(true)
  })

  test('Windows paths remain exact data, not generated client-side shell commands', async () => {
    const h = harness(); h.info.source = { type: 'local', path: "C:\\owner's folder\\opencode" }
    h.options.check = true
    await h.run()
    expect(h.local.localUpdateCheck.mock.calls[0]?.[0]).toEqual({ sourcePath: "C:\\owner's folder\\opencode" })
    expect(h.local.localUpdateStart).not.toHaveBeenCalled()
  })

  test('blocked/current local checks retain the server explanation and never ask to apply', async () => {
    const h = harness(); h.info.source = { type: 'local', path: '/server/TrajViz-Obs/opencode' }
    h.local.localUpdateCheck.mockResolvedValueOnce({ state: 'blocked', text: 'Save local changes and retry.' })
    expect((await h.run()).text).toContain('Save local changes')
    h.local.localUpdateCheck.mockResolvedValueOnce({ state: 'current', restartRequired: true, text: 'Checkout current; restart the old server.' })
    const result = await h.run()
    expect(result.state).toBe('current'); expect(result.restartRequired).toBe(true)
    expect(h.confirm).not.toHaveBeenCalled(); expect(h.local.localUpdateStart).not.toHaveBeenCalled()
  })

  test('an unavailable or older server gets bounded read-only retries and restart guidance', async () => {
    for (const type of ['rpc.unavailable', 'rpc.method_not_found']) {
      const h = harness(); h.info.source = { type: 'local', path: '/server/TrajViz-Obs/opencode' }
      h.local.localUpdateCheck.mockRejectedValue({ type, message: 'The old server does not know localUpdateCheck.' })
      const result = await h.run()
      expect(result.state).toBe('error'); expect(result.text).toContain('Local update support is unavailable')
      expect(result.text).toContain('matching-version OpenCode CLI'); expect(result.text).toContain('"/Applications/OpenCode.app/Contents/Resources/opencode-cli" service restart')
      expect(result.text).toContain('quitting Desktop alone leaves its service running'); expect(result.text).toContain('Then run /obs update again')
      expect(h.local.localUpdateCheck).toHaveBeenCalledTimes(type === 'rpc.unavailable' ? 4 : 1)
      expect(h.local.localUpdateStart).not.toHaveBeenCalled(); expect(h.confirm).not.toHaveBeenCalled()
    }
  })

  test('cold workspace registration recovers through read-only checks before a single confirmed start', async () => {
    const h = harness(); h.info.source = { type: 'local', path: '/server/TrajViz-Obs/opencode' }
    h.local.localUpdateCheck.mockRejectedValueOnce({ type: 'rpc.unavailable', message: 'Workspace is starting' })
    expect((await h.run()).state).toBe('updated')
    expect(h.local.localUpdateCheck).toHaveBeenCalledTimes(2); expect(h.local.localUpdateStart).toHaveBeenCalledTimes(1)
  })

  test('declining or cancelling the local review never starts the server job', async () => {
    const h = harness(); h.info.source = { type: 'local', path: '/server/TrajViz-Obs/opencode' }
    h.confirm.mockResolvedValueOnce(false)
    expect((await h.run()).text).toContain('No checkout was changed')
    const opened = Promise.withResolvers<void>(), accepted = Promise.withResolvers<boolean>()
    h.confirm.mockImplementation(async () => { opened.resolve(); return accepted.promise })
    const running = h.run(); await opened.promise; h.controller.abort()
    expect((await running).state).toBe('cancelled')
    accepted.resolve(true); await Promise.resolve()
    expect(h.local.localUpdateStart).not.toHaveBeenCalled()
  })

  test('running server tasks before review or starting during review prevent local mutation', async () => {
    for (const alreadyRunning of [true, false]) {
      const h = harness(); h.info.source = { type: 'local', path: '/server/TrajViz-Obs/opencode' }
      if (!alreadyRunning) h.session.active.mockResolvedValueOnce({})
      h.session.active.mockResolvedValue({ ses_other: { type: 'running' } })
      expect((await h.run()).text).toContain('Wait for all running tasks')
      expect(h.local.localUpdateStart).not.toHaveBeenCalled()
      expect(h.confirm.mock.calls).toHaveLength(alreadyRunning ? 0 : 1)
    }
  })

  test('changed native sources, reconnects, and final progress cancellation cannot apply a local plan', async () => {
    for (const changed of ['source', 'connection', 'cancel'] as const) {
      const h = harness(); h.info.source = { type: 'local', path: '/server/TrajViz-Obs/opencode' }
      let current = true; h.options.current = () => current
      h.confirm.mockImplementation(async () => {
        if (changed === 'source') h.info.source = { type: 'local', path: '/server/other/opencode' }
        if (changed === 'connection') current = false
        return true
      })
      h.progress.mockImplementation(text => { if (changed === 'cancel' && text.startsWith('Updating and building')) h.controller.abort() })
      expect((await h.run()).state).toBe(changed === 'cancel' ? 'cancelled' : 'error')
      expect(h.local.localUpdateStart).not.toHaveBeenCalled()
    }
  })

  test('a lost local start acknowledgement is resolved only by polling the same job', async () => {
    const h = harness(); h.info.source = { type: 'local', path: '/server/TrajViz-Obs/opencode' }
    h.local.localUpdateStart.mockRejectedValueOnce(new Error('Acknowledgement lost'))
    expect((await h.run()).state).toBe('updated')
    expect(h.local.localUpdateStart).toHaveBeenCalledTimes(1); expect(h.local.localUpdatePoll).toHaveBeenCalledTimes(1)
    h.local.localUpdateStart.mockRejectedValueOnce(new Error('Acknowledgement lost'))
    h.local.localUpdatePoll.mockResolvedValueOnce({ state: 'expired' })
    expect((await h.run()).text).toContain('do not restart the server or start another update')
    expect(h.local.localUpdateStart).toHaveBeenCalledTimes(2)
  })

  test('cancellation after local dispatch leaves the background build alone and reports uncertain completion', async () => {
    const h = harness(); h.info.source = { type: 'local', path: '/server/TrajViz-Obs/opencode' }
    const entered = Promise.withResolvers<void>()
    h.local.localUpdatePoll.mockImplementation(async (_input, options) => {
      entered.resolve()
      await new Promise<void>((_resolve, reject) => (options as { signal: AbortSignal }).signal.addEventListener('abort', () => reject(new Error('Cancelled')), { once: true }))
      return { state: 'expired' }
    })
    const running = h.run(); await entered.promise; h.controller.abort()
    const result = await running
    expect(result.state).toBe('cancelled'); expect(result.text).toContain('may still be updating or building')
    expect(result.text).not.toContain('No checkout was changed'); expect(h.local.localUpdateStart).toHaveBeenCalledTimes(1)
  })

  test('local plan failures and partial build errors remain explicit without claiming success', async () => {
    const h = harness(); h.info.source = { type: 'local', path: '/server/TrajViz-Obs/opencode' }
    h.local.localUpdateCheck.mockResolvedValueOnce({ state: 'available', text: 'Missing plan' })
    expect((await h.run()).text).toContain('did not provide a reviewed')
    h.local.localUpdateCheck.mockRejectedValueOnce(new Error('Server unavailable'))
    expect((await h.run()).text).toContain('Server unavailable')
    h.local.localUpdatePoll.mockResolvedValueOnce({ state: 'finished', result: { state: 'error', text: 'Build failed; repair before restart.' } })
    const failed = await h.run()
    expect(failed.state).toBe('error'); expect(failed.text).toContain('repair before restart')
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

  test('reconnect or cancellation caused by the final progress update cannot dispatch a mutation', async () => {
    for (const abort of [false, true]) {
      const h = harness(); let current = true
      h.options.current = () => current
      h.progress.mockImplementation(text => { if (text.startsWith('Updating Obs')) { if (abort) h.controller.abort(); else current = false } })
      expect((await h.run()).state).toBe(abort ? 'cancelled' : 'error')
      expect(h.plugin.update).not.toHaveBeenCalled()
    }
  })

  test('missing project scope and a plugin that failed during confirmation remain read-only', async () => {
    const missing = harness(); missing.options.location.directory = ''
    expect((await missing.run()).text).toContain('project location is not ready')
    expect(missing.plugin.list).not.toHaveBeenCalled(); expect(missing.plugin.update).not.toHaveBeenCalled()
    const failed = harness(); failed.confirm.mockImplementation(async () => { failed.info.state = { status: 'failed', error: 'late native failure' }; return true })
    expect((await failed.run()).text).toContain('late native failure')
    expect(failed.plugin.update).not.toHaveBeenCalled()
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
