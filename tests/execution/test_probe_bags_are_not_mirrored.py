# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The cluster lane keeps probe bags out by never fetching them.

The postprocessing pod is given the campaign by a staging fetch, not a bind mount, so "do
not give the pod the probe output" is a predicate on that fetch. What the pod never receives
it cannot convert, cannot fail on, and does not pay to download.
"""

from robovast.common.campaign_data import PROBE_DIR
from robovast.execution.cluster_execution.postprocess_stage import (BAG_DIR_NAMES,
                                                                    build_include)


def test_the_probe_directory_is_never_staged():
    include = build_include(skip_bags=False)

    assert not include(f"{PROBE_DIR}/node-a/rosbag2/rosbag2_0.mcap")
    assert include("cfg-a/0/rosbag2/rosbag2_0.mcap")


def test_the_reserved_directories_are_not_excluded_as_a_set():
    """``_jobs`` holds every job's real ``logs/rosout_bag``, so excluding the reserved set
    wholesale would drop the campaign's whole /rosout record while still exiting zero."""
    include = build_include(skip_bags=False)

    assert include("_jobs/batch-0/job-1/logs/rosout_bag/rosout_bag_0.mcap")
    assert include("_execution/execution.yaml")
    assert include("campaign.db")


def test_a_campaign_with_no_conversion_is_not_given_bags_at_all():
    """The host stage never reads a bag, so a pod with no conversion container must not pay
    to fetch them -- on a large campaign they are effectively the whole download."""
    include = build_include(skip_bags=True)

    for name in BAG_DIR_NAMES:
        assert not include(f"cfg-a/0/{name}/rosbag2_0.mcap"), name
    assert not include("_jobs/batch-0/job-1/logs/rosout_bag/rosout_bag_0.mcap")
    # What that stage does read still comes down.
    assert include("cfg-a/0/out.csv")
    assert include("cfg-a/0/test.xml")
    assert include("_jobs/batch-0/job-1/resource_usage_sut.csv")
    assert include("campaign.db")
