import type { McpToolStat } from '@/lib/robovastClient'

// Ordering, bar geometry and the labels for the MCP tools panel. Pure, so the rules that
// decide what a reader sees are testable without a service or a render.

// Busiest first, and never-called tools last whatever else sorts them: they are in the
// answer so they can be *seen* to be unused, and burying them among ties by name would
// spread them through the list rather than collecting them at the end where that reads.
export function rankTools(tools: McpToolStat[]): McpToolStat[] {
  return [...tools].sort((a, b) => (b.calls - a.calls) || a.tool.localeCompare(b.tool))
}

export interface BarShares {
  ok: number
  failed: number
}

// The green/red split of one row's bar. Both shares are of the BUSIEST tool's call count,
// not of this tool's own: laid end to end they make a bar whose total length compares call
// counts across tools, with the red tail showing how much of that was failure. Normalising
// each row to itself would make every bar full-width and compare nothing.
export function barShares(stat: McpToolStat, maxCalls: number): BarShares {
  if (maxCalls <= 0) return { ok: 0, failed: 0 }
  const errors = Math.min(stat.errors, stat.calls)
  return { ok: (stat.calls - errors) / maxCalls, failed: errors / maxCalls }
}

export function maxCalls(tools: McpToolStat[]): number {
  return tools.reduce((most, t) => Math.max(most, t.calls), 0)
}

export function formatDurationMs(ms: number): string {
  if (!ms) return '—'
  if (ms < 1000) return `${Math.round(ms)} ms`
  return `${(ms / 1000).toFixed(ms < 10000 ? 2 : 1)} s`
}

// What the record actually covers, said out loud rather than left to be inferred from a
// short list -- the same duty `usageChart`'s coverage line has. Both bounds are named
// because either can be the one that bit: a busy day is cut by the row cap long before
// the age cap would have reached it.
export function retentionNote(maxAgeS: number, maxRows: number): string {
  const days = Math.round(maxAgeS / 86400)
  return `the last ${days} days or ${maxRows.toLocaleString()} calls, whichever is shorter`
}
