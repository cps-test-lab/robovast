import { describe, expect, it } from 'vitest'
import { pageSql } from './useRunLog'

describe('pageSql', () => {
  // `seq` is the merge's own total order within a run: paging by LIMIT/OFFSET over anything with
  // ties would show some rows twice and drop others.
  it('orders run after run, each in merge order', () => {
    expect(pageSql('cfg', 3, undefined, 0)).toMatch(
      /FROM run_log WHERE config_name = 'cfg' AND run_id = 3 ORDER BY config_name, run_id, seq LIMIT 5000 OFFSET 0$/,
    )
  })

  it('pushes a severity floor into the scope', () => {
    expect(pageSql(undefined, undefined, ['error', 'warn'], 5000)).toContain(
      "WHERE severity IN ('error', 'warn') ORDER BY",
    )
  })
})
