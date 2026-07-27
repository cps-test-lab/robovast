# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The ``job`` table: one host record per execution job, shared by its runs.

``sysinfo.yaml`` is written once per *job*, not per run — a packed multi-config job
executes several (config, run) pairs and they all reach the same file through each run
dir's ``job`` symlink. These tests pin the two properties that follow: the record is
stored once and pointed at, and a run whose layout has no symlink still keeps its host
info rather than losing it.
"""

import json
import sqlite3

import yaml

from robovast.common.campaign_data import read_run_job, read_run_outcome
from robovast.common.store import STORE_FILENAME, CampaignStore

_SYSINFO = {"platform": {"system": "Linux"}, "cpu_name": "Intel Xeon",
            "instance_type": "n1-standard-4", "available_cpus": 4}


def _write_run(run_dir, *, job_dir=None, sysinfo=_SYSINFO):
    """A run dir with a test.xml, pointing at *job_dir* when given."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "test.xml").write_text(
        '<testsuite errors="0" failures="0" tests="1"><testcase time="1.0"/></testsuite>',
        encoding="utf-8")
    if job_dir is not None:
        job_dir.mkdir(parents=True, exist_ok=True)
        if sysinfo is not None:
            (job_dir / "sysinfo.yaml").write_text(yaml.dump(sysinfo), encoding="utf-8")
        (run_dir / "job").symlink_to(job_dir)
    elif sysinfo is not None:
        # The older layout: sysinfo beside the run, no job indirection.
        (run_dir / "sysinfo.yaml").write_text(yaml.dump(sysinfo), encoding="utf-8")


def test_runs_of_one_packed_job_share_a_single_job_row(tmp_path):
    """Two runs behind one ``job`` symlink must produce ONE job row, both pointing at it.

    This is the whole reason ``job`` is a table and not a ``run.sysinfo_json`` column: a
    per-run copy would repeat the blob and destroy the fact that the runs shared a host,
    which is what makes "did the slow runs land together?" answerable.
    """
    job = tmp_path / "_jobs" / "batch-0" / "job-3"
    _write_run(tmp_path / "cfg-a" / "0", job_dir=job)
    _write_run(tmp_path / "cfg-b" / "0", job_dir=job)

    with CampaignStore(tmp_path / STORE_FILENAME) as store:
        cid = store.create_campaign("c", {}, mode="batch")
        bid = store.open_batch(cid, 0, ".")
        for cfg in ("cfg-a", "cfg-b"):
            unit = store.record_unit(batch_id=bid, paramset_id=cfg, config_name=cfg,
                                     params={}, objectives={}, measures={},
                                     status="evaluated", result_dir=cfg)
            store.record_runs(unit, [read_run_outcome(tmp_path / cfg / "0", tmp_path)])

    conn = sqlite3.connect(tmp_path / STORE_FILENAME)
    conn.row_factory = sqlite3.Row
    jobs = conn.execute("SELECT id, job_dir, sysinfo_json FROM job").fetchall()
    assert len(jobs) == 1, "the shared job must be recorded once, not once per run"
    assert jobs[0]["job_dir"] == "_jobs/batch-0/job-3"
    assert json.loads(jobs[0]["sysinfo_json"])["cpu_name"] == "Intel Xeon"

    rows = conn.execute(
        "SELECT u.config_name, r.job_id FROM run r JOIN unit u ON r.unit_id = u.id "
        "ORDER BY u.config_name").fetchall()
    assert [r["job_id"] for r in rows] == [jobs[0]["id"]] * 2, \
        "both runs must point at the shared job row"


def test_run_without_job_symlink_keeps_its_sysinfo(tmp_path):
    """The older layout — ``sysinfo.yaml`` in the run dir, no ``job`` symlink.

    Requiring the symlink would silently drop a host record ``read_sysinfo`` can still
    find (it accepts three locations), turning populated columns into NULLs. Such a run is
    its own unit of host information, so its job_dir is the run's own directory.
    """
    _write_run(tmp_path / "cfg-a" / "0", job_dir=None)

    job_dir, sysinfo = read_run_job(tmp_path / "cfg-a" / "0", tmp_path)
    assert job_dir == "cfg-a/0"
    assert sysinfo["instance_type"] == "n1-standard-4"


def test_job_row_without_sysinfo_still_records_membership(tmp_path):
    """A job that never wrote ``sysinfo.yaml`` is still recorded: *which* job a run ran
    in is worth knowing even when the host record is missing."""
    job = tmp_path / "_jobs" / "batch-0" / "job-0"
    _write_run(tmp_path / "cfg-a" / "0", job_dir=job, sysinfo=None)

    job_dir, sysinfo = read_run_job(tmp_path / "cfg-a" / "0", tmp_path)
    assert job_dir == "_jobs/batch-0/job-0"
    assert sysinfo is None

    with CampaignStore(tmp_path / STORE_FILENAME) as store:
        cid = store.create_campaign("c", {}, mode="batch")
        assert store.upsert_job(cid, job_dir, None) is not None
        # Idempotent, and a host record arriving later fills the existing row.
        first = store.upsert_job(cid, job_dir, None)
        assert store.upsert_job(cid, job_dir, _SYSINFO) == first
        assert store.upsert_job(cid, "", None) is None, \
            "no resolvable job dir must not invent a job row"

    conn = sqlite3.connect(tmp_path / STORE_FILENAME)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT job_dir, sysinfo_json FROM job").fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["sysinfo_json"])["cpu_name"] == "Intel Xeon"


def test_job_dir_outside_the_campaign_is_not_recorded(tmp_path):
    """A ``job`` symlink escaping the campaign root belongs to no campaign path we can
    store relatively, so it must not be recorded as this campaign's job."""
    outside = tmp_path / "elsewhere" / "job-9"
    campaign = tmp_path / "campaign"
    (campaign / "cfg-a").mkdir(parents=True)
    _write_run(campaign / "cfg-a" / "0", job_dir=outside)

    job_dir, _ = read_run_job(campaign / "cfg-a" / "0", campaign)
    assert job_dir == "cfg-a/0", "falls back to the run's own dir, never a ../ path"
