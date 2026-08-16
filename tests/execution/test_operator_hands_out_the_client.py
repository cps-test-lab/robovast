# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""What an operator hands a user must be installable by that user.

``vast exec cluster token`` prints the onboarding block an operator copies to somebody
who will *drive* the service: a URL, a token, and the commands to connect. It told them
``pip install robovast`` -- 88 packages and ~290 MB of simulator, dataframe and Kubernetes
machinery, to run four HTTP verbs against a service that does the actual work.

That is precisely what the distribution split exists to stop, and it is the one place a
user is told what to install by somebody who is not reading these docs. It is also the
easiest line in the codebase to regress, because ``robovast`` is the obvious thing to
type.
"""

# pylint: disable=redefined-outer-name  # the pytest fixture idiom

import re

import pytest
from click.testing import CliRunner


@pytest.fixture
def handover(monkeypatch):
    """The block `vast exec cluster token` prints, without a cluster."""
    from unittest.mock import MagicMock, patch

    from robovast.execution.cluster_execution.cli import cluster

    with patch.multiple(
            "robovast.execution.cluster_execution.service_deploy",
            existing_auth_token=MagicMock(return_value="tok-123"),
            published_url=MagicMock(return_value="https://robovast.example.org")):
        result = CliRunner().invoke(cluster, ["token", "-n", "default"])
    assert result.exit_code == 0, result.output
    return result.output


def test_it_names_the_client_distribution(handover):
    assert "pip install robovast-client" in handover, handover


def test_it_does_not_name_the_full_product(handover):
    """`pip install robovast` must not appear -- matched so `robovast-client` does not
    count as a hit for `robovast`."""
    assert not re.search(r"pip install robovast(?![-\w])", handover), handover


def test_it_still_carries_the_three_ways_to_connect(handover):
    """The point of the block is that onboarding is one copy-paste. A correct package
    name in a block missing the URL or the agent registration is no better."""
    assert "https://robovast.example.org" in handover
    assert "tok-123" in handover
    assert "claude mcp add" in handover
    assert "Browser" in handover
