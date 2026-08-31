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

from robovast.results_processing import index_schema

logger = logging.getLogger(__name__)

#: Views over the campaign record. Names and listing order as before.
CAMPAIGN_VIEW_NAMES = ("run_view", "config_view", "container_failure_view")

#: Views over the measurements.
METRIC_VIEW_NAMES = ("run_validity_view",)


def _c(table: str) -> str:
    """A campaign-record table, schema-qualified."""
    return index_schema.qualified(table, index_schema.CAMPAIGN_SCHEMA)


def _tables_in(conn, schema: str) -> set:
    """Table names present in *schema*."""
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
        (schema or "public",)).fetchall()
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
        "WHERE table_schema = %s AND table_name = %s", (schema or "public", table)).fetchall()
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


def create_views(conn) -> list:
    """Create the views this index can support; return their names.

    Ordinary views rather than temporary ones. ``data.db`` had to use TEMP views because it
    was attached read-only and a store predating a table would carry a view referencing it;
    here there is one index whose shape the ingest controls, so defining them once means a
    reader does not pay to rebuild them per connection -- including the plugin panels, which
    open their own.
    """
    created = []
    for name, body in campaign_view_sql(conn).items():
        conn.execute(f'DROP VIEW IF EXISTS "{name}" CASCADE')
        conn.execute(f'CREATE VIEW "{name}" AS {body}')
        created.append(name)
    logger.debug("index: created views %s", ", ".join(created) or "(none)")
    return created
