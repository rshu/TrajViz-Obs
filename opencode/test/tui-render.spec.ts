import { expect, test } from 'bun:test'
import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

test('native update confirmation keeps its actions visible while long server details scroll', () => {
  const code = `
    import { testRender } from '@opentui/solid'
    import { createStore, produce } from 'solid-js/store'
    import { setup } from './src/tui.tsx'
    const noop=()=>{}, bindings=new Map(), frames=[]
    let renderDialog, closeDialog, confirmationLayer
    const plugin={id:'trajviz-obs',source:{type:'package',target:'trajviz-obs-opencode@latest',version:'controlled',outdated:true},features:{server:true,tui:true},state:{status:'active'}}
    const storage=(_key,{initial})=>{const [value,set]=createStore(initial);return[value,change=>set(produce(change))]}
    const context={
      options:{watch:false,statusline:false},location:{directory:'/a/long/remote/project/'.repeat(12)},storage:{store:storage,memory:storage},
      client:{rpc:()=>({events:{on:()=>noop}}),plugin:{list:async()=>({data:[plugin]}),check:async()=>({data:[plugin]}),update:async()=>{throw new Error('Render must not update a package')}},server:{info:async()=>({version:'2.2.5',urls:['https://controlled.example:4096']})}},
      data:{listen:()=>noop,session:{get:()=>undefined},location:{default:()=>undefined}},theme:{text:{base:'white',muted:'gray'}},
      keymap:{layer:factory=>{const layer=factory();for(const command of layer.commands??[])if(command.id)bindings.set(command.id,command);if(layer.commands?.some(command=>command.id==='trajviz-obs.update.accept'))confirmationLayer=layer}},
      ui:{router:{current:()=>({type:'home'})},panel:{current:()=>undefined,close:noop},slot:()=>noop,toast:{show:noop},dialog:{set:noop,
        show:(render,onClose)=>{const prior=closeDialog;renderDialog=render;closeDialog=onClose;prior?.()},
        clear:()=>{const prior=closeDialog;closeDialog=undefined;prior?.()},
      }},
    }
    const cleanup=setup(context)
    const running=bindings.get('trajviz-obs.command').run('update')
    for(let i=0;i<100&&!confirmationLayer?.enabled();i++)await Bun.sleep(1)
    if(!confirmationLayer?.enabled())throw new Error('No native confirmation opened')
    try{
      for(const width of [32,60,110])for(const height of [18,28]){
        const view=await testRender(()=>renderDialog(),{width,height,screenMode:'alternate-screen',consoleMode:'disabled'})
        try{
          await view.renderOnce();await view.flush()
          const first=view.captureCharFrame(),body=view.renderer.root.getChildren()[0],scroll=body.getChildren().find(child=>child.type==='scrollbox'||typeof child.scrollTo==='function')
          if(!scroll)throw new Error('Confirmation details need a scroll area')
          scroll.scrollTo(scroll.scrollHeight);await view.renderOnce();await view.flush()
          frames.push({width,height,first,end:view.captureCharFrame(),priority:confirmationLayer.priority})
        }finally{await view.dispose()}
      }
    }finally{cleanup();await running}
    console.log(JSON.stringify(frames))
  `
  const cwd = fileURLToPath(new URL('..', import.meta.url))
  const frames = JSON.parse(execFileSync(process.execPath, ['--preload', '@opentui/solid/preload', '--eval', code], { cwd, encoding: 'utf8', timeout: 15_000 })) as { width: number; height: number; first: string; end: string; priority: number }[]
  expect(frames).toHaveLength(6)
  for (const frame of frames) {
    expect(frame.priority).toBeGreaterThan(0)
    expect(frame.first).toContain('Update TrajViz Obs?')
    expect(frame.first).toContain('[u Update]'); expect(frame.first).toContain('[x Cancel]')
    expect(frame.end).toContain('[u Update]'); expect(frame.end).toContain('[x Cancel]')
    expect(frame.end).toContain('interrupted.')
  }
})

