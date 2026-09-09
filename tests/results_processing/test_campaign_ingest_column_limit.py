# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""A file whose flattened columns exceed Postgres's per-table limit must not cost the
rest of a run -- or, by the same mechanism repeated across ``ingest_campaign``'s walk, the
rest of an otherwise-healthy campaign -- its queryable index.

Uses a fake sink rather than a real Postgres: what is under test is ``ingest_run``'s
control flow around the refusal, not the database it eventually reaches, so this runs
unconditionally rather than only when a test database is configured.
"""

import pytest

from robovast.common.errors import TableColumnLimitExceeded
from robovast.results_processing import campaign_ingest


class _SelectivelyExplodingSink:
    """A sink that refuses one named table, like ``ensure_table`` would for a table with
    too many columns, and writes normally for everything else."""

    def __init__(self, refuses):
        self.refuses = set(refuses)
        self.writes = []

    def write(self, table, rows, context=None, types=None, source=""):  # noqa: D102
        if table in self.refuses:
            raise TableColumnLimitExceeded(
                f"'{table}' would need too many columns (from {source})")
        rows = list(rows)
        self.writes.append((table, rows))
        return len(rows)


def _run_dir(tmp_path, config_name, run_id, files):
    d = tmp_path / config_name / str(run_id)
    d.mkdir(parents=True)
    for name, content in files.items():
        (d / name).write_text(content, encoding="utf-8")
    return d


def test_ingest_run_skips_only_the_refused_file(tmp_path):
    run_dir = _run_dir(tmp_path, "goal-1", 0, {
        "costmap.csv": "cell_0,cell_1\n1,2\n",
        "nav_metrics.csv": "duration_s\n12.5\n",
    })
    sink = _SelectivelyExplodingSink(refuses={"costmap"})
    failed = []

    written = campaign_ingest.ingest_run(sink, run_dir, "goal-1", 0, failed=failed)

    assert written == {"nav_metrics": 1}, "the well-behaved file must still be written"
    assert len(failed) == 1
    table, path, message = failed[0]
    assert table == "costmap"
    assert "costmap.csv" in path
    assert "too many columns" in message


def test_ingest_run_without_a_failed_list_still_raises(tmp_path):
    """Callers that have not opted into per-file isolation keep the old behaviour."""
    run_dir = _run_dir(tmp_path, "goal-1", 0, {"costmap.csv": "cell_0,cell_1\n1,2\n"})
    sink = _SelectivelyExplodingSink(refuses={"costmap"})

    with pytest.raises(TableColumnLimitExceeded):
        campaign_ingest.ingest_run(sink, run_dir, "goal-1", 0)


def test_a_second_run_after_the_refused_one_still_ingests(tmp_path):
    """The refusal is scoped to one file, not to the rest of the walk: a second run's
    ``ingest_run`` call, sharing the same ``failed`` list, must be unaffected by the
    first run's refused file."""
    sink = _SelectivelyExplodingSink(refuses={"costmap"})
    failed = []

    run0 = _run_dir(tmp_path, "goal-1", 0, {"costmap.csv": "cell_0,cell_1\n1,2\n"})
    run1 = _run_dir(tmp_path, "goal-1", 1, {"nav_metrics.csv": "duration_s\n9.0\n"})

    campaign_ingest.ingest_run(sink, run0, "goal-1", 0, failed=failed)
    written1 = campaign_ingest.ingest_run(sink, run1, "goal-1", 1, failed=failed)

    assert written1 == {"nav_metrics": 1}
    assert len(failed) == 1, "only the first run's file was refused"
    assert [t for t, _rows in sink.writes] == ["nav_metrics"]
