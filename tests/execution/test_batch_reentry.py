# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Re-entering a batch that is already partly done.

A campaign whose service process went away keeps running: its Jobs are not children of
that process, and each one's uploader delivers its results into the campaign. What the
successor must not do is plan the batch from scratch and run the finished half a second
time, overwriting results that are already correct.

The property is stated so that it is also true of a campaign starting now, which is what
keeps it from being a mode: "plan against the campaign root you were given". A fresh root
is empty, every job is pending, and the batch behaves exactly as it always did.
"""

import types

from robovast.execution.cluster_execution.kubernetes_backend import BatchJobRunner
from robovast.execution.packer import JobSpec, WorkItem


def _job(index, *items):
    return JobSpec(items=[WorkItem(config={"name": c}, run_number=r) for c, r in items],
                   index=index)


def _verdict(root, config, run):
    d = root / config / str(run)
    d.mkdir(parents=True, exist_ok=True)
    (d / "test.xml").write_text("<testsuite/>")


def test_a_fresh_campaign_has_nothing_done(tmp_path):
    """The no-op that keeps this from being a mode."""
    jobs = [_job(0, ("cfg-a", 0)), _job(1, ("cfg-a", 1))]
    assert BatchJobRunner._jobs_already_done(jobs, str(tmp_path)) == set()


def test_a_run_with_a_verdict_is_not_run_again(tmp_path):
    jobs = [_job(0, ("cfg-a", 0)), _job(1, ("cfg-a", 1))]
    _verdict(tmp_path, "cfg-a", 0)

    assert BatchJobRunner._jobs_already_done(jobs, str(tmp_path)) == {0}


def test_a_run_directory_without_a_verdict_does_not_count(tmp_path):
    """A job that started and died left a directory behind, not a result.

    ``test.xml`` is the evidence the store and the status reconstruction are both built
    from; counting anything else here would let two readers disagree about one run.
    """
    jobs = [_job(0, ("cfg-a", 0))]
    (tmp_path / "cfg-a" / "0").mkdir(parents=True)
    (tmp_path / "cfg-a" / "0" / "console.log").write_text("started, then nothing")

    assert BatchJobRunner._jobs_already_done(jobs, str(tmp_path)) == set()


def test_a_packed_job_is_done_only_when_all_of_its_runs_are(tmp_path):
    """Re-created whole, because its items share one simulator process.

    There is no way to re-enter a packed job halfway, so a partly-landed one is honestly
    pending rather than optimistically finished.
    """
    jobs = [_job(0, ("cfg-a", 0), ("cfg-a", 1), ("cfg-a", 2))]
    _verdict(tmp_path, "cfg-a", 0)
    _verdict(tmp_path, "cfg-a", 2)

    assert BatchJobRunner._jobs_already_done(jobs, str(tmp_path)) == set()

    _verdict(tmp_path, "cfg-a", 1)
    assert BatchJobRunner._jobs_already_done(jobs, str(tmp_path)) == {0}


def test_jobs_are_matched_by_their_own_config_and_run(tmp_path):
    """Not by count: a verdict under one config says nothing about another's."""
    jobs = [_job(0, ("cfg-a", 0)), _job(1, ("cfg-b", 0))]
    _verdict(tmp_path, "cfg-a", 0)

    assert BatchJobRunner._jobs_already_done(jobs, str(tmp_path)) == {0}


# -- what the batch actually creates ----------------------------------------------------

def _runner(monkeypatch, jobs, created):
    """A ``BatchJobRunner`` stubbed down to its job-creation decision."""
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.kubernetes_backend.prepare_campaign_configs",
        lambda out_dir, data, cluster=False, instance_type_command=None: None)

    r = BatchJobRunner()
    r.cluster_config = object()
    r.campaign = "camp-2026-07-17-120000"
    r.namespace = "ns"
    r.configs = [{"name": "cfg-a"}]
    r._batch_tag = "batch-0"
    r.campaign_data = {"execution": {}}
    r._ensure_k8s_initialized = lambda: None
    r.k8s_client = types.SimpleNamespace(
        create_namespaced_secret=lambda namespace, body: None)
    r._write_job_param_files = lambda out_dir, campaign_root=None: None
    r._build_jobs = lambda: jobs
    r.create_job_manifest = lambda job, total, node_figures=None: {"job": job.index}
    r.k8s_batch_client = types.SimpleNamespace(
        create_namespaced_job=lambda namespace, body: created.append(body["job"]))
    r.get_remaining_jobs = lambda names: []
    r._write_job_links = lambda cr: None
    r.cleanup_jobs = lambda campaign=None: None
    r.cleanup_pods = lambda campaign=None: None
    r._invalidate_restarted_jobs = lambda *a, **kw: None
    r._capture_image_digest = lambda label: None
    return r