test('native wrapped pane navigation keeps every control above the session body', () => {
  // Use the production Solid transform in a fresh process, so logic-only tests
  // cannot cache a JSX module before the renderer's compiler is installed.
  const code = `
    import { testRender } from '@opentui/solid'
    import { createStore, produce } from 'solid-js/store'
    import { setup } from './src/tui.tsx'
    import { SNAPSHOT } from '../hooks/testdata/snapshot.ts'
    const frames = []
    for (const width of [120, 80, 60, 32]) for (const height of [18, 28]) for (let tab=0;tab<7;tab++) {
      let renderPane
      const noop = () => {}
      const panel = { name: 'trajviz-obs', sessionID: 'render-test', width, focused: true,
        focus: noop, close: noop, toggleFullscreen: noop }
      const storage = (key, { initial }) => {
        const value = structuredClone(initial)
        if (key === 'ui-v1') {
          value.tab=tab
          value.snapshots['render-test'] = {
            ...structuredClone(SNAPSHOT), title: 'A long controlled session title stays readable when navigation and titles wrap.',
          }
        }
        const [state, set] = createStore(value)
        return [state, change => set(produce(change))]
      }
      const context = {
        options: { watch: false, statusline: false }, storage: { store: storage, memory: storage },
        client: { rpc: () => ({ events: { on: () => noop } }) },
        data: { listen: () => noop }, theme: { text: { base: 'white', muted: 'gray' } },
        keymap: { layer: () => noop },
        ui: { panel: { close: noop }, slot: input => {
          if (input.append === 'session.panel') renderPane = input.render
          return noop
        } },
      }
      const cleanup = setup(context)
      const view = await testRender(() => renderPane(panel), {
        width, height, screenMode: 'alternate-screen', consoleMode: 'disabled',
      })
      try {
        await view.renderOnce()
        await view.flush()
        const body = view.renderer.root.getChildren()[0]
        const rows = body.getChildren().filter(child => ['obs-pane-tabs', 'obs-pane-actions'].includes(child.id))
        const scroll=body.getChildren().find(child=>child.id==='obs-pane-body')
        const first=view.captureCharFrame()
        scroll.scrollTo(scroll.scrollHeight)
        await view.renderOnce();await view.flush()
        frames.push({ width, height, tab, frame:first, end:view.captureCharFrame(), bodyHeight:scroll.height, rows: rows.map(row => ({
          id: row.id, top: row.y, bottom: row.y + row.height,
          children: row.getChildren().map(child => ({ top: child.y, bottom: child.y + child.height })),
        })) })
      } finally { cleanup(); await view.dispose() }
    }
    console.log(JSON.stringify(frames))
  `
  const cwd = fileURLToPath(new URL('..', import.meta.url))
  const output = execFileSync(process.execPath, ['--preload', '@opentui/solid/preload', '--eval', code], {
    cwd, encoding: 'utf8', timeout: 30_000,
  })
  const frames = JSON.parse(output) as { width: number; height:number; tab:number; frame: string; end:string; bodyHeight:number; rows: {
    id: string; top: number; bottom: number; children: { top: number; bottom: number }[];
  }[] }[]
  expect(frames).toHaveLength(56)
  for (const frame of frames) {
    expect(frame.rows.map(row => row.id)).toEqual(['obs-pane-tabs', 'obs-pane-actions'])
    for (const row of frame.rows) for (const child of row.children) {
      expect(child.top).toBeGreaterThanOrEqual(row.top)
      expect(child.bottom).toBeLessThanOrEqual(row.bottom)
    }
    expect(frame.rows[0]!.bottom).toBeLessThanOrEqual(frame.rows[1]!.top)
    expect(frame.frame).toContain('[x Close]')
    expect(frame.frame).toContain('Current session')
    if(frame.height===28)expect(frame.frame.replace(/\s/g, '')).toContain('A long controlled session title stays readable when navigation and titles wrap.'.replace(/\s/g, ''))
    expect(frame.bodyHeight).toBeGreaterThan(0)
    expect(frame.frame).toContain('↑↓ scroll')
    expect(frame.frame).not.toContain('Unable to draw')
    expect(frame.end).not.toContain('Unable to draw')
  }
})
