import { For, Show, createMemo, createSignal, createUniqueId, onCleanup, onMount, type JSX } from 'solid-js'
import { charts } from './charts.js'
import { compact, latestPhaseLabel, safeText } from './tui-model.js'
import type { Snapshot } from './types.js'

type Model = ReturnType<typeof charts>
type Geometry = { width: number; height: number; left: number; right: number; top: number; bottom: number; x(step: number): number; y(value: number, maximum: number): number }
const colors = { fresh: 'var(--obs-info)', cache_read: 'var(--obs-success)', cache_write: 'var(--obs-warning)', output: 'var(--obs-agent)', unknown: 'var(--obs-neutral)' }
const channelNames = { fresh: 'Fresh input', cache_read: 'Cache read', cache_write: 'Cache written', output: 'Output' }
const channels = ['fresh', 'cache_read', 'cache_write', 'output'] as const
const number = (value: number | null | undefined) => value == null || !Number.isFinite(value) ? 'unknown'
  : Number.isInteger(value) ? value.toLocaleString() : value.toLocaleString(undefined, { maximumSignificantDigits: 15 })
const axisNumber = (value: number) => Math.abs(value) >= 1000 ? compact(value) : value.toLocaleString(undefined, { maximumSignificantDigits: 4 })
const range = (bin: { start: number; end: number; step: number | null }) => `${bin.start === bin.end ? `Step ${bin.start}` : `Steps ${bin.start}–${bin.end}`}${bin.step != null && bin.start !== bin.end ? ` · sample at step ${bin.step}` : ''}`
const shorten = (value: string, length = 16) => { const chars = Array.from(safeText(value)); return chars.length > length ? chars.slice(0, length - 1).join('') + '…' : chars.join('') }

function Frame(props: { title: string; description: string; height?: number; legend?: { label: string; color: string }[]; empty?: string; children(g: Geometry): JSX.Element; axis?: Model['axis'] }) {
  const [width, setWidth] = createSignal(480)
  const id = createUniqueId()
  let host: HTMLDivElement | undefined
  onMount(() => {
    if (!host) return
    const resize = () => setWidth(Math.max(220, Math.round(host!.clientWidth)))
    resize()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(resize)
    observer.observe(host)
    onCleanup(() => observer.disconnect())
  })
  const geometry = (): Geometry => {
    const w = width(), h = props.height ?? 156, left = 44, right = w - 12, top = 24, bottom = h - 26
    const axis = props.axis ?? { start: 0, end: 1 }, span = axis.end - axis.start + 1
    return { width: w, height: h, left, right, top, bottom,
      x: step => left + (step - axis.start + 0.5) / span * (right - left),
      y: (value, maximum) => bottom - Math.max(0, value) / Math.max(maximum, Number.MIN_VALUE) * (bottom - top) }
  }
  return <section class="obs-chart" aria-label={props.title}>
    <h4>{props.title}</h4>
    <div ref={host} class="obs-chart-plot"><Show when={!props.empty} fallback={<p class="obs-chart-empty">{props.empty}</p>}><svg width="100%" height={props.height ?? 156} viewBox={`0 0 ${width()} ${props.height ?? 156}`} role="img" aria-labelledby={`${id}-title ${id}-description`}><title id={`${id}-title`}>{props.title}</title><desc id={`${id}-description`}>{props.description}</desc>{props.children(geometry())}</svg></Show></div>
    <Show when={props.legend?.length}><div class="obs-chart-legend"><For each={props.legend}>{item => <span><span class="obs-chart-swatch" style={{ 'background-color': item.color }} aria-hidden="true" />{item.label}</span>}</For></div></Show>
    <p class="obs-chart-caption">{props.description}</p>
  </section>
}

function Axes(props: { g: Geometry; axis: Model['axis']; maximum: number; unit: string }) {
  const g = () => props.g
  return <g>
    <For each={[0, props.maximum / 2, props.maximum]}>{tick => <g><line x1={g().left} x2={g().right} y1={g().y(tick, props.maximum)} y2={g().y(tick, props.maximum)} class="obs-chart-grid" /><text x={g().left - 6} y={g().y(tick, props.maximum) + 4} text-anchor="end">{axisNumber(tick)}</text></g>}</For>
    <text x={g().left} y={11}>{props.unit}</text><text x={g().left} y={g().height - 8} text-anchor="start">{props.axis.start}</text><Show when={props.axis.end !== props.axis.start}><text x={g().right} y={g().height - 8} text-anchor="end">{props.axis.end}</text></Show><text x={(g().left + g().right) / 2} y={g().height - 8} text-anchor="middle">Work step</text>
  </g>
}
const marker = (x: number, y: number) => `M${x - 3},${y - 3}l6,6m0,-6l-6,6`
const barWidth = (g: Geometry, count: number) => Math.max(1, Math.min(28, (g.right - g.left) / Math.max(1, count) * 0.72))

