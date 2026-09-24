// The Export dialog's state and the request it builds: which tables, which format, which
// bags, whether the records ship. Kept apart from the dialog so what it asks the service for
// can be checked without rendering it.
//
// The table list comes from `describeCampaignData`, all checked: an export of everything is
// the common case, and unchecking narrows it. `runs` is always written by the service, so it
// is shown but not offered as a choice. A request that keeps every table checked says
// `tables: null` -- "every table the records can give" -- rather than spelling the list out,
// so a table the catalog gains after the dialog was read is exported too.

import type { DataTable, ExportRequest } from './robovastClient'

export type ExportFormat = ExportRequest['format']
export type ExportBags = ExportRequest['bags']

export interface ExportState {
  /** Every table offered, in the order the dialog shows them. */
  tables: string[]
  /** The ones checked. */
  selected: Set<string>
  format: ExportFormat
  bags: ExportBags
  records: boolean
}

/** The table the service always writes, whatever the request names. */
export const ALWAYS_EXPORTED = 'runs'

/**
 * The tables an export may name, from the campaign's description: what is built from its
 * records or its record (`kind === 'table'`), without `runs`, in the described order.
 */
export function exportableTables(tables: DataTable[]): string[] {
  return tables
    .filter((t) => t.kind === 'table' && t.table !== ALWAYS_EXPORTED)
    .map((t) => t.table)
}

export function initialExportState(tables: string[]): ExportState {
  return { tables, selected: new Set(tables), format: 'parquet', bags: 'none', records: true }
}

export function toggleTable(state: ExportState, table: string): ExportState {
  const selected = new Set(state.selected)
  if (selected.has(table)) selected.delete(table)
  else selected.add(table)
  return { ...state, selected }
}

export function selectAllTables(state: ExportState, all: boolean): ExportState {
  return { ...state, selected: new Set(all ? state.tables : []) }
}

/** The request the state describes; every table checked is `tables: null`. */
export function buildExportRequest(state: ExportState): ExportRequest {
  const every = state.tables.every((t) => state.selected.has(t))
  return {
    tables: every ? null : state.tables.filter((t) => state.selected.has(t)),
    format: state.format,
    bags: state.bags,
    records: state.records,
  }
}

/** What the dialog says an option means, beside its control. */
export const BAG_LABELS: Record<ExportBags, string> = {
  none: 'No recordings',
  mcap: 'As recorded (mcap)',
  sqlite3: 'Rewritten as sqlite3 (for a ROS 2 without the mcap plugin)',
}

export const FORMAT_LABELS: Record<ExportFormat, string> = {
  parquet: 'Parquet (the tables as they are; opens with pandas or DuckDB)',
  csv: 'CSV (the same rows as text)',
}
