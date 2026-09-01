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

"""Two SQLite spellings Postgres accepts and means something else by.

Both are ``CAST``, both are legal Postgres, and both return a plausible wrong number
instead of an error -- which is why they are translated rather than documented as
"don't do that". Queries using them are already written: in the panels, in the campaign
advice, in notebooks, and in whatever an agent wrote last week against a campaign.

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

The two nest, and that is not hypothetical: the panel query above is exactly a
``CAST(CAST(...) ...)``. So this walks the string with a scanner rather than matching a
regex, tracking string literals and quoted identifiers so a ``CAST`` inside one is left
alone.

**What this deliberately is not.** It is not a dialect layer and must not grow into one.
Each entry earns its place by being a *silent* difference in SQL people have already
written; anything Postgres rejects outright is better left to fail, because the author
sees the error and fixes the query. Both entries here were found by differential test
against the implementation being replaced, and that is the only way another should be
added.
"""

import logging
import re

logger = logging.getLogger(__name__)

#: Recognised inside a ``CAST(... AS <type>)``, case-insensitively.
_REAL_TYPES = {"real"}
_INTEGER_TYPES = {"integer", "int"}

_CAST_START = re.compile(r"\bCAST\s*\(", re.IGNORECASE)


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


def _is_bare_as(body: str, i: int) -> bool:
    """Is *body* at *i* the keyword ``AS``, not part of a longer word?

    ``last_seen`` and ``as_of`` both contain the letters; neither is the keyword.
    """
    if body[i:i + 2].upper() != "AS":
        return False
    before_ok = i == 0 or not body[i - 1].isalnum()
    after_ok = i + 2 >= len(body) or not body[i + 2].isalnum()
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
        elif depth == 0 and _is_bare_as(body, i):
            found = i
            i += 2
            continue
        i += 1
    return found


def translate(sql: str) -> str:
    """Rewrite the SQLite ``CAST`` spellings that Postgres reads differently.

    Idempotent in practice: the replacements produce syntax this function does not match
    again (``double precision`` is not in :data:`_REAL_TYPES`, and ``trunc(...)::bigint``
    is not a ``CAST``).
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
        if not match:
            out.append(char)
            i += 1
            continue

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

    result = "".join(out)
    if changed:
        logger.debug("index: translated %d SQLite cast(s)", changed)
    return result
