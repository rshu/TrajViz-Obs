import type { Register } from 'claude-code'
import { parseArgs } from './lib/args'
import { resetServers, stopAll } from './lib/servers'
import { rememberTranscript } from './lib/session'
import { isEmpty, parseSnapshotId } from './lib/snapshot'
import { OBS_TOOL, serveTool } from './lib/tool'
import { onTurnComplete, resetStatusLine } from './lib/watch'
import { readDashboards, readFlags, readLatest, readPaneTab, readSession, readSnapshotMember, seedFlags } from './lib/state'
import { commandFailure, dispatch } from './commands/index'
import { cardColumns, renderCard, showsButtons } from './render/card'
import { paneUsage, renderPane } from './render/pane'

export const register: Register = (on, options) => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'obs',
      description: "Visualize and diagnose this session's trajectory with TrajViz",
      argumentHint: '[overview|dashboard|pane|issues|health|analyze|fixes|json|help] [--session id]',
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
    // A reload (a config change, /reload-plugins) starts the session hooks
    // again with the old environment's children killed: the state follows.
    await resetServers($)
    return next(e)
  })

  on('command.run', { command: 'obs' }, async ($, e) => {
    try {
      return await dispatch($, { args: e.args, parsed: parseArgs(e.args), options, presentation: e.presentation })
    } catch (error) {
      return { text: `obs: ${error instanceof Error ? error.message : String(error)}` }
    }
  }).catch((_$, e, next) => commandFailure(e.args, next.error))

  // The obs tool, answered here: the model's call never reaches a server.
  on('tool.call', { tool: 'mcp__trajviz-obs__obs' }, ($, e) => serveTool($, options, e))

  // The overview card: `/obs`'s output row drawn from the snapshot its
  // provenance line names (never the latest). An error row carries no id,
  // and a pruned member is gone: both leave the row to the engine.
  on('ui.render', { component: 'CommandOutput', props: { command: 'obs' } }, async ($, e, next) => {
    const id = parseSnapshotId(e.props.text)
    if (id === null) return next(e)
    const snap = await readSnapshotMember($, id)
    if (snap === null || isEmpty(snap)) return next(e)
    return renderCard($, e, snap, { options, showButtons: showsButtons(e), columns: cardColumns(e), usage: snap.engine_usage ?? null })
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
    return renderPane($, e, snap, tab, flags, dashboards, { options, usage: selected ? null : await paneUsage($, tab) })
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
    await stopAll($)
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
