# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The tap: a command started in a live job's simulation container, relayed line by line.

Driven through a fake exec runner, as the health read's tests are: what is under test is what
the service decides before anything runs -- the job is running, the simulator has a tap, no tap
is open on the job, the probe is recorded -- and how the runner's lines reach a reader. The
cluster runner's ``stream_in`` is then tried against a fake of the exec stream it drives.
"""

import contextlib
import json
import threading
import time
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from robovast.common.campaign_data import probed_runs
from robovast.common.execution import JOB_LINKS_MANIFEST, job_artifact_rel
from robovast.execution.control_server import ControllerState
from robovast.service.interface import (TAP_MAX_S, JobSummary, ListJobsResponse, Routes,
                                       TapEnd, TapRow)
from robovast.service.tap import TapStream
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore
from tests.service.null_service import NullService

_CID = "campaign-2026-09-20-120000"
_JOB = "cfga/1"

_ROS_NO_BACKEND_VAST = """\
version: 5
metadata: {name: t}
configuration:
- name: cfga
execution:
  runs: 1
  mode: ros2
  containers:
    simulation: {image: sim-image:1}
    sut: {image: sut-image:1}
"""

_ROQSIM_VAST = """\
version: 5
metadata: {name: t}
configuration:
- name: cfga
execution:
  runs: 1
  mode: ros2
  containers:
    simulation: {image: sim-image:1, backend: roqsim, config: w.yaml}
"""


class _OneJobService(NullService):
    """A service with one running job, addressed the way a Job's pod is: the target is the
    container's role, the run writes under ``/out/<config>/<run>``, and a probe is recorded on
    the job's artifact directory against the run it carries."""

    def list_jobs(self, campaign_id: str):
        del campaign_id
        return ListJobsResponse(jobs=[JobSummary(job_name=_JOB, status="running")])

    def _job_state_target(self, campaign_id: str, job_name: str, role: str) -> tuple:
        del campaign_id
        return role, f"/out/{job_name}"

    def _job_probe_dir(self, campaign_id: str, job_name: str) -> tuple:
        del campaign_id
        return str(Path("_jobs") / job_artifact_rel(0, "batch-0")), (job_name,)


@pytest.fixture
def transport(tmp_path):
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    service = _OneJobService(store=store)
    service._campaigns_root = lambda: tmp_path / "results"  # noqa: SLF001
    return service


def _campaign(transport, vast=_ROS_NO_BACKEND_VAST):
    """One live campaign with a running run, laid out as a campaign's results are."""
    from robovast.service.service_base import _TrackedCampaign
    cdir = transport._campaigns_root() / _CID
    config, run = _JOB.split("/")
    (cdir / config / run / "logs").mkdir(parents=True)
    job_rel = job_artifact_rel(0, "batch-0")
    (cdir / "_jobs" / job_rel / "logs").mkdir(parents=True)
    manifest = cdir / "_transient" / JOB_LINKS_MANIFEST
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(yaml.safe_dump({f"{_JOB}/job": f"../../_jobs/{job_rel}"}))
    (cdir / "_config").mkdir()
    (cdir / "_config" / "campaign.vast").write_text(vast, encoding="utf-8")
    state = ControllerState(phase="running", runs={"total": 1, "completed": 0})
    transport._campaigns[_CID] = _TrackedCampaign(campaign_id=_CID, results_dir=str(cdir),
                                                 state=state)
    return cdir


class _Runner:
    """An exec runner whose ``stream_in`` prints *lines* and then answers as told."""

    def __init__(self, lines=("a", "b", "c"), result=(0, False), hold=False):
        self.lines, self.result, self.hold = lines, result, hold
        self.calls = []
        self.started = threading.Event()

    def stream_in(self, target, argv, *, limit_s, on_line, should_stop, env=None):
        self.calls.append((target, argv, limit_s))
        self.started.set()
        for line in self.lines:
            on_line(line)
        while self.hold and not should_stop():
            time.sleep(0.02)
        if self.hold:
            return None, False
        return self.result


