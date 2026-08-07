# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""LocalTransport.list_jobs / get_job_log — the mode-1/2 (Docker) job view.

Local runs land on disk as ``<campaign>/<config>/<run>/``, but the run's console is
tee'd to the JOB's artifact dir (``_jobs[/<batch>]/job-<N>/logs/system.log``), not to
``<config>/<run>/logs/`` — that directory is created and left empty. The mapping from
run to job dir is ``_transient/job_links.yaml``. A "job" is a run:
``completed``/``failed`` by its ``test.xml``, ``running`` when the campaign is still
live and it has none yet. The per-job log is that ``system.log`` read from a byte
offset — no cluster needed.
"""

from pathlib import Path

import pytest
import yaml

from robovast.common.execution import JOB_LINKS_MANIFEST, job_artifact_rel
from robovast.execution.control_server import ControllerState
from robovast.service.client import LocalTransport
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore


@pytest.fixture
def transport(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "robovast.common.cli.project_config.ProjectConfig.load",
        staticmethod(lambda *a, **k: None))
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    return LocalTransport(store=store)


_PASS_XML = ('<testsuite errors="0" failures="0" tests="1">'
             '<testcase time="1.0"/></testsuite>')
_FAIL_XML = ('<testsuite errors="0" failures="1" tests="1">'
             '<testcase time="1.0"/></testsuite>')


def _run(campaign_dir: Path, config: str, run: str, *, xml=None, log=None,
         job_index=0, job_prefix="batch-0") -> Path:
    """Materialise one run exactly the way a local campaign does.

    The empty ``<config>/<run>/logs/`` is created deliberately: the runner makes it and
    never writes there, so a reader looking in it sees a silently blank log.
    """
    run_dir = campaign_dir / config / run
    (run_dir / "logs").mkdir(parents=True)
    if xml is not None:
        (run_dir / "test.xml").write_text(xml)
    if log is not None:
        job_rel = job_artifact_rel(job_index, job_prefix)
        (campaign_dir / "_jobs" / job_rel / "logs").mkdir(parents=True, exist_ok=True)
        (campaign_dir / "_jobs" / job_rel / "logs" / "system.log").write_text(log)
        manifest = campaign_dir / "_transient" / JOB_LINKS_MANIFEST
        manifest.parent.mkdir(parents=True, exist_ok=True)
        links = yaml.safe_load(manifest.read_text()) if manifest.is_file() else {}
        links[f"{config}/{run}/job"] = f"../../_jobs/{job_rel}"
        manifest.write_text(yaml.safe_dump(links))
    return run_dir


def test_list_jobs_classifies_runs(transport):
    cid = "campaign-2026-07-17-120000"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "0", xml=_PASS_XML)
    _run(cdir, "cfgA", "1", xml=_FAIL_XML)
    _run(cdir, "cfgB", "0")  # no test.xml, campaign not live → failed/incomplete

    resp = transport.list_jobs(cid)
    by_name = {j.job_name: j.status for j in resp.jobs}
    assert by_name == {"cfgA/0": "completed", "cfgA/1": "failed", "cfgB/0": "failed"}
    assert resp.counts.completed == 1 and resp.counts.failed == 2
    assert resp.counts.total == 3
    # display name is human friendly
    assert any(j.display_name == "cfgA · run 0" for j in resp.jobs)


def test_list_jobs_ignores_reserved_dirs(transport):
    cid = "campaign-2026-07-17-121000"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "0", xml=_PASS_XML)
    for reserved in ("_config", "_execution", "_transient"):
        (cdir / reserved / "0").mkdir(parents=True)

    resp = transport.list_jobs(cid)
    assert [j.job_name for j in resp.jobs] == ["cfgA/0"]


def test_get_job_log_reads_system_log_from_offset(transport):
    cid = "campaign-2026-07-17-122000"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "0", xml=_PASS_XML, log="line1\nline2\n")

    chunk = transport.get_job_log(cid, "cfgA/0")
    assert chunk.text == "line1\nline2\n"
    assert chunk.next_offset == len(b"line1\nline2\n")
    assert chunk.eof is True  # run finished (has test.xml) and campaign not live

    tail = transport.get_job_log(cid, "cfgA/0", offset=6)
    assert tail.text == "line2\n"


def test_get_job_log_ignores_empty_run_dir_logs(transport):
    """The run dir's own ``logs/`` is a decoy — reading it returns a blank log.

    Regression: the job log was served from ``<config>/<run>/logs/system.log``, which
    the runner creates but never writes, so the web UI showed an empty job log while
    the same output was visible in the campaign log.
    """
    cid = "campaign-2026-07-17-125000"
    cdir = transport._campaigns_root() / cid
    run_dir = _run(cdir, "cfgA", "0", xml=_PASS_XML, log="real\n")
    assert not (run_dir / "logs" / "system.log").exists()  # decoy stays empty
    assert transport.get_job_log(cid, "cfgA/0").text == "real\n"


def test_get_job_log_resolves_search_mode_batch_tag(transport):
    """Search mode tags a batch ``batch-<i>/reps-<n>``, nesting the job dir a level deeper."""
    cid = "campaign-2026-07-17-126000"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "0", xml=_PASS_XML, log="searched\n", job_prefix="batch-3/reps-5")
    assert (cdir / "_jobs" / "batch-3" / "reps-5" / "job-0" / "logs" / "system.log").is_file()
    assert transport.get_job_log(cid, "cfgA/0").text == "searched\n"


def test_get_job_log_without_manifest_entry_is_blank_not_wrong(transport):
    """Before the manifest exists (startup race) the log is empty, never another job's."""
    cid = "campaign-2026-07-17-127000"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "0", xml=_PASS_XML)  # no log → no manifest entry
    chunk = transport.get_job_log(cid, "cfgA/0")
    assert chunk.text == ""