def test_only_the_unfinished_jobs_are_created(monkeypatch, tmp_path):
    """The point of the whole change: finished work is not run a second time."""
    jobs = [_job(0, ("cfg-a", 0)), _job(1, ("cfg-a", 1)), _job(2, ("cfg-a", 2))]
    _verdict(tmp_path, "cfg-a", 0)
    _verdict(tmp_path, "cfg-a", 2)
    created = []

    _runner(monkeypatch, jobs, created).run_batch_in_pod(str(tmp_path), "tok")

    assert created == [1]


def test_a_fresh_batch_creates_every_job(monkeypatch, tmp_path):
    """Unchanged behaviour where nothing has run: the property has no mode in it."""
    jobs = [_job(0, ("cfg-a", 0)), _job(1, ("cfg-a", 1))]
    created = []

    _runner(monkeypatch, jobs, created).run_batch_in_pod(str(tmp_path), "tok")

    assert created == [0, 1]


def test_a_fully_finished_batch_creates_nothing(monkeypatch, tmp_path):
    """A restart that landed after the last job finished has nothing left to do."""
    jobs = [_job(0, ("cfg-a", 0))]
    _verdict(tmp_path, "cfg-a", 0)
    created = []

    _runner(monkeypatch, jobs, created).run_batch_in_pod(str(tmp_path), "tok")

    assert created == []


# -- the campaign row survives being asked for twice ------------------------------------

def test_the_campaign_row_is_idempotent_by_name(tmp_path):
    """A re-entered controller re-opens the row rather than adding a second one.

    The store it is handed is the one on the results volume, carrying the rows of an earlier
    life of this campaign. Two rows for one campaign would double every count read through
    them.
    """
    from robovast.common.store import STORE_FILENAME, CampaignStore

    store = CampaignStore(tmp_path / "camp" / STORE_FILENAME)
    first = store.create_campaign("camp-a", {"v": 1}, mode="batch",
                                  description="the original run", created_by="fred")
    again = store.create_campaign("camp-a", {"v": 1}, mode="batch")

    assert again == first
    rows = list(store._conn.execute(
        "SELECT description, created_by FROM campaign WHERE name = ?", ("camp-a",)))
    assert len(rows) == 1                             # not two rows for one campaign
    assert rows[0][0] == "the original run"           # the first write is the one kept
    assert rows[0][1] == "fred"


def test_a_different_campaign_still_gets_its_own_row(tmp_path):
    from robovast.common.store import STORE_FILENAME, CampaignStore

    store = CampaignStore(tmp_path / "camp" / STORE_FILENAME)
    a = store.create_campaign("camp-a", {}, mode="batch")
    b = store.create_campaign("camp-b", {}, mode="batch")
    assert a != b


def test_preparing_a_campaign_from_its_own_config_copies_nothing_onto_itself(tmp_path):
    """A re-entered campaign is relaunched from the ``_config/`` it froze, so the project
    tree and the campaign tree are one directory and every staging copy is a file onto
    itself. ``shutil.copy2`` refuses that outright, which failed a campaign whose Jobs were
    still running and had already delivered results."""
    from robovast.common.execution import prepare_campaign_configs

    campaign = tmp_path / "camp-2026-01-01-000000"
    config_dir = campaign / "_config"
    config_dir.mkdir(parents=True)
    (config_dir / "scenario.osc").write_text(
        "import osc.helpers\n\nscenario test_scenario:\n    timeout(10s)\n"
        "    do serial:\n        wait elapsed(1s)\n")
    (config_dir / "camp.vast").write_text("version: 5\n")
    (config_dir / "files").mkdir()
    (config_dir / "files" / "params.yaml").write_text("a: 1\n")

    data = {
        "scenario_file": str(config_dir / "scenario.osc"),
        "vast": str(config_dir / "camp.vast"),
        "_run_files": ["files/params.yaml"],
        "configs": [{"name": "cfg-a"}],
        "execution": {},
    }
    # The campaign's own directory as the project: what a resume hands the backend.
    prepare_campaign_configs(str(campaign), dict(data), cluster=True)

    assert "test_scenario" in (config_dir / "scenario.osc").read_text()
    assert (config_dir / "files" / "params.yaml").read_text() == "a: 1\n"
