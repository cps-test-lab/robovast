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

import json
import threading
from pathlib import Path

import pytest
import yaml

from robovast.common.config import SIMULATION_CONTAINER

from robovast.common.execution import JOB_LINKS_MANIFEST, job_artifact_rel
from robovast.execution.control_server import ControllerState
from robovast.service.client import LocalTransport
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore


@pytest.fixture
def transport(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "robovast.client.project_config.ProjectConfig.load",
        staticmethod(lambda *a, **k: None))
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    return LocalTransport(store=store)


_PASS_XML = ('<testsuite errors="0" failures="0" tests="1">'
             '<testcase time="1.0"/></testsuite>')
_FAIL_XML = ('<testsuite errors="0" failures="1" tests="1">'
             '<testcase time="1.0"/></testsuite>')


def _run(campaign_dir: Path, config: str, run: str, *, xml=None, log=None,
         sidecar_logs=None, job_index=0, job_prefix="batch-0") -> Path:
    """Materialise one run exactly the way a local campaign does.

    The empty ``<config>/<run>/logs/`` is created deliberately: the runner makes it and
    never writes there, so a reader looking in it sees a silently blank log.

    ``sidecar_logs`` is ``{container: text}`` written as ``logs/system_<container>.log``,
    which is what ``secondary_entrypoint.sh`` produces for the simulator and the SUT.
    """
    run_dir = campaign_dir / config / run
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
    if xml is not None:
        (run_dir / "test.xml").write_text(xml)
    if log is not None or sidecar_logs:
        job_rel = job_artifact_rel(job_index, job_prefix)
        logs = campaign_dir / "_jobs" / job_rel / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        if log is not None:
            (logs / "system.log").write_text(log)
        for name, text in (sidecar_logs or {}).items():
            (logs / f"system_{name}.log").write_text(text)
        manifest = campaign_dir / "_transient" / JOB_LINKS_MANIFEST
        manifest.parent.mkdir(parents=True, exist_ok=True)
        links = yaml.safe_load(manifest.read_text()) if manifest.is_file() else {}
        links[f"{config}/{run}/job"] = f"../../_jobs/{job_rel}"
        manifest.write_text(yaml.safe_dump(links))
    return run_dir


def _job_logs_dir(campaign_dir: Path, job_prefix="batch-0", job_index=0) -> Path:
    return campaign_dir / "_jobs" / job_artifact_rel(job_index, job_prefix) / "logs"


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


# --- three containers -------------------------------------------------------
#
# The ROS shape runs the simulator and the system under test in their own containers,
# which write logs/system_<name>.log beside the main container's logs/system.log. Only
# the latter was read, so the panel showed scenario-execution and neither the simulator
# nor nav2 -- the two whose output explains a failed run.


def test_get_job_log_merges_every_containers_log(transport):
    cid = "campaign-2026-07-17-123000"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "0", xml=_PASS_XML, log="executing scenario\n",
         sidecar_logs={"simulation": "mujoco loaded\n", "sut": "bt_navigator ready\n"})

    text = transport.get_job_log(cid, "cfgA/0").text
    assert "mujoco loaded" in text and "bt_navigator ready" in text
    # Tagged, because that prefix is what the web UI parses and colors per container.
    assert "[robovast]" in text and "[simulation]" in text and "[sut]" in text
    # Main container first: it matches the container plan's order and the cluster lane's.
    assert text.index("[robovast]") < text.index("[simulation]")


def test_get_job_log_leaves_a_single_container_untagged(transport):
    """A one-container job reads exactly as it did before -- no prefix to explain."""
    cid = "campaign-2026-07-17-123100"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "0", xml=_PASS_XML, log="line1\nline2\n")
    assert transport.get_job_log(cid, "cfgA/0").text == "line1\nline2\n"


def test_get_job_log_stays_append_only_as_files_grow_concurrently(transport):
    """The property the whole byte-offset protocol rests on.

    Concatenating the files per read would satisfy the first poll and corrupt the second:
    system.log growing shifts every later container's section, so the client's offset
    lands mid-line in output it has already seen. This is the test that catches that.
    """
    cid = "campaign-2026-07-17-123200"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "0", log="scenario a\n", sidecar_logs={"sut": "nav a\n"})
    logs = _job_logs_dir(cdir)

    first = transport.get_job_log(cid, "cfgA/0")
    # Both files grow between polls, which is the case concatenation cannot survive.
    with open(logs / "system.log", "a") as fh:
        fh.write("scenario b\n")
    with open(logs / "system_sut.log", "a") as fh:
        fh.write("nav b\n")
    second = transport.get_job_log(cid, "cfgA/0", offset=first.next_offset)

    assert second.next_offset > first.next_offset
    assert "scenario b" in second.text and "nav b" in second.text
    # A pure suffix: nothing already delivered is repeated.
    assert "scenario a" not in second.text and "nav a" not in second.text
    whole = transport.get_job_log(cid, "cfgA/0", offset=0).text
    assert whole == first.text + second.text


def test_get_job_log_withholds_a_half_written_line_while_live(transport, monkeypatch):
    """A line still being written must not be interleaved between two containers'.

    It is emitted whole on a later poll -- and flushed even without its newline once the
    run is over, or a container killed mid-line loses its last words (often the traceback).
    """
    cid = "campaign-2026-07-17-123300"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "0", log="done\n", sidecar_logs={"sut": "complete\nhalf"})
    monkeypatch.setattr(transport, "_is_done", lambda _entry: False)
    transport._campaigns[cid] = object()  # campaign is live → run not finished

    live = transport.get_job_log(cid, "cfgA/0")
    assert "complete" in live.text and "half" not in live.text
    assert live.eof is False

    del transport._campaigns[cid]  # campaign gone → flush whatever is left
    rest = transport.get_job_log(cid, "cfgA/0", offset=live.next_offset)
    assert "half" in rest.text


def test_get_job_log_holds_eof_until_a_sidecar_stops_writing(transport, monkeypatch):
    """A sidecar flushes during compose's stop grace -- after test.xml exists.

    The run is done (test.xml written by the main container) while the campaign is still
    live and the simulator is still writing. Ending the stream on test.xml alone closed
    the panel on exactly the shutdown output that says whether it saved its recording.
    """
    cid = "campaign-2026-07-17-123400"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "0", xml=_PASS_XML, log="scenario done\n",
         sidecar_logs={"simulation": "still flushing\n"})
    monkeypatch.setattr(transport, "_is_done", lambda _entry: False)
    transport._campaigns[cid] = object()  # campaign still tearing the job down

    first = transport.get_job_log(cid, "cfgA/0")
    assert "still flushing" in first.text
    assert first.eof is False, "this poll delivered bytes; more may follow"

    settled = transport.get_job_log(cid, "cfgA/0", offset=first.next_offset)
    assert settled.eof is True  # a poll with nothing new → the log has settled


