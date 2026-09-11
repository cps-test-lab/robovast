# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""A deployment where no command can run in a container, said once and acted on.

An API server that does not serve the exec subresource as a stream refuses every exec
equally, before any command exists. The cause is one sentence at the source; what a caller
needs on top of it is the *class* of the failure, because the callers that must degrade
rather than mis-attribute (report the deployment instead of the image, mark a check
unchecked instead of failed) branch on it.

A type cannot cross an HTTP boundary, which is why the refusal names its class in a header:
without that, each consumer would have to recognise the verdict by matching on the message,
and a message matched on by five clients is one nobody may reword.
"""

import pytest
from fastapi.testclient import TestClient

from robovast.common.errors import ExecPathUnavailable
from robovast.service.app import build_app
from robovast.service.interface import (ERROR_CODE_HEADER, EXEC_PATH_UNAVAILABLE, ExecRequest,
                                        Routes, ServiceError)

#: What the source says, shortened: the verdict, then the cause it was read from.
_SAID = ("no command can run in a container on this deployment: the connection was never "
         "upgraded to a websocket")


class _NoExecPath:
    """Impl whose exec fails the way an unupgradable deployment fails it."""

    def exec_in_container(self, request):
        raise ExecPathUnavailable(_SAID)

    def shutdown(self):
        pass


@pytest.fixture(name="client")
def _client():
    with TestClient(build_app(_NoExecPath())) as client:
        yield client


def _refuse(client):
    return client.post(Routes.EXEC,
                       json=ExecRequest(command="true", workspace_id="ws-1").model_dump())


def test_the_refusal_is_a_503_carrying_the_message(client):
    """503, not the 409 its ``RuntimeError`` base would otherwise map to: nothing is in
    conflict, and a caller may try again once the deployment can exec."""
    resp = _refuse(client)
    assert resp.status_code == 503
    assert _SAID in resp.json()["detail"]


def test_the_refusal_names_its_class_so_a_client_need_not_read_the_message(client):
    resp = _refuse(client)
    assert resp.headers[ERROR_CODE_HEADER] == EXEC_PATH_UNAVAILABLE


def test_an_ordinary_conflict_carries_no_code(client):
    """A code is a fact a client acts on. Putting one on every refusal makes it a field
    nobody reads, so a plain ``RuntimeError`` keeps its uncoded 409."""
    class _Busy(_NoExecPath):
        def exec_in_container(self, request):
            raise RuntimeError("a container is already running for this caller")

    with TestClient(build_app(_Busy())) as busy:
        resp = _refuse(busy)
    assert resp.status_code == 409
    assert ERROR_CODE_HEADER not in resp.headers


class _Response:
    """The part of a ``requests`` response ``raise_for_status`` reads."""

    def __init__(self, headers):
        self.ok = False
        self.status_code = 503
        self.reason = "Service Unavailable"
        self.url = "https://robovast.example.com/exec"
        self.headers = headers
        self.text = ""

    def json(self):
        return {"detail": _SAID}


def test_the_client_hands_the_class_to_the_caller_beside_the_message():
    from robovast.service.http_client import HTTPTransport

    with pytest.raises(ServiceError) as raised:
        HTTPTransport.raise_for_status(_Response({ERROR_CODE_HEADER: EXEC_PATH_UNAVAILABLE}))
    assert raised.value.code == EXEC_PATH_UNAVAILABLE
    assert _SAID in raised.value.detail


def test_a_refusal_without_a_code_leaves_the_field_empty_not_guessed():
    from robovast.service.http_client import HTTPTransport

    with pytest.raises(ServiceError) as raised:
        HTTPTransport.raise_for_status(_Response({}))
    assert raised.value.code == ""
