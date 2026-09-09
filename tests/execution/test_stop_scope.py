# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""A stop names the unit of work it lands on, and the scopes stay independent.

One flag for every stoppable thing could not say which was meant, and the cost of that was
silent: a stop aimed at the runs also cancelled the analysis of the batches that had already
finished, so their results sat complete on disk and reached no query surface.
"""

import pytest

from robovast.execution.control_server import (STOP_POSTPROCESSING, STOP_RUNS, STOP_SCOPES,
                                               STOP_SHARE, ControllerState, Phase,
                                               stop_checker, stop_scope_for_phase)


def test_stopping_the_runs_leaves_the_analysis_wanted():
    """The whole point of the split, stated as one assertion.

    ``stop_checker`` is the only thing the postprocessing pipeline sees, so a run-scoped
    stop leaving its predicate false is what lets the finished batches be indexed.
    """
    state = ControllerState()
    state.request_stop(STOP_RUNS)

    assert state.stop_requested is True
    assert state.postprocessing_stop_requested is False
    assert stop_checker(state)() is False


def test_stopping_the_analysis_does_not_say_the_campaign_was_stopped():
    """The converse: cancelling postprocessing must not read as a stopped campaign.

    ``stop_requested`` is what the run loop, the job waits and ``_finish_campaign`` read.
    """
    state = ControllerState()
    state.request_stop(STOP_POSTPROCESSING)

    assert state.postprocessing_stop_requested is True
    assert stop_checker(state)() is True
    assert state.stop_requested is False
    assert state.share_stop_requested is False


def test_stopping_the_upload_touches_neither_of_the_others():
    state = ControllerState()
    state.request_stop(STOP_SHARE)

    assert state.share_stop_requested is True
    assert state.stop_requested is False
    assert state.postprocessing_stop_requested is False


def test_request_stop_defaults_to_the_runs():
    """A caller that does not say what it is stopping means the runs -- what a stop meant
    when there was only one of them."""
    state = ControllerState()
    state.request_stop()
    assert state.stop_requested is True


def test_an_unknown_scope_is_refused_rather_than_ignored():
    """Never ignore an argument: a typo'd scope that set nothing would look like a stop
    that was honoured and cancel nothing at all."""
    state = ControllerState()
    with pytest.raises(ValueError, match="unknown stop scope"):
        state.request_stop("postprocessing_")
    assert not any(state.stop_requested_for(s) for s in STOP_SCOPES)


@pytest.mark.parametrize("phase", [
    Phase.INITIALIZING, Phase.BUILDING, Phase.STARTING, Phase.PLUGIN_INSTALL,
    Phase.VARIATION, Phase.RUNNING,
])
def test_the_run_phases_stop_the_runs(phase):
    assert stop_scope_for_phase(phase) == STOP_RUNS


@pytest.mark.parametrize("phase", [Phase.FINISHING, Phase.IMPORTING, Phase.POSTPROCESSING])
def test_the_post_run_phases_stop_the_analysis(phase):
    """``finishing`` and ``importing`` matter as much as ``postprocessing`` here.

    They sit between the run loop ending and postprocessing starting, so testing the phase
    for equality with ``postprocessing`` left a window in which a stop took the run scope,
    cancelled nothing -- the runs were already over -- and still answered "stop requested".
    """
    assert stop_scope_for_phase(phase) == STOP_POSTPROCESSING


def test_sharing_stops_the_upload():
    assert stop_scope_for_phase(Phase.SHARING) == STOP_SHARE


@pytest.mark.parametrize("phase", [
    Phase.FINISHED, Phase.FAILED, Phase.STOPPED, Phase.CRASHED, Phase.UNKNOWN,
])
def test_an_ended_campaign_has_nothing_to_stop(phase):
    """``None`` so the caller can refuse. Setting a flag nothing will read and answering
    "stop requested" sends the reader looking for an effect that never came."""
    assert stop_scope_for_phase(phase) is None


def test_every_phase_is_classified():
    """No phase falls through unclassified, now or when one is added.

    A live phase added later must land on a scope rather than silently becoming
    unstoppable, which is why the run set is derived from ``RUNNING_PHASES`` rather than
    listed out.
    """
    for phase in Phase:
        scope = stop_scope_for_phase(phase)
        if phase in {Phase.FINISHED, Phase.FAILED, Phase.STOPPED, Phase.CRASHED,
                     Phase.UNKNOWN}:
            assert scope is None, f"{phase} is terminal but claims scope {scope}"
        else:
            assert scope in STOP_SCOPES, f"{phase} is live but classified {scope!r}"


def test_stop_checker_is_none_without_a_state():
    """The re-run entry points postprocess a campaign nothing is driving."""
    assert stop_checker(None) is None
