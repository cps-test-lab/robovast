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

"""Column typing for the CSV -> results-index ingest: infer, declare, convert.

Every value a CSV yields is a string, so a column ingested verbatim lands in a
``TEXT`` column and every comparison over it becomes lexicographic. That failure
is silent and plausible: ``ORDER BY timestamp`` puts ``"10.022"`` before
``"9.5"``, so a trajectory comes out shuffled and its path length is wrong by a
factor, not by an error. This module decides, per column, whether the CSV's own
values say the column is numeric, and converts them so the index stores real
``INTEGER``/``REAL`` values and plain SQL means what it says.

The rule is deliberately strict — a column is numeric only if *every* non-empty
value in it is a number:

* ``"1"``, ``"-3"``                       -> ``INTEGER``
* ``"1.5"``, ``"-0.5"``, ``"1e-3"``       -> ``REAL`` (any ``.`` or exponent)
* ``"inf"``, ``"-inf"``, ``"nan"``        -> ``REAL``, stored as ``double precision``'s own
  ``Infinity``, ``-Infinity``, ``NaN``
* ``""``                                  -> ``NULL``, and no evidence either way
* anything else, including ``"1e999"``,
  ``"1,5"``, ``"007"``                    -> ``TEXT``, stored verbatim

**A non-finite value is a measurement, so it is stored as one.** "No return", "no
trajectory came back, so the path length is infinite" and "the ratio had no denominator"
are results a query has to be able to find, and ``double precision`` holds them as values
that order and compare: infinity above every number, ``NaN`` equal to itself and above
infinity. They stay distinct from the ``NULL`` that means nothing was measured, and the
column stays numeric, so one censored trial does not turn a column of numbers into text.

Two exclusions are load-bearing rather than fussy. Leading zeros mark an identifier whose
text matters (``"007"`` must not become ``7``), and a finite literal no number column can
hold is refused rather than mangled: one that overflows ``double precision`` (``"1e999"``
would become an infinity nobody measured) and an integer past ``bigint``'s 8 bytes. In
either case the whole column stays ``TEXT`` and the raw strings survive for inspection.

**A container is JSON-encoded with** ``allow_nan=False``. JSON has no token for a
non-finite number, and Python's ``json`` writes ``Infinity``, ``-Infinity`` and ``NaN``
anyway, reading them back without complaint -- so a record carrying one looks right until
SQL casts the column to ``jsonb``, and Postgres then fails the *whole query*, not the
offending row. Inside a container each non-finite float is written as the string
``"inf"``, ``"-inf"`` or ``"nan"`` (:data:`NON_FINITE_TEXT`) instead.
"""

import json
import math
import re

# A plain decimal number, optionally signed, optionally with an exponent. Anything
# outside this and :data:`_NON_FINITE_RE` — hex, thousands separators, units, surrounding
# whitespace — is text.
_NUMBER_RE = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")
# The spellings of a non-finite number that ``float()`` reads: ``inf``, ``infinity`` and
# ``nan``, signed or not, in any case -- what Python's ``csv`` writes for one, and what a
# metric script prints.
_NON_FINITE_RE = re.compile(r"^[+-]?(?:inf|infinity|nan)$", re.IGNORECASE)
# A leading zero before another digit ("007", "01.5") means the text is an
# identifier or a zero-padded field, not a quantity.
_LEADING_ZERO_RE = re.compile(r"^[+-]?0\d")
# Anything with a fractional part or an exponent is REAL even when its value is
# integral ("1e3"), because the CSV wrote it as a real.
_REAL_MARKER_RE = re.compile(r"[.eE]")

TEXT = "TEXT"
REAL = "REAL"
INTEGER = "INTEGER"
#: No evidence yet: the column has been seen but held only empty values. A verdict,
#: not SQL — :func:`column_def` declares such a column with no type at all (BLOB
#: affinity), so a later run's numbers are stored as numbers instead of being
#: coerced to strings by a premature ``TEXT`` declaration.
UNKNOWN = "UNKNOWN"

# Widest wins: one text value makes the column text, one real value makes an
# otherwise-integer column real.
_RANK = {UNKNOWN: 0, INTEGER: 1, REAL: 2, TEXT: 3}


# ``bigint`` holds integers in 8 bytes; a wider one would fail the insert, so it stays
# text (a 20-digit id is not a quantity anyone averages).
_INT64_MIN, _INT64_MAX = -2**63, 2**63 - 1


def value_type(value) -> str:
    """The narrowest type that can hold *value*: ``UNKNOWN``/``INTEGER``/``REAL``/``TEXT``."""
    if value is None or value == "":
        return UNKNOWN
    if not isinstance(value, str):
        # Already-typed values (scenario params from campaign.db, sysinfo) — trust
        # Python's type. bool is an int subclass and stores as 0/1 (see :func:`coerce`).
        if isinstance(value, int):
            return INTEGER if _INT64_MIN <= value <= _INT64_MAX else TEXT
        if isinstance(value, float):
            return REAL
        return TEXT
    if _NON_FINITE_RE.match(value):
        return REAL
    if not _NUMBER_RE.match(value) or _LEADING_ZERO_RE.match(value):
        return TEXT
    if _REAL_MARKER_RE.search(value):
        # An exponent can overflow to infinity ("1e999"): a finite literal stored as an
        # infinity nobody measured.
        return REAL if math.isfinite(float(value)) else TEXT
    return INTEGER if _INT64_MIN <= int(value) <= _INT64_MAX else TEXT


