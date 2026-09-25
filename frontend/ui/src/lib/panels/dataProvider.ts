// dbDataProvider: the host's implementation of the DataProvider seam (declared in
// @robovast/panel-kit, shared with panel remotes). It reads a single run's rows out of the campaign's
// tables through the read-only query/describe endpoints; a table is built for the run the first
// time a query names it.
//
// A run's rows are keyed by (config_name, run_id); a provider is bound to one such run, so every
// query is scoped to it. A TEXT column stays TEXT, so callers coerce numerics themselves.
//
// A run that is still recording (`run_view.live`) has the same provider plus the host-side
// extension below: its tables are built as its recording grows, so a reader takes its history
// through SQL once and then hears the rows that follow through `subscribeLive` -- one live
// subscription per run, carrying every table the mounted panels read (see liveFeed.ts). The
// extension is the host's, not the kit's: a panel remote is a prebuilt bundle against the kit's
// interface, and one that wants the live rows asks `isLiveProvider` first.

import type { DataProvider, DataRow, SeriesOptions } from '@robovast/panel-kit'
import { robovast, type DataDescribe } from '@/lib/robovastClient'
import { LiveRunFeed, type FeedListener } from './liveFeed'

/** The host's provider: the kit's seam plus what a live run needs. */
export interface LiveDataProvider extends DataProvider {
  /** Whether the run is still recording. Its tables grow, and `subscribeLive` carries the growth;
   *  for a finished run every subscription hears `eof` at once and nothing else. */
  live: boolean
  /** The rows of `table` as they land, after the history a query gave. See `FeedEvent` for the
   *  protocol -- in one line: SQL for what is there, the stream for what comes, and a `gap` sends
   *  the reader back to SQL once. */
  subscribeLive(table: string, listener: FeedListener): () => void
  /** A plain read of `table`'s rows for this run, in no particular order -- for a table with no
   *  time column (`sim_recording` is one row per run). */
  rows(table: string, opts?: { columns?: string[]; maxRows?: number }): Promise<DataRow[]>
  /** Release the live subscription. A provider is per run, so a run switch closes it. */
  close(): void
}

export const isLiveProvider = (data: DataProvider): data is LiveDataProvider =>
  typeof (data as LiveDataProvider).subscribeLive === 'function'

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
 *  `DISTINCT ON (bucket) ... ORDER BY bucket, time` says that: it returns the row itself, so the
 *  time column keeps its measured value rather than a bucket minimum that happens to equal it, and
 *  a `SELECT *` needs no aggregate hidden among its columns.
 *
 *  The inner `ORDER BY` must start with the `DISTINCT ON` expressions for "earliest" to be defined,
 *  so the rows are picked ordered by bucket and the wrapping query re-orders them by time. */
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

/** The react-query options for a campaign's `/describe`, shared by every reader of it.
 *
 *  `/describe` is per campaign, not per run, so the providers of one campaign's runs share one
 *  answer: a run switch builds a new provider, and asking again for each would repeat a request
 *  whose answer cannot differ. The key is the Data browser's (`['describe', campaignId]`) plus
 *  `version`, so an invalidation of that prefix reaches this entry too, and a campaign whose
 *  tables changed under a newer summary is asked again rather than served the old table list.
 *
 *  A table's `columns` are empty until it is built for some run, so this answer says which tables
 *  exist and `has` asks the run itself for the columns of one not built yet. */
export function describeQuery(campaignId: string, version: string) {
  return {
    queryKey: ['describe', campaignId, version],
    queryFn: () => robovast.describeCampaignData(campaignId),
    staleTime: Infinity,
    retry: false,
  } as const
}

/**
 * @param getDescribe the campaign's `/describe`, supplied by the host so that every provider of one
 *   campaign shares a single answer (see {@link describeQuery}); called lazily, on the first
 *   `has`.
 * @param opts.live the run is still recording: `subscribeLive` opens the run's live stream on the
 *   first subscription. Off, every subscription hears `eof` at once.
 * @param opts.feed how the live stream is opened; the client's route unless a test says otherwise.
 */
