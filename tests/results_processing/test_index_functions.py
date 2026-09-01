# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The query functions, held to what the SQLite ones returned.

The standard here is not "is this percentile correct" but "does it return what the
implementation it replaces returned". ``advice.py`` and the web UI's CPU row have been
reading these numbers for months, and every query an agent wrote against a campaign used
the names ``describe_campaign_data`` documents. A subtly different answer would be
believed.

Set ``ROBOVAST_TEST_PG_DSN`` to run them; without it they skip.
"""

import os
import random
import sqlite3

import pytest

from robovast.results_processing import data_query, index_functions

DSN = os.environ.get("ROBOVAST_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="ROBOVAST_TEST_PG_DSN is not set")


@pytest.fixture(name="pg")
def _pg():
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS fn_test CASCADE")
        conn.execute("CREATE SCHEMA fn_test")
        conn.execute("SET search_path TO fn_test")
        index_functions.install(conn)
        yield conn
        conn.execute("DROP SCHEMA IF EXISTS fn_test CASCADE")


@pytest.fixture(name="sq")
def _sq():
    """SQLite with the aggregates ``data_query`` registers today."""
    conn = sqlite3.connect(":memory:")
    data_query._register_aggregates(conn)  # pylint: disable=protected-access
    yield conn
    conn.close()


_DATASETS = {
    "small": [1.0, 2.0, 3.0, 4.0, 100.0],
    "single": [42.0],
    "pair": [1.0, 2.0],
    "identical": [5.0, 5.0, 5.0, 5.0],
    "negative": [-10.0, -1.0, 0.0, 1.0, 10.0],
    "wide": [round(random.Random(7).uniform(0, 500), 3) for _ in range(97)],
}


@pytest.mark.parametrize("name", sorted(_DATASETS))
@pytest.mark.parametrize("percent", [0, 5, 25, 50, 75, 95, 100])
def test_percentile_matches_the_sqlite_implementation(pg, sq, name, percent):
    """The contract is the old answer, not a defensible new one."""
    values = _DATASETS[name]
    sq.execute("CREATE TABLE t (v REAL)")
    sq.executemany("INSERT INTO t VALUES (?)", [(v,) for v in values])
    rows = ", ".join(f"({v})" for v in values)

    expected = sq.execute("SELECT PERCENTILE(v, ?) FROM t", (percent,)).fetchone()[0]
    got = pg.execute(
        f"SELECT percentile(v, {percent}) FROM (VALUES {rows}) x(v)").fetchone()[0]

    assert abs(float(got) - float(expected)) < 1e-9


@pytest.mark.parametrize("name", sorted(_DATASETS))
def test_median_matches_the_sqlite_implementation(pg, sq, name):
    values = _DATASETS[name]
    sq.execute("CREATE TABLE t (v REAL)")
    sq.executemany("INSERT INTO t VALUES (?)", [(v,) for v in values])
    rows = ", ".join(f"({v})" for v in values)

    expected = sq.execute("SELECT MEDIAN(v) FROM t").fetchone()[0]
    got = pg.execute(f"SELECT median(v) FROM (VALUES {rows}) x(v)").fetchone()[0]

    assert abs(float(got) - float(expected)) < 1e-9


def test_percentile_is_on_a_0_to_100_scale(pg):
    """Every existing caller writes PERCENTILE(cores, 95), not 0.95.

    A 0..1 reading would answer the 95th-percentile question with the 1st percentile --
    a plausible CPU figure, and wrong.
    """
    rows = "(VALUES (1.0),(2.0),(3.0),(4.0),(100.0)) x(v)"

    assert pg.execute(f"SELECT percentile(v, 100) FROM {rows}").fetchone()[0] == 100.0
    assert pg.execute(f"SELECT percentile(v, 0) FROM {rows}").fetchone()[0] == 1.0


def test_percentile_differs_by_p_at_all(pg):
    """Pins the bug this nearly shipped with.

    Postgres passes a finalfunc_extra final function the aggregate's direct arguments as
    NULLs rather than values, so p never reached the interpolation and every percentile
    returned the maximum. PERCENTILE(cores, 5) and PERCENTILE(cores, 95) agreed, and both
    looked like reasonable CPU numbers.
    """
    rows = "(VALUES (1.0),(2.0),(3.0),(4.0),(100.0)) x(v)"

    low = pg.execute(f"SELECT percentile(v, 5) FROM {rows}").fetchone()[0]
    high = pg.execute(f"SELECT percentile(v, 95) FROM {rows}").fetchone()[0]

    assert low < high, "p must reach the interpolation"


def test_percentile_groups_independently(pg):
    """The shape advice.py and the CPU row actually use: one percentile per container."""
    got = pg.execute(
        "SELECT g, percentile(v, 50) FROM "
        "(VALUES ('a',1.0),('a',3.0),('b',10.0),('b',20.0)) x(g,v) "
        "GROUP BY g ORDER BY g").fetchall()

    assert got == [("a", 2.0), ("b", 15.0)]


def test_an_all_null_group_is_null_not_zero(pg):
    """A column the clock map could not place is empty on purpose.

    Reading it as zero would report a robot at the origin rather than an unknown position.
    """
    got = pg.execute(
        "SELECT percentile(v, 50) FROM (VALUES (NULL::float8),(NULL::float8)) x(v)"
    ).fetchone()[0]

    assert got is None


def test_regexp_takes_pattern_first_like_the_sqlite_registration(pg):
    """The argument order is the contract.

    SQLite's registered function is REGEXP(pattern, value); Postgres' operator reads
    value ~ pattern. Swapping them silently makes every log search match nothing.
    """
    assert pg.execute("SELECT regexp('^ab.', 'abc')").fetchone()[0] is True
    assert pg.execute("SELECT regexp('^zz', 'abc')").fetchone()[0] is False
    # And the reversed reading must NOT accidentally work.
    assert pg.execute("SELECT regexp('abc', '^ab.')").fetchone()[0] is False


def test_regexp_on_null_is_false_not_null(pg):
    """A NULL message must not make a WHERE clause three-valued and drop rows silently."""
    assert pg.execute("SELECT regexp('^a', NULL)").fetchone()[0] is False


def test_the_native_functions_are_left_alone(pg):
    """STDDEV, VARIANCE and SQRT are Postgres' own; nothing here may shadow them.

    Against a real ``double precision`` column, which is what ``index_schema`` creates and
    therefore what these are ever applied to. It matters for more than tidiness: on a
    ``numeric`` input Postgres returns ``Decimal`` where SQLite returned ``float``, so a
    caller doing arithmetic on the result in Python would get a type error. On the column
    type the index actually uses, both return ``float``.
    """
    pg.execute("CREATE TABLE natives (v double precision)")
    pg.execute("INSERT INTO natives VALUES (1.0), (2.0), (3.0)")

    stddev = pg.execute("SELECT stddev(v) FROM natives").fetchone()[0]
    variance = pg.execute("SELECT variance(v) FROM natives").fetchone()[0]

    assert isinstance(stddev, float) and abs(stddev - 1.0) < 1e-9
    assert isinstance(variance, float) and abs(variance - 1.0) < 1e-9
    assert abs(pg.execute("SELECT sqrt(v) FROM natives WHERE v = 3.0").fetchone()[0]
               - 1.7320508) < 1e-6


def test_install_is_idempotent_and_cheap_on_the_second_call(pg):
    """The usual path is one SELECT against a one-row table."""
    assert index_functions.install(pg) is False

    version = pg.execute(
        f'SELECT version FROM "{index_functions.FUNCTIONS_VERSION_TABLE}"').fetchone()[0]
    assert version == index_functions.FUNCTIONS_VERSION


def test_a_bumped_version_reinstalls(pg, monkeypatch):
    """Otherwise a changed definition would only reach a freshly created index.

    Two deployments would then disagree about what PERCENTILE means, both looking healthy.
    """
    monkeypatch.setattr(index_functions, "FUNCTIONS_VERSION",
                        index_functions.FUNCTIONS_VERSION + 1)

    assert index_functions.install(pg) is True
