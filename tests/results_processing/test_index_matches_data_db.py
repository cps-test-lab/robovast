# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The queries the product actually runs, answered identically by both paths.

Every other test here checks one mechanism. This one checks the thing that matters to a
user: that the SQL already shipping in the campaign advice, the web UI's panels and the MCP
prompts returns the *same numbers* from the index as it did from ``data.db``.

It is the check that should gate the cutover, because eight bugs in this port were found by
differential test and none of them raised an error -- a percentile returning the maximum, a
60-second window reading as 128, a JSON boolean rendering as ``true`` instead of ``1``.
Each produced a believable number.

Needs a real campaign, so it skips unless both are pointed at:

* ``ROBOVAST_TEST_PG_DSN``  -- a Postgres to ingest into
* ``ROBOVAST_TEST_CAMPAIGN_DIR`` -- a campaign results directory (its CSVs)
* ``ROBOVAST_TEST_CAMPAIGN_DB``  -- that same campaign's ``_execution/data.db``
"""

import os
import sqlite3

import pytest

from robovast.results_processing import (campaign_ingest, data_query, index_dialect,
                                         index_query, index_views)

DSN = os.environ.get("ROBOVAST_TEST_PG_DSN")
CAMPAIGN_DIR = os.environ.get("ROBOVAST_TEST_CAMPAIGN_DIR")
CAMPAIGN_DB = os.environ.get("ROBOVAST_TEST_CAMPAIGN_DB")

pytestmark = pytest.mark.skipif(
    not (DSN and CAMPAIGN_DIR and CAMPAIGN_DB),
    reason="needs ROBOVAST_TEST_PG_DSN, ROBOVAST_TEST_CAMPAIGN_DIR and "
           "ROBOVAST_TEST_CAMPAIGN_DB")

CAMPAIGN_ID = "differential-campaign"
SCHEMA = "diff_test"

#: Real SQL, copied from where it ships. Each is a question somebody asks of a campaign.
QUERIES = {
    # frontend/ui/src/lib/panels/dataProvider.ts -- the dominant panel query.
    "panel downsample": (
        'SELECT CAST(CAST("timestamp" AS REAL) * 2 AS INTEGER) AS bucket, COUNT(*) AS n '
        'FROM poses WHERE config_name = (SELECT MIN(config_name) FROM poses) '
        'GROUP BY 1 ORDER BY 1'),
    # dataProvider.ts -- nearest-sample lookup.
    "panel nearest sample": (
        'SELECT "timestamp", frame FROM poses '
        'ORDER BY ABS(CAST("timestamp" AS REAL) - 30.0) LIMIT 1'),
    # results_processing/advice.py -- USAGE_SQL.
    "advice cpu percentile": (
        "SELECT container, PERCENTILE(cores, 95) AS cpu_p95, MAX(cores) AS cpu_peak, "
        "COUNT(*) AS ticks FROM ("
        "  SELECT container, config_name, run_id, timestamp, "
        "         SUM(cpu_percent) / 100.0 AS cores "
        "  FROM resource_usage WHERE in_window = 1 "
        "  GROUP BY container, config_name, run_id, timestamp) t "
        "GROUP BY container ORDER BY container"),
    # advice.py -- SYSTEM_MEM_SQL.
    "advice system memory": (
        "SELECT container, MAX(memory_peak) AS mem_peak FROM system_usage "
        "WHERE in_window = 1 AND memory_peak IS NOT NULL GROUP BY container "
        "ORDER BY container"),
    # advice.py -- THROTTLE_SQL's inner shape.
    "advice throttle": (
        "WITH per_run AS ("
        "  SELECT config_name, run_id, container, "
        "         MAX(nr_periods) - MIN(nr_periods) AS periods, "
        "         MAX(nr_throttled) - MIN(nr_throttled) AS throttled "
        "  FROM system_usage WHERE in_window = 1 AND nr_periods IS NOT NULL "
        "  GROUP BY config_name, run_id, container) "
        "SELECT container, SUM(periods) AS periods, SUM(throttled) AS throttled "
        "FROM per_run GROUP BY container ORDER BY container"),
    # The pose contract's documented speed query -- scoped to ONE run, which is not a
    # detail. Unscoped it spans every run, and since each run stamps the same sim times,
    # 59402 of 64468 rows tie: LAG then differences a position against one from a
    # different run, and the two engines break the tie differently. Both answers are
    # "right" for their ordering, and neither means anything. The documentation says to
    # filter by config_name and run_id for exactly this reason, and the differential
    # caught the query written without it.
    "pose speed window": (
        'SELECT COUNT(*) AS n, ROUND(MAX(speed)) AS fastest FROM ('
        '  SELECT (SQRT(POWER(x - px, 2) + POWER(y - py, 2)) / (stamp - ps)) AS speed '
        '  FROM (SELECT stamp, "position.x" x, "position.y" y, '
        '               LAG(stamp) OVER w ps, LAG("position.x") OVER w px, '
        '               LAG("position.y") OVER w py '
        '        FROM poses '
        '        WHERE frame = \'base_link\' AND config_name = \'plainmid-1\' '
        '          AND run_id = 0 '
        '        WINDOW w AS (ORDER BY stamp)) s '
        '  WHERE ps IS NOT NULL AND stamp > ps) v'),
    "run log severity": (
        "SELECT severity, COUNT(*) AS n FROM run_log GROUP BY 1 ORDER BY 1"),
    "behaviour rollup": (
        "SELECT status_name, COUNT(*) AS n FROM nav2_behaviors GROUP BY 1 ORDER BY 1"),
    "clearance extremes": (
        "SELECT config_name, MIN(data) AS lo, MAX(data) AS hi "
        "FROM rosbag2_clearance GROUP BY 1 ORDER BY 1"),
    "row counts": (
        "SELECT COUNT(*) AS poses, "
        "(SELECT COUNT(*) FROM sim_poses) AS sim_poses, "
        "(SELECT COUNT(*) FROM run_log) AS run_log FROM poses"),
}


@pytest.fixture(name="ingested", scope="module")
def _ingested():
    psycopg = pytest.importorskip("psycopg")
    previous = os.environ.get("ROBOVAST_INDEX_DSN")
    os.environ["ROBOVAST_INDEX_DSN"] = f"{DSN} options=-csearch_path={SCHEMA}"
    with psycopg.connect(DSN, autocommit=True) as setup:
        for statement in (f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE",
                          "DROP SCHEMA IF EXISTS campaign CASCADE",
                          f"CREATE SCHEMA {SCHEMA}"):
            setup.execute(statement)
    with index_query.open_index(readonly=False) as conn:
        campaign_ingest.ingest_campaign(conn, CAMPAIGN_DIR, CAMPAIGN_ID)
        index_views.create_views(conn)
    yield
    with psycopg.connect(DSN, autocommit=True) as teardown:
        teardown.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        teardown.execute("DROP SCHEMA IF EXISTS campaign CASCADE")
    if previous is None:
        os.environ.pop("ROBOVAST_INDEX_DSN", None)
    else:
        os.environ["ROBOVAST_INDEX_DSN"] = previous


def _from_data_db(sql):
    """The oracle: the same SQL against the campaign's own data.db."""
    conn = sqlite3.connect(f"file:{CAMPAIGN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    data_query._register_aggregates(conn)  # pylint: disable=protected-access
    try:
        return [tuple(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def _normalise(rows):
    """Floats to a fixed precision; everything else as-is.

    Not a fudge: the two engines differ in the last bits of a double, and this port's
    contract is the same *number*, not the same bit pattern. Six decimals is far tighter
    than any difference that would matter and far looser than float noise.
    """
    out = []
    for row in rows:
        out.append(tuple(
            round(float(v), 6) if isinstance(v, float) else
            (float(v) if hasattr(v, "as_integer_ratio") and not isinstance(v, int) else v)
            for v in row))
    return out


@pytest.mark.parametrize("name", sorted(QUERIES))
def test_the_shipping_queries_answer_identically(ingested, name):
    """One test per real query, so a failure names which question got a new answer."""
    sql = QUERIES[name]

    expected = _normalise(_from_data_db(sql))
    result = index_query.query_index(sql, max_rows=5000)
    got = _normalise([tuple(row[c] for c in result["columns"]) for row in result["rows"]])

    assert got == expected, f"{name} differs between data.db and the index"


def test_the_translation_is_what_makes_the_panel_query_agree(ingested):
    """Guards the premise rather than the fix.

    If Postgres ever stopped differing here, the dialect layer would be dead weight and
    should be deleted rather than carried. This fails when that day comes, which is the
    only way anyone would find out.
    """
    psycopg = pytest.importorskip("psycopg")
    sql = QUERIES["panel downsample"]

    with psycopg.connect(f"{DSN} options=-csearch_path={SCHEMA}", autocommit=True) as conn:
        untranslated = [tuple(r) for r in conn.execute(sql).fetchall()]

    assert _normalise(untranslated) != _normalise(_from_data_db(sql)), (
        "Postgres no longer differs on CAST -- index_dialect may be removable")
    assert index_dialect.translate(sql) != sql
