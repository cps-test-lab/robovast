# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""What a postprocessing pod is given of a campaign: ``campaign_archive.stage_include``.

The pod is handed the campaign as one tar stream from the service's data plane, so "do not
give the pod X" is a predicate on that stream, decided where the bytes are. What the pod
never receives it cannot convert, cannot fail on, and does not pay to download -- and the
same predicate sizes the disk the pod reserves, so the two cannot disagree.
"""

from robovast.common.campaign_data import PROBE_DIR
from robovast.execution.campaign_archive import (BAG_DIR_NAMES, NOT_STAGED_LOG,
                                                 campaign_source_bytes, stage_include)


def _file(include, rel):
    return include(rel, False)


def test_the_probe_directory_is_never_staged():
    """A calibration probe is deliberately not a run, so its bag is not campaign data:
    converting it costs a bag's work per node, and an interrupted probe's unfinalized bag
    fails a step on something nothing reads."""
    include = stage_include(skip_bags=False)

    assert not include(PROBE_DIR, True)
    assert not _file(include, f"{PROBE_DIR}/node-a/rosbag2/rosbag2_0.mcap")
    assert _file(include, "cfg-a/0/rosbag2/rosbag2_0.mcap")


def test_the_reserved_directories_are_not_excluded_as_a_set():
    """``_jobs`` holds every job's real ``logs/rosout_bag``, so excluding the reserved set
    wholesale would drop the campaign's whole /rosout record while still exiting zero."""
    include = stage_include(skip_bags=False)

    assert _file(include, "_jobs/batch-0/job-1/logs/rosout_bag/rosout_bag_0.mcap")
    assert _file(include, "_execution/execution.yaml")
    assert _file(include, "campaign.db")


def test_a_campaign_with_no_conversion_is_not_given_bags_at_all():
    """The host stage never reads a bag, so a pod with no conversion container must not pay
    to fetch them -- on a large campaign they are effectively the whole download."""
    include = stage_include(skip_bags=True)

    for name in BAG_DIR_NAMES:
        assert not include(f"cfg-a/0/{name}", True), name
        assert not _file(include, f"cfg-a/0/{name}/rosbag2_0.mcap"), name
    assert not _file(include, "_jobs/batch-0/job-1/logs/rosout_bag/rosout_bag_0.mcap")
    # What that stage does read still comes down.
    assert _file(include, "cfg-a/0/out.csv")
    assert _file(include, "cfg-a/0/test.xml")
    assert _file(include, "_jobs/batch-0/job-1/resource_usage_sut.csv")
    assert _file(include, "campaign.db")


def test_the_log_this_attempt_writes_is_not_staged_back_into_it():
    """The conversion APPENDS to `_execution/postprocessing.log`, so a copy of the previous
    attempt's would become the head of this attempt's log. It is also where the running
    Job's log is published, so staging it back would fold this attempt's own head into
    itself.
    """
    for skip_bags in (False, True):
        include = stage_include(skip_bags=skip_bags)

        assert not _file(include, NOT_STAGED_LOG), skip_bags
        # Its neighbours in the same directory are still needed.
        assert _file(include, "_execution/execution.yaml")
        assert _file(include, "_execution/interventions.json")


def test_the_finished_log_sections_are_not_staged_either():
    """Archived sections of the campaign log are immutable history read only by whoever
    streams it. Nothing in the pod produces or consumes one, so staging them would transfer
    bytes it cannot use and hand the delivery a second copy to send back.
    """
    from robovast.common.campaign_logs import SECTIONS_DIR

    include = stage_include(skip_bags=False)

    assert not include(f"_execution/{SECTIONS_DIR}", True)
    assert not _file(include, f"_execution/{SECTIONS_DIR}/0001-postprocessing.log")
    assert not _file(include, f"_execution/{SECTIONS_DIR}/0002-share.log")
    assert _file(include, "_execution/execution.yaml")


# -- one batch's jobs, for a per-batch pod ------------------------------------


