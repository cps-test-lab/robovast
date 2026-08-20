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


def _job_state_transport(transport, monkeypatch, *, command, exec_result):
    """A transport whose campaign has *command* as its simulator's health command."""
    monkeypatch.setattr("robovast.common.simulators.health_command",
                        lambda execution, *, run_dir, base_dir="": command)
    monkeypatch.setattr(transport, "_campaign_execution", lambda cid: {"containers": {}})

    class _Lane:
        def __init__(self):
            self.calls = []

        def exec_in(self, target, argv, limit_s, env=None):
            self.calls.append((target, argv, limit_s))
            return exec_result

    lane = _Lane()
    monkeypatch.setattr(transport, "_exec_lane", lambda: lane)
    return lane


def test_get_job_state_passes_the_simulator_s_json_through(transport, monkeypatch):
    """RoboVAST parses no simulator's file format: the tool that owns the records reads them, and
    what it says travels unreshaped. A vocabulary of our own here would be a second definition of
    someone else's data."""
    cid = "campaign-2026-08-20-120000"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "1", log="running\n", job_index=0)
    _live(transport, cid, "running", total=1, completed=0)
    reply = '{"findings": [], "state": {"sim_ts": 12.5, "entities": []}}'
    lane = _job_state_transport(transport, monkeypatch,
                                command="roqsim health --json /out/cfgA/1",
                                exec_result=(0, reply, "", False))

    state = transport.get_job_state(cid, "cfgA/1")

    assert state.simulator == {"findings": [], "state": {"sim_ts": 12.5, "entities": []}}
    assert state.unavailable == []
    # The run dir is derived from the run, not read from RUN_OUTPUT_DIR -- the backends set that
    # only for a job that is exactly one run, so a packed job would have none.
    target, argv, _limit = lane.calls[0]
    assert target == transport._CONTAINER_NAME
    assert argv == ["roqsim", "health", "--json", "/out/cfgA/1"]


def test_get_job_state_says_when_the_simulator_cannot_report(transport, monkeypatch):
    """A simulator RoboVAST merely launches cannot be asked how it is doing. That is a normal
    answer and must never render as a healthy run."""
    cid = "campaign-2026-08-20-121000"
    cdir = transport._campaigns_root() / cid
    _run(cdir, "cfgA", "1", log="running\n", job_index=0)
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
    _live(transport, cid, "running", total=2, completed=1)
    _job_state_transport(transport, monkeypatch, command="x", exec_result=(0, "{}", "", False))

    with pytest.raises(RuntimeError, match="not running"):
        transport.get_job_state(cid, "cfgA/0")
