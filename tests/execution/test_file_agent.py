# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The file agent of a cluster pod: line files reach the campaign as they grow.

The agent core runs against the real data plane (``build_data_app``), a FastAPI TestClient
standing in for the pod's HTTP transport; the HTTP transport and the whole script run
against the data plane served by uvicorn on a loopback port.
"""

import ast
import io
import os
import signal
import subprocess
import sys
import tarfile
import threading
import time
from importlib.resources import files
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from robovast.common import execution
from robovast.execution.cluster_execution import pod_access, pod_upload
from robovast.execution.data import file_agent
from robovast.execution.data.file_agent import Agent, Inotify, is_line_file
from robovast.service import tar_io
from robovast.service.data_app import build_data_app
from robovast.service.interface import Routes

TOKEN = "file-agent-token"
CAMPAIGN = "camp-2026-01-01-000000"

linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="inotify is Linux")


@pytest.fixture
def plane(tmp_path):
    """``(campaign_dir, transport, bodies)``: the data plane, a transport into it, and
    every tar body it carried."""
    root = tmp_path / "results"
    (root / CAMPAIGN).mkdir(parents=True)
    client = TestClient(build_data_app(root, TOKEN),
                        headers={"Authorization": f"Bearer {TOKEN}"})
    calls = []

    def transport(write_body):
        buf = io.BytesIO()
        write_body(buf)
        calls.append(buf.getvalue())
        resp = client.put(Routes.campaign_outputs(CAMPAIGN), content=buf.getvalue())
        resp.raise_for_status()
        return resp.json()

    with client:
        yield root / CAMPAIGN, transport, calls


def _agent(tmp_path, transport, **kw):
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    return Agent(str(out), str(tmp_path / "file_agent.json"), transport, **kw), out


def _append(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text)


# -- the names the standalone script states --------------------------------------------------

def test_the_names_the_script_states_are_the_ones_the_service_defines():
    assert file_agent.DATA_URL_ENV == pod_access.DATA_URL_ENV
    assert file_agent.CAMPAIGN_ID_ENV == pod_access.CAMPAIGN_ID_ENV
    assert file_agent.TOKEN_ENV == pod_access.TOKEN_ENV
    assert file_agent.IPC_DIR_ENV == pod_upload.IPC_DIR_ENV
    assert file_agent.OUT_DIR_ENV == pod_upload.OUT_DIR_ENV
    assert file_agent.IPC_DIR == execution.IPC_DIR
    assert file_agent.OUT_DIR == pod_upload.OUT_DIR
    assert file_agent.MAIN_CONTAINER == execution.MAIN_CONTAINER
    assert file_agent.AGENT_CONTAINER == pod_upload.AGENT_CONTAINER
    assert file_agent.OFFSET_HEADER == tar_io.OFFSET_HEADER
    assert file_agent.INCOMING_SUFFIX == tar_io.INCOMING_SUFFIX
    assert file_agent.IN_PROGRESS_SUFFIX == pod_upload.IN_PROGRESS_SUFFIX


def test_the_script_is_standard_library_only():
    """It runs on the sidecar image, copied on its own: nothing of robovast, nothing
    installed."""
    source = files("robovast.execution.data").joinpath("file_agent.py").read_text()
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "a relative import cannot resolve in the pod"
            imported.add(node.module.split(".")[0])
    assert imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)


def test_the_script_is_shipped_with_the_campaign_and_reserved_at_the_config_mount():
    assert execution.FILE_AGENT_SCRIPT in execution.RESERVED_CONFIG_MOUNT_NAMES
    assert pod_upload.AGENT_SCRIPT == f"/config/{execution.FILE_AGENT_SCRIPT}"
    assert files("robovast.execution.data").joinpath(execution.FILE_AGENT_SCRIPT).is_file()


def test_a_campaign_stages_the_script_into_its_transient_dir(tmp_path):
    (tmp_path / "s.vast").write_text("version: 6\n", encoding="utf-8")
    (tmp_path / "s.osc").write_text("scenario x:\n    do serial:\n        wait elapsed(1s)\n",
                                    encoding="utf-8")
    out = tmp_path / "campaign"
    execution.prepare_campaign_configs(str(out), {
        "vast": str(tmp_path / "s.vast"), "scenario_file": str(tmp_path / "s.osc"),
        "configs": [{"name": "c1", "config": {}}], "execution": {"runs": 1}}, cluster=True)
    staged = out / "_transient" / execution.FILE_AGENT_SCRIPT
    assert staged.read_bytes() == Path(file_agent.__file__).read_bytes()


# -- what is shipped --------------------------------------------------------------------------

@pytest.mark.parametrize("rel", [
    "cfg/0/logs/system.log", "_jobs/job-1/logs/simulation.log", "cfg/0/resource_usage_main.csv",
    "cfg/0/events.jsonl", "cfg/0/deep/er/table.csv",
])
def test_line_files_are_shipped(rel):
    assert is_line_file(rel)


@pytest.mark.parametrize("rel", [
    "cfg/0/rosbag2/metadata.csv", "cfg/0/rosbag2/rosbag2_0.log",
    "cfg/0/logs/rosout_bag/x.log", "cfg/0/logs/rosout_bag/metadata.jsonl",
    "cfg/0/samples.csv.part", "cfg/0/logs/system.log.part",
    "cfg/0/logs/system.log.robovast-incoming", "cfg/0/run.log", "cfg/0/run.npz",
    "cfg/0/logs/trace.bin",
])
def test_everything_else_is_not(rel):
    assert not is_line_file(rel)


# -- the agent against the data plane ---------------------------------------------------------

def test_growth_lands_appended_and_a_partial_line_waits(tmp_path, plane):
    campaign, transport, _calls = plane
    agent, out = _agent(tmp_path, transport)
    log = out / "cfg" / "0" / "logs" / "system.log"

    _append(log, "one\ntwo\npart")
    agent.notice([str(log)])
    assert agent.deliver()
    assert (campaign / "cfg/0/logs/system.log").read_text() == "one\ntwo\n"

    _append(log, "ial\nthree\n")
    agent.notice([str(log)])
    assert agent.deliver()
    assert (campaign / "cfg/0/logs/system.log").read_text() == "one\ntwo\npartial\nthree\n"

    _append(log, "tail without newline")
    agent.notice([str(log)])
    assert agent.deliver()
    assert (campaign / "cfg/0/logs/system.log").read_text().endswith("three\n")
    assert file_agent.drain(agent)
    assert (campaign / "cfg/0/logs/system.log").read_text() == log.read_text()


def test_a_member_carries_its_offset(tmp_path, plane):
    _campaign, transport, calls = plane
    agent, out = _agent(tmp_path, transport)
    csv = out / "cfg" / "0" / "resource_usage_main.csv"
    _append(csv, "a,b\n")
    agent.scan()
    agent.deliver()
    _append(csv, "1,2\n")
    agent.scan()
    agent.deliver()
    with tarfile.open(fileobj=io.BytesIO(calls[-1]), mode="r|") as tar:
        members = [(m.name, m.pax_headers.get(tar_io.OFFSET_HEADER)) for m in tar]
    assert members == [("cfg/0/resource_usage_main.csv", "4")]


def test_a_restarted_agent_does_not_resend(tmp_path, plane):
    campaign, transport, calls = plane
    agent, out = _agent(tmp_path, transport)
    log = out / "cfg" / "0" / "logs" / "system.log"
    _append(log, "one\n")
    agent.scan()
    assert agent.deliver()
    sent = len(calls)

    restarted, _ = _agent(tmp_path, transport)
    restarted.scan()
    assert restarted.deliver()
    assert len(calls) == sent, "nothing grew, so nothing is sent"
    _append(log, "two\n")
    restarted.scan()
    assert restarted.deliver()
    assert (campaign / "cfg/0/logs/system.log").read_text() == "one\ntwo\n"


def test_a_range_the_campaign_already_holds_is_skipped(tmp_path, plane):
    campaign, transport, _calls = plane
    agent, out = _agent(tmp_path, transport)
    log = out / "cfg" / "0" / "logs" / "system.log"
    _append(log, "one\n")
    agent.scan()
    assert agent.deliver()
    (campaign / "cfg/0/logs/system.log").write_text("one\ntwo\nthree\n")
    _append(log, "two\n")
    agent.notice([str(log)])
    assert agent.deliver()
    assert (campaign / "cfg/0/logs/system.log").read_text() == "one\ntwo\nthree\n"
    assert agent.whole == set() and not agent.pending


def test_a_file_that_did_not_continue_is_resynced_whole(tmp_path, plane):
    """A range that starts past the campaign's end is refused and the file sent whole."""
    campaign, transport, _calls = plane
    agent, out = _agent(tmp_path, transport)
    log = out / "cfg" / "0" / "logs" / "system.log"
    _append(log, "one\ntwo\n")
    agent.scan()
    assert agent.deliver()
    (campaign / "cfg/0/logs/system.log").write_text("x\n")
    _append(log, "three\n")
    agent.notice([str(log)])
    assert agent.deliver()
    assert (campaign / "cfg/0/logs/system.log").read_text() == "x\n", "not appended"
    assert agent.pending
    assert agent.deliver()
    assert (campaign / "cfg/0/logs/system.log").read_text() == "one\ntwo\nthree\n"


