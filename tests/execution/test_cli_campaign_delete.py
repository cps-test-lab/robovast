# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast campaign delete A B C`` — one line per campaign, and an exit status that is the answer.

Each campaign is deleted or refused on its own, so the command must not stop at the first
refusal, and must not exit 0 over one it could not delete: a script clearing out campaigns
reads the exit status, not the lines.
"""

import contextlib

import pytest
from click.testing import CliRunner

from robovast.client import campaign_cli
from robovast.service.interface import CampaignDeletion, DeleteCampaignsResponse

A, B, C = "a-2026-09-01-101500", "b-2026-09-01-101500", "c-2026-09-01-101500"


@pytest.fixture(name="asked")
def _asked(monkeypatch):
    """Point the command at a fake service: B is running, the rest delete."""
    seen = []

    class _Client:
        def delete_campaigns(self, request):
            seen.append(request.campaign_ids)
            return DeleteCampaignsResponse(results=[
                CampaignDeletion(campaign_id=cid, outcome="running", ok=False,
                                 message="still running; stop it before deleting.")
                if cid == B else
                CampaignDeletion(campaign_id=cid, outcome="deleted", ok=True,
                                 message=f"Deleted campaign {cid!r}.")
                for cid in request.campaign_ids])

    @contextlib.contextmanager
    def _client(*_a, **_k):
        yield _Client(), "fake service"

    monkeypatch.setattr(campaign_cli, "service_client", _client)
    return seen


def _run(*args, **kwargs):
    return CliRunner().invoke(campaign_cli.campaign, ["delete", *args], **kwargs)


def test_every_campaign_is_sent_in_one_call_and_reported_on_its_own_line(asked):
    result = _run(A, C, "--yes")
    assert result.exit_code == 0, result.output
    assert asked == [[A, C]]
    assert f"Deleted campaign '{A}'" in result.output
    assert f"Deleted campaign '{C}'" in result.output


def test_one_refusal_fails_the_command_but_not_the_others(asked):
    result = _run(A, B, C, "--yes")
    assert result.exit_code != 0
    assert asked == [[A, B, C]]
    assert f"Deleted campaign '{C}'" in result.output, "the refusal must not stop the rest"
    assert f"{B}: still running" in result.output
    assert "1 of 3" in result.output


def test_the_prompt_names_every_campaign_and_no_answers_nothing_is_sent(asked):
    result = _run(A, C, input="n\n")
    assert A in result.output and C in result.output
    assert "Aborted." in result.output
    assert asked == []


def test_a_campaign_named_twice_is_sent_once(asked):
    assert _run(A, A, "--yes").exit_code == 0
    assert asked == [[A]]


def test_at_least_one_campaign_is_required(asked):
    assert _run("--yes").exit_code != 0
    assert asked == []
