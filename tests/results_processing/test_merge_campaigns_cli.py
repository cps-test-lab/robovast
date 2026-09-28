# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast results merge-campaigns`` reads the campaigns from a directory it is told.

There is no ambient project and no default results directory, so a missing ``--results-dir``
is a usage error answered before anything runs -- not a ``Path(None)`` deep in the merge."""

from click.testing import CliRunner

from robovast.results_processing.cli import merge_results_cmd


def test_the_results_directory_is_required(tmp_path):
    result = CliRunner().invoke(merge_results_cmd, [str(tmp_path / "merged")])
    assert result.exit_code == 2
    assert "Missing option '--results-dir'" in result.output
    assert "Traceback" not in result.output
