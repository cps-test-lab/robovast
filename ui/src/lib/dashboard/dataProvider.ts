// DataProvider: the panel↔data seam. Panels ask for rows by table + time and never learn where the
// data comes from. Today the only implementation is `dbDataProvider`, which reads a single run's rows
// out of the campaign's postprocessed `data.db` through the existing read-only query/describe endpoints.
// A future live view can implement the same interface over a rosbridge buffer (nearest = latest sample)
// without touching any panel.
//
// A run in data.db is keyed by (config_name, run_id); a provider is bound to one such run, so every
// query is scoped to it. All data.db columns are TEXT, so callers coerce numerics themselves.

import { robovast, type DataDescribe } from '@/lib/robovastClient'

export type DataRow = Record<string, unknown>

export interface SeriesOptions {
  timeCol?: string // defaults to 'timestamp'
  t0?: number
  t1?: number
  columns?: string[] // defaults to '*'
  maxRows?: number
  // Equality filters (col = value) ANDed onto the run scope -- isolate one series from a
  // multi-keyed table (e.g. poses keyed by `frame`, costmaps by `topic`).
  match?: Record<string, string | number>
  // Thin the result to one row per 1/`hz` second, per distinct `key` value when given. For the
  // multi-keyed case: a table holding every TF frame of a world carries one series per frame, so
  // "all of them at recording rate" scales with the number of moving things (a 17-bone walker at
  // 30 Hz over 90 s is 46k rows by itself) while a viewer needs nothing near that resolution.
  // Requires `columns`: the SQL must name them, because the time column is replaced by an aggregate.
  decimate?: { hz: number; key?: string }
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
  /** GET a run-scoped JSON endpoint under the campaign (`config_name`+`run_id` applied), for a panel
   *  needing a specialized endpoint the generic provider doesn't model. This is how an external panel
   *  (e.g. robovast_nav's costmap, via `fetchRun('costmap', {topic, t})`) reaches its own data without
   *  the generic seam knowing anything nav-specific. */
  fetchRun<T>(endpoint: string, params?: Record<string, string | number>): Promise<T>
  /** URL of one of the run's artifact files (e.g. the scene3d panel's `scene/scene.json`), fetched
   *  directly by the consumer. Path-style so relative sibling fetches stay within the run. */
  runFileUrl(path: string): string
  /** URL of a file the whole campaign shares rather than one run (a world every run compiled
   *  identically, a frozen `_config/` input). Path is campaign-relative. */
  campaignFileUrl(path: string): string
  /** The distinct values of `column` in `table` for this run, ascending. This is how a panel
   *  discovers what a multi-keyed table actually contains -- which TF frames a run recorded -- rather
   *  than having every one of them enumerated in the .vast. */
  distinct(table: string, column: string): Promise<string[]>
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
      for (const [col, val] of Object.entries(opts.match ?? {})) {
        clauses.push(`"${col}" = ${typeof val === 'number' ? val : sqlStr(String(val))}`)
      }
      let select = cols(opts.columns)
      let group = ''
      if (opts.decimate) {
        if (!opts.columns?.length) {
          throw new Error('series: decimate requires an explicit `columns` list')
        }
        // One row per time bucket (per key, when the table is multi-keyed). MIN() on the time column
        // is what makes the surviving row *deterministic* rather than arbitrary: SQLite takes the
        // bare columns from the row that produced the min, so each bucket yields its earliest real
        // sample, columns intact. Aliasing the aggregate back to the time column keeps the row shape
        // identical to an undecimated query, so callers cannot tell the difference.
        const rest = opts.columns.filter((c) => c !== timeCol).map((c) => `"${c}"`)
        select = [...rest, `MIN(CAST("${timeCol}" AS REAL)) AS "${timeCol}"`].join(', ')
        const bucket = `CAST(CAST("${timeCol}" AS REAL) * ${opts.decimate.hz} AS INTEGER)`
        group = ` GROUP BY ${opts.decimate.key ? `"${opts.decimate.key}", ` : ''}${bucket}`
      }
      const sql =
        `SELECT ${select} FROM "${table}" WHERE ${clauses.join(' AND ')}${group} ` +
        `ORDER BY CAST("${timeCol}" AS REAL)`
      const res = await robovast.queryCampaignDataSql(campaignId, sql, opts.maxRows ?? 5000)
      return res.rows
    },

    async distinct(table, column) {
      const sql =
        `SELECT DISTINCT "${column}" AS v FROM "${table}" WHERE ${where} ` +
        `AND "${column}" IS NOT NULL ORDER BY v`
      const res = await robovast.queryCampaignDataSql(campaignId, sql, 1000)
      return res.rows.map((r) => String(r.v))
    },

    async nearest(table, t, timeCol = 'timestamp') {
      const sql =
        `SELECT * FROM "${table}" WHERE ${where} ` +
        `ORDER BY ABS(CAST("${timeCol}" AS REAL) - ${t}) LIMIT 1`
      const res = await robovast.queryCampaignDataSql(campaignId, sql, 1)
      return res.rows[0] ?? null
    },

    fetchRun(endpoint, params = {}) {
      return robovast.runEndpoint(campaignId, configName, runId, endpoint, params)
    },

    runFileUrl(path) {
      return robovast.runFileUrl(campaignId, configName, runId, path)
    },

    campaignFileUrl(path) {
      return robovast.campaignFileUrl(campaignId, path)
    },
  }
}