def _tap(transport, monkeypatch, runner, **kwargs):
    monkeypatch.setattr(transport, "_exec_runner", lambda: runner)
    return transport.tap_job(_CID, _JOB, kwargs.pop("selection", ["/odom"]),
                             max_seconds=kwargs.pop("max_seconds", 10), **kwargs)


def _drain(tap):
    items = list(tap)
    tap.close()
    return items


# -- the relay ---------------------------------------------------------------------------


def test_the_lines_are_relayed_in_order_and_the_end_carries_the_exit_code(transport, monkeypatch):
    _campaign(transport)
    items = _drain(_tap(transport, monkeypatch, _Runner(("x: 1", "x: 2"), (0, False))))
    assert [row.line for row in items[:-1]] == ["x: 1", "x: 2"]
    assert all(isinstance(row, TapRow) and row.t_wall > 0 for row in items[:-1])
    assert items[-1] == TapEnd(exit_code=0, timed_out=False)


def test_the_command_is_the_backends_bounded_and_in_the_runs_environment(transport, monkeypatch):
    """The tap runs where ``ros2`` resolves, and under a bound of its own in the container,
    since ending the relay ends no process in the container."""
    _campaign(transport)
    runner = _Runner()
    _drain(_tap(transport, monkeypatch, runner, selection=["/odom", "csv"], max_seconds=7))
    (target, argv, limit_s), = runner.calls
    assert target == "simulation" and limit_s == 7
    assert argv[:2] == ["/bin/bash", "-c"]
    body = argv[2].splitlines()[-1]
    assert body.startswith("exec timeout --signal=INT --kill-after=5 7 ")
    assert body.endswith("env PYTHONUNBUFFERED=1 ros2 topic echo --csv /odom")


def test_max_seconds_is_capped_and_the_cut_is_reported(transport, monkeypatch):
    _campaign(transport)
    runner = _Runner(("only",), (124, True))
    items = _drain(_tap(transport, monkeypatch, runner, max_seconds=10 * TAP_MAX_S))
    assert runner.calls[0][2] == TAP_MAX_S
    assert items[-1] == TapEnd(exit_code=124, timed_out=True)


def test_the_probe_is_recorded_before_the_command_runs(transport, monkeypatch):
    _campaign(transport)
    cdir = transport._campaigns_root() / _CID
    seen = []

    class _Recording(_Runner):
        def stream_in(self, *args, **kwargs):
            seen.append(dict(probed_runs(cdir)))
            return super().stream_in(*args, **kwargs)

    _drain(_tap(transport, monkeypatch, _Recording(), selection=["/odom", "/tf"], source="mcp"))
    assert seen and list(seen[0]) == [_JOB], "the probe was not recorded before the exec"
    entry = probed_runs(cdir)[_JOB]
    assert (entry["kind"], entry["source"]) == ("probed", "mcp")
    assert entry["detail"] == "tap /odom /tf for 10s"


def test_a_second_tap_on_the_same_job_is_refused_until_the_first_ends(transport, monkeypatch):
    _campaign(transport)
    first_runner = _Runner(("l",), hold=True)
    first = _tap(transport, monkeypatch, first_runner)
    assert first_runner.started.wait(2)
    with pytest.raises(RuntimeError, match="already open"):
        transport.tap_job(_CID, _JOB, ["/odom"], max_seconds=10)
    first.close()
    assert first.join(2), "closing the stream did not end the exec"
    # The slot is released with the exec, so the next reader gets a tap of their own.
    second = _tap(transport, monkeypatch, _Runner(("m",)))
    assert [getattr(row, "line", None) for row in _drain(second)] == ["m", None]


