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
"""Cluster backend for auxiliary variation containers (sidecar + ``pods/exec``).

When a variation plugin declares a
:class:`~robovast.common.variation.container_runner.ContainerSpec`, the host
launcher adds that image as a **kept-alive sidecar** in the controller pod, with
a shared ``emptyDir`` mounted at :data:`AUX_WORKSPACE` in both the ``controller``
container and the sidecar. This module runs the plugin's commands *inside* that
sidecar via the Kubernetes ``pods/exec`` subresource — the in-cluster equivalent
of ``docker exec``.

Because the sidecar is already running (no per-call container create and no image
re-pull), each call pays only the exec stream setup (tens–low-hundreds of ms
in-cluster) plus the command's native runtime.
"""

import logging
import os
import socket
import subprocess
import time

logger = logging.getLogger(__name__)

# Shared emptyDir mount point, identical in the controller and every aux sidecar,
# so a plugin's absolute workspace paths resolve the same on both sides. Kept in
# sync with the volume injected by controller_launcher._aux_sidecar_containers.
AUX_WORKSPACE = "/aux"


def _pod_name() -> str:
    """The controller pod's own name (== hostname for a pod)."""
    return os.environ.get("HOSTNAME") or socket.gethostname()


class ClusterContainerRunner:
    """Runs commands in a controller-pod sidecar via the ``pods/exec`` API.

    The workspace for every aux container is a per-container subdirectory under
    the shared :data:`AUX_WORKSPACE` emptyDir, so multiple sidecars don't collide
    and the path is identical inside the sidecar and the controller.
    """

    def __init__(self, spec, namespace, core_v1=None):
        self._spec = spec
        self._namespace = namespace
        self._core_v1 = core_v1
        self._container = spec.container_name()
        self.workspace = os.path.join(AUX_WORKSPACE, self._container)
        os.makedirs(self.workspace, exist_ok=True)

    def _client(self):
        if self._core_v1 is None:
            from kubernetes import client, config  # pylint: disable=import-outside-toplevel
            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config()
            self._core_v1 = client.CoreV1Api()
        return self._core_v1

    def run(self, command, progress_update_callback=None):
        progress_update_callback = progress_update_callback or logger.debug
        full_cmd = list(self._spec.command_prefix) + list(command)
        # Retry the first exec a few times: the sidecar may still be starting when
        # the controller reaches the variation step.
        last_exc = None
        for attempt in range(10):
            try:
                return self._exec(full_cmd, progress_update_callback)
            except subprocess.CalledProcessError:
                raise  # a real non-zero exit from the command — don't retry
            except Exception as exc:  # pylint: disable=broad-except
                last_exc = exc
                logger.debug("exec into %s not ready yet (attempt %d): %s",
                             self._container, attempt + 1, exc)
                time.sleep(1)
        raise RuntimeError(
            f"Could not exec into aux sidecar '{self._container}': {last_exc}")

    def _exec(self, full_cmd, progress_update_callback):
        from kubernetes.stream import stream  # pylint: disable=import-outside-toplevel

        logger.debug("exec %s -c %s -- %s", _pod_name(), self._container, " ".join(full_cmd))
        resp = stream(
            self._client().connect_get_namespaced_pod_exec,
            _pod_name(), self._namespace,
            container=self._container,
            command=full_cmd,
            stderr=True, stdin=False, stdout=True, tty=False,
            _preload_content=False,
        )
        output_lines = []
        while resp.is_open():
            resp.update(timeout=1)
            if resp.peek_stdout():
                for line in resp.read_stdout().splitlines():
                    progress_update_callback(line)
                    output_lines.append(line)
            if resp.peek_stderr():
                for line in resp.read_stderr().splitlines():
                    progress_update_callback(line)
                    output_lines.append(line)
        returncode = resp.returncode
        resp.close()
        if returncode != 0:
            logger.error(
                "Container command failed (exit %s) in %s: %s\nOutput:\n%s",
                returncode, self._container, " ".join(full_cmd), "\n".join(output_lines))
            raise subprocess.CalledProcessError(returncode, full_cmd)

    def close(self):
        # The sidecar lives for the whole campaign and is reaped with the pod;
        # nothing to tear down here.
        pass


def make_cluster_container_runner_factory(namespace, core_v1=None):
    """Return a factory that builds :class:`ClusterContainerRunner` for a spec.

    Registered as the process-wide runner factory by the in-pod controller (see
    :func:`robovast.common.config_generation.set_container_runner_factory`).
    """
    def factory(spec):
        return ClusterContainerRunner(spec, namespace, core_v1)
    return factory
