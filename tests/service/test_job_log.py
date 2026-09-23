# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A job's log as rows, read from the files its containers write in the campaign.

The pod's file agent delivers the files' growth into the campaign. These pin what a reader of a growing
file must get right: the job's directory rather than the run's, a cursor that never
repeats or skips a row, a record not cut from its continuation lines, a half-written line
held back while it is written, and an end that waits for the last container to finish.
"""

import os
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from robovast.common.execution import JOB_LINKS_MANIFEST, job_artifact_rel
from robovast.service import job_log
from robovast.service.app import build_app
from tests.service.null_service import NullService
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore

CID = "campaign-2026-07-17-122000"
_XML = '<testsuite errors="0" failures="0" tests="1"><testcase time="1.0"/></testsuite>'


def _stamp(t, msg, node="node", level="INFO"):
    return f"[{level}] [{t:.6f}] [{node}]: {msg}\n"


@pytest.fixture(name="transport")
def _transport(tmp_path):
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    lt = NullService(store=store)
    lt._campaigns_root = lambda: tmp_path / "results"   # noqa: SLF001
    return lt


def _run(transport, config="cfgA", run="0", *, log=None, sidecars=None, xml=None,
         job_prefix="batch-0", job_index=0, settled=True) -> Path:
    """One run laid out as a campaign lays it out; the job dir's ``logs/`` is returned."""
    cdir = transport._campaigns_root() / CID           # noqa: SLF001
    run_dir = cdir / config / run
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)     # the run dir's decoy
    job_rel = job_artifact_rel(job_index, job_prefix)
    logs = cdir / "_jobs" / job_rel / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    if log is not None:
        (logs / "system.log").write_text(log)
    for name, text in (sidecars or {}).items():
        (logs / f"system_{name}.log").write_text(text)
    manifest = cdir / "_transient" / JOB_LINKS_MANIFEST
    manifest.parent.mkdir(parents=True, exist_ok=True)
    links = yaml.safe_load(manifest.read_text()) if manifest.is_file() else {}
    links[f"{config}/{run}/job"] = f"../../_jobs/{job_rel}"
    manifest.write_text(yaml.safe_dump(links))
    if xml is not None:
        (run_dir / "test.xml").write_text(xml)
    if settled:
        _age(logs, run_dir)
    return logs


def _age(*dirs):
    """Make every file under *dirs* older than the settle time."""
    past = time.time() - 60
    for top in dirs:
        for path in Path(top).rglob("*"):
            if path.is_file():
                os.utime(path, (past, past))


def _live(transport, monkeypatch):
    monkeypatch.setattr(transport, "_is_done", lambda _entry: False)
    transport._campaigns[CID] = object()               # noqa: SLF001


def _messages(chunk):
    return [row.message for row in chunk.rows]


# -- where the log is ------------------------------------------------------------------

def test_the_log_is_read_from_the_job_dir_not_the_runs_empty_logs(transport):
    _run(transport, log=_stamp(1, "real"), xml=_XML)
    chunk = transport.get_job_log(CID, "cfgA/0")
    assert _messages(chunk) == ["real"]
    assert chunk.rows[0].container == "robovast" and chunk.rows[0].node == "node"


def test_a_search_batch_nests_the_job_dir_a_level_deeper(transport):
    _run(transport, log=_stamp(1, "searched"), xml=_XML, job_prefix="batch-3/reps-5")
    assert _messages(transport.get_job_log(CID, "cfgA/0")) == ["searched"]


def test_before_the_manifest_exists_a_run_has_an_empty_log(transport, monkeypatch):
    run_dir = transport._campaigns_root() / CID / "cfgA" / "0"   # noqa: SLF001
    run_dir.mkdir(parents=True)
    _live(transport, monkeypatch)
    chunk = transport.get_job_log(CID, "cfgA/0")
    assert (chunk.rows, chunk.eof) == ([], False)


@pytest.mark.parametrize("job", ["nope/0", "../../etc"])
def test_what_is_not_a_job_of_the_campaign_is_refused(transport, job):
    (transport._campaigns_root() / CID).mkdir(parents=True)    # noqa: SLF001
    with pytest.raises(KeyError):
        transport.get_job_log(CID, job)


