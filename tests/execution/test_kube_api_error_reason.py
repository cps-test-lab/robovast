# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""What a failed Kubernetes call is allowed to say to its caller.

``ApiException.reason`` is not an HTTP reason phrase, and the generated client is why: its
stream helper turns *any* exception into ``ApiException(status=0, reason=str(e))``. So a
websocket handshake that the peer answered with an ordinary HTTP response arrives as that
exception's whole repr — response headers, an audit id, a ``None`` body — and forwarding it
verbatim reached a caller as several hundred characters naming nothing to fix, with the one
load-bearing fact (the connection was never upgraded) buried in the middle.

Three properties are pinned here: the reason a caller gets is short and says what happened,
the call that failed is the call that gets named, and a handshake is classified by *every*
entry point rather than only the one that happened to be looked at.
"""

import ast
import pathlib

import pytest

from robovast.common.errors import ExecPathUnavailable
from robovast.execution.cluster_execution import kube_client, kube_exec_lane
from robovast.execution.cluster_execution.kube_client import api_error_reason, exec_stream

pytest.importorskip("kubernetes")

#: How ``websocket`` stringifies a handshake the peer answered without upgrading: the
#: status line, then the response headers, then the body, joined by its own separator.
_HANDSHAKE_REPR = (
    "Handshake status 200 OK -+-+- {'audit-id': '14e83072-3fd8-4c15-be5b-bdc0cd05fbee', "
    "'cache-control': 'no-cache, private', 'content-type': 'application/json', "
    "'date': 'Mon, 07 Sep 2026 16:29:35 GMT', 'transfer-encoding': 'chunked'} -+-+- None"
)


class _Wrapped:
    """An ``ApiException`` as the stream helper builds one: status 0, a repr for a reason."""

    status = 0
    reason = _HANDSHAKE_REPR
    body = None


class _Refused:
    """An ``ApiException`` from the REST path: a status code and a ``Status`` body."""

    status = 403
    reason = "Forbidden"
    body = ('{"kind":"Status","status":"Failure","message":"pods \\"x\\" is forbidden: '
            'User cannot create resource \\"pods\\"","code":403}')


def test_a_handshake_failure_says_the_connection_was_not_upgraded():
    reason = api_error_reason(_Wrapped())
    assert "not upgraded" in reason or "never upgraded" in reason
    assert "200" in reason, "the code the peer answered with is the fact to keep"


def test_a_handshake_failure_carries_none_of_the_transport_dump():
    """Header noise is not a diagnosis. An audit id and a flow-schema uid describe the
    request, not the failure, and they crowd out what the reader needs."""
    reason = api_error_reason(_Wrapped())
    assert "-+-+-" not in reason
    assert "audit-id" not in reason and "14e83072" not in reason
    assert "transfer-encoding" not in reason
    assert len(reason) < 400, reason


def test_the_api_servers_own_message_wins_over_the_reason_phrase():
    """``Forbidden`` names the class; the Status body names the resource and the verb."""
    reason = api_error_reason(_Refused())
    assert "403" in reason
    assert "is forbidden" in reason
    assert "cannot create resource" in reason


def test_an_unrecognised_reason_still_comes_through_short():
    """Built from the parts that describe a failure rather than by stripping known noise,
    so a shape this has never seen is readable rather than dropped."""

    class _Odd:
        status = 0
        reason = "something new\nwith a second line"
        body = None

    assert api_error_reason(_Odd()) == "something new"


def _refusing_stream(monkeypatch, reason):
    """``kubernetes.stream.stream`` failing the way the generated client fails it."""
    import kubernetes.stream
    from kubernetes.client.rest import ApiException

    def _refuse(*_a, **_k):
        raise ApiException(status=0, reason=reason)

    class _Core:
        connect_get_namespaced_pod_exec = staticmethod(lambda *_a, **_k: None)

    monkeypatch.setattr(kubernetes.stream, "stream", _refuse)
    return _Core()


def test_a_handshake_failure_is_its_own_type_not_a_bare_runtime_error(monkeypatch):
    """The type is the fix. An upgrade answered with an ordinary response refuses every exec
    on the deployment, and the callers that must degrade rather than blame the image or the
    .vast can only tell that from the class -- a message they would have to match on is one
    nobody may reword.

    Which operation the failure is attributed to is settled the same way. The handshake is
    the one part of an exec that fails before the command exists, and unlabelled it was
    reported by whichever wrapper enclosed the call, pointing a caller at an operation that
    had in fact succeeded.
    """
    _refusing_stream(monkeypatch, _HANDSHAKE_REPR)
    with pytest.raises(ExecPathUnavailable) as raised:
        exec_stream("exec-pod", "ns", "held", ["true"], limit_s=5)
    message = str(raised.value)
    assert "no command can run in a container" in message
    assert "never upgraded" in message, "the cause travels with the verdict"
    assert "start exec pod" not in message, "the pod started; the stream did not open"
    assert "-+-+-" not in message, "header noise is not a diagnosis"
    assert "exec-pod" not in message and "held" not in message, (
        "the pod this attempt happened to name is incidental, and naming it invites a "
        "caller to try another one")


def test_an_exec_that_failed_for_any_other_reason_is_not_a_deployment_verdict(monkeypatch):
    """Only the handshake says *nothing* can exec. A pod that went away between the check
    and the call is one pod, and reported as a deployment-wide outage it would send a
    caller to their cluster administrator over a race they can retry."""
    _refusing_stream(monkeypatch, "pods 'exec-pod' not found")
    with pytest.raises(RuntimeError) as raised:
        exec_stream("exec-pod", "ns", "held", ["true"], limit_s=5)
    assert not isinstance(raised.value, ExecPathUnavailable)
    assert "exec stream into exec-pod/held" in str(raised.value)


def _lane_refusing_to_create(monkeypatch, reason):
    """A lane whose pod creation fails the way the generated client fails it."""
    from kubernetes.client.rest import ApiException

    from robovast.service.container_exec import ExecSpec

    lane = kube_exec_lane.KubeExecLane("ns")

    class _Core:
        @staticmethod
        def create_namespaced_pod(*_a, **_k):
            raise ApiException(status=0, reason=reason)

    lane._core = _Core()  # noqa: SLF001 - avoids loading a kubeconfig for a refusal test
    monkeypatch.setattr(lane, "stop_held", lambda *_a, **_k: False)
    monkeypatch.setattr(lane, "_discard_staged", lambda *_a, **_k: 0)
    monkeypatch.setattr(lane, "_held_manifest", lambda *_a, **_k: {})
    # aux_spec set: an aux container stages nothing, so the refusal is reached without a
    # staging store this test would otherwise have to stand up.
    spec = ExecSpec(image="img", command="true", config_dir="", env={}, aux_spec=object())
    return lane, spec


def test_starting_an_exec_pod_classifies_a_handshake_the_same_way(monkeypatch):
    """The entry point, not the transport, was the hole. Opening a stream into a running
    pod and creating the pod to exec into are two ways into the same exec path, and a
    deployment that answers the upgrade with an ordinary response refuses both. Classified
    at only one of them, which one a caller reached first decided whether the service
    degraded gracefully or reported a defect in itself -- and the callers that must report
    ``unchecked`` rather than blame the world, the image or the ``.vast`` match on the type.
    """
    lane, spec = _lane_refusing_to_create(monkeypatch, _HANDSHAKE_REPR)
    with pytest.raises(ExecPathUnavailable) as raised:
        lane.start_held(spec, 60)
    message = str(raised.value)
    assert "no command can run in a container" in message
    assert "never upgraded" in message, "the cause travels with the verdict"
    assert "-+-+-" not in message, "header noise is not a diagnosis"


def test_a_pod_that_could_not_be_created_for_another_reason_names_that_call(monkeypatch):
    """Only the handshake says *nothing* can exec. A quota that refuses one pod is this
    call's own answer, and it keeps the operation's name so a caller is not sent to look at
    an exec stream that was never opened."""
    lane, spec = _lane_refusing_to_create(monkeypatch, "exceeded quota: pods")
    with pytest.raises(RuntimeError) as raised:
        lane.start_held(spec, 60)
    assert not isinstance(raised.value, ExecPathUnavailable)
    assert "could not start exec pod" in str(raised.value)


def _raises_rendering_an_api_error(module):
    """Every ``raise`` in *module* whose message it builds by rendering an ApiException."""
    tree = ast.parse(pathlib.Path(module.__file__).read_text())
    classifier = next((n for n in ast.walk(tree)
                       if isinstance(n, ast.FunctionDef) and n.name == "raise_api_error"), None)
    exempt = set(ast.walk(classifier)) if classifier is not None else set()
    return [node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Raise) and node not in exempt
            and any(isinstance(c, ast.Call) and getattr(c.func, "id", "") == "api_error_reason"
                    for c in ast.walk(node))]


@pytest.mark.parametrize("module", [kube_client, kube_exec_lane])
def test_no_call_site_renders_an_api_error_into_its_own_raise(module):
    """The rule, rather than one more instance of it. ``api_error_reason`` renders a reason
    and does not classify it, so a ``raise`` that calls it directly is a failure the
    deployment-wide type can never escape from -- which is the defect this file's other
    tests pin at two entry points and cannot pin at a third nobody has written yet.
    Rendering for a log is unaffected: a site that logs and continues raises nothing.
    """
    offenders = _raises_rendering_an_api_error(module)
    assert not offenders, (
        f"{pathlib.Path(module.__file__).name} lines {offenders}: raise through "
        "raise_api_error() so a handshake keeps its type")


class _PodRead:
    """A ``core`` whose pod read fails with *reason*, for the waits that poll one."""

    def __init__(self, reason):
        from kubernetes.client.rest import ApiException
        self._exc = ApiException(status=0, reason=reason)

    def read_namespaced_pod(self, *_a, **_k):
        raise self._exc


def test_waiting_for_a_pod_classifies_a_handshake_too():
    """The wait is inside the exec path, not beside it: it exists only so an exec can follow,
    and it polls through the same client. Left unclassified it was the one way into the path
    that still handed callers an untyped failure -- and a caller that must report `unchecked`
    rather than blame the project cannot tell from an ApiException that it should."""
    from robovast.execution.cluster_execution.kube_client import wait_pod_ready
    with pytest.raises(ExecPathUnavailable):
        wait_pod_ready(_PodRead(_HANDSHAKE_REPR), "ns", "exec-pod", timeout_s=5)


def test_waiting_for_a_pod_to_go_classifies_a_handshake_too():
    """``wait_pod_gone`` treats 404 as the answer it wants and re-raised everything else as
    it came, so the same condition escaped untyped by a second route."""
    from robovast.execution.cluster_execution.kube_client import wait_pod_gone
    with pytest.raises(ExecPathUnavailable):
        wait_pod_gone(_PodRead(_HANDSHAKE_REPR), "ns", "exec-pod", timeout_s=5)


def test_a_pod_read_that_failed_for_another_reason_names_that_wait():
    """Not every read failure is the deployment refusing every exec. One that is not keeps
    the operation's name, so a caller is not sent to look at an exec that never happened."""
    from robovast.execution.cluster_execution.kube_client import wait_pod_ready
    with pytest.raises(RuntimeError) as raised:
        wait_pod_ready(_PodRead("etcdserver: request timed out"), "ns", "exec-pod", timeout_s=5)
    assert not isinstance(raised.value, ExecPathUnavailable)
    assert "could not read pod exec-pod" in str(raised.value)
