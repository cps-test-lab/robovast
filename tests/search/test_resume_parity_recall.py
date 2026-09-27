# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""A resumed search that recalled cells tells its strategy what the uninterrupted one did.

A cell an earlier batch measured is not run again; the strategy is told what it scored
then. The replay that rebuilds a strategy on a resume must tell it the same thing, or the
strategy is handed a shorter generation than it saw live -- optuna then closes a trial that
completed as FAIL -- and every proposal after the resume differs from the search that was
never interrupted.

Driven through the controller and its store end to end, on a space small enough that the
strategies re-propose cells within the first batches: one search runs straight through,
the other stops half way and a fresh controller, with a fresh strategy, carries it on from
the store.
"""

import sqlite3

import pytest

from robovast.common.config import SearchConfig

from .test_loop_and_store import _search_controller

BATCHES = 6
STOP_AFTER = 3

#: Each strategy with a discrete space it re-proposes cells of within STOP_AFTER batches.
CASES = {
    "optuna": {
        "strategy": "optuna", "strategy_parameters": {"n_startup_trials": 2}, "per_batch": 3,
        "search_space": {"a": {"type": "choice", "values": [0, 1]},
                         "b": {"type": "choice", "values": [0, 1, 2]}}},
    "boundary": {
        "strategy": "boundary", "strategy_parameters": {"level": 0.5, "candidates": 16},
        "per_batch": 3,
        "search_space": {"a": {"type": "int", "low": 0, "high": 2},
                         "b": {"type": "int", "low": 0, "high": 1}}},
}


def _cfg(name, batches):
    if name == "optuna":
        pytest.importorskip("optuna")
    return SearchConfig(
        extract={"plugin": "failure_rate"},
        objectives=[{"name": "failure_rate", "direction": "maximize"}],
        budget=[{"batches": batches}], seed=3, **CASES[name])


def _spy(controller):
    """Record every ask and tell the controller's strategy sees, replay included.

    Wrapped on the instance, so ``resume`` -- which calls ``self.ask``/``self.tell`` --
    goes through the recording too.
    """
    strategy = controller.strategy
    log = {"asks": [], "tells": []}
    ask, tell = strategy.ask, strategy.tell

    def spy_ask(n):
        proposed = ask(n)
        log["asks"].append([ps.id for ps in proposed])
        return proposed

    def spy_tell(evaluations):
        log["tells"].append([(ev.params.id, dict(ev.objectives)) for ev in evaluations])
        return tell(evaluations)

    strategy.ask, strategy.tell = spy_ask, spy_tell
    return log


def _recalled_somewhere(tells):
    """Whether any batch told the strategy about a cell an earlier batch already told."""
    seen = set()
    for told in tells:
        ids = {cell for cell, _ in told}
        if ids & seen:
            return True
        seen |= ids
    return False


@pytest.mark.parametrize("name", CASES)
def test_a_resumed_search_tells_and_asks_what_the_uninterrupted_one_did(tmp_path, name):
    straight, _, _ = _search_controller(_cfg(name, BATCHES), tmp_path / "straight")
    whole = _spy(straight)
    straight.run()
    # The case is only a test of recall if the first half recalled something.
    assert _recalled_somewhere(whole["tells"][:STOP_AFTER]), whole["tells"]

    first, store, _ = _search_controller(_cfg(name, STOP_AFTER), tmp_path / "resumed")
    first.run()
    second, _, _ = _search_controller(_cfg(name, BATCHES), tmp_path / "resumed")
    second.store = store
    resumed = _spy(second)
    second.run()

    # The replay tells exactly what the first half told live, recalled cells included ...
    assert resumed["tells"][:STOP_AFTER] == whole["tells"][:STOP_AFTER]
    # ... so the first proposal after the resume, and everything after it, is the same.
    assert resumed["asks"][STOP_AFTER:] == whole["asks"][STOP_AFTER:]
    assert resumed["tells"][STOP_AFTER:] == whole["tells"][STOP_AFTER:]


def test_a_recalled_cell_is_a_row_naming_the_unit_that_measured_it(tmp_path):
    controller, store, _ = _search_controller(_cfg("optuna", STOP_AFTER), tmp_path)
    controller.run()

    conn = sqlite3.connect(store.db_path)
    conn.row_factory = sqlite3.Row
    recalls = conn.execute(
        "SELECT r.paramset_id, r.config_name, r.result_dir, r.objectives_json, r.n_reps, "
        "s.paramset_id AS source_id, s.status AS source_status, "
        "rb.idx AS batch, sb.idx AS source_batch "
        "FROM unit r JOIN unit s ON r.recalled_from = s.id "
        "JOIN batch rb ON r.batch_id = rb.id JOIN batch sb ON s.batch_id = sb.id "
        "WHERE r.status = 'recalled'").fetchall()
    assert recalls
    for row in recalls:
        assert row["source_id"] == row["paramset_id"]
        assert row["source_status"] == "evaluated"
        assert row["source_batch"] < row["batch"]
        # Nothing of the outcome is copied: the answer is the unit it names.
        assert (row["config_name"], row["result_dir"], row["objectives_json"],
                row["n_reps"]) == ("", "", "{}", 0)
    # No runs hang off a recalled cell, and every batch says it records them.
    assert conn.execute(
        "SELECT COUNT(*) FROM run WHERE unit_id IN "
        "(SELECT id FROM unit WHERE status = 'recalled')").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM batch WHERE recalls_recorded IS NOT 1").fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize("name", CASES)
def test_a_search_recorded_before_recalls_had_rows_resumes_exactly(tmp_path, name):
    """Batches written before schema 14 have no recalled rows: the replay reads those cells
    off the proposals it re-asks, and tells what the uninterrupted search told."""
    straight, _, _ = _search_controller(_cfg(name, BATCHES), tmp_path / "straight")
    whole = _spy(straight)
    straight.run()

    first, store, _ = _search_controller(_cfg(name, STOP_AFTER), tmp_path / "resumed")
    first.run()
    conn = sqlite3.connect(store.db_path)
    assert conn.execute("DELETE FROM unit WHERE status = 'recalled'").rowcount
    conn.execute("UPDATE batch SET recalls_recorded = NULL")
    conn.commit()
    conn.close()
    second, _, _ = _search_controller(_cfg(name, BATCHES), tmp_path / "resumed")
    second.store = store
    resumed = _spy(second)
    second.run()

    assert resumed["tells"] == whole["tells"]
    assert resumed["asks"] == whole["asks"]


def test_a_batch_migrated_from_schema_13_does_not_hold_its_recalls(tmp_path):
    from robovast.common.store import _MIGRATION_INITIAL, _MIGRATIONS, CampaignStore
    from robovast.search.history import recorded_batches

    db = tmp_path / "v13.db"
    conn = sqlite3.connect(db)
    conn.executescript(_MIGRATION_INITIAL)
    for step in _MIGRATIONS[1:13]:
        conn.executescript(step)
    conn.execute("PRAGMA user_version = 13")
    conn.execute("INSERT INTO campaign (id, name, mode, batches) VALUES (1, 'old', 'search', 1)")
    conn.execute("INSERT INTO batch (id, campaign_id, idx, asked) VALUES (1, 1, 0, 2)")
    conn.execute(
        "INSERT INTO unit (batch_id, paramset_id, params_json, objectives_json, status) "
        "VALUES (1, 'a', '{}', '{\"f\": 1.0}', 'evaluated')")
    conn.commit()
    conn.close()

    with CampaignStore(db) as store:
        (batch,) = recorded_batches(store, 1)

    assert not batch.recalls_recorded
    assert [ev.params.id for ev in batch.told] == ["a"]


def test_with_recalls_reads_each_distinct_measured_proposal_in_proposal_order():
    from robovast.search.history import RecordedBatch
    from robovast.search.types import Evaluation, ParamSet

    a, b = (Evaluation(params=ParamSet(values={"x": v}), objectives={"f": 1.0})
            for v in (1, 2))
    fresh = ParamSet(values={"x": 3})
    batch = RecordedBatch(asked=4, recalls_recorded=False).with_recalls(
        [b.params, fresh, a.params, b.params], {a.params.id: a, b.params.id: b})

    assert batch.recalled == [b, a]
    assert batch.recalls_recorded
