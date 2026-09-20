# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast campaign priority|pause|resume`` — the operator's half of the admission queue.

The value these verbs are *for* is a negative one: moving a long campaign out of the way of
a short one is the case that made a priority knob necessary at all. Click reads a leading
dash as an option, so the command that takes it has to say otherwise — without that,
``priority -1 <id>`` fails on exactly the input it exists to accept, and the remedy (a bare
``--``) is not something a reader would guess.
"""

import contextlib

import pytest
from click.testing import CliRunner

from robovast.client import campaign_cli
from robovast.service.interface import ActionResult


@pytest.fixture
def calls(monkeypatch):
    """Point the verbs at a fake service and record what reached it."""
    seen = []

    class _Client:
        def set_campaign_scheduling(self, campaign_id, priority=None, paused=None):
            seen.append((campaign_id, priority, paused))
            return ActionResult(ok=True, message="ok")

    @contextlib.contextmanager
    def _client(*_a, **_k):
        yield _Client(), "fake service"

    monkeypatch.setattr(campaign_cli, "service_client", _client)
    return seen


def _run(*args):
    return CliRunner().invoke(campaign_cli.campaign, list(args))


def test_a_negative_priority_is_a_value_not_an_option(calls):
    """The case the feature exists for, and the one click would otherwise reject."""
    result = _run('priority', '-1', 'camp-1')
    assert result.exit_code == 0, result.output
    assert calls == [("camp-1", -1, None)]


def test_a_positive_priority_reaches_the_service(calls):
    assert _run('priority', '3', 'camp-1').exit_code == 0
    assert calls == [("camp-1", 3, None)]


def test_the_default_rank_can_be_restored(calls):
    """Putting a campaign back to normal is a real request, not an empty one."""
    assert _run('priority', '0', 'camp-1').exit_code == 0
    assert calls == [("camp-1", 0, None)]


def test_a_value_that_is_not_a_number_is_still_refused(calls):
    """Letting a dash through must not let everything through."""
    result = _run('priority', 'abc', 'camp-1')
    assert result.exit_code != 0
    assert "not a valid integer" in result.output
    assert calls == []


def test_a_mistyped_option_is_still_refused(calls):
    result = _run('priority', '--bogus', 'camp-1')
    assert result.exit_code != 0
    assert calls == []


def test_pause_sets_only_the_hold(calls):
    """The rank must be left alone, or pausing would reset what it resumes at."""
    assert _run('pause', 'camp-1').exit_code == 0
    assert calls == [("camp-1", None, True)]


def test_resume_sets_only_the_hold(calls):
    assert _run('resume', 'camp-1').exit_code == 0
    assert calls == [("camp-1", None, False)]
