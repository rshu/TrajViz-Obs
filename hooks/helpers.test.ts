import { expect, test } from 'claude-code/testing'
import type { EngineInterface, RenderElement } from 'claude-code'
import { artifactsDir, cacheRoot, configDir, isSsh, isWindows, projectSlug, venvPython } from './lib/paths'
import { clip, displayText, fmtCompact, fmtInt, fmtPct, fmtSecs, safeRenderText } from './lib/format'
import { bar, fit, sparkline } from './lib/sparkline'
import { DEFAULT_COLOR, encodeCells, sparklineRaster, stackedRaster, swimlaneRaster } from './lib/raster'

/** A `$` whose only facet is `env.get` over the given variables. */
const envOf = (vars: Record<string, string>): EngineInterface =>
  ({ env: { get: async (name: string) => vars[name] } }) as unknown as EngineInterface

/** The cells back from the Raster encoding: `[cp, fg, bg]` per cell. */
function decodeCells(b64: string): [number, number, number][] {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
  const clean = b64.replace(/=+$/, '')
  const bytes: number[] = []
  let bits = 0
  let acc = 0
  for (const ch of clean) {
    acc = (acc << 6) | alphabet.indexOf(ch)
    bits += 6
    if (bits >= 8) {
      bits -= 8
      bytes.push((acc >> bits) & 0xff)
    }
  }
  const words: number[] = []
  for (let i = 0; i + 3 < bytes.length; i += 4) {
    words.push(((bytes[i] as number) | ((bytes[i + 1] as number) << 8) | ((bytes[i + 2] as number) << 16) | ((bytes[i + 3] as number) << 24)) >>> 0)
  }
  const cells: [number, number, number][] = []
  for (let i = 0; i + 2 < words.length; i += 3) cells.push([words[i] as number, words[i + 1] as number, words[i + 2] as number])
  return cells
}

const glyph = (cells: [number, number, number][], columns: number, col: number, row: number): string =>
  String.fromCodePoint(cells[row * columns + col]![0])

test('display text removes terminal sequences and controls while retaining readable Unicode and lines', () => {
  expect(displayText('\u001b[31m失败 ✓\u001b[0m\r\nnext\tcolumn\rprogress\u0000\u0007\u007f')).toBe('失败 ✓\nnext\tcolumn\nprogress')
  expect(displayText('\u001b]8;;https://example.test\u0007link\u001b]8;;\u001b\\')).toBe('link')
  expect(displayText('\u009b32mgreen\u009b0m\u009d0;title\u009c text')).toBe('green text')
  expect(displayText('before\u001bPdevice command\u001b\\ after\u001b(B')).toBe('before after')
  expect(displayText('preview\u001b[38;2;12')).toBe('preview')
  expect(displayText('preview\u001b]8;;truncated')).toBe('preview')
  expect(displayText('plain 👩‍💻 café\n\tunchanged')).toBe('plain 👩‍💻 café\n\tunchanged')
})

test('render sanitation preserves action identity and source text instead of changing stored data', () => {
  const press = { plugin: 'trajviz-obs', handle: 42 }
  const tree: RenderElement = { type: 'Box', props: {}, children: [
    { type: 'Text', props: {}, children: ['\u001b[31merror\u001b[0m'] },
    { type: 'Markdown', props: { text: '| file |\n| a\u0000.py |' } },
    { type: 'Code', props: { source: 'one\rtwo' } },
    { type: 'Button', props: { key: 'action', label: 'Open\u0007', hotkey: 'd' }, press },
  ] }
  const clean = safeRenderText(tree)
  const children = 'children' in clean ? clean.children : undefined
  expect(children?.[0]).toMatchObject({ children: ['error'] })
  expect(children?.[1]).toMatchObject({ props: { text: '| file |\n| a.py |' } })
  expect(children?.[2]).toMatchObject({ props: { source: 'one\ntwo' } })
  expect(children?.[3]).toMatchObject({ props: { key: 'action', label: 'Open', hotkey: 'd' }, press })
  expect((children?.[3] as { press: unknown }).press).toBe(press)
  expect(tree.children?.[0]).toMatchObject({ children: ['\u001b[31merror\u001b[0m'] })
})