function Tokens(props: { model: Model }) {
  const bins = () => props.model.tokens.bins
  const maximum = () => Math.max(1, props.model.tokens.max ?? 0)
  const partial = () => bins().some(bin => bin.samples > 0 && (bin.unknown > 0 || bin.total === null))
  return <Frame title="Tokens per step" axis={props.model.axis} description={`Input, cache, and output from one recorded sample per bin; reasoning is shown in Context details. ${partial() ? '× marks missing or partial usage; visible segments are observed counts only.' : 'Colors distinguish fresh input, cache, and output.'}`} legend={[...channels.map(channel => ({ label: channelNames[channel], color: colors[channel] })), ...(partial() ? [{ label: '× Missing / partial', color: colors.unknown }] : [])]} empty={props.model.tokens.max === null ? 'Token usage was not recorded.' : undefined}>{g => <>
    <Axes g={g} axis={props.model.axis} maximum={maximum()} unit="tokens" />
    <For each={bins()}>{bin => {
      const x = () => g.x((bin.start + bin.end) / 2), width = () => barWidth(g, bins().length)
      let sum = 0
      const stack = channels.flatMap(channel => { const value = bin[channel]; if (value === null) return []; const start = sum; sum += value; return [{ channel, value, start }] })
      const tooltip = () => `${range(bin)}\n${channels.map(channel => `${channelNames[channel]}: ${number(bin[channel])} tokens`).join('\n')}\nPlotted total: ${number(bin.total)}${bin.total === null && bin.knownTotal !== null ? ` · ${number(bin.knownTotal)} observed` : ''}${bin.unknown ? ` · ${bin.unknown} samples with missing components` : ''}`
      return <g><title>{tooltip()}</title><For each={stack}>{part => <rect x={x() - width() / 2} y={g.y(part.start + part.value, maximum())} width={width()} height={Math.max(0, g.y(part.start, maximum()) - g.y(part.start + part.value, maximum()))} fill={colors[part.channel]} />}</For><Show when={bin.total === 0}><circle cx={x()} cy={g.bottom} r={2} fill={colors.fresh} /></Show><Show when={bin.samples > 0 && (bin.total === null || bin.unknown > 0)}><path d={marker(x(), bin.knownTotal === null ? g.bottom - 4 : g.y(bin.knownTotal, maximum()) - 5)} stroke={colors.unknown} stroke-width={1.4} fill="none" /></Show></g>
    }}</For>
  </>}</Frame>
}

function Durations(props: { model: Model }) {
  const bins = () => props.model.durations.bins, maximum = () => Math.max(0.001, props.model.durations.max ?? 0)
  return <Frame title="Step time" axis={props.model.axis} description="Engine step timing excludes recorded subagent waits when available; Timeline shows full recorded step duration. Each bin keeps its slowest observed sample. △ marks a bin containing source-reported outliers; × marks missing timing." legend={[{ label: 'Step time', color: colors.fresh }, { label: '△ Outlier in bin', color: colors.cache_write }, { label: '× Unknown', color: colors.unknown }]} empty={props.model.durations.max === null ? 'Step timing was not recorded.' : undefined}>{g => <>
    <Axes g={g} axis={props.model.axis} maximum={maximum()} unit="seconds" />
    <For each={bins()}>{bin => {
      const x = () => g.x((bin.start + bin.end) / 2), width = () => barWidth(g, bins().length)
      return <g><title>{range(bin)} · {number(bin.seconds)} seconds{bin.outlier ? ` · outlier steps ${bin.outliers.join(', ')}` : ''}{bin.unknown ? ` · ${bin.unknown} unknown timing samples` : ''}</title><Show when={bin.seconds !== null} fallback={bin.samples ? <path d={marker(x(), g.bottom - 4)} stroke={colors.unknown} stroke-width={1.4} fill="none" /> : undefined}><rect x={x() - width() / 2} y={g.y(bin.seconds ?? 0, maximum())} width={width()} height={Math.max(0, g.bottom - g.y(bin.seconds ?? 0, maximum()))} fill={bin.outlier ? colors.cache_write : colors.fresh} /><Show when={bin.seconds === 0}><circle cx={x()} cy={g.bottom} r={2} fill={colors.fresh} /></Show><Show when={bin.outlier}><path d={`M${x()},${g.y(bin.seconds ?? 0, maximum()) - 7}l-3,5h6z`} fill={colors.cache_write} /></Show><Show when={bin.unknown > 0}><path d={marker(x(), g.bottom - 4)} stroke={colors.unknown} stroke-width={1.4} fill="none" /></Show></Show></g>
    }}</For>
  </>}</Frame>
}

