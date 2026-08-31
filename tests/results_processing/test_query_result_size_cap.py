# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A query's reply is bounded by its *size*, not only by its row count.

``max_rows`` and ``_MAX_CELL_BYTES`` bound two axes separately and neither bounds their
product. Against a real campaign, the **default** ``SELECT * FROM poses`` -- 500 rows,
well inside every documented cap -- serialized to ~270 KB, about 67,000 tokens; the
5000-row clamp to roughly ten times that. Both were reported as successful replies.

That is a wrong answer rather than a large one: an agent that spends its entire context
on a single ``SELECT *`` cannot then do anything with what it read, and the caps that
were supposed to prevent it all reported themselves satisfied.

A caller who wants the data rather than the answer has ``stream_query_csv``, which has no
row cap at all. This path is for answers.

The rows now come from the central index rather than a per-campaign ``data.db``, which
changes nothing about the ceiling: it is client-side, applied to the reply after the
fetch. So the arithmetic is pinned directly on :func:`_cap_result_size` and runs with no
database at all, and the end-to-end tests -- the ones that also pin what the caller is
*told* -- run against an ingested campaign when ``ROBOVAST_TEST_PG_DSN`` is set.
"""

# pylint: disable=redefined-outer-name,protected-access  # pytest fixtures; the size
# ceiling is deliberately private and this is the test that owns it.

import json

import pytest

from robovast.results_processing import data_query
from robovast.results_processing.data_query import _cap_result_size, query_data_db

from .conftest import DSN, drop_schema, ingest, reset_schema

#: 1 KB per row: nowhere near the 2048-byte cell cap, so nothing is trimmed per-cell.
WIDE_ROWS = 5000
WIDE_CELL = "x" * 1024


def _rows(count: int) -> list:
    return [{"run_id": f"run-{i}", "t": i * 0.1, "blob": WIDE_CELL} for i in range(count)]


def _tokens(result) -> int:
    return len(json.dumps(result, default=str)) // 4


# -- the arithmetic, which needs no database --------------------------------


def test_rows_that_are_individually_legal_are_collectively_capped():
    """The regression: 5000 legal rows of a legal width is not a legal reply."""
    rows, capped = _cap_result_size(_rows(WIDE_ROWS))

    assert capped is True
    assert len(rows) < WIDE_ROWS
    assert len(json.dumps(rows).encode()) <= data_query._MAX_RESULT_BYTES * 1.1


def test_at_least_one_row_survives():
    """An empty result reads as "no data", which is a different answer from "your query
    was too wide". Even a single oversized row comes back."""
    rows, capped = _cap_result_size([{"blob": "x" * 100_000}])

    assert len(rows) == 1
    assert capped is True


def test_a_small_result_is_untouched():
    """The cap must be invisible to every query that was already reasonable -- which is
    the overwhelming majority, and all of the ones worth encouraging."""
    small = [{"n": 5000}]

    assert _cap_result_size(small) == (small, False)


def test_a_raised_ceiling_admits_more_rows_and_is_still_a_ceiling():
    """Raised, not removed: the budget is a parameter, not a switch."""
    default, _ = _cap_result_size(_rows(WIDE_ROWS))
    raised, capped = _cap_result_size(_rows(WIDE_ROWS), 256 * 1024)

    assert len(raised) > len(default)
    assert capped is True

    whole, capped = _cap_result_size(_rows(WIDE_ROWS), 8 * 1024 * 1024)
    assert (len(whole), capped) == (WIDE_ROWS, False)


def test_the_default_budget_is_the_small_one():
    """The default has to be the *small* one: an agent that forgets the parameter loses a
    query, while a chart that forgets it loses only resolution. Wiring it the other way
    round would make the context blow-up the silent case again."""
    assert data_query._MAX_RESULT_BYTES == 64 * 1024
    assert (_cap_result_size(_rows(WIDE_ROWS))
            == _cap_result_size(_rows(WIDE_ROWS), data_query._MAX_RESULT_BYTES))


# -- what the caller is told, which needs the index -------------------------

pg = pytest.mark.skipif(not DSN, reason="ROBOVAST_TEST_PG_DSN is not set")

WIDE = "camp-wide-2026-08-10-07150921"


