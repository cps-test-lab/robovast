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

import types

import pytest

from robovast.execution.cluster_execution.kube_client import (exec_stream, pod_pending_reason,
                                                              pod_workload_containers,
                                                              wait_pod_gone, wait_pod_ready)


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


def _running_container(name):
    """An init container that is past its image and executing."""
    state = _State()
    state.running = object()
    return type("C", (), {"name": name, "state": state})()


# -- pod_pending_reason -------------------------------------------------------


def test_a_pending_pod_names_the_reason_it_is_stuck():
    """The phase alone is just "Pending", which explains nothing.

    Without it a bad image reference surfaces on the aux pod as five minutes of silence
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


def test_pod_initializing_names_the_init_container_that_is_still_running():
    """Kubelet's own wording for this is a placeholder with no message.

    An init container that is *running* is waiting for nothing, so the only reason on the
    pod is the main container's ``PodInitializing`` -- which, reported as it stands, tells
    a reader that a pod is initializing and nothing about what would end it.
    """
    pod = _Pod("Pending",
               init=[_running_container("mc-tools")],
               main=[_container("PodInitializing")])
    assert pod_pending_reason(pod) == ("PodInitializing: init container mc-tools "
                                       "is still running")


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


def test_wait_pod_ready_ends_when_the_caller_no_longer_wants_it():
    """A stopped campaign must not go on waiting out a pull it will never use.

    The wait is the longest thing a composition does on this lane, so a stop answered
    only when the image lands is a stop nothing distinguishes from a hang.
    """
    with pytest.raises(RuntimeError, match="stopped while waiting"):
        wait_pod_ready(_Core([_Pod("Pending")] * 20), "ns", "p", timeout_s=30,
                       should_stop=lambda: True)


def test_wait_pod_ready_keeps_waiting_while_the_caller_still_wants_it():
    """The predicate ends the wait; it must not shorten a healthy one."""
    wait_pod_ready(_Core([_Pod("Pending"), _Pod("Running")]), "ns", "p", timeout_s=30,
                   should_stop=lambda: False)


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
    code, out, err, timed_out = exec_stream("p", "ns", "c", ["true"], limit_s=5)
    assert (code, out, err, timed_out) == (3, "hello\n", "warn\n", False)
    assert resp.closed


def test_exec_stream_bounds_a_command_that_never_finishes(monkeypatch):
    """Without a bound this loop spins for as long as the command runs.

    That is fine until the command never finishes: a hung aux-container helper then hung
    the campaign's worker thread with it, silently and forever.
    """
    resp = _Resp([], never_closes=True)
    _patched_stream(monkeypatch, resp)
    code, _out, _err, timed_out = exec_stream("p", "ns", "c", ["sleep", "inf"], limit_s=0.05)
    assert timed_out is True
    assert code == 124, "same code the local lane's subprocess timeout reports"
    assert resp.closed, "a bounded exec still releases the channel"


def test_exec_stream_treats_a_missing_status_as_a_failure(monkeypatch):
    """Reporting 0 for a channel that closed without a status would invent a success."""
    resp = _Resp([("out", "")], returncode=None)
    _patched_stream(monkeypatch, resp)
    code, _out, _err, timed_out = exec_stream("p", "ns", "c", ["x"], limit_s=5)
    assert (code, timed_out) == (124, True)


def test_exec_stream_writes_stdin_when_given(monkeypatch):
    resp = _Resp([("", "")])
    _patched_stream(monkeypatch, resp)
    exec_stream("p", "ns", "c", ["cat"], limit_s=5, stdin_data="payload")
    assert resp.stdin == ["payload"]


def test_an_exec_leaves_every_other_caller_s_client_alone(monkeypatch):
    """``kubernetes.stream`` streams by rebinding ``ApiClient.request`` for the duration of
    the call. A client shared with anything else therefore has that rebinding applied under
    it, and an ordinary REST call issued through it meanwhile goes out as a websocket
    handshake and is refused -- carrying whatever status the REST call would have returned,
    so a read or a delete fails wearing the exec path's error and reads as the deployment
    being unable to exec at all. Streaming on a client of its own is what keeps one
    subsystem's exec from deciding what another subsystem's API call does.
    """
    import kubernetes.stream
    shared = _shared_client()
    observed = {}

    # The rebinding is an *instance* attribute, which is also how it is detected here: a
    # bound method is rebuilt on every attribute access, so comparing one to a previously
    # read one answers about method objects rather than about the client.
    def rebound(api) -> bool:
        return "request" in vars(api)

    def rebinding_stream(func, *_a, **_k):
        # What the real helper does: swap the request method on the client it is given.
        func.__self__.api_client.request = lambda *_x, **_y: None
        observed["streamed_on_the_shared_client"] = func.__self__.api_client is shared.api_client
        observed["shared_rebound_mid_stream"] = rebound(shared.api_client)
        return _Resp([("", "")])

    monkeypatch.setattr(kubernetes.stream, "stream", rebinding_stream)
    exec_stream("p", "ns", "c", ["true"], limit_s=5)

    assert observed["streamed_on_the_shared_client"] is False, (
        "exec_stream must build its own client, not stream on one anybody else holds")
    assert observed["shared_rebound_mid_stream"] is False, (
        "an unrelated caller's client was rebound while the exec was open")
    assert not rebound(shared.api_client), "the rebinding outlived the exec"


def test_the_streaming_helper_is_confined_to_exec_stream():
    """The rule, rather than one more instance of it. ``kubernetes.stream`` streams by
    rebinding ``ApiClient.request`` on the client it is handed, so whoever calls it decides
    what every other holder of that client gets for the duration. One call site, inside the
    one function that builds a client of its own, is what keeps that blast radius at zero --
    and a second call site elsewhere would reintroduce the fault silently, since it breaks
    only under concurrency and only in whatever unrelated call happens to be in flight.
    """
    import ast
    import pathlib as _pathlib

    from robovast.execution.cluster_execution import kube_client

    owner = _pathlib.Path(kube_client.__file__)
    tree = ast.parse(owner.read_text())
    exec_stream_def = next(n for n in ast.walk(tree)
                           if isinstance(n, ast.FunctionDef) and n.name == "exec_stream")
    allowed = set(ast.walk(exec_stream_def))
    offenders = [n.lineno for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "stream"
                 and n not in allowed]
    assert not offenders, (
        f"{owner.name} lines {offenders}: stream() outside exec_stream, which is the only "
        "place that owns a client nobody else holds")

    # And nowhere else at all: a module that imports the helper can call it on a shared
    # client without ever touching this file.
    repo_src = _pathlib.Path(__file__).resolve().parents[2] / "src"
    importers = []
    for tree_root in (repo_src / "robovast", repo_src / "robovast_cluster" / "robovast"):
        for path in tree_root.rglob("*.py"):
            if path == owner:
                continue
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                if "kubernetes.stream" in line and not line.lstrip().startswith("#"):
                    importers.append(f"{path.name}:{lineno}")
    assert not importers, (
        "only kube_client.exec_stream may reach the streaming helper; found " + ", ".join(importers))


def _shared_client():
    """A ``CoreV1Api`` standing in for the one a lane caches and threads share."""
    from kubernetes import client
    return client.CoreV1Api(client.ApiClient())


# --- which containers a pod actually runs -----------------------------------
#
# "Init container" is overloaded: one with restartPolicy Always is a NATIVE SIDECAR that
# runs for the pod's whole life. Three call sites answered "which containers?" from
# spec.containers alone and each was wrong the same way once the simulator and the SUT
# became sidecars — resource accounting dropped the two biggest reservations, image
# pinning pinned every role to the scenario's digest, and the job log showed one
# container out of three. These pin the shared answer.


def _spec_container(name, restart_policy=None, cpu=None):
    resources = types.SimpleNamespace(requests={"cpu": cpu}) if cpu else None
    return types.SimpleNamespace(name=name, restart_policy=restart_policy,
                                 resources=resources)


def _scenario_pod(init=None, containers=None):
    return types.SimpleNamespace(spec=types.SimpleNamespace(
        containers=containers if containers is not None else [_spec_container("robovast")],
        init_containers=init))


def test_workload_containers_includes_native_sidecars_and_orders_main_first():
    pod = _scenario_pod(init=[
        _spec_container("s3-init"),                              # ordinary: staging, exits
        _spec_container("simulation", restart_policy="Always"),
        _spec_container("sut", restart_policy="Always"),
    ])
    assert [c.name for c in pod_workload_containers(pod)] == [
        "robovast", "simulation", "sut"]


def test_workload_containers_excludes_ordinary_init_containers():
    """``s3-init`` populates /config and exits; counting it would double-count its CPU
    and put its (empty) log in the panel."""
    pod = _scenario_pod(init=[_spec_container("s3-init")])
    assert [c.name for c in pod_workload_containers(pod)] == ["robovast"]


def test_workload_containers_returns_specs_not_names():
    """Resource accounting reads ``.resources`` off these; the log path reads ``.name``.
    Returning names would force one of the two callers to re-fetch."""
    pod = _scenario_pod(init=[_spec_container("simulation", "Always", cpu="4")])
    sidecar = pod_workload_containers(pod)[1]
    assert sidecar.resources.requests["cpu"] == "4"


@pytest.mark.parametrize("pod", [
    types.SimpleNamespace(spec=None),                    # not scheduled yet
    _scenario_pod(init=None),                            # a pod with no init containers
    _scenario_pod(init=None, containers=None),
])
def test_workload_containers_tolerates_a_pod_without_the_fields(pod):
    """Every field here is optional in the kube API, and this runs against arbitrary
    pods on the node during resource accounting — not only RoboVAST's own."""
    assert isinstance(pod_workload_containers(pod), list)
