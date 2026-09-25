# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A query is read before it runs: one SELECT, what it names, which runs it narrows to."""

import duckdb
import pytest

from robovast_data.statement import QueryError, parse


@pytest.mark.parametrize("sql", [
    "COPY poses TO 'out.csv'",
    "ATTACH 'other.db'",
    "SET enable_external_access = true",
    "CREATE TABLE x AS SELECT 1",
    "INSTALL httpfs",
])
def test_anything_but_a_select_is_refused(sql):
    with pytest.raises(QueryError, match="only a SELECT"):
        parse(sql)


def test_one_statement_at_a_time():
    with pytest.raises(QueryError, match="one statement at a time"):
        parse("SELECT 1; SELECT 2")


def test_a_real_cast_is_a_double():
    """DuckDB's REAL is 4 bytes: an epoch timestamp through it moves by half a minute."""
    value = duckdb.sql(parse("SELECT CAST(1787518471.334247 AS REAL) AS v").sql).fetchone()[0]
    assert value == 1787518471.334247


def test_an_integer_cast_truncates_as_sqlite_did():
    """DuckDB rounds; every panel's bucket boundary would move by half a bucket."""
    row = duckdb.sql(parse("SELECT CAST(8.6 AS INTEGER), CAST(-8.6 AS INT), "
                           "CAST(CAST(8.6 AS REAL) * 10 AS INTEGER)").sql).fetchone()
    assert row == (8, -8, 86)


def test_a_cast_inside_a_string_is_left_alone():
    assert "'CAST(x AS REAL)'" in parse("SELECT 'CAST(x AS REAL)' AS s").sql


def test_the_relations_a_query_names_exclude_its_ctes():
    statement = parse("WITH p AS (SELECT * FROM poses) SELECT * FROM p "
                      "JOIN runs USING (run_id) JOIN campaign.unit u ON true")
    assert statement.relations == {"poses", "runs", "campaign.unit"}


def test_main_is_the_default_schema():
    assert parse("SELECT * FROM main.poses").relations == {"poses"}


def test_equalities_on_the_run_key_narrow_a_table():
    narrowing = parse("SELECT * FROM poses WHERE config_name = 'a' AND run_id IN (1, 2) "
                      "AND frame = 'base_link'").narrowing["poses"]
    assert narrowing.config_names == {"a"} and narrowing.run_ids == {1, 2}
    assert narrowing.admits("a", 2) and not narrowing.admits("b", 2)


def test_a_qualified_predicate_narrows_only_its_own_table():
    narrowing = parse("SELECT * FROM run_log l LEFT JOIN runs r ON l.run_id = r.run_id "
                      "WHERE l.config_name = 'a' AND r.run_id = 3").narrowing
    assert narrowing["run_log"].config_names == {"a"} and narrowing["run_log"].run_ids is None
    assert narrowing["runs"].run_ids == {3}


@pytest.mark.parametrize("sql", [
    "SELECT * FROM poses WHERE config_name = 'a' OR run_id = 1",
    "SELECT * FROM poses WHERE run_id = ?",
    "SELECT * FROM poses WHERE run_id + 0 = 1",
    "SELECT * FROM poses JOIN runs USING (run_id) WHERE run_id = 1",
    "WITH p AS (SELECT * FROM poses) SELECT * FROM p WHERE run_id = 1",
])
def test_anything_less_plain_narrows_nothing(sql):
    assert "poses" not in parse(sql).narrowing
