# Copyright (C) 2026 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""The compose worker borrowing the parent's auxiliary-container runner.

A ``.vast`` declaring ``plugins:`` composes in a subprocess, where the backend's runner
factory -- a ContextVar -- does not exist. Before the proxy, that made external variation
plugins and aux-container variations mutually exclusive on a cluster backend: a campaign
using both refused to compose at all.

The server side is exercised over a real Unix socket rather than a mock, because the
contract being kept is a wire one: what these tests would miss by stubbing the transport
is exactly what broke.
"""

import os
import subprocess
import threading

import pytest

from robovast.common.container_runner_proxy import (ContainerRunnerServer,
                                                    proxy_container_runner_factory)
from robovast.common.variation.container_runner import ContainerSpec

SPEC = ContainerSpec(image="ghcr.io/example/scenery_builder",
                     command_prefix=["scenery"], env={"A": "1"})


class FakeRunner:
    """Stands in for a backend runner living in the parent process."""

    def __init__(self, spec, workspace, lines=(), fail=None):
        self.spec = spec
        self.workspace = workspace
        self.calls = []
        self.closed = False
        self._lines = list(lines)
        self._fail = fail

    def run(self, command, progress_update_callback=None):
        self.calls.append(list(command))
        for line in self._lines:
            if progress_update_callback:
                progress_update_callback(line)
        if self._fail is not None:
            raise self._fail

    def close(self):
        self.closed = True


class FakeRunnerWithExpose(FakeRunner):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.exposed = []

    def expose(self, host_path, container_path):
        self.exposed.append((host_path, container_path))


@pytest.fixture(name="serve")
def _serve(tmp_path):
    """Start a server for a factory, yield a worker-side factory, always stop it."""
    servers = []

    def start(factory):
        # A path per server: in production each composition gets a fresh mkdtemp job
        # dir, so the name is only unique here by counting.
        server = ContainerRunnerServer(factory, str(tmp_path / f"aux{len(servers)}.sock"))
        server.start()
        servers.append(server)
        return proxy_container_runner_factory(server.socket_path)

    yield start
    for server in servers:
        server.stop()


def test_runner_crosses_the_process_boundary(serve, tmp_path):
    """The worker gets the parent's workspace and reaches the parent's runner."""
    made = []

    def factory(spec):
        r = FakeRunner(spec, str(tmp_path / "ws"), lines=["building", "done"])
        made.append(r)
        return r

    runner = serve(factory)(SPEC)

    # The workspace is a path, and parent and worker share a filesystem -- which is
    # what lets the proxy carry no data.
    assert runner.workspace == str(tmp_path / "ws")
    # The spec arrives intact, not as a name: the cluster factory execs into the
    # container the spec's image is named after.
    assert made[0].spec == SPEC

    seen = []
    runner.run(["generate", "map"], seen.append)
    assert made[0].calls == [["generate", "map"]]
    # Streamed while the command runs, not returned at the end, so a plugin's progress
    # reaches the campaign log live.
    assert seen == ["building", "done"]

    runner.close()
    assert made[0].closed is True


def test_failed_command_keeps_returncode_cmd_and_output(serve, tmp_path):
    """CalledProcessError crosses field by field, ``output`` included.

    Callers read ``output`` to report *why* a container command failed; a message-only
    round trip drops the one thing they need.
    """
    failure = subprocess.CalledProcessError(3, ["scenery", "x"], output="mesh is not manifold")
    runner = serve(lambda spec: FakeRunner(spec, str(tmp_path), fail=failure))(SPEC)

    with pytest.raises(subprocess.CalledProcessError) as ei:
        runner.run(["x"])
    assert ei.value.returncode == 3
    assert ei.value.cmd == ["scenery", "x"]
    assert ei.value.output == "mesh is not manifold"


def test_other_failures_cross_as_runtime_error(serve, tmp_path):
    runner = serve(lambda spec: FakeRunner(spec, str(tmp_path),
                                           fail=ValueError("pod is gone")))(SPEC)
    with pytest.raises(RuntimeError, match="pod is gone"):
        runner.run(["x"])


def test_expose_is_offered_only_when_the_real_runner_has_it(serve, tmp_path):
    """``expose`` is deliberately outside the Protocol; the proxy must not invent it.

    A runner that cannot place a tree at a fixed absolute path does not define it, and
    the caller then refuses rather than running without the mount. A proxy that always
    had it would turn that refusal into a failure inside the container.
    """
    plain = serve(lambda spec: FakeRunner(spec, str(tmp_path)))(SPEC)
    assert not hasattr(plain, "expose")

    real = FakeRunnerWithExpose(SPEC, str(tmp_path))
    rich = serve(lambda spec: real)(SPEC)
    rich.expose("/host/tree", "/aux/tree")
    assert real.exposed == [("/host/tree", "/aux/tree")]


def test_each_runner_gets_its_own_connection(serve, tmp_path):
    """Two runners must not interleave frames on one connection."""
    made = []
    proxy_factory = serve(lambda spec: made.append(
        FakeRunner(spec, str(tmp_path), lines=[f"r{len(made)}"])) or made[-1])

    first, second = proxy_factory(SPEC), proxy_factory(SPEC)
    a, b = [], []
    first.run(["one"], a.append)
    second.run(["two"], b.append)
    assert made[0].calls == [["one"]] and made[1].calls == [["two"]]
    assert a == ["r0"] and b == ["r1"]


def test_stop_closes_runners_the_worker_abandoned(tmp_path):
    """A worker that raised mid-composition leaves runners open; the parent lets go.

    Each holds a temp workspace and, on the cluster, a mirrored prefix in the object
    store -- and by then the parent is the only side still running.
    """
    made = []
    server = ContainerRunnerServer(
        lambda spec: made.append(FakeRunner(spec, str(tmp_path))) or made[-1],
        str(tmp_path / "aux.sock"))
    server.start()
    proxy_container_runner_factory(server.socket_path)(SPEC)   # never closed
    assert made[0].closed is False

    server.stop()
    assert made[0].closed is True
    # And the socket does not outlive the composition it belonged to.
    assert not os.path.exists(server.socket_path)


def test_close_is_idempotent(serve, tmp_path):
    made = []
    runner = serve(lambda spec: made.append(FakeRunner(spec, str(tmp_path)))
                   or made[-1])(SPEC)
    runner.close()
    runner.close()          # must not raise, and must not reach a popped runner
    assert made[0].closed is True


def test_server_survives_a_dropped_connection(serve, tmp_path):
    """One worker connection dying must not take the serving thread with it."""
    made = []
    proxy_factory = serve(lambda spec: made.append(FakeRunner(spec, str(tmp_path)))
                          or made[-1])

    doomed = proxy_factory(SPEC)
    doomed._conn.close()    # pylint: disable=protected-access - simulating a dead worker

    survivor = proxy_factory(SPEC)
    survivor.run(["still works"])
    assert made[1].calls == [["still works"]]


def test_serving_thread_is_not_left_running(tmp_path):
    before = threading.active_count()
    server = ContainerRunnerServer(lambda spec: FakeRunner(spec, str(tmp_path)),
                                   str(tmp_path / "aux.sock"))
    server.start()
    server.stop()
    assert threading.active_count() == before


# ------------------------------------------------------- the seam it was built for


def test_parent_serves_its_factory_to_the_worker(tmp_path, monkeypatch):
    """``_compose_isolated`` names a live socket in job.json when a factory is active.

    The regression this guards: a cluster campaign declaring ``plugins:`` composes in a
    subprocess, and the backend factory is a ContextVar that does not go with it. The
    worker therefore had no runner and no local docker, and the campaign refused to
    compose at all rather than running one trial.
    """
    import json

    from robovast.common import config_generation as cg

    made = []
    token = cg.set_container_runner_factory(
        lambda spec: made.append(FakeRunner(spec, str(tmp_path))) or made[-1])
    seen = {}

    class FakePopen:
        """Stands in for the worker: reads the job file and uses the served socket."""

        def __init__(self, cmd, **kwargs):
            del kwargs
            with open(cmd[-1], encoding="utf-8") as f:
                job = json.load(f)
            seen["socket"] = job["container_runner_socket"]
            # Exactly what compose_worker does with it.
            runner = proxy_container_runner_factory(job["container_runner_socket"])(SPEC)
            seen["workspace"] = runner.workspace
            runner.run(["generate"])
            runner.close()
            with open(job["result_path"], "w", encoding="utf-8") as f:
                json.dump({"configs": []}, f)
            self.stdout = iter(())
            self.returncode = 0

        def wait(self):
            return 0

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    monkeypatch.setattr(cg, "_result_from_transport", lambda transport, out: transport)
    try:
        cg._compose_isolated(str(tmp_path / "x.vast"), str(tmp_path), False, lambda _m: None)
    finally:
        cg._container_runner_factory.reset(token)

    assert seen["socket"], "the worker was given no socket to borrow the runner over"
    assert seen["workspace"] == str(tmp_path)
    assert made[0].calls == [["generate"]]      # the parent's runner really ran it
    assert made[0].closed is True
    # The socket belongs to the composition, and goes with the job dir.
    assert not os.path.exists(seen["socket"])


def test_no_factory_means_no_socket_and_the_local_fallback_stands(tmp_path, monkeypatch):
    """A CLI run has no backend factory; the worker must still find its own docker."""
    import json

    from robovast.common import config_generation as cg

    seen = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            del kwargs
            with open(cmd[-1], encoding="utf-8") as f:
                seen["socket"] = json.load(f)["container_runner_socket"]
            with open(json.load(open(cmd[-1], encoding="utf-8"))["result_path"],
                      "w", encoding="utf-8") as f:
                json.dump({"configs": []}, f)
            self.stdout = iter(())
            self.returncode = 0

        def wait(self):
            return 0

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    monkeypatch.setattr(cg, "_result_from_transport", lambda transport, out: transport)
    cg._compose_isolated(str(tmp_path / "x.vast"), str(tmp_path), False, lambda _m: None)

    assert seen["socket"] is None


def test_worker_installs_the_proxy_before_composing(tmp_path, monkeypatch):
    """compose_worker turns the socket into the factory ``_make_container_runner`` reads.

    Asserted through ``_make_container_runner`` rather than the ContextVar, because that
    is the function the variation actually goes through -- and the one that otherwise
    raises "not yet supported under isolated plugin composition on a cluster backend".
    """
    import json

    from robovast.common import compose_worker
    from robovast.common import config_generation as cg

    made = []
    server = ContainerRunnerServer(
        lambda spec: made.append(FakeRunner(spec, str(tmp_path))) or made[-1],
        str(tmp_path / "aux.sock"))
    server.start()

    job = tmp_path / "job.json"
    job.write_text(json.dumps({
        "container_runner_socket": server.socket_path,
        "variation_file": str(tmp_path / "x.vast"),
        "output_dir": str(tmp_path),
        "result_path": str(tmp_path / "result.json"),
    }))

    got = {}

    def fake_generate(**kwargs):
        del kwargs
        got["runner"] = cg._make_container_runner(SPEC, purpose="variation Floorplan")
        return {"configs": []}

    monkeypatch.setattr(cg, "generate_scenario_variations", fake_generate)
    monkeypatch.setattr(cg, "_result_to_transport", lambda r: r)
    # The worker installs the factory process-wide and never resets it, which is right
    # for a one-shot subprocess and wrong for a test process: left set, every later test
    # reaching _make_container_runner would get this dead proxy instead of its own
    # fallback.
    token = cg._container_runner_factory.set(None)
    try:
        assert compose_worker.main(["compose_worker", str(job)]) == 0
        # While the server is still up: stopping it is what ends a composition, and a
        # runner does not outlive that.
        got["runner"].run(["go"])
    finally:
        cg._container_runner_factory.reset(token)
        server.stop()

    assert made[0].calls == [["go"]]
