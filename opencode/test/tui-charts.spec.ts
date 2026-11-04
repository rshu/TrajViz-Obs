import { expect, test } from 'bun:test'
import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

test('native charts preserve gaps, zeroes, colors and cell bounds across terminal modes', () => {
  const code = `
    import { testRender } from '@opentui/solid'
    import { TerminalCharts, terminalPalette, chartLabel } from './src/tui-charts.tsx'
    import { SNAPSHOT } from '../hooks/testdata/snapshot.ts'
    const theme = { text: {base:'#ffffff',muted:'#aaaaaa',feedback:{
      error:{base:'#ff0000'},warning:{base:'#ffff00'},success:{base:'#00ff00'},info:{base:'#0000ff'},
    }},hue:{accent:{500:'#ff00ff'}},categorical:[{500:'#0000ff'},{500:'#00ff00'},{500:'#ffff00'},{500:'#ff00ff'}] }
    const source = structuredClone(SNAPSHOT)
    source.context = {...source.context,window:100,window_source:'measured',window_note:null}
    source.series = {
      tokens:{steps:[0,1,2,3,4,5],fresh:[0,1,1000,null,10,1],cache_read:[0,0,2000,null,20,0],cache_write:[0,0,1000,null,1,0],output:[0,0,500,null,5,0]},
      durations:{steps:[0,1,2,3,4,5],seconds:[0,null,1,5,2,10],outliers:[5]},
      context:{turns:[0,1,2,3,4,5],occupancy:[0,10,null,40,30,80],compactions:[2]},
    }
    source.tools.by_tool=[{tool:'读取配置🧪\\nnext line',calls:1000,errors:1,unknown:0},{tool:'one',calls:1,errors:0,unknown:0}]
    source.swimlane={total_steps:6,lanes:[{id:'main',label:'开发代理🧪\\nnext line',segments:[{start:0,end:1},{start:4,end:5}]}]}
    source.phases=[{name:'verify fix',start:3,end:5},{name:'understand',start:0,end:2}]
    const before = JSON.stringify(source), results=[]
    for (const mode of ['color','no-color','dumb']) {
      delete process.env.NO_COLOR
      process.env.TERM=mode==='dumb'?'dumb':'xterm-256color'
      if(mode==='no-color')process.env.NO_COLOR='1'
      const palette=terminalPalette(theme)
      for (const width of [32,60]) for(const chart of ['overview','tools','agents','context']) {
        const view=await testRender(()=> <TerminalCharts snapshot={source} width={width} palette={palette} mode={chart}/>,{width,height:80,screenMode:'alternate-screen',consoleMode:'disabled'})
        try {
          await view.renderOnce();await view.flush()
          const overflow=[]
          function walk(node){if(Number.isFinite(node.x)&&Number.isFinite(node.width)&&node.x+node.width>width)overflow.push(node.id);for(const child of node.getChildren?.()??[])walk(child)}
          walk(view.renderer.root)
          results.push({mode,width,chart,frame:view.captureCharFrame(),overflow,colors:[...new Set(view.captureSpans().lines.flatMap(line=>line.spans.filter(span=>span.text.trim()).map(span=>span.fg.toInts().join(','))))]})
        }finally{await view.dispose()}
      }
    }
    if(JSON.stringify(source)!==before)throw new Error('Chart rendering mutated source')
    for(const width of [1,2,4,8,12])if(Bun.stringWidth(chartLabel('开发代理🧪\\nline',width))>width)throw new Error('Wide label overflow')
    console.log(JSON.stringify(results))
  `
  const output = execFileSync(process.execPath,['--preload','@opentui/solid/preload','--eval',code],{
    cwd:fileURLToPath(new URL('..',import.meta.url)),encoding:'utf8',timeout:30_000,
  })
  const frames = JSON.parse(output) as {mode:string;width:number;chart:string;frame:string;overflow:string[];colors:string[]}[]
  expect(frames).toHaveLength(24)
  for(const item of frames) {
    expect(item.overflow).toEqual([])
    expect(item.frame).not.toContain('\u001b')
    if(item.mode!=='color')expect(item.colors).toEqual(['255,255,255,255'])
    if(item.chart==='overview') {
      expect(item.frame).toContain('Tokens / step')
      expect(item.frame).toContain('Step time')
      expect(item.frame.replace(/\s/g,'')).toContain('Recordedsubagentwaitsexcludedwhenavailable;Timelineshowsfullstepduration.')
      expect(item.frame).toContain('?')
      expect(item.frame).toContain('_')
      expect(item.frame.replace(/\s/g,'')).toContain('Outliersteps:5')
      expect(item.frame).toContain('C')
      expect(item.frame.replace(/\s/g,'').match(/Latestphase:verifyfix/g)).toHaveLength(1)
      if(item.mode==='color')expect(item.colors).toContain('0,0,255,255')
    }
    if(item.chart==='tools')expect(item.frame.replace(/\s/g,'')).toContain('1calls')
    if(item.chart!=='overview')expect(item.frame).not.toContain('Latest phase:')
    if(item.chart==='agents')expect(item.frame.replace(/\s/g,'')).toContain('work-stepactivity')
    if(item.mode==='dumb')expect(item.frame).not.toMatch(/[█▁▂▃▄▅▆▇━]/)
  }
})

