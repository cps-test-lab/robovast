# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""A strategy that proposes the same cell twice does not kill the batch.

``ParamSet.id`` is derived from the values, and everything downstream is addressed by it:
the config's name, its result directory, the unit row. So two draws with the same values
are one cell with one place to put its results -- and on a discrete space a strategy
proposes exactly that routinely, because TPE re-proposes a category it likes and a random
or low-discrepancy draw collides as soon as the space has few enough levels.

Composed anyway, the two became two configs under one name, and ``Compose._resolve_names``
read that as its OTHER cause -- a variation that expanded combinatorially -- and aborted
the campaign telling the operator to make variation parameters scalar, on a campaign that
need not declare a single variation. Had it got past that, both would have written into
one result directory and been recorded as two units over one set of runs.

The repeat is collapsed before composition. What the strategy PROPOSED survives in
``batch.asked``, because that count -- not the number of rows -- is what a resume replays
the strategy through.
"""

import os
import sqlite3
import textwrap

import pytest

from robovast.common.store import STORE_FILENAME, CampaignStore
from robovast.search.compose import Compose, distinct_draws
from robovast.search.history import recorded_batches
from robovast.search.types import ParamSet

from .test_loop_and_store import FakeCompose, _cfg, _search_controller

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXAMPLE = os.path.join(REPO, "configs", "examples", "quadrotor_landing")


def _ps(x):
    return ParamSet(values={"x": x})


# -- collapsing ------------------------------------------------------------------


def test_repeats_collapse_to_the_first_of_each_in_order():
    draws = [_ps(1), _ps(2), _ps(1), _ps(3), _ps(2), _ps(1)]
    assert [ps.values for ps in distinct_draws(draws)] == [{"x": 1}, {"x": 2}, {"x": 3}]


def test_a_batch_of_distinct_draws_is_returned_unchanged():
    draws = [_ps(1), _ps(2), _ps(3)]
    assert distinct_draws(draws) == draws


def test_the_collapse_is_reported(caplog):
    with caplog.at_level("WARNING"):
        distinct_draws([_ps(1), _ps(1), _ps(2)], "Batch 4")
    assert "Batch 4" in caplog.text
    assert "3 parameter set(s) but only 2 distinct" in caplog.text


# -- composition -----------------------------------------------------------------


BASE_VAST = textwrap.dedent("""\
    version: 3
    execution:
      containers: {scenario: {image: ghcr.io/cps-test-lab/robovast:latest}}
      runs: 1
      scenario_file: scenario.osc
    """)


@pytest.fixture()
def base_vast():
    if not os.path.exists(os.path.join(EXAMPLE, "scenario.osc")):
        pytest.skip("quadrotor_landing example scenario not available")
    path = os.path.join(EXAMPLE, ".robovast_test_repeated_draw.vast")
    with open(path, "w") as f:
        f.write(BASE_VAST)
    yield path
    os.remove(path)


def test_composing_a_repeat_is_refused_by_its_real_name(base_vast, tmp_path):
    """The message must name the repeated draw. Before, the same input reached
    `_resolve_names` and came back as a combinatorial variation -- a diagnosis pointing at
    variation parameters this .vast does not have."""
    repeated = ParamSet(values={"thrust_gain": 2.0, "mass": 1.5})
    with pytest.raises(ValueError) as excinfo:
        Compose(base_vast).compose([repeated, repeated], str(tmp_path / "art"))

    message = str(excinfo.value)
    assert repeated.id in message
    assert "distinct_draws" in message
    assert "num_paths" not in message      # not the combinatorial-variation diagnosis


def test_the_collapsed_batch_composes(base_vast, tmp_path):
    repeated = ParamSet(values={"thrust_gain": 2.0, "mass": 1.5})
    other = ParamSet(values={"thrust_gain": 0.5, "mass": 2.5})
    param_sets = distinct_draws([repeated, other, repeated])

    campaign_data, name_by_id = Compose(base_vast).compose(param_sets, str(tmp_path / "art"))

    assert len(campaign_data["configs"]) == 2
    assert set(name_by_id) == {repeated.id, other.id}


# -- the loop --------------------------------------------------------------------


class RepeatingStrategy:
    """Proposes one cell per batch, twice -- what a two-level space produces constantly."""

    RESUMABLE = True

    def __init__(self, cfg):
        self.objectives = cfg.objectives
        self.asked = []
        self.told = []
        self._n = 0

    def ask(self, n):
        self._n += 1
        return [ParamSet(values={"x": float(self._n)}) for _ in range(n)]

    def tell(self, evaluations):
        self.told.append(len(evaluations))

    def report(self):
        from robovast.search.types import SearchReport
        return SearchReport(extra={})


def test_a_batch_of_repeats_runs_and_records_one_cell(tmp_path):
    cfg = _cfg(batches=2, per_batch=3)
    strategy = RepeatingStrategy(cfg)
    controller, store, _ = _search_controller(
        cfg, tmp_path, strategy=strategy, compose=FakeCompose())

    controller.run()

    # One config composed and run per batch, not three under one name.
    conn = sqlite3.connect(store.db_path)
    conn.row_factory = sqlite3.Row
    units = conn.execute("SELECT * FROM unit ORDER BY id").fetchall()
    assert [u["status"] for u in units] == ["evaluated", "evaluated"]
    assert len({u["paramset_id"] for u in units}) == 2
    # And its runs are counted once: two reps of one cell per batch.
    assert conn.execute("SELECT COUNT(*) FROM run").fetchone()[0] == 4
    # The strategy is told the one evaluation that cell produced.
    assert strategy.told == [1, 1]


def test_the_batch_row_records_what_was_asked_not_what_was_kept(tmp_path):
    cfg = _cfg(batches=2, per_batch=3)
    controller, store, _ = _search_controller(
        cfg, tmp_path, strategy=RepeatingStrategy(cfg), compose=FakeCompose())
    controller.run()

    conn = sqlite3.connect(store.db_path)
    assert [r[0] for r in conn.execute("SELECT asked FROM batch ORDER BY idx")] == [3, 3]

    # Which is what a resume replays: ask three, having been told one.
    replayed = recorded_batches(store, 1)
    assert [b.asked for b in replayed] == [3, 3]
    assert [len(b.evaluations) for b in replayed] == [1, 1]


def test_a_store_written_before_asked_existed_falls_back_to_its_rows(tmp_path):
    """No draw was ever collapsed there -- a repeat aborted the campaign instead -- so the
    row count IS the ask size, and reading it keeps those campaigns resumable."""
    store = CampaignStore(tmp_path / STORE_FILENAME)
    campaign_id = store.create_campaign(name="c", mode="search", config_dir=str(tmp_path),
                                        config={})
    batch_id = store.open_batch(campaign_id, 0, ".")      # no `asked`
    for i in range(2):
        store.record_unit(batch_id=batch_id, paramset_id=f"p{i}", config_name=f"c{i}",
                          params={"x": i}, objectives={"f": 1.0}, measures={},
                          n_samples=1, status="evaluated", result_dir=f"c{i}")

    batches = recorded_batches(store, campaign_id)
    assert [b.asked for b in batches] == [2]
