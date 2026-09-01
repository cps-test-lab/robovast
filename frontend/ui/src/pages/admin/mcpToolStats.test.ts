import { describe, expect, it } from 'vitest'
import { barShares, formatDurationMs, maxCalls, rankTools, retentionNote } from './mcpToolStats'
import type { McpToolStat } from '@/lib/robovastClient'

function stat(tool: string, calls: number, errors = 0): McpToolStat {
  return { tool, calls, errors, mean_ms: 0, max_ms: 0, last_at: null }
}

describe('rankTools', () => {
  it('puts the busiest first', () => {
    const ranked = rankTools([stat('a', 2), stat('b', 9), stat('c', 5)])
    expect(ranked.map((t) => t.tool)).toEqual(['b', 'c', 'a'])
  })

  it('collects the never-called tools at the end', () => {
    const ranked = rankTools([stat('zzz', 0), stat('aaa', 0), stat('mid', 3)])
    expect(ranked.map((t) => t.tool)).toEqual(['mid', 'aaa', 'zzz'])
  })

  it('does not mutate its input', () => {
    const tools = [stat('a', 1), stat('b', 2)]
    rankTools(tools)
    expect(tools.map((t) => t.tool)).toEqual(['a', 'b'])
  })
})

describe('barShares', () => {
  it('scales both segments against the busiest tool, not the row', () => {
    expect(barShares(stat('a', 25, 5), 100)).toEqual({ ok: 0.2, failed: 0.05 })
  })

  it('gives a full-width bar only to the busiest tool', () => {
    const shares = barShares(stat('a', 100, 0), 100)
    expect(shares.ok + shares.failed).toBe(1)
  })

  it('survives an empty log', () => {
    expect(barShares(stat('a', 0), 0)).toEqual({ ok: 0, failed: 0 })
  })
})

describe('maxCalls', () => {
  it('is zero for an empty list', () => {
    expect(maxCalls([])).toBe(0)
  })

  it('is the busiest tool count', () => {
    expect(maxCalls([stat('a', 3), stat('b', 7)])).toBe(7)
  })
})

describe('formatDurationMs', () => {
  it('reads in ms below a second and seconds above', () => {
    expect(formatDurationMs(42.4)).toBe('42 ms')
    expect(formatDurationMs(1500)).toBe('1.50 s')
    expect(formatDurationMs(65000)).toBe('65.0 s')
  })

  it('marks a tool that has never been called rather than claiming 0 ms', () => {
    expect(formatDurationMs(0)).toBe('—')
  })
})

describe('retentionNote', () => {
  it('names both bounds, because either can be the one that bit', () => {
    const note = retentionNote(30 * 24 * 3600, 200000)
    expect(note).toContain('30 days')
    // The grouping separator is the reader's locale, so the assertion is on the number.
    expect(note).toMatch(/200[.,\s]000/)
  })
})
