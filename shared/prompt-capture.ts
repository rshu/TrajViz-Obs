import { spawn } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import path from 'node:path'

export type PromptObservation = {
  session_id: string
  stage: string
  kind?: string
  correlation?: unknown
  model?: unknown
  data: unknown
}
type Options = {
  adapter: 'pi' | 'claude-code' | 'opencode'
  bridge: string
  python: () => string | undefined
  hostVersion?: string
}
const MAX_RECORD_BYTES = 4 * 1024 * 1024
const MAX_QUEUE_BYTES = 8 * 1024 * 1024
const MAX_PENDING = 16

/** Local prompt evidence. Independent of product telemetry; never installs a runtime. */
export class PromptCapture {
  private readonly instance = randomUUID()
  private readonly root = process.env.TRAJVIZ_OBS_PROMPT_CAPTURE_DIR
  private sequence = 0
  private queue: string[] = []
  private bytes = 0
  private active?: Promise<void>
  private writing = false
  private child?: ReturnType<typeof spawn>
  private stopped = false
  private accepting = true
  private dropped = 0
  private failed = 0
  private lastSession?: string
  private closing?: Promise<void>
  constructor(private readonly options: Options) {}
  get enabled(): boolean {
    return !this.stopped && process.env.TRAJVIZ_OBS_PROMPT_CAPTURE === '1'
      && Boolean(this.root && path.isAbsolute(this.root) && !this.root.includes('\0'))
      && this.root === process.env.TRAJVIZ_OBS_PROMPT_CAPTURE_DIR
  }
  record(observation: PromptObservation): void {
    this.enqueue(observation)
  }
  private enqueue(observation: PromptObservation, terminal = false): void {
    if (!this.enabled || (!this.accepting && !terminal)) return
    this.lastSession = observation.session_id
    try {
      // Serialize now: SDKs may reuse or change their objects after the hook returns.
      const serialized = JSON.stringify({
        schema_version: 1, adapter: this.options.adapter,
        session_id: observation.session_id, instance_id: this.instance,
        record_id: randomUUID(), sequence: ++this.sequence,
        observed_at: new Date().toISOString(), host_version: this.options.hostVersion,
        stage: observation.stage, kind: observation.kind,
        correlation: observation.correlation, model: observation.model, data: observation.data,
        capture_health: { dropped: this.dropped, failed: this.failed },
      })
      const size = Buffer.byteLength(serialized)
      if (size > MAX_RECORD_BYTES || this.queue.length + (this.writing ? 1 : 0) >= MAX_PENDING || this.bytes + size > MAX_QUEUE_BYTES) {
        this.dropped++; return
      }
      this.queue.push(serialized); this.bytes += size
      this.start()
    } catch { this.dropped++ }
  }
  private start(): void {
    if (this.active || !this.queue.length || !this.enabled) return
    this.active = this.drain().catch(() => { this.failed++ }).finally(() => { this.active = undefined; this.start() })
  }
  private async drain(): Promise<void> {
    while (this.queue.length && this.enabled) {
      const serialized = this.queue.shift()!
      this.writing = true
      try { await this.write(serialized) }
      finally { this.bytes = Math.max(0, this.bytes - Buffer.byteLength(serialized)); this.writing = false }
    }
    if (!this.enabled) { this.queue = []; this.bytes = 0 }
  }
  private async write(serialized: string): Promise<void> {
    let python: string | undefined
    try { python = this.options.python() } catch { this.failed++; return }
    if (!python || !path.isAbsolute(python)) { this.failed++; return }
      await new Promise<void>(resolve => {
        try {
          const child = spawn(python, ['-I', path.join(this.options.bridge, 'prompt_capture.py'), 'write', '--root', this.root!], {
            stdio: ['pipe', 'ignore', 'ignore'], windowsHide: true,
            env: { ...(process.env.SystemRoot ? { SystemRoot: process.env.SystemRoot } : {}), TRAJVIZ_OBS_PROMPT_CAPTURE: '1' },
          })
          this.child = child
          const timer = setTimeout(() => { child.kill('SIGKILL') }, 3_000)
          timer.unref()
          let done = false
          const finish = (failed: boolean) => {
            if (done) return
            done = true; clearTimeout(timer)
            if (this.child === child) this.child = undefined
            if (failed) this.failed++
            resolve()
          }
          child.once('error', () => finish(true))
          child.once('close', code => finish(code !== 0))
          child.stdin.on('error', () => {})
          child.stdin.end(serialized)
        } catch { this.failed++; resolve() }
      })
  }
  async flush(): Promise<void> {
    this.start()
    while (this.active) await this.active
  }
  close(): Promise<void> {
    if (this.closing) return this.closing
    this.accepting = false
    return this.closing = (async () => {
      let timeout: ReturnType<typeof setTimeout> | undefined
      const drain = async () => {
        await this.flush()
        if (this.lastSession && (this.dropped || this.failed)) {
          // Reserve the final status after draining: a full queue must not hide overflow.
          this.enqueue({ session_id: this.lastSession, stage: 'capture.status', data: { dropped: this.dropped, failed: this.failed } }, true)
          await this.flush()
        }
      }
      try { await Promise.race([drain(), new Promise<void>(resolve => { timeout = setTimeout(resolve, 3_500) })]) }
      finally { clearTimeout(timeout); this.stopped = true; this.queue = []; this.bytes = 0; this.child?.kill('SIGKILL') }
    })()
  }
}