def test_a_file_rewritten_shorter_is_sent_whole(tmp_path, plane):
    campaign, transport, _calls = plane
    agent, out = _agent(tmp_path, transport)
    csv = out / "cfg" / "0" / "table.csv"
    _append(csv, "a\nb\nc\n")
    agent.scan()
    assert agent.deliver()
    csv.write_text("z\n")
    agent.notice([str(csv)])
    assert agent.deliver()
    assert (campaign / "cfg/0/table.csv").read_text() == "z\n"


def test_the_byte_budget_splits_a_delivery_and_the_rest_follows(tmp_path, plane):
    campaign, transport, calls = plane
    agent, out = _agent(tmp_path, transport, max_bytes=10)
    csv = out / "cfg" / "0" / "table.csv"
    _append(csv, "".join(f"{i}\n" for i in range(20)))
    agent.scan()
    rounds = 0
    while agent.pending and rounds < 20:
        assert agent.deliver()
        rounds += 1
    assert len(calls) > 3
    assert (campaign / "cfg/0/table.csv").read_text() == csv.read_text()


def test_a_failed_delivery_changes_nothing(tmp_path, plane):
    campaign, transport, _calls = plane

    def down(_write_body):
        raise ConnectionRefusedError("service rolling")

    agent, out = _agent(tmp_path, down)
    log = out / "cfg" / "0" / "logs" / "system.log"
    _append(log, "one\n")
    agent.scan()
    assert not agent.deliver()
    assert agent.pending and agent.offsets == {}
    agent.transport = transport
    assert agent.deliver()
    assert (campaign / "cfg/0/logs/system.log").read_text() == "one\n"


