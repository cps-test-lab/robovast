# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast exec`` lists its subgroups without importing them.

``vast exec cluster`` is 1,200 lines living in the cluster package. Registering it the
ordinary way -- ``@execution.group()`` in the shared CLI module -- put it in the import
path of every ``vast`` invocation, because ``load_plugins()`` imports that module each
time. That is what made ``vast login`` pay for the Kubernetes client.

Click asks two separate questions: which names can I offer (``list_commands``) and which
one am I about to run (``get_command``). Answering the first from entry-point *metadata*
keeps the listing free; only the chosen subgroup is loaded.

The other half is degradation. An install without the cluster package should be short a
subcommand, not broken — the same rule ``load_plugins()`` follows for a missing plugin.
"""

import sys

import pytest
from click.testing import CliRunner

from robovast.execution.execution_utils import cli as exec_cli


class _BlockCluster:
    """Simulate the cluster package being unimportable."""

    def find_spec(self, name, path=None, target=None):
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


def test_a_sibling_command_does_not_load_the_cluster_subgroup(cluster_absent):
    """The property that matters: a sibling of `cluster` must not pay for the cluster
    lane. With the cluster module unimportable, this still has to work.

    `wait` used to be the example here; it is now the client's top-level `vast wait`, so
    it is no longer a sibling and could not test this if it tried.
    """
    result = CliRunner().invoke(exec_cli.execution, ["stop-container", "--help"])
    assert result.exit_code == 0, result.output


def test_an_unloadable_subgroup_warns_rather_than_crashing(cluster_absent):
    result = CliRunner().invoke(exec_cli.execution, ["cluster", "--help"])
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
