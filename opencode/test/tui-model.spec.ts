import { describe, expect, test } from 'bun:test'
import { SNAPSHOT } from '../../hooks/testdata/snapshot.js'
import { PARTIAL_SNAPSHOT, UNKNOWN_SNAPSHOT, ZERO_SNAPSHOT } from '../../hooks/testdata/quality.js'
import { TABS, commandText, compact, duration, metrics, percent, safeText, sections, statusText, trend } from '../src/tui-model.js'

describe('native pane data', () => {
  test('every tab keeps its diagnostic data and full issue evidence', () => {
    expect(TABS).toEqual(['Overview', 'Tools', 'Issues', 'Patterns', 'Agents', 'Context', 'Timeline'])
    for (let tab = 0; tab < TABS.length; tab++) {
      const view = sections(SNAPSHOT, tab, 32)
      expect(view.length).toBeGreaterThan(0)
      expect(view.flatMap(section => section.lines).join('\n').length).toBeGreaterThan(0)
    }
    const issues = sections(SNAPSHOT, 2, 32).flatMap(section => section.lines).join('\n')
    for (const issue of SNAPSHOT.issues) {
      expect(issues).toContain(issue.evidence)
      expect(issues).toContain(issue.detail)
      expect(issues).toContain(issue.why)
      expect(issues).toContain(issue.steps.join(', '))
    }
    const tools = sections(SNAPSHOT, 1, 32).flatMap(section => section.lines).join('\n')
    for (const row of SNAPSHOT.tools.by_tool) expect(tools).toContain(row.tool)
    const agents = sections(SNAPSHOT, 4, 32).flatMap(section => section.lines).join('\n')
    for (const row of SNAPSHOT.agents) expect(agents).toContain(row.label)
    expect(sections(SNAPSHOT, 5, 32)[0]?.lines.join('\n')).toContain('71%')
  })

  test('unknown usage stays unknown; native reported cost includes a true zero', () => {
    const snapshot = structuredClone(SNAPSHOT)
    snapshot.context.current_pct = null
    snapshot.engine_usage = { context: { window: 200_000 }, cost: { usd: 0 } }
    const values = metrics(snapshot)
    expect(values.find(row => row.label === 'Context')?.value).toBe('—')
    expect(values.find(row => row.label === 'Cost')?.value).toBe('$0.00')
    delete snapshot.engine_usage
    expect(metrics(snapshot).some(row => row.label === 'Cost')).toBe(false)
    expect(statusText(snapshot)).toContain('ctx —')
    expect(statusText({ schema_version: 1, empty: true, reason: 'Not yet', transcript: { path: '' } })).toContain('waiting')
  })

  test('partial evidence keeps unknown tool outcomes separate from zero observed failures in every view', () => {
    const original = JSON.stringify(PARTIAL_SNAPSHOT)
    const values = metrics(PARTIAL_SNAPSHOT)
    expect(values.find(row => row.label === 'Tokens')?.value).toBe('—')
    expect(values.find(row => row.label === 'Tools')?.detail).toContain('0 observed failures · 1 unknown outcome')
    expect(values.find(row => row.label === 'Cache read')?.value).toBe('—')
    expect(statusText(PARTIAL_SNAPSHOT)).toContain('0 observed failures · 1 unknown outcome')
    for (let tab = 0; tab < TABS.length; tab++) {
      const text = sections(PARTIAL_SNAPSHOT, tab, 32).flatMap(section => section.lines).join('\n')
      expect(text).not.toMatch(/\b(?:null|undefined|NaN)\b/)
    }
    const overview = sections(PARTIAL_SNAPSHOT, 0, 80)
    expect(overview.find(section => section.title === 'Health')?.lines.join('\n')).toContain('Tool Success unavailable')
    expect(overview.find(section => section.title === 'Data quality')?.lines.join('\n')).toContain('/obs view data_quality')
    expect(overview.find(section => section.title.startsWith('Trends'))?.lines[0]).toBe('Tokens/step ·▁·')
    expect(sections(PARTIAL_SNAPSHOT, 3, 32).some(section => section.lines.some(line => line.startsWith('Unavailable:')))).toBe(true)
    expect(sections(PARTIAL_SNAPSHOT, 4, 32)[0]?.lines.join('\n')).toContain('0 observed failures · 1 unknown outcome')
    expect(sections(PARTIAL_SNAPSHOT, 6, 32)[0]?.lines.join('\n')).toContain('— tokens')
    expect(JSON.stringify(PARTIAL_SNAPSHOT)).toBe(original)
  })

  test('all-unknown measurements and measured zeros produce different metrics and trend glyphs', () => {
    expect(metrics(UNKNOWN_SNAPSHOT).find(row => row.label === 'Tokens')?.value).toBe('—')
    expect(sections(UNKNOWN_SNAPSHOT, 0, 80).find(section => section.title.startsWith('Trends'))?.lines[0]).toBe('Tokens/step ·')
    expect(metrics(ZERO_SNAPSHOT).find(row => row.label === 'Tokens')?.value).toBe('0')
    expect(metrics(ZERO_SNAPSHOT).find(row => row.label === 'Tools')?.detail).toBe('100% success · 0 failed')
    expect(sections(ZERO_SNAPSHOT, 0, 80).find(section => section.title.startsWith('Trends'))?.lines[0]).toBe('Tokens/step ▁')
    const quality = sections(ZERO_SNAPSHOT, 0, 80).find(section => section.title === 'Data quality')?.lines.join('\n')
    expect(quality).toContain('Capture completeness unknown')
    expect(quality).not.toContain('unavailable')
  })

  test('partial evidence qualifies empty issue results and uses singular and plural counts', () => {
    const overview = sections(PARTIAL_SNAPSHOT, 0, 80)
    expect(overview.find(section => section.title === 'Top issues')?.lines).toEqual(['No observed issues.'])
    expect(sections(PARTIAL_SNAPSHOT, 2, 80)[0]?.lines).toEqual(['No observed issues.'])
    expect(overview.find(section => section.title === 'Data quality')?.lines[0]).toBe('Usage incomplete · 1 unknown tool outcome · 1 field unavailable · 1 field unknown · 1 field partial')
    const plural = { ...PARTIAL_SNAPSHOT, tools: { ...PARTIAL_SNAPSHOT.tools, unknown: 2 }, data_quality: { ...PARTIAL_SNAPSHOT.data_quality!, fields: { ...PARTIAL_SNAPSHOT.data_quality!.fields, 'tokens.output': { status: 'unavailable' as const }, duration: { status: 'partial' as const }, text: { status: 'unknown' as const } } } }
    expect(sections(plural, 0, 80).find(section => section.title === 'Data quality')?.lines[0]).toBe('Usage incomplete · 2 unknown tool outcomes · 2 fields unavailable · 2 fields unknown · 2 fields partial')
  })

  test('normal empty sessions explain how to get started while retaining specific error reasons', () => {
    const snapshot = { schema_version: 1 as const, empty: true as const, reason: 'no trajectory steps found (detected format: opencode)', transcript: { path: '' } }
    expect(sections(snapshot, 0, 80)[0]?.lines).toEqual(['Nothing to analyze yet. Start a turn or choose a captured session with /obs sessions.'])
    expect(sections({ ...snapshot, reason: 'Capture was removed.' }, 0, 80)[0]?.lines).toEqual(['Capture was removed.'])
  })

  test('sanitizes previews, labels, notes and empty reasons without mutating source', () => {
    const snapshot = structuredClone(SNAPSHOT)
    snapshot.issues[0]!.evidence = '\u001b[31mtool\u001b[0m\u0000\u001b]8;;https://example.org\u0007link\u001b]8;;\u0007'
    snapshot.trajviz.warnings = ['bad\u001b[2J note\ud800']
    const rendered = sections(snapshot, 2, 40).flatMap(section => section.lines).join('\n')
    expect(rendered).toContain('toollink')
    expect(rendered).not.toMatch(/[\u0000\u001b]/)
    expect(rendered).toContain('note�')
    expect(snapshot.issues[0]!.evidence).toContain('\u001b[31m')
    expect(sections({ schema_version: 1, empty: true, reason: '\u001b[2JEmpty', transcript: { path: '' } }, 0, 10)[0]?.lines).toEqual(['Empty'])
    expect(safeText('日本語\t😀\r\ntext')).toBe('日本語  😀\ntext')
    expect(safeText('x\u001b]title cut')).toBe('x')
  })

  test('renders wide view tables as row cards, preserving escaped pipes and source exports', () => {
    const table = '| step | tool_name | command |\n|---|---|---|\n| 1 | bash | cat a \\| sort |\n| 2 | read | a.py |'
    const result = { text: `Tools · 2 rows\n${table}\nFull view: /tmp/tools.json`, data: { inline: { table } } }
    const rendered = commandText(result)
    expect(rendered).toContain('step: 1\ntool name: bash\ncommand: cat a | sort')
    expect(rendered).toContain('step: 2\ntool name: read\ncommand: a.py')
    expect(rendered).toContain('Full view: /tmp/tools.json')
    expect(result.data.inline.table).toBe(table)
    expect(commandText({ text: '[{"step":1}]', data: result.data })).toBe('[{"step":1}]')
    expect(commandText({ text: table, data: { path: 'scorecard.md' } })).toContain('tool name: bash')
    const fenced = `Example:\n\`\`\`text\n${table}\n\`\`\``
    expect(commandText({ text: fenced, data: {} })).toBe(fenced)
    const nested = `\`\`\`\`markdown\n\`\`\`\n${table}\n\`\`\`\`\n`
    expect(commandText({ text: nested, data: {} })).toBe(nested)
  })
})

describe('bounded terminal figures', () => {
  test('keeps spikes and missing samples within requested columns', () => {
    expect(trend([1, 100, 1, 1, 1, 1], 2)).toHaveLength(2)
    expect(trend([1, 100, 1, 1, 1, 1], 2)).toStartWith('█')
    expect(trend([null, null], 10)).toBe('··')
    expect(trend([], 10)).toBe('No samples')
    expect(trend([0, 0], 0)).toBe('▁')
    expect(trend(Array.from({ length: 400 }, (_, i) => i), 100)).toHaveLength(48)
  })

  test('formats thresholds without impossible clock values', () => {
    expect(compact(999)).toBe('999')
    expect(compact(999.5)).toBe('1k')
    expect(compact(1_200_000)).toBe('1.2M')
    expect(duration(59.5)).toBe('1m00s')
    expect(duration(3599.5)).toBe('1h00m')
    expect(duration(null)).toBe('—')
    expect(percent(Number.NaN)).toBe('—')
  })
})