def test_get_job_log_never_duplicates_after_a_truncation(transport):
    """Re-reading a shrunk file from 0 would duplicate it into an append-only stream a
    client already holds an offset into."""
    cid = "campaign-2026-07-17-123500"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "0", log="one\ntwo\n")
    first = transport.get_job_log(cid, "cfgA/0")

    (_job_logs_dir(cdir) / "system.log").write_text("x\n")  # rotated under us
    second = transport.get_job_log(cid, "cfgA/0", offset=first.next_offset)
    assert second.text == ""
    assert second.next_offset == first.next_offset


def test_job_log_tails_are_lru_bounded(transport):
    """A long-lived service must not accumulate one buffer per job it ever served."""
    cid = "campaign-2026-07-17-123600"
    cdir = transport._campaigns_root() / cid
    for i in range(transport._JOB_LOG_CACHE_MAX + 5):
        _run(cdir, f"cfg{i}", "0", log=f"job {i}\n", job_index=i)
        transport.get_job_log(cid, f"cfg{i}/0")

    assert len(transport._job_log_tails) == transport._JOB_LOG_CACHE_MAX
    assert (cid, "cfg0/0") not in transport._job_log_tails  # oldest evicted


# -- a campaign's own plugins reach the build planner ------------------------
#
# Which containers build depends on the simulator backend (a stepped simulator folds
# `simulation` into `scenario`), and the backend can live in the campaign's `plugins:` --
# root-level glue is deliberately not in the service image. Nothing installed those
# plugins before the specs were extracted, and no base_dir was passed, so a project that
# `validate_project` accepted failed at `start_campaign` with "Unknown robovast.simulators
# plugin". The compose path (config_generation) had always done both.


def test_build_planning_installs_the_campaigns_plugins_first(transport, tmp_path,
                                                             monkeypatch):
    import types

    seen = {}

    def _fake_ensure(vast_dir, specs, **_kw):
        seen['dir'], seen['specs'] = vast_dir, specs

    def _fake_extract(_config, base_dir=None, image_project=None,
                      image_project_tag=None):
        seen['base_dir'] = base_dir
        seen['project'] = (image_project, image_project_tag)
        return {}

    monkeypatch.setattr("robovast.common.config_plugins.ensure_workspace_plugins",
                        _fake_ensure)
    monkeypatch.setattr("robovast.service.image_build.extract_build_specs",
                        _fake_extract)

    vast = tmp_path / "proj" / "c.vast"
    vast.parent.mkdir(parents=True)
    vast.write_text("version: 2\n")
    project = types.SimpleNamespace(config_path=str(vast))
    config = types.SimpleNamespace(plugins=["./plugins/backend-1.0-py3-none-any.whl"])

    transport._build_specs_for(project, config, image_project="example.org/team",
                               image_project_tag="v9")

    assert seen['specs'] == ["./plugins/backend-1.0-py3-none-any.whl"], \
        "the campaign's plugins were not installed before its build specs were read"
    assert seen['dir'] == str(vast.parent)
    assert seen['base_dir'] == str(vast.parent), \
        "no base_dir means a '<file>.py:<Class>' backend cannot resolve either"
    assert seen['project'] == ("example.org/team", "v9"), \
        ("the campaign's image project has to reach extract_build_specs, or a backend's "
         "family: ref is carried into a Dockerfile FROM unresolved")


# -- stop_job: killing one running job, and refusing everything else ---------------------
#
# The lane is single-flight behind a container named `robovast`, so the kill lands on
# whichever job is current no matter what was asked for. That is why the precondition is
# checked against `list_jobs` first: accepting a stale name would report success for
# killing a DIFFERENT run than the caller named.


def _killed(campaign_dir):
    """The campaign's kill ledger, resolved to the run keys it covers."""
    from robovast.common.campaign_data import killed_runs
    return killed_runs(campaign_dir)


def test_stop_job_kills_the_running_job_without_stopping_the_campaign(transport, monkeypatch):
    cid = "campaign-2026-08-13-140000"
    cdir = transport._campaigns_root() / cid
    # One run per job, which is this lane's default packing (``runs_per_job: 1``).
    _run(cdir, "cfgA", "0", xml=_PASS_XML, log="done\n", job_index=0)
    _run(cdir, "cfgA", "1", log="running\n", job_index=1)  # no test.xml + live → running
    _live(transport, cid, "running", total=3, completed=1)
    killed_containers = []
    monkeypatch.setattr(transport, "_kill_scenario_container",
                        lambda: killed_containers.append(True))

    result = transport.stop_job(cid, "cfgA/1", "wedged in recovery", "webui")

    assert result.ok
    assert killed_containers == [True], "the scenario container was not killed"
    # The cooperative flag is what ENDS a campaign. Leaving it clear is the entire
    # difference between this and stop(); setting it here would end the sweep.
    assert transport._campaigns[cid].state.stop_requested is False
    assert sorted(_killed(cdir)) == ["cfgA/1"]
    assert _killed(cdir)["cfgA/1"]["detail"] == "wedged in recovery"
    assert _killed(cdir)["cfgA/1"]["source"] == "webui"
    # What the results actually report: the killed run is `killed`, and the one that had
    # already delivered keeps its verdict.
    from robovast.common.campaign_data import read_run_outcomes
    assert {o["run_id"]: o["status"]
            for o in read_run_outcomes(cdir / "cfgA", cdir)} == {0: "passed", 1: "killed"}


def test_stop_job_refuses_a_job_that_is_not_the_running_one(transport, monkeypatch):
    """A stale name must be refused, not silently applied to the current run."""
    cid = "campaign-2026-08-13-141000"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "0", xml=_PASS_XML)  # already completed
    _run(cdir, "cfgA", "1")                 # the one actually running
    _live(transport, cid, "running", total=3, completed=1)
    monkeypatch.setattr(transport, "_kill_scenario_container",
                        lambda: pytest.fail("nothing may be killed on a refusal"))

    with pytest.raises(RuntimeError) as excinfo:
        transport.stop_job(cid, "cfgA/0", None, "mcp")

    message = str(excinfo.value)
    assert "completed" in message, "the refusal must name the phase the job is in"
    assert "cfgA/1" in message, "the refusal must name the job that IS running"
    assert _killed(cdir) == {}, "a refused stop must record nothing"