function Context(props: { model: Model }) {
  const bins = () => props.model.context.bins, maximum = () => Math.max(100, props.model.context.max ?? 0)
  const path = (g: Geometry) => {
    let drawing = false
    return bins().map(bin => {
      if (!bin.samples) { drawing = false; return '' }
      if (bin.percent === null || bin.step === null) { drawing = false; return '' }
      if (bin.unknown) drawing = false
      const command = drawing ? 'L' : 'M'
      drawing = bin.unknown === 0
      return `${command}${g.x(bin.step)},${g.y(bin.percent, maximum())}`
    }).join(' ')
  }
  return <Frame title="Context occupancy" axis={props.model.axis} description={`Observed context / ${number(props.model.context.window)}-token window (${props.model.context.windowSource === 'default' ? 'assumed' : safeText(props.model.context.windowSource)}). Lines stop at missing samples; vertical marks show compactions. Ranges retain observed minima and maxima.${props.model.context.windowNote ? ' ' + safeText(props.model.context.windowNote) : ''}`} legend={[{ label: 'Occupancy', color: colors.output }, { label: 'Compaction', color: colors.cache_write }, { label: '× Unknown', color: colors.unknown }]} empty={props.model.context.max === null ? 'Context occupancy or its window was not recorded.' : undefined}>{g => <>
    <Axes g={g} axis={props.model.axis} maximum={maximum()} unit="% of window" />
    <For each={bins()}>{bin => <g><title>{range(bin)} · context {number(bin.percent)}% · observed range {number(bin.min)}–{number(bin.max)}%{bin.unknown ? ` · ${bin.unknown} unknown samples` : ''}</title><For each={bin.compactions}>{step => <line x1={g.x(step)} x2={g.x(step)} y1={g.top} y2={g.bottom} stroke={colors.cache_write} stroke-width={1} stroke-dasharray="3 3"><title>Compaction at step {step}</title></line>}</For><Show when={bin.min !== null && bin.max !== null}><line x1={g.x((bin.start + bin.end) / 2)} x2={g.x((bin.start + bin.end) / 2)} y1={g.y(bin.min ?? 0, maximum())} y2={g.y(bin.max ?? 0, maximum())} stroke={colors.output} stroke-width={2} /></Show><Show when={bin.percent !== null && bin.step !== null}><circle cx={g.x(bin.step ?? 0)} cy={g.y(bin.percent ?? 0, maximum())} r={2} fill={colors.output} /></Show><Show when={bin.samples > 0 && (bin.percent === null || bin.unknown > 0)}><path d={marker(g.x((bin.start + bin.end) / 2), g.bottom - 4)} stroke={colors.unknown} stroke-width={1.4} fill="none" /></Show></g>}</For><path d={path(g)} fill="none" stroke={colors.output} stroke-width={2} />
  </>}</Frame>
}