def widen(current: str, value) -> str:
    """The type *current* must widen to in order to also hold *value*."""
    return widest(current, value_type(value))


def widest(*types: str) -> str:
    """The widest of several column types (``TEXT`` beats ``REAL`` beats ``INTEGER``)."""
    return max(types, key=_RANK.__getitem__)


def infer_column_types(rows, columns) -> dict:
    """Infer a column type per column from *rows* (an iterable of dict rows).

    Returns ``{column: INTEGER|REAL|TEXT|UNKNOWN}``. A column is numeric only when
    every non-empty value in *rows* is numeric, so one stray label demotes it to
    ``TEXT`` and the raw strings stay readable.
    """
    types = {c: UNKNOWN for c in columns}
    for row in rows:
        for col in columns:
            if types[col] == TEXT:
                continue
            types[col] = widen(types[col], row.get(col))
    return types


def coerce(value, col_type: str):
    """Convert a CSV string to *col_type*, or return it unchanged if it cannot be.

    Conversion is explicit so the stored value is right even when a later run widens
    the column. A value that does not fit is returned verbatim — the caller reports the
    mixed column rather than dropping the row.
    """
    if value is None or value == "":
        return None
    # A genuine JSON boolean, which only a .jsonl source produces -- `behaviors.jsonl`
    # carries `is_active`. Stored as 1/0, which is what sqlite3 did with a Python bool
    # whatever the column's declared type, so the values match what data.db held.
    #
    # Not cosmetic: psycopg adapts a bool to Postgres' own `t`/`f` literal, and COPY into
    # the bigint that `infer_column_types` declares for it (bool IS an int in Python, so
    # it is judged INTEGER) fails with `invalid input syntax for type bigint: "f"`. That
    # took down a whole campaign's postprocessing after the runs had already been paid for.
    if isinstance(value, bool):
        return int(value)
    if col_type not in (INTEGER, REAL) or not isinstance(value, str):
        return value
    try:
        return int(value) if col_type == INTEGER else float(value)
    except ValueError:
        return value


#: The spelling each non-finite float takes inside a JSON-encoded container, where JSON has
#: no token for one, in the order ``+inf``, ``-inf``, ``nan``. These are what
#: :func:`float` parses and what Postgres accepts for ``double precision``, so
#: ``(col::jsonb ->> 0)::double precision`` reads one back as the number.
NON_FINITE_TEXT = ("inf", "-inf", "nan")
_POSITIVE_INFINITY, _NEGATIVE_INFINITY, _NOT_A_NUMBER = NON_FINITE_TEXT


def as_stored(value):
    """*value* with every non-finite float replaced by its :data:`NON_FINITE_TEXT` spelling,
    for JSON encoding. Recurses into lists, tuples and dicts."""
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return _NOT_A_NUMBER
        return _POSITIVE_INFINITY if value > 0 else _NEGATIVE_INFINITY
    if isinstance(value, dict):
        return {as_stored(key): as_stored(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [as_stored(item) for item in value]
    return value


def json_text(value, **dumps_kwargs) -> str:
    """*value* JSON-encoded for a column SQL casts to ``jsonb``.

    ``allow_nan=False`` is the guard behind :func:`as_stored`: a float that reaches the
    encoder non-finite raises here, inside the ingest and naming the value, instead of
    being written as a token that breaks every later query over the column.
    """
    return json.dumps(as_stored(value), allow_nan=False, **dumps_kwargs)


def sql_value(value, col_type: str):
    """A CSV or param value ready to insert: containers JSON-encoded, numbers typed.

    A non-finite number on its own goes to the driver as the float it is, which
    ``double precision`` stores natively; inside a container it is written as its
    :data:`NON_FINITE_TEXT` spelling (:func:`json_text`).
    """
    if isinstance(value, (list, dict)):
        return json_text(value)
    return coerce(value, col_type)


def column_def(name: str, col_type: str) -> str:
    """A column definition for DDL; :data:`UNKNOWN` declares no type at all."""
    return f'"{name}"' if col_type == UNKNOWN else f'"{name}" {col_type}'


def cast_expr(name: str, col_type: str) -> str:
    """A SELECT expression re-typing an already-stored column to *col_type*.

    Used when a table is rebuilt because later runs widened a column: the values in it
    were converted under the old, narrower verdict and have to be brought over as the
    new type so storage matches the declaration. ``CAST`` reformats a number on its
    way to ``TEXT`` (``1e-3`` was already stored as ``0.001``), which is the price of
    a homogeneous column; the value itself is preserved.
    """
    return f'"{name}"' if col_type == UNKNOWN else f'CAST("{name}" AS {col_type})'