def test_a_cursor_this_service_did_not_issue_is_refused(transport):
    _run(transport, log=_stamp(1, "a"), xml=_XML)
    with pytest.raises(ValueError, match="cursor"):
        transport.get_job_log(CID, "cfgA/0", cursor="not-a-cursor!")


# -- rows ---------------------------------------------------------------------------------

def test_a_record_keeps_its_continuation_lines_and_an_unstamped_line_is_its_own_row(
        transport):
    _run(transport, xml=_XML, log="booting without a stamp\n"
         + _stamp(1, "Traceback follows", level="ERROR") + "  File x\n  Error: y\n")
    rows = transport.get_job_log(CID, "cfgA/0").rows
    assert [(r.time_source, r.message) for r in rows] == [
        ("none", "booting without a stamp"),
        ("stamp", "Traceback follows\n  File x\n  Error: y")]
    assert rows[1].level == "ERROR" and rows[1].severity == "error"


def test_every_container_is_in_one_stream_in_stamp_order(transport):
    _run(transport, xml=_XML, log=_stamp(1, "scenario") + _stamp(3, "scenario later"),
         sidecars={"simulation": _stamp(2, "mujoco loaded"), "sut": _stamp(4, "nav up")})
    rows = transport.get_job_log(CID, "cfgA/0").rows
    assert [(r.container, r.message) for r in rows] == [
        ("robovast", "scenario"), ("simulation", "mujoco loaded"),
        ("robovast", "scenario later"), ("sut", "nav up")]


def test_the_cursor_continues_without_repeating_or_skipping(transport, monkeypatch):
    logs = _run(transport, log=_stamp(1, "a"), sidecars={"sut": _stamp(1.5, "nav a")})
    _live(transport, monkeypatch)
    first = transport.get_job_log(CID, "cfgA/0")
    with open(logs / "system.log", "a") as fh:
        fh.write(_stamp(2, "b"))
    with open(logs / "system_sut.log", "a") as fh:
        fh.write(_stamp(2.5, "nav b"))
    _age(logs)
    second = transport.get_job_log(CID, "cfgA/0", cursor=first.cursor)
    assert _messages(first) == ["a", "nav a"]
    assert _messages(second) == ["b", "nav b"]
    again = transport.get_job_log(CID, "cfgA/0", cursor=second.cursor)
    assert again.rows == []


def test_the_last_record_waits_while_its_file_is_written(transport, monkeypatch):
    """The next line may still be one of its continuation lines."""
    logs = _run(transport, log=_stamp(1, "done") + _stamp(2, "Traceback"), settled=False)
    _live(transport, monkeypatch)
    hot = transport.get_job_log(CID, "cfgA/0")
    assert _messages(hot) == ["done"]
    with open(logs / "system.log", "a") as fh:
        fh.write("  File z\n")
    _age(logs)
    cooled = transport.get_job_log(CID, "cfgA/0", cursor=hot.cursor)
    assert _messages(cooled) == ["Traceback\n  File z"]


def test_a_half_written_line_waits_and_is_flushed_when_the_job_is_over(transport,
                                                                        monkeypatch):
    logs = _run(transport, log=_stamp(1, "complete"))
    with open(logs / "system.log", "a") as fh:
        fh.write("[INFO] [2.0] [node]: ha")
    _age(logs)
    _live(transport, monkeypatch)
    live = transport.get_job_log(CID, "cfgA/0")
    assert _messages(live) == ["complete"] and live.eof is False

    del transport._campaigns[CID]                       # noqa: SLF001
    rest = transport.get_job_log(CID, "cfgA/0", cursor=live.cursor)
    assert _messages(rest) == ["ha"]


# -- the end ------------------------------------------------------------------------------

def test_eof_comes_after_the_last_rows_of_a_finished_job(transport):
    _run(transport, log=_stamp(1, "a"), xml=_XML)
    first = transport.get_job_log(CID, "cfgA/0")
    assert _messages(first) == ["a"] and first.eof is False
    assert transport.get_job_log(CID, "cfgA/0", cursor=first.cursor).eof is True


