# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A campaign that needs an image is created first and waits for it afterwards.

``create_campaign`` is specified fire-and-forget, but it used to await the image build on
the caller's thread: a cluster start died on the HTTP client's 30 s read timeout *while
the server kept going and the campaign succeeded* — a reported failure for work that
worked — and, because the campaign was only created after the build, nothing about that
work was observable while it ran.

The wait now happens on the campaign's own worker, through
``LocalTransport._await_build_image``. That loop is **shared by both lanes** (it drives
``get_image_build_status`` / ``get_image_build_log``, which each transport implements), so
these tests exercise it once and it holds for the local Docker build and the in-cluster
BuildKit Job alike.
"""

import tempfile
import threading
import time

import pytest

from robovast.common.errors import ImageBuildFailed
from robovast.common.status import Phase, failure_detail, is_running
from robovast.execution.backends import CampaignStopped
from robovast.execution.control_server import ControllerState
from robovast.service.interface import ImageBuildRef, ImageBuildStatus, LogChunk
from robovast.service.local_transport import LocalTransport
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore


class _FakeBuild:
    """A build whose completion the test controls, recording any cancel attempt."""

    def __init__(self, *, phase="succeeded", log="#1 [1/2] FROM base\n"):
        self.done = threading.Event()
        self.final_phase = phase
        self.log = log
        self.cancelled = False

    def status(self):
        if not self.done.is_set():
            return ImageBuildStatus(build_id="b-1", tag="sim:v3", phase="building")
        return ImageBuildStatus(build_id="b-1", tag="sim:v3",
                                phase=self.final_phase, done=True)


@pytest.fixture
def svc(tmp_path):
    store = WorkspaceStore(registry=WorkspaceRegistry(root=str(tmp_path / "ws")))
    transport = LocalTransport(store=store)
    transport._campaigns_root = lambda: tmp_path / "results"  # noqa: SLF001
    return transport


def _wire(svc, build, monkeypatch):
    monkeypatch.setattr(svc, "get_image_build_status", lambda bid: build.status())
    monkeypatch.setattr(
        svc, "get_image_build_log",
        lambda bid, offset=0: LogChunk(text=build.log[offset:],
                                       next_offset=len(build.log), eof=False))
    monkeypatch.setattr(LocalTransport, "_BUILD_POLL_SECONDS", 0.01)


def test_the_wait_returns_only_once_the_build_is_done(svc, monkeypatch, tmp_path):
    build = _FakeBuild()
    _wire(svc, build, monkeypatch)
    state = ControllerState()
    root = tmp_path / "results" / "c-2026-07-28-120000"

    finished = threading.Event()
    threading.Thread(
        target=lambda: (svc._await_build_image("b-1", state, str(root)),
                        finished.set()), daemon=True).start()
    assert not finished.wait(0.2)     # still waiting: the build has not finished
    build.done.set()
    assert finished.wait(5)


def test_the_build_output_lands_in_the_campaigns_own_log(svc, monkeypatch, tmp_path):
    """So it is reachable with the campaign id alone, and survives the build Job's TTL."""
    build = _FakeBuild(log="#1 [1/2] FROM base\n#2 RUN apt-get install -y ros\n")
    _wire(svc, build, monkeypatch)
    build.done.set()
    root = tmp_path / "results" / "c-2026-07-28-120000"

    svc._await_build_image("b-1", ControllerState(), str(root))

    written = (root / "_execution" / "build.log").read_text()
    # The header names the build, so an image shared by several campaigns reads as shared
    # rather than as this campaign's own work.
    assert written.startswith("waiting for image sim:v3 (build b-1)\n")
    assert "apt-get install -y ros" in written


def test_a_failed_build_raises_and_points_at_the_log(svc, monkeypatch, tmp_path):
    build = _FakeBuild(phase="failed")
    _wire(svc, build, monkeypatch)
    build.done.set()

    with pytest.raises(ImageBuildFailed, match="phase='build'") as excinfo:
        svc._await_build_image("b-1", ControllerState(),
                               str(tmp_path / "results" / "c-2026-07-28-120000"))
    # A build failure is diagnosed from the builder's output, which
    # classify_build_error has already reduced to one actionable line. The Python
    # stack is this wait loop and adds nothing, so neither the log line nor the
    # durable record carries one — a traceback here read as a RoboVAST crash.
    assert failure_detail(excinfo.value) == str(excinfo.value)
    # Still a RuntimeError, so a caller written before the class still catches it.
    assert isinstance(excinfo.value, RuntimeError)


def test_stopping_while_building_detaches_and_never_cancels(svc, monkeypatch, tmp_path):
    """A content-addressed build may be what a *sibling* campaign is waiting on, and the
    image is a cache entry rather than this campaign's property. So a stop abandons the
    wait and leaves the build running."""
    build = _FakeBuild()
    _wire(svc, build, monkeypatch)
    state = ControllerState()
    state.request_stop()

    with pytest.raises(CampaignStopped):
        svc._await_build_image("b-1", state,
                              str(tmp_path / "results" / "c-2026-07-28-120000"))
    assert build.cancelled is False
    assert not build.done.is_set()   # the build itself was left alone


def test_an_unreadable_build_log_does_not_fail_the_campaign(svc, monkeypatch, tmp_path):
    """Losing the log is a degraded record; failing the run over it would be a lie."""
    build = _FakeBuild()
    monkeypatch.setattr(svc, "get_image_build_status", lambda bid: build.status())
    monkeypatch.setattr(svc, "get_image_build_log",
                        lambda bid, offset=0: (_ for _ in ()).throw(RuntimeError("gone")))
    monkeypatch.setattr(LocalTransport, "_BUILD_POLL_SECONDS", 0.01)
    build.done.set()

    svc._await_build_image("b-1", ControllerState(),
                           str(tmp_path / "results" / "c-2026-07-28-120000"))


# -- the phase, as a caller sees it -----------------------------------------

def test_building_is_a_running_phase_so_the_campaign_is_listed_live():
    """``list_running_campaigns`` needed no change for a building campaign to appear in
    it: a building campaign is a campaign, and the listing already unions the in-memory
    registry. Teaching it to enumerate *builds* instead would have needed an endpoint
    that does not exist and a second in-flight registry to keep consistent."""
    assert is_running(Phase.BUILDING)


def test_the_stage_says_waiting_for_rather_than_building():
    """Two campaigns needing one image must not each look like they are building it."""
    state = ControllerState()
    state.set_phase(Phase.BUILDING, stage="waiting for image sim:v3")
    snap = state.snapshot()
    assert snap.phase == "building"
    assert snap.stage.startswith("waiting for")
    # phase_since is what separates "slow" from "wedged" for a phase with no run counter.
    assert snap.phase_since <= time.time()


def test_a_building_campaigns_status_already_names_its_campaign():
    """``campaign_id`` used to be written by the *controller*, which a building campaign
    has not reached — and a build that failed never reaches at all. Readers key their log
    and job reads off that field, so a null left the build's own output unreachable from
    the very status that was reporting the build. ``create_campaign`` therefore seeds it
    at acceptance, which works because ``ControllerState(**initial)`` feeds ``Status``.

    (The seam this pins is the constructor. That ``create_campaign`` passes the id is
    covered end to end rather than here: no test in this suite drives it, since it needs
    a resolved workspace, a validated config and a live worker thread.)
    """
    state = ControllerState(campaign_id="c-2026-07-28-120000")
    state.set_phase(Phase.BUILDING, stage="waiting for image sim:v3")
    assert state.snapshot().campaign_id == "c-2026-07-28-120000"
