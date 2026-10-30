// The mod's $.state contract and the bridge's snapshot document, transcribed
// from docs/superpowers/plans/2026-10-22-obs-bridge.md (Task 5). Named in
// plugin.json as "types"; `claude plugin validate` checks every $.state key
// the hooks module names against it. Self-contained: no import; StateFamily
// resolves inside the module augmentation below.

export type TranscriptInfo = {
  path: string
  session_id: string
  size: number
  mtime_ms: number
  sha256: string
  capture_session_id?: string
}

export type TrajvizInfo = {
  version: string
  format: string
  format_label: string
  warnings: string[]
}

export type Counts = {
  records: number
  assistant_steps: number
  user_records: number
  prompts: number
  compactions: number | null
  agents: number
  tool_calls: number
  /** Every detected issue, before the snapshot's top-ten preview. */
  issues?: number
}

export type Tokens = {
  /** Null means the source did not record this measurement; zero is a measured zero. */
  total: number | null
  input: number | null
  output: number | null
  cache_read: number | null
  cache_write: number | null
  reasoning: number | null
}

export type Timing = {
  wall_clock_s: number | null
  started_at: string | null
  finished_at: string | null
  avg_step_s: number | null
  p95_step_s: number | null
  output_tokens_per_sec: number | null
  tool_wait_share: number | null
}

export type ToolRow = { tool: string; calls: number; errors: number | null; unknown?: number }

export type Tools = {
  success_rate: number | null
  calls: number
  failed: number | null
  /** Calls whose final outcome was not recorded. */
  unknown?: number
  /** Top 12 by calls. */
  by_tool: ToolRow[]
}

export type HealthRow = { metric: string; status: string; label: string; detail: string }

export type Issue = {
  kind: string
  title: string
  detail: string
  why: string
  steps: number[]
  /** `<tool>: <command or path excerpt ≤ 80 chars>` of the first step's first tool call, else "". */
  evidence: string
}

export type Bottleneck = { step: number; duration_s: number; cause: string; title: string; detail: string }

export type Context = {
  window: number | null
  window_source: string
  window_note: string | null
  current_occupancy: number | null
  current_pct: number | null
  peak_occupancy: number | null
  peak_pct: number | null
  compaction_count: number | null
  largest_drop: number | null
}

export type Series = {
  tokens: { steps: number[]; fresh: (number | null)[]; cache_read: (number | null)[]; cache_write: (number | null)[]; output: (number | null)[] }
  durations: { steps: number[]; seconds: (number | null)[]; outliers: number[] }
  context: { turns: number[]; occupancy: (number | null)[]; compactions: number[] }
}

export type Phase = { name: string; start: number; end: number }

export type Agent = {
  id: string
  label: string
  type: string | null
  description: string | null
  steps: number
  tokens: number | null
  tool_calls: number
  errors: number | null
  unknown_outcomes?: number
  observed_errors?: number
  duration_s: number | null
  cache_pct: number | null
  gen_tps: number | null
}

export type Segment = { start: number; end: number }

export type Swimlane = {
  total_steps: number
  lanes: { id: string; label: string; segments: Segment[] }[]
}

/** Each list top 8 (hotspots top 5); rows are TrajViz's own, drawn as given. */
export type Patterns = {
  tool_sequences: Record<string, unknown>[]
  failure_patterns: Record<string, unknown>[]
  error_clusters: Record<string, unknown>[] | { reliable: boolean; clusters: Record<string, unknown>[] }
  failure_chains: Record<string, unknown>[] | { metrics: Record<string, unknown>; chains: Record<string, unknown>[] }
  fruitless_streaks: Record<string, unknown>[]
  tool_selection: Record<string, unknown>[]
  edit_thrash: Record<string, unknown>[]
  repeated_searches: Record<string, unknown>[]
  phase_regressions: Record<string, unknown>[]
  premature_compactions: Record<string, unknown>[]
  hotspots: Record<string, unknown>[] | Record<string, Record<string, unknown>[]>
}

