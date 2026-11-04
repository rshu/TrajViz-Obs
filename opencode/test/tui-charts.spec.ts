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