@pytest.fixture(scope="module")
def wide_campaign(tmp_path_factory):
    """A campaign whose rows are individually legal and collectively enormous.

    Module-scoped: ingesting 5 MB of poses once is the whole cost of this file.
    """
    if not DSN:
        pytest.skip("ROBOVAST_TEST_PG_DSN is not set")
    psycopg = pytest.importorskip("psycopg")
    import os

    from robovast.common import index_db

    previous = os.environ.get(index_db.DSN_ENV)
    reset_schema(psycopg)
    from .conftest import SCHEMA
    os.environ[index_db.DSN_ENV] = f"{DSN} options=-csearch_path={SCHEMA}"

    root = tmp_path_factory.mktemp("wide") / WIDE
    run = root / "cfg-a" / "0"
    run.mkdir(parents=True)
    (root / "_execution").mkdir(parents=True)
    with (run / "poses.csv").open("w") as handle:
        handle.write("pose_id,t,blob\n")
        for i in range(WIDE_ROWS):
            handle.write(f"run-{i},{i * 0.1},{WIDE_CELL}\n")
    ingest(root, WIDE)

    yield root

    drop_schema(psycopg)
    if previous is None:
        os.environ.pop(index_db.DSN_ENV, None)
    else:
        os.environ[index_db.DSN_ENV] = previous


@pg
def test_a_wide_select_is_bounded_by_size(wide_campaign):
    """End to end: the reply, not just the trimming helper, stays inside the ceiling."""
    result = query_data_db(wide_campaign, "SELECT * FROM poses", max_rows=WIDE_ROWS)

    assert len(json.dumps(result).encode()) <= data_query._MAX_RESULT_BYTES * 1.1
    assert result["row_count"] < WIDE_ROWS, "the size cap did not engage"
    assert result["truncated"] is True


@pg
def test_it_says_why_and_what_to_do_instead(wide_campaign):
    """Truncation the caller cannot see is how a partial answer becomes a wrong one.

    The note has to distinguish this from the row cap, because the caller's fix differs:
    asking for fewer rows does not help a query whose *rows* are the problem.
    """
    result = query_data_db(wide_campaign, "SELECT * FROM poses", max_rows=WIDE_ROWS)

    note = result.get("note", "")
    assert "ceiling" in note or "KB" in note, note
    assert "aggregate" in note.lower(), "must name the cheaper question"
    assert "csv" in note.lower(), "must name the way out for bulk data"


@pg
def test_a_small_result_is_reported_as_untouched(wide_campaign):
    """The cap must be invisible to every query that was already reasonable."""
    result = query_data_db(wide_campaign, "SELECT COUNT(*) AS n FROM poses")

    assert result["row_count"] == 1
    assert result["truncated"] is False
    assert "note" not in result
    assert _tokens(result) < 100


@pg
def test_the_row_cap_still_applies_on_its_own(wide_campaign):
    """Two independent bounds. A narrow query hits rows, not bytes."""
    result = query_data_db(wide_campaign, "SELECT pose_id FROM poses", max_rows=10)

    assert result["row_count"] == 10
    assert result["truncated"] is True
    assert "ceiling" not in result.get("note", ""), "this is the row cap, not the size cap"


@pg
def test_a_rendering_caller_can_raise_the_ceiling(wide_campaign):
    """The ceiling is a *token* budget, so it belongs to callers who spend tokens.

    The web UI draws the rows rather than reading them, and at the default a run-view chart
    over ``poses`` stopped at ~120 rows while still reporting the 5000-row cap -- which reads
    as "the run ended here", not "the reply did". A caller who can hold the rows says so.
    """
    result = query_data_db(wide_campaign, "SELECT * FROM poses", max_rows=WIDE_ROWS,
                           max_bytes=16 * 1024 * 1024)

    assert result["row_count"] == WIDE_ROWS, "the raised ceiling was not honoured"
    assert result["truncated"] is False
    assert "note" not in result


@pg
def test_the_raised_ceiling_is_still_a_ceiling(wide_campaign):
    """Raised, not removed: a caller asking for more than it named still gets bounded."""
    result = query_data_db(wide_campaign, "SELECT * FROM poses", max_rows=WIDE_ROWS,
                           max_bytes=256 * 1024)

    assert result["row_count"] < WIDE_ROWS
    assert result["truncated"] is True
    assert "256 KB" in result["note"], result["note"]


@pg
def test_omitting_the_budget_fails_safe(wide_campaign):
    """The default has to be the *small* one: an agent that forgets the parameter loses a
    query, while a chart that forgets it loses only resolution."""
    default = query_data_db(wide_campaign, "SELECT * FROM poses", max_rows=WIDE_ROWS)
    explicit = query_data_db(wide_campaign, "SELECT * FROM poses", max_rows=WIDE_ROWS,
                             max_bytes=data_query._MAX_RESULT_BYTES)

    assert default["row_count"] == explicit["row_count"]
