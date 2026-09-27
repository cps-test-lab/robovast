# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A campaign action the service refuses makes its verb exit 1, with the service's sentence.

``stop``, ``stop-job``, ``priority``, ``pause`` and ``resume`` get an ``ActionResult`` back;
one with ``ok: false`` is an action that was not taken, and a script branching on the exit
status must not read it as done.
"""

import contextlib

import pytest
from click.testing import CliRunner

from robovast.client import campaign_cli
from robovast.service.interface import ActionResult

_REFUSAL = "campaign c1 is finished; there is nothing left to queue"


class _Refusing:
    def stop(self, campaign_id):
        return ActionResult(ok=False, message=_REFUSAL)

    def stop_job(self, campaign_id, job_name, reason, source):
        return ActionResult(ok=False, message=_REFUSAL)

    def set_campaign_scheduling(self, campaign_id, priority, paused):
        return ActionResult(ok=False, message=_REFUSAL)


@pytest.mark.parametrize("verb", [
    ["stop", "c1"], ["stop-job", "job-1", "c1"], ["priority", "2", "c1"], ["pause", "c1"],
    ["resume", "c1"]])
def test_a_refused_action_exits_1_with_the_services_sentence(monkeypatch, verb):
    @contextlib.contextmanager
    def _service(*_a, **_k):
        yield _Refusing(), "fake service"

    monkeypatch.setattr(campaign_cli, "service_client", _service)
    result = CliRunner().invoke(campaign_cli.campaign, verb)
    assert result.exit_code == 1, result.output
    assert f"Error: {_REFUSAL}" in result.stderr
