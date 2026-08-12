// ANSI escape sequences in a log line, rendered as style instead of shown as noise.
//
// Real producers emit them: gz writes `ESC[1;33mWarning [Utils.cc:132]ESC[0m …`, and because the
// ESC byte itself is invisible the panel showed a bare `[1;33m` in the middle of the message. The
// sequences are *kept* in `run_log` rather than stripped at ingest — they are what the producer
// actually wrote, SQL and `search_run_logs`' `grep` see the same bytes a `cat` of `system.log`
// would, and stripping is irreversible. Interpreting them is the reader's job, which is here.
//
// Deliberately a small subset: the SGR (`…m`) codes a log actually uses. Any other escape
// sequence is dropped rather than printed, because a cursor-move rendered as text is worse than a
// cursor-move ignored.

import type { CSSProperties } from 'react'

/** A run of text sharing one style. */
export interface AnsiSpan {
  text: string
  style: CSSProperties
}

/** The 16 basic colours, chosen to stay legible on **both** themes rather than to match a
 *  terminal exactly. Terminal palettes assume a known background; a panel has two, so pure
 *  yellow (invisible on white) and pure blue (invisible on black) are pulled toward mid-tones. */
const FG: Record<number, string> = {
  30: '#5a6b7a', // black -> slate, so it is not invisible on a dark panel
  31: '#d32f2f',
  32: '#2f7d31',
  33: '#b58900', // yellow -> amber; pure yellow cannot be read on white
  34: '#1f6feb',
  35: '#8250df',
  36: '#2e9599',
  37: '#8a949e', // white -> grey, same reason as black above
  90: '#78868f',
  91: '#e05252',
  92: '#3f9e41',
  93: '#c9a227',
  94: '#4a8ef0',
  95: '#9a6ae0',
  96: '#3fb0b0',
  97: '#b6bfc7',
}

/** `ESC [ … m` — the only sequence interpreted. Others are matched so they can be dropped. */
//: The ESC byte written as an escape, never as a literal: a raw control character in
//: source is invisible to the next reader, which is what `no-control-regex` guards against.
const ESC = '\u001b'

/** `ESC [ … m` — the only sequence interpreted; others are matched so they can be dropped.
 *  Built from a string rather than a regex literal, for the same reason. */
const CSI = new RegExp(`${ESC}\\[([0-9;]*)([A-Za-z])`, 'g')

/** 256-colour cube -> hex, for `38;5;N`. */
function cube256(n: number): string {
  if (n < 16) return FG[n < 8 ? n + 30 : n + 82] ?? 'inherit'
  if (n >= 232) {
        const v = 8 + (n - 232) * 10
    return `rgb(${v},${v},${v})`
  }
  const i = n - 16
  const level = (x: number) => (x === 0 ? 0 : 55 + x * 40)
  return `rgb(${level(Math.floor(i / 36))},${level(Math.floor((i % 36) / 6))},${level(i % 6)})`
}

function applySgr(style: CSSProperties, params: number[]): CSSProperties {
  const next: CSSProperties = { ...style }
  for (let i = 0; i < params.length; i++) {
    const p = params[i]
    if (p === 0) return {} // reset
    else if (p === 1) next.fontWeight = 700
    else if (p === 2) next.opacity = 0.7
    else if (p === 3) next.fontStyle = 'italic'
    else if (p === 4) next.textDecoration = 'underline'
    else if (p === 22) { delete next.fontWeight; delete next.opacity }
    else if (p === 23) delete next.fontStyle
    else if (p === 24) delete next.textDecoration
    else if (p === 39) delete next.color
    else if (FG[p]) next.color = FG[p]
    else if (p === 38) {
      // Extended colour: `38;5;N` (cube) or `38;2;r;g;b` (truecolour).
      if (params[i + 1] === 5) { next.color = cube256(params[i + 2] ?? 0); i += 2 }
      else if (params[i + 1] === 2) {
        next.color = `rgb(${params[i + 2] ?? 0},${params[i + 3] ?? 0},${params[i + 4] ?? 0})`
        i += 4
      }
    }
    // Background codes (40-49, 100-107) are parsed and ignored on purpose: a producer's chosen
    // background rarely survives contact with a themed panel, and one wrong pairing makes a line
    // unreadable rather than merely uncoloured.
  }
  return next
}

/** Split *text* into styled spans, dropping the escape sequences themselves. */
export function parseAnsi(text: string): AnsiSpan[] {
  if (!text.includes(ESC)) return [{ text, style: {} }]
  const spans: AnsiSpan[] = []
  let style: CSSProperties = {}
  let at = 0
  CSI.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = CSI.exec(text)) !== null) {
    if (m.index > at) spans.push({ text: text.slice(at, m.index), style })
    if (m[2] === 'm') {
      const params = m[1] === '' ? [0] : m[1].split(';').map((p) => Number(p) || 0)
      style = applySgr(style, params)
    }
    at = m.index + m[0].length
  }
  if (at < text.length) spans.push({ text: text.slice(at), style })
  return spans.filter((s) => s.text.length)
}

/** Strip every escape sequence — for measuring or matching against the visible text. */
export function stripAnsi(text: string): string {
  return text.includes(ESC) ? text.replace(CSI, '') : text
}
