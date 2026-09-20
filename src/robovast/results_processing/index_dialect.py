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

"""SQLite spellings Postgres accepts: two mean something else here, one is quadratic.

Queries using them are already written: in the panels, in the campaign advice, in
notebooks, and in whatever an agent wrote last week against a campaign.

**``CAST(x AS REAL)``.** SQLite's ``REAL`` is an 8-byte double. Postgres' ``real`` is a
**4-byte float**, so casting an epoch timestamp through it loses about half a minute:
``1787518471.334247`` comes back as ``1787518500.0``. Measured on the view that computes a
trial's wall span, a **60-second window reads as 128 seconds** -- so every stall ratio
derived from it is wrong by a factor, and nothing raises. Translated to ``double
precision``, which is what the SQL always meant.

**``CAST(x AS INTEGER)``.** SQLite truncates toward zero; Postgres rounds half-to-even.
``8.6`` becomes ``8`` in one and ``9`` in the other. That sits in the dominant panel query,
which groups by ``CAST(CAST("timestamp" AS REAL) * <hz> AS INTEGER)`` to downsample -- so
every plot's bucket boundaries shift by half a bucket and the chart still looks fine.
Translated to ``trunc(...)::bigint``.

The two casts nest, and that is not hypothetical: the panel query above is exactly a
``CAST(CAST(...) ...)``. So this walks the string with a scanner rather than matching a
regex, tracking string literals and quoted identifiers so a ``CAST`` inside one is left
alone.

**``PERCENTILE(col, p)``.** Postgres computes the same value natively, under a different
*syntax* -- ``percentile_cont(fraction) WITHIN GROUP (ORDER BY col)`` -- and the
two-argument spelling every caller writes is defined as an aggregate for the connections
that do not pass through here (:mod:`~robovast.results_processing.index_functions`). That
definition cannot be made fast, and the reason is structural: a two-argument aggregate
needs a three-argument transition function, no built-in has that shape, and a wrapper in
SQL or PL/pgSQL flattens the state array on every row. The values are copied once per row,
so the call costs **quadratic** time in the rows of a group, and a panel asking for
several percentiles of a campaign's ticks reaches the statement timeout rather than an
answer. Translated, it is the same value by the same interpolation -- ``p`` stays 0..100
and stays clamped -- computed by the server.

Two calls keep the aggregate, because ``percentile_cont`` is an ordered-set aggregate and
cannot express them: one carrying ``OVER``, which Postgres refuses on an ordered-set
aggregate, and one whose value argument is ``DISTINCT``. ``p`` is a direct argument there,
evaluated once per group, so one naming a column of the rows being aggregated raises.

**What this deliberately is not.** It is not a dialect layer and must not grow into one.
An entry earns its place by being a *silent* difference in SQL people have already written,
or a spelling the server can only evaluate quadratically; anything Postgres rejects
outright is better left to fail, because the author sees the error and fixes the query.
Every entry here is held by differential test to the number the SQLite implementation of
the same spelling returns, and that is the only way another should be added.
"""

import logging
import re

logger = logging.getLogger(__name__)

#: Recognised inside a ``CAST(... AS <type>)``, case-insensitively.
_REAL_TYPES = {"real"}
_INTEGER_TYPES = {"integer", "int"}

_CAST_START = re.compile(r"\bCAST\s*\(", re.IGNORECASE)

#: The two-argument ``PERCENTILE`` call, and only that: not the tail of a longer name
#: (``_rv_percentile_final``), not ``percentile_cont``, and not a qualified call.
_PERCENTILE_START = re.compile(r"(?<![A-Za-z0-9_.\"])percentile\s*\(", re.IGNORECASE)

#: ``p`` is 0..100 and clamped there, which is what the two-argument aggregate does with
#: an out-of-range percentile; ``percentile_cont`` raises on a fraction outside 0..1.
_PERCENTILE_FORM = ("percentile_cont(least(1.0, greatest(0.0, ({p}) / 100.0))) "
                    "WITHIN GROUP (ORDER BY ({value}))")


def _skip_quoted(sql: str, i: int) -> int:
    """Index just past the string literal or quoted identifier starting at *i*."""
    quote = sql[i]
    i += 1
    while i < len(sql):
        if sql[i] == quote:
            # A doubled quote is an escaped one, not the end.
            if i + 1 < len(sql) and sql[i + 1] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    return i


def _match_cast(sql: str, open_paren: int):
    """``(inner, type, end)`` for the CAST whose ``(`` is at *open_paren*, else ``None``.

    *end* is the index just past the closing paren. Returns ``None`` when the parentheses
    do not balance -- a malformed query is the database's to reject, not this module's to
    guess at.
    """
    depth = 0
    i = open_paren
    while i < len(sql):
        char = sql[i]
        if char in "'\"":
            i = _skip_quoted(sql, i)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                body = sql[open_paren + 1:i]
                # Split on the LAST top-level ``AS``, so a nested cast's own AS is not
                # mistaken for this one's.
                split = _last_top_level_as(body)
                if split is None:
                    return None
                return body[:split].strip(), body[split + 2:].strip(), i + 1
        i += 1
    return None


