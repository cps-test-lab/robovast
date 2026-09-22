# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Whether a campaign has anything for its analysis to read.

The analysis reads run directories and nothing else, so this is the question that decides
whether a stopped campaign is owed a postprocessing pass at all. What must not count as a
run is everything else a campaign directory holds before one exists.
"""

from robovast.common.campaign_data import PROBE_DIR, campaign_has_runs


def test_a_campaign_directory_that_does_not_exist_has_no_runs(tmp_path):
    """Where a campaign stopped while it was starting is: nothing was ever written."""
    assert campaign_has_runs(tmp_path / "never-created") is False


def test_reserved_and_config_directories_are_not_runs(tmp_path):
    """A campaign stopped before its first run still has its config, its records and its
    jobs' directories -- none of which is a run."""
    for reserved in ("_config", "_execution", "_transient", "_jobs", PROBE_DIR):
        (tmp_path / reserved / "0").mkdir(parents=True)
    (tmp_path / "cfg" / "_config").mkdir(parents=True)

    assert campaign_has_runs(tmp_path) is False


def test_one_run_directory_is_enough(tmp_path):
    (tmp_path / "cfg-a").mkdir()
    (tmp_path / "cfg-b" / "3").mkdir(parents=True)

    assert campaign_has_runs(tmp_path) is True


def test_a_campaign_directory_that_cannot_be_read_raises(tmp_path):
    """"No runs" and "unreadable" both skip postprocessing, so they must not be the same
    answer: a campaign whose directory cannot be read is missing its derived data, and
    nothing would say so."""
    import os

    import pytest

    campaign = tmp_path / "camp"
    (campaign / "cfg-a" / "0").mkdir(parents=True)
    campaign.chmod(0o000)
    try:
        if os.access(campaign, os.R_OK):  # root reads anything; the guard is untestable
            pytest.skip("this user can read an unreadable directory")
        with pytest.raises(PermissionError):
            campaign_has_runs(campaign)
    finally:
        campaign.chmod(0o755)
