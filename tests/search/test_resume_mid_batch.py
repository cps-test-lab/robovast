# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""A batch the process died inside is asked again on a resume, not counted as finished.

The batch row is written when the batch opens and a unit row as each cell is scored, so a
process that dies part-way leaves a batch holding some of its cells. Counted as finished,
the replay told the strategy a partial generation it never saw, and the resumed search
began the next batch after it -- a different search from the one that was interrupted.
"""

import sqlite3

import pytest

from robovast.common.config import SearchConfig
from robovast.common.store import CampaignStore
from robovast.search.evaluator import Evaluator
from robovast.search.history import recorded_batches

from .test_loop_and_store import _search_controller
from .test_resume_parity_recall import _spy

BATCHES = 5
DIE_IN_BATCH = 2
PER_BATCH = 3


class ProcessDied(BaseException):
    """What a killed process looks like from inside the loop: nothing catches it."""


def _cfg(name, batches=BATCHES):
    if name == "optuna":
        pytest.importorskip("optuna")
    params = {"optuna": {"n_startup_trials": 2}, "boundary": {"level": 0.5},
              "random": {}}[name]
    return SearchConfig(
        strategy=name, strategy_parameters=params, per_batch=PER_BATCH,
        search_space={"x": {"type": "float", "low": 0.0, "high": 1.0},
                      "y": {"type": "float", "low": -1.0, "high": 1.0}},
        extract={"plugin": "failure_rate"},
        objectives=[{"name": "failure_rate", "direction": "maximize"}],
        budget=[{"batches": batches}], seed=5)


class _DiesMidBatch:
    """Scores cells until the second cell of batch DIE_IN_BATCH, then dies."""

    def __init__(self, evaluator):
        self.evaluator = evaluator
        self.calls = 0

    def evaluate(self, config_dir, params):
        self.calls += 1
        if self.calls == DIE_IN_BATCH * PER_BATCH + 2:
            raise ProcessDied()
        return self.evaluator.evaluate(config_dir, params)


@pytest.mark.parametrize("name", ["random", "boundary", "optuna"])
def test_a_search_killed_mid_batch_resumes_to_the_uninterrupted_sequence(tmp_path, name):
    straight, _, _ = _search_controller(_cfg(name), tmp_path / "straight")
    whole = _spy(straight)
    straight.run()

    cfg = _cfg(name)
    dying = _DiesMidBatch(Evaluator(cfg, str(tmp_path / "resumed")))
    first, store, _ = _search_controller(cfg, tmp_path / "resumed", evaluator=dying)
    with pytest.raises(ProcessDied):
        first.run()
    # The batch it died in holds one cell of its own: a partial record.
    conn = sqlite3.connect(store.db_path)
    assert conn.execute(
        "SELECT COUNT(*) FROM unit u JOIN batch b ON u.batch_id = b.id "
        "WHERE b.idx = ?", (DIE_IN_BATCH,)).fetchone()[0] == 1
    conn.close()

    second, _, _ = _search_controller(_cfg(name), tmp_path / "resumed")
    second.store = store
    resumed = _spy(second)
    second.run()

    assert resumed["tells"] == whole["tells"]
    assert resumed["asks"] == whole["asks"]
    assert second._batches_done == BATCHES        # noqa: SLF001
    conn = sqlite3.connect(store.db_path)
    assert conn.execute("SELECT idx FROM batch ORDER BY idx").fetchall() == [
        (i,) for i in range(BATCHES)]             # one row per batch, the re-asked one too
    conn.close()


def _v14_store(tmp_path, recorded_batches_count):
    """A store written under schema 14: three search batches, no ``closed`` column."""
    from robovast.common.store import _MIGRATION_INITIAL, _MIGRATIONS

    db = tmp_path / "v14.db"
    conn = sqlite3.connect(db)
    conn.executescript(_MIGRATION_INITIAL)
    for step in _MIGRATIONS[1:14]:
        conn.executescript(step)
    conn.execute("PRAGMA user_version = 14")
    conn.execute("INSERT INTO campaign (id, name, mode, batches) VALUES (1, 'old', 'search', ?)",
                 (recorded_batches_count,))
    for idx in range(3):
        conn.execute("INSERT INTO batch (id, campaign_id, idx, asked, recalls_recorded) "
                     "VALUES (?, 1, ?, 1, 1)", (idx + 1, idx))
        conn.execute("INSERT INTO unit (batch_id, paramset_id, params_json, objectives_json, "
                     "status) VALUES (?, ?, '{}', '{}', 'evaluated')", (idx + 1, f"p{idx}"))
    conn.commit()
    conn.close()
    return db


def test_an_older_store_counts_every_batch_but_the_last_as_finished(tmp_path):
    """No outcome recorded: the process died without one, most likely inside its last
    batch, so that batch is asked again."""
    with CampaignStore(_v14_store(tmp_path, None)) as store:
        assert len(recorded_batches(store, 1)) == 2
        assert store.discard_open_batches(1) == [2]


def test_an_older_store_whose_outcome_counted_the_last_batch_keeps_it(tmp_path):
    with CampaignStore(_v14_store(tmp_path, 3)) as store:
        assert len(recorded_batches(store, 1)) == 3
        assert store.discard_open_batches(1) == []
