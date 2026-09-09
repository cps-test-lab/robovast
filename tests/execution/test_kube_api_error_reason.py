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

Two properties are pinned here: the reason a caller gets is short and says what happened,
and the call that failed is the call that gets named.
"""

import pytest

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


def test_a_failed_exec_handshake_names_the_exec_not_whatever_enclosed_it(monkeypatch):
    """The misattribution this closes: the handshake is the one part of an exec that fails
    before the command exists, and unlabelled it was reported by whichever wrapper happened
    to enclose the call — pointing a caller at an operation that had in fact succeeded."""
    import kubernetes.stream
    from kubernetes.client.rest import ApiException

    def _refuse(*_a, **_k):
        raise ApiException(status=0, reason=_HANDSHAKE_REPR)

    class _Core:
        connect_get_namespaced_pod_exec = staticmethod(lambda *_a, **_k: None)

    monkeypatch.setattr(kubernetes.stream, "stream", _refuse)
    with pytest.raises(RuntimeError) as raised:
        exec_stream(_Core(), "exec-pod", "ns", "held", ["true"], limit_s=5)
    message = str(raised.value)
    assert "exec stream into exec-pod/held" in message
    assert "start exec pod" not in message, "the pod started; the stream did not open"
    assert "-+-+-" not in message