def test_eof_waits_for_the_verdict_to_settle_while_a_sidecar_may_still_flush(
        transport, monkeypatch):
    """A sidecar flushes during the stop grace, after the scenario wrote ``test.xml``."""
    logs = _run(transport, log=_stamp(1, "scenario done"))
    run_dir = transport._campaigns_root() / CID / "cfgA" / "0"   # noqa: SLF001
    (run_dir / "test.xml").write_text(_XML)                       # fresh: not settled
    _live(transport, monkeypatch)
    first = transport.get_job_log(CID, "cfgA/0")
    quiet = transport.get_job_log(CID, "cfgA/0", cursor=first.cursor)
    assert quiet.eof is False, "the verdict is too fresh to end on"

    with open(logs / "system_simulation.log", "a") as fh:
        fh.write(_stamp(5, "recording saved"))
    _age(logs, run_dir)
    last = transport.get_job_log(CID, "cfgA/0", cursor=quiet.cursor)
    assert _messages(last) == ["recording saved"]
    assert transport.get_job_log(CID, "cfgA/0", cursor=last.cursor).eof is True


def test_a_file_replaced_by_a_shorter_one_is_read_again_whole(transport):
    logs = _run(transport, log=_stamp(1, "one") + _stamp(2, "two"), xml=_XML)
    first = transport.get_job_log(CID, "cfgA/0")
    (logs / "system.log").write_text(_stamp(3, "x"))
    _age(logs)
    assert _messages(transport.get_job_log(CID, "cfgA/0", cursor=first.cursor)) == ["x"]


# -- the artifact hint --------------------------------------------------------------------

def test_a_job_the_manifest_does_not_name_is_found_through_the_hint(transport, monkeypatch):
    """A cluster names jobs by their Kubernetes Job, which the manifest does not key on."""
    _run(transport, log=_stamp(1, "packed job"), xml=_XML, job_index=7)
    monkeypatch.setattr(transport, "_job_artifact_hint",
                        lambda cid, name: "_jobs/batch-0/job-7" if name == "camp-b0-j7" else "")
    assert _messages(transport.get_job_log(CID, "camp-b0-j7")) == ["packed job"]


# -- the stream ---------------------------------------------------------------------------

def test_the_stream_pushes_rows_and_ends_a_finished_job(transport):
    _run(transport, log=_stamp(1, "a") + _stamp(2, "b"), xml=_XML)
    with TestClient(build_app(transport, mount_mcp=False)) as client:
        body = client.get(f"/campaigns/{CID}/job-log/stream",
                          params={"job_name": "cfgA/0"}).text
    assert '"message": "a"' in body and '"message": "b"' in body
    assert "id: " in body and body.rstrip().endswith("event: eof\ndata: {}")


def test_the_stream_resumes_from_its_cursor(transport):
    _run(transport, log=_stamp(1, "a"), xml=_XML)
    cursor = transport.get_job_log(CID, "cfgA/0").cursor
    with TestClient(build_app(transport, mount_mcp=False)) as client:
        body = client.get(f"/campaigns/{CID}/job-log/stream", params={"job_name": "cfgA/0"},
                          headers={"Last-Event-ID": cursor}).text
    assert '"message": "a"' not in body and "event: eof" in body


def test_the_stream_names_an_unknown_job_instead_of_hanging(transport):
    (transport._campaigns_root() / CID).mkdir(parents=True)    # noqa: SLF001
    with TestClient(build_app(transport, mount_mcp=False)) as client:
        body = client.get(f"/campaigns/{CID}/job-log/stream",
                          params={"job_name": "nope/0"}).text
    assert "event: streamerror" in body and "event: eof" in body


def test_the_route_answers_rows_and_a_cursor(transport):
    _run(transport, log=_stamp(1, "a"), xml=_XML)
    with TestClient(build_app(transport, mount_mcp=False)) as client:
        reply = client.get(f"/campaigns/{CID}/job-log", params={"job_name": "cfgA/0"}).json()
    assert [r["message"] for r in reply["rows"]] == ["a"] and reply["cursor"]


def test_the_watch_wakes_on_a_write(tmp_path):
    logs = tmp_path / "job" / "logs"
    logs.mkdir(parents=True)
    watch = job_log.LogWatch(tmp_path / "job")
    try:
        (logs / "system.log").write_text("x\n")
        started = time.monotonic()
        watch.wait(5.0)
        assert time.monotonic() - started < 4.0
    finally:
        watch.close()
