// dbDataProvider: the host's implementation of the DataProvider seam (declared in
// @robovast/panel-kit, shared with panel remotes). It reads a single run's rows out of the campaign's
// postprocessed `data.db` through the read-only query/describe endpoints.
//
// A run in data.db is keyed by (config_name, run_id); a provider is bound to one such run, so every
// query is scoped to it. All data.db columns are TEXT, so callers coerce numerics themselves.

import type { DataProvider } from '@robovast/panel-kit'
import { robovast, type DataDescribe } from '@/lib/robovastClient'

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
