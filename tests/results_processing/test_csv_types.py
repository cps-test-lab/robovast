# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Tests for CSV column-type inference (``robovast.results_processing.csv_types``)."""

import json
import math

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
    ("nan", REAL),          # double precision holds NaN and the infinities natively
    ("inf", REAL),
    ("-inf", REAL),
    ("1e999", TEXT),        # a finite literal that overflows: not an infinity anyone measured
    ("-1e999", TEXT),
    ("1e308", REAL),        # large but finite: still a number
    ("9" * 25, TEXT),       # wider than bigint's 8 bytes
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


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), "inf", "-inf", "+inf",
                                   "Infinity", "INF"])
def test_an_infinity_is_a_number(value):
    """A censored measurement is a result: ``double precision`` holds it, so the column
    stays numeric and ``NULL`` is left to mean that nothing was measured."""
    assert value_type(value) == REAL
    stored = sql_value(value, REAL)
    assert isinstance(stored, float) and math.isinf(stored)


@pytest.mark.parametrize("value", [float("nan"), "nan", "NaN", "-nan"])
def test_a_nan_is_a_number(value):
    assert value_type(value) == REAL
    stored = sql_value(value, REAL)
    assert isinstance(stored, float) and math.isnan(stored)


def test_one_censored_trial_does_not_turn_a_column_of_numbers_into_text():
    """The failure this module exists to prevent: a text column compares lexicographically,
    so one ``inf`` would make every ``ORDER BY`` over the finite trials wrong."""
    rows = [{"clearance": "1.5"}, {"clearance": "inf"}, {"clearance": "10.25"}]

    assert infer_column_types(rows, ["clearance"])["clearance"] == REAL


def test_an_integer_column_with_an_infinity_widens_to_real():
    rows = [{"steps": "3"}, {"steps": "inf"}]

    assert infer_column_types(rows, ["steps"])["steps"] == REAL


def test_a_finite_literal_that_overflows_stays_text():
    """``1e999`` is not an infinity anybody measured; storing it as one would invent one."""
    assert value_type("1e999") == TEXT


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


def test_the_campaign_record_json_is_made_castable_on_its_way_into_the_index():
    """``campaign.db`` is written with Python's ``json``, whose ``Infinity`` token Postgres
    refuses on a ``jsonb`` cast; the mirror rewrites it and leaves everything else as is."""
    from robovast.results_processing.dimension_ingest import _indexable

    assert json.loads(_indexable("objectives_json", '{"length": Infinity, "gap": NaN}'),
                      parse_constant=_refuse_constant) == {"length": "inf", "gap": "nan"}
    assert _indexable("objectives_json", '{"length": 1.5}') == '{"length": 1.5}'
    assert _indexable("status", "Infinity") == "Infinity", "only the *_json columns"
