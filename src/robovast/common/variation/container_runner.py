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
"""Generic auxiliary-container support for variation plugins.

Some variation plugins need to run an auxiliary Docker image while they produce
their variation (e.g. ``FloorplanVariation`` runs ``scenery_builder``). A plugin
declares this by overriding :meth:`Variation.get_required_container` to return a
:class:`ContainerSpec`; the active execution backend then provides a matching
:class:`ContainerRunner` on the variation instance (``self.container_runner``).

The plugin talks to the container through a single, backend-agnostic contract:

* ``runner.workspace`` — a working directory that is visible **at the same
  absolute path** to both the caller and the container. The plugin stages inputs
  there and reads outputs from there; no path translation is needed.
* ``runner.run(command, progress_update)`` — run ``command`` (logical args; the
  spec's :attr:`ContainerSpec.command_prefix` is prepended automatically) in the
  container, streaming output, raising :class:`subprocess.CalledProcessError` on
  a non-zero exit.

Two backends implement this:

* :class:`LocalContainerRunner` (here) — ephemeral ``docker run --rm`` per call.
* ``ClusterContainerRunner`` (in :mod:`robovast.execution.cluster_execution`) —
  ``kubectl exec``-equivalent into a long-lived container in the campaign's auxiliary
  pod. There is no sidecar sharing a filesystem with the driver, so the workspace is
  mirrored in and out around each ``run`` rather than shared live -- which is why the
  contract above says "at the same absolute path" and not "the same filesystem".
"""

import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Protocol, runtime_checkable

from robovast.common.errors import CampaignStopped
from robovast.common.stop import terminate_group, watch_stop

logger = logging.getLogger(__name__)


def run_with_live_output(cmd, progress_update_callback, *, should_stop=None,
                         terminate=None, stopped_reason=""):
    """Run *cmd*, streaming each output line to *progress_update_callback*.

    Raises :class:`subprocess.CalledProcessError` on a non-zero exit code (after
    logging the full captured output at ERROR level).

    With *should_stop*, the command is ended as soon as the work is no longer wanted and
    :class:`CampaignStopped` is raised instead -- a killed command exits non-zero like a
    broken one, and only the watch can tell the two apart. *terminate* is how this
    particular command is ended when its own process group is the wrong handle.
    """
    logger.debug("Executing: %s", ' '.join(cmd))
    output_lines = []
    with subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        # A session of its own only where something may end it: the backstop in
        # :func:`~robovast.common.stop.terminate_group` signals the group, and a child
        # sharing ours would take the caller with it. Where nothing can stop the command,
        # staying in the caller's group is what keeps an interactive Ctrl+C reaching it.
        start_new_session=should_stop is not None,
    ) as proc:
        with watch_stop(should_stop, proc, terminate=terminate) as watch:
            for line in proc.stdout:
                stripped = line.rstrip('\n')
                progress_update_callback(stripped)
                output_lines.append(stripped)
            proc.wait()
        if watch.stopped:
            raise CampaignStopped(stopped_reason or f"{cmd[0]} stopped by request")
        if proc.returncode != 0:
            logger.error(
                "Command failed (exit %d): %s\nOutput:\n%s",
                proc.returncode, ' '.join(cmd), '\n'.join(output_lines)
            )
            # The output is ATTACHED, not only logged. `CalledProcessError.__str__` is just
            # "returned non-zero exit status 1", so a caller that turns this into a message
            # for a user (the scene cache, into "no 3D geometry") had nothing to say about
            # the cause, and the reason a build failed lived only in the service log.
            raise subprocess.CalledProcessError(proc.returncode, cmd,
                                                output='\n'.join(output_lines))


def _end_container(name: str, proc) -> None:
    """End the named container and the ``docker run`` attached to it.

    The removal first, because that is what actually ends the work: a signalled client
    detaches and leaves the container running, and an entrypoint that ignores SIGTERM
    ignores a forwarded one. Removing the container makes the attached client exit, which
    is what releases the caller reading its output.

    Then the client's own group regardless, as the backstop: a removal that fails -- a
    daemon that will not answer, a name that resolves to nothing -- would otherwise leave
    the caller blocked on a stream that never closes, which is the exact stall a stop is
    supposed to end.

    Best-effort and never raised: a stop that cannot be delivered is no reason to fail a
    campaign that is ending anyway.
    """
    try:
        subprocess.run(["docker", "rm", "-f", name],  # nosec B603 B607 - fixed argv
                       check=False, capture_output=True)
    except OSError as e:
        logger.debug("could not remove auxiliary container %s: %s", name, e)
    terminate_group(proc)


