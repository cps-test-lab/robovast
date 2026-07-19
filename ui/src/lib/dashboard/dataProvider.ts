// DataProvider: the panel↔data seam. Panels ask for rows by table + time and never learn where the
// data comes from. Today the only implementation is `dbDataProvider`, which reads a single run's rows
// out of the campaign's postprocessed `data.db` through the existing read-only query/describe endpoints.
// A future live view can implement the same interface over a rosbridge buffer (nearest = latest sample)
// without touching any panel.
//
// A run in data.db is keyed by (config_name, run_id); a provider is bound to one such run, so every
// query is scoped to it. All data.db columns are TEXT, so callers coerce numerics themselves.

import { robovast, type DataDescribe, type CostmapFrame } from '@/lib/robovastClient'

export type DataRow = Record<string, unknown>

export interface SeriesOptions {
  timeCol?: string // defaults to 'timestamp'
  t0?: number
  t1?: number
  columns?: string[] // defaults to '*'
  maxRows?: number
}

export interface DataProvider {
  /** True if `table` exists (and, when given, contains every one of `columns`). */
  has(table: string, columns?: string[]): Promise<boolean>
  /** [min, max] of `timeCol` across the run, or null if the table has no rows. */
  timeRange(table: string, timeCol?: string): Promise<[number, number] | null>
  /** All rows of the run in [t0, t1] ordered by time. */
  series(table: string, opts?: SeriesOptions): Promise<DataRow[]>
  /** The single row whose `timeCol` is nearest `t`, or null if the table is empty. */
  nearest(table: string, t: number, timeCol?: string): Promise<DataRow | null>
  /** The costmap grid frame for `topic` nearest `t` (heavy payload, dedicated endpoint), or null. */
  costmapFrame(topic: string, t: number): Promise<CostmapFrame | null>
}

/** Quote a value as a SQL string literal (single-quote escaped). */
function sqlStr(v: string): string {
  return `'${v.replace(/'/g, "''")}'`
}

const isInt = (v: string | number): boolean => /^-?\d+$/.test(String(v))

export function dbDataProvider(
  campaignId: string,
  configName: string,
  runId: string | number,
): DataProvider {
  // run scope, reused by every query. run_id is an integer column; refuse a non-integer rather than
  // silently building broken SQL.
  if (!isInt(runId)) throw new Error(`run_id must be an integer, got ${runId!}`)
  const where = `config_name = ${sqlStr(configName)} AND run_id = ${runId}`

  // /describe is per-campaign, not per-run — fetch once and share.
  let describe: Promise<DataDescribe> | null = null
  const getDescribe = () => (describe ??= robovast.describeCampaignData(campaignId))

  const cols = (columns?: string[]) =>
    columns && columns.length ? columns.map((c) => `"${c}"`).join(', ') : '*'

  return {
    async has(table, columns) {
      const d = await getDescribe()
      const t = d.tables.find((x) => x.table === table)
      if (!t) return false
      if (!columns?.length) return true
      return columns.every((c) => t.columns.includes(c))
    },

    async timeRange(table, timeCol = 'timestamp') {
      const sql =
        `SELECT MIN(CAST("${timeCol}" AS REAL)) AS lo, MAX(CAST("${timeCol}" AS REAL)) AS hi ` +
        `FROM "${table}" WHERE ${where}`
      const res = await robovast.queryCampaignDataSql(campaignId, sql, 1)
      const row = res.rows[0]
      if (!row || row.lo == null || row.hi == null) return null
      return [Number(row.lo), Number(row.hi)]
    },

    async series(table, opts = {}) {
      const timeCol = opts.timeCol ?? 'timestamp'
      const clauses = [where]
      if (opts.t0 != null) clauses.push(`CAST("${timeCol}" AS REAL) >= ${opts.t0}`)
      if (opts.t1 != null) clauses.push(`CAST("${timeCol}" AS REAL) <= ${opts.t1}`)
      const sql =
        `SELECT ${cols(opts.columns)} FROM "${table}" WHERE ${clauses.join(' AND ')} ` +
        `ORDER BY CAST("${timeCol}" AS REAL)`
      const res = await robovast.queryCampaignDataSql(campaignId, sql, opts.maxRows ?? 5000)
      return res.rows
    },

    async nearest(table, t, timeCol = 'timestamp') {
      const sql =
        `SELECT * FROM "${table}" WHERE ${where} ` +
        `ORDER BY ABS(CAST("${timeCol}" AS REAL) - ${t}) LIMIT 1`
      const res = await robovast.queryCampaignDataSql(campaignId, sql, 1)
      return res.rows[0] ?? null
    },

    costmapFrame(topic, t) {
      return robovast.costmapFrame(campaignId, configName, runId, topic, t)
    },
  }
}
