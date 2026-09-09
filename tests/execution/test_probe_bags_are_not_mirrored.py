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


def test_staging_restores_the_job_links_the_store_cannot_hold(tmp_path, monkeypatch):
    """A campaign has every file in the object store and none of its links.

    A symlink is not an object, so ``<config>/<run>/job`` -- the way into a run's job
    artifacts -- cannot survive the round trip. Metadata generation reads sysinfo.yaml
    through that link, and failed with "sysinfo.yaml not found" on a campaign whose
    sysinfo.yaml had been staged all along. The link manifest does survive, so staging
    completes the tree from it.
    """
    import yaml

    from robovast.common.campaign_data import read_sysinfo
    from robovast.execution.cluster_execution import in_pod_storage, postprocess_stage

    campaign_id = "camp-1"
    root = tmp_path / campaign_id
    job = root / "_jobs" / "batch-0" / "job-0"
    job.mkdir(parents=True)
    (job / "sysinfo.yaml").write_text(yaml.safe_dump({"cpu_name": "Intel Xeon"}))
    (root / "cfg-a" / "0").mkdir(parents=True)
    (root / "_transient").mkdir()
    (root / "_transient" / "job_links.yaml").write_text(
        yaml.safe_dump({"cfg-a/0/job": "../../_jobs/batch-0/job-0"}))

    class _Store:
        def download_prefix(self, *_a, **_kw):
            return 0  # the tree above stands in for what a real fetch would have written

    monkeypatch.setattr(postprocess_stage, "cluster_config_from_env", lambda: object())
    monkeypatch.setattr(in_pod_storage, "campaign_storage_location",
                        lambda cfg, cid: ("b", "p/"))
    monkeypatch.setattr(in_pod_storage, "storage_client_for", lambda cfg: _Store())
    monkeypatch.setenv(postprocess_stage.ENV_CAMPAIGN_ID, campaign_id)
    monkeypatch.setenv(postprocess_stage.ENV_STAGE_DEST, str(tmp_path))

    assert postprocess_stage.main() == 0
    # The link exists, and what reads through it now resolves.
    assert (root / "cfg-a" / "0" / "job").is_symlink()
    assert read_sysinfo(root / "cfg-a" / "0")["cpu_name"] == "Intel Xeon"


def test_the_log_this_attempt_writes_is_not_staged_back_into_it():
    """The conversion APPENDS to `_execution/postprocessing.log`, so a copy of the previous
    attempt's would become the head of this attempt's log -- and did: a postprocess whose
    conversion failed presented, as its own account, the image-pull failure of the attempt
    before it. It is also where the running Job's log is published, so staging it back would
    fold this attempt's own head into itself.
    """
    from robovast.execution.cluster_execution.postprocess_stage import NOT_STAGED_LOG

    for skip_bags in (False, True):
        include = build_include(skip_bags=skip_bags)

        assert not include(NOT_STAGED_LOG), skip_bags
        # Its neighbours in the same directory are still needed.
        assert include("_execution/execution.yaml")
        assert include("_execution/interventions.json")


def test_the_finished_log_sections_are_not_staged_either():
    """Archived sections of the campaign log are immutable history read only by whoever
    streams it. Nothing in the pod produces or consumes one, so staging them would transfer
    bytes it cannot use and hand the tail upload a second copy to publish.
    """
    from robovast.execution.cluster_execution.postprocess_stage import not_staged_sections

    include = build_include(skip_bags=False)

    assert not include(f"{not_staged_sections()}0001-postprocessing.log")
    assert not include(f"{not_staged_sections()}0002-share.log")
    assert include("_execution/execution.yaml")


# -- one batch's jobs, for a per-batch pod ------------------------------------


def test_only_the_named_batchs_jobs_are_staged():
    """Every batch's bags sit under the same campaign prefix, and bags are the bulk.

    A search converts once per batch, so without this batch N pays to download batches
    0..N-1 as well -- the transfer grows with the campaign while the work per batch does
    not.
    """
    include = build_include(skip_bags=False, batch_jobs="batch-3")

    assert include("_jobs/batch-3/job-1/rosbag2/rosbag2_0.mcap")
    assert not include("_jobs/batch-0/job-1/rosbag2/rosbag2_0.mcap")
    assert not include("_jobs/batch-2/job-7/logs/rosout_bag/rosout_bag_0.mcap")


def test_a_repetitions_group_keeps_the_batch_its_runs_link_to():
    """A run's ``job`` symlink points at ``_jobs/<batch>/reps-<n>/job-<m>`` exactly.

    Matching on the first segment alone would stage the whole of ``batch-3`` -- every
    repetitions group of it -- which is the transfer this exists to avoid. Matching too
    narrowly would leave a staged run's link dangling, which reads downstream as a run
    whose artifacts were lost rather than one this pod was never given.
    """
    include = build_include(skip_bags=False, batch_jobs="batch-3/reps-5")

    assert include("_jobs/batch-3/reps-5/job-0/rosbag2/rosbag2_0.mcap")
    assert not include("_jobs/batch-3/reps-4/job-0/rosbag2/rosbag2_0.mcap")


def test_everything_outside_the_jobs_tree_is_untouched_by_the_narrowing():
    """Only ``_jobs/`` is narrowed. A run directory holds its verdict, its parameters and
    the symlink -- kilobytes -- and the pod's own derivation reads them for every run it
    scores, so narrowing them too would cost correctness for nothing."""
    include = build_include(skip_bags=False, batch_jobs="batch-3")

    assert include("cfg-a/0/test.xml")
    assert include("cfg-a/0/nav_metrics.csv")
    assert include("_config/campaign.vast")
    assert include("_execution/execution.yaml")
    assert include("campaign.db")


def test_no_batch_named_stages_every_batch():
    """The campaign-level pass derives the whole campaign, so narrowing would hide it."""
    include = build_include(skip_bags=False)

    assert include("_jobs/batch-0/job-1/rosbag2/rosbag2_0.mcap")
    assert include("_jobs/batch-9/job-1/rosbag2/rosbag2_0.mcap")