def test_get_job_log_unknown_job_raises(transport):
    cid = "campaign-2026-07-17-123000"
    (transport._campaigns_root() / cid).mkdir(parents=True)
    with pytest.raises(KeyError):
        transport.get_job_log(cid, "nope/0")


def test_get_job_log_rejects_path_traversal(transport):
    cid = "campaign-2026-07-17-124000"
    (transport._campaigns_root() / cid).mkdir(parents=True)
    with pytest.raises(KeyError):
        transport.get_job_log(cid, "../../etc")


# -- the /usage jobs tally --------------------------------------------------
#
# The sidebar's jobs meter used to be hard-wired to "0/0" on this lane: the fields were
# documented as belonging to backends that run Kubernetes Jobs, but local *does* execute
# scenario runs — sequentially, as containers. The counts come off the controller
# snapshot, not off disk: list_jobs above calls a run with no test.xml "running" while the
# campaign is live, so a run that died without writing one would be reported as still
# executing for the rest of the campaign.


def _live(transport, cid, phase, *, total=0, completed=0):
    """Register a campaign in the phase and run-progress a live controller would report."""
    from robovast.service.local_transport import _LocalCampaign
    state = ControllerState(phase=phase, runs={"total": total, "completed": completed})
    entry = _LocalCampaign(campaign_id=cid, results_dir=str(transport._campaigns_root() / cid),
                           state=state)
    transport._campaigns[cid] = entry
    # The 10s memoisation in resource_usage would otherwise serve an earlier sample.
    transport._usage_cache = None
    return entry


def test_usage_tally_reports_the_executing_run_and_the_rest_pending(transport):
    _live(transport, "campaign-2026-07-17-130000", "running", total=5, completed=2)
    usage = transport.resource_usage()
    # 0-or-1 running by construction: the lane is single-flight, which is also what
    # parallel_runs advertises — a consumer sizing a sweep must see the same story twice.
    assert (usage.jobs_running, usage.jobs_pending) == (1, 2)
    assert usage.parallel_runs is False


def test_usage_tally_counts_a_batch_that_has_not_started_executing(transport):
    """Before the ``running`` phase nothing executes, but the batch is still owed."""
    _live(transport, "campaign-2026-07-17-131000", "building", total=5)
    usage = transport.resource_usage()
    assert (usage.jobs_running, usage.jobs_pending) == (0, 5)


def test_usage_tally_ignores_finished_campaigns(transport):
    """A terminal campaign is past work: it counts in neither bucket, which is what
    lets the UI hide the row instead of showing a full bar forever."""
    _live(transport, "campaign-2026-07-17-132000", "finished", total=5, completed=5)
    usage = transport.resource_usage()
    assert (usage.jobs_running, usage.jobs_pending) == (0, 0)


def test_usage_tally_is_zero_with_no_campaigns(transport):
    usage = transport.resource_usage()
    assert (usage.jobs_running, usage.jobs_pending) == (0, 0)