export type TimelineRow = {
  step: number
  role: string
  category: string
  agent: string
  errors: number | null
  unknown_outcomes?: number
  tool_calls: number
  duration_s: number | null
  tokens: number | null
  preview: string
}

export type Files = {
  unique: number | null
  reads: number | null
  writes: number | null
  searches: number | null
  /** Top 8. */
  top: { file: string; reads: number; writes: number }[]
}

export type Plan = { items: number | null; done: number | null; stalled: number | null }

export type Skill = { skill: string; calls: number }

export type Artifacts = {
  dir: string | null
  views: string | null
  labels: string | null
}

export type EvidenceField = {
  status: string
  observed?: number
  partial?: number
  total?: number
  reasons?: Record<string, number>
}

export type Availability = { status: string; basis?: string; reason?: string; fields?: Record<string, EvidenceField> }

/** Evidence coverage over the captured source; it does not assert capture completeness. */
export type DataQuality = {
  source: string
  basis: string
  capture_complete: boolean | null
  fields: Record<string, EvidenceField>
  limitations?: string[]
  profile?: string
  profile_version?: number
}

/** The bridge's `snapshot` document: mirrors the contract exactly. */
export type Snapshot = {
  schema_version: 1
  empty?: false
  generated_at: string
  duration_ms: number
  /** Engine usage captured with this snapshot, never the usage of a later turn. */
  engine_usage?: { context: { window: number; percent?: number }; cost?: { usd: number } }
  transcript: TranscriptInfo
  trajviz: TrajvizInfo
  title: string | null
  task: string | null
  counts: Counts
  tokens: Tokens
  timing: Timing
  tools: Tools
  health: HealthRow[]
  data_quality?: DataQuality
  availability?: Record<string, Availability>
  /** Top 10. */
  issues: Issue[]
  /** Top 5. */
  bottlenecks: Bottleneck[]
  context: Context
  series: Series
  phases: Phase[]
  agents: Agent[]
  swimlane: Swimlane
  patterns: Patterns
  /** The last 60 steps. */
  timeline: TimelineRow[]
  /** The `diagnostics` view as is. */
  diagnostics: Record<string, unknown>
  files: Files
  plan: Plan
  skills: Skill[]
  artifacts: Artifacts
  /** The overview text; "" until the bridge fills it. */
  text: string
}

/** `snapshot.empty_snapshot(reason, transcript_path)`: a session with nothing to analyze yet. */
export type EmptySnapshot = {
  schema_version: 1
  empty: true
  reason: string
  transcript: Partial<TranscriptInfo> & { path: string }
}

export type SessionState = {
  /** The immutable Python environment selected by this Claude session. */
  environment?: { revision?: number; pythonOption?: string; sourceCwd?: string; python: string; trajviz: string | null; extras: string[]; source: string; createdAt: number }
  transcriptPath: string | null
  sessionId: string | null
  selected: { path: string; id: string; label: string; captureSessionId?: string } | null
}

export type Flags = {
  watch: boolean
  statusline: boolean
  isInteractive: boolean
  envReady: boolean
}

export type Dashboards = {
  insight: { url: string; port: number } | null
  converge: { url: string; port: number } | null
}

export type RefreshState = {
  lastRunAt: number
  lastDurationMs: number
  lastSize: number
  lastMtimeMs: number
  busy: boolean
  /** When `busy` was claimed (the host's clock, ms since the epoch); 0 while clear. A claim older than the longest run is stale and reclaimed. */
  busySince: number
}

declare module 'claude-code' {
  interface PluginState {
    'trajviz-obs': {
      latest: Snapshot | EmptySnapshot | null
      snapshot: StateFamily<Snapshot | EmptySnapshot | null>
      snapshotIds: string[]
      session: SessionState
      flags: Flags
      dashboards: Dashboards
      paneTab: number
      refresh: RefreshState
    }
  }
}
