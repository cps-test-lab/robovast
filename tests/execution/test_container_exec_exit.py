# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast container exec`` exits with ``vast``'s own codes, never the command's.

A command exiting 2 passed through would read as a usage error of ``vast`` itself.
"""

import contextlib

import pytest
from click.testing import CliRunner

from robovast.client import container_cli
from robovast.execution.wait_exit import CommonExit
from robovast.service.interface import ExecResult


@pytest.fixture
def run(monkeypatch):
    def _run(result: ExecResult):
        class _Client:
            def exec_in_container(self, _request):
                return result

        @contextlib.contextmanager
        def _client(*_a, **_k):
            yield _Client(), "fake service"

        monkeypatch.setattr(container_cli, "service_client", _client)
        return CliRunner().invoke(container_cli.container, ["exec", "true"])
    return _run


def test_a_command_that_succeeds_exits_success(run):
    assert run(ExecResult(exit_code=0)).exit_code == CommonExit.SUCCESS


@pytest.mark.parametrize("code", [1, 2, 3, 137])
def test_a_failing_command_exits_failed_and_reports_its_own_status(run, code):
    result = run(ExecResult(exit_code=code))
    assert result.exit_code == CommonExit.FAILED
    assert f"[exit {code}" in result.output


def test_a_timed_out_command_exits_failed(run):
    assert run(ExecResult(exit_code=0, timed_out=True)).exit_code == CommonExit.FAILED