def test_a_refusal_records_nothing(transport, monkeypatch):
    _campaign(transport)
    cdir = transport._campaigns_root() / _CID
    monkeypatch.setattr(transport, "_exec_runner", lambda: _Runner())
    with pytest.raises((KeyError, RuntimeError)):
        transport.tap_job(_CID, "cfga/9", ["/odom"], max_seconds=10)
    assert probed_runs(cdir) == {}


def test_a_simulator_without_a_tap_is_refused_by_name(transport, monkeypatch):
    """roqsim declines: its recording is the live view. Refused naming it, and nothing recorded,
    since a tap that printed nothing would otherwise read as a run publishing nothing."""
    pytest.importorskip("robovast_sim_roqsim")
    cdir = _campaign(transport, _ROQSIM_VAST)
    monkeypatch.setattr(transport, "_exec_runner", lambda: _Runner())
    with pytest.raises(ValueError, match="no tap for roqsim"):
        transport.tap_job(_CID, _JOB, ["/odom"], max_seconds=10)
    assert probed_runs(cdir) == {}


def test_closing_the_stream_ends_the_exec_and_reads_as_ended(transport, monkeypatch):
    _campaign(transport)
    runner = _Runner(("l",), hold=True)
    tap = _tap(transport, monkeypatch, runner)
    assert tap.poll(2).line == "l"
    tap.close()
    assert runner.started.wait(1) and tap.join(2)
    assert tap.poll(0.1) == TapEnd(exit_code=None, timed_out=False)


def test_a_runner_that_cannot_open_the_exec_raises_in_the_reader():
    def run(on_line, should_stop):
        del on_line, should_stop
        raise RuntimeError("could not open the exec: no pod")

    tap = TapStream(run)
    with pytest.raises(RuntimeError, match="no pod"):
        while tap.poll(2) is None:
            pass


# -- the cluster runner's stream_in -------------------------------------------------------

def _kube_runner():
    from robovast.execution.cluster_execution.kube_exec_runner import KubeExecRunner
    return KubeExecRunner("ns", stage_dir=lambda s: s, discard_staged=lambda s: False,
                          token_for=lambda s: "t")


def test_the_kube_runner_maps_onto_exec_stream_with_both_line_callbacks(monkeypatch):
    seen = {}

    def exec_stream(pod, namespace, container, command, *, limit_s, stdin_data=None,
                    on_stdout_line=None, on_stderr_line=None, should_stop=None):
        seen.update(pod=pod, namespace=namespace, container=container, command=command,
                    limit_s=limit_s, stop=should_stop)
        on_stdout_line("out")
        on_stderr_line("err")
        return 0, "out\n", "err\n", False

    monkeypatch.setattr("robovast.execution.cluster_execution.kube_client.exec_stream",
                        exec_stream)
    lines = []

    def stop():
        return False

    code, timed_out = _kube_runner().stream_in(
        ("pod-1", "simulation"), ["ros2", "topic", "list"], limit_s=9, on_line=lines.append,
        should_stop=stop, env={"ignored": "1"})
    assert (code, timed_out) == (0, False) and lines == ["out", "err"]
    assert seen == {"pod": "pod-1", "namespace": "ns", "container": "simulation",
                    "command": ["ros2", "topic", "list"], "limit_s": 9, "stop": stop}


def test_the_kube_runner_reads_a_stop_as_the_normal_end(monkeypatch):
    from robovast.common.errors import CampaignStopped

    def exec_stream(*args, **kwargs):
        del args, kwargs
        raise CampaignStopped("exec in pod-1/simulation stopped by request")

    monkeypatch.setattr("robovast.execution.cluster_execution.kube_client.exec_stream",
                        exec_stream)
    assert _kube_runner().stream_in(("pod-1", "simulation"), ["x"], limit_s=5,
                                  on_line=lambda _l: None, should_stop=lambda: True) == \
        (None, False)


# -- over HTTP: the route, and the client that reads it ------------------------------------


