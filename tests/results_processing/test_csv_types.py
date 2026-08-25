# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Tests for CSV column-type inference (``robovast.results_processing.csv_types``)."""

import pytest

from robovast.results_processing.csv_types import (INTEGER, REAL, TEXT, UNKNOWN, cast_expr, coerce,
                                                   column_def, infer_column_types, sql_value,
                                                   value_type, widest)


@pytest.mark.parametrize("value,expected", [
    ("1", INTEGER),
    ("-3", INTEGER),
    ("+3", INTEGER),
    ("0", INTEGER),
    ("1.5", REAL),
    ("-0.5", REAL),
    (".5", REAL),
    ("2.", REAL),
    ("1e-3", REAL),
    ("1E5", REAL),          # scientific notation is a real even at an integral value
    ("", UNKNOWN),
    (None, UNKNOWN),
    ("passed", TEXT),
    ("1,5", TEXT),          # decimal comma is not a number here
    ("0x10", TEXT),
    (" 1", TEXT),           # surrounding whitespace: not a clean number
    ("1 m", TEXT),
    ("007", TEXT),          # zero-padded identifier must keep its text
    ("01.5", TEXT),
    ("nan", TEXT),          # no SQLite representation — sqlite3 would store NULL
    ("inf", TEXT),
    ("-inf", TEXT),
    ("1e999", TEXT),        # overflows to infinity: the same loss as literal "inf"
    ("-1e999", TEXT),
    ("1e308", REAL),        # large but finite: still a number
    ("9" * 25, TEXT),       # wider than SQLite's 8-byte integer
])
def test_value_type(value, expected):
    assert value_type(value) == expected


def test_value_type_of_already_typed_values():
    """Params come from campaign.db as Python values, not CSV text."""
    assert value_type(3) == INTEGER
    assert value_type(True) == INTEGER      # bool is an int subclass; stores as 0/1
    assert value_type(1.5) == REAL
    assert value_type("[1, 2]") == TEXT     # JSON-encoded non-scalar param


def test_widest_prefers_the_wider_type():
    assert widest(UNKNOWN, INTEGER) == INTEGER
    assert widest(INTEGER, REAL) == REAL
    assert widest(REAL, TEXT) == TEXT
    assert widest(TEXT, INTEGER) == TEXT


def test_infer_column_types_over_rows():
    rows = [
        {"timestamp": "9.5", "seq": "1", "frame": "odom", "gap": ""},
        {"timestamp": "10.022", "seq": "2", "frame": "map", "gap": ""},
    ]
    types = infer_column_types(rows, ["timestamp", "seq", "frame", "gap"])
    assert types == {"timestamp": REAL, "seq": INTEGER, "frame": TEXT, "gap": UNKNOWN}


def test_one_integral_value_does_not_narrow_a_real_column():
    rows = [{"x": "1"}, {"x": "1.5"}, {"x": "2"}]
    assert infer_column_types(rows, ["x"])["x"] == REAL


def test_single_non_numeric_value_makes_the_whole_column_text():
    """Strictness is the point: the raw strings stay readable instead of being lost."""
    rows = [{"x": "1.0"}, {"x": "1.5"}, {"x": "n/a"}]
    assert infer_column_types(rows, ["x"])["x"] == TEXT


def test_empty_values_become_null_not_zero():
    assert coerce("", REAL) is None
    assert coerce(None, INTEGER) is None


def test_coerce_converts_by_column_type():
    assert coerce("10.022", REAL) == pytest.approx(10.022)
    assert coerce("2", INTEGER) == 2
    assert coerce("2", TEXT) == "2"
    assert coerce("2", UNKNOWN) == "2"


def test_coerce_keeps_a_value_it_cannot_convert():
    """A later run disagreeing with the column's type must not drop the value."""
    assert coerce("n/a", REAL) == "n/a"


def test_column_def_declares_no_type_for_an_unknown_column():
    """UNKNOWN is a verdict, never SQL: the column is declared without a type."""
    assert column_def("x", REAL) == '"x" REAL'
    assert column_def("x", UNKNOWN) == '"x"'


def test_cast_expr_retypes_a_stored_column_but_leaves_unknown_alone():
    assert cast_expr("x", TEXT) == 'CAST("x" AS TEXT)'
    assert cast_expr("x", UNKNOWN) == '"x"'


def test_sql_value_json_encodes_containers():
    assert sql_value([{"x": 1.0}], TEXT) == '[{"x": 1.0}]'
    assert sql_value("1.5", REAL) == pytest.approx(1.5)