def test_stop_job_refuses_an_unknown_job(transport, monkeypatch):
    cid = "campaign-2026-08-13-142000"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "0")
    _live(transport, cid, "running", total=1)
    monkeypatch.setattr(transport, "_kill_scenario_container",
                        lambda: pytest.fail("nothing may be killed on a refusal"))

    with pytest.raises(KeyError) as excinfo:
        transport.stop_job(cid, "cfgZ/9", None, "mcp")

    assert "cfgZ/9" in str(excinfo.value)
    assert _killed(cdir) == {}


def test_stop_job_refuses_an_untracked_campaign(transport):
    with pytest.raises(KeyError):
        transport.stop_job("campaign-does-not-exist", "cfgA/0", None, "cli")


def test_stop_job_records_before_it_kills(transport, monkeypatch):
    """The container dies asynchronously, so the record cannot depend on what follows.

    If the kill raised and the record had not been written yet, the run would end with no
    result and no explanation — indistinguishable from one that vanished on its own.
    """
    cid = "campaign-2026-08-13-143000"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "0")
    _live(transport, cid, "running", total=1)

    def _explode():
        raise OSError("docker daemon went away")

    monkeypatch.setattr(transport, "_kill_scenario_container", _explode)

    with pytest.raises(OSError):
        transport.stop_job(cid, "cfgA/0", "why", "cli")

    assert sorted(_killed(cdir)) == ["cfgA/0"], \
        "the kill was not recorded before the container teardown was attempted"


# -- the HTTP surface: what the web UI's Stop button actually hits ------------------------


def _app_client(transport, monkeypatch):
    """A TestClient over the real app, with the container kill recorded rather than run.

    Always patched, never left real: closing the client runs the app's lifespan, whose
    ``shutdown`` terminates still-running campaigns — which would otherwise shell out to
    ``docker rm -f robovast`` on the machine running the tests. The returned list also
    lets a test assert that a *refused* stop killed nothing, as long as it asserts inside
    the client's scope (the shutdown kill lands after it).
    """
    from fastapi.testclient import TestClient

    from robovast.service.app import build_app
    kills = []
    monkeypatch.setattr(transport, "_kill_scenario_container",
                        lambda: kills.append(True))
    return TestClient(build_app(transport)), kills


def test_job_stop_route_kills_the_job_and_records_the_reason(transport, monkeypatch):
    cid = "campaign-2026-08-13-150000"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "0", job_index=0)
    _live(transport, cid, "running", total=1)
    client, kills = _app_client(transport, monkeypatch)

    with client:
        resp = client.post(f"/campaigns/{cid}/job-stop",
                           params={"job_name": "cfgA/0", "reason": "wedged",
                                   "source": "webui"})
        assert kills == [True], "the scenario container was not killed"

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    entry = _killed(cdir)["cfgA/0"]
    assert (entry["detail"], entry["source"]) == ("wedged", "webui")


def test_job_stop_route_refusing_a_finished_job_is_a_409(transport, monkeypatch):
    """A conflict, not a 500: the job finished between the poll and the click.

    409 is what ``_guard`` maps ``RuntimeError`` to, and it is the status the web UI
    renders as a warning rather than an error — this is an expected race, not a fault.
    """
    cid = "campaign-2026-08-13-151000"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "0", xml=_PASS_XML, job_index=0)
    _live(transport, cid, "running", total=1, completed=1)
    client, kills = _app_client(transport, monkeypatch)

    with client:
        resp = client.post(f"/campaigns/{cid}/job-stop", params={"job_name": "cfgA/0"})
        # Asserted in here: the lifespan's own shutdown kill lands on the way out.
        assert kills == [], "a refused stop must kill nothing"

    assert resp.status_code == 409
    assert "not running" in resp.json()["detail"]
    assert _killed(cdir) == {}


def test_job_stop_route_unknown_job_is_a_404(transport, monkeypatch):
    cid = "campaign-2026-08-13-152000"
    _run(transport._campaigns_root() / cid, "cfgA", "0")
    _live(transport, cid, "running", total=1)
    client, _kills = _app_client(transport, monkeypatch)

    with client:
        resp = client.post(f"/campaigns/{cid}/job-stop", params={"job_name": "cfgZ/9"})

    assert resp.status_code == 404


def test_job_stop_route_carries_a_job_name_containing_a_slash(transport, monkeypatch):
    """``job_name`` is a query param precisely because locally it contains a '/'.

    As a path segment it would have to be double-encoded by every client; this asserts the
    plain name survives the round trip.
    """
    cid = "campaign-2026-08-13-153000"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfg-with-dash", "7", job_index=0)
    _live(transport, cid, "running", total=1)
    client, _kills = _app_client(transport, monkeypatch)

    with client:
        resp = client.post(f"/campaigns/{cid}/job-stop",
                           params={"job_name": "cfg-with-dash/7"})

    assert resp.status_code == 200
    assert sorted(_killed(cdir)) == ["cfg-with-dash/7"]


def test_a_killed_run_stops_reporting_itself_as_running(transport, monkeypatch):
    """The reported bug: the Jobs list kept a row for a job that was already dead.

    "running" on this lane means *no ``test.xml`` yet*, and a killed run's ``test.xml`` is
    exactly the thing that never arrives — so it stayed running for the rest of the
    campaign's life, leaving a live row and a Stop button on a job nothing was executing.
    """
    cid = "campaign-2026-08-13-160000"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "goal-2", "0", job_index=0)
    _live(transport, cid, "running", total=1)
    monkeypatch.setattr(transport, "_kill_scenario_container", lambda: None)

    assert transport.list_jobs(cid).jobs[0].status == "running"

    transport.stop_job(cid, "goal-2/0", "wedged", "webui")

    resp = transport.list_jobs(cid)
    assert resp.jobs[0].status == "killed", "a stopped job must not still report running"
    assert resp.counts.running == 0
    assert resp.counts.killed == 1
    assert resp.counts.failed == 0, "a kill is not a failed job"
    # The row explains itself without opening the (empty) log.
    assert resp.jobs[0].detail == "manually stopped via webui: wedged"


def test_a_killed_job_that_had_already_delivered_keeps_its_verdict(transport, monkeypatch):
    """Same precedence as the run outcome: a kill only explains a run that produced nothing."""
    cid = "campaign-2026-08-13-161000"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "goal-2", "0", xml=_PASS_XML, job_index=0)
    _live(transport, cid, "running", total=1)
    from robovast.common.campaign_data import KIND_KILLED, record_intervention
    record_intervention(cdir, kind=KIND_KILLED, job_dir="_jobs/batch-0/job-0",
                        job_name="goal-2/0", source="webui", detail="late kill",
                        runs=("goal-2/0",))

    assert transport.list_jobs(cid).jobs[0].status == "completed"


