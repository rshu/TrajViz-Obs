// `/obs` argument parsing: shell-like tokens, a subcommand, `--flags`,
// positionals (`k=v` stays positional: view options) and a `--` passthrough.

export type Parsed = {
  sub: string
  positional: string[]
  flags: Record<string, string | true>
  passthrough: string[]
}

/** Quote for this tokenizer, preserving real whitespace, quotes and backslashes. */
export function quoteArgument(value: string): string {
  return `"${value.replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`
}

/** An equals form keeps a leading dash inside a value, for the tokenizer and argparse. */
export function valueArgv(flag: string, value: string): string[] {
  return value.startsWith('-') ? [`--${flag}=${value}`] : [`--${flag}`, value]
}

export function quoteOption(flag: string, value: string): string {
  return `--${flag}${value.startsWith('-') ? '=' : ' '}${quoteArgument(value)}`
}

export function argvValue(argv: readonly string[], flag: string): string | undefined {
  let value: string | undefined
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === `--${flag}`) value = argv[++i]
    else if (argv[i]!.startsWith(`--${flag}=`)) value = argv[i]!.slice(flag.length + 3)
  }
  return value
}

/** Free questions keep apostrophes; quoted text and text after -- are literal. */
function analyzeParts(args: string) {
  const raw = args.trimStart().replace(/^analyze(?=\s|$)/, '')
  let question = '', session: string | undefined, captureSessionId: string | undefined
  let hasSession = false, hasCapture = false, quote: string | undefined
  for (let i = 0; i < raw.length;) {
    const ch = raw[i]!
    const boundary = i === 0 || /\s/.test(raw[i - 1]!)
    if (quote) {
      question += ch; i++
      if (ch === '\\' && quote === '"' && i < raw.length) question += raw[i++]
      else if (ch === quote) quote = undefined
      continue
    }
    if (boundary && /^--(?=\s|$)/.test(raw.slice(i))) { question += raw.slice(i + 2); break }
    if (boundary) {
      const flag = /^--?(session-id|session)(?=\s|=|$)/.exec(raw.slice(i))
      if (flag) {
        if (flag[1] === 'session') hasSession = true
        else hasCapture = true
        const value = /^--?(?:session-id|session)(?:=|\s+)("(?:\\.|[^"\\])*"|'[^']*'|[^\s]+)(?=\s|$)/.exec(raw.slice(i))
        const decoded = value ? tokenize(value[1]!)[0] : undefined
        if (value && decoded !== undefined && (!decoded.startsWith('-') || /^--?(?:session-id|session)=/.test(value[0]))) {
          if (flag[1] === 'session') session = decoded
          else captureSessionId = decoded
          i += value[0].length; continue
        }
      }
      if (ch === '"' || ch === "'") quote = ch
    }
    question += ch; i++
  }
  return { question: question.trim() || undefined, session, captureSessionId, hasSession, hasCapture }
}

export function analyzeSelection(args: string): { question: string | undefined; session: string | undefined; captureSessionId?: string } {
  const parsed = analyzeParts(args)
  return { question: parsed.question, session: parsed.session, ...(parsed.captureSessionId === undefined ? {} : { captureSessionId: parsed.captureSessionId }) }
}

export function selectionError(flags: Parsed['flags'], rawAnalyze?: string): string | undefined {
  const selected = rawAnalyze ? analyzeParts(rawAnalyze) : undefined
  const session = selected ? selected.session : flags.session
  const capture = selected ? selected.captureSessionId : flags['session-id']
  const hasSession = selected ? selected.hasSession : flags.session !== undefined
  const hasCapture = selected ? selected.hasCapture : flags['session-id'] !== undefined
  if (hasSession && (typeof session !== 'string' || !session.trim())) return '--session needs a session id, prefix, or transcript path.'
  if (hasCapture && (typeof capture !== 'string' || !capture.trim())) return '--session-id needs an exact session id inside the capture.'
  if (hasCapture && !hasSession) return '--session-id requires --session <capture path>.'
  if (typeof capture === 'string' && (capture.length > 4096 || capture.includes('\u0000'))) return '--session-id must be an exact captured session id up to 4096 characters, without NUL.'
  return undefined
}

/** Every subcommand the grammar names, in the help table's order. */
export const SUBCOMMANDS: readonly string[] = [
  'overview', 'pane', 'issues', 'health', 'patterns', 'agents', 'context', 'timeline', 'view',
  'json', 'bundle', 'schemas',
  'dashboard', 'converge',
  'compare', 'scorecard', 'converge-cli', 'labels',
  'analyze', 'fixes', 'label',
  'attribution', 'collect', 'sessions', 'watch', 'statusline', 'setup', 'doctor', 'help',
]

const DEFAULT_SUB = 'overview'
/** Flags that never consume the next positional, regardless of their order. */
export const BOOLEAN_FLAGS: readonly string[] = ['with-trajectory', 'json', 'fuzzy-commands', 'reverse', 'no-self', 'assistant-only']

/**
 * Splits as a POSIX shell would: whitespace separates, single quotes keep
 * everything, double quotes keep everything but `\"` and `\\`, a backslash
 * outside quotes escapes the next character. An unclosed quote runs to the end.
 */
