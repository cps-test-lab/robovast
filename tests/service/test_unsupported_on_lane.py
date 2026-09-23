# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""An operation a lane does not offer is refused by name, the same way on every surface.

The two execution lanes do not offer the same operations: the local Docker lane runs one
campaign at a time and has no queue to rank, the cluster lane has no screen to open a
window on. That asymmetry is allowed. What is not allowed is a refusal that reads as
something else -- bad input, a conflict, a bug -- or an acceptance that quietly does
nothing. So there is one exception for it, it names the operation and the lane, and it
survives the trip to every client: a 501 with the sentence as ``detail`` and its class in
the error header, a ``ServiceError`` with the same status and code on the other side, one
line on the CLI, an ``error`` field in an MCP tool's answer.
"""

import contextlib

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from robovast.client import campaign_cli
from robovast.execution.cluster_execution.cluster_service import ClusterService
from robovast.mcp_server import service_access
from robovast.mcp_server.plugins import execution as execution_tools
from robovast.service.app import build_app
from robovast.service.client import LocalTransport
from robovast.service.http_client import HTTPTransport
from robovast.service.interface import (ERROR_CODE_HEADER, UNSUPPORTED_ON_LANE,
                                        RobovastInterface, Routes,
                                        ServiceError, UnsupportedOnLane)

_SENTENCE = "set_campaign_scheduling is not supported on the local lane"


# -- the sentence ------------------------------------------------------------------------

def test_the_refusal_names_the_operation_and_the_lane():
    e = UnsupportedOnLane("set_campaign_scheduling", "local")
    assert str(e) == _SENTENCE
    assert (e.operation, e.lane, e.hint) == ("set_campaign_scheduling", "local", "")


def test_a_hint_follows_the_sentence_as_its_own_sentence():
    e = UnsupportedOnLane("show_gui", "cluster", hint="re-run without it")
    assert str(e) == "show_gui is not supported on the cluster lane. re-run without it"


def test_it_is_a_service_error_with_the_status_and_code_a_client_reads():
    """In process it must hit the same ``except`` arms a refusal over HTTP does, and carry
    what that refusal would carry, or the MCP mounted inside the service reports it
    differently from a remote one."""
    e = UnsupportedOnLane("x", "local")
    assert isinstance(e, ServiceError)
    assert (e.status, e.code) == (501, UNSUPPORTED_ON_LANE)
    assert e.include_traceback is False, "a refusal is the whole report; frames name nothing"


def test_the_interface_itself_has_no_lane_to_name():
    """A default on the interface is raised by whichever implementation inherited it, and
    says so when that implementation declares no lane -- never an empty name in a sentence."""
    assert RobovastInterface.LANE == ""  # pylint: disable=no-member
    assert str(UnsupportedOnLane("x", "")) == "x is not supported by this service"


# -- which lane is answering -------------------------------------------------------------

@pytest.mark.parametrize("impl, lane", [(LocalTransport, "local"), (ClusterService, "cluster"),
                                        (HTTPTransport, "http")])
def test_every_transport_declares_its_lane(impl, lane):
    """The name in the sentence comes from the class, so a refusal inherited from the
    interface still says which side declined."""
    assert impl.LANE == lane


# -- over HTTP ---------------------------------------------------------------------------

class _NoQueue:
    """A lane whose scheduling operation is the local lane's."""

    def set_campaign_scheduling(self, campaign_id, priority=None, paused=None):
        del campaign_id, priority, paused
        raise UnsupportedOnLane("set_campaign_scheduling", "local")

    def shutdown(self):
        pass


@pytest.fixture(name="client")
def _client():
    with TestClient(build_app(_NoQueue())) as client:
        yield client


def _refuse(client):
    return client.post(Routes.campaign_scheduling("camp-1"), params={"priority": 1})


def test_over_http_it_is_a_501_carrying_the_sentence(client):
    """501, not the 500 a bare ``NotImplementedError`` becomes: that one still means a bug,
    and this one means the operation exists and this lane does not offer it."""
    resp = _refuse(client)
    assert resp.status_code == 501
    assert resp.json()["detail"] == _SENTENCE


def test_over_http_it_names_its_class_so_a_client_need_not_read_the_sentence(client):
    assert _refuse(client).headers[ERROR_CODE_HEADER] == UNSUPPORTED_ON_LANE


class _Response:
    """The part of a ``requests`` response ``raise_for_status`` reads."""

    ok = False
    status_code = 501
    reason = "Not Implemented"
    url = "https://robovast.example.com/campaigns/camp-1/scheduling"
    text = ""
    headers = {ERROR_CODE_HEADER: UNSUPPORTED_ON_LANE}

    @staticmethod
    def json():
        return {"detail": _SENTENCE}


def test_the_client_hands_the_caller_the_same_status_code_and_sentence():
    with pytest.raises(ServiceError) as raised:
        HTTPTransport.raise_for_status(_Response())
    assert (raised.value.status, raised.value.code) == (501, UNSUPPORTED_ON_LANE)
    assert str(raised.value) == _SENTENCE


# -- on the CLI --------------------------------------------------------------------------

def test_the_cli_prints_the_sentence_and_nothing_else(monkeypatch):
    @contextlib.contextmanager
    def _service(*_a, **_k):
        yield _NoQueue(), "fake service"

    monkeypatch.setattr(campaign_cli, "service_client", _service)
    result = CliRunner().invoke(campaign_cli.campaign, ["pause", "camp-1"])
    assert result.exit_code == 1
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert lines[-1] == f"Error: {_SENTENCE}", result.output
    assert "Traceback" not in result.output


# -- in an MCP tool ----------------------------------------------------------------------

def test_an_mcp_tool_answers_with_the_sentence_as_its_error(monkeypatch):
    """Wherever the MCP runs: mounted in the service it is handed the exception, over HTTP
    the coded ``ServiceError`` -- and both are the same sentence to the caller."""
    class _NoWindow:
        def create_campaign(self, request):
            del request
            raise UnsupportedOnLane("show_gui", "cluster", hint="re-run without it")

    monkeypatch.setattr(service_access, "service_client", lambda: _NoWindow())
    out = execution_tools.start_campaign(workspace_id="ws-1", config_path="demo.vast",
                                         description="d", show_gui=True)
    assert out == {"error": "show_gui is not supported on the cluster lane. re-run without it"}


def test_the_mcp_does_not_dress_it_as_some_other_refusal():
    """The exec-path consequence is composed for one code; this one is printed as it is."""
    e = UnsupportedOnLane("exec_in_job", "local")
    assert service_access.error_result(e) == {"error": str(e)}
