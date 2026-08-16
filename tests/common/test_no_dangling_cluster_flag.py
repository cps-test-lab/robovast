# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The commands that outlived the deleted ``--cluster`` flag.

Removing ``--cluster`` (one way to reach the service, no per-call port-forward) left three
call sites still reading a ``cluster`` name the decorator no longer bound. Python does not
catch that at import time, and neither failure was loud in the right way:

* ``vast workspace world`` raised ``NameError`` on **every** invocation — the name was
  simply absent from that module.
* ``vast exec command`` and ``vast exec stop-container`` resolved ``cluster`` to the
  module-level click ``Group`` of the same name, which is always truthy, so both silently
  pinned the request to the cluster lane with no way to select another.

The second is the one worth a test: it never raised, so nothing pointed at it. These
assert the *request* each command builds, because that — not the exit code — is where the
wrong lane was chosen.
"""

import contextlib

import pytest
from click.testing import CliRunner

from robovast.common.cli import cli as root_cli
from robovast.execution.execution_utils import cli as exec_cli


class _Recorder:
    """A service client that records what it was asked for."""

    def __init__(self):
        self.calls = {}

    # -- used by `vast workspace world`
    def describe_world(self, *args, **kwargs):
        self.calls["describe_world"] = (args, kwargs)
        raise AssertionError("stop here: the request was built, which is what we assert")

    # -- used by `vast exec command`
    def exec_in_container(self, request):
        self.calls["exec_in_container"] = request
        raise AssertionError("stop here: the request was built, which is what we assert")

    # -- used by `vast exec stop-container`
    def stop_exec_container(self, backend):
        self.calls["stop_exec_container"] = backend
        raise AssertionError("stop here: the request was built, which is what we assert")


@pytest.fixture
def recorder(monkeypatch):
    """Patch ``service_client`` in both CLI modules; return the shared recorder."""
    rec = _Recorder()

    @contextlib.contextmanager
    def _client(*_a, **_k):
        yield rec, "fake service"

    monkeypatch.setattr(root_cli, "service_client", _client)
    monkeypatch.setattr(exec_cli, "service_client", _client)
    monkeypatch.setattr(root_cli, "_resolve_workspace_id", lambda _c, w: w,
                        raising=False)
    return rec


def test_workspace_world_does_not_raise_name_error(recorder):
    """It reached the client at all. Before the fix this never got past the call site."""
    result = CliRunner().invoke(root_cli.workspace, ["world", "ws-1"])
    assert not isinstance(result.exception, NameError), result.exception
    assert "describe_world" in recorder.calls


def test_exec_command_does_not_pin_the_cluster_lane(recorder):
    """``backend`` must be unset so the service picks its own lane."""
    CliRunner().invoke(exec_cli.execution, ["command", "true"])
    request = recorder.calls.get("exec_in_container")
    assert request is not None, "the command never reached the client"
    assert request.backend is None, (
        f"backend was pinned to {request.backend!r}; the deleted --cluster flag used to "
        "select it, and a truthy leftover forced 'cluster' on every call")


def test_stop_container_does_not_pin_the_cluster_lane(recorder):
    CliRunner().invoke(exec_cli.execution, ["stop-container"])
    assert "stop_exec_container" in recorder.calls, "never reached the client"
    assert recorder.calls["stop_exec_container"] is None