def test_a_killed_run_is_not_reported_as_failed_after_the_campaign_ends(transport, monkeypatch):
    """With the campaign gone, a resultless run reads `failed` — unless somebody stopped it."""
    cid = "campaign-2026-08-13-162000"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "goal-2", "0", job_index=0)
    _run(cdir, "goal-3", "0", job_index=1)  # simply lost its result
    from robovast.common.campaign_data import KIND_KILLED, record_intervention
    record_intervention(cdir, kind=KIND_KILLED, job_dir="_jobs/batch-0/job-0",
                        job_name="goal-2/0", source="cli", detail=None,
                        runs=("goal-2/0",))

    by_name = {j.job_name: j.status for j in transport.list_jobs(cid).jobs}
    assert by_name == {"goal-2/0": "killed", "goal-3/0": "failed"}


# -- get_job_state: what a running job is doing, from the run's own tools -------------------------


#: A ROS-shape execution block: the simulator has a container of its own, which is the shape every
#: roqsim campaign uses. Written to the campaign's ``_config/`` so the transport reads it for real
#: -- stubbing ``_campaign_execution`` was what hid a returned pydantic model AND a health command
#: sent to the wrong container, for as long as both existed.
_ROS_SHAPE_VAST = """\
version: 2
metadata: {name: t}
configuration:
- name: cfga
execution:
  runs: 1
  mode: ros2
  containers:
    simulation: {image: sim-image:1, backend: roqsim, config: w.yaml}
    sut: {image: sut-image:1}
"""

#: The stepped shape: the simulation container is declared but has neither image nor command, so
#: the simulator IS the scenario container and the role must resolve to it rather than to a name
#: nothing started. (An *absent* simulation block is a third case -- a campaign with no simulator.)
_STEPPED_VAST = """\
version: 2
metadata: {name: t}
configuration:
- name: cfga
execution:
  runs: 1
  mode: base
  containers:
    scenario: {image: scen-image:1}
    simulation: {backend: roqsim, config: w.yaml}
"""


#: What the resource read prints back: the marker line the reader splits on, then each file's
#: header and tail. Two ticks in the main container so "newest only" is actually exercised.
_RESOURCE_CSVS = ("@@ resource_usage_main.csv\n"
                  "timestamp,pid,name,cpu_percent,memory_rss_bytes\n"
                  "100.0,7,scenario_execution,3.5,1000\n"
                  "200.0,7,scenario_execution,0.0,1100\n"
                  "@@ resource_usage_sut.csv\n"
                  "timestamp,pid,name,cpu_percent,memory_rss_bytes\n"
                  "200.0,9,amcl,99.5,2000\n")


#: A campaign with no simulator at all: nothing to ask about itself, which is a normal answer and
#: must never render as a healthy run.
_NO_SIM_VAST = """\
version: 2
metadata: {name: t}
configuration:
- name: cfga
execution:
  runs: 1
  mode: base
  containers:
    scenario: {image: scen-image:1}
"""


def _freeze_vast(campaign_dir, text=_ROS_SHAPE_VAST):
    """Put a real frozen ``.vast`` where the transport looks for this campaign's config."""
    config_dir = Path(campaign_dir) / "_config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "campaign.vast").write_text(text, encoding="utf-8")


def _job_state_transport(transport, monkeypatch, *, command, exec_result, tree_result=None,
                         resource_result=None):
    """A transport whose campaign has *command* as its simulator's health command.

    The reads are answered separately, as the real container does: the scenario's tree comes from
    scenario-execution's reader, the samples from the run's resource monitor, and the rest from the
    simulator's. A single canned reply for all of them would let one read's output be mistaken for
    another's.

    Matched on the *joined* argv, because every read now runs through a shell that sources the run's
    ROS overlay first -- the command is inside one argv element rather than being the elements.
    """
    monkeypatch.setattr("robovast.common.simulators.health_command",
                        lambda execution, *, run_dir, base_dir="": command)
    tree = tree_result if tree_result is not None else (0, '{"found": false, "error": "none"}',
                                                       "", False)
    resources = resource_result if resource_result is not None else (0, "", "", False)

    class _Lane:
        def __init__(self):
            self.calls = []

        def exec_in(self, target, argv, limit_s, env=None):
            self.calls.append((target, argv, limit_s))
            joined = " ".join(argv)
            if "scenario_execution.tree_state" in joined:
                return tree
            if "resource_usage_" in joined:
                return resources
            return exec_result

    lane = _Lane()
    monkeypatch.setattr(transport, "_exec_lane", lambda: lane)
    return lane


def _call_with(lane, needle):
    """The one recorded exec whose command mentions *needle*."""
    hits = [c for c in lane.calls if needle in " ".join(c[1])]
    assert hits, f"no exec mentioning {needle!r}; got {[c[1] for c in lane.calls]}"
    return hits[0]


def test_get_job_state_passes_the_simulator_s_json_through(transport, monkeypatch):
    """RoboVAST parses no simulator's file format: the tool that owns the records reads them, and
    what it says travels unreshaped. A vocabulary of our own here would be a second definition of
    someone else's data."""
    cid = "campaign-2026-08-20-120000"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "1", log="running\n", job_index=0)
    _freeze_vast(cdir)
    _live(transport, cid, "running", total=1, completed=0)
    reply = '{"findings": [], "state": {"sim_ts": 12.5, "entities": []}}'
    tree = '{"found": true, "running": {"name": "drive_to"}, "counts": {"RUNNING": 1}}'
    lane = _job_state_transport(transport, monkeypatch,
                                command="roqsim health --json /out/cfgA/1",
                                exec_result=(0, reply, "", False),
                                tree_result=(0, tree, "", False),
                                resource_result=(0, _RESOURCE_CSVS, "", False))

    state = transport.get_job_state(cid, "cfgA/1")

    assert state.simulator == {"findings": [], "state": {"sim_ts": 12.5, "entities": []}}
    assert state.scenario["running"]["name"] == "drive_to"
    assert state.unavailable == []
    # The run dir is derived from the run, not read from RUN_OUTPUT_DIR -- the backends set that
    # only for a job that is exactly one run, so a packed job would have none.
    sim_call = _call_with(lane, "roqsim health")
    assert "roqsim health --json /out/cfgA/1" in " ".join(sim_call[1])