test('slug, formats, sparkline', () => {
  expect(projectSlug('/Users/me/a.b_c')).toBe('-Users-me-a-b-c')
  expect(fmtCompact(1234567)).toBe('1.2M')
  expect(fmtPct(null)).toBe('?')
  expect(sparkline([0, 1, null, 8], 4)).toBe('▁▂·█')
  expect(fit([1, 9, 2, 8], 2)).toEqual([9, 8])

  expect(fmtInt(1234567)).toBe('1,234,567')
  expect(fmtInt(-1234)).toBe('-1,234')
  expect(fmtInt(999)).toBe('999')
  expect(fmtCompact(999)).toBe('999')
  expect(fmtCompact(1200)).toBe('1.2k')
  expect(fmtCompact(12345)).toBe('12k')
  expect(fmtCompact(3400000)).toBe('3.4M')
  expect(fmtCompact(2500000000)).toBe('2.5G')
  expect(fmtSecs(null)).toBe('?')
  expect(fmtSecs(3.14)).toBe('3.1s')
  expect(fmtSecs(45.6)).toBe('46s')
  expect(fmtSecs(245)).toBe('4m05s')
  expect(fmtSecs(3725)).toBe('1h02m')
  // Rounding carries into the next unit instead of printing 60 seconds or minutes.
  expect(fmtSecs(59.4)).toBe('59s')
  expect(fmtSecs(59.5)).toBe('1m00s')
  expect(fmtSecs(119.6)).toBe('2m00s')
  expect(fmtSecs(3599.6)).toBe('1h00m')
  expect(fmtSecs(3599.4)).toBe('59m59s')
  expect(fmtPct(12.345)).toBe('12%')
  expect(fmtPct(12.345, 1)).toBe('12.3%')
  expect(clip('abcdef', 10)).toBe('abcdef')
  expect(clip('abcdef', 4)).toBe('abc…')
  expect(clip('abcdef', 0)).toBe('')

  expect(fit([1, 2, 3], 5)).toEqual([1, 2, 3])
  expect(fit([null, null, 4, null], 2)).toEqual([null, 4])
  expect(sparkline([], 10)).toBe('')
  expect(sparkline([0, 0], 2)).toBe('▁▁')
  expect(sparkline([0, 1, 2, 3, 4, 5, 6, 7], 8)).toBe('▁▂▃▄▅▆▇█')
  expect(sparkline([9, 1, 2, 9], 2)).toBe('██')
  expect(bar(4, 8, 8)).toBe('████    ')
  expect(bar(8, 8, 4)).toBe('████')
  expect(bar(0, 8, 4)).toBe('    ')
  expect(bar(3, 8, 4).length).toBe(4)
  expect(bar(3, 0, 4)).toBe('    ')
})

test('raster cells pack as LE u32 triplets', () => {
  // Uint32Array.of(0x2588, 0xff8800, 0x01000000) as bytes, standard base64 (the RasterProps example).
  expect(encodeCells([{ cp: 0x2588, fg: 0xff8800, bg: DEFAULT_COLOR }])).toBe('iCUAAACI/wAAAAAB')
  expect(DEFAULT_COLOR).toBe(0x01000000)
  expect(encodeCells([])).toBe('')

  const r = sparklineRaster([0, 4, 8], 3, 2, 0x00aaff)
  expect(r.columns).toBe(3)
  expect(r.rows).toBe(2)
  const cells = decodeCells(r.cells)
  expect(cells.length).toBe(6)
  // Column 0 (value 0): a hairline on the bottom row, nothing above.
  expect(glyph(cells, 3, 0, 1)).toBe('▁')
  expect(glyph(cells, 3, 0, 0)).toBe(' ')
  // Column 1 (half): a full block on the bottom row, nothing above.
  expect(glyph(cells, 3, 1, 1)).toBe('█')
  expect(glyph(cells, 3, 1, 0)).toBe(' ')
  // Column 2 (max): full blocks on both rows, in the given colour over the default background.
  expect(glyph(cells, 3, 2, 1)).toBe('█')
  expect(glyph(cells, 3, 2, 0)).toBe('█')
  expect(cells[2]).toEqual([0x2588, 0x00aaff, DEFAULT_COLOR])
  // A level between two rows is a partial block on its top row.
  const p = decodeCells(sparklineRaster([8, 5], 2, 2, 0x00aaff).cells)
  expect(glyph(p, 2, 1, 1)).toBe('█')
  expect(glyph(p, 2, 1, 0)).toBe('▂')
  // Nulls are a middle dot on the bottom row; a blank cell is a space in the default colours.
  const n = decodeCells(sparklineRaster([null, 1], 2, 1, 0x00aaff).cells)
  expect(n[0]).toEqual([0xb7, DEFAULT_COLOR, DEFAULT_COLOR])
  // More values than columns are fitted (bucket max); fewer leave the rest blank.
  const f = decodeCells(sparklineRaster([9, 1, 2, 9], 2, 1, 0x00aaff).cells)
  expect(glyph(f, 2, 0, 0)).toBe('█')
  expect(glyph(f, 2, 1, 0)).toBe('█')
  const w = sparklineRaster([1], 3, 1, 0x00aaff)
  expect(decodeCells(w.cells)[2]).toEqual([0x20, DEFAULT_COLOR, DEFAULT_COLOR])
})

