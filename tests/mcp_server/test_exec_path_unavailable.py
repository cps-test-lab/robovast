# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""What the MCP says when nothing can run in a container on this deployment.

Five tools answer by asking a container, and each of them used to report the same
deployment property in its own words -- "introspecting roqsim_plugins in <image> failed",
"could not describe this world in <image>", "the check itself failed here" -- which reads
as five separate defects, in an image and in a .vast that are both fine.

So the consequence is composed once, in this layer, because this is the only layer that
knows the tool names. The two spellings of the cause it recognises are the exception itself
(the MCP mounted inside the service) and the refusal's code (over HTTP), never the message.
"""

import json

import pytest

from robovast.common.errors import ExecPathUnavailable
from robovast.mcp_server import service_access
from robovast.mcp_server.plugins import image_catalog
from robovast.service.interface import EXEC_PATH_UNAVAILABLE, ImageResolution, ServiceError

_SAID = "no command can run in a container on this deployment: the connection was never upgraded"


def _over_http():
    """The same fact as a client sees it: a coded refusal, the type having been dropped."""
    return ServiceError(503, _SAID, "https://robovast.example.com/exec",
                        code=EXEC_PATH_UNAVAILABLE)


@pytest.mark.parametrize("error", [ExecPathUnavailable(_SAID), _over_http()],
                         ids=["in-process", "over-http"])
def test_the_verdict_is_recognised_wherever_the_mcp_runs(error):
    """Mounted inside the service the tools are handed the exception; over HTTP they are
    handed a ``ServiceError`` with the code. Both must reach the same answer, or the same
    deployment reports differently depending on where the MCP happens to run."""
    out = service_access.error_result(error)
    assert _SAID in out["error"]
    assert "exec_in_container" in out["error"], "which tools cannot answer is the consequence"
    assert "unaffected" in out["error"], "and which still can"


def test_an_ordinary_failure_is_not_given_the_consequence():
    """The sentence is long, and a tool that appends it to every failure teaches a caller
    to skip it -- including the time it was true."""
    out = service_access.error_result(RuntimeError("a container is already running"))
    assert out["error"] == "a container is already running"


def test_the_message_is_never_what_is_matched_on():
    """A verdict recognised by its wording is a wording nobody may change. This one carries
    no code and is not the exception, so it is an ordinary failure however it reads."""
    out = service_access.error_result(ServiceError(503, _SAID, ""))
    assert "exec_in_container" not in out["error"]


class _NoExecPath:
    """A client that resolves images fine and cannot exec at all -- which is the state that
    made this look like a per-image problem: the image resolves, so the image looks wrong."""

    def resolve_image(self, request):
        return ImageResolution(image="robovast-build:abc123")

    def exec_in_container(self, request):
        raise _over_http()


@pytest.fixture(autouse=True)
def _clear_cache():
    image_catalog._cache.clear()
    yield
    image_catalog._cache.clear()


def test_a_catalog_reports_the_deployment_not_the_image(monkeypatch):
    monkeypatch.setattr(service_access, "service_client", lambda: _NoExecPath())
    out = image_catalog.list_image_catalog(
        address="/sources/ws-1/a.vast", catalog="roqsim_plugins")
    assert "no command can run in a container" in out["error"]
    assert "introspecting" not in out["error"], (
        "the introspection never ran, and naming it points at an image that is fine")
    assert "robovast-build:abc123" not in out["error"]


def test_a_catalog_that_did_run_and_failed_still_says_so(monkeypatch):
    """The other half of the same rule: a command that ran and failed IS about the image,
    and must keep saying which image and what it said."""
    class _Failing(_NoExecPath):
        def exec_in_container(self, request):
            from robovast.service.interface import ExecResult
            return ExecResult(exit_code=1, stdout="", stderr="No module named roqsim")

    monkeypatch.setattr(service_access, "service_client", lambda: _Failing())
    out = image_catalog.list_image_catalog(
        address="/sources/ws-1/a.vast", catalog="roqsim_plugins")
    assert "robovast-build:abc123" in out["error"]
    assert "No module named roqsim" in out["error"]


def test_a_working_catalog_is_untouched(monkeypatch):
    class _Working(_NoExecPath):
        def exec_in_container(self, request):
            from robovast.service.interface import ExecResult
            return ExecResult(exit_code=0, stdout=json.dumps(
                {"items": [{"name": "contact_monitor", "kind": "plugin", "doc": "d"}]}))

    monkeypatch.setattr(service_access, "service_client", lambda: _Working())
    out = image_catalog.list_image_catalog(
        address="/sources/ws-1/a.vast", catalog="roqsim_plugins")
    assert [item["name"] for item in out["items"]] == ["contact_monitor"]
