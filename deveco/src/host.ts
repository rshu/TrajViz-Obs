/** The function-and-hooks ABI shipped in DevEco Code 0.2.6 (legacy SDK 1.17.9).
 * Keep the boundary structural: DevEco's distribution renames its plugin SDK,
 * while the actual client still uses the legacy path/query response envelope.
 */
export type HostResponse<T> = { data?: T; error?: unknown; response?: Response }
export type HostSessionOptions = {
  path: { id: string }
  query?: { directory?: string; limit?: number }
  signal?: AbortSignal
  throwOnError?: boolean
}
export type HostListOptions = {
  query?: { directory?: string; limit?: number; start?: number; roots?: boolean }
  signal?: AbortSignal
  throwOnError?: boolean
}
export type HostClient = {
  session: {
    get(options: HostSessionOptions): Promise<HostResponse<Record<string, unknown>>>
    messages(options: HostSessionOptions): Promise<HostResponse<unknown[]>>
    children?(options: HostSessionOptions): Promise<HostResponse<Record<string, unknown>[]>>
    list?(options?: HostListOptions): Promise<HostResponse<Record<string, unknown>[]>>
  }
}
export type HostContext = {
  client: HostClient
  directory: string
  worktree?: string
  project?: Record<string, unknown>
  serverUrl?: URL
  $?: unknown
}
export type HostToolContext = {
  sessionID: string
  messageID?: string
  callID?: string
  agent?: string
  directory?: string
  worktree?: string
  abort?: AbortSignal
  metadata?: (value: Record<string, unknown>) => void
}
export type HostTool = {
  description: string
  args: Record<string, unknown>
  execute(args: any, context: HostToolContext): Promise<string>
}
export type HookInput = Record<string, unknown>
export type HostHooks = {
  dispose?: () => Promise<void>
  event?: (input: { event: { type: string; properties?: Record<string, unknown> } }) => Promise<void>
  config?: (config: Record<string, unknown>) => Promise<void>
  tool?: Record<string, HostTool>
  'chat.message'?: (input: HookInput, output: { message: Record<string, unknown>; parts: Record<string, unknown>[] }) => Promise<void>
  'chat.params'?: (input: HookInput, output: Record<string, unknown>) => Promise<void>
  'chat.headers'?: (input: HookInput, output: { headers: Record<string, string> }) => Promise<void>
  'command.execute.before'?: (input: { command: string; sessionID: string; arguments: string }, output: { parts: Record<string, unknown>[] }) => Promise<void>
  'tool.execute.before'?: (input: HookInput, output: { args: unknown }) => Promise<void>
  'tool.execute.after'?: (input: HookInput, output: Record<string, unknown>) => Promise<void>
  'experimental.chat.system.transform'?: (input: HookInput, output: { system: unknown[] }) => Promise<void>
  'experimental.chat.messages.transform'?: (input: HookInput, output: { messages: unknown[] }) => Promise<void>
  'experimental.session.compacting'?: (input: HookInput, output: { context: unknown[]; prompt?: string }) => Promise<void>
}
export type HostPlugin = (context: HostContext, options?: Record<string, unknown>) => Promise<HostHooks>