def test_the_health_read_tries_the_job_dir_before_the_run_dir(transport, monkeypatch):
    """A simulator's records MOVE. While a run is live its clock record sits in the job's own
    output dir; only results collection puts it beside the run. This read is only ever asked about
    a running job, so the job dir is the answer -- and pointing it at the run dir returned "no
    records" for a simulator that was reporting perfectly well one directory away."""
    cid = "campaign-2026-08-20-120600"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "1", log="running\n", job_index=0)
    _freeze_vast(cdir)
    _live(transport, cid, "running", total=1, completed=0)
    monkeypatch.setattr(transport, "_job_output_dir",
                        lambda cid_, job, run_dir: "/out/_jobs/batch-0/job-0")
    monkeypatch.setattr("robovast.common.simulators.health_command",
                        lambda execution, *, run_dir, base_dir="": f"tool --json {run_dir}")

    class _Lane:
        def __init__(self):
            self.dirs = []

        def exec_in(self, target, argv, limit_s, env=None):
            script = " ".join(argv)
            if "tool --json" not in script:
                return (0, "", "", False)
            self.dirs.append(script.rsplit(" ", 1)[-1])
            # Only the job dir has the record, exactly as a live run has it.
            if "_jobs" in script:
                return (0, '{"findings": [], "state": {"sim_ts": 9.0}}', "", False)
            return (2, "", "no records here", False)

    lane = _Lane()
    monkeypatch.setattr(transport, "_exec_lane", lambda: lane)

    state = transport.get_job_state(cid, "cfgA/1")

    assert state.simulator == {"findings": [], "state": {"sim_ts": 9.0}}
    assert lane.dirs == ["/out/_jobs/batch-0/job-0"], \
        "the job dir must be tried FIRST, and answering there must cost no second exec"
    # The other two reads are stubbed to say nothing here, so they report themselves as usual --
    # what this test asserts is that none of those reasons is about the simulator.
    assert not any("tool --json" in line for line in state.unavailable)


def test_the_health_read_falls_back_to_the_run_dir(transport, monkeypatch):
    """Where a backend writes is the backend's business and one lane's layout is not the other's,
    so the run dir is tried after the job dir rather than assumed away."""
    cid = "campaign-2026-08-20-120700"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "1", log="running\n", job_index=0)
    _freeze_vast(cdir)
    _live(transport, cid, "running", total=1, completed=0)
    monkeypatch.setattr(transport, "_job_output_dir",
                        lambda cid_, job, run_dir: "/out/_jobs/batch-0/job-0")
    monkeypatch.setattr("robovast.common.simulators.health_command",
                        lambda execution, *, run_dir, base_dir="": f"tool --json {run_dir}")

    class _Lane:
        def __init__(self):
            self.dirs = []

        def exec_in(self, target, argv, limit_s, env=None):
            script = " ".join(argv)
            if "tool --json" not in script:
                return (0, "", "", False)
            self.dirs.append(script.rsplit(" ", 1)[-1])
            if "_jobs" in script:
                return (2, "", "no records here", False)
            return (0, '{"findings": [], "state": {"sim_ts": 1.0}}', "", False)

    lane = _Lane()
    monkeypatch.setattr(transport, "_exec_lane", lambda: lane)

    state = transport.get_job_state(cid, "cfgA/1")

    assert state.simulator == {"findings": [], "state": {"sim_ts": 1.0}}
    assert lane.dirs == ["/out/_jobs/batch-0/job-0", "/out/cfgA/1"]


def test_get_job_state_asks_each_container_that_owns_its_read(transport, monkeypatch):
    """The simulator's command belongs in the simulator's container and the scenario's in the
    scenario's. Sending both to one target ran ``roqsim health`` in a container with no roqsim in
    it for every ROS-shape campaign -- which is every roqsim one."""
    cid = "campaign-2026-08-20-120100"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "1", log="running\n", job_index=0)
    _freeze_vast(cdir)                      # a simulation container of its own
    _live(transport, cid, "running", total=1, completed=0)
    lane = _job_state_transport(transport, monkeypatch,
                                command="roqsim health --json /out/cfgA/1",
                                exec_result=(0, "{}", "", False),
                                tree_result=(0, '{"found": false, "error": "x"}', "", False))

    transport.get_job_state(cid, "cfgA/1")

    assert _call_with(lane, "roqsim health")[0] == SIMULATION_CONTAINER
    assert _call_with(lane, "tree_state")[0] == transport._CONTAINER_NAME


def test_get_job_state_reads_a_stepped_simulator_in_the_scenario_container(transport, monkeypatch):
    """A simulator stepped in-process IS the scenario container, so the role resolves to it. The
    campaign's own container plan answers that -- guessing the role's name would address a
    container nothing started."""
    cid = "campaign-2026-08-20-120200"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "1", log="running\n", job_index=0)
    _freeze_vast(cdir, _STEPPED_VAST)       # no simulation container
    _live(transport, cid, "running", total=1, completed=0)
    lane = _job_state_transport(transport, monkeypatch,
                                command="roqsim health --json /out/cfgA/1",
                                exec_result=(0, "{}", "", False))

    transport.get_job_state(cid, "cfgA/1")

    assert _call_with(lane, "roqsim health")[0] == transport._CONTAINER_NAME


def test_get_job_state_reads_run_the_run_s_own_environment(transport, monkeypatch):
    """``scenario_execution`` is colcon-built into an overlay no shell rc sources, so a bare argv
    answers "No module named 'scenario_execution'" in every image ever built. Rebuilding does not
    fix that; sourcing the overlay the run itself sources does."""
    cid = "campaign-2026-08-20-120300"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "1", log="running\n", job_index=0)
    _freeze_vast(cdir)
    _live(transport, cid, "running", total=1, completed=0)
    lane = _job_state_transport(transport, monkeypatch,
                                command="roqsim health --json /out/cfgA/1",
                                exec_result=(0, "{}", "", False))

    transport.get_job_state(cid, "cfgA/1")

    for needle in ("tree_state", "roqsim health"):
        script = " ".join(_call_with(lane, needle)[1])
        assert "/ws/install/setup.bash" in script
        assert script.index("/ws/install/setup.bash") < script.index(needle)


def test_get_job_state_states_an_unreadable_configuration_as_such(transport, monkeypatch):
    """A config that cannot be read is a different answer from a simulator that cannot report, and
    collapsing them makes a broken campaign read as a capability gap."""
    cid = "campaign-2026-08-20-120400"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "1", log="running\n", job_index=0)
    _live(transport, cid, "running", total=1, completed=0)   # no frozen .vast at all
    _job_state_transport(transport, monkeypatch, command="roqsim health",
                         exec_result=(0, "{}", "", False))

    state = transport.get_job_state(cid, "cfgA/1")

    assert state.simulator is None
    assert any("could not read this campaign's configuration" in line
               for line in state.unavailable)
    assert not any("does not report its own state" in line for line in state.unavailable)


