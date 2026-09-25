# Copyright (C) 2026 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""The flat views a caller starts from, ported to the index.

``run_view`` exists because every per-run question needs ``run JOIN unit`` -- ``run_id`` is
unique only *within* a configuration -- and a forgotten join does not raise, it silently
returns rows from the wrong configs. Making the join a property of the schema rather than
something the caller has to remember is the whole point, and that reasoning is unchanged by
where the rows live.

Two things did have to change, and both were found by diffing against SQLite rather than by
reading documentation:

**``json_tree`` has no Postgres equivalent**, so ``config_view`` is a recursive CTE. Getting
it *close* would have been worse than not having it: the documented way to use this view is
``WHERE fullkey LIKE '$.execution%'``, so a path spelled differently returns nothing rather
than erroring. Two details are therefore reproduced exactly --

* **key quoting.** SQLite emits a key bare only when it is purely alphanumeric starting with
  a letter; anything else is quoted. So it writes ``$.postprocessing`` but
  ``$."results_processing"``, and a port that quoted uniformly (or never) would break every
  ``LIKE`` a user has written.
* **the type vocabulary.** SQLite reports ``text``/``integer``/``real``; Postgres reports
  ``string``/``number``. A query filtering ``WHERE type = 'text'`` would match nothing.
* **booleans as 1 and 0.** SQLite has no boolean type, so ``json_tree`` renders JSON
  ``true`` as the integer ``1``. Nobody would guess that, and a reader of a boolean config
  key would silently get a different value.

Verified on a real campaign's ``.vast``: 238 rows, identical on ``fullkey``, ``key``,
``type`` and ``value``.

