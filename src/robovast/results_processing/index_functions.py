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

"""The functions a RoboVAST query expects, defined once in the index.

SQLite ships none of ``STDDEV``, ``VARIANCE``, ``MEDIAN``, ``PERCENTILE``, ``REGEXP``, and
not even ``SQRT`` (it is a compile-time option), so ``data_query`` registered all six on
every connection. Queries have been written against those **names** ever since: the
campaign advice, the web UI's CPU percentile row, ``search_run_logs``, ``robovast_nav``'s
distance queries, and -- because ``describe_campaign_data`` and the MCP prompts document
them -- every query an agent has ever written against a campaign.

Moving to Postgres must not quietly change what those names mean, so the three it does not
have are created under exactly the same spellings and the same semantics:

* ``STDDEV`` / ``VARIANCE`` / ``SQRT`` are native. Nothing to do, and nothing that could
  drift.
* ``MEDIAN(col)`` and ``PERCENTILE(col, p)`` are created. ``p`` is **0..100, not 0..1** --
  the existing callers pass ``PERCENTILE(cores, 95)`` -- and the interpolation is linear
  between the two neighbouring samples, which is what the SQLite implementation did and
  what ``percentile_cont`` does natively. Getting the scale wrong here would return the
  1st-percentile value for a query asking for the 95th, which is a plausible number and a
  wrong answer.
* ``REGEXP(pattern, value)`` is created as a function rather than left to Postgres' ``~``
  operator, because the argument order is part of the contract: SQLite's registered
  function takes ``(pattern, value)`` while ``~`` reads ``value ~ pattern``. A silent swap
  would make every log search match nothing.

These are installed once into the database rather than per connection. A connection-scoped
definition would be the closer analogue of what SQLite did, but it would also mean every
reader paying for six ``CREATE`` statements on a connection it may use for one query, and
a plugin panel that opens its own connection would not have them at all.
"""

import logging

logger = logging.getLogger(__name__)

#: Bumped when a definition below changes, so an existing database picks the change up.
#: Without it a redefinition would only reach a freshly created index, and two deployments
#: would disagree about what ``PERCENTILE`` means while both looking healthy.
FUNCTIONS_VERSION = 1

#: Where the version is recorded.
FUNCTIONS_VERSION_TABLE = "_index_functions"

#: ``MEDIAN`` and ``PERCENTILE`` are ordered-set aggregates in Postgres, which is a
#: different *syntax* (``percentile_cont(p) WITHIN GROUP (ORDER BY col)``) from the
#: two-argument call every existing query writes. Wrapping them keeps the call sites
#: unchanged; the wrapper collects into an array and interpolates in the final function,
#: which is what makes ``PERCENTILE(col, p)`` legal at all.
_DEFINITIONS = (
    # NULLs are skipped rather than treated as zero: a column the clock map could not place
    # is legitimately empty, and averaging it as 0 would report a robot at the origin.
    """
    CREATE OR REPLACE FUNCTION _rv_percentile_final(vals double precision[],
                                                    p double precision)
    RETURNS double precision LANGUAGE plpgsql IMMUTABLE AS $$
    DECLARE
        sorted double precision[];
        n integer;
        pos double precision;
        lo integer;
        hi integer;
    BEGIN
        SELECT array_agg(v ORDER BY v) INTO sorted
        FROM unnest(vals) AS v WHERE v IS NOT NULL;
        n := coalesce(array_length(sorted, 1), 0);
        IF n = 0 THEN RETURN NULL; END IF;
        IF n = 1 THEN RETURN sorted[1]; END IF;
        -- p is 0..100 to match every existing caller.
        pos := (greatest(0, least(100, p)) / 100.0) * (n - 1) + 1;
        lo := floor(pos)::integer;
        hi := ceil(pos)::integer;
        IF lo = hi THEN RETURN sorted[lo]; END IF;
        RETURN sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo);
    END $$;
    """,
    # ``p`` has to travel IN the state. Postgres' finalfunc_extra passes the aggregate's
    # direct arguments to the final function as NULLs -- placeholders for type resolution,
    # not values -- so a final function reading ``p`` from there gets nothing. That is not a
    # loud failure: it returned the maximum for every percentile, so PERCENTILE(cores, 5)
    # and PERCENTILE(cores, 95) agreed and both looked like plausible CPU figures.
    #
    # So element 1 of the state is ``p`` and the rest are the values.
    """
    CREATE OR REPLACE FUNCTION _rv_percentile_state(state double precision[],
                                                    v double precision,
                                                    p double precision)
    RETURNS double precision[] LANGUAGE sql IMMUTABLE AS
    $$ SELECT CASE WHEN coalesce(array_length(state, 1), 0) = 0
                   THEN ARRAY[p, v]
                   ELSE array_append(state, v) END $$;
    """,
    """
    CREATE OR REPLACE FUNCTION _rv_percentile_wrap(state double precision[])
    RETURNS double precision LANGUAGE sql IMMUTABLE AS
    $$ SELECT CASE WHEN coalesce(array_length(state, 1), 0) < 2 THEN NULL
                   ELSE _rv_percentile_final(state[2:], state[1]) END $$;
    """,
    """
    DROP AGGREGATE IF EXISTS percentile(double precision, double precision);
    """,
    """
    CREATE AGGREGATE percentile(double precision, double precision) (
        sfunc = _rv_percentile_state,
        stype = double precision[],
        initcond = '{}',
        finalfunc = _rv_percentile_wrap
    );
    """,
    """
    CREATE OR REPLACE FUNCTION _rv_median_final(vals double precision[])
    RETURNS double precision LANGUAGE sql IMMUTABLE AS
    $$ SELECT _rv_percentile_final(vals, 50) $$;
    """,
    """
    DROP AGGREGATE IF EXISTS median(double precision);
    """,
    """
    CREATE AGGREGATE median(double precision) (
        sfunc = array_append,
        stype = double precision[],
        initcond = '{}',
        finalfunc = _rv_median_final
    );
    """,
    # (pattern, value), matching SQLite's registered order -- see the module docstring.
    """
    CREATE OR REPLACE FUNCTION regexp(pattern text, value text)
    RETURNS boolean LANGUAGE sql IMMUTABLE AS
    $$ SELECT CASE WHEN value IS NULL OR pattern IS NULL THEN false
                   ELSE value ~ pattern END $$;
    """,
)


def install(conn) -> bool:
    """Define the functions if this database does not already have this version.

    Returns True when something was installed. Idempotent and cheap to call: the usual
    path is one ``SELECT`` against a one-row table.
    """
    conn.execute(f'CREATE TABLE IF NOT EXISTS "{FUNCTIONS_VERSION_TABLE}" '
                 "(version integer PRIMARY KEY)")
    row = conn.execute(f'SELECT version FROM "{FUNCTIONS_VERSION_TABLE}"').fetchone()
    if row and row[0] >= FUNCTIONS_VERSION:
        return False

    # Applied in dependency order, once, with nothing caught. An earlier draft retried the
    # list and swallowed failures, which would have recorded the version as installed while
    # PERCENTILE did not exist -- and the first symptom would have been a panel returning
    # "function does not exist" long after the deploy that broke it.
    for statement in _DEFINITIONS:
        conn.execute(statement)

    conn.execute(f'DELETE FROM "{FUNCTIONS_VERSION_TABLE}"')
    conn.execute(f'INSERT INTO "{FUNCTIONS_VERSION_TABLE}" (version) VALUES (%s)',
                 (FUNCTIONS_VERSION,))
    logger.info("index: installed query functions v%s", FUNCTIONS_VERSION)
    return True
