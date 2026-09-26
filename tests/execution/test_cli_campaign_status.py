# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast campaign status`` prints a campaign's phase and its batch's run counter once.

The counter is read from the status the service returns, whose run progress is the
``runs`` block (``completed`` / ``total``); a reader that looks for it under another name
prints the phase alone and a running campaign looks as if it had no runs to count.
"""

import contextlib

from click.testing import CliRunner

from robovast.client import campaign_cli
from robovast.client.status import Phase, Status


def _run(monkeypatch, status, *args):
    class _Client:
        def get_status(self, campaign_id):
            return status

    @contextlib.contextmanager
    def _service(*_a, **_k):
        yield _Client(), "fake service"

    monkeypatch.setattr(campaign_cli, "service_client", _service)
    result = CliRunner().invoke(campaign_cli.campaign, ["status", "c1", *args])
    assert result.exit_code == 0, result.output
    return result.output


def test_a_running_campaign_prints_its_batch_run_counter(monkeypatch):
    out = _run(monkeypatch, Status(phase=Phase.RUNNING, campaign_id="c1",
                                   runs={"completed": 3, "total": 8}))
    assert "phase     running" in out
    assert "runs      3 / 8" in out


def test_a_campaign_with_no_runs_counted_prints_only_its_phase(monkeypatch):
    """Before a batch begins there is nothing to count, and ``0 / 0`` would read as a
    campaign that ran nothing."""
    out = _run(monkeypatch, Status(phase=Phase.INITIALIZING, campaign_id="c1"))
    assert "phase     initializing" in out
    assert "runs" not in out
