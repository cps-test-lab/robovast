# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""What ``vast serve`` tells the two clients someone is about to point at it.

A local service is reached by a person *and* by an agent, and only the person can be
handed a link. The agent needs a registration command, and getting it wrong is quiet: a
missing header 401s loudly, but a stale token authenticates nothing and looks like the
service is down.

The ephemeral case is the one worth pinning. ``resolve_token`` invents a token when none
is configured, and it invents a *different* one on every start — so a registration copied
from that banner stops working at the next restart, with nothing to say why. The banner
has to say so itself.
"""

import pytest

from robovast.service.app import startup_banner

URL = "http://127.0.0.1:8800"


def test_a_person_gets_a_link_carrying_the_token():
    banner = startup_banner(URL, "tok-1", ephemeral=True, mount_mcp=True)
    assert f"{URL}/login?token=tok-1" in banner


def test_an_agent_gets_the_registration_command():
    banner = startup_banner(URL, "tok-1", ephemeral=True, mount_mcp=True)
    assert "claude mcp add --transport http robovast http://127.0.0.1:8800/mcp" in banner
    assert "Authorization: Bearer tok-1" in banner


def test_an_ephemeral_token_says_it_will_not_survive_a_restart():
    """Otherwise the registration above is a trap: correct now, silently dead tomorrow."""
    banner = startup_banner(URL, "tok-1", ephemeral=True, mount_mcp=True)
    assert "changes on restart" in banner
    assert "ROBOVAST_AUTH_TOKEN" in banner


def test_a_configured_token_needs_no_warning_but_still_offers_the_command():
    """Whoever set the token can reuse it; the agent still cannot guess the command."""
    banner = startup_banner(URL, "tok-1", ephemeral=False, mount_mcp=True)
    assert "claude mcp add" in banner
    assert "changes on restart" not in banner
    assert "/login?token=" not in banner


@pytest.mark.parametrize("ephemeral", [True, False])
def test_no_mcp_means_no_agent_line(ephemeral):
    banner = startup_banner(URL, "tok-1", ephemeral=ephemeral, mount_mcp=False)
    assert "claude mcp add" not in banner


def test_nothing_to_say_prints_nothing():
    """A configured token and --no-mcp: no link to give, no command to give."""
    assert startup_banner(URL, "tok-1", ephemeral=False, mount_mcp=False) == ""


def test_the_agent_command_comes_from_the_shared_helper():
    """Three surfaces hand out access; a header set that drifts between them is the bug
    `mcp_add_command` exists to prevent, so this asserts the banner routes through it."""
    from robovast.client.login import mcp_add_command
    banner = startup_banner(URL, "tok-1", ephemeral=False, mount_mcp=True)
    for line in mcp_add_command(URL, "tok-1"):
        assert line in banner
