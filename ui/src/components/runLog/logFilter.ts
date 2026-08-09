// The filter vocabulary, shared by the run-view panel, the Explorer tab and (by name) the
// `search_run_logs` MCP tool — so "warn only, container sut" means one thing in all three.

import { stripAnsi } from './ansi'
import type { LogRow } from './useRunLog'

/** Severities, least to most severe — the same three `common/log_summary.SEVERITIES` defines. */
export const SEVERITIES = ['other', 'warn', 'error'] as const
export type Severity = (typeof SEVERITIES)[number]

/** The severity control, as three states rather than a pair of booleans: `off` — no colour;
 *  `on` — colour warnings and errors; `only` — colour them and hide everything else.
 *
 *  One control for both severities, not one each. They are read together ("did anything go
 *  wrong here?"), colouring one without the other tells half the story, and two chips cost twice
 *  the bar for a distinction the row's own colour already makes. */
export type HighlightMode = 'off' | 'on' | 'only'

export interface LogFilter {
  /** Substring, or a regular expression when `regex` is set. */
  text: string
  regex: boolean
  /** Warnings and errors together — see {@link HighlightMode}. */
  highlight: HighlightMode
  /** Empty means "every one this log has", not "none" — an empty selection would show nothing
   *  and read as a broken filter. */
  containers: string[]
  nodes: string[]
  sources: string[]
}

export const EMPTY_FILTER: LogFilter = {
  text: '',
  regex: false,
  // On by default: the reason to open a log is usually that something went wrong, and a warning
  // nobody coloured reads exactly like the 500 informational lines around it.
  highlight: 'on',
  containers: [],
  nodes: [],
  sources: [],
}

/** One selectable value with how many rows carry it — the count is what makes the dropdown a
 *  summary of the run and not just a list of strings. */
export interface FacetValue {
  value: string
  count: number
}

export interface Facets {
  containers: FacetValue[]
  nodes: FacetValue[]
  sources: FacetValue[]
}

function tally(rows: readonly LogRow[], pick: (r: LogRow) => string): FacetValue[] {
  const counts = new Map<string, number>()
  for (const row of rows) {
    const key = pick(row)
    counts.set(key, (counts.get(key) ?? 0) + 1)
  }
  return [...counts.entries()]
    .map(([value, count]) => ({ value, count }))
    .sort((a, b) => b.count - a.count || a.value.localeCompare(b.value))
}

/** The facets of a loaded log. Free, because the rows are already in memory — and it lists
 *  only what this run actually produced, rather than every container a campaign could have. */
export function facetsOf(rows: readonly LogRow[]): Facets {
  return {
    containers: tally(rows, (r) => r.container),
    nodes: tally(rows, (r) => r.node),
    sources: tally(rows, (r) => r.source),
  }
}

/** A compiled filter: `test` decides one row, `invalidRegex` reports a pattern that will not
 *  compile so the UI can say so instead of silently matching nothing. */
export interface CompiledFilter {
  test: (row: LogRow) => boolean
  invalidRegex: boolean
  /** True when nothing is filtered out, so a caller can skip the pass entirely. */
  passthrough: boolean
}

export function compileFilter(filter: LogFilter): CompiledFilter {
  let re: RegExp | null = null
  let invalidRegex = false
  if (filter.text && filter.regex) {
    try {
      re = new RegExp(filter.text, 'i')
    } catch {
      invalidRegex = true
    }
  }
  const needle = filter.text.toLowerCase()
  const containers = new Set(filter.containers)
  const nodes = new Set(filter.nodes)
  const sources = new Set(filter.sources)

  // `only` hides everything that is neither a warning nor an error.
  const onlySeverities =
    filter.highlight === 'only' ? new Set<string>(['warn', 'error']) : new Set<string>()

  const passthrough =
    !filter.text &&
    !onlySeverities.size &&
    !containers.size &&
    !nodes.size &&
    !sources.size

  const test = (row: LogRow): boolean => {
    if (onlySeverities.size && !onlySeverities.has(row.severity)) return false
    if (containers.size && !containers.has(row.container)) return false
    if (nodes.size && !nodes.has(row.node)) return false
    if (sources.size && !sources.has(row.source)) return false
    if (!filter.text) return true
    if (invalidRegex) return false
    // Matched against the *visible* text: a gz line is stored as
    // `ESC[1;33mWarning [Utils.cc:132]ESC[0m`, so a search for `^Warning` would otherwise fail
    // on a line that plainly begins with the word.
    const message = stripAnsi(row.message)
    if (re) return re.test(message) || re.test(row.node)
    return message.toLowerCase().includes(needle) || row.node.toLowerCase().includes(needle)
  }
  return { test, invalidRegex, passthrough }
}

/** Which colour a row takes, given the control. `other` never colours, so the return type is
 *  narrower than {@link Severity} on purpose — there is no "other" colour to look up. */
export function highlightOf(row: LogRow, filter: LogFilter): 'warn' | 'error' | null {
  if (filter.highlight === 'off') return null
  if (row.severity === 'error') return 'error'
  if (row.severity === 'warn') return 'warn'
  return null
}