test('stacked and swimlane rasters', () => {
  const s = stackedRaster([{ values: [2, 4], fg: 0x111111 }, { values: [2, 0], fg: 0x222222 }], 2, 4)
  expect(s.columns).toBe(2)
  expect(s.rows).toBe(4)
  const sc = decodeCells(s.cells)
  expect(sc.length).toBe(8)
  // Column 0: total 4 of max 4 = all four rows, the lower two in the first colour, the upper two in the second.
  expect(sc[3 * 2 + 0]).toEqual([0x2588, 0x111111, DEFAULT_COLOR])
  expect(sc[2 * 2 + 0]).toEqual([0x2588, 0x111111, DEFAULT_COLOR])
  expect(sc[1 * 2 + 0]).toEqual([0x2588, 0x222222, DEFAULT_COLOR])
  expect(sc[0 * 2 + 0]).toEqual([0x2588, 0x222222, DEFAULT_COLOR])
  // Column 1: total 4, all in the first colour.
  expect(sc[0 * 2 + 1]).toEqual([0x2588, 0x111111, DEFAULT_COLOR])
  expect(sc[3 * 2 + 1]).toEqual([0x2588, 0x111111, DEFAULT_COLOR])
  // A partial top shows as a partial block in the top segment's colour.
  const h = decodeCells(stackedRaster([{ values: [4, 1], fg: 0x111111 }], 2, 2).cells)
  expect(glyph(h, 2, 1, 1)).toBe('▄')
  expect(glyph(h, 2, 1, 0)).toBe(' ')
  // Empty input is still a legal Raster (rows and columns at least 1).
  const e = stackedRaster([], 0, 0)
  expect(e.columns).toBe(1)
  expect(e.rows).toBe(1)
  expect(decodeCells(e.cells).length).toBe(1)

  const l = swimlaneRaster(
    [
      { segments: [{ start: 0, end: 9 }], fg: 0xaaaaaa },
      { segments: [{ start: 5, end: 7 }], fg: 0xbbbbbb },
    ],
    10,
    10,
  )
  expect(l.columns).toBe(10)
  expect(l.rows).toBe(2)
  const lc = decodeCells(l.cells)
  expect(lc.length).toBe(20)
  for (let c = 0; c < 10; c++) expect(lc[c]).toEqual([0x2588, 0xaaaaaa, DEFAULT_COLOR])
  expect(glyph(lc, 10, 4, 1)).toBe(' ')
  expect(glyph(lc, 10, 5, 1)).toBe('█')
  expect(glyph(lc, 10, 7, 1)).toBe('█')
  expect(glyph(lc, 10, 8, 1)).toBe(' ')
  expect(lc[15]).toEqual([0x2588, 0xbbbbbb, DEFAULT_COLOR])
  // A one-step segment is at least one column wide even when steps outnumber columns.
  const t = decodeCells(swimlaneRaster([{ segments: [{ start: 50, end: 50 }], fg: 0x1 }], 100, 10).cells)
  expect(t.filter(c => c[0] === 0x2588).length).toBe(1)
  expect(swimlaneRaster([], 0, 5).rows).toBe(1)
  // Fewer steps than columns: a segment over every step reaches the right edge,
  // and adjacent one-step segments leave no gap between them.
  const w = decodeCells(
    swimlaneRaster(
      [
        { segments: [{ start: 0, end: 4 }], fg: 0x1 },
        { segments: [{ start: 0, end: 0 }, { start: 1, end: 1 }], fg: 0x2 },
        { segments: [{ start: 3, end: 3 }], fg: 0x3 },
      ],
      5,
      10,
    ).cells,
  )
  for (let c = 0; c < 10; c++) expect(w[c]).toEqual([0x2588, 0x1, DEFAULT_COLOR])
  for (let c = 0; c < 4; c++) expect(glyph(w, 10, c, 1)).toBe('█')
  for (let c = 4; c < 10; c++) expect(glyph(w, 10, c, 1)).toBe(' ')
  for (let c = 0; c < 10; c++) expect(glyph(w, 10, c, 2)).toBe(c === 6 || c === 7 ? '█' : ' ')
  // Exact ratios stay exact: 3 of 10 steps over 10 columns end on column 2, not 3.
  const x = decodeCells(swimlaneRaster([{ segments: [{ start: 0, end: 2 }], fg: 0x1 }], 10, 10).cells)
  expect(x.filter(c => c[0] === 0x2588).length).toBe(3)
})