``parent`` is the one column that is *not* reproduced literally. SQLite's is an opaque
internal row id (0, 13, 2203 -- offsets into its parse), so there is nothing meaningful to
match; this emits the parent's ``fullkey`` instead, which is at least addressable. Callers
are told to descend by ``fullkey`` anyway.
"""

import logging

from psycopg import errors

from robovast.results_processing import index_schema, index_scope
from robovast_decode.runs import RUNLESS_UNIT_STATUSES

logger = logging.getLogger(__name__)

#: Views over the campaign record. Names and listing order as before.
CAMPAIGN_VIEW_NAMES = ("run_view", "config_view", "container_failure_view")

#: Views over the measurements.
METRIC_VIEW_NAMES = ("run_validity_view", "pose_track_view")


def _c(table: str) -> str:
    """A campaign-record table, schema-qualified."""
    return index_schema.qualified(table, index_schema.CAMPAIGN_SCHEMA)


def _resolve(conn, schema: str) -> str:
    """A schema name, resolving the metric schema's empty string to the live one.

    Not ``public``. Metric tables live wherever the connection's ``search_path`` points,
    which a deployment sets and the tests set per case -- assuming ``public`` finds no
    tables and silently produces no views, which reads as "this campaign has no
    measurements".
    """
    if schema:
        return schema
    return conn.execute("SELECT current_schema()").fetchone()[0] or "public"


def _tables_in(conn, schema: str) -> set:
    """Table names present in *schema*.

    Base tables only: ``information_schema.tables`` lists views too, and a view built over these
    is not one of the things they are read for -- a rebuild would offer the previous run's own
    output as an input to itself.
    """
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = %s AND table_type = 'BASE TABLE'",
        (_resolve(conn, schema),)).fetchall()
    return {r[0] for r in rows}


def _columns_in(conn, schema: str, table: str) -> set:
    """Column names of one table, empty when it has none.

    Needed beside :func:`_tables_in` because a probe may be added to the sampler at any
    time, so ``system_usage`` has no fixed column set: two campaigns a month apart have
    different ones, and a view naming a column the older rows lack takes down the columns
    it *could* have answered.
    """
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s",
        (_resolve(conn, schema), table)).fetchall()
    return {r[0] for r in rows}


#: ``json_tree`` in a recursive CTE -- see the module docstring on why the quoting and the
#: type names are spelled out rather than left to Postgres' defaults.
_CONFIG_TREE = """
    WITH RECURSIVE tree AS (
        SELECT c.campaign_id, '$'::text AS fullkey, NULL::text AS key,
               NULL::text AS parent, c.config_json::jsonb AS node
        FROM {campaign} c
        UNION ALL
        SELECT t.campaign_id,
               CASE WHEN jsonb_typeof(t.node) = 'array'
                    THEN t.fullkey || '[' || (e.idx - 1)::text || ']'
                    WHEN e.key ~ '^[A-Za-z][A-Za-z0-9]*$'
                    THEN t.fullkey || '.' || e.key
                    ELSE t.fullkey || '."' || replace(e.key, '"', '\\"') || '"' END,
               CASE WHEN jsonb_typeof(t.node) = 'array'
                    THEN (e.idx - 1)::text ELSE e.key END,
               t.fullkey, e.value
        FROM tree t
        CROSS JOIN LATERAL (
            SELECT k AS key, v AS value, 0 AS idx
            FROM jsonb_each(CASE WHEN jsonb_typeof(t.node) = 'object'
                                 THEN t.node ELSE '{{}}'::jsonb END) AS x(k, v)
            UNION ALL
            SELECT NULL, v, i
            FROM jsonb_array_elements(CASE WHEN jsonb_typeof(t.node) = 'array'
                                           THEN t.node ELSE '[]'::jsonb END)
                 WITH ORDINALITY AS y(v, i)
        ) e
    )
    SELECT campaign_id, fullkey, key, parent,
           CASE jsonb_typeof(node)
               WHEN 'string' THEN 'text'
               WHEN 'number' THEN CASE WHEN node::text ~ '[.eE]' THEN 'real' ELSE 'integer' END
               WHEN 'boolean' THEN CASE WHEN node::text = 'true' THEN 'true' ELSE 'false' END
               ELSE jsonb_typeof(node) END AS type,
           CASE WHEN jsonb_typeof(node) IN ('object', 'array') THEN NULL
                -- SQLite has no boolean type, so json_tree renders JSON true as the
                -- integer 1. A reader of a boolean config key gets that, not 'true'.
                WHEN jsonb_typeof(node) = 'boolean'
                     THEN CASE WHEN node::text = 'true' THEN '1' ELSE '0' END
                ELSE node #>> '{{}}' END AS value
    FROM tree
