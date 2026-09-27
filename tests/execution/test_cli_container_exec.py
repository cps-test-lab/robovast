# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast container exec`` takes the inputs ``exec_in_container`` takes, and sends them
as the one request both build: which container answers, and whether to replace a held
one. A CLI caller that cannot name the simulation container cannot check the simulator.
"""

import contextlib

from click.testing import CliRunner

from robovast.client import container_cli
from robovast.service.interface import ExecContainerState, ExecResult


def _run(monkeypatch, *args):
    asked = []

    class _Client:
        def exec_in_container(self, request):
            asked.append(request)
            return ExecResult(exit_code=0, stdout="ok\n", stderr="", timed_out=False,
                              duration_s=0.1, limit_s=60, limit_source="command",
                              container=ExecContainerState(image="img:1"))

    @contextlib.contextmanager
    def _service(*_a, **_k):
        yield _Client(), "fake service"

    monkeypatch.setattr(container_cli, "service_client", _service)
    result = CliRunner().invoke(container_cli.container, ["exec", *args])
    assert result.exit_code == 0, result.output
    return asked[0]


def test_the_container_and_fresh_reach_the_request(monkeypatch):
    request = _run(monkeypatch, "--workspace", "ws1", "--container", "simulation",
                   "--fresh", "roqsim --version")
    assert request.container == "simulation"
    assert request.fresh is True
    assert request.command == "roqsim --version"


def test_the_defaults_leave_the_choice_to_the_service(monkeypatch):
    """An empty container is the service's default, and a plain call joins a held
    container rather than replacing it -- the same defaults the MCP tool sends."""
    request = _run(monkeypatch, "--workspace", "ws1", "ls")
    assert request.container == ""
    assert request.fresh is False