test('paths from the environment', async () => {
  const posix = envOf({ HOME: '/Users/me', XDG_CACHE_HOME: '/Users/me/xdg/' })
  expect(await cacheRoot(posix)).toBe('/Users/me/xdg/trajviz-obs')
  expect(await cacheRoot(envOf({ HOME: '/Users/me' }))).toBe('/Users/me/.cache/trajviz-obs')
  expect(await cacheRoot(envOf({ HOME: '/Users/me', XDG_CACHE_HOME: '' }))).toBe('/Users/me/.cache/trajviz-obs')
  // Windows: HOME wins over LOCALAPPDATA (Git Bash, MSYS, CI images); LOCALAPPDATA only when HOME is unset.
  const win = envOf({ OS: 'Windows_NT', LOCALAPPDATA: 'C:\\Users\\me\\AppData\\Local' })
  const winHome = envOf({ OS: 'Windows_NT', LOCALAPPDATA: 'C:\\Users\\me\\AppData\\Local', HOME: 'C:\\Users\\me' })
  expect(await isWindows(win)).toBe(true)
  expect(await isWindows(posix)).toBe(false)
  expect(await cacheRoot(win)).toBe('C:\\Users\\me\\AppData\\Local/trajviz-obs')
  expect(await cacheRoot(winHome)).toBe('C:\\Users\\me/.cache/trajviz-obs')
  expect(await cacheRoot(envOf({ OS: 'Windows_NT', LOCALAPPDATA: 'C:\\L', HOME: 'C:\\Users\\me', XDG_CACHE_HOME: 'D:\\xdg' }))).toBe('D:\\xdg/trajviz-obs')
  expect(await cacheRoot(envOf({ OS: 'Windows_NT' }))).toBe('./.cache/trajviz-obs')
  expect(await venvPython(win)).toBe('C:\\Users\\me\\AppData\\Local/trajviz-obs/venv/Scripts/python.exe')
  expect(await venvPython(winHome)).toBe('C:\\Users\\me/.cache/trajviz-obs/venv/Scripts/python.exe')
  expect(await venvPython(posix)).toBe('/Users/me/xdg/trajviz-obs/venv/bin/python')
  expect(await artifactsDir(posix, 'abc-123')).toBe('/Users/me/xdg/trajviz-obs/sessions/abc-123')
  expect(await configDir(posix)).toBe('/Users/me/.claude')
  expect(await configDir(envOf({ HOME: '/Users/me', CLAUDE_CONFIG_DIR: '/cfg/' }))).toBe('/cfg')
  expect(await isSsh(posix)).toBe(false)
  expect(await isSsh(envOf({ SSH_TTY: '/dev/ttys001' }))).toBe(true)
  expect(await isSsh(envOf({ SSH_CONNECTION: '1.2.3.4 1 5.6.7.8 22' }))).toBe(true)
})