function Tools(props: { model: Model }) {
  const rows = () => props.model.tools, maximum = () => Math.max(1, ...rows().map(row => row.calls))
  return <Frame title="Tool calls" height={Math.max(100, rows().length * 28 + 22)} description="Top 8 tools by recorded calls. Red segments are observed failures; unknown outcomes remain separate from successes." legend={[{ label: 'Calls', color: colors.fresh }, { label: 'Observed failures', color: 'var(--obs-danger)' }, { label: '? Unknown outcomes', color: colors.unknown }]} empty={!rows().length ? 'No tool calls recorded.' : undefined}>{g => <For each={rows()}>{(row, index) => {
    const left = Math.min(106, g.width * .36), right = g.width - 46, plot = right - left, top = 12 + index() * 28
    const width = () => row.calls / maximum() * plot
    return <g><title>{safeText(row.name)} · {number(row.calls)} calls · {number(row.errors)} observed failures · {number(row.unknown)} unknown outcomes</title><text x={left - 7} y={top + 11} text-anchor="end">{shorten(row.name, Math.max(6, Math.floor((left - 10) / 9)))}</text><rect x={left} y={top} width={width()} height={14} rx={2} fill={colors.fresh} /><Show when={row.errors !== null && row.errors > 0}><rect x={left} y={top} width={Math.min(row.calls, row.errors ?? 0) / maximum() * plot} height={14} rx={2} fill="var(--obs-danger)" /></Show><text x={right + 7} y={top + 11}>{compact(row.calls)}{row.unknown === null || row.unknown > 0 || row.errors === null ? ' ?' : ''}</text></g>
  }}</For>}</Frame>
}

function Agents(props: { model: Model }) {
  const rows = () => props.model.agents
  return <Frame title="Agent activity" axis={props.model.axis} height={Math.max(100, rows().length * 28 + 36)} description={`Recorded activity on the canonical work-step axis, including recorded user messages. ${props.model.omittedLanes ? `${props.model.omittedLanes} additional lanes omitted.` : 'Empty intervals are gaps, not measured idle time.'}${rows().some(row => row.omittedSegments > 0) ? ' Dense lanes show bins with recorded activity.' : ''}`} legend={[{ label: 'Agent activity', color: colors.output }, { label: 'User messages', color: colors.unknown }]} empty={!rows().length ? 'No agent activity recorded.' : undefined}>{g => {
    const left = Math.min(106, g.width * .36), right = g.width - 12, span = props.model.axis.end - props.model.axis.start + 1
    const x = (step: number) => left + (step - props.model.axis.start) / span * (right - left)
    return <><For each={rows()}>{(row, index) => <g><title>{safeText(row.label)} · {row.segments.length} recorded activity ranges</title><text x={left - 7} y={index() * 28 + 23} text-anchor="end" fill={row.kind === 'user' ? colors.unknown : colors.output}>{shorten(row.label, Math.max(6, Math.floor((left - 10) / 9)))}</text><line x1={left} x2={right} y1={index() * 28 + 18} y2={index() * 28 + 18} class="obs-chart-grid" /><Show when={row.omittedSegments === 0} fallback={<For each={row.bins}>{(count, binIndex) => <Show when={count > 0}><rect x={x(props.model.tokens.bins[binIndex()]!.start)} y={index() * 28 + 11} width={Math.max(1, x(props.model.tokens.bins[binIndex()]!.end + 1) - x(props.model.tokens.bins[binIndex()]!.start))} height={12} rx={2} fill={row.kind === 'user' ? colors.unknown : colors.output}><title>{safeText(row.label)} · {count} recorded work steps in bin {props.model.tokens.bins[binIndex()]!.start}–{props.model.tokens.bins[binIndex()]!.end}</title></rect></Show>}</For>}><For each={row.segments}>{segment => <rect x={x(segment.start)} y={index() * 28 + 11} width={Math.max(1, x(segment.end + 1) - x(segment.start))} height={12} rx={2} fill={row.kind === 'user' ? colors.unknown : colors.output}><title>{safeText(row.label)} · steps {segment.start}–{segment.end}</title></rect>}</For></Show></g>}</For><text x={left} y={g.height - 5}>{props.model.axis.start}</text><text x={right} y={g.height - 5} text-anchor="end">{props.model.axis.end}</text><text x={(left + right) / 2} y={g.height - 5} text-anchor="middle">Work step</text></>
  }}</Frame>
}

export function DesktopCharts(props: { snapshot: Snapshot; tab: number }) {
  const model = createMemo(() => charts(props.snapshot, 64))
  return <><div class="obs-charts"><Show when={props.tab === 0}><Tokens model={model()} /><Durations model={model()} /><Context model={model()} /></Show><Show when={props.tab === 1}><Tools model={model()} /></Show><Show when={props.tab === 4}><Agents model={model()} /></Show><Show when={props.tab === 5}><Context model={model()} /></Show></div><Show when={props.tab === 0 && latestPhaseLabel(props.snapshot)}>{label => <p class="obs-line obs-tone-agent">{label()}</p>}</Show></>
}
