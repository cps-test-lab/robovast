export type DataRow = Record<string, unknown>;
export interface SeriesOptions {
    timeCol?: string;
    t0?: number;
    t1?: number;
    columns?: string[];
    maxRows?: number;
    match?: Record<string, string | number>;
    decimate?: {
        hz: number;
        key?: string;
    };
}
export interface DataProvider {
    /** Stable identity of the run this provider is bound to (`<campaign>:<config>:<run>`).
     *
     *  **Include it in every cache key.** React Query's cache is global and outlives the panel remount
     *  that a run switch causes, and campaigns reuse table names -- `poses` and `nav2_behaviors` mean a
     *  different run in every campaign. Keying on the table name alone therefore served the *previous*
     *  campaign's rows after a switch, which is a wrong answer rendered confidently. */
    scope: string;
    /** The run this provider is bound to, as separate fields. Present as well as `scope` because a
     *  consumer that needs to *call* something per run (the scene routes take config_name and run_id as
     *  query params) must not have to split a string a config name could legally contain a colon in. */
    campaignId: string;
    configName: string;
    runId: string;
    /** True if `table` exists (and, when given, contains every one of `columns`). */
    has(table: string, columns?: string[]): Promise<boolean>;
    /** [min, max] of `timeCol` across the run, or null if the table has no rows. */
    timeRange(table: string, timeCol?: string): Promise<[number, number] | null>;
    /** All rows of the run in [t0, t1] ordered by time. */
    series(table: string, opts?: SeriesOptions): Promise<DataRow[]>;
    /** The single row whose `timeCol` is nearest `t`, or null if the table is empty. */
    nearest(table: string, t: number, timeCol?: string): Promise<DataRow | null>;
    /** GET a run-scoped JSON endpoint under the campaign (`config_name`+`run_id` applied), for a panel
     *  needing a specialized endpoint the generic provider doesn't model. This is how an external panel
     *  (e.g. robovast_nav's costmap, via `fetchRun('costmap', {topic, t})`) reaches its own data without
     *  the generic seam knowing anything nav-specific. */
    fetchRun<T>(endpoint: string, params?: Record<string, string | number>): Promise<T>;
    /** URL of one of the run's artifact files (e.g. the scene3d panel's `scene/scene.json`), fetched
     *  directly by the consumer. Path-style so relative sibling fetches stay within the run. */
    runFileUrl(path: string): string;
    /** URL of a file the whole campaign shares rather than one run (a world every run compiled
     *  identically, a frozen `_config/` input). Path is campaign-relative. */
    campaignFileUrl(path: string): string;
    /** The distinct values of `column` in `table` for this run, ascending. This is how a panel
     *  discovers what a multi-keyed table actually contains -- which TF frames a run recorded -- rather
     *  than having every one of them enumerated in the .vast. */
    distinct(table: string, column: string): Promise<string[]>;
}
