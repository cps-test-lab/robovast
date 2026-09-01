# Porting the `runs` dimension table to the central index

**Status: done.** `runs` is built in the central index by
`campaign_ingest.build_runs_table`, called from `ingest_campaign` before the metric files so
a partial ingest still says which runs exist. Everything below is what it reproduces, kept
as the record of *why* each column looks the way it does; the tests are
`tests/results_processing/test_runs_table.py`, which also covers `read_runs` and
`read_table(..., with_params=True)` end to end.

Two things about the port rather than the table:

* The `param_*` columns are declared through `index_schema.ensure_table`, so a campaign
  whose `speed` is numeric and another's that is `'n/a'` share one column that widens to
  text and records a widening note. Under `data.db` that was a per-campaign warning; in one
  index it is a cross-campaign fact.
* Every row carries `campaign_id` (from `index_schema.CONTEXT_COLUMNS`, written by the
  sink), and `clear_campaign` removes exactly this campaign's rows before a re-ingest — the
  reproducibility invariant is a test, not a claim.

Two things it deliberately does **not** do, because the retired writer's rule no longer
holds in a shared index:

* It does not `DROP TABLE runs`. The table holds every campaign; dropping it to rebuild one
  would take the corpus with it.
* The `param_` collision skip is kept verbatim from the original, and is vacuous in
  practice: no fixed column name starts with `param_`, so a scenario parameter called
  `status` lands in `param_status` and cannot shadow the run's outcome. Kept anyway, since
  the fixed column set is what may grow.

Written while retiring the `data.db` writer. `runs` existed **only** in
`postprocessing_plugins._build_runs_table` (plus its helpers `_clock_map_info`, `_shm_info`,
`_max_bytes`), which this change deletes. The index has `campaign.run` mirrored from
`campaign.db` and the `run_view` over it, but **no** flattened `param_*` columns and no
`runs` table — so `common.analysis.db`'s `read_runs`, `attach_params` and
`read_table(..., with_params=True)` have nothing to join against until this is ported.

Recover the deleted implementation with:

    git show <this commit>^:src/robovast/results_processing/postprocessing_plugins.py

This note is what it did, so it can be reproduced against Postgres without that code.

## Grain

One row per `(config_name, run_id)` for every run directory found by walking the config dirs
— **not** per row in `campaign.db.run`. Plus one extra row per *composition-failed* unit (see
below). Indexed on `(config_name, run_id)`, the join key every metric table shares.

## Fixed columns, in order

    config_name, run_id, status, passed, duration_s, errors, failures, objective,
    start_time, end_time,
    instance_type, node_label, cpu_name, available_cpus, available_mem_bytes,
    shm_peak_bytes, shm_limit_bytes,
    clock_map_source, clock_map_samples, clock_map_wall_span_s, clock_map_sim_span_s,
    probed

Typing (the decisions, not the SQLite spellings):

* INTEGER — `run_id`, `passed`, `errors`, `failures`, `available_mem_bytes`,
  `shm_peak_bytes`, `shm_limit_bytes`, `clock_map_samples`, `probed`.
* REAL — `duration_s`, `objective`, `clock_map_wall_span_s`, `clock_map_sim_span_s`, and
  **`available_cpus`**. Deliberately not INTEGER: `execution.resources.cpu` accepts
  fractional cores, and an INTEGER column truncates a 0.5-core reservation to 0, which then
  reads as "this run had no CPU" in every query joining to it.
* TEXT — everything else.

## Sources per column

* **`status`, `passed`, `errors`, `failures`, `duration_s`, `start_time`** — `campaign.db`'s
  `run` table joined to `unit` (`run.unit_id = unit.id`), which is the operational source of
  truth written live from each `test.xml`. A store predating the `run` table falls back to
  `campaign_data.read_run_outcome(run_dir, campaign_path)`, i.e. re-parsing that run's
  `test.xml`. Both paths must stay: the fallback is also what supplies `sysinfo` on an old
  store.
* **`objective`, the params** — `campaign.db`'s `unit` table, keyed by `config_name`
  (`params_json` parsed as JSON; unparsable or empty → `{}`). Columns `status` and
  `paramset_id` are absent from old stores: the query retries with only
  `(config_name, params_json, objective)` rather than losing every unit's params.
* **`end_time`** — derived: `datetime.fromisoformat(start_time) + duration_s`, NULL when
  either input is missing or unparsable. Not stored anywhere.
* **`instance_type`, `node_label`, `cpu_name`, `available_cpus`, `available_mem_bytes`** —
  from `campaign.db`'s `job.sysinfo_json` via `LEFT JOIN job ON run.job_id = job.id`
  (per *job*, so shared by the runs of a packed multi-config job), falling back to the
  `sysinfo` dict `read_run_outcome` attaches when the store has no `job` table. Notes:
  * the key is **`available_mem`**, spelled exactly so, and its value is a
    Kubernetes-style quantity (a plain byte count, or `"16Gi"` when the `.vast` set a
    limit). Normalize with `common.quantity.to_bytes` → `available_mem_bytes`. Reading it
    raw makes the column numeric in some runs and text in others. (A fixture once invented
    `available_mem_gb`, which no producer emits; the test passed while the column was NULL
    in every real campaign.)
  * `node_label` is a *hash* of the node name (see `collect_sysinfo`), absent for local runs
    and for cluster runs recorded before the pod hashed one → NULL, never `""`. It is
    deliberately not called `node_name` (that means a behaviour-tree node in
    `nav2_behavior_tree`). It answers what `instance_type` cannot: on bare metal every node
    has the same kind, so only the machine separates a slow one from a fast one.
