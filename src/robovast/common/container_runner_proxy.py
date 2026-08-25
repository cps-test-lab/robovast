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

"""Lend the isolated compose worker the parent's auxiliary-container runner.

A ``.vast`` that declares ``plugins:`` composes in a subprocess
(``config_generation._compose_isolated``), so the plugin's pinned dependencies are
imported there and never in the long-lived service process. A variation that declares
:meth:`Variation.get_required_container` needs a
:class:`~robovast.common.variation.container_runner.ContainerRunner`, and the active
factory is a **ContextVar** — it does not cross a process boundary. The two features
were therefore mutually exclusive on a cluster backend: the worker had no factory and
no local ``docker`` to fall back on, so a campaign combining external variation plugins
with floorplan generation could not compose at all.

**The runner stays in the parent and is called across the boundary.** The parent serves
its own live factory on a Unix socket beside the job file; the worker installs a factory
whose runners forward each call. Nothing about a backend is serialized — not a Kubernetes
client, not a storage client, and above all not the credentials either would need. The
parent keeps owning the aux Pod's lifetime, which it already did.

Serializing a per-backend "runner descriptor" instead was the alternative, and it is worse
in the way that matters: every backend would have to describe itself, the worker would have
to rebuild live clients from that description, and the description would have to carry the
secrets those clients authenticate with — into a file, for a subprocess. Forwarding four
method calls needs none of that and is written once for every backend, including the local
``docker`` one and any added later.

**What crosses, and what does not.** Only the contract in
:class:`~robovast.common.variation.container_runner.ContainerRunner`: ``workspace`` (a
path), ``run``, ``close``, and ``expose`` where the real runner has it. Parent and worker
share a filesystem — the worker is a child process on the same host — so a path is a
valid answer to both, which is what lets the proxy carry no data. ``run``'s output is
streamed frame by frame rather than returned at the end, so a plugin's progress reaches
the campaign log while the command is still running, exactly as it does in-process.

Failures cross faithfully: a non-zero command raises
:class:`subprocess.CalledProcessError` in the worker with its ``returncode``, ``cmd`` and
``output`` intact, because callers read ``output`` to say *why* something failed rather
than only that it did.
"""

import contextlib
import json
import logging
import os
import socket
import subprocess  # nosec B404 - for CalledProcessError, not for spawning
import threading
from dataclasses import asdict

logger = logging.getLogger(__name__)

#: Name of the socket inside the compose job's temp dir. The directory is the one
#: ``_compose_isolated`` already creates with ``mkdtemp`` (mode 0700), so the socket
#: is reachable only by the user running the service, and is removed with it.
SOCKET_NAME = "container_runner.sock"

#: Guards against a wedged peer holding a compose thread forever. Generous, because
#: the calls behind it are container image pulls and multi-minute mesh generation --
#: this is a deadlock backstop, not a per-operation budget.
_IO_TIMEOUT_S = 3600.0


def _send(conn, obj) -> None:
    conn.sendall((json.dumps(obj) + "\n").encode("utf-8"))


class _FrameReader:
    """Newline-delimited JSON frames off a stream socket."""

    def __init__(self, conn):
        self._conn = conn
        self._buf = b""

    def read(self):
        """The next frame, or ``None`` at end of stream."""
        while b"\n" not in self._buf:
            chunk = self._conn.recv(65536)
            if not chunk:
                return None
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return json.loads(line.decode("utf-8"))


# --------------------------------------------------------------------------- parent


