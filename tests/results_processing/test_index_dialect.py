# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""SQLite spellings Postgres accepts: two mean something else here, one is quadratic.

The casts are legal Postgres and return a plausible wrong number rather than raising, and
a translated ``PERCENTILE`` must return what the two-argument aggregate returns -- so these
tests assert the *numbers*, not just the rewritten string. The string tests exist only to
pin the scanner's edges: nesting, literals, case, and the calls that must keep the
two-argument aggregate.
"""

import os
import random
import sqlite3

import pytest

from robovast.results_processing.index_dialect import translate

DSN = os.environ.get("ROBOVAST_TEST_PG_DSN")


# -- the scanner ------------------------------------------------------------

def test_the_nested_panel_cast_is_translated_at_both_levels():
    """The real one: dataProvider.ts groups by CAST(CAST(...) * hz AS INTEGER)."""
    got = translate('SELECT CAST(CAST("timestamp" AS REAL) * 2 AS INTEGER) FROM poses')

    assert got == ('SELECT trunc(CAST("timestamp" AS double precision) * 2)::bigint '
                   'FROM poses')


def test_a_cast_inside_a_string_literal_is_left_alone():
    """Rewriting inside a literal would change the data, not the query."""
    sql = "SELECT 'CAST(x AS REAL)' AS literal FROM t"

    assert translate(sql) == sql


def test_a_cast_inside_a_quoted_identifier_is_left_alone():
    """Column names come from CSV headers and can contain anything."""
    sql = 'SELECT "a CAST(x AS INTEGER) column" FROM t'

    assert translate(sql) == sql


def test_a_type_that_means_the_same_in_both_is_untouched():
    sql = "SELECT CAST(x AS TEXT) FROM t"

    assert translate(sql) == sql


def test_lowercase_is_translated_too():
    assert "double precision" in translate("select cast(v as real) from t")


def test_an_unbalanced_cast_is_left_for_the_database_to_reject():
    """A malformed query is not this module's to guess at."""
    sql = "SELECT CAST(x FROM t"

    assert translate(sql) == sql


def test_translating_twice_changes_nothing_more():
    once = translate('SELECT CAST(CAST(a AS REAL) AS INTEGER) FROM t')

    assert translate(once) == once


# -- the percentile call ----------------------------------------------------

def test_the_panel_percentile_becomes_the_native_ordered_set_aggregate():
    """The real one: the resource-usage panel asks for several of these at once."""
    got = translate("SELECT PERCENTILE(cores, 95) FROM resource_usage")

    assert got == ("SELECT percentile_cont(least(1.0, greatest(0.0, (95) / 100.0))) "
                   "WITHIN GROUP (ORDER BY (cores)) FROM resource_usage")


def test_a_cast_inside_a_percentile_argument_is_translated_too():
    assert "double precision" in translate("SELECT PERCENTILE(CAST(e AS REAL), 50) FROM t")


def test_a_filtered_percentile_keeps_its_filter_after_the_within_group():
    """FILTER follows the whole call, so replacing the call alone puts it in the right place."""
    got = translate("SELECT PERCENTILE(v, 50) FILTER (WHERE v > 2) FROM t")

    assert got.endswith("WITHIN GROUP (ORDER BY (v)) FILTER (WHERE v > 2) FROM t")


@pytest.mark.parametrize("sql", [
    "SELECT PERCENTILE(v, 50) OVER () FROM t",
    "SELECT PERCENTILE(v, 50) FILTER (WHERE b) OVER (w) FROM t",
    "SELECT PERCENTILE(DISTINCT v, 50) FROM t",
    "SELECT PERCENTILE(a, b, c) FROM t",
    "SELECT PERCENTILE(v, 50 FROM t",
])
def test_a_call_the_ordered_set_aggregate_cannot_express_is_left_alone(sql):
    """Postgres refuses OVER and DISTINCT on an ordered-set aggregate, so these keep the
    two-argument aggregate rather than becoming an error."""
    assert translate(sql) == sql


@pytest.mark.parametrize("sql", [
    "SELECT _rv_percentile_final(a, 50) FROM t",
    "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY v) FROM t",
    "SELECT x.percentile(v, 50) FROM t",
    "SELECT 'PERCENTILE(v, 50)' AS literal FROM t",
    'SELECT "PERCENTILE(v, 50) column" FROM t',
])
def test_something_that_is_not_the_two_argument_call_is_left_alone(sql):
    assert translate(sql) == sql