def test_get_job_state_reports_the_newest_resource_sample_per_container(transport, monkeypatch):
    """0% CPU is a deadlock and 100% is a spin, and a log and a tree that both say RUNNING cannot
    tell them apart. Per process, so the answer names the node rather than only the container."""
    cid = "campaign-2026-08-20-120500"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "1", log="running\n", job_index=0)
    _freeze_vast(cdir)
    _live(transport, cid, "running", total=1, completed=0)
    _job_state_transport(transport, monkeypatch, command=None,
                         exec_result=(0, "", "", False),
                         resource_result=(0, _RESOURCE_CSVS, "", False))

    state = transport.get_job_state(cid, "cfgA/1")

    assert state.resources["main"]["at"] == 200.0
    assert state.resources["main"]["processes"] == [
        {"name": "scenario_execution", "cpu_percent": 0.0, "memory_rss_bytes": 1100}]
    assert state.resources["sut"]["processes"][0]["cpu_percent"] == 99.5


def test_get_job_state_says_when_the_simulator_cannot_report(transport, monkeypatch):
    """A simulator RoboVAST merely launches cannot be asked how it is doing. That is a normal
    answer and must never render as a healthy run."""
    cid = "campaign-2026-08-20-121000"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "1", log="running\n", job_index=0)
    _freeze_vast(cdir)
    _live(transport, cid, "running", total=1, completed=0)
    _job_state_transport(transport, monkeypatch, command=None, exec_result=(0, "", "", False))

    state = transport.get_job_state(cid, "cfgA/1")

    assert state.simulator is None
    assert any("does not report its own state" in line for line in state.unavailable)


def test_get_job_state_reports_a_timeout_without_asserting_a_cause(transport, monkeypatch):
    """A wedged container is itself a finding -- but this call cannot confirm it, and saying so is
    the difference between a diagnosis and a guess."""
    cid = "campaign-2026-08-20-122000"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "1", log="running\n", job_index=0)
    _freeze_vast(cdir)
    _live(transport, cid, "running", total=1, completed=0)
    _job_state_transport(transport, monkeypatch, command="roqsim health --json /out/cfgA/1",
                         exec_result=(0, "", "", True))

    state = transport.get_job_state(cid, "cfgA/1")

    assert state.simulator is None
    assert any("did not answer" in line for line in state.unavailable)


def test_get_job_state_reports_unreadable_output_rather_than_swallowing_it(transport, monkeypatch):
    """A tool whose output cannot be parsed is a different problem from a misbehaving run.
    Conflating them hides both."""
    cid = "campaign-2026-08-20-123000"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "1", log="running\n", job_index=0)
    _freeze_vast(cdir)
    _live(transport, cid, "running", total=1, completed=0)
    _job_state_transport(transport, monkeypatch, command="roqsim health --json /out/cfgA/1",
                         exec_result=(1, "Traceback: boom", "", False))

    state = transport.get_job_state(cid, "cfgA/1")

    assert state.simulator is None
    assert any("not JSON" in line for line in state.unavailable)


def test_get_job_state_refuses_a_job_that_is_not_running(transport, monkeypatch):
    """Same precondition as stop_job, and checked against the status the caller was shown: only a
    job that is underway has a state to read."""
    cid = "campaign-2026-08-20-124000"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "0", xml=_PASS_XML)   # completed
    _run(cdir, "cfgA", "1", log="running\n", job_index=1)
    _freeze_vast(cdir)
    _live(transport, cid, "running", total=2, completed=1)
    _job_state_transport(transport, monkeypatch, command="x", exec_result=(0, "{}", "", False))

    with pytest.raises(RuntimeError, match="not running"):
        transport.get_job_state(cid, "cfgA/0")


# -- exec_in_job: the probe, and the record it leaves ---------------------------------------------


def _probe_transport(transport, monkeypatch, *, exec_result=(0, "out", "", False)):
    class _Lane:
        calls: list = []

        def exec_in(self, target, argv, limit_s, env=None):
            _Lane.calls.append((target, argv, limit_s))
            return exec_result

    _Lane.calls = []
    monkeypatch.setattr(transport, "_exec_lane", lambda: _Lane())
    return _Lane


def _probed(campaign_dir):
    from robovast.common.campaign_data import probed_runs
    return probed_runs(campaign_dir)


def test_exec_in_job_records_the_probe_before_running_it(transport, monkeypatch):
    """Recorded first, not after: the command may wedge the run or the service may die, and
    perturbed data with nothing saying why is the case this ledger exists for."""
    cid = "campaign-2026-08-20-130000"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "1", log="running\n", job_index=0)
    _live(transport, cid, "running", total=1, completed=0)
    order = []

    class _Lane:
        def exec_in(self, target, argv, limit_s, env=None):
            # By the time the command runs, the ledger is already on disk.
            order.append(sorted(_probed(cdir)))
            return (0, "ok", "", False)

    monkeypatch.setattr(transport, "_exec_lane", lambda: _Lane())

    result = transport.exec_in_job(cid, "cfgA/1", "ros2 node list", source="mcp")

    assert result.exit_code == 0 and result.stdout == "ok"
    assert order == [["cfgA/1"]], "the probe was not recorded before the command ran"
    entry = _probed(cdir)["cfgA/1"]
    assert (entry["kind"], entry["source"], entry["detail"]) == \
        ("probed", "mcp", "ros2 node list")


def test_exec_in_job_enters_the_role_the_caller_named(transport, monkeypatch):
    """A role, not a container name: the concrete name differs by lane, so the caller names what
    it means and the lane resolves it."""
    cid = "campaign-2026-08-20-131000"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "1", log="running\n", job_index=0)
    _live(transport, cid, "running", total=1, completed=0)
    lane = _probe_transport(transport, monkeypatch)

    transport.exec_in_job(cid, "cfgA/1", "true", container="simulation")
    transport.exec_in_job(cid, "cfgA/1", "true", container="scenario")

    targets = [c[0] for c in lane.calls]
    assert targets == ["simulation", transport._CONTAINER_NAME]


def test_exec_in_job_refuses_an_unknown_role(transport, monkeypatch):
    """Naming the roles that exist beats a container that silently does not."""
    cid = "campaign-2026-08-20-132000"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "1", log="running\n", job_index=0)
    _live(transport, cid, "running", total=1, completed=0)
    _probe_transport(transport, monkeypatch)

    with pytest.raises(ValueError, match="unknown container role"):
        transport.exec_in_job(cid, "cfgA/1", "true", container="sidecar")