class ContainerRunnerServer:
    """Serves *factory*'s runners on a Unix socket, for one compose worker.

    **One thread per connection, because one connection per runner.** The worker opens a
    connection when it makes a runner and keeps it until it closes it, so a server that
    handled connections one at a time would sit reading the first runner's socket while
    the second runner's ``make`` waited behind it -- a deadlock the moment a composition
    holds two runners at once, which a sweep with two aux-container variations does.
    Handling them concurrently also keeps the ordering assumption out of the design: the
    worker composes sequentially today, and nothing here depends on that staying true.

    Nothing else about this is a general-purpose RPC server, and it should not become
    one -- its whole lifetime is one subprocess's.
    """

    def __init__(self, factory, socket_path: str):
        self._factory = factory
        self.socket_path = socket_path
        self._sock = None
        self._acceptor = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._handlers: list = []
        self._conns: set = set()
        self._runners: dict = {}
        self._next_id = 0

    def start(self) -> None:
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.socket_path)
        self._sock.listen(8)
        # So stop() is not waiting on accept() for the full timeout after the worker
        # has already gone.
        self._sock.settimeout(0.5)
        self._acceptor = threading.Thread(
            target=self._serve, name="aux-runner-proxy", daemon=True)
        self._acceptor.start()

    def stop(self) -> None:
        """Stop serving and release any runner the worker did not close itself.

        A worker that raised mid-composition leaves its runners open; each holds a temp
        workspace and, on the cluster, a mirrored prefix in the object store. The parent
        is the only side still running by then, so it is the side that must let go.

        Live connections are shut down rather than waited on: a handler blocked reading
        from a worker that has died would otherwise hold this for the full I/O timeout,
        and that timeout is sized for an image pull.
        """
        self._stop.set()
        if self._sock is not None:
            self._sock.close()          # breaks accept()
            self._sock = None
        if self._acceptor is not None:
            self._acceptor.join(timeout=5)
            self._acceptor = None
        with self._lock:
            conns, handlers = list(self._conns), list(self._handlers)
        for conn in conns:
            with contextlib.suppress(OSError):
                conn.shutdown(socket.SHUT_RDWR)   # breaks a blocked recv()
        for handler in handlers:
            handler.join(timeout=5)
        with self._lock:
            runners, self._runners = list(self._runners.values()), {}
            self._handlers, self._conns = [], set()
        for runner in runners:
            try:
                runner.close()
            except Exception:  # pylint: disable=broad-except - cleanup must not mask the real error
                logger.debug("Aux runner close failed during proxy shutdown", exc_info=True)
        with contextlib.suppress(OSError):
            os.unlink(self.socket_path)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            conn.settimeout(_IO_TIMEOUT_S)
            handler = threading.Thread(
                target=self._handle, args=(conn,), name="aux-runner-proxy-conn",
                daemon=True)
            with self._lock:
                self._conns.add(conn)
                self._handlers.append(handler)
            handler.start()

    def _handle(self, conn) -> None:
        try:
            self._converse(conn)
        except Exception:  # pylint: disable=broad-except
            # The worker sees a closed socket and raises there; this thread must not
            # die silently and take the acceptor or other connections with it.
            logger.warning("Aux runner proxy connection failed", exc_info=True)
        finally:
            with self._lock:
                self._conns.discard(conn)
            with contextlib.suppress(OSError):
                conn.close()

    def _converse(self, conn) -> None:
        reader = _FrameReader(conn)
        while True:
            request = reader.read()
            if request is None:
                return
            _send(conn, self._dispatch(conn, request))

    def _dispatch(self, conn, request) -> dict:
        op = request.get("op")
        try:
            if op == "make":
                return self._op_make(request)
            if op == "run":
                return self._op_run(conn, request)
            if op == "expose":
                self._runner(request).expose(request["host_path"],
                                             request["container_path"])
                return {"ok": True}
            if op == "close":
                return self._op_close(request)
            return {"ok": False, "error": {"kind": "error", "type": "ValueError",
                                           "message": f"unknown op {op!r}"}}
        except Exception as exc:  # pylint: disable=broad-except - forwarded, not swallowed
            return {"ok": False, "error": _error_frame(exc)}

    def _runner(self, request):
        with self._lock:
            return self._runners[request["id"]]

    def _op_make(self, request) -> dict:
        from robovast.common.variation.container_runner import \
            ContainerSpec  # pylint: disable=import-outside-toplevel
        runner = self._factory(ContainerSpec(**request["spec"]))
        with self._lock:
            self._next_id += 1
            runner_id = self._next_id
            self._runners[runner_id] = runner
        # ``expose`` is deliberately not part of the Protocol: a runner that cannot place
        # a tree at a fixed absolute path simply does not define it, and the caller then
        # refuses rather than running without the mount. Report which kind this is so the
        # proxy can be the same kind -- a proxy that always had ``expose`` would turn that
        # refusal into a failure inside the container.
        return {"ok": True, "id": runner_id, "workspace": runner.workspace,
                "expose": hasattr(runner, "expose")}

    def _op_run(self, conn, request) -> dict:
        runner = self._runner(request)
        try:
            runner.run(request["command"], lambda line: _send(conn, {"log": line}))
        except Exception as exc:  # pylint: disable=broad-except - forwarded, not swallowed
            return {"ok": False, "error": _error_frame(exc)}
        return {"ok": True}

    def _op_close(self, request) -> dict:
        with self._lock:
            runner = self._runners.pop(request["id"], None)
        if runner is not None:
            runner.close()
        return {"ok": True}