# -- inotify ----------------------------------------------------------------------------------

@linux_only
def test_inotify_sees_a_write_in_a_directory_created_after_the_watch(tmp_path):
    with Inotify() as ino:
        ino.add_tree(str(tmp_path))
        sub = tmp_path / "cfg" / "0" / "logs"
        sub.mkdir(parents=True)
        seen = set()
        deadline = time.monotonic() + 5
        while str(sub) not in seen and time.monotonic() < deadline:
            seen |= ino.wait(0.5)
        _append(sub / "system.log", "one\n")
        deadline = time.monotonic() + 5
        while str(sub / "system.log") not in seen and time.monotonic() < deadline:
            seen |= ino.wait(0.5)
        assert str(sub / "system.log") in seen


@linux_only
def test_inotify_wait_times_out_and_wakes(tmp_path):
    with Inotify() as ino:
        ino.add_tree(str(tmp_path))
        start = time.monotonic()
        assert ino.wait(0.1) == set()
        assert time.monotonic() - start < 2
        ino.wake()
        assert ino.wait(10) == set()


# -- the pod's transport and the whole script -------------------------------------------------

@pytest.fixture
def served(tmp_path):
    """The data plane served over loopback HTTP: ``(campaign_dir, data_url)``."""
    import uvicorn  # pylint: disable=import-outside-toplevel
    root = tmp_path / "results"
    (root / CAMPAIGN).mkdir(parents=True)
    server = uvicorn.Server(uvicorn.Config(build_data_app(root, TOKEN), host="127.0.0.1",
                                           port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not (server.started and server.servers) and time.monotonic() < deadline:
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield root / CAMPAIGN, f"http://127.0.0.1:{port}{Routes.DATA}"
    server.should_exit = True
    thread.join(timeout=10)


def test_the_http_transport_delivers_chunked(tmp_path, served):
    campaign, url = served
    agent, out = _agent(tmp_path, file_agent.http_transport(url, CAMPAIGN, TOKEN))
    log = out / "cfg" / "0" / "logs" / "system.log"
    _append(log, "one\ntwo\n")
    agent.scan()
    assert agent.deliver()
    assert (campaign / "cfg/0/logs/system.log").read_text() == "one\ntwo\n"


def test_the_http_transport_raises_on_a_refusal(tmp_path, served):
    _campaign, url = served
    agent, out = _agent(tmp_path, file_agent.http_transport(url, CAMPAIGN, "wrong"))
    _append(out / "cfg" / "0" / "logs" / "system.log", "one\n")
    agent.scan()
    assert not agent.deliver()


@linux_only
def test_the_script_ships_while_running_and_ends_on_the_markers(tmp_path, served):
    campaign, url = served
    out, ipc = tmp_path / "out", tmp_path / "ipc"
    out.mkdir()
    ipc.mkdir()
    env = dict(os.environ, **{
        file_agent.DATA_URL_ENV: url, file_agent.CAMPAIGN_ID_ENV: CAMPAIGN,
        file_agent.TOKEN_ENV: TOKEN, file_agent.OUT_DIR_ENV: str(out),
        file_agent.IPC_DIR_ENV: str(ipc)})
    script = str(files("robovast.execution.data").joinpath("file_agent.py"))
    proc = subprocess.Popen([sys.executable, script, "--grace", "30", "simulation"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        time.sleep(0.5)
        log = out / "cfg" / "0" / "logs" / "system.log"
        _append(log, "one\npart")
        target = campaign / "cfg/0/logs/system.log"
        deadline = time.monotonic() + 15
        while not (target.exists() and target.read_text() == "one\n"):
            assert time.monotonic() < deadline, "the growth never arrived"
            time.sleep(0.1)

        (ipc / "done.main").touch()
        time.sleep(1.5)
        assert proc.poll() is None, "a sidecar's marker is still missing"
        (ipc / "done.simulation").touch()
        assert proc.wait(timeout=15) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        output = proc.communicate()[0]
    assert target.read_text() == "one\npart", output
    assert (ipc / "done.agent").exists()
    assert (ipc / file_agent.STATE_FILE).exists()


@linux_only
def test_the_script_drains_and_signs_on_sigterm(tmp_path, served):
    campaign, url = served
    out, ipc = tmp_path / "out", tmp_path / "ipc"
    out.mkdir()
    ipc.mkdir()
    env = dict(os.environ, **{
        file_agent.DATA_URL_ENV: url, file_agent.CAMPAIGN_ID_ENV: CAMPAIGN,
        file_agent.TOKEN_ENV: TOKEN, file_agent.OUT_DIR_ENV: str(out),
        file_agent.IPC_DIR_ENV: str(ipc)})
    _append(out / "cfg" / "0" / "events.jsonl", '{"a": 1}\n{"b"')
    script = str(files("robovast.execution.data").joinpath("file_agent.py"))
    proc = subprocess.Popen([sys.executable, script], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        time.sleep(1.0)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=15) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        output = proc.communicate()[0]
    assert (campaign / "cfg/0/events.jsonl").read_text() == '{"a": 1}\n{"b"', output
    assert (ipc / "done.agent").exists()


def test_the_script_refuses_to_start_without_its_access(monkeypatch):
    for name in (file_agent.DATA_URL_ENV, file_agent.CAMPAIGN_ID_ENV, file_agent.TOKEN_ENV):
        monkeypatch.delenv(name, raising=False)
    assert file_agent.main([]) == 2


# -- the pod ----------------------------------------------------------------------------------

def test_the_uploader_waits_for_the_agent():
    command = pod_upload.uploader_command("c-1", ["simulation"])
    assert 'WAIT_FOR="main simulation agent"' in command[2]
    assert pod_upload.uploader_command("c-1", ["simulation", "agent"]) == command


def test_the_agent_command_names_the_sidecars_it_waits_for():
    assert pod_upload.agent_command(["sut", "simulation"]) == [
        "python3", "/config/file_agent.py", "--grace", str(pod_upload.AGENT_GRACE_SECONDS),
        "sut", "simulation"]
    assert pod_upload.AGENT_GRACE_SECONDS < pod_upload.UPLOAD_GRACE_SECONDS
    with pytest.raises(ValueError):
        pod_upload.agent_command(["a b"])
