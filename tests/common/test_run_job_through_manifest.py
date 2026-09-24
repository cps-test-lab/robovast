# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A run's job is found through the campaign's job-link manifest, not its ``job`` symlink.

The backend writes ``_transient/job_links.yaml`` before the first job starts, and it names
every run's job. The ``<config>/<run>/job`` symlinks are made from it only once a batch
ends, so a batch that was stopped leaves its finished runs -- complete, with a verdict and
a host record in their job's directory -- without one. Every reader of a run's job has to
find it anyway: the campaign's metadata record, the ``job`` table and the index's host
columns are all built from it.
"""

import sqlite3

import yaml

from robovast.common.campaign_data import (read_run_job, read_run_outcome, read_run_outcomes,
                                           read_sysinfo, run_job_dir)
from robovast.common.execution import JOB_LINKS_MANIFEST
from robovast.common.store import STORE_FILENAME, CampaignStore
from robovast.results_processing.metadata import MetadataGenerator

_SYSINFO = {"cpu_name": "Intel Xeon", "instance_type": "n1-standard-4", "available_cpus": 4}


def _stopped_batch(tmp_path, runs=2):
    """A campaign whose batch was stopped after its runs finished: every run has a verdict
    and its job a host record, the manifest names them, and no ``job`` symlink exists."""
    root = tmp_path / "camp-2026-09-21-120000"
    (root / "_execution").mkdir(parents=True)
    (root / "_transient").mkdir(parents=True)
    (root / "_transient" / "configurations.yaml").write_text(yaml.safe_dump({
        "_run_files": [], "metadata": {"name": "camp"},
        "configs": [{"name": "cfg"}], "created_at": "2026-09-21T12:00:00"}))
    (root / "_execution" / "execution.yaml").write_text(yaml.safe_dump({
        "runs": runs, "execution_type": "cluster"}))
    links = {}
    for number in range(runs):
        run = root / "cfg" / str(number)
        run.mkdir(parents=True)
        (run / "test.xml").write_text(
            '<?xml version="1.0"?><testsuite tests="1" failures="0" time="1.5" '
            'timestamp="2026-09-21T12:00:00"><testcase name="scenario" time="1.5"/>'
            '</testsuite>')
        job = root / "_jobs" / "batch-0" / f"job-{number}"
        job.mkdir(parents=True)
        (job / "sysinfo.yaml").write_text(yaml.safe_dump(_SYSINFO))
        links[f"cfg/{number}/job"] = f"../../_jobs/batch-0/job-{number}"
    (root / "_transient" / JOB_LINKS_MANIFEST).write_text(yaml.safe_dump(links))
    for number in range(runs):
        assert not (root / "cfg" / str(number) / "job").exists()
    return root


def test_the_manifest_names_the_job_a_stopped_batch_never_linked(tmp_path):
    root = _stopped_batch(tmp_path)
    run = root / "cfg" / "0"

    assert run_job_dir(run, root) == root / "_jobs" / "batch-0" / "job-0"
    assert read_sysinfo(run, root)["cpu_name"] == "Intel Xeon"
    job_dir, sysinfo = read_run_job(run, root)
    assert job_dir == "_jobs/batch-0/job-0"
    assert sysinfo["instance_type"] == "n1-standard-4"


def test_a_stopped_batchs_runs_keep_their_job_rows(tmp_path):
    """Without the manifest each run became its own job with no host record: the
    campaign lost which machine its runs ran on, with nothing reporting the loss."""
    root = _stopped_batch(tmp_path)
    with CampaignStore(root / STORE_FILENAME) as store:
        cid = store.create_campaign("c", {}, mode="batch")
        bid = store.open_batch(cid, 0, ".")
        unit = store.record_unit(batch_id=bid, paramset_id="cfg", config_name="cfg",
                                 params={}, objectives={}, measures={},
                                 status="evaluated", result_dir="cfg")
        store.record_runs(unit, read_run_outcomes(root / "cfg", root))

    conn = sqlite3.connect(root / STORE_FILENAME)
    rows = conn.execute("SELECT job_dir, sysinfo_json IS NOT NULL FROM job "
                        "ORDER BY job_dir").fetchall()
    assert rows == [("_jobs/batch-0/job-0", 1), ("_jobs/batch-0/job-1", 1)]


def test_a_stopped_batchs_campaign_can_describe_itself(tmp_path):
    """The metadata record raised on the first run whose symlink was missing, so the
    campaign could not be marked postprocessed although its derived data was complete."""
    root = _stopped_batch(tmp_path)

    metadata = MetadataGenerator(root).generate_metadata()

    results = metadata["configurations"][0]["test_results"]
    assert [r["sysinfo"]["cpu_name"] for r in results] == ["Intel Xeon", "Intel Xeon"]


def test_an_outcome_can_share_one_read_of_the_manifest(tmp_path):
    root = _stopped_batch(tmp_path)
    links = {"cfg/0/job": "../../_jobs/batch-0/job-1"}

    outcome = read_run_outcome(root / "cfg" / "0", root, links=links)

    assert outcome["job_dir"] == "_jobs/batch-0/job-1", "the manifest given is the one used"