def test_translating_a_percentile_twice_changes_nothing_more():
    once = translate("SELECT PERCENTILE(CAST(v AS REAL), 95) FROM t")

    assert translate(once) == once


# -- the numbers, which are the point ---------------------------------------

@pytest.mark.skipif(not DSN, reason="ROBOVAST_TEST_PG_DSN is not set")
@pytest.mark.parametrize("value", [8.4, 8.5, 8.6, 9.5, 17.5, -8.5, -8.6])
def test_integer_cast_truncates_like_sqlite_rather_than_rounding(value):
    """SQLite truncates toward zero; Postgres rounds half-to-even.

    Untranslated, every downsampled plot's buckets shift by half a bucket -- and the
    chart still looks fine.
    """
    psycopg = pytest.importorskip("psycopg")
    expected = sqlite3.connect(":memory:").execute(
        "SELECT CAST(? AS INTEGER)", (value,)).fetchone()[0]

    with psycopg.connect(DSN, autocommit=True) as conn:
        got = conn.execute(translate(f"SELECT CAST({value} AS INTEGER)")).fetchone()[0]

    assert got == expected


@pytest.mark.skipif(not DSN, reason="ROBOVAST_TEST_PG_DSN is not set")
def test_real_cast_keeps_double_precision_rather_than_narrowing_to_float4():
    """The measured trap: SQLite's REAL is 8 bytes, Postgres' is 4.

    An epoch stamp loses ~30 seconds through Postgres' real, and a 60-second wall span
    -- which is what run_validity_view computes -- reads as 128 seconds. Every stall
    ratio derived from it would then be wrong by a factor, silently.
    """
    psycopg = pytest.importorskip("psycopg")
    early, late = 1787518471.334247, 1787518531.334247  # exactly 60 s apart

    with psycopg.connect(DSN, autocommit=True) as conn:
        untranslated = conn.execute(
            f"SELECT CAST({late} AS REAL) - CAST({early} AS REAL)").fetchone()[0]
        translated = conn.execute(translate(
            f"SELECT CAST({late} AS REAL) - CAST({early} AS REAL)")).fetchone()[0]

    assert abs(translated - 60.0) < 1e-6
    assert abs(untranslated - 60.0) > 1.0, "the trap is real; if this fails, so has the premise"


@pytest.mark.skipif(not DSN, reason="ROBOVAST_TEST_PG_DSN is not set")
@pytest.mark.parametrize("values", [
    [1.0, 2.0, 3.0, 4.0, 100.0],
    [42.0],
    [5.0, 5.0, 5.0, 5.0],
    [-10.0, -1.0, 0.0, 1.0, 10.0],
    [1.0, None, 3.0, None, 5.0],
    [None, None],
    [],
    [round(random.Random(7).uniform(0, 500), 3) for _ in range(97)],
])
@pytest.mark.parametrize("percent", [0, 5, 25, 50, 75, 95, 100, 150, -5])
def test_the_translated_percentile_returns_what_the_aggregate_returned(values, percent):
    """The contract is the number the two-argument aggregate returns, not a defensible one.

    Same standard as test_index_functions: the panels, the campaign advice and every query
    an agent writes read these numbers, so a subtly different one is believed.
    """
    psycopg = pytest.importorskip("psycopg")
    from robovast.results_processing import data_query  # pylint: disable=import-outside-toplevel

    sqlite = sqlite3.connect(":memory:")
    data_query._register_aggregates(sqlite)  # pylint: disable=protected-access
    sqlite.execute("CREATE TABLE t (v REAL)")
    sqlite.executemany("INSERT INTO t VALUES (?)", [(v,) for v in values])
    expected = sqlite.execute(f"SELECT PERCENTILE(v, {percent}) FROM t").fetchone()[0]

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("CREATE TEMP TABLE t (v double precision)")
        with conn.cursor() as cursor:
            cursor.executemany("INSERT INTO t VALUES (%s)", [(v,) for v in values])
        got = conn.execute(translate(f"SELECT PERCENTILE(v, {percent}) FROM t")).fetchone()[0]

    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected)
