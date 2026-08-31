# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The early-stop verdict: is a stopping criterion about to end this search?

Beside ``stall_report`` in the status contract, and tested to the same standard, because it is
the same kind of thing: a tri-state verdict derived once and rendered by three surfaces. The
card used to derive the stall verdict itself and drifted from the contract three times; this one
starts on the right side of that.
"""

from robovast.client.status import (BudgetItem, Status, status_response,
                                    stopping_soon_report)


def _stale(current, limit=3):
    return BudgetItem(label="stale_batches", current=current, limit=limit,
                      kind="no_improvement", op=">=")


def _search(**kw):
    kw.setdefault("phase", "running")
    return Status(mode="search", **kw)


# -- the verdict -------------------------------------------------------------


def test_one_round_out_is_firing_soon():
    r = stopping_soon_report(_search(budget=[_stale(2)]))
    assert r["stopping_soon"] is True
    assert "2 of 3 rounds" in r["stopping_reason"]
    # Singular, because "1 more rounds" is the kind of thing a reader notices instead of the
    # fact it is telling them.
    assert "1 more round without" in r["stopping_reason"]


def test_already_at_the_patience_is_firing_soon():
    """The criterion fires at ``>=``, so at 3 of 3 the next evaluation ends the search. Reporting
    False here would go quiet at the one moment the answer matters most."""
    assert stopping_soon_report(_search(budget=[_stale(3)]))["stopping_soon"] is True


def test_further_out_is_not_firing_soon():
    r = stopping_soon_report(_search(budget=[_stale(0)]))
    assert r["stopping_soon"] is False
    # No reason when it is not firing: the row exists to be acted on, and there is nothing to do.
    assert "stopping_reason" not in r


def test_the_closest_criterion_wins_when_several_are_declared():
    """The search stops at whichever fires first — the same rule the ring applies to budgets."""
    r = stopping_soon_report(_search(budget=[_stale(1, 9), _stale(4, 5)]))
    assert r["stopping_soon"] is True and "4 of 5" in r["stopping_reason"]


# -- the tri-state, which is the whole point --------------------------------


def test_no_measurable_criterion_yields_no_verdict_not_false():
    """``False`` would assert this search will spend its whole budget, when nothing was checked.
    That is the same error as calling an un-budgeted run healthy."""
    r = stopping_soon_report(_search(budget=[
        BudgetItem(label="runs", current=120, limit=180, kind="runs", op=">=")]))
    assert r["stopping_soon"] is None
    assert "declares no search.stopping criterion" in r["stopping_verdict"]


def test_an_unmeasurable_criterion_is_told_apart_from_none_at_all():
    """A search bounded by target_objective HAS a convergence criterion, so telling its owner to
    add one would be wrong. The two messages separate a fixable omission from an inherent limit:
    those criteria fire on a value that can move any distance in one round, so there is no
    distance to measure."""
    r = stopping_soon_report(_search(budget=[
        BudgetItem(label="robustness", current=-1.42, limit=-2.0,
                   kind="target_objective", op="<=")]))
    assert r["stopping_soon"] is None
    assert "any distance in one round" in r["stopping_verdict"]
    assert "Add a `no_improvement`" not in r["stopping_verdict"]


def test_a_criterion_with_no_position_yet_yields_no_verdict():
    """NaN reaches the wire as null (controller._budget_item). Treating it as 0 would report a
    fresh search as comfortably far from converging, which it has not yet measured."""
    assert stopping_soon_report(
        _search(budget=[_stale(None)]))["stopping_soon"] is None


# -- who gets no verdict at all ---------------------------------------------


def test_a_terminal_campaign_gets_nothing():
    """It has already stopped; why is in ``stop``. A verdict here would be an accusation about
    a campaign that is over — the same reason the stall verdict withdraws."""
    assert stopping_soon_report(
        Status(mode="search", phase="finished", budget=[_stale(2)])) == {}


def test_a_batch_campaign_gets_nothing():
    """No search, no rounds, nothing to converge."""
    assert stopping_soon_report(Status(mode="batch", phase="running")) == {}


# -- the wire ---------------------------------------------------------------


def test_the_verdict_is_served_but_never_stored():
    """On StatusResponse, not Status: Status is persisted verbatim as the campaign's durable
    outcome, and a stored verdict is read back later as a live claim about a finished campaign."""
    st = _search(progress_since=0, budget=[_stale(2)])
    served = status_response(st)
    assert served.stopping_soon is True and served.stopping_reason
    assert not hasattr(st, "stopping_soon")


def test_the_verdict_never_announces_a_search_that_has_not_stopped():
    """End to end over the seam: the rows this reads come from ``StopConditions.progress``.

    A stale count measured on a different comparison from the one ``should_stop`` uses arrives
    here as a verdict about a search that is not going to stop. With a min_delta the search
    gains less than each round and more than across the window, the row reached its patience
    and stayed there -- so the reader was told the search had converged, and then told that
    "0 more rounds without an improvement" would end it, for as long as it kept improving.
    """
    from robovast.common.config import NoImprovementStop
    from robovast.execution.controller import CampaignController
    from robovast.search.stopping import StopConditions, StopSnapshot

    stop = StopConditions([], [NoImprovementStop(type='no_improvement', patience=3,
                                                 min_delta=0.1)],
                          'margin', 'maximize')
    for i, best in enumerate([0.0, 0.05, 0.10, 0.15], start=1):
        snap = StopSnapshot(batch=i, elapsed=1.0, best_objective=best)
        fired = stop.should_stop(snap) is not None
        rows = [CampaignController._budget_item(p) for p in stop.progress(snap)]
        stale = next(r for r in rows if r["kind"] == "no_improvement")
        assert stale["done"] is fired, f"batch {i}: the row and the stop must agree"
        verdict = stopping_soon_report(_search(budget=[BudgetItem(**r) for r in rows]))
        assert "0 more round" not in verdict.get("stopping_reason", "")
