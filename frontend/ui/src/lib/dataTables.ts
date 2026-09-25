// Pure helpers for the Data browser's schema panel: how a described table is named, labelled
// and first queried.
//
// Views and per-run tables live in the default schema `main` and are queried unqualified; the
// campaign's record is the schema `campaign` and is always qualified (`campaign.run`).

import type { DataTable } from './robovastClient'

/** The name a query uses for *t*: unqualified in `main`, `schema.table` otherwise. */
export function qualifiedName(t: Pick<DataTable, 'schema' | 'table'>): string {
  return !t.schema || t.schema === 'main' ? t.table : `${t.schema}.${t.table}`
}

/** The first look at a table. */
export function browseSql(t: Pick<DataTable, 'schema' | 'table'>): string {
  return `SELECT * FROM ${qualifiedName(t)} LIMIT 500`
}

/** `name (rows)`, marked when it is a view, and with how many of its runs a per-run table is
 *  built for — a table is built the first time a query names it, so a partial count is the
 *  normal state of a campaign nobody has queried yet rather than missing data. */
export function tableLabel(t: DataTable): string {
  const parts = [qualifiedName(t)]
  if (t.rows != null) parts.push(`(${t.rows})`)
  if (t.kind === 'view') parts.push('view')
  if (t.runs != null) parts.push(`built ${t.built ?? 0}/${t.runs}`)
  return parts.join(' ')
}

/** What the column list says while it is empty: a per-run table reports its columns once it is
 *  built for some run. */
export function columnsPlaceholder(t: DataTable): string {
  return t.kind === 'table' ? 'columns appear once a query builds it' : ''
}
