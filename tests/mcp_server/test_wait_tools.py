# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Neither a campaign nor a build is waited for inside a tool call.

Both operations return the moment the work is *named* and then run on, and both used to
offer nothing but "poll this" prose — which is how an agent came to read one status and
end its turn mid-campaign.

They were once answered differently: the campaign wait went to a shell command, the build
wait stayed a blocking tool on the argument that a build is minutes rather than days, so
holding the caller costs nothing. That argument had a cap in it. The tool blocked for at
most 600s, and a ROS build doing apt + pip + colcon came back unfinished, to be re-called
— blocking again — in exactly the case where blocking cost most. The single-read half
(``get_image_build_status``) already existed, so the blocking loop was a third thing beside
it rather than the missing one.

So both are shell commands now (``vast wait``, ``vast image wait``), over loops in
``robovast_client`` that a harness can background, and what each tool owes its caller is
the *command*, in band, with the ids already filled in. Waiting for the loops themselves is
tested in ``tests/execution/test_cli_wait`` and ``tests/execution/test_image_build_wait``.
"""

from robovast.mcp_server.plugins import execution


def test_starting_a_campaign_hands_back_the_command_that_waits_for_it():
    """In-band, with the id filled in: a launch that returns only an id leaves "and now
    wait for it" to be remembered, and not remembering it is the reported bug.

    It names a *shell* command rather than an MCP tool, because holding the wait inside a
    tool call would occupy the caller for as long as the campaign runs.
    """
    step = execution._wait_next_step("camp-1")
    assert "vast wait camp-1" in step
    assert "background" in step


def test_building_an_image_hands_back_the_command_that_waits_for_it():
    """The same debt, and for a while the only surface that still paid it in prose."""
    step = execution._build_wait_next_step("b1", {"sut": "b1"}, False)
    assert "vast image wait b1" in step
    assert "background" in step


def test_a_multi_container_build_waits_for_every_id():
    """A project builds one image per container that adds packages. Naming only
    ``build_id`` would wait for one of them and call the rest built."""
    step = execution._build_wait_next_step(
        "b1", {"sut": "b1", "nav": "b2"}, False)
    assert "b1" in step and "b2" in step


def test_a_cache_hit_is_not_waited_for():
    """It already finished, so the wait is the one wrong next step — and the command
    would sit on a build id that never runs."""
    step = execution._build_wait_next_step("b1", {"sut": "b1"}, True)
    assert "vast image wait" not in step
    assert "start_campaign" in step


def test_the_blocking_build_wait_tool_is_gone():
    """It is not enough that the function was deleted: an entry left in ``_TOOLS`` would
    still spend the surface budget the deletion was meant to return."""
    assert not hasattr(execution, "wait_for_image_build")
    assert "wait_for_image_build" not in {fn.__name__ for fn in execution._TOOLS}
