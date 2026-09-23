import { describe, expect, it } from 'vitest'
import { browseSql, columnsPlaceholder, qualifiedName, tableLabel } from './dataTables'
import type { DataTable } from './robovastClient'

const table = (over: Partial<DataTable>): DataTable => ({
  schema: 'main', table: 't', columns: [], rows: null, kind: 'table', runs: null, built: null,
  failed: {}, description: '', column_notes: {}, ...over,
})

describe('dataTables', () => {
  // Views and tables are in `main` and queried unqualified; the record is always `campaign.`.
  it('qualifies only what is outside main', () => {
    expect(qualifiedName(table({ table: 'run_view' }))).toBe('run_view')
    expect(qualifiedName(table({ schema: 'campaign', table: 'run' }))).toBe('campaign.run')
    expect(browseSql(table({ schema: 'campaign', table: 'run' })))
      .toBe('SELECT * FROM campaign.run LIMIT 500')
  })

  it('marks a view, and says how far a per-run table is built', () => {
    expect(tableLabel(table({ table: 'run_view', kind: 'view', rows: 4 }))).toBe('run_view (4) view')
    expect(tableLabel(table({ table: 'poses', rows: 10, runs: 4, built: 1 })))
      .toBe('poses (10) built 1/4')
    expect(tableLabel(table({ schema: 'campaign', table: 'run', kind: 'record', rows: 4 })))
      .toBe('campaign.run (4)')
  })

  it('explains an empty column list only for a table built on first query', () => {
    expect(columnsPlaceholder(table({}))).toMatch(/once a query builds it/)
    expect(columnsPlaceholder(table({ kind: 'view' }))).toBe('')
  })
})
