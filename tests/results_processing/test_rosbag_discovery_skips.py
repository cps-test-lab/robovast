# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Reserved campaign directories hold no runs, so they hold no bags worth converting."""

from robovast.results_processing.data.rosbags_common import find_rosbags


def _tree(tmp_path, *rels):
    for rel in rels:
        (tmp_path / rel).mkdir(parents=True)
    return tmp_path


def test_a_probes_bags_are_not_converted(tmp_path):
    """A calibration probe is deliberately not a run, so its bag is not campaign data.

    Converting it cost a bag's work per node, and when a probe was interrupted its bag was
    never finalized -- so opening it failed the whole postprocessing step, on data nothing
    was ever going to read. Seen on a live campaign: 54 bags for a 50-run campaign, one
    error, and the error was the dead probe's.
    """
    from robovast.common.campaign_data import RESERVED_CAMPAIGN_DIRS

    root = _tree(tmp_path, "goal-1/0/rosbag2", "goal-1/1/rosbag2",
                 "_calibration/node-a/rosbag2", "_jobs/job-0/rosbag2")
    found = find_rosbags(str(root), skip_names=RESERVED_CAMPAIGN_DIRS)
    assert len(found) == 2, found
    assert all("_calibration" not in f and "_jobs" not in f for f in found)


def test_without_a_skip_list_nothing_is_pruned(tmp_path):
    """The names are passed in, not hardcoded: this module is copied into the container
    standalone and can import no definition of what "reserved" means."""
    root = _tree(tmp_path, "goal-1/0/rosbag2", "_calibration/node-a/rosbag2")
    assert len(find_rosbags(str(root))) == 2


def test_ordinary_run_directories_are_untouched(tmp_path):
    """The prune must not reach anything a campaign actually produced."""
    root = _tree(tmp_path, "goal-1/0/rosbag2", "goal-2/0/rosbag2", "_config/x")
    found = find_rosbags(str(root), skip_names=("_config", "_calibration"))
    assert len(found) == 2
