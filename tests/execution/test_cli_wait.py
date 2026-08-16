# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast exec wait`` — the way a caller waits for a campaign without holding a request.

An agent harness can background a shell command and be notified when it exits; it cannot
do that with an MCP call, which occupies the conversation for as long as it blocks. For a
campaign that may run for days that difference is the whole point, so the campaign wait
lives here rather than on the tool surface.

The exit code is the contract: a script (or a harness) branches on it without parsing
anything, and "the campaign failed" must not look like "I stopped waiting".
"""

import contextlib

import pytest
from click.testing import CliRunner

from robovast.client.status import Phase, Status
from robovast.client import cli as client_cli


@pytest.fixture
def service(monkeypatch):
    """Point the command at a fake service; *phases* is consumed one per poll."""
    def _install(phases, **status_fields):
        seen = []

        class _Client:
            def get_status(self, campaign_id):
                phase = phases[min(len(seen), len(phases) - 1)]
                seen.append(phase)
                return Status(phase=phase, campaign_id=campaign_id, **status_fields)

        @contextlib.contextmanager
        def _client(*_a, **_k):
            yield _Client(), "fake service"

        monkeypatch.setattr(client_cli, "service_client", _client)
        return seen
    return _install


def _run(campaign="c1", *args):
    return CliRunner().invoke(client_cli.cli,
                              ["wait", campaign, "--interval", "0.01", *args])


def test_a_finished_campaign_exits_zero(service):
    service([Phase.RUNNING, Phase.FINISHING, Phase.FINISHED])
    result = _run()
    assert result.exit_code == 0
    assert "finished" in result.output


def test_it_waits_through_finishing(service):
    """``finishing`` is the window where share and postprocessing still run. Exiting
    there would report a campaign as over before its metrics exist — the original bug."""
    seen = service([Phase.FINISHING, Phase.FINISHING, Phase.FINISHED])
    assert _run().exit_code == 0
    assert len(seen) >= 3  # it kept polling rather than stopping at `finishing`


def test_a_failed_campaign_exits_one(service):
    service([Phase.FAILED], error="image build failed")
    result = _run()
    assert result.exit_code == 1
    assert "image build failed" in result.output


def test_a_stopped_campaign_exits_one(service):
    service([Phase.STOPPED])
    assert _run().exit_code == 1


def test_a_timeout_is_its_own_exit_code(service):
    """Distinct from failure: the campaign is still running and can be waited on again.
    Collapsing the two would make a caller treat a live campaign as a dead one."""
    service([Phase.RUNNING])
    result = _run("c1", "--timeout", "0.05")
    assert result.exit_code == 2


def test_a_finished_campaign_whose_postprocessing_failed_says_so(service):
    """It exits 0 — the runs are the deliverable and they passed — so the reason to look
    for missing CSVs has to be *said*, or a successful exit promises data that is absent.
    """
    service([Phase.FINISHED], postprocessing_error="conversion died")
    result = _run()
    assert result.exit_code == 0
    assert "postprocessing failed" in result.output


def test_no_phase_at_all_is_its_own_exit_code(service):
    """``unknown`` is terminal but it is not failure, and conflating them misleads.

    The service reports it for two things: an id that names no campaign, and a campaign
    that died before it ever wrote to the store. Neither is "the campaign ran and failed",
    which is what exit 1 told a caller — sending them to hunt for a failure that never
    happened, or worse, to believe a typo'd id had really run and broken.
    """
    service([Phase.UNKNOWN])
    result = _run()
    assert result.exit_code == 3
    assert "knows no phase" in result.output
