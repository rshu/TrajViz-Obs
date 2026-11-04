import { For, Show, createMemo } from 'solid-js'
import type { ResolvedTheme } from '@opencode/theme/tui'
import type { RGBA } from '@opentui/core'
import { charts, type TokenBin } from './charts.js'
import { compact, duration, latestPhaseLabel, safeText } from './tui-model.js'
import type { Fragment, Tone } from './presentation.js'
import type { Snapshot } from './types.js'

type Color = string | RGBA
export type TerminalPalette = { base: Color; muted: Color; enabled: boolean; ascii: boolean; tone(tone?: Tone): Color; category(index: number): Color }

export function terminalPalette(theme: ResolvedTheme): TerminalPalette {
  const ascii = process.env.TERM === 'dumb', enabled = process.env.NO_COLOR === undefined && !ascii
  const color = (tone: Tone = 'neutral'): Color => {
    if (!enabled) return theme.text.base
    if (tone === 'agent') return theme.hue?.accent?.[500] ?? theme.text.base
    return tone === 'neutral' ? theme.text.base : theme.text.feedback?.[tone === 'danger' ? 'error' : tone]?.base ?? theme.text.base
  }
  return { base: theme.text.base, muted: enabled ? theme.text.muted : theme.text.base, enabled, ascii, tone: color,
    category: index => enabled ? theme.categorical?.[index % (theme.categorical?.length || 1)]?.[500] ?? color(['info','success','warning','agent'][index % 4] as Tone) : theme.text.base }
}

/** Terminal-cell clipping keeps a wide grapheme intact and never emits control codes. */
export function chartLabel(value: string, columns: number, ascii = false): string {
  const clean = safeText(value).replace(/\s+/g, ' ').trim(), suffix = ascii ? '~' : '…'
  if (Bun.stringWidth(clean) <= columns) return clean
  let clipped = ''
  for (const {segment} of new Intl.Segmenter(undefined, {granularity:'grapheme'}).segment(clean)) {
    if (Bun.stringWidth(clipped + segment + suffix) > columns) break
    clipped += segment
  }
  return columns > 0 ? clipped + suffix : ''
}

export function TerminalText(props: { palette: TerminalPalette; children?: string; fragments?: Fragment[]; tone?: Tone; muted?: boolean }) {
  return <text fg={props.muted ? props.palette.muted : props.palette.tone(props.tone)} wrapMode="word" flexShrink={0}>
    <Show when={props.fragments} fallback={safeText(props.children ?? '')}>
      <For each={props.fragments}>{fragment => <span style={{fg:props.palette.tone(fragment.tone)}}><Show when={fragment.emphasis} fallback={fragment.text}><b>{fragment.text}</b></Show></span>}</For>
    </Show>
  </text>
}

type Cell = { text: string; color?: Color }
const glyphs = '▁▂▃▄▅▆▇█'
const level = (value: number, max: number | null, ascii: boolean): string => value === 0 ? '_' : ascii ? '#' : glyphs[Math.min(7, Math.max(0, Math.ceil(value / (max || value) * 8) - 1))]!