def test_exec_in_job_refuses_a_job_that_is_not_running(transport, monkeypatch):
    """And records nothing: a refused probe did not touch the run."""
    cid = "campaign-2026-08-20-133000"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "0", xml=_PASS_XML)
    _run(cdir, "cfgA", "1", log="running\n", job_index=1)
    _live(transport, cid, "running", total=2, completed=1)
    _probe_transport(transport, monkeypatch)

    with pytest.raises(RuntimeError, match="not running"):
        transport.exec_in_job(cid, "cfgA/0", "true")
    assert _probed(cdir) == {}


def test_exec_in_job_refuses_an_empty_command(transport, monkeypatch):
    """Unlike exec_in_container, an empty command has no meaning here: there is no scenario to
    start, only a live one to look at -- and it would record a probe that did nothing."""
    cid = "campaign-2026-08-20-134000"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "1", log="running\n", job_index=0)
    _live(transport, cid, "running", total=1, completed=0)
    _probe_transport(transport, monkeypatch)

    with pytest.raises(ValueError, match="needs a command"):
        transport.exec_in_job(cid, "cfgA/1", "   ")
    assert _probed(cdir) == {}


def test_a_probe_does_not_change_the_run_s_status(transport, monkeypatch):
    """The whole reason probed is a separate column: a probed run keeps whatever verdict it
    reached, and folding it into status would put a human's action into the measurement."""
    cid = "campaign-2026-08-20-135000"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "1", log="running\n", job_index=0)
    _live(transport, cid, "running", total=1, completed=0)
    _probe_transport(transport, monkeypatch)

    transport.exec_in_job(cid, "cfgA/1", "true")

    assert transport.list_jobs(cid).jobs[0].status == "running"


def test_the_scenario_tree_is_read_even_when_the_simulator_cannot_report(transport, monkeypatch):
    """The stuck action is the more useful half and it does not depend on the simulator, so a
    campaign whose simulator reports nothing must still get it. Coupling the two reads would let
    the absence of one hide the other."""
    cid = "campaign-2026-08-20-126000"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "1", log="running\n", job_index=0)
    _freeze_vast(cdir, _NO_SIM_VAST)
    _live(transport, cid, "running", total=1, completed=0)
    tree = '{"found": true, "running": {"name": "drive_to", "since": 31.4}}'
    _job_state_transport(transport, monkeypatch, command=None,
                         exec_result=(0, "", "", False), tree_result=(0, tree, "", False))

    state = transport.get_job_state(cid, "cfgA/1")

    assert state.simulator is None
    assert state.scenario["running"]["name"] == "drive_to"
    assert any("does not report its own state" in line for line in state.unavailable)


def test_a_run_without_bt_log_says_so_rather_than_showing_an_empty_tree(transport, monkeypatch):
    """The reader's own phrasing is carried through: it already names the reason (a run with
    bt_log off, or one that has not ticked), which is more use than "unavailable" and is already
    written for a reader."""
    cid = "campaign-2026-08-20-127000"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "1", log="running\n", job_index=0)
    _freeze_vast(cdir)
    _live(transport, cid, "running", total=1, completed=0)
    reason = '{"found": false, "error": "no behaviors.jsonl in or below \'/out/cfgA/1\'"}'
    _job_state_transport(transport, monkeypatch, command="tool --json /out/cfgA/1",
                         exec_result=(0, "{}", "", False), tree_result=(0, reason, "", False))

    state = transport.get_job_state(cid, "cfgA/1")

    assert state.scenario is None
    assert any("behaviors.jsonl" in line for line in state.unavailable)


# -- the pull: findings on the status path, without a standing anything ----------------------------


#: A frozen `.vast` these tests can hand the transport, valid so `_campaign_execution` gets past
#: validation. Its own copy rather than a shared constant: what the pull needs from a campaign's
#: config is only that reading it *works*, so a fixture shaped for some other question would couple
#: these tests to that question.
_PULL_VAST = """\
version: 2
metadata: {name: t}
configuration:
- name: cfga
execution:
  runs: 1
  mode: ros2
  containers:
    simulation: {image: sim-image:1, backend: roqsim, config: w.yaml}
"""


def _healthy_campaign(transport, monkeypatch, cid, *, findings, exec_result=None):
    """A live one-run campaign whose simulator reports *findings*, and the lane it is asked over."""
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "1", log="running\n", job_index=0)
    _freeze_vast(cdir, _PULL_VAST)
    _live(transport, cid, "running", total=1, completed=0)
    reply = json.dumps({"findings": findings, "state": {"sim_ts": 3.1}})
    return _job_state_transport(transport, monkeypatch, command="tool --json /out/cfgA/1",
                                exec_result=exec_result or (0, reply, "", False))


def _drain(transport, cid, polls=1):
    """Poll the status, then let the refresher it spawned finish before asserting on it.

    The refresh is deliberately off the request thread -- a status read must not wait even for the
    exec's own timeout -- so a test has to join it rather than assume the first read carries the
    answer.
    """
    for _ in range(polls):
        transport.get_status(cid)
        for thread in list(threading.enumerate()):
            if thread.name == f"health-{cid}":
                thread.join(timeout=5)
    # Read once more, after joining: findings arrive one poll later by design, so a status taken
    # before the refresher finished would make every assertion below pass for the wrong reason.
    return transport.get_status(cid)


def test_a_status_read_carries_the_running_job_s_findings(transport, monkeypatch):
    """The delivery this exists for: a wedged run holds ``running`` for its whole life, so the
    verdict has to ride on the thing the waiter already polls."""
    cid = "campaign-2026-08-20-130000"
    _healthy_campaign(transport, monkeypatch, cid,
                      findings=[{"level": "error", "check": "sim-time-rate", "detail": "3.1s"}])

    status = _drain(transport, cid)               # one poll pays for the read, the next serves it

    assert [(f.job_name, f.level, f.check) for f in status.health] == [
        ("cfgA/1", "error", "sim-time-rate")]


def test_nobody_polling_means_nothing_is_asked(transport, monkeypatch):
    """"Absent costs nothing": no process runs in any container and no exec is issued at all until
    somebody reads the status. A campaign nobody is debugging must pay nothing for this."""
    cid = "campaign-2026-08-20-131000"
    lane = _healthy_campaign(transport, monkeypatch, cid, findings=[])

    assert lane.calls == []