* **`shm_peak_bytes`, `shm_limit_bytes`** — the max over the run's own
  `resource_usage.csv` (`resource_usage.FILENAME` in the run dir) of `shm_used_bytes` /
  `shm_total_bytes`. Empty, unparsable and negative cells are *absent*, not 0; no file at
  all → `(None, None)`. NULL means unmeasured (a run from before the monitor sampled the
  pool, or a runtime with no `/dev/shm`) and must not be stored as 0 — the column exists to
  explain a SIGBUS (exit 135), which `available_mem_bytes` (process memory) cannot.
* **`clock_map_*`** — `clock_map.load_clock_map(<job dir>/logs/<clock_map.FILENAME>)`, with
  the job dir resolved through `execution.job_artifact_dir(campaign, "<config>/<run>")`;
  when there is none, `clock_map.find_run_clock_map(<run dir>)` (roqsim streams its own map
  beside the recording). The `.info` gives `source`, `samples`, `wall_span_s`, `sim_span_s`.
  A run with no map reports `clock_map.SOURCE_NONE`, which is a finding, not an error: its
  log is wall-time only. The two spans are the same window on each clock, so their ratio is
  the simulated seconds bought per wall second.
* **`probed`** — `1` when `campaign_data.probed_runs(campaign_path)` contains
  `"<config_name>/<run_id>"`, else `0`. Resolved once per campaign; the ledger is absent for
  every campaign nobody touched, and then no manifest is read at all. It is a **separate
  column, never folded into `status`**: a probed run can still pass, and putting a human's
  intervention into the measured outcome is the mistake that keeping `killed` out of
  `num_failed` avoids. Its granularity follows the *job*: with `runs_per_job > 1` the whole
  packed job is marked, since which of its runs was in flight cannot be recovered — it
  over-excludes rather than admitting a perturbed run. That wording is the column note the
  writer attached (`_STATIC_COLUMN_NOTES`), also deleted with it, and worth reattaching.

## `param_*` columns

* Column name is `param_<key>`, over the union of keys across **all** units'
  `params_json` (including composition-failed ones), sorted, and skipping any key whose
  `param_` name would collide with a fixed column. So a run whose params differ from its
  siblings still gets every sibling's column, NULL where it has no value — the table is one
  shape for the whole campaign.
* Type is inferred per column from the *values* with `csv_types.widen`, starting at UNKNOWN
  and folding in each unit that has the key. A list/dict value is JSON-encoded first (so it
  widens to text). This is what makes `ORDER BY param_wind_strength` and
  `WHERE param_speed > 0.5` mean what they say instead of comparing strings.
* Values are written through `csv_types.sql_value(value, type)` — the same conversion the
  metric ingest uses.

## Composition-failed units

A `unit` with `status = 'composition_failed'` has no `config_name` and no directory on disk,
so the config-dir walk cannot see it. Those units are carried separately, keyed by
`paramset_id` (their only identity), and appended as **run-less rows**: `config_name` =
that identity, `run_id` NULL, `status` `'composition_failed'`, `passed` 0, `probed` 0, every
other run-derived column NULL, and the `param_*` values filled in — they are what the search
proposed and what turned out to be unrealizable. A campaign that could not build half of
what it proposed must not read as one that proposed less. (`index_views.run_view` already
has the equivalent UNION arm; the port should reuse that rule rather than restate it.)

## Tests that were deleted with it

`tests/results_processing/test_runs_table.py` (the timing/sysinfo columns and the
`param_*` typing) and one case in `tests/common/test_interventions.py`
(`probed` is set without touching `status`). Both are re-created against the index in
`tests/results_processing/test_runs_table.py`, which drives `campaign.db` directly
rather than through `test.xml` — the `available_mem` quantity trap included, and one
case that pins the `test.xml` fallback for a run directory the store never recorded.

## Still missing from the index

`postprocessing_steps` **is now ported** —
`campaign_ingest.build_postprocessing_steps_table`, called at the *end* of
`ingest_campaign` because `name_map` is only complete after the file walk; building it
where `runs` is built would resolve `table_name` to NULL for every step. The curated
`_STATIC_COLUMN_NOTES` / `_POSE_CONTRACT_NOTES` went with the same writer and are restored
alongside it as `campaign_ingest.record_column_notes`, registered through
`index_schema.record_note(..., NOTE_DOC)` and attached only to tables this campaign
actually wrote. Tests: `tests/results_processing/test_postprocessing_steps.py`.

`run_health` was built by the same retired writer and is **not** ported. It is still a live
surface (`data_query._TABLE_DESCRIPTIONS`, the MCP results prompts,
`config.results_processing.health_checks`, `robovast_nav`'s check plugin), so it is a
regression, but it is not part of `runs` and it is not mechanical. A check is `check(conn)`
and issues its own SQL against the campaign's derived tables (`robovast_nav.health_checks`),
which under one index means Postgres placeholders *and* a campaign predicate the plugin has
no way to know. Porting it is a change to the health-check plugin contract, not a change to
an ingest.