/** Compact charts share actual work-step bins; no horizontal position implies wall time. */
export function TerminalCharts(props: { snapshot: Snapshot; width: number; palette: TerminalPalette; mode: 'overview'|'tools'|'agents'|'context'; compact?: boolean }) {
  const width = () => Math.max(8, Math.floor(props.width))
  const margin = () => Math.min(12, Math.max(5, Math.floor(width() / 3)))
  const columns = () => Math.max(1, Math.min(48, width() - margin()))
  const model = createMemo(() => charts(props.snapshot, columns()))
  const text = (value: string, tone?: Tone) => <TerminalText palette={props.palette} tone={tone}>{value}</TerminalText>
  const Row = (row: { label?: string; cells: Cell[] }) => <box flexDirection="row" flexShrink={0} width={width()}>
    <text width={margin()} truncate wrapMode="none" fg={props.palette.muted} flexShrink={0}>{chartLabel(row.label ?? '',margin() - 1,props.palette.ascii)}</text>
    <text width={columns()} wrapMode="none" flexShrink={0} fg={props.palette.base}><For each={row.cells}>{cell => <span style={{fg:cell.color ?? props.palette.base}}>{cell.text}</span>}</For></text>
  </box>
  const axis = () => text(`Work steps #${model().axis.start}–#${model().axis.end} · ${model().tokens.bins.length} bins`, 'info')
  const keys = ['fresh','cache_read','cache_write','output'] as const
  const tokenCell = (bin: TokenBin, row: number, height: number): Cell => {
    if (!bin.samples) return {text:' '}
    if (bin.knownTotal === null) return {text:row === 0 ? '?' : ' '}
    if (bin.unknown && row === 0) return {text:'?'}
    if (bin.knownTotal === 0) return {text:row === height - 1 ? '_' : ' '}
    const y = (height - row - 0.5) / height * (model().tokens.max || bin.knownTotal)
    if (y > bin.knownTotal) {
      if (row !== height - 1) return {text:' '}
      const index = keys.findIndex(key=>(bin[key] ?? 0)>0)
      return {text:props.palette.enabled && !props.palette.ascii?'▁':'FCWO'[index]!,color:props.palette.category(index)}
    }
    let cumulative = 0
    for (const [index,key] of keys.entries()) {
      cumulative += bin[key] ?? 0
      if (y <= cumulative) return {text:props.palette.enabled && !props.palette.ascii ? '█' : 'FCWO'[index]!,color:props.palette.category(index)}
    }
    return {text:' '}
  }
  const Tokens = () => <box flexDirection="column" flexShrink={0}>
    {text(`Tokens / step · plotted max/bin ${compact(model().tokens.max)}`, 'info')}
    <text flexShrink={0} fg={props.palette.base} wrapMode="word"><For each={['F fresh  ','C cache read  ','W cache write  ','O output']}>{(label,index) => <span style={{fg:props.palette.category(index())}}>{label}</span>}</For></text>
    <For each={Array.from({length:props.compact ? 3 : 4},(_,index)=>index)}>{row => <Row label={row === 0 ? compact(model().tokens.max) : ''} cells={model().tokens.bins.map(bin=>tokenCell(bin,row,props.compact ? 3 : 4))} />}</For>
    {text('? incomplete · _ measured zero · blank no sample')}
    {text('Input/cache/output; reasoning not plotted.')}
  </box>
  const Durations = () => <box flexDirection="column" flexShrink={0}>
    {text(`Step duration · peak ${duration(model().durations.max)}`, 'info')}
    <Row label="seconds" cells={model().durations.bins.map(bin=>({text:!bin.samples?' ':bin.seconds===null?'?':level(bin.seconds,model().durations.max,props.palette.ascii),color:props.palette.tone(bin.seconds===null?'neutral':bin.outlier?'warning':'info')}))} />
    <Row label="! / ?" cells={model().durations.bins.map(bin=>({text:bin.outlier?bin.unknown?'*':'!':bin.unknown?'?':' ',color:props.palette.tone(bin.outlier?'warning':'neutral')}))} />
    {text('! outlier · ? incomplete · * both')}
    <Show when={model().durations.bins.some(bin=>bin.outlier)}>{text(`Outlier steps: ${model().durations.bins.flatMap(bin=>bin.outliers).join(', ')}`)}</Show>
  </box>
  const Context = () => <box flexDirection="column" flexShrink={0}>
    {text(`Context occupancy · peak per bin ${model().context.max===null?'unknown':`${model().context.max!.toFixed(1)}%`}`, 'info')}
    <Row label="% window" cells={model().context.bins.map(bin=>({text:!bin.samples?' ':bin.max===null?'?':level(bin.max,model().context.max,props.palette.ascii),color:props.palette.tone(bin.max===null?'neutral':'info')}))} />
    <Row label="C / ?" cells={model().context.bins.map(bin=>({text:bin.compactions.length?bin.unknown?'*':'C':bin.unknown?'?':' ',color:props.palette.tone(bin.compactions.length?'agent':'neutral')}))} />
    {text('C compaction · ? incomplete · * both')}
    {text('_ measured zero · blank no sample')}
    {text(`Window: ${compact(model().context.window)} · ${safeText(model().context.windowSource)}`)}
    <Show when={model().context.windowNote}>{text(safeText(model().context.windowNote!))}</Show>
  </box>
  const Tools = () => <box flexDirection="column" flexShrink={0}>
    {text('Tool calls · top 8 by count', 'info')}
    <For each={model().tools} fallback={text('No recorded tool calls.')} >{tool => {
      const count = tool.calls>0?Math.max(1,Math.round(tool.calls / (model().toolMax || 1) * columns())):0
      const unknown = tool.unknown===null || tool.errors===null ? tool.calls : Math.min(tool.calls,tool.unknown)
      const failures = Math.min(tool.calls - unknown,tool.errors ?? 0)
      const success = Math.max(0,tool.calls - unknown - failures)
      const cells = Array.from({length:columns()},(_,index):Cell=> {
        if(index>=count)return {text:' '}
        const position=(index+0.5)/Math.max(1,count)*tool.calls
        const state=position<=success?'success':position<=success+failures?'danger':'neutral'
        return {text:state==='neutral'?'?':props.palette.ascii?state==='danger'?'x':'#':'█',color:props.palette.tone(state)}
      })
      return <box flexDirection="column" flexShrink={0}><Row label={tool.name} cells={cells}/><TerminalText palette={props.palette} fragments={[
        {text:`${compact(tool.calls)} calls · `},
        {text:`${compact(tool.errors)} failed`,tone:tool.errors!==null && tool.errors>0?'danger':'neutral'},
        {text:` · ${compact(tool.unknown)} unknown`},
      ]}/></box>
    }}</For>
    {text('/obs view tool_usage for the full ranking.')}
  </box>
  const Agents = () => <box flexDirection="column" flexShrink={0}>
    {text('Agent work-step activity', 'info')}
    <For each={model().agents} fallback={text('No recorded activity lanes.')} >{(lane,index)=><Row label={lane.label} cells={lane.bins.map(value=>({text:value>0?props.palette.ascii?'#':'━':' ',color:props.palette.category(index())}))}/>}</For>
    {text('Active work steps; blank means no recorded activity.')}
    <Show when={model().omittedLanes}>{text(`${model().omittedLanes} more lanes · /obs view agent_swimlane`)}</Show>
  </box>
  return <box id={`obs-charts-${props.mode}`} flexDirection="column" gap={1} flexShrink={0}>
    <Show when={props.mode!=='tools'}>{axis()}</Show>
    <Show when={props.mode==='overview'}><Tokens/><Durations/><Context/><Show when={latestPhaseLabel(props.snapshot)}>{label=>text(label(),'agent')}</Show></Show>
    <Show when={props.mode==='context'}><Context/></Show>
    <Show when={props.mode==='tools'}><Tools/></Show>
    <Show when={props.mode==='agents'}><Agents/></Show>
  </box>
}
