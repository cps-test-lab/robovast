# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The kept-alive-pod primitives shared by the aux pod and the container-exec lane.

Two subsystems run a pod and exec into it, and each had its own copy of this. The copies
were not equally correct — only the exec lane reported *why* a pod would not start, only
it waited for a delete to finish, and only it bounded an exec. These tests pin the three
properties that were missing on the other side, so sharing cannot regress back to the
weaker version.

No cluster is needed: the API surface used here is small enough to fake.
"""

import pytest

from robovast.common.kube import (exec_stream, pod_pending_reason, wait_pod_gone,
                                  wait_pod_ready)


class _State:
    def __init__(self, reason=None, message=""):
        self.waiting = None
        if reason is not None:
            self.waiting = type("W", (), {"reason": reason, "message": message})()


class _Status:
    def __init__(self, phase, init=None, main=None):
        self.phase = phase
        self.init_container_statuses = init
        self.container_statuses = main


class _Pod:
    def __init__(self, phase, init=None, main=None):
        self.status = _Status(phase, init, main)


def _container(reason=None, message=""):
    return type("C", (), {"state": _State(reason, message)})()


# -- pod_pending_reason -------------------------------------------------------


def test_a_pending_pod_names_the_reason_it_is_stuck():
    """The phase alone is just "Pending", which explains nothing.

    A bad image reference used to surface on the aux pod as five minutes of silence
    followed by "was not Running within 300s".
    """
    pod = _Pod("Pending", main=[_container("ImagePullBackOff", "manifest unknown")])
    assert pod_pending_reason(pod) == "ImagePullBackOff: manifest unknown"


def test_the_init_container_reason_wins():
    """It runs first, so when both are waiting it is the one that explains the other."""
    pod = _Pod("Pending",
               init=[_container("ErrImagePull", "sidecar")],
               main=[_container("PodInitializing")])
    assert pod_pending_reason(pod).startswith("ErrImagePull")


def test_a_healthy_pod_has_no_reason():
    assert pod_pending_reason(_Pod("Running", main=[_container()])) == ""


# -- wait_pod_ready -----------------------------------------------------------


class _Core:
    def __init__(self, pods, deletes_seen=None):
        self._pods = list(pods)
        self.deleted = deletes_seen if deletes_seen is not None else []

    def read_namespaced_pod(self, name, namespace):
        if not self._pods:
            raise AssertionError("read more times than the test scripted")
        item = self._pods.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_wait_pod_ready_returns_as_soon_as_it_is_running():
    wait_pod_ready(_Core([_Pod("Running")]), "ns", "p", timeout_s=5)


def test_wait_pod_ready_reports_the_pending_reason_on_timeout():
    """The whole point of sharing this: a timeout that names ImagePullBackOff."""
    pod = _Pod("Pending", main=[_container("ImagePullBackOff", "no such tag")])
    with pytest.raises(RuntimeError, match="ImagePullBackOff"):
        wait_pod_ready(_Core([pod] * 20), "ns", "p", timeout_s=0.01)


def test_wait_pod_ready_fails_fast_on_a_terminal_phase():
    """A Failed pod will never become Running; waiting out the timeout tells no one more."""
    with pytest.raises(RuntimeError, match="Failed"):
        wait_pod_ready(_Core([_Pod("Failed")]), "ns", "p", timeout_s=30)


# -- wait_pod_gone ------------------------------------------------------------


def _api_exception(status):
    from kubernetes.client.rest import ApiException
    return ApiException(status=status, reason="gone" if status == 404 else "boom")


def test_wait_pod_gone_returns_when_the_read_404s():
    wait_pod_gone(_Core([_api_exception(404)]), "ns", "p", timeout_s=5)


def test_wait_pod_gone_raises_if_it_never_terminates():
    """Returning early is what let a caller adopt a Terminating corpse."""
    with pytest.raises(RuntimeError, match="terminating"):
        wait_pod_gone(_Core([_Pod("Running")] * 50), "ns", "p", timeout_s=0.01)


def test_wait_pod_gone_propagates_a_non_404():
    with pytest.raises(Exception, match="boom"):
        wait_pod_gone(_Core([_api_exception(500)]), "ns", "p", timeout_s=5)


# -- exec_stream --------------------------------------------------------------


class _Resp:
    """A scripted websocket-ish response, enough for the loop under test."""

    def __init__(self, chunks, returncode=0, never_closes=False):
        self._chunks = list(chunks)
        self._returncode = returncode
        self._never_closes = never_closes
        self.closed = False
        self.stdin = []
        self._out = ""
        self._err = ""

    def write_stdin(self, data):
        self.stdin.append(data)

    def is_open(self):
        return self._never_closes or bool(self._chunks)

    def update(self, timeout=None):
        if self._chunks:
            self._out, self._err = self._chunks.pop(0)

    def peek_stdout(self):
        return self._out

    def read_stdout(self):
        out, self._out = self._out, ""
        return out

    def peek_stderr(self):
        return self._err

    def read_stderr(self):
        err, self._err = self._err, ""
        return err

    @property
    def returncode(self):
        return self._returncode

    def close(self):
        self.closed = True


def _patched_stream(monkeypatch, resp):
    import kubernetes.stream
    monkeypatch.setattr(kubernetes.stream, "stream", lambda *a, **k: resp)


def test_exec_stream_collects_both_streams_and_the_exit_code(monkeypatch):
    resp = _Resp([("hello\n", ""), ("", "warn\n")], returncode=3)
    _patched_stream(monkeypatch, resp)
    core = type("C", (), {"connect_get_namespaced_pod_exec": object()})()
    code, out, err, timed_out = exec_stream(core, "p", "ns", "c", ["true"], limit_s=5)
    assert (code, out, err, timed_out) == (3, "hello\n", "warn\n", False)
    assert resp.closed


def test_exec_stream_bounds_a_command_that_never_finishes(monkeypatch):
    """Without a bound this loop spins for as long as the command runs.

    That is fine until the command never finishes: a hung aux-container helper then hung
    the campaign's worker thread with it, silently and forever.
    """
    resp = _Resp([], never_closes=True)
    _patched_stream(monkeypatch, resp)
    core = type("C", (), {"connect_get_namespaced_pod_exec": object()})()
    code, _out, _err, timed_out = exec_stream(core, "p", "ns", "c", ["sleep", "inf"],
                                              limit_s=0.05)
    assert timed_out is True
    assert code == 124, "same code the local lane's subprocess timeout reports"
    assert resp.closed, "a bounded exec still releases the channel"


def test_exec_stream_treats_a_missing_status_as_a_failure(monkeypatch):
    """Reporting 0 for a channel that closed without a status would invent a success."""
    resp = _Resp([("out", "")], returncode=None)
    _patched_stream(monkeypatch, resp)
    core = type("C", (), {"connect_get_namespaced_pod_exec": object()})()
    code, _out, _err, timed_out = exec_stream(core, "p", "ns", "c", ["x"], limit_s=5)
    assert (code, timed_out) == (124, True)


def test_exec_stream_writes_stdin_when_given(monkeypatch):
    resp = _Resp([("", "")])
    _patched_stream(monkeypatch, resp)
    core = type("C", (), {"connect_get_namespaced_pod_exec": object()})()
    exec_stream(core, "p", "ns", "c", ["cat"], limit_s=5, stdin_data="payload")
    assert resp.stdin == ["payload"]