class _Impl:
    """A service whose tap is a canned stream, or a refusal."""

    def __init__(self, lines=("p", "q"), refuse=None):
        self.lines, self.refuse = lines, refuse
        self.calls = []

    def tap_job(self, campaign_id, job_name, selection=None, *, max_seconds=TAP_MAX_S,
                source="api"):
        self.calls.append((campaign_id, job_name, selection, max_seconds, source))
        if self.refuse is not None:
            raise self.refuse
        lines = self.lines

        def run(on_line, should_stop):
            del should_stop
            for line in lines:
                on_line(line)
            return 0, False

        return TapStream(run)

    def shutdown(self):
        pass


def _stream(app, path, params):
    from fastapi.testclient import TestClient
    timer = threading.Timer(5, lambda: setattr(app.state, "should_exit", lambda: True))
    timer.start()
    try:
        with TestClient(app) as client:
            with client.stream("GET", path, params=params) as response:
                assert response.headers["content-type"].startswith("text/event-stream")
                return "".join(response.iter_text())
    finally:
        timer.cancel()


def _events(body):
    out = []
    for frame in body.split("\n\n"):
        event, data = "message", None
        for line in frame.splitlines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        if data is not None:
            out.append((event, json.loads(data)))
    return out


def test_the_route_relays_lines_and_ends_with_the_exit_code():
    from robovast.service.app import build_app
    impl = _Impl()
    body = _stream(build_app(impl), Routes.job_tap("camp-1"),
                   {"job_name": "cfga/1", "selection": "/a,/b", "max_seconds": 3})
    events = _events(body)
    assert [(e, d["line"]) for e, d in events if e == "line"] == [("line", "p"), ("line", "q")]
    assert events[-1] == ("eof", {"exit_code": 0, "timed_out": False})
    assert impl.calls == [("camp-1", "cfga/1", ["/a", "/b"], 3, "api")]


def test_the_route_reports_a_refusal_as_a_streamerror():
    from robovast.service.app import build_app
    body = _stream(build_app(_Impl(refuse=ValueError("no tap for roqsim: nothing to relay"))),
                   Routes.job_tap("camp-1"), {"job_name": "cfga/1"})
    assert _events(body) == [("streamerror", "no tap for roqsim: nothing to relay"), ("eof", {})]


class _Response:
    ok = True
    status_code = 200
    url = "http://localhost/x"
    headers: dict = {}

    def __init__(self, frames):
        self.frames = frames
        self.closed = False

    def iter_lines(self, decode_unicode=True):
        del decode_unicode
        yield from self.frames

    def close(self):
        self.closed = True


def test_the_http_client_reads_the_stream_as_the_interfaces_iterator(monkeypatch):
    from robovast.service.http_client import HTTPTransport
    frames = [": open", "", "event: heartbeat", "data: {}", "",
              "event: line", 'data: {"t_wall": 1.0, "line": "p"}', "",
              "event: line", 'data: {"t_wall": 2.0, "line": "q"}', "",
              "event: eof", 'data: {"exit_code": 124, "timed_out": true}', ""]
    resp = _Response(frames)
    seen = {}
    client = HTTPTransport.__new__(HTTPTransport)
    client.base_url = "http://localhost:8000"
    client.session = type("S", (), {"get": lambda self, url, **kw: seen.update(url=url, **kw)
                                    or resp})()
    items = list(client.tap_job("camp-1", "cfga/1", ["/a"], max_seconds=5))
    assert [getattr(i, "line", None) for i in items] == ["p", "q", None]
    assert items[-1] == TapEnd(exit_code=124, timed_out=True)
    assert seen["url"].endswith(Routes.job_tap("camp-1"))
    assert seen["params"] == {"job_name": "cfga/1", "max_seconds": 5, "selection": "/a"}
    assert seen["stream"] is True and resp.closed