def test_the_ttl_collapses_many_watchers_into_one_check(transport, monkeypatch):
    """N watchers cost one check per interval, not N. The claim is taken under the lock precisely
    so concurrent status reads cannot each start their own exec."""
    cid = "campaign-2026-08-20-132000"
    lane = _healthy_campaign(transport, monkeypatch, cid, findings=[])

    _drain(transport, cid, polls=6)

    assert len([c for c in lane.calls if "tool" in " ".join(c[1])]) == 1


def test_the_ttl_expiring_asks_again(transport, monkeypatch):
    """The other half of the same property: this is a cache, not a one-shot. A finding that appears
    after the first poll still has to arrive."""
    cid = "campaign-2026-08-20-133000"
    lane = _healthy_campaign(transport, monkeypatch, cid, findings=[])

    _drain(transport, cid)
    with transport._health_guard:
        transport._health[cid]["at"] = 0.0        # as if a whole interval had passed
        transport._health[cid]["jobs"] = {}
    _drain(transport, cid)

    assert len([c for c in lane.calls if "tool" in " ".join(c[1])]) == 2


def test_a_status_read_never_waits_on_a_wedged_container(transport, monkeypatch):
    """The non-negotiable one. A hung container must not slow every watcher of the campaign it is
    hanging in -- which is exactly when a reader needs an answer -- so the read answers from what it
    has and the exec happens elsewhere."""
    cid = "campaign-2026-08-20-134000"
    started, release = threading.Event(), threading.Event()
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "1", log="running\n", job_index=0)
    _live(transport, cid, "running", total=1, completed=0)
    _freeze_vast(cdir, _PULL_VAST)
    monkeypatch.setattr("robovast.common.simulators.health_command",
                        lambda execution, *, run_dir, base_dir="": "tool --json /out")

    class _WedgedLane:
        def exec_in(self, target, argv, limit_s, env=None):
            started.set()
            release.wait(10)
            return (0, "", "", True)

    monkeypatch.setattr(transport, "_exec_lane", lambda: _WedgedLane())
    try:
        transport.get_status(cid)                 # must return while the exec is still blocked
        assert started.wait(5), "the refresh should have been started"
        status = transport.get_status(cid)
        assert status.health == [], "no findings yet -- and that is not a claim of health"
    finally:
        release.set()
        for thread in list(threading.enumerate()):
            if thread.name == f"health-{cid}":
                thread.join(timeout=5)


def test_a_read_that_failed_is_a_reason_and_never_a_verdict(transport, monkeypatch):
    """An empty answer that reads as "nothing is happening" is the failure mode this whole path
    exists to prevent, so the status carries no findings and ``get_job_state`` says why."""
    cid = "campaign-2026-08-20-135000"
    _healthy_campaign(transport, monkeypatch, cid, findings=[],
                      exec_result=(0, "", "", True))

    status = _drain(transport, cid)
    state = transport.get_job_state(cid, "cfgA/1")

    assert status.health == []
    assert any("did not answer within" in line for line in state.unavailable)
    assert state.simulator is None


def test_get_job_state_is_served_from_what_the_poll_already_paid_for(transport, monkeypatch):
    """An agent asking must not be charged for a check a poll has already done, which is the reason
    the two share one cache rather than each having their own."""
    cid = "campaign-2026-08-20-136000"
    lane = _healthy_campaign(transport, monkeypatch, cid, findings=[])

    _drain(transport, cid)
    state = transport.get_job_state(cid, "cfgA/1")

    assert state.simulator == {"findings": [], "state": {"sim_ts": 3.1}}
    assert len([c for c in lane.calls if "tool" in " ".join(c[1])]) == 1


def test_a_malformed_finding_does_not_take_the_status_read_down(transport, monkeypatch):
    """The document belongs to the simulator, so a shape RoboVAST did not expect is that
    simulator's business. A finding with no level or check can be neither matched nor ranked, so it
    is dropped rather than guessed at."""
    cid = "campaign-2026-08-20-137000"
    _healthy_campaign(transport, monkeypatch, cid, findings=[
        "not a dict", {"detail": "no level, no check"}, {"level": "error", "check": "ok"}])

    status = _drain(transport, cid)

    assert [f.check for f in status.health] == ["ok"]


def test_reading_a_live_job_taints_nothing(transport, monkeypatch):
    """The rule the whole read/probe split rests on: the service chose this command, so no ledger
    entry is written and no run is marked. Only a caller-supplied command is a probe."""
    cid = "campaign-2026-08-20-138000"
    _healthy_campaign(transport, monkeypatch, cid, findings=[])

    _drain(transport, cid, polls=3)
    transport.get_job_state(cid, "cfgA/1")

    ledger = transport._campaigns_root() / cid / "_execution" / "interventions.json"
    assert not ledger.exists()


def test_a_finished_campaign_is_forgotten_rather_than_asked(transport, monkeypatch):
    """What a run reported while it was wedged is history once it is over, and the results are the
    record then. Holding it would also leak an entry per campaign for the service's lifetime."""
    cid = "campaign-2026-08-20-139000"
    _healthy_campaign(transport, monkeypatch, cid,
                      findings=[{"level": "error", "check": "sim-time-rate"}])
    _drain(transport, cid)
    assert transport._health.get(cid)

    _live(transport, cid, "finished", total=1, completed=1)
    status = transport.get_status(cid)

    assert status.health == []
    assert cid not in transport._health


def test_the_service_never_reads_a_simulator_s_own_records():
    """The invariant a well-meaning shortcut breaks first, so it is asserted rather than trusted.

    A live read *could* open the simulator's pose or clock record directly and would be marginally
    cheaper. It is deliberately not done: those files belong to the simulator and are free to be
    reshaped, so a reader here would be a hidden cross-repo coupling that breaks silently on the
    day they are. The service execs the container's own tool and reads the JSON it declares.

    Scoped to the service tree on purpose. Naming those records is correct in two other places: the
    simulator-specific backend package, which *is* the code that knows its simulator, and the
    results tables, where the CSV became a documented column set through the generic
    one-table-per-CSV-stem ingest rather than through anything reading it by name.
    """
    # From a module in it rather than from the package: `robovast.service` is a namespace package,
    # so it has no `__file__` of its own.
    import robovast.service.local_transport as _lt
    service_dir = Path(_lt.__file__).parent
    offenders = [path.name for path in service_dir.rglob("*.py")
                 if "sim_poses" in path.read_text(encoding="utf-8")]
    assert offenders == [], (
        f"{offenders} names a simulator's own record. Ask the simulator's tool instead — see "
        "SimulatorBackend.health_command.")