def _error_frame(exc) -> dict:
    """*exc* as a frame the worker can raise faithfully.

    ``CalledProcessError`` is carried field by field rather than as a message: callers
    read ``output`` to report *why* a container command failed, and a message-only
    round trip would silently drop it.
    """
    if isinstance(exc, subprocess.CalledProcessError):
        return {"kind": "called_process_error", "returncode": exc.returncode,
                "cmd": exc.cmd, "output": exc.output}
    return {"kind": "error", "type": type(exc).__name__, "message": str(exc)}


# --------------------------------------------------------------------------- worker


def _raise(error) -> None:
    if (error or {}).get("kind") == "called_process_error":
        raise subprocess.CalledProcessError(
            error["returncode"], error["cmd"], output=error.get("output"))
    raise RuntimeError(
        f"auxiliary container runner failed in the parent process: "
        f"{(error or {}).get('type', 'error')}: {(error or {}).get('message', '')}")


class _ProxyRunner:
    """A :class:`ContainerRunner` whose work happens in the parent process."""

    def __init__(self, conn, reader, runner_id: int, workspace: str):
        self._conn = conn
        self._reader = reader
        self._id = runner_id
        self.workspace = workspace
        self._closed = False

    def _call(self, request, progress_update_callback=None) -> dict:
        _send(self._conn, {**request, "id": self._id})
        while True:
            frame = self._reader.read()
            if frame is None:
                raise RuntimeError(
                    "the parent process closed the auxiliary-container connection "
                    "while a variation was using it")
            if "log" in frame:
                if progress_update_callback:
                    progress_update_callback(frame["log"])
                continue
            if not frame.get("ok"):
                _raise(frame.get("error"))
            return frame

    def run(self, command, progress_update_callback=None) -> None:
        self._call({"op": "run", "command": list(command)}, progress_update_callback)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._call({"op": "close"})


class _ProxyRunnerWithExpose(_ProxyRunner):
    """The same, for a parent runner that offers ``expose``."""

    def expose(self, host_path: str, container_path: str) -> None:
        self._call({"op": "expose", "host_path": str(host_path),
                    "container_path": str(container_path)})


def proxy_container_runner_factory(socket_path: str):
    """A ``factory(spec) -> ContainerRunner`` backed by the parent's factory.

    One connection is opened per runner, so a runner's frames cannot interleave with
    another's -- and closing a runner closes its connection, leaving nothing behind in
    the worker for the parent to reap.
    """
    def factory(spec):
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(_IO_TIMEOUT_S)
        conn.connect(socket_path)
        reader = _FrameReader(conn)
        _send(conn, {"op": "make", "spec": asdict(spec)})
        frame = reader.read()
        if frame is None:
            conn.close()
            raise RuntimeError(
                "the parent process closed the auxiliary-container connection before "
                "it provided a runner")
        if not frame.get("ok"):
            conn.close()
            _raise(frame.get("error"))
        cls = _ProxyRunnerWithExpose if frame.get("expose") else _ProxyRunner
        return cls(conn, reader, frame["id"], frame["workspace"])
    return factory
