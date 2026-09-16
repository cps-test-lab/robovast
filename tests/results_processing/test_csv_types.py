# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Tests for CSV column-type inference (``robovast.results_processing.csv_types``)."""

import json

import pytest

from robovast.results_processing.csv_types import (INTEGER, REAL, TEXT, UNKNOWN, as_stored,
                                                   cast_expr, coerce, column_def,
                                                   infer_column_types, json_text, sql_value,
                                                   value_type, widest)


def _refuse_constant(token):
    raise AssertionError(f"{token} is not JSON and no strict parser accepts it")


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


# -- booleans, which only a .jsonl source produces ---------------------------

def test_a_json_boolean_is_stored_as_one_or_zero():
    """`behaviors.jsonl` carries a real JSON `is_active`, and it must store as 1/0.

    `value_type` already judges a bool INTEGER -- deliberately, since bool is an int
    subclass and sqlite3 stored a Python bool as 1/0 whatever the column was declared.
    The conversion has to agree, or the declared type and the written value disagree.
    """
    assert sql_value(True, INTEGER) == 1
    assert sql_value(False, INTEGER) == 0


def test_a_boolean_is_not_left_for_the_driver_to_adapt():
    """The regression this pins, which cost a campaign its postprocessing.

    Left unconverted, psycopg adapts a Python bool to Postgres' own `t`/`f` literal, and
    COPY into the bigint that inference declared for the column fails outright:
    `invalid input syntax for type bigint: "f"`. The runs had already been paid for by
    the time the ingest ran, so this surfaced at the most expensive possible moment.
    """
    for declared in (INTEGER, REAL, TEXT, UNKNOWN):
        assert sql_value(False, declared) == 0, (
            f"a bool must not reach the driver as a bool, whatever the column says "
            f"it holds (declared {declared})")
        assert not isinstance(sql_value(False, declared), bool)


def test_the_declaration_and_the_value_agree_for_a_boolean_column():
    """Inference and conversion must reach the same answer, which is the actual bug."""
    rows = [{"is_active": True}, {"is_active": False}]

    declared = infer_column_types(rows, ["is_active"])["is_active"]

    assert declared == INTEGER
    assert all(isinstance(sql_value(r["is_active"], declared), int) for r in rows)


# -- non-finite floats, which JSON has no token for --------------------------


@pytest.mark.parametrize("value,stored", [
    (float("inf"), "inf"),
    (float("-inf"), "-inf"),
    (float("nan"), "nan"),
])
def test_a_non_finite_float_is_stored_as_its_own_spelling(value, stored):
    """A censored measurement is a result, so it keeps a value of its own.

    ``float()`` reads all three back and Postgres takes them as ``double precision``
    input, so the number survives; ``NULL`` is left to mean that nothing was measured,
    which is a different answer and the only one the record could otherwise not tell it
    apart from.
    """
    for declared in (REAL, TEXT, UNKNOWN):
        assert sql_value(value, declared) == stored, f"declared {declared}"
        assert sql_value(value, declared) is not None


def test_a_non_finite_float_is_typed_like_the_string_that_spells_it():
    """Already-typed values reach the ingest as floats -- from a ``.jsonl``, or from a
    param the campaign record holds -- and are judged by the same rule as CSV text."""
    assert value_type(float("inf")) == TEXT
    assert value_type(float("-inf")) == TEXT
    assert value_type(float("nan")) == TEXT
    assert value_type(1.5) == REAL


def test_the_declaration_and_the_value_agree_for_a_censored_column():
    """The column says text and holds text: the invariant the boolean case also pins."""
    rows = [{"clearance": 1.5}, {"clearance": float("inf")}]

    declared = infer_column_types(rows, ["clearance"])["clearance"]

    assert declared == TEXT
    assert sql_value(rows[1]["clearance"], declared) == "inf"
    assert isinstance(sql_value(rows[1]["clearance"], declared), str)


def test_a_declared_numeric_column_is_not_given_a_non_finite_float_either():
    """A bag declares its types, so no value was read to widen the column first.

    ``"1e999"`` is the same value by another spelling: it overflows to an infinity on
    conversion, which is why both have to be caught after the coercion and not before.
    """
    assert sql_value("inf", REAL) == "inf"
    assert sql_value("-inf", REAL) == "-inf"
    assert sql_value("nan", REAL) == "nan"
    assert sql_value("1e999", REAL) == "inf"


def test_a_non_finite_float_in_a_container_never_becomes_a_json_token():
    """One of them anywhere in the value makes every query casting the column fail --
    the whole query, not the row -- so the substitution has to reach into the value."""
    encoded = sql_value({"path_length": float("inf"),
                         "gaps": [1.0, float("nan"), float("-inf")]}, TEXT)

    assert json.loads(encoded, parse_constant=_refuse_constant) == {
        "path_length": "inf", "gaps": [1.0, "nan", "-inf"]}


def test_json_text_writes_what_a_strict_parser_accepts():
    assert json.loads(json_text({"a": [float("nan")]}),
                      parse_constant=_refuse_constant) == {"a": ["nan"]}
    assert json_text([1.0, 2.0]) == "[1.0, 2.0]"


def test_as_stored_leaves_everything_finite_alone():
    value = {"speed": 0.5, "name": "goal-1", "steps": [1, 2], "ok": True, "gap": None}

    assert as_stored(value) == value


def test_a_finite_value_is_unaffected():
    """The ordinary path, which the substitution must not touch."""
    assert sql_value("1.5", REAL) == pytest.approx(1.5)
    assert sql_value(1.5, REAL) == pytest.approx(1.5)
    assert sql_value("1e308", REAL) == pytest.approx(1e308)
    assert sql_value("2", INTEGER) == 2
    assert sql_value("passed", TEXT) == "passed"
    assert sql_value("", REAL) is None
    assert sql_value([1.0, {"x": 2.0}], TEXT) == '[1.0, {"x": 2.0}]'
