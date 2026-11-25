import type { Register } from 'claude-code'
import { parseArgs } from './lib/args'
import { safeRenderText } from './lib/format'
import { resetServers, stopAll } from './lib/servers'
import { rememberTranscript } from './lib/session'
import { isEmpty, parseSnapshotId } from './lib/snapshot'
import { finishTelemetry, reportTelemetry, setTelemetrySurface, telemetryConsentAtStart } from './lib/telemetry'
import { capturePromptObservation, capturePromptRequest, createPromptCapture, finishPromptCapture, promptRequestCorrelation } from './lib/prompt-capture'
import { OBS_TOOL, serveTool } from './lib/tool'
import { onTurnComplete, resetStatusLine } from './lib/watch'
import { readDashboards, readFlags, readLatest, readPaneTab, readSession, readSnapshotMember, resetRefreshLock, seedFlags } from './lib/state'
import { commandFailure, dispatch } from './commands/index'
import { cardColumns, renderCard, showsButtons } from './render/card'
import { paneUsage, renderPane, renderPaneFailure } from './render/pane'

export const register: Register = (on, options) => {
  let started = false
  let promptCapture = createPromptCapture(options)

  // Observe prompt components without changing their text or claiming an agent/request scope.
  on('prompt.compose', async ($, e, next) => {
    const capture = promptCapture
    const result = await next(e)
    await capturePromptObservation($, capture, { stage: 'component.compose', model: { id: e.model }, data: { input: e, output: result, linkage: 'unlinked' } })
    return result
  })
  on('prompt.section', async ($, e, next) => {
    const capture = promptCapture
    const result = await next(e)
    await capturePromptObservation($, capture, { stage: 'component.section', data: { input: e, output: result, linkage: 'unlinked' } })
    return result
  })
  on('prompt.context', async ($, e, next) => {
    const capture = promptCapture
    const result = await next(e)
    await capturePromptObservation($, capture, { stage: 'component.context', data: { input: e, output: result, linkage: 'unlinked' } })
    return result
  })
  on('prompt.attachment', async ($, e, next) => {
    const capture = promptCapture
    const result = await next(e)
    await capturePromptObservation($, capture, { stage: 'component.attachment',
      correlation: { agent_id: e.agentId ?? null, scope: e.agentId ? 'agent' : 'main' }, data: { input: e, output: result, linkage: 'request_unlinked' } })
    return result
  })
  on('skill.prompt', async ($, e, next) => {
    const capture = promptCapture
    const result = await next(e)
    await capturePromptObservation($, capture, { stage: 'component.skill', data: { input: e, output: result, linkage: 'unlinked' } })
    return result
  })
  on('tool.describe', async ($, e, next) => {
    const capture = promptCapture
    const result = await next(e)
    await capturePromptObservation($, capture, { stage: 'component.tool', data: { input: e, output: result, linkage: 'unlinked' } })
    return result
  })
  on('turn.step', async function* ($, e, next) {
    const capture = promptCapture
    await capturePromptRequest($, capture, e)
    let completed = false
    try {
      const result = yield* next(e)
      completed = true
      await capturePromptObservation($, capture, { stage: 'request.complete', kind: 'primary', correlation: promptRequestCorrelation(e),
        data: { usage: result.usage, stop_reason: result.stopReason, outcome: result.stopReason === null ? 'unavailable' : 'success' } })
      return result
    } catch (error) {
      completed = true
      await capturePromptObservation($, capture, { stage: 'request.complete', kind: 'primary', correlation: promptRequestCorrelation(e),
        data: { outcome: error instanceof Error && error.name === 'AbortError' ? 'cancelled' : 'error' } })
      throw error
    } finally {
      if (!completed) await capturePromptObservation($, capture, { stage: 'request.complete', kind: 'primary', correlation: promptRequestCorrelation(e), data: { outcome: 'cancelled' } })
    }
  })
  on('session.start', async ($, e, next) => {
    if (promptCapture.stopped) promptCapture = createPromptCapture(options)
    // Session state survives a reload; the old environment's operations do
    // not. A duplicate start in this live environment must retain its locks.
    if (!started) {
      started = true
      await resetRefreshLock($)
    }
    await $.command.register({
      name: 'obs',
      description: "Visualize and diagnose this session's trajectory with TrajViz",
      argumentHint: '[overview|dashboard|pane|issues|health|analyze|fixes|json|update|help] [--session id]',
      immediate: true,
    })
    // --tools '' and some hosts deliberately disable model tools. The slash
    // command and live UI still work, so optional registration cannot abort
    // their flags and dashboard initialization.
    try {
      await $.tool.register(OBS_TOOL)
    } catch (error) {
      $.ui.log(`obs model tool unavailable: ${error instanceof Error ? error.message : String(error)}`, { to: 'debug' })
    }
    const previouslyEnabled = (await readFlags($)).statusline
    await seedFlags($, options, e.isInteractive)
    // Usage may be delayed; a status draw must not hold up plugin startup.
    void resetStatusLine($, previouslyEnabled).catch(error => {
      $.ui.log(`obs statusline: ${error instanceof Error ? error.message : String(error)}`, { to: 'debug' })
    })
    // A restarted function environment (a config change or source reload)
    // has lost its previous children, so the dashboard state follows.
    await resetServers($)
    setTelemetrySurface(e.surface, e.isInteractive)
    void reportTelemetry($, options, 'activation', 'success', 0)
    return next(e)
  })

  on('command.run', { command: 'obs' }, async ($, e) => {
    try {
      const personRequested = e.origin?.kind === 'composer' || e.origin?.kind === 'bridge'
      return await dispatch($, { args: e.args, parsed: parseArgs(e.args), options, presentation: e.presentation, personRequested })
    } catch (error) {
      return { text: `obs: ${error instanceof Error ? error.message : String(error)}` }
    }
  }).catch((_$, e, next) => commandFailure(e.args, next.error))

  // The obs tool, answered here: the model's call never reaches a server.
  on('tool.call', { tool: 'mcp__trajviz-obs__obs' }, ($, e) => serveTool($, options, e))

  // The overview card: the text output row drawn from the snapshot its
  // provenance line names (never the latest). An error row carries no id,
  // and a pruned member is gone: both leave the row to the engine.
  on('ui.render', { component: 'CommandOutput', props: { command: 'obs' } }, async ($, e, next) => {
    const id = parseSnapshotId(e.props.text)
    if (id === null) return next(e)
    const snap = await readSnapshotMember($, id)
    if (snap === null || isEmpty(snap)) return next(e)
    return safeRenderText(renderCard($, e, snap, { options, showButtons: showsButtons(e), columns: cardColumns(e), usage: snap.engine_usage ?? null }))
  })

  // The live pane (`$.ui.open({ id: 'obs' })`): the latest snapshot on the
  // open tab. Each state read subscribes the pane, so a new snapshot, a tab
  // press or a flag change redraws it.
  on('ui.render', { component: 'Pane', requestId: 'obs' }, async ($, e) => {
    const snap = await readLatest($)
    const tab = await readPaneTab($)
    const flags = await readFlags($)
    const dashboards = await readDashboards($)
    const selected = (await readSession($)).selected
    const capturedUsage = snap !== null && !isEmpty(snap) ? snap.engine_usage ?? null : null
    return safeRenderText(renderPane($, e, snap, tab, flags, dashboards, { options, usage: selected ? null : await paneUsage($, tab, capturedUsage) }))
  }).catch(($, e, next) => safeRenderText(renderPaneFailure($, e, next.error, options)))

  // Slash commands report their own close. The host raises this separately
  // for its native close mark and keyboard shortcut.
  on('ui.close', { id: 'obs', origin: { kind: 'person' } }, async ($, e, next) => {
    const startedAt = Date.now()
    const consentAtStart = telemetryConsentAtStart($)
    const closed = await next(e)
    void reportTelemetry($, options, 'pane.close', closed.deny === undefined ? 'success' : 'error', Date.now() - startedAt, consentAtStart)
    return closed
  })

  // Live refresh: after a main-loop turn (`e.agentId` absent), one more
  // snapshot for the open pane or the status line, detached from the
  // dispatch so the answer is never held for it.
  on('turn.complete', async ($, e, next) => {
    const done = await next(e)
    if (e.agentId === undefined) void onTurnComplete($, options)
    return done
  })

  // The dashboards are the session's: its end ends their children.
  on('session.end', async ($, e, next) => {
    const telemetry = e.reason === 'clear' || e.reason === 'resume' ? undefined : finishTelemetry($)
    await stopAll($)
    await telemetry
    await finishPromptCapture($, promptCapture)
    // /clear and /resume change the conversation without a new session.start.
    if (e.reason === 'clear' || e.reason === 'resume') promptCapture = createPromptCapture(options)
    return next(e)
  })

  // Observe-only pass-throughs: each carries the transcript Claude Code is
  // writing, the latest of which is the session to analyze.
  on('classic.SessionStart', async ($, e, next) => {
    await rememberTranscript($, e)
    return next(e)
  }).catch(($, e, next) => next(e))
  on('classic.Stop', async ($, e, next) => {
    await rememberTranscript($, e)
    return next(e)
  }).catch(($, e, next) => next(e))
  on('classic.UserPromptSubmit', async ($, e, next) => {
    await rememberTranscript($, e)
    return next(e)
  }).catch(($, e, next) => next(e))
}
