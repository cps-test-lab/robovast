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
"""

# pylint: disable=redefined-outer-name,protected-access  # pytest fixtures; the size
# ceiling is deliberately private and this is the test that owns it.

import json
import sqlite3

import pytest

from robovast.results_processing import data_query
from robovast.results_processing.data_query import query_data_db


@pytest.fixture
def wide_campaign(tmp_path):
    """A campaign whose rows are individually legal and collectively enormous."""
    execution = tmp_path / "_execution"
    execution.mkdir()
    con = sqlite3.connect(execution / "data.db")
    con.execute("CREATE TABLE poses (run_id TEXT, t REAL, blob TEXT)")
    # 1 KB per row: nowhere near the 2048-byte cell cap, so nothing is trimmed per-cell.
    con.executemany("INSERT INTO poses VALUES (?, ?, ?)",
                    [(f"run-{i}", i * 0.1, "x" * 1024) for i in range(5000)])
    con.commit()
    con.close()
    return str(tmp_path)


def _tokens(result) -> int:
    return len(json.dumps(result, default=str)) // 4


def test_a_wide_select_is_bounded_by_size(wide_campaign):
    """The regression: 5000 legal rows of a legal width is not a legal reply."""
    result = query_data_db(wide_campaign, "SELECT * FROM poses", max_rows=5000)

    assert len(json.dumps(result).encode()) <= data_query._MAX_RESULT_BYTES * 1.1
    assert result["row_count"] < 5000, "the size cap did not engage"
    assert result["truncated"] is True


def test_it_says_why_and_what_to_do_instead(wide_campaign):
    """Truncation the caller cannot see is how a partial answer becomes a wrong one.

    The note has to distinguish this from the row cap, because the caller's fix differs:
    asking for fewer rows does not help a query whose *rows* are the problem.
    """
    result = query_data_db(wide_campaign, "SELECT * FROM poses", max_rows=5000)

    note = result.get("note", "")
    assert "ceiling" in note or "KB" in note, note
    assert "aggregate" in note.lower(), "must name the cheaper question"
    assert "csv" in note.lower(), "must name the way out for bulk data"


def test_at_least_one_row_survives(wide_campaign):
    """An empty result reads as "no data", which is a different answer from "your query
    was too wide". Even a single oversized row comes back."""
    con = sqlite3.connect(f"{wide_campaign}/_execution/data.db")
    con.execute("DELETE FROM poses WHERE run_id != 'run-0'")
    con.commit()
    con.close()

    result = query_data_db(wide_campaign, "SELECT * FROM poses", max_rows=5000)
    assert result["row_count"] == 1


def test_a_small_result_is_untouched(wide_campaign):
    """The cap must be invisible to every query that was already reasonable -- which is
    the overwhelming majority, and all of the ones worth encouraging."""
    result = query_data_db(wide_campaign, "SELECT COUNT(*) AS n FROM poses")

    assert result["row_count"] == 1
    assert result["truncated"] is False
    assert "note" not in result
    assert _tokens(result) < 100


def test_the_row_cap_still_applies_on_its_own(wide_campaign):
    """Two independent bounds. A narrow query hits rows, not bytes."""
    result = query_data_db(wide_campaign, "SELECT run_id FROM poses", max_rows=10)

    assert result["row_count"] == 10
    assert result["truncated"] is True
    assert "ceiling" not in result.get("note", ""), "this is the row cap, not the size cap"


def test_a_rendering_caller_can_raise_the_ceiling(wide_campaign):
    """The ceiling is a *token* budget, so it belongs to callers who spend tokens.

    The web UI draws the rows rather than reading them, and at the default a run-view chart
    over ``poses`` stopped at ~120 rows while still reporting the 5000-row cap -- which reads
    as "the run ended here", not "the reply did". A caller who can hold the rows says so.
    """
    result = query_data_db(wide_campaign, "SELECT * FROM poses", max_rows=5000,
                           max_bytes=8 * 1024 * 1024)

    assert result["row_count"] == 5000, "the raised ceiling was not honoured"
    assert result["truncated"] is False
    assert "note" not in result


def test_the_raised_ceiling_is_still_a_ceiling(wide_campaign):
    """Raised, not removed: a caller asking for more than it named still gets bounded."""
    result = query_data_db(wide_campaign, "SELECT * FROM poses", max_rows=5000,
                           max_bytes=256 * 1024)

    assert result["row_count"] < 5000
    assert result["truncated"] is True
    assert "256 KB" in result["note"], result["note"]


def test_omitting_the_budget_fails_safe(wide_campaign):
    """The default has to be the *small* one: an agent that forgets the parameter loses a
    query, while a chart that forgets it loses only resolution. Wiring it the other way
    round would make the context blow-up the silent case again."""
    default = query_data_db(wide_campaign, "SELECT * FROM poses", max_rows=5000)
    explicit = query_data_db(wide_campaign, "SELECT * FROM poses", max_rows=5000,
                             max_bytes=data_query._MAX_RESULT_BYTES)

    assert default["row_count"] == explicit["row_count"]
