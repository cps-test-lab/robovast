"""``read_batch_objectives``: the per-batch objective trajectory behind the campaign card's
live chart and ``get_campaign_status``'s ``objective_history``.

The cases worth pinning are the ones where a wrong answer looks plausible: a batch that
measured nothing must read as a gap rather than as a zero, a minimizing search's "best" is
the minimum, and a campaign with no scalar objective must say *why* it has no trajectory
instead of returning an empty one.
"""
import pytest

from robovast.common.store import STORE_FILENAME, CampaignStore, read_batch_objectives


def _campaign(tmp_path, *, mode="search", objectives=None, batches=()):
    """A store on disk with *batches* = ((status, objective), ...) per batch."""
    objectives = [{"name": "failure_rate", "direction": "maximize"}] \
        if objectives is None else objectives
    config = {"search": {"objectives": objectives}} if objectives else {}
    store = CampaignStore(tmp_path / STORE_FILENAME)
    cid = store.create_campaign(name="c", config=config, mode=mode, config_dir=".")
    for idx, units in enumerate(batches):
        bid = store.open_batch(cid, idx, ".")
        for n, (status, value) in enumerate(units):
            store.record_unit(
                batch_id=bid, paramset_id=f"p{idx}{n}", config_name=f"c{idx}{n}",
                params={}, objectives={} if value is None else {objectives[0]["name"]: value},
                measures={}, status=status, result_dir=f"c{idx}{n}", n_samples=1)
    store.close()
    return tmp_path


def test_aggregates_one_row_per_batch_with_a_rising_best(tmp_path):
    root = _campaign(tmp_path, batches=[
        [("evaluated", 0.2), ("evaluated", 0.6)],
        [("evaluated", 0.5), ("evaluated", 0.9)],
    ])
    got = read_batch_objectives(root)
    assert got["objective_name"] == "failure_rate"
    assert got["unavailable"] is None
    assert [b["idx"] for b in got["batches"]] == [0, 1]
    assert got["batches"][0] == {"idx": 0, "n_units": 2, "n_scored": 2, "min": 0.2,
                                 "max": 0.6, "mean": pytest.approx(0.4), "best_so_far": 0.6}
    assert got["batches"][1]["best_so_far"] == 0.9


def test_unmeasured_units_are_excluded_rather_than_scored_as_zero(tmp_path):
    """A cell that ran but produced nothing is a coverage loss, not a bad result.

    Scoring it would drag the mean toward zero and invent a `min` of 0.0 -- a plausible
    chart of a fact that never happened.
    """
    root = _campaign(tmp_path, batches=[
        [("evaluated", 0.8), ("no_sample", None), ("composition_failed", None)],
    ])
    batch = read_batch_objectives(root)["batches"][0]
    # Both counts, and they must DIFFER: `n_units` is every cell the batch had, `n_scored` only
    # the ones that yielded the objective. An earlier cut filtered inside the JOIN, which also
    # dropped the unmeasured cells from `n_units` -- so the two were always equal and "1 of 3
    # measured nothing" was unreportable. The statistics still come only from the evaluated one.
    assert (batch["n_units"], batch["n_scored"]) == (3, 1)
    assert (batch["min"], batch["max"], batch["mean"]) == (0.8, 0.8, 0.8)


def test_a_batch_that_scored_nothing_is_a_gap_and_best_carries_forward(tmp_path):
    root = _campaign(tmp_path, batches=[
        [("evaluated", 0.7)],
        [("no_sample", None)],
        [("evaluated", 0.4)],
    ])
    b = read_batch_objectives(root)["batches"]
    assert (b[1]["n_units"], b[1]["n_scored"]) == (1, 0), "the cell existed; it measured nothing"
    assert b[1]["min"] is b[1]["max"] is b[1]["mean"] is None, "a gap, not a zero"
    assert b[1]["best_so_far"] == 0.7, "the best found so far survives a scoreless round"
    assert b[2]["best_so_far"] == 0.7, "and a worse round does not replace it"


def test_minimize_takes_the_minimum_as_best(tmp_path):
    """The bug this pins: the web UI's old per-batch summary hardcoded `maximize`, so a
    minimizing campaign reported its WORST value as its best."""
    root = _campaign(tmp_path,
                     objectives=[{"name": "settling_time", "direction": "minimize"}],
                     batches=[[("evaluated", 5.0), ("evaluated", 2.0)],
                              [("evaluated", 9.0)]])
    got = read_batch_objectives(root)
    assert got["direction"] == "minimize"
    assert got["batches"][0]["best_so_far"] == 2.0
    assert got["batches"][1]["best_so_far"] == 2.0


def test_multi_objective_says_why_rather_than_returning_an_empty_trajectory(tmp_path):
    """`unit.objective` is NULL with more than one objective, so an empty list here would
    read as "the search found nothing" rather than "there is no scalar to trend"."""
    root = _campaign(tmp_path, objectives=[{"name": "a", "direction": "maximize"},
                                           {"name": "b", "direction": "minimize"}],
                     batches=[[("evaluated", 0.5)]])
    got = read_batch_objectives(root)
    assert got["unavailable"] == "multi_objective"
    assert got["batches"] == []


def test_batch_mode_campaign_has_no_trajectory(tmp_path):
    root = _campaign(tmp_path, mode="batch", batches=[[("passed", None)]])
    assert read_batch_objectives(root)["unavailable"] == "batch_mode"


def test_absent_store_is_none_not_an_error(tmp_path):
    assert read_batch_objectives(tmp_path) is None
