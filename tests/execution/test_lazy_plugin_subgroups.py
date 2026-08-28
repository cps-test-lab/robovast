# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast cluster`` and ``vast service`` list their operator verbs without importing them.

Those verbs need the Kubernetes client. Registering either the ordinary way -- as a
``@group.command()`` in a module the group's own file imports -- puts it in the import path
of every ``vast`` invocation, because ``load_plugins()`` imports each
``robovast.cli_plugins`` entry point every time. That is what made ``vast login`` pay for
Kubernetes.

Click asks two separate questions: which names can I offer (``list_commands``) and which one
am I about to run (``get_command``). Answering the first from entry-point *metadata* keeps
the listing free; only the chosen subcommand is loaded. ``LazyPluginGroup`` does this for
``cluster`` (group ``robovast.cluster_plugins``) and for ``service``
(``robovast.service_plugins``).

The other half is degradation. Both groups span two distributions **by design** -- they are
named after the object they act on, not after what you installed -- so an install without
the cluster package must be short the operator verbs and keep the ones a driver uses, rather
than losing the group. That is the property worth asserting.

Launching is not part of any of this: ``vast workspace run`` is the client's own verb with
no entry point in between, covered in ``tests/service/test_client_needs_no_core.py``.
"""

import sys

import pytest
from click.testing import CliRunner

from robovast.client import cluster_cli, service_cli


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


def test_the_operator_verbs_are_listed_without_being_imported():
    """They are the cluster package's, reached through ``robovast.cluster_plugins``."""
    result = CliRunner().invoke(cluster_cli.cluster, ["--help"])
    assert result.exit_code == 0
    assert "setup" in result.output


def test_the_cluster_group_survives_the_cluster_package_being_absent(cluster_absent):
    """The split's payoff: no kubeconfig, still a usable ``vast cluster``."""
    result = CliRunner().invoke(cluster_cli.cluster, ["--help"])
    assert result.exit_code == 0, result.output


def test_the_client_half_survives_the_cluster_package_being_absent(cluster_absent):
    """``store-cleanup`` goes through the service, so it must not need the lane."""
    result = CliRunner().invoke(cluster_cli.cluster, ["store-cleanup", "--help"])
    assert result.exit_code == 0, result.output


def test_the_service_group_keeps_its_client_half(cluster_absent):
    """``service`` spans two distributions; without the cluster one it is short verbs,
    not broken."""
    result = CliRunner().invoke(service_cli.service, ["--help"])
    assert result.exit_code == 0, result.output
    for verb in ("log", "info", "resources", "restart"):
        assert CliRunner().invoke(
            service_cli.service, [verb, "--help"]).exit_code == 0, verb


def test_an_unloadable_operator_verb_warns_rather_than_crashing(cluster_absent):
    """``setup`` is the cluster package's, so it degrades -- announced, not by traceback."""
    result = CliRunner().invoke(cluster_cli.cluster, ["setup", "--help"])
    assert "could not be loaded" in result.output
    assert result.exit_code != 0  # the subcommand is unavailable, but nothing exploded


def test_launching_is_not_under_any_of_these(cluster_absent):
    """``run`` moved to ``vast workspace run``; no old path may answer to it."""
    for group in (cluster_cli.cluster, service_cli.service):
        assert CliRunner().invoke(group, ["run", "--help"]).exit_code != 0
