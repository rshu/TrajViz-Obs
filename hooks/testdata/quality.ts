import type { Snapshot } from '../../types'
import { SNAPSHOT } from './snapshot'

/** Captured operations with partial observations, including a genuine measured zero. */
export const PARTIAL_SNAPSHOT: Snapshot = {
  ...SNAPSHOT,
  title: 'Partial captured session',
  tokens: { total: null, input: 7, output: null, cache_read: 0, cache_write: null, reasoning: null },
  tools: { calls: 1, failed: 0, unknown: 1, success_rate: null, by_tool: [{ tool: 'Running tool', calls: 1, errors: 0, unknown: 1 }] },
  health: [
    { metric: 'Tool Success', status: 'unavailable', label: 'N/A', detail: 'One outcome was not recorded' },
    { metric: 'Errors', status: 'unavailable', label: 'N/A', detail: 'Zero observed failures; one outcome unknown' },
  ],
  issues: [],
  counts: { ...SNAPSHOT.counts, assistant_steps: 3, agents: 1, tool_calls: 1, issues: 0 },
  timing: { ...SNAPSHOT.timing, wall_clock_s: null, avg_step_s: null, p95_step_s: null },
  context: { ...SNAPSHOT.context, current_occupancy: null, current_pct: null, peak_occupancy: 0, peak_pct: 0, compaction_count: null },
  files: { unique: null, reads: null, writes: null, searches: null, top: [] },
  plan: { items: null, done: null, stalled: null },
  series: {
    tokens: { steps: [0, 1, 2], fresh: [null, 0, 7], cache_read: [null, 0, 0], cache_write: [null, 0, null], output: [null, 0, null] },
    durations: { steps: [0, 1, 2], seconds: [null, 0, 1], outliers: [] },
    context: { turns: [0, 1, 2], occupancy: [null, 0, null], compactions: [] },
  },
  agents: [{ id: 'main', label: 'main', type: null, description: null, steps: 3, tokens: null, tool_calls: 1, errors: null, observed_errors: 0, unknown_outcomes: 1, duration_s: null, cache_pct: null, gen_tps: null }],
  swimlane: { total_steps: 3, lanes: [{ id: 'main', label: 'main', segments: [{ start: 0, end: 2 }] }] },
  timeline: [{ step: 2, role: 'assistant', category: 'tool_calls', agent: 'main', errors: 0, unknown_outcomes: 1, tool_calls: 1, duration_s: null, tokens: null, preview: 'An operation is still running.' }],
  patterns: { tool_sequences: [], failure_patterns: [], error_clusters: [], failure_chains: [], fruitless_streaks: [], tool_selection: [], edit_thrash: [], repeated_searches: [], phase_regressions: [], premature_compactions: [], hotspots: [] },
  phases: [],
  bottlenecks: [],
  data_quality: {
    source: 'otlp', basis: 'captured_operations', capture_complete: null,
    fields: {
      'tokens.total': { status: 'unavailable', observed: 0, total: 3, reasons: { not_recorded: 3 } },
      'tokens.input': { status: 'partial', observed: 2, total: 3 },
      tool_outcomes: { status: 'unknown', observed: 0, total: 1 },
    },
  },
  availability: {
    failure_patterns: { status: 'unavailable', basis: 'captured_operations' },
    file_interactions: { status: 'unavailable', basis: 'captured_operations' },
    skill_calls: { status: 'unavailable', basis: 'captured_operations' },
  },
  trajviz: { ...SNAPSHOT.trajviz, version: '0.3.1', warnings: [] },
}

/** No usage was captured; internal source placeholders must never be drawn as zeros. */
export const UNKNOWN_SNAPSHOT: Snapshot = {
  ...PARTIAL_SNAPSHOT,
  tokens: { total: null, input: null, output: null, cache_read: null, cache_write: null, reasoning: null },
  series: {
    tokens: { steps: [0], fresh: [null], cache_read: [null], cache_write: [null], output: [null] },
    durations: { steps: [0], seconds: [null], outliers: [] },
    context: { turns: [0], occupancy: [null], compactions: [] },
  },
}

/** Real zero observations remain visible, even while capture completeness is unasserted. */
export const ZERO_SNAPSHOT: Snapshot = {
  ...PARTIAL_SNAPSHOT,
  tokens: { total: 0, input: 0, output: 0, cache_read: 0, cache_write: 0, reasoning: 0 },
  tools: { calls: 1, failed: 0, unknown: 0, success_rate: 100, by_tool: [{ tool: 'Complete tool', calls: 1, errors: 0, unknown: 0 }] },
  timing: { ...PARTIAL_SNAPSHOT.timing, wall_clock_s: 0, avg_step_s: 0, p95_step_s: 0 },
  context: { ...PARTIAL_SNAPSHOT.context, current_occupancy: 0, current_pct: 0, peak_occupancy: 0, peak_pct: 0, compaction_count: 0, largest_drop: 0 },
  files: { unique: 0, reads: 0, writes: 0, searches: 0, top: [] },
  plan: { items: 0, done: 0, stalled: 0 },
  agents: PARTIAL_SNAPSHOT.agents.map(agent => ({ ...agent, tokens: 0, errors: 0, unknown_outcomes: 0, duration_s: 0 })),
  timeline: PARTIAL_SNAPSHOT.timeline.map(step => ({ ...step, tokens: 0, errors: 0, unknown_outcomes: 0, duration_s: 0 })),
  data_quality: { source: 'native', basis: 'source_records', capture_complete: null, fields: {} },
  availability: {},
  health: [{ metric: 'Errors', status: 'good', label: '0', detail: 'All recorded outcomes succeeded' }],
  series: {
    tokens: { steps: [0], fresh: [0], cache_read: [0], cache_write: [0], output: [0] },
    durations: { steps: [0], seconds: [0], outliers: [] },
    context: { turns: [0], occupancy: [0], compactions: [] },
  },
}