def _keyword_at(sql: str, i: int, word: str) -> bool:
    """Is *sql* at *i* the keyword *word*, not part of a longer identifier?

    ``last_seen`` and ``as_of`` both contain ``AS``; neither is the keyword.
    """
    end = i + len(word)
    if sql[i:end].upper() != word:
        return False
    before_ok = i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == "_")
    after_ok = end >= len(sql) or not (sql[end].isalnum() or sql[end] == "_")
    return before_ok and after_ok


def _last_top_level_as(body: str):
    """Index of the last ``AS`` at paren depth 0 in *body*, or ``None``."""
    depth = 0
    found = None
    i = 0
    while i < len(body):
        char = body[i]
        if char in "'\"":
            i = _skip_quoted(body, i)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and _keyword_at(body, i, "AS"):
            found = i
            i += 2
            continue
        i += 1
    return found


def _match_call(sql: str, open_paren: int):
    """``(body, end)`` for the call whose ``(`` is at *open_paren*, else ``None``.

    *end* is the index just past the closing paren. ``None`` when the parentheses do not
    balance -- a malformed query is the database's to reject, not this module's to guess at.
    """
    depth = 0
    i = open_paren
    while i < len(sql):
        char = sql[i]
        if char in "'\"":
            i = _skip_quoted(sql, i)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return sql[open_paren + 1:i], i + 1
        i += 1
    return None


def _split_arguments(body: str) -> list:
    """*body* split on its top-level commas, so a nested call keeps its own."""
    parts = []
    depth = 0
    start = 0
    i = 0
    while i < len(body):
        char = body[i]
        if char in "'\"":
            i = _skip_quoted(body, i)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(body[start:i])
            start = i + 1
        i += 1
    parts.append(body[start:])
    return parts


def _skip_space(sql: str, i: int) -> int:
    """Index of the first non-space character at or after *i*."""
    while i < len(sql) and sql[i].isspace():
        i += 1
    return i


def _is_windowed(sql: str, end: int) -> bool:
    """Does the call ending at *end* carry ``OVER``, past an optional ``FILTER``?

    Postgres refuses ``OVER`` on an ordered-set aggregate, so such a call is left to the
    two-argument aggregate rather than turned into an error.
    """
    i = _skip_space(sql, end)
    if _keyword_at(sql, i, "FILTER"):
        j = _skip_space(sql, i + len("FILTER"))
        if j >= len(sql) or sql[j] != "(":
            return False
        matched = _match_call(sql, j)
        if matched is None:
            return False
        i = _skip_space(sql, matched[1])
    return _keyword_at(sql, i, "OVER")


def _percentile(sql: str, open_paren: int):
    """The ``percentile_cont`` form of the ``PERCENTILE`` call at *open_paren*.

    ``(rewritten, end)``, or ``None`` for a call this cannot express: anything but two
    arguments, a ``DISTINCT`` value, or a windowed call.
    """
    matched = _match_call(sql, open_paren)
    if matched is None:
        return None
    body, end = matched
    arguments = _split_arguments(body)
    if len(arguments) != 2 or _is_windowed(sql, end):
        return None
    value, percent = (argument.strip() for argument in arguments)
    if _keyword_at(value, 0, "DISTINCT") or _keyword_at(value, 0, "ALL"):
        return None
    return _PERCENTILE_FORM.format(p=translate(percent), value=translate(value)), end


def translate(sql: str) -> str:
    """Rewrite the spellings Postgres reads differently, or evaluates quadratically.

    Idempotent in practice: the replacements produce syntax this function does not match
    again (``double precision`` is not in :data:`_REAL_TYPES`, ``trunc(...)::bigint`` is
    not a ``CAST``, and ``percentile_cont`` is not ``PERCENTILE``).
    """
    out = []
    i = 0
    changed = 0
    while i < len(sql):
        char = sql[i]
        if char in "'\"":
            end = _skip_quoted(sql, i)
            out.append(sql[i:end])
            i = end
            continue

        match = _CAST_START.match(sql, i)
        if match:
            open_paren = match.end() - 1
            parsed = _match_cast(sql, open_paren)
            if parsed is None:
                out.append(sql[i:match.end()])
                i = match.end()
                continue
            inner, cast_type, end = parsed
            # Recurse: the inner expression may itself hold a CAST -- the panel query is
            # exactly CAST(CAST("timestamp" AS REAL) * 2 AS INTEGER).
            inner = translate(inner)
            normalized = cast_type.strip().lower()
            if normalized in _REAL_TYPES:
                out.append(f"CAST({inner} AS double precision)")
                changed += 1
            elif normalized in _INTEGER_TYPES:
                out.append(f"trunc({inner})::bigint")
                changed += 1
            else:
                out.append(f"CAST({inner} AS {cast_type})")
            i = end
            continue

        match = _PERCENTILE_START.match(sql, i)
        if match:
            # The arguments are translated in turn, so a CAST inside one is rewritten
            # too: PERCENTILE(CAST(error AS REAL), 50) is a query people write.
            parsed = _percentile(sql, match.end() - 1)
            if parsed is None:
                out.append(sql[i:match.end()])
                i = match.end()
                continue
            rewritten, end = parsed
            out.append(rewritten)
            changed += 1
            i = end
            continue

        out.append(char)
        i += 1

    result = "".join(out)
    if changed:
        logger.debug("index: translated %d SQLite spelling(s)", changed)
    return result
