# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Ctrl+C on ``vast serve`` tears down the in-flight local campaign.

Local campaigns run on daemon worker threads, so a bare process exit would kill
the worker mid-run and orphan its ``docker compose`` containers. The service's
lifespan calls :meth:`LocalTransport.shutdown`, which requests a cooperative
stop of every still-running campaign and joins the workers.
"""

# pylint: disable=protected-access  # the tests seed _campaigns directly: the point is to
# exercise shutdown against a campaign already in flight, which no public API can stage.

import threading
from unittest import mock

from robovast.execution.control_server import (STOP_POSTPROCESSING, STOP_RUNS,
                                               STOP_SHARE)
from robovast.service.client import LocalTransport, _LocalCampaign


class _State:
    """Minimal stand-in for ControllerState — tracks stop + a phase snapshot.

    ``_is_done`` reads ``snapshot().phase`` (a not-yet-started campaign registered
    before its thread exists is still live), so the fake must carry a phase."""

    def __init__(self, phase="running"):
        # ``stopped`` is the RUN scope, which is what the worker doubles here wait on.
        # ``stopped_scopes`` records every scope asked for, because what a stop lands on
        # now depends on what is running and a single flag cannot tell the cases apart.
        self.stopped = threading.Event()
        self.stopped_scopes = set()
        self.phase = phase

    def request_stop(self, scope=STOP_RUNS):
        self.stopped_scopes.add(scope)
        if scope == STOP_RUNS:
            self.stopped.set()

    def snapshot(self):
        from types import SimpleNamespace
        return SimpleNamespace(phase=self.phase)


def _transport() -> LocalTransport:
    lt = LocalTransport.__new__(LocalTransport)
    lt._campaigns = {}
    lt._lock = threading.Lock()
    # __init__ is bypassed, so every attribute shutdown() touches has to be seeded here.
    # Shutdown reaps held exec containers before it looks at campaigns: they are what
    # nothing else reaps, and a service holding one with no campaign running would
    # otherwise return with it still up.
    lt._exec_mgr = None
    return lt


def _add_running(lt: LocalTransport, cid: str) -> _LocalCampaign:
    """Register a campaign whose worker loops until its state is stopped."""
    state = _State()
    entry = _LocalCampaign(cid, "/tmp", state)

    def _work():
        state.stopped.wait(timeout=5)

    entry.thread = threading.Thread(target=_work, name=cid)
    entry.thread.start()
    lt._campaigns[cid] = entry
    return entry


def test_shutdown_stops_running_campaign_and_kills_container():
    lt = _transport()
    entry = _add_running(lt, "campaign-live")

    with mock.patch("subprocess.run") as run:
        lt.shutdown()

    assert entry.state.stopped.is_set()          # cooperative stop requested
    assert not entry.thread.is_alive()           # worker joined before we returned
    run.assert_called_once()                     # scenario container force-removed
    assert run.call_args[0][0] == ["docker", "rm", "-f", "robovast"]


def test_shutdown_skips_finished_campaigns():
    """A campaign whose worker already exited is not touched (no docker call)."""
    lt = _transport()
    # A finished campaign reads terminal (its worker recorded a terminal phase);
    # _is_done -> True regardless of the thread handle, so shutdown skips it.
    done = _LocalCampaign("campaign-done", "/tmp", _State(phase="finished"))
    done.thread = None
    lt._campaigns["campaign-done"] = done

    with mock.patch("subprocess.run") as run:
        lt.shutdown()

    assert not done.state.stopped.is_set()
    run.assert_not_called()


def test_shutdown_is_noop_without_campaigns():
    lt = _transport()
    with mock.patch("subprocess.run") as run:
        lt.shutdown()  # must not raise
    run.assert_not_called()


def test_web_ui_stop_route_requests_stop_and_kills_container():
    """The web UI's Stop button (POST /campaigns/{id}/stop) drives impl.stop().

    Exercises the exact HTTP path the browser hits, asserting it reaches the
    backend's cooperative-stop mechanism (request_stop + scenario container kill).
    """
    from fastapi.testclient import TestClient

    from robovast.service.app import build_app

    lt = _transport()
    entry = _add_running(lt, "campaign-ui")

    with mock.patch("subprocess.run") as run:
        with TestClient(build_app(lt)) as client:
            resp = client.post("/campaigns/campaign-ui/stop")

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert entry.state.stopped.is_set()
    # Count the *docker* calls, not every subprocess the patch happens to see. Unlike the
    # shutdown() tests above, this one wraps a whole application build, so anything
    # constructing the app is inside the window -- and an unrelated import-time
    # subprocess once made this fail, but only when nothing had imported that module
    # earlier in the process. The claim here is "the container is removed exactly once",
    # which is what this measures and a total call count does not.
    removals = [c for c in run.call_args_list
                if c.args and c.args[0][:1] == ["docker"]]
    assert removals == [mock.call(["docker", "rm", "-f", "robovast"],
                                  check=False, capture_output=True)], run.call_args_list
    entry.state.stopped.set()  # let the fake worker exit


def test_web_ui_stop_unknown_campaign_reports_not_tracked():
    """Stopping an unknown campaign is a clean ok=False, not a 500."""
    from fastapi.testclient import TestClient

    from robovast.service.app import build_app

    lt = _transport()
    with TestClient(build_app(lt)) as client:
        resp = client.post("/campaigns/nope/stop")

    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_stop_during_postprocessing_says_what_it_leaves():
    """A stop lands differently once the runs are over, and the reply has to say so.

    The pipeline polls its own scope, so the step in flight is torn down rather than run to
    the end — but the runs are over and every result they produced is kept. What the stop
    gives up is the derived data, which nothing but a re-run brings back, and an operator
    told only "stop requested" would have no reason to ask for one.

    It must NOT name the phase the campaign ends in: that depends on how the *runs* ended,
    and a campaign whose runs were stopped first ends ``stopped``, not ``finished``.
    """
    lt = _transport()
    entry = _add_running(lt, "campaign-pp")
    entry.state.phase = "postprocessing"

    with mock.patch("subprocess.run"):
        res = lt.stop("campaign-pp")

    assert res.ok
    # The postprocessing scope, and *only* it: the runs are already over, and flagging them
    # is what used to discard the analysis of the batches that had finished.
    assert entry.state.stopped_scopes == {STOP_POSTPROCESSING}
    assert "postprocessing" in res.message and "re-run postprocessing" in res.message
    assert "finished" not in res.message


def test_stop_during_sharing_cancels_the_upload():
    """An upload is a long-running thing of its own, so a stop reaches it and says so.

    Its own scope, not the runs': the campaign is over by then, and the partial object the
    cancellation leaves is what the reply is about.
    """
    lt = _transport()
    entry = _add_running(lt, "campaign-share")
    entry.state.phase = "sharing"

    with mock.patch("subprocess.run"):
        res = lt.stop("campaign-share")

    assert res.ok
    assert entry.state.stopped_scopes == {STOP_SHARE}
    assert "upload" in res.message


def test_stop_on_an_ended_campaign_is_refused():
    """Nothing is running, so nothing is stopped — and the reply must not claim otherwise.

    A cheerful "stop requested" over a campaign that is already over sends the reader
    looking for an effect that never came.
    """
    lt = _transport()
    entry = _add_running(lt, "campaign-done")
    entry.state.phase = "finished"

    with mock.patch("subprocess.run"):
        res = lt.stop("campaign-done")

    assert res.ok is False
    assert "already over" in res.message and "finished" in res.message
    assert entry.state.stopped_scopes == set()


def test_stop_while_running_keeps_its_own_wording():
    """The postprocessing sentence must not turn up on a campaign that is still running —
    there it would promise results that the stopped runs will not produce."""
    lt = _transport()
    entry = _add_running(lt, "campaign-run")

    with mock.patch("subprocess.run"):
        res = lt.stop("campaign-run")

    assert res.ok and res.message == "stop requested"
    assert entry.state.stopped_scopes == {STOP_RUNS}
    entry.state.stopped.set()  # let the fake worker exit


def test_stop_between_the_runs_and_postprocessing_cancels_the_analysis():
    """``finishing`` is past the runs, so a stop there is a stop of what is left.

    The window is real — the controller sets ``finishing`` and the worker only then enters
    ``postprocessing`` — and testing the phase for equality with ``postprocessing`` left it
    uncovered: the stop took the run scope, cancelled nothing (the runs were already over)
    and still answered "stop requested".
    """
    lt = _transport()
    entry = _add_running(lt, "campaign-finishing")
    entry.state.phase = "finishing"

    with mock.patch("subprocess.run"):
        res = lt.stop("campaign-finishing")

    assert res.ok
    assert entry.state.stopped_scopes == {STOP_POSTPROCESSING}
