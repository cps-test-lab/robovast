# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``cluster monitor``, ``cluster jobs-cleanup`` and ``cluster cleanup`` act on one
kubeconfig context -- ``-x``, else the active one -- and take no ``.vast``: a campaign
runs on its service's cluster, so a file says nothing about which cluster to watch or
clean.
"""

from unittest import mock

import pytest
from click.testing import CliRunner

from robovast.execution.cluster_execution import cli as cluster_cli


@pytest.mark.parametrize("command", [cluster_cli.monitor, cluster_cli.run_cleanup,
                                     cluster_cli.cleanup])
def test_the_verb_rejects_a_vast(command, tmp_path):
    vast = tmp_path / "project.vast"
    vast.write_text("execution: {}\n")
    result = CliRunner().invoke(command, ["--vast", str(vast)])
    assert result.exit_code == 2
    assert "No such option" in result.output and "--vast" in result.output


@pytest.mark.parametrize("args, expected", [([], "active-ctx"), (["-x", "named"], "named")])
def test_the_monitor_watches_one_context(args, expected):
    seen = []

    def _counts(namespace, context=None):
        seen.append(context)
        return {}

    with mock.patch("robovast.execution.cluster_execution.cluster_execution"
                    ".get_cluster_job_counts_per_campaign", side_effect=_counts), \
         mock.patch("robovast.execution.cluster_execution.cluster_context"
                    ".get_active_kube_context", return_value="active-ctx"), \
         mock.patch.object(cluster_cli, "_monitor_via_service", return_value=False):
        result = CliRunner().invoke(cluster_cli.monitor, ["--once", *args])
    assert result.exit_code == 0, result.output
    assert seen == [expected]
