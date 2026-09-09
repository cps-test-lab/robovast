# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""``build_include``'s ``exclude_config`` knob: the staging pod fetches ``_config/``
separately, with its executable bits restored (see ``main``'s two ``download_prefix``
calls), so the bulk fetch must not also carry it -- once for correctness (no wasted
transfer) and once because a second, unrestored copy would silently win if it landed
after the first.
"""

from robovast.execution.cluster_execution import postprocess_stage


def test_config_files_pass_by_default():
    """A caller that has not opted in keeps seeing `_config/` -- the bulk fetch is the
    only caller staging `_config/` on its own, so nothing else may lose it silently."""
    include = postprocess_stage.build_include(skip_bags=False)
    assert include("_config/campaign.vast")
    assert include("_config/postprocess.sh")


def test_config_files_are_excluded_when_asked():
    include = postprocess_stage.build_include(skip_bags=False, exclude_config=True)
    assert not include("_config/campaign.vast")
    assert not include("_config/postprocess.sh")


def test_exclude_config_does_not_touch_anything_else():
    """The exclusion is scoped to the top-level `_config/` segment, not to any path that
    merely contains the substring."""
    include = postprocess_stage.build_include(skip_bags=False, exclude_config=True)
    assert include("goal-1/0/nav_metrics.csv")
    assert include("goal-1/0/config_dump.csv"), "a run file merely containing 'config' " \
        "must not be caught by a substring match"
    assert include("_execution/execution.yaml")


def test_exclude_config_composes_with_the_existing_exclusions():
    """`_config/` exclusion must not disable the probe/job filtering already there."""
    include = postprocess_stage.build_include(skip_bags=False, batch_jobs="batch-3",
                                              exclude_config=True)
    assert not include("_config/campaign.vast")
    assert not include("_calibration/rosbag2/metadata.yaml")
    assert not include("_jobs/batch-7/job-0/rosbag2/metadata.yaml")
    assert include("_jobs/batch-3/job-0/rosbag2/metadata.yaml")
