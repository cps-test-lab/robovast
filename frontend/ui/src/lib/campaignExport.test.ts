import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  buildExportRequest,
  exportableTables,
  initialExportState,
  selectAllTables,
  toggleTable,
} from './campaignExport'
import { robovast, type DataTable } from './robovastClient'

const table = (name: string, kind: string, schema = 'main'): DataTable => ({
  schema, table: name, columns: [], rows: null, kind, runs: null, built: null, failed: {},
  description: '', column_notes: {},
})

const described: DataTable[] = [
  table('run_view', 'view'),
  table('runs', 'table'),
  table('poses', 'table'),
  table('run_log', 'table'),
  table('unit', 'record', 'campaign'),
]

describe('the tables offered', () => {
  it('are the built tables, in order, without runs, views or the record', () => {
    expect(exportableTables(described)).toEqual(['poses', 'run_log'])
  })

  it('start all checked, as parquet, without bags, with the records', () => {
    const state = initialExportState(['poses', 'run_log'])
    expect([...state.selected]).toEqual(['poses', 'run_log'])
    expect(state.format).toBe('parquet')
    expect(state.bags).toBe('none')
    expect(state.records).toBe(true)
  })
})

describe('the request the dialog builds', () => {
  const all = initialExportState(['poses', 'run_log'])

  // Every table checked is the service's own default, so a table the catalog gains later is
  // exported too rather than the list frozen at the moment the dialog was opened.
  it('names no table while every table is checked', () => {
    expect(buildExportRequest(all)).toEqual(
      { tables: null, format: 'parquet', bags: 'none', records: true })
  })

  it('names the checked tables once one is unchecked', () => {
    const state = toggleTable(all, 'run_log')
    expect(buildExportRequest(state).tables).toEqual(['poses'])
    expect(buildExportRequest(toggleTable(state, 'run_log')).tables).toBeNull()
  })

  it('can name no table at all, which still exports runs', () => {
    expect(buildExportRequest(selectAllTables(all, false)).tables).toEqual([])
    expect(buildExportRequest(selectAllTables(selectAllTables(all, false), true)).tables)
      .toBeNull()
  })

  it('carries the format, the bags and the records as set', () => {
    const state = { ...all, format: 'csv' as const, bags: 'sqlite3' as const, records: false }
    expect(buildExportRequest(state)).toEqual(
      { tables: null, format: 'csv', bags: 'sqlite3', records: false })
  })

  it('does not change the state it reads', () => {
    const before = [...all.selected]
    toggleTable(all, 'poses')
    selectAllTables(all, false)
    expect([...all.selected]).toEqual(before)
  })
})

describe('the export calls', () => {
  afterEach(() => vi.unstubAllGlobals())

  function serve(body: unknown) {
    const fetch = vi.fn(async () => new Response(JSON.stringify(body), { status: 200 }))
    vi.stubGlobal('fetch', fetch)
    return fetch
  }

  it('starts an export with the request as built', async () => {
    const fetch = serve({ export_id: '0123456789ab', url: '/data/x' })
    const body = buildExportRequest(initialExportState(['poses']))
    expect(await robovast.createExport('camp/1', body))
      .toEqual({ export_id: '0123456789ab', url: '/data/x' })
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toMatch(/\/campaigns\/camp%2F1\/exports$/)
    expect(init.method).toBe('POST')
    expect(JSON.parse(String(init.body))).toEqual(body)
  })

  it('polls the status by id', async () => {
    const fetch = serve({ export_id: '0123456789ab', done: true, error: '', bytes: 5,
                          tables: { runs: 1 }, started_at: null, finished_at: null })
    const status = await robovast.getExportStatus('c', '0123456789ab')
    expect(status.done).toBe(true)
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toMatch(/\/campaigns\/c\/exports\/0123456789ab$/)
    expect(init.method).toBe('GET')
  })

  it('names the file on the data plane', () => {
    expect(robovast.exportUrl('c', '0123456789ab'))
      .toMatch(/\/data\/campaigns\/c\/exports\/0123456789ab$/)
  })
})
