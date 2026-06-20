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

"""Host-side client for the in-controller control channel.

The CLI ``monitor`` (and future tools) reach the controller pod's HTTP server
(see :mod:`robovast.execution.control_server`) through ``kubectl port-forward`` —
the same kubectl transport the launcher and archiver use, so it relies only on
the user's kubeconfig (no extra RBAC). Helpers here locate the controller pod,
open a port-forward, and call ``GET /status`` / ``POST /command``.
"""

import logging
import re
import subprocess
import time
from contextlib import contextmanager

import requests

from robovast.execution.control_server import DEFAULT_PORT

logger = logging.getLogger(__name__)

_CONTROLLER_SELECTOR = "app=robovast-controller"
_FORWARD_RE = re.compile(r"Forwarding from 127\.0\.0\.1:(\d+)")


def _ctx_args(kube_context):
    return ["--context", kube_context] if kube_context else []


def find_controller_pod(namespace="default", kube_context=None, campaign=None):
    """Return ``(pod_name, phase)`` of the controller pod, or ``(None, None)``.

    Prefers a Running pod (the live controller); otherwise returns the most recent
    terminal pod so the monitor can report a campaign that has already finished.
    With *campaign* given, restricts to that campaign's controller
    (``campaign-id=<label-safe>``).
    """
    from .cluster_execution import _label_safe_campaign  # pylint: disable=import-outside-toplevel

    selector = _CONTROLLER_SELECTOR
    if campaign is not None:
        selector += f",campaign-id={_label_safe_campaign(campaign)}"
    cmd = (["kubectl"] + _ctx_args(kube_context) +
           ["get", "pods", "-n", namespace, "-l", selector, "--sort-by=.metadata.creationTimestamp",
            "-o", "jsonpath={range .items[*]}{.metadata.name}{\" \"}{.status.phase}{\"\\n\"}{end}"])
    try:
        out = subprocess.run(cmd, check=False, capture_output=True, text=True)  # nosec - controlled args
    except FileNotFoundError:
        return None, None
    if out.returncode != 0:
        return None, None

    pods = []
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        if line:
            name, _, phase = line.partition(" ")
            pods.append((name, phase))
    if not pods:
        return None, None
    for name, phase in reversed(pods):       # newest first
        if phase == "Running":
            return name, phase
    return pods[-1]                          # newest terminal pod


@contextmanager
def port_forward(pod, namespace="default", kube_context=None, remote_port=DEFAULT_PORT,
                 timeout=15.0):
    """Open a ``kubectl port-forward`` to *pod* and yield the localhost base URL.

    kubectl picks a free local port (``:<remote>``); we parse it from kubectl's
    output. The forward is torn down on exit.
    """
    cmd = (["kubectl"] + _ctx_args(kube_context) +
           ["port-forward", "-n", namespace, f"pod/{pod}", f":{remote_port}"])
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,  # nosec - controlled args
                            text=True)
    try:
        local_port = None
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("kubectl port-forward exited before establishing a tunnel")
            line = proc.stdout.readline() if proc.stdout else ""
            if not line:
                time.sleep(0.05)
                continue
            match = _FORWARD_RE.search(line)
            if match:
                local_port = int(match.group(1))
                break
        if local_port is None:
            raise TimeoutError("timed out establishing kubectl port-forward")
        yield f"http://127.0.0.1:{local_port}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def get_status(base_url, timeout=5.0) -> dict:
    """``GET /status`` -> parsed JSON dict."""
    resp = requests.get(f"{base_url}/status", timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def send_command(base_url, name, timeout=10.0, **args) -> dict:
    """``POST /command`` with ``{name, args}`` -> parsed JSON ``CommandResult``."""
    resp = requests.post(f"{base_url}/command", json={"name": name, "args": args},
                         timeout=timeout)
    resp.raise_for_status()
    return resp.json()
