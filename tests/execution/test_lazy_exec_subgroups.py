# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast exec`` lists its subcommands without importing them, at two levels.

``vast exec local`` needs Docker and the config schema; the operator half of ``vast exec
cluster`` needs the Kubernetes client. Registering either the ordinary way -- as a
``@group()`` in a module the group's own file imports -- put it in the import path of every
``vast`` invocation, because ``load_plugins()`` imports each ``robovast.cli_plugins`` entry
point every time. That is what made ``vast login`` pay for Docker and Kubernetes.

Click asks two separate questions: which names can I offer (``list_commands``) and which
one am I about to run (``get_command``). Answering the first from entry-point *metadata*
keeps the listing free; only the chosen subcommand is loaded. ``LazyPluginGroup`` does this
for ``exec`` (group ``robovast.exec_plugins``) and again for ``exec cluster`` (group
``robovast.cluster_plugins``).

The other half is degradation, and the boundary moved when the launch verbs became the
client's. Blocking the cluster package used to take ``vast exec cluster`` with it, because
the group itself lived there. Now the group and ``run``/``stop``/``stop-job``/``log``/
``download-cleanup`` are in ``robovast-client``, so an install without the cluster package
is short the *operator* verbs and keeps the ones a driver actually uses -- which is the
whole point of the split, and therefore the thing worth asserting.
"""

import sys

import pytest
from click.testing import CliRunner

from robovast.client import exec_cli


class _BlockCluster:
    """Simulate the cluster package being unimportable."""

    # None is how a MetaPathFinder declines a module
    def find_spec(self, name, path=None, target=None):  # pylint: disable=useless-return
        if name == "robovast.execution.cluster_execution.cli":
            raise ImportError("cluster package not installed")
        return None


@pytest.fixture
def cluster_absent():
    finder = _BlockCluster()
    sys.meta_path.insert(0, finder)
    sys.modules.pop("robovast.execution.cluster_execution.cli", None)
    try:
        yield
    finally:
        sys.meta_path.remove(finder)


def test_the_cluster_subgroup_is_listed():
    result = CliRunner().invoke(exec_cli.execution, ["--help"])
    assert result.exit_code == 0
    assert "cluster" in result.output


def test_the_local_lane_is_listed_without_being_imported():
    """``local`` is the core's, reached through ``robovast.exec_plugins``."""
    result = CliRunner().invoke(exec_cli.execution, ["--help"])
    assert result.exit_code == 0
    assert "local" in result.output


def test_a_sibling_command_does_not_load_the_cluster_subgroup(cluster_absent):
    """The property that matters: a sibling of `cluster` must not pay for the cluster
    lane. With the cluster module unimportable, this still has to work.

    `wait` used to be the example here; it is now the client's top-level `vast wait`, so
    it is no longer a sibling and could not test this if it tried.
    """
    result = CliRunner().invoke(exec_cli.execution, ["stop-container", "--help"])
    assert result.exit_code == 0, result.output


def test_the_cluster_group_survives_the_cluster_package_being_absent(cluster_absent):
    """The split's payoff: no kubeconfig, still a usable ``exec cluster``."""
    result = CliRunner().invoke(exec_cli.execution, ["cluster", "--help"])
    assert result.exit_code == 0, result.output


def test_the_launch_verb_survives_the_cluster_package_being_absent(cluster_absent):
    """``run`` is the verb the client audience installs for; it must not need the lane."""
    result = CliRunner().invoke(exec_cli.execution, ["cluster", "run", "--help"])
    assert result.exit_code == 0, result.output


def test_an_unloadable_operator_verb_warns_rather_than_crashing(cluster_absent):
    """``setup`` is the cluster package's, so it degrades -- announced, not by traceback."""
    result = CliRunner().invoke(exec_cli.execution, ["cluster", "setup", "--help"])
    assert "could not be loaded" in result.output
    assert result.exit_code != 0  # the subcommand is unavailable, but nothing exploded


def test_the_group_still_works_with_the_subgroup_unloadable(cluster_absent):
    """Short a subcommand, not broken."""
    result = CliRunner().invoke(exec_cli.execution, ["--help"])
    assert result.exit_code == 0


def test_an_unknown_subcommand_is_still_an_ordinary_error():
    """The lazy lookup must not turn a typo into something stranger."""
    result = CliRunner().invoke(exec_cli.execution, ["clsuter"])
    assert result.exit_code != 0
    assert "No such command" in result.output


def test_an_unknown_cluster_subcommand_is_still_an_ordinary_error():
    """Same, one level down: the second lazy group must not swallow a typo either."""
    result = CliRunner().invoke(exec_cli.execution, ["cluster", "steup"])
    assert result.exit_code != 0
    assert "No such command" in result.output
