import { randomUUID } from 'node:crypto'
import type { ExtensionAPI, ExtensionContext, ContextWithSystemEvent, BeforeProviderRequestEvent, TurnStartEvent, TurnEndEvent, SessionCompactEvent } from '@earendil-works/pi-coding-agent'
import { PromptCapture } from '../../shared/prompt-capture.js'
import type { Runtime } from './backend.js'

type Sink = Pick<PromptCapture, 'enabled' | 'record' | 'flush' | 'close'>
type Run = { id: string; session: string; source: 'agent_start' | 'observed'; origin: string | null; sequence: number; turn?: number }
type Request = { sequence: number; origin: string | null; turn?: number; providerEvents: number }
type Lineage = { id: string; session: string; origin: string | null }

/** Observe Pi's declared boundaries without returning a context or payload replacement. */
export class PiPromptCapture {
  private readonly capture: Sink
  private run?: Run
  private request?: Request
  private lineage?: Lineage
  private closed = false
  private closing?: Promise<void>
  constructor(runtime: Pick<Runtime, 'bridge' | 'telemetryPython'>, hostVersion: string, capture?: Sink) {
    this.capture = capture ?? new PromptCapture({ adapter: 'pi', bridge: runtime.bridge, python: () => runtime.telemetryPython(), hostVersion })
  }
  private observe(ctx: ExtensionContext, action: (session: string, leaf: string | null) => void): void {
    try {
      if (this.closed || !this.capture.enabled) return
      const session = ctx.sessionManager.getSessionId()
      if (!session) return
      action(session, ctx.sessionManager.getLeafId())
    } catch { /* Capture failure must not change Pi's prompt, result, or extension flow. */ }
  }
  private lineageFor(session: string, leaf: string | null): void {
    if (!this.lineage || this.lineage.session !== session) {
      this.lineage = { id: randomUUID(), session, origin: leaf }
      this.run = undefined; this.request = undefined
    }
  }
  private current(session: string, leaf: string | null): Run {
    this.lineageFor(session, leaf)
    if (!this.run || this.run.session !== session) {
      this.run = { id: randomUUID(), session, source: 'observed', origin: leaf, sequence: 0 }
      this.request = undefined
    }
    return this.run
  }
  private record(ctx: ExtensionContext, stage: string, data: unknown, correlation: object, kind?: string): void {
    const model = ctx.model
    this.capture.record({ session_id: ctx.sessionManager.getSessionId(), stage, ...(kind ? { kind } : {}),
      correlation,
      ...(model ? { model: { provider: model.provider, id: model.id, api: model.api, context_window: model.contextWindow } } : {}),
      data,
    })
  }
  private correlation(run: Run, leaf: string | null, confidence: string): object {
    return { run_id: run.id, run_source: run.source, run_origin_entry_id: run.origin, observed_leaf_entry_id: leaf,
      ...(this.lineage ? { lineage_id: this.lineage.id, lineage_source: 'local_epoch', lineage_origin_entry_id: this.lineage.origin } : {}),
      ...(run.turn !== undefined ? { turn_index: run.turn } : {}),
      ...(this.request ? { request_seq: this.request.sequence, request_origin_entry_id: this.request.origin,
        provider_event_seq: this.request.providerEvents } : {}),
      confidence, provider_request_id_available: false,
    }
  }
  reset(): void { this.run = undefined; this.request = undefined; this.lineage = undefined }
  settled(): void { this.run = undefined; this.request = undefined }
  start(ctx: ExtensionContext): void {
    this.observe(ctx, (session, leaf) => {
      this.lineageFor(session, leaf)
      this.run = { id: randomUUID(), session, source: 'agent_start', origin: leaf, sequence: 0 }
      this.request = undefined
    })
  }
  turn(event: TurnStartEvent, ctx: ExtensionContext): void {
    this.observe(ctx, (session, leaf) => {
      this.current(session, leaf).turn = event.turnIndex
      this.request = undefined
    })
  }
  context(event: ContextWithSystemEvent, ctx: ExtensionContext): void {
    this.observe(ctx, (session, leaf) => {
      const run = this.current(session, leaf)
      this.request = { sequence: ++run.sequence, origin: leaf, turn: run.turn, providerEvents: 0 }
      this.record(ctx, 'context', { messages: event.messages, provenance: { hook: 'context_with_system',
        boundary: 'after_conversation_transforms_before_provider_serialization', later_handlers_may_modify: true } },
      this.correlation(run, leaf, run.turn !== undefined ? 'native_turn_context' : 'observed_context'), 'primary')
    })
  }
  provider(event: BeforeProviderRequestEvent, ctx: ExtensionContext): void {
    this.observe(ctx, (session, leaf) => {
      const run = this.current(session, leaf)
      if (this.request) this.request.providerEvents++
      this.record(ctx, 'provider.request', { payload: event.payload, provenance: { hook: 'before_provider_request',
        boundary: 'provider_payload_before_later_extension_handlers', later_handlers_may_modify: true,
        transport_may_send_delta: true, purpose: 'not_exposed_by_hook', association: 'temporal_not_provider_identity' } },
      this.correlation(run, leaf, this.request ? 'inferred_active_context' : 'unlinked_provider_request'))
    })
  }
  response(event: TurnEndEvent, ctx: ExtensionContext): void {
    this.observe(ctx, (session, leaf) => {
      const run = this.current(session, leaf)
      const linked = this.request && this.request.turn === event.turnIndex
      if (!linked) this.request = undefined
      run.turn = event.turnIndex
      try {
        this.record(ctx, 'response', { ...event, provenance: { hook: 'turn_end' } },
        this.correlation(run, leaf, linked ? 'native_turn_response' : 'unlinked_turn_response'), 'primary')
      } finally { this.request = undefined }
    })
  }
  compact(event: SessionCompactEvent, ctx: ExtensionContext): void {
    this.observe(ctx, (session, leaf) => {
      const run = this.current(session, leaf)
      this.request = undefined
      this.record(ctx, 'lifecycle.compaction', { ...event, provenance: { hook: 'session_compact',
        boundary: 'after_native_compaction', affects_next_context: true, request_payload_exposed: false,
        model_source: 'active_session_not_verified_summarizer' } },
      this.correlation(run, leaf, 'native_compaction'), 'compaction')
    })
  }
  async flush(): Promise<void> { try { await this.capture.flush() } catch { /* Capture remains best effort. */ } }
  close(): Promise<void> {
    if (this.closing) return this.closing
    this.closed = true; this.reset()
    return this.closing = (async () => { try { await this.capture.close() } catch { /* Never disrupt Pi shutdown. */ } })()
  }
}

export function registerPromptCapture(pi: ExtensionAPI, capture: () => PiPromptCapture | undefined): void {
  pi.on('session_start', () => { capture()?.reset() })
  pi.on('session_tree', event => { if (event.newLeafId !== event.oldLeafId) capture()?.reset() })
  pi.on('session_compact', (event, ctx) => { capture()?.compact(event, ctx) })
  pi.on('agent_start', (_event, ctx) => { capture()?.start(ctx) })
  pi.on('turn_start', (event, ctx) => { capture()?.turn(event, ctx) })
  pi.on('context_with_system', (event, ctx) => { capture()?.context(event, ctx) })
  pi.on('before_provider_request', (event, ctx) => { capture()?.provider(event, ctx) })
  pi.on('turn_end', (event, ctx) => { capture()?.response(event, ctx) })
  pi.on('agent_settled', () => { capture()?.settled() })
}