def test_the_http_client_raises_the_services_sentence_on_a_streamerror():
    from robovast.service.http_client import HTTPTransport
    from robovast.service.interface import ServiceError
    resp = _Response(["event: streamerror", 'data: "no tap for roqsim"', "", "event: eof",
                      "data: {}", ""])
    client = HTTPTransport.__new__(HTTPTransport)
    client.base_url = "http://localhost:8000"
    client.session = type("S", (), {"get": lambda self, url, **kw: resp})()
    with pytest.raises(ServiceError, match="no tap for roqsim"):
        list(client.tap_job("camp-1", "cfga/1"))


# -- the CLI and the MCP tool ---------------------------------------------------------------


class _Client:
    base_url = "http://localhost:8000"

    def __init__(self, end=TapEnd(exit_code=0, timed_out=False)):
        self.end = end
        self.calls = []

    def tap_job(self, campaign_id, job_name, selection=None, *, max_seconds=TAP_MAX_S,
                source="api"):
        self.calls.append((campaign_id, job_name, list(selection or []), max_seconds, source))
        yield TapRow(t_wall=1.0, line="x: 1")
        yield TapRow(t_wall=2.0, line="x: 2")
        yield self.end


def test_the_cli_prints_the_lines_and_says_how_the_tap_ended(monkeypatch):
    from robovast.client import campaign_cli
    client = _Client(TapEnd(exit_code=124, timed_out=True))

    @contextlib.contextmanager
    def _service(*_a, **_k):
        yield client, "fake service"

    monkeypatch.setattr(campaign_cli, "service_client", _service)
    result = CliRunner().invoke(campaign_cli.campaign,
                                ["tap", "cfga/1", "camp-1", "--select", "/a,/b",
                                 "--max-seconds", "4"])
    assert result.exit_code == 0, result.output
    assert "x: 1\nx: 2\n" in result.output
    assert "4s bound was reached" in result.output
    assert client.calls == [("camp-1", "cfga/1", ["/a", "/b"], 4, "cli")]


def test_the_mcp_tool_collects_the_lines_within_its_own_bound(monkeypatch):
    from robovast.mcp_server import service_access
    from robovast.mcp_server.plugins import execution
    client = _Client()
    monkeypatch.setattr(service_access, "service_client", lambda: client)
    out = execution.tap_job("camp-1", "cfga/1", ["/a"], max_seconds=90)
    assert out["lines"] == ["x: 1", "x: 2"] and out["count"] == 2
    assert (out["exit_code"], out["timed_out"], out["max_seconds"]) == (0, False, 30)
    assert out["stream_url"] == ("http://localhost:8000/campaigns/camp-1/job-tap"
                                 "?job_name=cfga%2F1&selection=%2Fa&max_seconds=30")
    assert client.calls == [("camp-1", "cfga/1", ["/a"], 30, "mcp")]


def test_the_mcp_tool_fails_loudly_without_a_service(monkeypatch):
    from robovast.mcp_server import service_access
    from robovast.mcp_server.plugins import execution
    monkeypatch.setattr(service_access, "service_client", lambda: None)
    assert "error" in execution.tap_job("camp-1", "cfga/1")


def test_the_mcp_tool_hands_a_refusal_through_as_the_sentence(monkeypatch):
    from robovast.mcp_server import service_access
    from robovast.mcp_server.plugins import execution

    class _Refusing:
        def tap_job(self, *a, **k):
            del a, k
            raise ValueError("no tap for roqsim: nothing to relay")
            yield  # pylint: disable=unreachable

    monkeypatch.setattr(service_access, "service_client", lambda: _Refusing())
    assert execution.tap_job("camp-1", "cfga/1")["error"].startswith("no tap for roqsim")


def test_the_interface_default_refuses_naming_the_implementation():
    from types import SimpleNamespace

    from robovast.service.interface import RobovastInterface, UnsupportedOperation
    with pytest.raises(UnsupportedOperation,
                       match="tap_job is not supported by the test implementation"):
        RobovastInterface.tap_job(SimpleNamespace(IMPLEMENTATION="test"), "c", "j")
