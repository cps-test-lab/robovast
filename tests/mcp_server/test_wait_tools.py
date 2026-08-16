# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Waiting for an image build is a call; waiting for a campaign is not.

Both operations return the moment the work is *named* and then run on, and both used to
offer nothing but "poll this" prose — which is how an agent came to read one status and
end its turn mid-campaign.

They are answered differently on purpose. A build takes minutes and always has work
behind it in the same turn, so blocking inside the tool costs nothing. A campaign can run
for days, and blocking there would occupy the caller for the whole of it — so the campaign
wait is a shell command (``vast wait``, tested in ``tests/execution/test_cli_wait``)
that a harness can background and be notified about. Same poll loop underneath; only who
holds the wait differs.
"""

from robovast.mcp_server.plugins import execution


def test_image_build_wait_blocks_until_done(monkeypatch):
    polls = iter([False, False, True])
    monkeypatch.setattr(execution, "get_image_build_status",
                        lambda bid: {"build_id": bid, "done": next(polls)})
    out = execution.wait_for_image_build("b1", poll_interval_s=1)
    assert out["done"] is True


def test_image_build_wait_says_call_again_on_timeout(monkeypatch):
    """A bounded wait ending is not a failure: the build is untouched and the recovery is
    to repeat the call — which is also what happens when an MCP client kills it."""
    monkeypatch.setattr(execution, "get_image_build_status",
                        lambda bid: {"build_id": bid, "done": False})
    out = execution.wait_for_image_build("b1", timeout_s=2, poll_interval_s=1)
    assert out["done"] is False
    assert out["next_step"] == "wait_for_image_build(build_id='b1')"


def test_image_build_failure_points_at_the_log(monkeypatch):
    monkeypatch.setattr(
        execution, "get_image_build_status",
        lambda bid: {"build_id": bid, "done": True, "error_detail": {"phase": "pip"}})
    out = execution.wait_for_image_build("b1", poll_interval_s=1)
    assert out["next_step"] == "get_image_build_log(build_id='b1')"


def test_starting_a_campaign_hands_back_the_command_that_waits_for_it(monkeypatch):
    """In-band, with the id filled in: a launch that returns only an id leaves "and now
    wait for it" to be remembered, and not remembering it is the reported bug.

    It names a *shell* command rather than an MCP tool, because holding the wait inside a
    tool call would occupy the caller for as long as the campaign runs.
    """
    step = execution._wait_next_step("camp-1")
    assert "vast wait camp-1" in step
    assert "background" in step
