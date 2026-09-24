# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""No command reads a ``cluster`` name its decorator does not bind.

There is one way to reach the service and no per-call port-forward, so no ``--cluster``
parameter. Python does not catch a leftover reference to one at import time, and neither
failure is loud in the right way:

* ``vast workspace world`` raises ``NameError`` on **every** invocation — the name is
  simply absent from that module.
* ``vast exec command`` and ``vast exec stop-container`` resolve ``cluster`` to the
  module-level click ``Group`` of the same name, which is always truthy, so it silently
  ends up in the request.

These assert the *request* each command builds, because that — not the exit code — is
where a leftover selector shows.
"""

import contextlib

import pytest
from click.testing import CliRunner

from robovast.client import cli as root_cli
from robovast.client import container_cli


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

    # -- used by `vast exec stop-container`. No parameter, matching the interface: a
    # fake that accepts more than the real transport hides a stale call site.
    def stop_exec_container(self):
        self.calls["stop_exec_container"] = True
        raise AssertionError("stop here: the request was built, which is what we assert")


@pytest.fixture
def recorder(monkeypatch):
    """Patch ``service_client`` in both CLI modules; return the shared recorder."""
    rec = _Recorder()

    @contextlib.contextmanager
    def _client(*_a, **_k):
        yield rec, "fake service"

    monkeypatch.setattr(root_cli, "service_client", _client)
    monkeypatch.setattr(container_cli, "service_client", _client)
    monkeypatch.setattr(root_cli, "_resolve_workspace_id", lambda _c, w: w,
                        raising=False)
    return rec


def test_workspace_world_does_not_raise_name_error(recorder):
    """It reached the client at all. Before the fix this never got past the call site."""
    result = CliRunner().invoke(root_cli.workspace, ["world", "ws-1"])
    assert not isinstance(result.exception, NameError), result.exception
    assert "describe_world" in recorder.calls


def test_exec_command_carries_no_backend_selector(recorder):
    """The exec request carries no backend selector: the service has one backend."""
    CliRunner().invoke(container_cli.container, ["exec", "true"])
    request = recorder.calls.get("exec_in_container")
    assert request is not None, "the command never reached the client"
    assert not hasattr(request, "backend"), (
        "ExecRequest grew a backend selector; a service runs one backend, so a "
        "per-request one can only ever be wrong or ignored")


def test_stop_container_takes_no_backend_selector(recorder):
    CliRunner().invoke(container_cli.container, ["stop"])
    assert recorder.calls.get("stop_exec_container"), (
        "never reached the client -- or was called with an argument it does not take")
