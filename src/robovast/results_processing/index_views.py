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

from robovast.results_processing import index_schema

logger = logging.getLogger(__name__)

#: Views over the campaign record. Names and listing order as before.
CAMPAIGN_VIEW_NAMES = ("run_view", "config_view", "container_failure_view")

#: Views over the measurements.
METRIC_VIEW_NAMES = ("run_validity_view",)


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
    """Table names present in *schema*."""
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
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
        # A composition-failed unit has no run rows, so the join alone drops it -- and with
        # it the only record that the draw was attempted. Added back as one run-less row,
        # or a search campaign silently reports only the draws that happened to work.
        views["run_view"] = f"""
            SELECT r.campaign_id, u.config_name, r.run_id, r.status, r.passed, r.duration_s,
                   r.errors, r.failures, r.tests, r.start_time, r.failure_message,
                   u.params_json, u.objective, u.paramset_id, {batch}, {host}
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
                   u.params_json, u.objective, u.paramset_id, {batch},
                   NULL AS job_dir, NULL AS sysinfo_json
            FROM {_c('unit')} u
            {bjoin}
            WHERE u.status = 'composition_failed'
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
    if "system_usage" not in _tables_in(conn, ""):
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


def create_views(conn) -> list:
    """Create the views this index can support; return their names.

    Ordinary views rather than temporary ones. ``data.db`` had to use TEMP views because it
    was attached read-only and a store predating a table would carry a view referencing it;
    here there is one index whose shape the ingest controls, so defining them once means a
    reader does not pay to rebuild them per connection -- including the plugin panels, which
    open their own.
    """
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
        # or a view could quietly stop existing everywhere and read as "no data".
        try:
            with conn.transaction():
                conn.execute(f'CREATE VIEW "{name}" AS {body}')
        except (errors.UndefinedColumn, errors.UndefinedTable, errors.UndefinedObject) as exc:
            logger.info("index: view %s not created -- %s", name,
                        str(exc).splitlines()[0])
            continue
        created.append(name)
    logger.debug("index: created views %s", ", ".join(created) or "(none)")
    return created
