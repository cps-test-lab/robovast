// dbDataProvider: the host's implementation of the DataProvider seam (declared in
// @robovast/panel-kit, shared with panel remotes). It reads a single run's rows out of the campaign's
// postprocessed `data.db` through the read-only query/describe endpoints.
//
// A run in data.db is keyed by (config_name, run_id); a provider is bound to one such run, so every
// query is scoped to it. All data.db columns are TEXT, so callers coerce numerics themselves.

import type { DataProvider, SeriesOptions } from '@robovast/panel-kit'
import { robovast, type DataDescribe } from '@/lib/robovastClient'

/** Quote a value as a SQL string literal (single-quote escaped). */
function sqlStr(v: string): string {
  return `'${v.replace(/'/g, "''")}'`
}

const isInt = (v: string | number): boolean => /^-?\d+$/.test(String(v))

/** Build the SELECT for {@link DataProvider.series} -- pure, so the SQL is testable without a service.
 *
 *  Decimation groups the rows into 1/`hz`-second buckets and keeps ONE per bucket: the bucket's
 *  earliest real sample, with every column of that row intact.
 *
 *  `DISTINCT ON (bucket) ... ORDER BY bucket, time` is how the index (Postgres) says that. The
 *  original spelling -- `MIN(time)` with the other columns bare under a `GROUP BY bucket` -- was
 *  SQLite's bare-column rule, which Postgres rejects outright ("column must appear in the GROUP BY
 *  clause"). It also had to be un-aliased back into the time column, and to hide the aggregate under
 *  a sentinel alias for the `SELECT *` case; `DISTINCT ON` returns the row itself, so neither is
 *  needed and the time column keeps its measured value rather than a bucket minimum that happens to
 *  equal it.
 *
 *  The wrapping subquery is Postgres' rule that the outer `ORDER BY` must start with the
 *  `DISTINCT ON` expressions: the rows are picked ordered by bucket, then re-ordered by time. */
export function buildSeriesSql(table: string, where: string, opts: SeriesOptions = {}): string {
  const timeCol = opts.timeCol ?? 'timestamp'
  const time = `CAST("${timeCol}" AS REAL)`
  const clauses = [where]
  if (opts.t0 != null) clauses.push(`${time} >= ${opts.t0}`)
  if (opts.t1 != null) clauses.push(`${time} <= ${opts.t1}`)
  for (const [col, val] of Object.entries(opts.match ?? {})) {
    clauses.push(`"${col}" = ${typeof val === 'number' ? val : sqlStr(String(val))}`)
  }
  const select = opts.columns?.length ? opts.columns.map((c) => `"${c}"`).join(', ') : '*'
  const from = `FROM "${table}" WHERE ${clauses.join(' AND ')}`
  if (!opts.decimate) return `SELECT ${select} ${from} ORDER BY ${time}`

  // `hz` reaches here from a .vast binding, so it is checked before it is interpolated: a string
  // would paste in as a bare identifier (garbage buckets, or a way out of the run scope) and 0
  // would collapse the whole run into a single row -- both of which render as a plausible chart.
  const hz = Number(opts.decimate.hz)
  if (!Number.isFinite(hz) || hz <= 0) {
    throw new Error(
      `series: decimate.hz must be a finite number > 0, got ${JSON.stringify(opts.decimate.hz)}`,
    )
  }
  const bucket = `CAST(${time} * ${hz} AS INTEGER)`
  const key = opts.decimate.key ? `"${opts.decimate.key}", ` : ''
  // The inner SELECT must carry the time column whatever the caller asked for: the outer ORDER BY
  // reads it from the subquery, not from the table.
  const inner = opts.columns?.length
    ? [...new Set([...opts.columns, timeCol])].map((c) => `"${c}"`).join(', ')
    : select
  return (
    `SELECT * FROM (SELECT DISTINCT ON (${key}${bucket}) ${inner} ${from} ` +
    `ORDER BY ${key}${bucket}, ${time}) t ORDER BY ${time}`
  )
}

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

  // A const rather than a method on the literal below, so `series` can delegate to it without `this`
  // -- a panel that destructures the provider (`const { series } = data`) must keep working.
  const seriesPage = async (table: string, opts: SeriesOptions = {}) => {
    const sql = buildSeriesSql(table, where, opts)
    const res = await robovast.queryCampaignDataSql(campaignId, sql, opts.maxRows ?? 5000)
    return { rows: res.rows, truncated: res.truncated, note: res.note }
  }

  return {
    scope: `${campaignId}:${configName}:${runId}`,
    campaignId,
    configName,
    runId: String(runId),

    async has(table, columns) {
      const d = await getDescribe()
      const t = d.tables.find((x) => x.table === table)
      if (!t) return false
      if (!columns?.length) return true
      // /describe lists a column as "name TYPE" (e.g. "parent_id TEXT"), so the name has
      // to be split off first -- comparing against the whole entry never matches, which
      // made every column look absent and only ever showed as a panel reporting missing
      // data rather than as an error.
      const names = new Set(t.columns.map((c) => c.split(/\s+/)[0]))
      return columns.every((c) => names.has(c))
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

    seriesPage,

    series: (table, opts) => seriesPage(table, opts).then((page) => page.rows),

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
