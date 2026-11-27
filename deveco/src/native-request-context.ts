import { isSpanContextValid, trace } from '@opentelemetry/api'

export type NativeTraceContext = {
  basis: 'native_processor_span'
  trace_id: string
  span_id: string
  sessionID: string
  userMessageID: string
  purpose: 'assistant' | 'compaction' | 'title'
}
const nativeID = (value: unknown, prefix: string): value is string =>
  typeof value === 'string' && value.startsWith(prefix) && /^[A-Za-z0-9_-]{1,256}$/.test(value)

/** A request reference, never an assistant-ID guess. The private OTLP journal
 * resolves the active span's ancestry to the native SessionProcessor later.
 * Legacy DevEco chat.headers exposes the user message, not the assistant.
 */
export function getNativeTraceContext(input: Record<string, unknown>): NativeTraceContext | undefined {
  if (process.env.TRAJVIZ_NATIVE_TRACE_CAPTURE !== '1') return undefined
  const message = input.message as Record<string, unknown> | undefined
  if (!nativeID(input.sessionID, 'ses_') || !nativeID(message?.id, 'msg_')) return undefined
  if (message?.sessionID !== undefined && message.sessionID !== input.sessionID) return undefined
  const span = trace.getActiveSpan()
  if (!span?.isRecording()) return undefined
  const current = span.spanContext()
  if (!isSpanContextValid(current) || (current.traceFlags & 1) !== 1) return undefined
  const purpose = input.agent === 'title' ? 'title' : input.agent === 'compaction' ? 'compaction' : 'assistant'
  return { basis: 'native_processor_span', trace_id: current.traceId, span_id: current.spanId,
    sessionID: input.sessionID, userMessageID: message!.id as string, purpose }
}