export function dbDataProvider(
  campaignId: string,
  configName: string,
  runId: string | number,
  getDescribe: () => Promise<DataDescribe>,
  opts: { live?: boolean; feed?: () => LiveRunFeed } = {},
): LiveDataProvider {
  // run scope, reused by every query. run_id is an integer column; refuse a non-integer rather than
  // silently building broken SQL.
  if (!isInt(runId)) throw new Error(`run_id must be an integer, got ${runId!}`)
  const where = `config_name = ${sqlStr(configName)} AND run_id = ${runId}`

  // A const rather than a method on the literal below, so `series` can delegate to it without `this`
  // -- a panel that destructures the provider (`const { series } = data`) must keep working.
  const seriesPage = async (table: string, opts: SeriesOptions = {}) => {
    const sql = buildSeriesSql(table, where, opts)
    const res = await robovast.queryCampaignDataSql(campaignId, sql, opts.maxRows ?? 5000)
    return { rows: res.rows, truncated: res.truncated, note: res.note }
  }

  const live = opts.live === true
  // Opened on the first subscription rather than with the provider: a view whose panels read no
  // table of a live run -- one that only shows its log -- holds no socket for it.
  let feed: LiveRunFeed | null = null
  const openFeed = () =>
    feed ?? (feed = opts.feed
      ? opts.feed()
      : new LiveRunFeed((tables) =>
          robovast.liveRunStreamUrl(campaignId, configName, runId, tables)))

  return {
    scope: `${campaignId}:${configName}:${runId}`,
    campaignId,
    configName,
    runId: String(runId),
    live,

    subscribeLive(table, listener) {
      if (!live) {
        // A finished run's tables are all there: the one read the listener makes on `eof` is the
        // whole of it, and no socket is opened to say so.
        let cancelled = false
        queueMicrotask(() => {
          if (!cancelled) listener({ kind: 'eof' })
        })
        return () => {
          cancelled = true
        }
      }
      return openFeed().subscribe(table, listener)
    },

    close() {
      feed?.close()
      feed = null
    },

    async rows(table, { columns, maxRows = 1000 } = {}) {
      const select = columns?.length ? columns.map((c) => `"${c}"`).join(', ') : '*'
      const res = await robovast.queryCampaignDataSql(
        campaignId, `SELECT ${select} FROM "${table}" WHERE ${where}`, maxRows)
      return res.rows
    },

    async has(table, columns) {
      if (live) {
        // A live run's tables appear as its recording grows, so the campaign's cached table list
        // cannot answer for it; the run itself is asked, and a table it does not carry yet is
        // simply not there yet.
        try {
          const page = await robovast.queryCampaignDataSql(
            campaignId, `SELECT * FROM "${table}" WHERE ${where} LIMIT 0`, 1)
          const names = new Set(page.columns)
          return (columns ?? []).every((c) => names.has(c))
        } catch {
          return false
        }
      }
      const d = await getDescribe()
      const t = d.tables.find((x) => x.table === table)
      if (!t) return false
      if (!columns?.length) return true
      // /describe lists a column as "name TYPE" (e.g. "parent_id TEXT"), so the name has
      // to be split off first -- comparing against the whole entry never matches, which
      // made every column look absent and only ever showed as a panel reporting missing
      // data rather than as an error.
      //
      // No columns means the table is not built for any run yet: an empty page of this run's
      // rows builds it for this run and names its columns.
      const listed = t.columns.length
        ? t.columns.map((c) => c.split(/\s+/)[0])
        : (await robovast.queryCampaignDataSql(
            campaignId, `SELECT * FROM "${table}" WHERE ${where} LIMIT 0`, 1)).columns
      const names = new Set(listed)
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
