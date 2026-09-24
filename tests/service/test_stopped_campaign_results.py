# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""A stopped campaign keeps the analysis of the batches that finished.

Those batches are complete on disk the moment the runs end -- ``campaign.db`` carries a
``unit`` row per cell with its params and objective, and ``run_view``/``config_view`` are
views over the mirror of exactly those tables. Only the index ingest was missing, because a
stop of the runs also cancelled the postprocessing that performs it. Stopping a search after
a few batches is a normal way to end one; what it measured has to stay queryable.
"""

# pylint: disable=protected-access  # the worker's stopped path cannot be staged publicly

import types
from pathlib import Path

import pytest
import yaml

from robovast.execution.backends import CampaignStopped
from robovast.execution.control_server import STOP_RUNS, Phase
from robovast.service.interface import (CreateCampaignRequest, CreateWorkspaceRequest,
                                        WriteFileRequest)
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore
from tests.service.null_service import NullService


@pytest.fixture(name="svc")
def _svc(tmp_path):
    store = WorkspaceStore(registry=WorkspaceRegistry(root=str(tmp_path / "ws")))
    transport = NullService(store=store)
    results = tmp_path / "results"
    results.mkdir()
    transport._campaigns_root = lambda: results
    return transport


def _launch(svc, monkeypatch, *, stopped=True, postprocess=True, request_stop=False,
            raises=None, stop_while_staging=False, ran=True):
    """Run a campaign whose loop raises ``CampaignStopped``, through the real worker.

    Driven end to end rather than restated: a test that re-implements the branch asserts
    its own copy, and keeps passing while the path it stands for regresses.

    *ran* leaves a run directory behind, as a batch that got as far as one does: what a
    stopped campaign is owed analysis for is what it has on disk.

    Returns ``(ends_at_of_each_postprocess, final_status)``.
    """
    ws = svc.create_workspace(CreateWorkspaceRequest(name="ws1"))
    svc.write_file(WriteFileRequest(
        address=f"/sources/{ws.workspace_id}/pilot.vast",
        content=yaml.safe_dump({
            "version": 4,
            "metadata": {"name": "pilot"},
            "configuration": [{"name": "config1"}],
            "execution": {"scenario_file": "scenario.osc", "runs": 1,
                          "containers": {"scenario": {"image": "base:1"}}},
        })))
    svc.write_file(WriteFileRequest(
        address=f"/sources/{ws.workspace_id}/scenario.osc",
        content="scenario pilot:\n"))

    def run(*a, **k):
        if ran:
            (Path(a[2]) / k["campaign_id"] / "config1" / "0").mkdir(parents=True)
        if raises is not None:
            # A stop whose first visible effect is something else failing: the campaign's
            # own container or worker was killed under the step it was in, and the
            # exception describes that consequence rather than the stop.
            k["state"].request_stop(STOP_RUNS)
            raise raises
        if request_stop:
            # A stop seen at a batch boundary: the loop ends without raising, leaving only
            # the flag behind -- which is exactly what makes this case easy to miss.
            k["state"].request_stop(STOP_RUNS)
        if stopped:
            # The controller sets the phase before it raises, and the worker's tail must
            # leave that standing -- so the double does too.
            k["state"].set_phase(Phase.STOPPED)
            raise CampaignStopped("stopped by request")

    monkeypatch.setattr("robovast.execution.controller.run_batch_campaign", run)

    def _specs(self, *a, **k):
        if stop_while_staging:
            # A stop that arrives before the campaign has any runs. The driver reads it at
            # the next boundary and raises there itself, so nothing else has published a
            # phase by the time the outcome is written.
            for entry in svc._campaigns.values():
                entry.state.request_stop(STOP_RUNS)
        return {}, None

    monkeypatch.setattr(NullService, "_build_specs_for", _specs)
    monkeypatch.setattr(NullService, "_build_backend", lambda self, state: None)

    done = []
    monkeypatch.setattr(
        NullService, "_postprocess",
        lambda self, cid, rd, state, entry, ends_at=Phase.FINISHED: done.append(ends_at))

    ref = svc.create_campaign(CreateCampaignRequest(
        workspace_id=ws.workspace_id, config_path="pilot.vast",
        postprocess=postprocess, description="stop test",
        # A pinned public tag rather than a built image: this test is about the worker's
        # stopped path, and a build would be the slowest thing in it.
        allow_opaque_image=True))
    for entry in list(svc._campaigns.values()):
        if entry.thread:
            entry.thread.join(10)
    return done, svc.get_status(ref.campaign_id)


def test_a_stopped_campaign_still_postprocesses_its_finished_batches(svc, monkeypatch):
    """The claim: the ingest happens, so what the campaign did measure reaches the index
    instead of sitting unreadable on disk."""
    done, _ = _launch(svc, monkeypatch)
    assert done == [Phase.STOPPED]


def test_it_ends_back_in_stopped_not_finished(svc, monkeypatch):
    """How the campaign ended is not the analysis step's to restate.

    ``status_recovery.record_step_outcome`` applies the same rule on the re-run path, and a
    campaign reporting ``finished`` until the next service restart said ``stopped`` would be
    one fact with two answers.
    """
    done, status = _launch(svc, monkeypatch)
    assert done == [Phase.STOPPED]
    assert status.phase == Phase.STOPPED


def test_a_campaign_that_asked_for_no_postprocessing_does_not_get_it(svc, monkeypatch):
    done, _ = _launch(svc, monkeypatch, postprocess=False)
    assert done == []


def test_a_shutting_down_service_does_not_start_it(svc, monkeypatch):
    """On Ctrl+C the storage tunnel dies with the process group -- the line
    ``_record_campaign_stopped`` already draws -- and work this process cannot finish is
    not worth starting."""
    monkeypatch.setattr(NullService, "_record_campaign_stopped",
                        lambda self, *a, **k: setattr(self, "_shutting_down", True))
    done, _ = _launch(svc, monkeypatch)
    assert done == []


def test_a_stop_between_batches_is_postprocessed_when_the_controller_chains_it(svc, monkeypatch):
    """A search stopped at a batch boundary returns cleanly -- and must not fall through.

    The loop treats such a stop as an ordinary stopping criterion (``stop_kind="external"``)
    and ends without raising, so the ``CampaignStopped`` path never sees it. Where the
    controller chains the analysis, that chain skips itself whenever a stop was requested,
    so the service postprocesses the batches the search completed.

    It ends ``finished``, which is not this branch's choice: by the loop's own account the
    campaign finished.
    """
    done, _ = _launch(svc, monkeypatch, stopped=False, request_stop=True)
    assert done == [Phase.FINISHED]


def test_the_worker_does_not_double_postprocess_an_ordinary_campaign(svc, monkeypatch):
    """The counterpart: with no stop, the controller's chain owns it and the service must
    keep its hands off, or every campaign would postprocess twice."""
    done, _ = _launch(svc, monkeypatch, stopped=False)
    assert done == []


def test_shutdown_marks_the_service_before_it_tears_anything_down(tmp_path, monkeypatch):
    """The flag has to be set first, or a worker reaching its tail during the teardown
    starts the very work the flag exists to prevent."""
    lt = NullService(workspace_dir=str(tmp_path), results_dir=str(tmp_path / "r"))
    seen = []
    monkeypatch.setattr(type(lt), "_shutdown_running_campaigns", lambda self, running: None)
    monkeypatch.setattr(type(lt), "_is_done",
                        lambda self, e: seen.append(lt._shutting_down))

    assert lt._shutting_down is False
    lt._campaigns["c1"] = types.SimpleNamespace(
        campaign_id="c1", state=types.SimpleNamespace(
            snapshot=lambda: types.SimpleNamespace(phase=Phase.RUNNING)), thread=None)
    lt.shutdown()

    assert seen and all(seen), "campaigns were inspected before the flag was set"
    assert lt._shutting_down is True


class _PhaseState:
    """Records the phases ``_postprocess`` moves through."""

    def __init__(self, phase=Phase.RUNNING):
        self.phase = phase
        self.stages = []

    def set_phase(self, phase, stage=None):
        self.phase = phase
        self.stages.append(phase)

    def update(self, **fields):
        pass

    def snapshot(self):
        return types.SimpleNamespace(phase=self.phase)

    @property
    def postprocessing_stop_requested(self):
        return False


def _ran_postprocessing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "robovast.results_processing.postprocessing.run_postprocessing",
        lambda **kw: (True, "done"))
    monkeypatch.setattr(
        "robovast.results_processing.postprocessing.campaign_defines_postprocessing",
        lambda _dir: True)
    (tmp_path / "c1" / "_execution").mkdir(parents=True)


def test_postprocess_ends_in_the_phase_it_was_given(svc, tmp_path, monkeypatch):
    """``_postprocess`` no longer hardcodes ``finished``: a stopped campaign's analysis runs
    to completion and the campaign is still ``stopped`` afterwards."""
    _ran_postprocessing(monkeypatch, tmp_path)
    state = _PhaseState()
    svc._record_outcome = lambda *a, **k: None

    svc._postprocess("c1", str(tmp_path), state, None, ends_at=Phase.STOPPED)

    assert state.phase == Phase.STOPPED
    assert Phase.POSTPROCESSING in state.stages


def test_postprocess_defaults_to_finished(svc, tmp_path, monkeypatch):
    """The ordinary path is unchanged: a campaign whose runs finished ends ``finished``."""
    _ran_postprocessing(monkeypatch, tmp_path)
    state = _PhaseState()
    svc._record_outcome = lambda *a, **k: None

    svc._postprocess("c1", str(tmp_path), state, None)

    assert state.phase == Phase.FINISHED


def test_a_stop_that_surfaces_as_a_failure_is_still_a_stop(svc, monkeypatch):
    """The cause is the flag, not the exception.

    A stop kills what the campaign was in the middle of -- the composition worker, an
    auxiliary container, the connection they were using -- and whichever of those notices
    first raises its own error. Read as a failure, that files the operator's own request
    under faults, hides the analysis the finished batches are owed behind a failed
    campaign, and sends whoever reads it after a bug in a step that was working.
    """
    done, status = _launch(svc, monkeypatch, stopped=False,
                           raises=RuntimeError("worker exited with -15"))

    assert status.phase == Phase.STOPPED
    assert not status.error, "a stop must not be recorded as the campaign's failure"
    assert done == [Phase.STOPPED]


def test_a_campaign_stopped_before_it_ran_still_ends_stopped(svc, monkeypatch):
    """The driver raises this one itself, so nothing else has published a phase.

    A stop asked for while a campaign is staging, installing its plugins or waiting for
    its image is answered where it lands -- and the durable outcome is written from the
    phase, so a campaign left saying ``starting`` would reconstruct after a restart as one
    that never ended.
    """
    done, status = _launch(svc, monkeypatch, stopped=False, stop_while_staging=True)

    assert status.phase == Phase.STOPPED
    assert done == [], "nothing ran, so there is nothing to postprocess"


def test_a_campaign_stopped_before_any_run_gets_no_postprocessing(svc, monkeypatch):
    """The batch began but the stop landed before any run existed -- jobs still queued.

    The owed pass reads run directories and nothing else, so it would derive nothing; on the
    cluster it is a Job of its own, a pod scheduled to read an empty campaign. The
    campaign still ends ``stopped`` and says so.
    """
    done, status = _launch(svc, monkeypatch, ran=False)

    assert status.phase == Phase.STOPPED
    assert done == []
    assert not status.postprocessing_error, "skipping a pass with nothing to read is not a failure"
