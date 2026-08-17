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

/** Alias the decimating aggregate hides under when the caller wants `SELECT *` (see
 *  {@link buildSeriesSql}). Stripped from every row before they leave the provider, so it must not
 *  collide with a real column: `data.db` columns come from ROS message fields, which cannot look
 *  like this. */
export const DECIMATE_ALIAS = '__rv_decimate_t'

/** Build the SELECT for {@link DataProvider.series} -- pure, so the SQL is testable without a service.
 *
 *  Returns the alias to strip from the result rows, or null when nothing was hidden in them.
 *
 *  Decimation groups the rows into 1/`hz`-second buckets and keeps ONE per bucket. Which one is
 *  decided by SQLite's bare-column rule: with exactly one `min()` in the result set, every bare
 *  column comes from the row that produced the minimum -- so each bucket yields its earliest real
 *  sample, columns intact, rather than an arbitrary row.
 *
 *  That aggregate has to be *somewhere*, which is what splits the two branches. With `columns` given
 *  it replaces the time column and is aliased back to its name (the row shape stays identical). With
 *  `columns` omitted -- the vega panel, whose author-written spec may read any column -- `SELECT *`
 *  is kept and the aggregate rides along under {@link DECIMATE_ALIAS}.
 *
 *  Note `HAVING MIN(...)` is not a third option: HAVING reads the aggregate as a *condition*, so the
 *  bucket at t < 1/hz (min 0.0, falsy) disappears. */
export function buildSeriesSql(
  table: string,
  where: string,
  opts: SeriesOptions = {},
): { sql: string; sentinel: string | null } {
  const timeCol = opts.timeCol ?? 'timestamp'
  const clauses = [where]
  if (opts.t0 != null) clauses.push(`CAST("${timeCol}" AS REAL) >= ${opts.t0}`)
  if (opts.t1 != null) clauses.push(`CAST("${timeCol}" AS REAL) <= ${opts.t1}`)
  for (const [col, val] of Object.entries(opts.match ?? {})) {
    clauses.push(`"${col}" = ${typeof val === 'number' ? val : sqlStr(String(val))}`)
  }
  let select = opts.columns?.length ? opts.columns.map((c) => `"${c}"`).join(', ') : '*'
  let group = ''
  let sentinel: string | null = null
  if (opts.decimate) {
    // `hz` reaches here from a .vast binding, so it is checked before it is interpolated: a string
    // would paste in as a bare identifier (garbage buckets, or a way out of the run scope) and 0
    // would collapse the whole run into a single row -- both of which render as a plausible chart.
    const hz = Number(opts.decimate.hz)
    if (!Number.isFinite(hz) || hz <= 0) {
      throw new Error(
        `series: decimate.hz must be a finite number > 0, got ${JSON.stringify(opts.decimate.hz)}`,
      )
    }
    const agg = `MIN(CAST("${timeCol}" AS REAL))`
    if (opts.columns?.length) {
      const rest = opts.columns.filter((c) => c !== timeCol).map((c) => `"${c}"`)
      select = [...rest, `${agg} AS "${timeCol}"`].join(', ')
    } else {
      sentinel = DECIMATE_ALIAS
      select = `*, ${agg} AS "${sentinel}"`
    }
    const bucket = `CAST(CAST("${timeCol}" AS REAL) * ${hz} AS INTEGER)`
    group = ` GROUP BY ${opts.decimate.key ? `"${opts.decimate.key}", ` : ''}${bucket}`
  }
  const sql =
    `SELECT ${select} FROM "${table}" WHERE ${clauses.join(' AND ')}${group} ` +
    `ORDER BY CAST("${timeCol}" AS REAL)`
  return { sql, sentinel }
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
    const { sql, sentinel } = buildSeriesSql(table, where, opts)
    const res = await robovast.queryCampaignDataSql(campaignId, sql, opts.maxRows ?? 5000)
    // Strip here rather than in the caller: the alias would otherwise show up in
    // TimeSeriesSource.columns, in numeric coercion, and in any Vega `fold` over the row.
    if (sentinel) for (const row of res.rows) delete row[sentinel]
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
