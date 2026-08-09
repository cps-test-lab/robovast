// Loading the merged run log, once per (campaign, config, run) — the data half of the log
// surfaces, shared by the run-view panel and the Explorer tab.
//
// Deliberately ONE bulk load rather than a query per cursor position. The panel follows the
// playback clock, which ticks at frame rate; querying per position would need throttling,
// coalescing and rejection of out-of-order responses — the same race class that produced the
// stale local costmap. Loading once removes the failure mode instead of managing it, and
// makes the text filter instant (and regex-capable) because it runs over rows already here.
//
// What the server *does* do is the scoping and the ordering: only high-selectivity predicates
// push down (the run, and a severity floor), because those shrink the payload. Filtering by
// text or container does not, and would only re-fetch what is already in memory.

import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { robovast } from '@/lib/robovastClient'

/** One merged log event, as the `run_log` table stores it. */
export interface LogRow {
  sim_time: number | null
  wall_ts: number | null
  time_source: string
  in_window: number
  container: string
  node: string
  source: string
  level: string
  severity: string
  message: string
  config_name?: string
  run_id?: number
}

/** What this run's timestamps are worth, read from `runs` beside the log itself. */
export interface ClockProvenance {
  source: string
  samples: number
  wallSpanS: number
}

/** Where this run's trial ended, read from `scenario_timestamps` — the one place
 *  postprocessing records it, so the log, the playback clock and `search_run_logs` cut at
 *  the same moment instead of each matching the log text again. */
export interface Verdict {
  /** On the run's clock. Null when the clock map could not place the verdict. */
  simTime: number | null
  /** On the wall clock. **This is what the log is cut by** — the clock map does not
   *  extrapolate, so a run whose `/clock` stopped at shutdown has no sim time for any line
   *  after the verdict, which is exactly the log with the most shutdown in it. */
  wallTs: number | null
  /** `succeeded` or `failed`. */
  status: string
}

export interface RunLogData {
  rows: LogRow[]
  /** Ascending `sim_time` of the rows that have one — the search array for the cursor. */
  simTimes: number[]
  /** Index in `rows` of each entry in `simTimes`. */
  simIndex: number[]
  clock: ClockProvenance | null
  /** Where the trial ended, or null when this run reached no verdict (and for a campaign
   *  postprocessed before the verdict was recorded). Absent for a multi-run scope, where one
   *  answer would not be true of every run. */
  verdict: Verdict | null
  /** True when the load stopped at `maxRows`, so the view can say so rather than imply completeness. */
  truncated: boolean
  /** Absent `run_log` table (postprocessing predates it) vs. present but empty. */
  missingTable: boolean
  total: number
}

const COLUMNS =
  'sim_time, wall_ts, time_source, in_window, container, node, source, level, severity, message'

/** How many rows one load will take. Above the service's own 5000-row clamp per request, so
 *  the loader pages; the ceiling exists to bound the browser's memory on a chatty sweep, and
 *  hitting it is reported rather than silently truncating the log. */
export const DEFAULT_MAX_ROWS = 20000

/** Rows per request — the service clamps a data query at 5000 (`data_query.py`). */
const PAGE = 5000

const quote = (v: string) => `'${v.replace(/'/g, "''")}'`

/** SQL for one page. `severityFloor` is the only content predicate pushed down: it can drop
 *  most of a run's rows, where the others only re-express what is already loaded. */
function pageSql(
  configName: string | undefined,
  runId: number | undefined,
  severityFloor: string[] | undefined,
  offset: number,
): string {
  const where: string[] = []
  if (configName) where.push(`config_name = ${quote(configName)}`)
  if (runId != null) where.push(`run_id = ${runId}`)
  if (severityFloor?.length)
    where.push(`severity IN (${severityFloor.map(quote).join(', ')})`)
  const scope = where.length ? ` WHERE ${where.join(' AND ')}` : ''
  // ORDER BY on the server: the merge already wrote the rows in wall order, but a table is a
  // set and the panel's cursor search needs them sorted. NULL sim_time (pre-roll: logged
  // before the simulator's clock existed) sorts first, which is where it happened.
  return (
    `SELECT config_name, run_id, ${COLUMNS} FROM run_log${scope} ` +
    `ORDER BY sim_time IS NOT NULL, sim_time, wall_ts, rowid ` +
    `LIMIT ${PAGE} OFFSET ${offset}`
  )
}

const num = (v: unknown): number | null =>
  v === null || v === undefined || v === '' ? null : Number(v)

function toRow(r: Record<string, unknown>): LogRow {
  return {
    sim_time: num(r.sim_time),
    wall_ts: num(r.wall_ts),
    time_source: String(r.time_source ?? ''),
    in_window: Number(r.in_window ?? 1),
    container: String(r.container ?? ''),
    node: String(r.node ?? ''),
    source: String(r.source ?? ''),
    level: String(r.level ?? ''),
    severity: String(r.severity ?? 'other'),
    message: String(r.message ?? ''),
    config_name: r.config_name == null ? undefined : String(r.config_name),
    run_id: r.run_id == null ? undefined : Number(r.run_id),
  }
}

