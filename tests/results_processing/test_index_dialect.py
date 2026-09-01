# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The two SQLite casts Postgres accepts and means something else by.

Both are legal Postgres and both return a plausible wrong number rather than raising, so
these tests assert the *numbers*, not just the rewritten string. The string tests exist
only to pin the scanner's edges -- nesting, literals, case.
"""

import os
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
