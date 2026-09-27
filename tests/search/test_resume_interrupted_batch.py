# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""A search interrupted in the middle of a batch resumes as if it had not been.

The interrupted batch holds some of its units. Replayed as it stands, it would tell the
strategy a short batch the uninterrupted search never told -- optuna closes the missing
trials as failed, qd closes the generation incomplete -- and every proposal after it would
differ. The resumed loop re-asks it, keeps what it recorded, runs only the missing cells and
tells the whole batch.

Each case runs one search straight through, and one that is killed after *k* units of a
batch and carried on by a fresh controller with a fresh strategy from the store alone.
"""

# pylint: disable=protected-access

import hashlib
import shutil
import sqlite3

import pytest

from robovast.common.config import SearchConfig
from robovast.common.store import STORE_FILENAME, CampaignStore, read_batch_objectives
from robovast.search.history import recorded_batches, unfinished_batch
from robovast.search.types import Evaluation, ParamSet

from .test_loop_and_store import FakeBackend, _search_controller
from .test_resume_parity_recall import _spy

BATCHES = 6
INTERRUPTED = 2          # the batch the first half is killed in

CASES = {
    "optuna": {
        "strategy": "optuna", "strategy_parameters": {"n_startup_trials": 4}, "per_batch": 4,
        "search_space": {"a": {"type": "choice", "values": [0, 1, 2, 3, 4]},
                         "b": {"type": "choice", "values": [0, 1, 2, 3]}}},
    "qd": {
        "strategy": "qd", "per_batch": 4,
        "strategy_parameters": {"archive": {"type": "cvt", "cells": 16, "measures": {
            "m1": {"low": 0.0, "high": 1.0}, "m2": {"low": 0.0, "high": 1.0}}}},
        "search_space": {"a": {"type": "float", "low": 0.0, "high": 1.0},
                         "b": {"type": "float", "low": 0.0, "high": 1.0}}},
    "boundary": {
        "strategy": "boundary", "strategy_parameters": {"level": 0.5, "candidates": 16},
        "per_batch": 4,
        "search_space": {"a": {"type": "int", "low": 0, "high": 9},
                         "b": {"type": "int", "low": 0, "high": 9}}},
    "random": {
        "strategy": "random", "per_batch": 4,
        "search_space": {"a": {"type": "float", "low": 0.0, "high": 1.0}}},
}


def _cfg(name, batches=BATCHES):
    if name in ("optuna", "qd"):
        pytest.importorskip({"optuna": "optuna", "qd": "ribs"}[name])
    return SearchConfig(
        extract={"plugin": "failure_rate"},
        objectives=[{"name": "f", "direction": "maximize"}],
        budget=[{"batches": batches}], seed=5, **CASES[name])


def _digits(ps, i):
    return int(hashlib.sha256(ps.id.encode()).hexdigest()[4 * i:4 * i + 4], 16) / 0xFFFF


class _Scorer:
    """Scores a cell from its identity alone, so a cell run twice scores the same."""

    def __init__(self):
        self.scored = []

    def evaluate(self, config_dir, ps):
        self.scored.append(ps.id)
        return Evaluation(params=ps, objectives={"f": _digits(ps, 0)},
                          measures={"m1": _digits(ps, 1), "m2": _digits(ps, 2)}, n_samples=1)


class _TwoGroups:
    """A repetition policy splitting every batch into two reps groups by cell identity."""

    def assign(self, param_sets, history):
        return [ParamSet(values=ps.values, id=ps.id, n_reps=1 + int(_digits(ps, 3) * 2))
                for ps in param_sets]


class _TaggingBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.tags = []

    def run_batch(self, campaign_data, *, campaign_root, batch_tag, runs, options):
        self.tags.append(batch_tag)
        return super().run_batch(campaign_data, campaign_root=campaign_root,
                                 batch_tag=batch_tag, runs=runs, options=options)


class _Killed(BaseException):
    """The process going away: nothing after it runs."""


def _controller(cfg, root, policy=None):
    controller, store, _ = _search_controller(cfg, root, evaluator=_Scorer())
    controller.backend = _TaggingBackend()
    controller.repetition_policy = policy
    return controller, store


def _kill_after(store, batch_idx, k, at_completion=False):
    """Kill the loop once batch *batch_idx* has recorded *k* units, or at its completion."""
    ids, count = {}, [0]
    open_batch, record_unit, complete_batch = (store.open_batch, store.record_unit,
                                               store.complete_batch)

    def spy_open(campaign_id, idx, *args, **kwargs):
        ids[idx] = open_batch(campaign_id, idx, *args, **kwargs)
        return ids[idx]

    def spy_record(*args, **kwargs):
        if kwargs["batch_id"] == ids.get(batch_idx) and not at_completion:
            if count[0] == k:
                raise _Killed()
            count[0] += 1
        return record_unit(*args, **kwargs)

    def spy_complete(batch_id):
        if at_completion and batch_id == ids.get(batch_idx):
            raise _Killed()
        return complete_batch(batch_id)

    store.open_batch, store.record_unit, store.complete_batch = (spy_open, spy_record,
                                                                 spy_complete)


def _units(db_path):
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT b.idx, u.paramset_id, u.status, u.n_reps FROM unit u "
        "JOIN batch b ON u.batch_id = b.id ORDER BY b.idx, u.id").fetchall()
    conn.close()
    return rows


def _outcome(report):
    extra = {k: v for k, v in report.extra.items() if k != "stop"}
    stop = {k: v for k, v in report.extra["stop"].items() if k != "elapsed_s"}
    return ([(ev.params.id, ev.objectives, ev.measures) for ev in report.evaluations],
            extra, stop)


def _straight(name, root, policy=None):
    controller, store = _controller(_cfg(name), root, policy)
    log = _spy(controller)
    report = controller.run()
    store.close()
    return log, report, root / "camp" / STORE_FILENAME


def _interrupt(name, root, k, policy=None, at_completion=False, batches=BATCHES):
    controller, store = _controller(_cfg(name, batches), root, policy)
    _kill_after(store, INTERRUPTED, k, at_completion)
    with pytest.raises(_Killed):
        controller.run()
    store.close()
    return root / "camp" / STORE_FILENAME


def _resume(name, root, policy=None, batches=BATCHES):
    controller, store = _controller(_cfg(name, batches), root, policy)
    log = _spy(controller)
    report = controller.run()
    store.close()
    return log, report, controller


def _assert_same_search(straight, resumed):
    (whole, whole_report, whole_db), (log, report, db) = straight, resumed
    assert log["asks"] == whole["asks"]
    assert log["tells"] == whole["tells"]
    assert _outcome(report) == _outcome(whole_report)
    assert _units(db) == _units(whole_db)


@pytest.mark.parametrize("k", [0, 2])
@pytest.mark.parametrize("name", CASES)
def test_a_batch_interrupted_after_k_units_resumes_as_the_uninterrupted_search(
        tmp_path, name, k):
    straight = _straight(name, tmp_path / "straight")
    db = _interrupt(name, tmp_path / "resumed", k)

    with CampaignStore(db) as store:
        pending = unfinished_batch(store, 1)
        assert len(recorded_batches(store, 1)) == INTERRUPTED
    assert pending.idx == INTERRUPTED and len(pending.units) == k

    log, report, controller = _resume(name, tmp_path / "resumed")
    _assert_same_search(straight, (log, report, db))
    # The cells the interrupted run recorded were not scored again.
    assert not set(pending.units) & set(controller.evaluator.scored)


@pytest.mark.parametrize("name", ["optuna", "boundary"])
def test_a_batch_interrupted_before_it_was_marked_complete_runs_nothing_again(tmp_path, name):
    """Every unit and recall recorded, the kill lands before the mark: the resume re-asks
    the batch, runs none of it and tells it."""
    straight = _straight(name, tmp_path / "straight")
    db = _interrupt(name, tmp_path / "resumed", 0, at_completion=True)

    log, report, controller = _resume(name, tmp_path / "resumed")
    _assert_same_search(straight, (log, report, db))
    assert not any(tag == f"batch-{INTERRUPTED}" for tag in controller.backend.tags)


def test_a_reps_group_the_interrupted_batch_recorded_whole_is_not_run_again(tmp_path):
    policy = _TwoGroups()
    straight = _straight("random", tmp_path / "straight", policy)
    groups = {}
    for idx, _, _, reps in _units(straight[2]):
        if idx == INTERRUPTED:
            groups[reps] = groups.get(reps, 0) + 1
    assert len(groups) == 2, groups
    first = groups[min(groups)]
    db = _interrupt("random", tmp_path / "resumed", first, policy)

    log, report, controller = _resume("random", tmp_path / "resumed", policy)
    _assert_same_search(straight, (log, report, db))
    ran = [t for t in controller.backend.tags if t.startswith(f"batch-{INTERRUPTED}/")]
    assert ran == [f"batch-{INTERRUPTED}/reps-{max(groups)}"]


def test_a_reask_that_proposes_other_cells_stops_the_resume(tmp_path):
    db = _interrupt("random", tmp_path, 1)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE unit SET paramset_id = 'elsewhere' WHERE batch_id = "
                 "(SELECT id FROM batch WHERE idx = ?)", (INTERRUPTED,))
    conn.commit()
    conn.close()

    controller, store = _controller(_cfg("random"), tmp_path)
    with pytest.raises(RuntimeError, match="elsewhere"):
        controller.run()
    store.close()


def _to_schema_14(db, *, batches):
    """*db* as schema 14 wrote it: no ``complete`` column, and the campaign's recorded
    ``batches`` count set to *batches*."""
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE batch DROP COLUMN complete")
    conn.execute("UPDATE campaign SET batches = ?", (batches,))
    conn.execute("PRAGMA user_version = 14")
    conn.commit()
    conn.close()


@pytest.mark.parametrize("recorded_count", [None, INTERRUPTED])
@pytest.mark.parametrize("name", ["optuna", "qd"])
def test_a_schema_14_store_with_a_short_last_batch_resumes_exactly(
        tmp_path, name, recorded_count):
    straight = _straight(name, tmp_path / "straight")
    db = _interrupt(name, tmp_path / "resumed", 1)
    _to_schema_14(db, batches=recorded_count)

    with CampaignStore(db) as store:
        assert len(recorded_batches(store, 1)) == INTERRUPTED
        assert unfinished_batch(store, 1).idx == INTERRUPTED

    _assert_same_search(straight, _resume(name, tmp_path / "resumed")[:2] + (db,))


def test_a_schema_14_last_batch_with_every_unit_runs_nothing_on_resume(tmp_path):
    """A campaign that ended between batches but recorded no count: its last batch is
    finished by re-asking it, which finds every cell recorded."""
    straight = _straight("optuna", tmp_path / "straight")
    controller, store = _controller(_cfg("optuna", INTERRUPTED), tmp_path / "resumed")
    controller.run()
    store.close()
    db = tmp_path / "resumed" / "camp" / STORE_FILENAME
    _to_schema_14(db, batches=None)

    log, report, resumed = _resume("optuna", tmp_path / "resumed")
    _assert_same_search(straight, (log, report, db))
    assert not any(t == f"batch-{INTERRUPTED - 1}" for t in resumed.backend.tags)


def test_a_schema_14_short_batch_that_is_not_the_last_replays_as_it_was_told(tmp_path):
    """Resumed before this schema, a short batch was told short and the search went on. It
    is history: replayed as the short batch it was, the store resumes as that search."""
    db = _interrupt("optuna", tmp_path / "short", 1)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE batch SET complete = 1 WHERE idx = ?", (INTERRUPTED,))
    conn.commit()
    conn.close()
    # The search as it went on from the short batch.
    went_on, _, _ = _resume("optuna", tmp_path / "short", batches=BATCHES)
    with CampaignStore(db) as store:
        assert len(recorded_batches(store, 1)[INTERRUPTED].evaluations) == 1

    shutil.copytree(tmp_path / "short", tmp_path / "old")
    old = tmp_path / "old" / "camp" / STORE_FILENAME
    _to_schema_14(old, batches=None)
    with CampaignStore(old) as store:
        batches = recorded_batches(store, 1)
        assert len(batches) == BATCHES - 1 and unfinished_batch(store, 1) is not None
        assert len(batches[INTERRUPTED].evaluations) == 1

    log, _, _ = _resume("optuna", tmp_path / "old", batches=BATCHES + 2)
    assert log["asks"][:BATCHES] == went_on["asks"]
    assert log["tells"][:BATCHES] == went_on["tells"]


def test_the_migration_marks_what_the_record_decides(tmp_path):
    from robovast.common.store import _MIGRATION_INITIAL, _MIGRATIONS

    db = tmp_path / "v14.db"
    conn = sqlite3.connect(db)
    conn.executescript(_MIGRATION_INITIAL)
    for step in _MIGRATIONS[1:14]:
        conn.executescript(step)
    conn.execute("PRAGMA user_version = 14")
    conn.execute("INSERT INTO campaign (id, name, mode, batches) VALUES (1, 'a', 'search', 1)")
    conn.execute("INSERT INTO campaign (id, name, mode, batches) VALUES (2, 'b', 'search', 2)")
    for cid, idx in [(1, 0), (1, 1), (1, 2), (2, 0), (2, 1)]:
        conn.execute("INSERT INTO batch (campaign_id, idx) VALUES (?, ?)", (cid, idx))
    conn.commit()
    conn.close()

    with CampaignStore(db) as store:
        complete = store._conn.execute(
            "SELECT campaign_id, idx, complete FROM batch ORDER BY campaign_id, idx").fetchall()
    # Not the last, or covered by the recorded count; campaign 1's last batch is neither.
    assert [tuple(r) for r in complete] == [(1, 0, 1), (1, 1, 1), (1, 2, None),
                                            (2, 0, 1), (2, 1, 1)]


def test_the_history_marks_a_batch_that_is_not_complete(tmp_path):
    db = _interrupt("random", tmp_path, 1)
    history = read_batch_objectives(db.parent)
    assert [b["complete"] for b in history["batches"]] == [True] * INTERRUPTED + [False]

    # A store this robovast has not migrated is read by the migration's rule.
    _to_schema_14(db, batches=None)
    history = read_batch_objectives(db.parent)
    assert [b["complete"] for b in history["batches"]] == [True] * INTERRUPTED + [False]