test('native dark and light palettes keep chart legends readable without inventing colors', () => {
  const code = `
    import { RGBA } from '@opentui/core'
    import { terminalPalette } from './src/tui-charts.tsx'
    // The dark 500 shades reproduce colors captured from the native 256-color
    // terminal. Other candidates remain supplied by the theme's public ramps.
    const rgb = value => RGBA.fromHex(value)
    const blue = {400:rgb('#5fafaf'),500:rgb('#005f87')}
    const purple = {400:rgb('#afafd7'),500:rgb('#5f5f87')}
    const make = light => ({background:{base:rgb(light?'#ffffff':'#121212')},
      text:{base:rgb(light?'#121212':'#eeeeee'),muted:rgb('#7f7f7f'),feedback:{
        info:{base:rgb('#5fafaf')},success:{base:rgb('#87d787')},warning:{base:rgb('#ffaf5f')},error:{base:rgb('#d75f87')},
      }},hue:{accent:purple},categorical:[blue,purple]})
    const lum = color => color.toInts().slice(0,3).map(c=>c/255).map(c=>c<=0.04045?c/12.92:((c+0.055)/1.055)**2.4).reduce((sum,c,index)=>sum+c*[0.2126,0.7152,0.0722][index],0)
    const contrast = (color,bg) => (Math.max(lum(color),lum(bg))+0.05)/(Math.min(lum(color),lum(bg))+0.05)
    delete process.env.NO_COLOR;process.env.TERM='xterm-256color'
    const results=[]
    for(const light of [false,true]) {
      const theme=make(light),before=JSON.stringify(theme),palette=terminalPalette(theme)
      const colors=[palette.category(0),palette.category(1),palette.tone('agent'),palette.tone('info'),palette.tone('success'),palette.tone('warning'),palette.tone('danger'),palette.muted]
      const allowed=[theme.text.base,theme.text.muted,...Object.values(theme.text.feedback).map(item=>item.base),...Object.values(blue),...Object.values(purple)]
      results.push({light,ratios:colors.map(color=>contrast(color,theme.background.base)),allFromTheme:colors.every(color=>allowed.includes(color)),unchanged:JSON.stringify(theme)===before,blue:palette.category(0).toInts()})
    }
    const noCandidate=make(false);noCandidate.categorical=[{500:rgb('#121212')}]
    results.push({fallback:terminalPalette(noCandidate).category(0)===noCandidate.text.base})
    const transparent=make(false);transparent.background.base=RGBA.fromInts(18,18,18,0)
    results.push({unknownBackgroundPreserved:terminalPalette(transparent).category(0)===blue[500]})
    for(const mode of ['no-color','dumb']) {
      delete process.env.NO_COLOR;process.env.TERM=mode==='dumb'?'dumb':'xterm-256color'
      if(mode==='no-color')process.env.NO_COLOR='1'
      const theme=make(false),palette=terminalPalette(theme)
      results.push({mode,baseOnly:[palette.category(0),palette.tone('agent'),palette.tone('danger'),palette.muted].every(color=>color===theme.text.base)})
    }
    console.log(JSON.stringify(results))
  `
  const output = execFileSync(process.execPath, ['--preload', '@opentui/solid/preload', '--eval', code], {
    cwd: fileURLToPath(new URL('..', import.meta.url)), encoding: 'utf8', timeout: 10_000,
  })
  const [dark, light, fallback, unknown, noColor, dumb] = JSON.parse(output)
  for (const result of [dark, light]) {
    expect(result.allFromTheme).toBe(true)
    expect(result.unchanged).toBe(true)
    for (const ratio of result.ratios) expect(ratio).toBeGreaterThanOrEqual(4.5)
    for (const ratio of result.ratios.slice(0, 3)) expect(ratio).toBeGreaterThanOrEqual(7)
  }
  expect(dark.blue).toEqual([95, 175, 175, 255])
  expect(light.blue).toEqual([0, 95, 135, 255])
  expect(fallback.fallback).toBe(true)
  expect(unknown.unknownBackgroundPreserved).toBe(true)
  expect(noColor.baseOnly).toBe(true)
  expect(dumb.baseOnly).toBe(true)
})