export function tokenize(input: string): string[] {
  const out: string[] = []
  let cur = ''
  let inToken = false
  let quote: '"' | "'" | null = null
  for (let i = 0; i < input.length; i++) {
    const ch = input[i] as string
    if (quote === "'") {
      if (ch === "'") quote = null
      else cur += ch
    } else if (quote === '"') {
      if (ch === '"') quote = null
      else if (ch === '\\' && (input[i + 1] === '"' || input[i + 1] === '\\')) cur += input[++i]
      else cur += ch
    } else if (ch === '\\' && i + 1 < input.length) {
      cur += input[++i]
      inToken = true
    } else if (ch === '"' || ch === "'") {
      quote = ch
      inToken = true
    } else if (/\s/.test(ch)) {
      if (inToken) out.push(cur)
      cur = ''
      inToken = false
    } else {
      cur += ch
      inToken = true
    }
  }
  if (inToken) out.push(cur)
  return out
}

const isFlag = (t: string): boolean => t.length > 1 && t.startsWith('-') && t !== '--'

/** One argument after the subcommand: a flag with its value, or a positional. */
type Arg = { flag: string; value: string | true } | { positional: string }

type Walked = { sub: string; args: Arg[]; passthrough: string[] }

/**
 * The grammar `parseArgs` and `flagValues` share. `sub` is the first token
 * unless it is a flag (then `overview`). `--k=v`, `--k v` (the value is the
 * next token when it is not a flag) and `--flag` are flags, in their order;
 * `-o PATH` reads the same way. Everything after `--` is `passthrough`, untouched.
 */
function walk(input: string): Walked {
  const tokens = tokenize(input)
  const out: Walked = { sub: DEFAULT_SUB, args: [], passthrough: [] }
  let i = 0
  if (tokens.length > 0 && !isFlag(tokens[0] as string) && tokens[0] !== '--') {
    out.sub = tokens[0] as string
    i = 1
  }
  for (; i < tokens.length; i++) {
    const t = tokens[i] as string
    if (t === '--') {
      out.passthrough = tokens.slice(i + 1)
      break
    }
    if (!isFlag(t)) {
      out.args.push({ positional: t })
      continue
    }
    const body = t.replace(/^-{1,2}/, '')
    const eq = body.indexOf('=')
    if (eq >= 0) {
      out.args.push({ flag: body.slice(0, eq), value: body.slice(eq + 1) })
      continue
    }
    const next = tokens[i + 1]
    if (!BOOLEAN_FLAGS.includes(body) && next !== undefined && next !== '--' && (!isFlag(next) || /^-\d+(?:\.\d+)?$/.test(next))) {
      out.args.push({ flag: body, value: next })
      i++
    } else {
      out.args.push({ flag: body, value: true })
    }
  }
  return out
}

/**
 * The parse every handler reads: `flags` keeps one value per name (the last
 * one wins), `positional` the rest in order. A handler that takes a flag
 * more than once (`json --view ID`…) reads the repeats with `flagValues`.
 */
export function parseArgs(input: string): Parsed {
  const { sub, args, passthrough } = walk(input)
  const parsed: Parsed = { sub, positional: [], flags: {}, passthrough }
  for (const a of args) {
    if ('flag' in a) parsed.flags[a.flag] = a.value
    else parsed.positional.push(a.positional)
  }
  return parsed
}

/**
 * Every value of `--name` in `input`, in order (`true` where it carries none),
 * for a flag the grammar lets repeat. `parseArgs` keeps only the last.
 */
export function flagValues(input: string, name: string): (string | true)[] {
  const values: (string | true)[] = []
  for (const a of walk(input).args) if ('flag' in a && a.flag === name) values.push(a.value)
  return values
}

/**
 * Every token after the subcommand, untouched, for a pass-through to one of
 * TrajViz's own CLIs (`converge-cli`, `collect`): a leading `--` is dropped,
 * nothing else is read as `/obs`'s own.
 */
export function passArgs(input: string): string[] {
  const rest = tokenize(input).slice(1)
  return rest[0] === '--' ? rest.slice(1) : rest
}

function editDistance(a: string, b: string): number {
  const prev: number[] = Array.from({ length: b.length + 1 }, (_, j) => j)
  for (let i = 1; i <= a.length; i++) {
    let diag = prev[0] as number
    prev[0] = i
    for (let j = 1; j <= b.length; j++) {
      const tmp = prev[j] as number
      const cost = a[i - 1] === b[j - 1] ? 0 : 1
      prev[j] = Math.min(tmp + 1, (prev[j - 1] as number) + 1, diag + cost)
      diag = tmp
    }
  }
  return prev[b.length] as number
}

/** The closest subcommand within an edit distance of 2 (ties: the earlier in SUBCOMMANDS), else undefined. */
export function suggest(name: string): string | undefined {
  let best: string | undefined
  let bestDistance = 3
  for (const sub of SUBCOMMANDS) {
    const d = editDistance(name.toLowerCase(), sub)
    if (d < bestDistance) {
      best = sub
      bestDistance = d
    }
  }
  return best
}