export interface UseRunLogOptions {
  campaignId: string
  /** Omit to load every config (the Explorer's campaign level). */
  configName?: string
  /** Omit to load every run of the scope. */
  runId?: number
  /** Push a severity floor into the query — the one content filter worth doing server-side. */
  severities?: string[]
  maxRows?: number
  enabled?: boolean
}

/** Load a run's (or a config's, or a campaign's) merged log, paged to `maxRows`. */
export function useRunLog(opts: UseRunLogOptions) {
  const { campaignId, configName, runId, severities, maxRows = DEFAULT_MAX_ROWS } = opts
  const enabled = (opts.enabled ?? true) && !!campaignId

  const query = useQuery({
    queryKey: ['run-log', campaignId, configName ?? '', runId ?? '', severities?.join(',') ?? ''],
    enabled,
    staleTime: 5 * 60_000,
    retry: false,
    queryFn: async (): Promise<RunLogData> => {
      const rows: LogRow[] = []
      let truncated = false
      let missingTable = false
      for (let offset = 0; offset < maxRows; offset += PAGE) {
        const want = Math.min(PAGE, maxRows - offset)
        let page
        try {
          page = await robovast.queryCampaignDataSql(
            campaignId,
            pageSql(configName, runId, severities, offset),
            want,
          )
        } catch (e) {
          // "no such table: run_log" is the campaign whose postprocessing predates this, and
          // is worth reporting as exactly that rather than as a failed request.
          if (offset === 0 && /no such table/i.test(String((e as Error).message))) {
            missingTable = true
            break
          }
          throw e
        }
        const got = page.rows ?? []
        // Rows come back keyed by column name (`DataQueryResult.rows: list[dict]`).
        for (const row of got) rows.push(toRow(row as Record<string, unknown>))
        if (got.length < want) break
        if (offset + PAGE >= maxRows) truncated = true
      }

      // The clock provenance lives on `runs`, so a reader can tell "not aligned" from
      // "nothing logged before the clock started". Missing for a multi-run scope, where one
      // answer would not be true of every run.
      let clock: ClockProvenance | null = null
      if (!missingTable && configName && runId != null) {
        try {
          const res = await robovast.queryCampaignDataSql(
            campaignId,
            `SELECT clock_map_source, clock_map_samples, clock_map_wall_span_s FROM runs ` +
              `WHERE config_name = ${quote(configName)} AND run_id = ${runId}`,
            1,
          )
          const row = (res.rows ?? [])[0] as Record<string, unknown> | undefined
          if (row)
            clock = {
              source: String(row.clock_map_source ?? 'none'),
              samples: Number(row.clock_map_samples ?? 0),
              wallSpanS: Number(row.clock_map_wall_span_s ?? 0),
            }
        } catch {
          // An older campaign's `runs` has no such columns. The view then says nothing about
          // alignment, which is honest -- it does not know.
        }
      }

      // Where the trial ended. Read, not re-derived: postprocessing already matched the
      // verdict once (`common/scenario_markers`) and wrote it here, so this view, the
      // playback clock and `search_run_logs` cut at the same moment.
      let verdict: Verdict | null = null
      if (!missingTable && configName && runId != null) {
        try {
          const res = await robovast.queryCampaignDataSql(
            campaignId,
            `SELECT timestamp, wall_ts, status FROM scenario_timestamps ` +
              `WHERE config_name = ${quote(configName)} AND run_id = ${runId}`,
            1,
          )
          const row = (res.rows ?? [])[0] as Record<string, unknown> | undefined
          // A row with no `status` is a run that reached no verdict -- killed by its
          // deadline, say. Recorded as null rather than as a zero timestamp, so the
          // controls can say there is nothing to trim instead of trimming to the start.
          if (row?.status)
            verdict = {
              simTime: num(row.timestamp),
              wallTs: num(row.wall_ts),
              status: String(row.status),
            }
        } catch {
          // A campaign postprocessed before `wall_ts` existed. Nothing is trimmed and the
          // toggle says why -- re-run postprocessing to get it.
        }
      }

      const simTimes: number[] = []
      const simIndex: number[] = []
      rows.forEach((row, i) => {
        if (row.sim_time != null) {
          simTimes.push(row.sim_time)
          simIndex.push(i)
        }
      })
      return { rows, simTimes, simIndex, clock, verdict, truncated, missingTable,
               total: rows.length }
    },
  })

  return useMemo(
    () => ({
      data: query.data,
      isPending: query.isPending && enabled,
      error: query.error as Error | undefined,
    }),
    [query.data, query.isPending, query.error, enabled],
  )
}
