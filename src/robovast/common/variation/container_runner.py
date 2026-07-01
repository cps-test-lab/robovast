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
  ``kubectl exec``-equivalent into a long-lived sidecar in the controller pod.
"""

import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


def run_with_live_output(cmd, progress_update_callback):
    """Run *cmd*, streaming each output line to *progress_update_callback*.

    Raises :class:`subprocess.CalledProcessError` on a non-zero exit code (after
    logging the full captured output at ERROR level).
    """
    logger.debug("Executing: %s", ' '.join(cmd))
    output_lines = []
    with subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ) as proc:
        for line in proc.stdout:
            stripped = line.rstrip('\n')
            progress_update_callback(stripped)
            output_lines.append(stripped)
        proc.wait()
        if proc.returncode != 0:
            logger.error(
                "Command failed (exit %d): %s\nOutput:\n%s",
                proc.returncode, ' '.join(cmd), '\n'.join(output_lines)
            )
            raise subprocess.CalledProcessError(proc.returncode, cmd)


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
        run_as_user: ``uid[:gid]`` the container process runs as. Locally this is
            the ``docker run --user`` value (defaults to the current user so files
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
        """
        base = self.image.rsplit("/", 1)[-1].split(":", 1)[0].split("@", 1)[0]
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


# A factory maps a ContainerSpec to a concrete runner for the active backend.
ContainerRunnerFactory = Callable[[ContainerSpec], ContainerRunner]


class LocalContainerRunner:
    """Runs commands via ephemeral ``docker run --rm`` (local execution).

    Each :meth:`run` starts a fresh container with the shared workspace bind
    mounted at the same path inside the container, so the plugin's absolute
    workspace paths are valid on both sides. Cleanup is automatic via ``--rm``.
    """

    def __init__(self, spec: ContainerSpec):
        self._spec = spec
        self._tmp = tempfile.mkdtemp(prefix="robovast_aux_")
        # mkdtemp is 0700; make it traversable so a container running as a
        # different uid than us (spec.run_as_user) can reach staged files —
        # mirroring the cluster's shared /aux volume.
        try:
            os.chmod(self._tmp, 0o777)
        except OSError:
            pass
        self.workspace = self._tmp

    def run(self, command: List[str], progress_update_callback=None) -> None:
        progress_update_callback = progress_update_callback or logger.debug
        user = self._spec.run_as_user or f"{os.getuid()}:{os.getgid()}"
        full_cmd = list(self._spec.command_prefix) + list(command)

        docker_cmd = [
            "docker", "run", "--rm",
            "--user", user,
            "--network", "host",
            "-v", f"{self.workspace}:{self.workspace}",
        ]
        for key, val in (self._spec.env or {}).items():
            docker_cmd += ["-e", f"{key}={val}"]
        # `docker run` applies the image ENTRYPOINT to the trailing args. We
        # already carry the entrypoint in command_prefix, so override the image
        # entrypoint to run our full argv verbatim (mirrors the exec backend).
        if full_cmd:
            docker_cmd += ["--entrypoint", full_cmd[0]]
        docker_cmd += [self._spec.image]
        docker_cmd += full_cmd[1:]

        run_with_live_output(docker_cmd, progress_update_callback)

    def close(self) -> None:
        if self._tmp and os.path.isdir(self._tmp):
            shutil.rmtree(self._tmp, ignore_errors=True)
        self._tmp = None