"""


def campaign_view_sql(conn) -> dict:
    """``{view: SELECT}`` for the campaign-record tables that exist.

    Checked rather than assumed: a view over a missing table is created happily and then
    fails at query time with a confusing "relation does not exist". When ``job`` or
    ``batch`` is absent its columns are selected as NULL rather than dropped, so
    ``run_view`` keeps the **same column set** whatever the index holds -- the caller
    writes one query and a missing host record reads as NULL, not as a different schema.
    """
    have = _tables_in(conn, index_schema.CAMPAIGN_SCHEMA)
    views = {}

    if {"run", "unit"} <= have:
        if "job" in have:
            host = "j.job_dir, j.sysinfo_json"
            join = (f"LEFT JOIN {_c('job')} j "
                    "ON r.job_id = j.id AND j.campaign_id = r.campaign_id")
        else:
            host = "NULL AS job_dir, NULL AS sysinfo_json"
            join = ""
        # LEFT JOIN even though unit.batch_id is NOT NULL: an orphan id would silently
        # DROP runs, which is the failure class this view exists to prevent.
        if "batch" in have:
            batch = "b.idx AS batch"
            bjoin = (f"LEFT JOIN {_c('batch')} b "
                     "ON u.batch_id = b.id AND b.campaign_id = u.campaign_id")
        else:
            batch = "NULL AS batch"
            bjoin = ""
        # Selected as NULL when no campaign in the index has it, for the reason `_columns_in`
        # exists: the index holds campaigns recorded before the sim/sut channels were kept,
        # and a view naming a column those rows lack takes down every column it could have
        # answered.
        channels = ("u.channels_json"
                    if "channels_json" in _columns_in(conn, index_schema.CAMPAIGN_SCHEMA,
                                                      "unit")
                    else "NULL AS channels_json")
        # A unit with no run rows is dropped by the join alone -- and with it the only
        # record that the cell was part of the design. Added back as one run-less row each,
        # or a campaign silently reports the cells that happened to work as its whole shape:
        # the draws a search could not compose, and the configurations a sweep declared and
        # never got back.
        runless = ", ".join(f"'{status}'" for status in RUNLESS_UNIT_STATUSES)
        views["run_view"] = f"""
            SELECT r.campaign_id, u.config_name, r.run_id, r.status, r.passed, r.duration_s,
                   r.errors, r.failures, r.tests, r.start_time, r.failure_message,
                   u.params_json, {channels}, u.objective, u.paramset_id, {batch}, {host}
            FROM {_c('run')} r
            JOIN {_c('unit')} u ON r.unit_id = u.id AND u.campaign_id = r.campaign_id
            {bjoin}
            {join}
            UNION ALL
            SELECT u.campaign_id,
                   COALESCE(NULLIF(u.config_name, ''), u.paramset_id) AS config_name,
                   NULL AS run_id, u.status, 0 AS passed, NULL AS duration_s,
                   NULL AS errors, NULL AS failures, NULL AS tests,
                   NULL AS start_time, NULL AS failure_message,
                   u.params_json, {channels}, u.objective, u.paramset_id, {batch},
                   NULL AS job_dir, NULL AS sysinfo_json
            FROM {_c('unit')} u
            {bjoin}
            WHERE u.status IN ({runless})
        """

    if "container_failure" in have:
        # The UNION ALL is the same guard run_view uses, for the same reason: expanding an
        # empty array yields NO rows, so a failure whose runs could not be resolved would
        # vanish -- silently, and exactly when something already went wrong enough that the
        # runner could not name them.
        views["container_failure_view"] = f"""
            SELECT cf.*, je.value AS run_key
            FROM {_c('container_failure')} cf,
                 jsonb_array_elements_text(cf.runs_json::jsonb) je
            UNION ALL
            SELECT cf.*, NULL AS run_key
            FROM {_c('container_failure')} cf
            WHERE cf.runs_json IS NULL
               OR jsonb_array_length(cf.runs_json::jsonb) = 0
        """

    if "campaign" in have:
        # ``value`` is NULL for objects and arrays on purpose: a container row returning a
        # serialized subtree would be truncated by the cell cap into a config that looks
        # complete and is not. A caller descends by fullkey instead, and every row stays
        # small.
        views["config_view"] = _CONFIG_TREE.format(campaign=_c("campaign"))

    return views


#: The metrics-side view. Its SQL is the SQLite one with the casts spelled for Postgres --
#: which is not cosmetic here: ``CAST(wall_ts AS REAL)`` through Postgres' 4-byte ``real``
#: turns a 60-second window into 128 seconds, and this view divides by that window. See
#: :mod:`robovast.results_processing.index_dialect`.
def metric_view_sql(conn) -> dict:
    """``{view: SELECT}`` for the measurement tables that exist.

    ``run_validity_view`` answers the question a reader of a campaign actually has: *was
    this run a clean observation of the system under test, or partly a measurement of its
    CPU quota?* It exists because the raw form is a trap three ways and every consumer was
    re-deriving it -- ``nr_throttled``/``nr_periods`` are monotonic counters, so a ``SUM``
    is meaningless and a bare ``MAX`` includes whatever happened before the trial window;
    the *ratio* carries the meaning, not the count; and the threshold separating "binding"
    from "noise" is calibrated rather than obvious.

    It flags and never filters. A capped run stays in the results with ``quota_bound = 1``
    beside it, because a run silently dropped is worse than one labelled honestly -- and
    because throttling is a screen, not a verdict: it says a resource explanation is
    *available* for a failure, not that the stack misbehaved.
    """
    from .advice import (STALL_WARN_RATIO,  # pylint: disable=import-outside-toplevel
                         THROTTLE_WARN_RATIO)

    views = {}
    tables = _tables_in(conn, "")
    pose_track = _pose_track_sql(conn, tables)
    if pose_track:
        views["pose_track_view"] = pose_track
    if "system_usage" not in tables:
        return views

    columns = _columns_in(conn, "", "system_usage")
    # Selected as NULL when the sampler that recorded this campaign had no PSI probe -- the
    # same treatment a missing ``job`` table gets, and for the same reason: one column set
    # whatever the index holds, so an older campaign answers "not measured" rather than
    # "no contention".
    if "cpu_stall_full_usec" in columns:
        stall_full = ("MAX(cpu_stall_full_usec) - MIN(cpu_stall_full_usec) "
                      "AS stalled_full_usec")
    else:
        stall_full = "NULL::bigint AS stalled_full_usec"
    if "cpu_stall_some_usec" in columns:
        stall_some = ("MAX(cpu_stall_some_usec) - MIN(cpu_stall_some_usec) "
                      "AS stalled_some_usec")
    else:
        stall_some = "NULL::bigint AS stalled_some_usec"

    views["run_validity_view"] = f"""
        WITH per_run AS (
            SELECT campaign_id, config_name, run_id, container,
                   MAX(nr_periods) - MIN(nr_periods) AS periods,
                   MAX(nr_throttled) - MIN(nr_throttled) AS throttled,
                   MAX(throttled_usec) - MIN(throttled_usec) AS throttled_usec,
                   {stall_some},
                   {stall_full},
                   -- The window's own wall span, and the only honest denominator for a
                   -- stall total: a microsecond count means nothing without the time it
                   -- was drawn from, exactly as a throttle count means nothing without
                   -- nr_periods. double precision, NOT real -- see index_dialect.
                   (MAX(CAST(wall_ts AS double precision))
                    - MIN(CAST(wall_ts AS double precision))) * 1000000.0 AS span_usec
            FROM system_usage
            WHERE in_window = 1 AND nr_periods IS NOT NULL
            GROUP BY campaign_id, config_name, run_id, container)
        SELECT campaign_id, config_name, run_id, container, periods, throttled,
               throttled_usec, stalled_some_usec, stalled_full_usec,
               CASE WHEN periods > 0
                    THEN CAST(throttled AS double precision) / periods END AS throttle_ratio,
               CASE WHEN span_usec > 0 AND stalled_full_usec IS NOT NULL
                    THEN stalled_full_usec / span_usec END AS stall_ratio,
               CASE WHEN periods > 0
                         AND CAST(throttled AS double precision) / periods
                             >= {THROTTLE_WARN_RATIO}
                    THEN 1 ELSE 0 END AS quota_bound,
               -- Contention is what is LEFT once the container's own ceiling is ruled out.
               -- Throttling raises the stall counter too, so the two cannot be separated
               -- by subtraction; the ceiling is attributed first because its remedy is a
               -- line in the campaign's own file. NULL, not 0, where the probe is absent:
               -- silence is not a pass.
               CASE WHEN stalled_full_usec IS NULL OR span_usec <= 0 THEN NULL
                    WHEN stalled_full_usec / span_usec >= {STALL_WARN_RATIO}
                         AND NOT (periods > 0
                                  AND CAST(throttled AS double precision) / periods
                                      >= {THROTTLE_WARN_RATIO})
                    THEN 1 ELSE 0 END AS contended
        FROM per_run
    """
    return views


#: The key one track is identified by. ``source`` is the table, because the same entity can be
#: recorded by two producers (TF and the simulator) and their samples must not interleave.
_TRACK_KEY = "source, campaign_id, config_name, run_id, frame"

#: What a branch of the view selects from a pose table, beside the clock and ``position.x``
#: that :func:`~robovast.results_processing.campaign_ingest.pose_clock` answers for, and beside
#: ``position.z``/``orientation.yaw``, which a branch substitutes for when they are absent.
_BRANCH_COLUMNS = {"campaign_id", "config_name", "run_id", "frame", "position.y"}


def _pose_track_sql(conn, tables) -> str | None:
    """``pose_track_view``: one row per recorded track, summarised over every one of its poses.

    A track is one entity (``frame``) of one run, as one pose-contract table recorded it. The
    view spans every such table the index holds -- ``poses`` from a bag, ``sim_poses`` from
    the simulator, whatever a later producer writes -- because the contract, not the table
    name, is what makes the arithmetic valid. It exists for the reason ``run_validity_view``
    does: the derivation is a trap, and one every consumer of these tables has to walk past.
    A speed differenced on the arrival clock measures the transport; one ordered by it is
    ordered arbitrarily within a tick; and a length summed without the campaign predicate
    joins two campaigns' tracks into one with a jump across the map.

    Each table contributes on its own measurement clock (:func:`campaign_ingest.pose_clock`).
    A sample with no measurement time -- a latched ``/tf_static`` transform -- is not a point
    on a track and is left out. ``position.z`` is part of the length where the table has it,
    so a drone or an end effector moving vertically has one.

    A reposition is travel here: a body spawned at the world origin and then placed at its
    start pose contributes that jump to ``length_m`` and ``max_speed_m_s``. The view does not
    guess a threshold to drop it; ``max_step_m`` makes it visible instead. The window partitions by the
    whole key, which is what lets ``WHERE campaign_id = ...`` reach every table underneath
    rather than being applied after the corpus has been windowed.
    """
    from .campaign_ingest import \
        pose_clock  # pylint: disable=import-outside-toplevel

    branches = []
    for table in sorted(tables):
        columns = _columns_in(conn, "", table)
        clock = pose_clock(columns)
        # Every column a branch names, not only the ones that make it a pose table: a branch
        # naming a column its table lacks raises on CREATE VIEW, which `_rebuild` catches -- so
        # one odd table would take the whole view away from every campaign.
        if clock is None or not _BRANCH_COLUMNS <= columns:
            continue
        z = 'CAST("position.z" AS double precision)' if "position.z" in columns else "0.0"
        yaw = ('CAST("orientation.yaw" AS double precision)' if "orientation.yaw" in columns
               else "NULL::double precision")
        branches.append(f"""
            SELECT '{table}'::text AS source, campaign_id, config_name,
                   CAST(run_id AS integer) AS run_id, frame,
                   CAST("{clock}" AS double precision) AS t,
                   CAST("position.x" AS double precision) AS x,
                   CAST("position.y" AS double precision) AS y,
                   {z} AS z, {yaw} AS yaw
            FROM "{table}"
            WHERE "{clock}" IS NOT NULL""")
    if not branches:
        return None
    return f"""
        WITH p AS ({" UNION ALL ".join(branches)}),
             d AS (SELECT {_TRACK_KEY}, t, x, y, z,
                          SQRT(POWER(x - LAG(x) OVER w, 2) + POWER(y - LAG(y) OVER w, 2)
                               + POWER(z - LAG(z) OVER w, 2)) AS step_m,
                          t - LAG(t) OVER w AS dt,
                          FIRST_VALUE(x) OVER w AS start_x, FIRST_VALUE(y) OVER w AS start_y,
                          FIRST_VALUE(z) OVER w AS start_z,
                          FIRST_VALUE(yaw) OVER w AS start_yaw,
                          LAST_VALUE(x) OVER wf AS end_x, LAST_VALUE(y) OVER wf AS end_y,
                          LAST_VALUE(z) OVER wf AS end_z, LAST_VALUE(yaw) OVER wf AS end_yaw
                   FROM p
                   WINDOW w AS (PARTITION BY {_TRACK_KEY} ORDER BY t),
                          wf AS (PARTITION BY {_TRACK_KEY} ORDER BY t
                                 ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING))
        SELECT {_TRACK_KEY},
               COUNT(*) AS points,
               COALESCE(SUM(step_m), 0) AS length_m,
               MAX(t) - MIN(t) AS duration_s,
               CASE WHEN MAX(t) > MIN(t)
                    THEN COALESCE(SUM(step_m), 0) / (MAX(t) - MIN(t)) END AS avg_speed_m_s,
               MAX(CASE WHEN dt > 0 THEN step_m / dt END) AS max_speed_m_s,
               MAX(step_m) AS max_step_m,
               MIN(start_x) AS start_x, MIN(start_y) AS start_y, MIN(start_z) AS start_z,
               MIN(start_yaw) AS start_yaw,
               MIN(end_x) AS end_x, MIN(end_y) AS end_y, MIN(end_z) AS end_z,
               MIN(end_yaw) AS end_yaw,
               MIN(x) AS min_x, MAX(x) AS max_x, MIN(y) AS min_y, MAX(y) AS max_y,
               MIN(z) AS min_z, MAX(z) AS max_z
        FROM d
        GROUP BY {_TRACK_KEY}
    """


def create_views(conn) -> list:
    """Create the views this index can support; return their names.

    Objects in the index rather than views on each connection: there is one index whose
    shape the ingest controls, so defining them once means a reader does not pay to rebuild
    them per connection -- including the plugin panels, which open their own.

    One index is shared by every campaign, so this runs at the end of *every* ingest and
    two of them can be in it at once. Both things that makes hard are handled here: the
    :func:`~robovast.results_processing.index_schema.ddl_lock` serialises the writers, and
    the whole rebuild is one transaction so a reader never observes the state between the
    ``DROP`` and the ``CREATE`` -- it waits for the swap and then sees the new view, rather
    than being told the relation does not exist, which reads as a campaign with no runs.
    """
    with index_schema.ddl_lock(conn), conn.transaction():
        created = _rebuild(conn)
    logger.debug("index: created views %s", ", ".join(created) or "(none)")
    return created


def _rebuild(conn) -> list:
    """Drop and recreate every supported view. Caller holds the lock and the transaction."""
    created = []
    definitions = {**campaign_view_sql(conn), **metric_view_sql(conn)}
    for name, body in definitions.items():
        conn.execute(f'DROP VIEW IF EXISTS "{name}" CASCADE')
        # A view is skipped when the index cannot support it, rather than taking the ingest
        # down with it. The callers above already decide this by which TABLES exist; a
        # column can be missing for the same reason a table can -- a campaign store written
        # before a column was added, or one from a campaign that ended before it was
        # populated -- and refusing the whole ingest for it would make the index unable to
        # hold exactly the campaigns worth reading.
        #
        # Deliberately narrow: only "that relation/column is not there" is tolerated. A
        # syntax error or a type mismatch is this module's own defect and must still raise,
        # or a view could quietly stop existing everywhere and read as "no data". A
        # SAVEPOINT rather than a transaction, since the caller opened one: an unsupported
        # view rolls back to here and the views around it still commit together.
        try:
            with conn.transaction():
                # security_invoker is not decoration. A view runs with its OWNER's rights
                # by default, so the row-level security on the tables underneath does not
                # apply to it -- which is how an unscoped `FROM run_view` served another
                # campaign's runs to the browser. Postgres 15+ makes the view read as the
                # caller, so the caller's campaign scope applies. Created WITH it rather
                # than altered afterwards: between the two statements the view is live and
                # unscoped.
                conn.execute(f'CREATE VIEW "{name}" WITH (security_invoker = true) '
                             f"AS {body}")
                index_scope.secure_view(conn, name)
        except (errors.UndefinedColumn, errors.UndefinedTable, errors.UndefinedObject) as exc:
            logger.info("index: view %s not created -- %s", name,
                        str(exc).splitlines()[0])
            continue
        created.append(name)
    return created