def test_only_the_named_batchs_jobs_are_staged():
    """Every batch's bags sit under the same campaign tree, and bags are the bulk.

    A search converts once per batch, so without this batch N pays to download batches
    0..N-1 as well -- the transfer grows with the campaign while the work per batch does
    not.
    """
    include = stage_include(skip_bags=False, batch_jobs="batch-3")

    assert _file(include, "_jobs/batch-3/job-1/rosbag2/rosbag2_0.mcap")
    assert not _file(include, "_jobs/batch-0/job-1/rosbag2/rosbag2_0.mcap")
    assert not _file(include, "_jobs/batch-2/job-7/logs/rosout_bag/rosout_bag_0.mcap")
    # A sibling batch is pruned whole; the directories on the way to the wanted one are
    # kept, or the wanted one could never be reached.
    assert include("_jobs", True)
    assert include("_jobs/batch-3", True)
    assert not include("_jobs/batch-0", True)


def test_a_repetitions_group_keeps_the_batch_its_runs_link_to():
    """A run's ``job`` symlink points at ``_jobs/<batch>/reps-<n>/job-<m>`` exactly.

    Matching on the first segment alone would stage the whole of ``batch-3`` -- every
    repetitions group of it -- which is the transfer this exists to avoid. Matching too
    narrowly would leave a staged run's link dangling, which reads downstream as a run
    whose artifacts were lost rather than one this pod was never given.
    """
    include = stage_include(skip_bags=False, batch_jobs="batch-3/reps-5")

    assert _file(include, "_jobs/batch-3/reps-5/job-0/rosbag2/rosbag2_0.mcap")
    assert not _file(include, "_jobs/batch-3/reps-4/job-0/rosbag2/rosbag2_0.mcap")
    assert include("_jobs/batch-3", True)
    assert not include("_jobs/batch-3/reps-4", True)


def test_everything_outside_the_jobs_tree_is_untouched_by_the_narrowing():
    """Only ``_jobs/`` is narrowed. A run directory holds its verdict, its parameters and
    the symlink -- kilobytes -- and the pod's own derivation reads them for every run it
    scores, so narrowing them too would cost correctness for nothing."""
    include = stage_include(skip_bags=False, batch_jobs="batch-3")

    assert _file(include, "cfg-a/0/test.xml")
    assert _file(include, "cfg-a/0/nav_metrics.csv")
    assert _file(include, "_config/campaign.vast")
    assert _file(include, "_execution/execution.yaml")
    assert _file(include, "campaign.db")


def test_no_batch_named_stages_every_batch():
    """The campaign-level pass derives the whole campaign, so narrowing would hide it."""
    include = stage_include(skip_bags=False)

    assert _file(include, "_jobs/batch-0/job-1/rosbag2/rosbag2_0.mcap")
    assert _file(include, "_jobs/batch-9/job-1/rosbag2/rosbag2_0.mcap")


# -- the same selection sizes the pod's disk ----------------------------------


def _campaign(tmp_path):
    root = tmp_path / "camp"
    for rel, size in (("cfg/0/rosbag2/b.mcap", 4096), ("cfg/0/out.csv", 512),
                      ("_jobs/batch-3/j/rosbag2/b.mcap", 2048),
                      ("_jobs/batch-0/j/rosbag2/b.mcap", 1024),
                      (f"{PROBE_DIR}/node-a/rosbag2/b.mcap", 8192)):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
    # A `job` link is a member, not a path to follow: it contributes nothing.
    (root / "cfg" / "0" / "job").symlink_to("../../_jobs/batch-3/j")
    return root


def test_the_size_walk_applies_the_same_selection_as_the_archive(tmp_path):
    """The pod's disk request comes from this figure, so it has to describe what the
    stream carries: what is pruned from the archive is pruned from the sum."""
    root = _campaign(tmp_path)

    everything = campaign_source_bytes(str(root), include=stage_include())
    assert everything == 4096 + 512 + 2048 + 1024      # the probe is never staged

    no_bags = campaign_source_bytes(str(root), include=stage_include(skip_bags=True))
    assert no_bags == 512

    one_batch = campaign_source_bytes(
        str(root), include=stage_include(batch_jobs="batch-3"))
    assert one_batch == 4096 + 512 + 2048


def test_without_a_selection_the_walk_counts_the_whole_tree(tmp_path):
    root = _campaign(tmp_path)
    assert campaign_source_bytes(str(root)) == 4096 + 512 + 2048 + 1024 + 8192