@dataclass
class ContainerSpec:
    """Declares an auxiliary container a variation plugin needs while it runs.

    Attributes:
        image: Container image reference (e.g. ``ghcr.io/secorolab/scenery_builder``).
        command_prefix: The image's entrypoint, prepended to every ``run()``
            command. ``docker run`` applies the image ENTRYPOINT automatically,
            but ``kubectl exec`` into a kept-alive sidecar does not, so the runner
            prepends this on both backends to keep invocations identical. Empty
            means "the command is already a full argv / the binary is on PATH".
        keep_alive_command: Command the cluster backend runs to keep the sidecar
            alive for the campaign (the image's own one-shot entrypoint is
            overridden with this). Ignored by the local backend, which uses
            ephemeral ``docker run``.
        env: Environment variables to set in the container.
        run_as_user: ``uid[:gid]`` the container process runs as. Under ``docker run``
            this is the ``--user`` value (defaults to the current user so files
            written into the workspace are owned by the caller); in-cluster it is
            the sidecar's ``runAsUser``.
    """

    image: str
    command_prefix: List[str] = field(default_factory=list)
    keep_alive_command: List[str] = field(default_factory=lambda: ["sleep", "infinity"])
    env: dict = field(default_factory=dict)
    run_as_user: Optional[str] = None

    def container_name(self) -> str:
        """Deterministic sidecar/container name derived from the image.

        Both the host-side manifest injection and the in-pod runner compute this
        from the same spec, so they always agree on the exec target.

        A symbolic ``family:<member>`` ref is named after its **member**. Read as an
        ordinary reference the member is its *tag*, so dropping the tag would name every
        family ref ``aux-family``: two specs naming different members would deduplicate onto
        one container, and a refusal could not say which image it wanted.
        """
        # Imported here, not at module scope: this module is the plugin-facing half of the
        # contract and is imported by plugins, while `common.execution` pulls in the whole
        # campaign model.
        from robovast.common.execution import (  # pylint: disable=import-outside-toplevel
            FAMILY_IMAGE_PREFIX, is_family_image_ref)

        image = (self.image or "").strip()
        base = (image[len(FAMILY_IMAGE_PREFIX):] if is_family_image_ref(image)
                else image.rsplit("/", 1)[-1].split(":", 1)[0].split("@", 1)[0])
        safe = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-") or "aux"
        return f"aux-{safe}"


@runtime_checkable
class ContainerRunner(Protocol):
    """Backend-agnostic handle a variation uses to run commands in its container."""

    workspace: str

    def run(self, command: List[str], progress_update_callback=None) -> None:
        """Run *command* in the container; raise on non-zero exit."""

    def close(self) -> None:
        """Release any resources (temp dirs, etc.). Idempotent."""

    # Optional, and deliberately not part of the Protocol -- a runner that cannot place a
    # tree at a fixed absolute path simply does not define it, and
    # ``stage_for_container`` refuses rather than running without the mount:
    #
    #     def expose(self, host_path: str, container_path: str) -> None


# A factory maps a ContainerSpec to a concrete runner for the active backend.
ContainerRunnerFactory = Callable[[ContainerSpec], ContainerRunner]


class LocalContainerRunner:
    """Runs commands via ephemeral ``docker run --rm`` (local execution).

    Each :meth:`run` starts a fresh container with the shared workspace bind
    mounted at the same path inside the container, so the plugin's absolute
    workspace paths are valid on both sides. Cleanup is automatic via ``--rm``.
    """

    def __init__(self, spec: ContainerSpec, *, should_stop=None):
        self._spec = spec
        # Whoever arranged this runner for a span that can be stopped -- a campaign --
        # passes its flag as a predicate. A preview or a CLI run passes none and the
        # container runs to its own end.
        self._should_stop = should_stop
        self._tmp = tempfile.mkdtemp(prefix="robovast_aux_")
        # mkdtemp is 0700; make it traversable so a container running as a
        # different uid than us (spec.run_as_user) can reach staged files —
        # mirroring the cluster's shared /aux volume.
        try:
            os.chmod(self._tmp, 0o777)
        except OSError:
            pass
        self.workspace = self._tmp
        self._exposed: dict = {}

    def expose(self, host_path: str, container_path: str) -> None:
        """Also make *host_path* visible at the fixed *container_path*.

        For a staged tree whose own files name absolute paths into each other: it can only
        be read back where those paths resolve. Read-only, because the generator's output
        is what ``collect_from_container`` brings back, never this.
        """
        self._exposed[str(container_path)] = str(host_path)

    def run(self, command: List[str], progress_update_callback=None) -> None:
        progress_update_callback = progress_update_callback or logger.debug
        user = self._spec.run_as_user or f"{os.getuid()}:{os.getgid()}"
        full_cmd = list(self._spec.command_prefix) + list(command)

        # Named so a stop has something to end. Unique per call, because ``--rm`` means a
        # name is only taken for as long as the container runs and a campaign composing
        # several configurations reuses this runner.
        name = f"{self._spec.container_name()}-{uuid.uuid4().hex[:8]}"
        docker_cmd = [
            "docker", "run", "--rm", "--name", name,
            "--user", user,
            "--network", "host",
            "-v", f"{self.workspace}:{self.workspace}",
        ]
        for container_path, host_path in sorted(self._exposed.items()):
            docker_cmd += ["-v", f"{host_path}:{container_path}:ro"]
        for key, val in (self._spec.env or {}).items():
            docker_cmd += ["-e", f"{key}={val}"]
        # `docker run` applies the image ENTRYPOINT to the trailing args. We
        # already carry the entrypoint in command_prefix, so override the image
        # entrypoint to run our full argv verbatim (mirrors the exec backend).
        if full_cmd:
            docker_cmd += ["--entrypoint", full_cmd[0]]
        docker_cmd += [self._spec.image]
        docker_cmd += full_cmd[1:]

        run_with_live_output(
            docker_cmd, progress_update_callback, should_stop=self._should_stop,
            # The container, not the client's process group: a signalled ``docker run``
            # detaches and leaves the container running, and an entrypoint that ignores
            # SIGTERM ignores a forwarded one. Removing it is what actually ends the work.
            terminate=lambda proc: _end_container(name, proc),
            stopped_reason=(f"auxiliary container {self._spec.image} stopped by request "
                            f"while composing"))

    def close(self) -> None:
        if self._tmp and os.path.isdir(self._tmp):
            shutil.rmtree(self._tmp, ignore_errors=True)
        self._tmp = None
