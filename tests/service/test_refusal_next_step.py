# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""A refusal that knows the caller's next command carries it across HTTP.

An :class:`~robovast.common.errors.ActionableError` holds the one command that moves the
caller forward, and the MCP tools hand it back as ``next_step``. In process that is the
exception's attribute; over HTTP the exception is gone, so the service names the command in
a header and the transport puts it back on the :class:`ServiceError` it raises. A caller
then reads the same ``next_step`` whether its tools run inside the service or talk to it.
"""

import pytest
from fastapi.testclient import TestClient

from robovast.common.errors import ImageNotBuilt, InsufficientStorageError
from robovast.service.app import build_app
from robovast.service.interface import (NEXT_STEP_HEADER, ExecRequest, Routes,
                                        ServiceError)


class _Refusing:
    """Impl whose exec is refused with *error*."""

    def __init__(self, error):
        self.error = error

    def exec_in_container(self, request):
        raise self.error

    def shutdown(self):
        pass


def _exec(error):
    with TestClient(build_app(_Refusing(error))) as client:
        return client.post(Routes.EXEC,
                           json=ExecRequest(command="true", workspace_id="ws-1").model_dump())


def test_an_image_not_built_is_a_refusal_with_its_message_not_a_500():
    resp = _exec(ImageNotBuilt("the image for 'sut' is not built",
                               next_step="build_experiment_image(...)"))
    assert resp.status_code == 409
    assert resp.json()["detail"] == "the image for 'sut' is not built"
    assert resp.headers[NEXT_STEP_HEADER] == "build_experiment_image(...)"


def test_a_storage_refusal_keeps_its_507_and_names_the_command():
    resp = _exec(InsufficientStorageError("Cannot start a campaign. 3 GB free.",
                                          next_step="vast service cache --clear"))
    assert resp.status_code == 507
    assert resp.headers[NEXT_STEP_HEADER] == "vast service cache --clear"


def test_a_refusal_with_nothing_to_do_next_carries_no_header():
    """Empty is a real answer, and an empty header is not one."""
    resp = _exec(ImageNotBuilt("the image for 'sut' is not built"))
    assert resp.status_code == 409
    assert NEXT_STEP_HEADER not in resp.headers


class _Response:
    """The part of a ``requests`` response ``raise_for_status`` reads."""

    def __init__(self, headers):
        self.ok = False
        self.status_code = 409
        self.reason = "Conflict"
        self.url = "https://robovast.example.com/exec"
        self.headers = headers
        self.text = ""

    def json(self):
        return {"detail": "the image for 'sut' is not built"}


def test_the_client_hands_the_command_to_the_caller():
    from robovast.service.http_client import HTTPTransport

    with pytest.raises(ServiceError) as raised:
        HTTPTransport.raise_for_status(_Response({NEXT_STEP_HEADER: "vast image wait b1"}))
    assert raised.value.next_step == "vast image wait b1"


def test_the_mcp_answer_over_http_carries_the_command():
    from robovast.mcp_server.service_access import error_result

    answer = error_result(ServiceError(409, "the image for 'sut' is not built",
                                       next_step="vast image wait b1"))
    assert answer == {"error": "the image for 'sut' is not built",
                      "next_step": "vast image wait b1"}
