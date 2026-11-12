import { homedir } from 'node:os'
import path from 'node:path'

export function userStateRoot(): string {
  const local = process.platform === 'win32' ? process.env.LOCALAPPDATA : undefined
  return local || process.env.XDG_STATE_HOME || path.join(homedir(), '.local', 'state')
}
