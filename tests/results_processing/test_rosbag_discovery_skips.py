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
    from robovast.common.campaign_data import PROBE_DIR

    root = _tree(tmp_path, "goal-1/0/rosbag2", "goal-1/1/rosbag2",
                 "_calibration/node-a/rosbag2", "_jobs/batch-0/job-0/logs/rosout_bag")
    found = find_rosbags(str(root), skip_names=[PROBE_DIR])
    assert len(found) == 2, found
    assert all("_calibration" not in f for f in found)


def test_the_jobs_directory_is_not_a_thing_to_prune(tmp_path):
    """``_jobs/<batch>/<job>/logs/rosout_bag`` is each job's REAL log bag. Pruning the
    reserved set wholesale would drop every /rosout record in the campaign while the
    conversion still exited zero -- worse than the failure the prune was added to fix."""
    from robovast.common.campaign_data import PROBE_DIR

    root = _tree(tmp_path, "_jobs/batch-0/job-0/logs/rosout_bag",
                 "_calibration/node-a/logs/rosout_bag")
    found = find_rosbags(str(root), bag_dir_name="logs/rosout_bag", skip_names=[PROBE_DIR])
    assert len(found) == 1 and "_jobs" in found[0]


def test_without_a_skip_list_nothing_is_pruned(tmp_path):
    """The names are passed in, not hardcoded: this module is copied into the container
    standalone and can import no definition of what "reserved" means."""
    root = _tree(tmp_path, "goal-1/0/rosbag2", "_calibration/node-a/rosbag2")
    assert len(find_rosbags(str(root))) == 2


def test_ordinary_run_directories_are_untouched(tmp_path):
    """The prune must not reach anything a campaign actually produced."""
    root = _tree(tmp_path, "goal-1/0/rosbag2", "goal-2/0/rosbag2", "_config/x")
    found = find_rosbags(str(root), skip_names=("_calibration",))
    assert len(found) == 2
