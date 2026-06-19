#!/usr/bin/env python3
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
"""Host-side launcher for the in-cluster campaign controller (search on cluster).

For a search campaign, ``vast exec cluster run`` does not create scenario Jobs
directly. Instead it launches a short-lived **controller pod** that runs the
``CampaignController`` in-cluster (so the per-batch S3 traffic stays in-cluster),
and streams its progress to the terminal:

1. build a wheel of the current dev source (``poetry build``),
2. create the controller pod (the ``robovast-controller`` image, idle command,
   bound to the controller ServiceAccount created at ``cluster setup``),
3. ``kubectl cp`` the wheel + the campaign inputs into the pod,
4. ``kubectl exec`` ``pip install --no-deps <wheel>`` then
   ``python -m robovast.execution.controller`` (KubernetesBackend), streaming logs,
5. copy the campaign (store + results) back to the local results dir,
6. delete the controller pod.

The pod is per-run and not long-lived. ``kubectl`` is used for cp/exec (the same
transport :mod:`.archiver` uses); the controller talks to storage and the K8s API
from inside the cluster via its ServiceAccount.
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime

import yaml

from robovast.common.execution import resolve_controller_image

logger = logging.getLogger(__name__)

# ServiceAccount granting the controller pod permission to create/monitor jobs.
# Created by setup_cluster() (see cluster_setup.apply_controller_rbac).
CONTROLLER_SERVICE_ACCOUNT = "robovast-controller"

_POD_WORKSPACE = "/workspace"
_POD_CAMPAIGN_DIR = f"{_POD_WORKSPACE}/campaign"
_POD_RESULTS_DIR = f"{_POD_WORKSPACE}/results"
# In-pod directory the dev wheel is copied into. The wheel keeps its original
# filename (pip requires the canonical ``name-version-...whl`` form), so we
# install ``<dir>/*.whl`` rather than a fixed path.
_POD_WHEEL_DIR = "/tmp/robovast_wheel"  # nosec - in-pod path, not host temp


def _kubectl(ctx_args, *args, check=True, stream=False, input_text=None):
    cmd = ["kubectl"] + ctx_args + list(args)
    logger.debug("kubectl %s", " ".join(args))
    if stream:
        return subprocess.run(cmd, check=check)  # nosec - args are controlled
    return subprocess.run(cmd, check=check, capture_output=True, text=True,
                          input=input_text)  # nosec - args are controlled


def cleanup_controller_pods(namespace="default", kube_context=None):
    """Delete all controller pods in *namespace* (label ``app=robovast-controller``).

    Best-effort: used to reap stragglers at launch and by the cluster cleanup
    command. Safe for the per-run, single-user controller model.
    """
    ctx_args = ["--context", kube_context] if kube_context else []
    _kubectl(ctx_args, "delete", "pod", "-n", namespace,
             "-l", "app=robovast-controller",
             "--ignore-not-found", "--grace-period=0", check=False)


def reap_orphaned_runs(namespace="default", kube_context=None):
    """Reap leftovers from a previous controller run that didn't clean up.

    Deletes any stale controller pods and — only when such a pod is found (i.e.
    a prior run died) — also clears the scenario jobs/pods/Kueue workloads it may
    have left behind, via :func:`cleanup_cluster_campaign`. Skipping the job sweep
    on a clean start avoids disturbing an unrelated in-flight batch run.
    """
    ctx_args = ["--context", kube_context] if kube_context else []
    listed = _kubectl(ctx_args, "get", "pods", "-n", namespace,
                      "-l", "app=robovast-controller", "-o", "name", check=False)
    stale = bool(listed.returncode == 0 and (listed.stdout or "").strip())

    cleanup_controller_pods(namespace=namespace, kube_context=kube_context)

    if stale:
        logger.info("Found stale controller pod(s); reaping orphaned scenario jobs...")
        from .cluster_execution import \
            cleanup_cluster_campaign  # pylint: disable=import-outside-toplevel
        try:
            cleanup_cluster_campaign(namespace=namespace, campaign=None, context=kube_context)
        except Exception as exc:  # pragma: no cover - best-effort
            logger.warning("Failed to reap orphaned scenario jobs: %s", exc)


def build_dev_wheel():
    """Build a wheel of the current robovast source. Returns its path, or None.

    The wheel carries the *current* dev code, which is ``pip install --no-deps``'d
    over the baseline robovast baked into the controller image. Returns ``None``
    when no source tree / poetry is available (then the image's baseline is used).
    """
    import robovast  # pylint: disable=import-outside-toplevel
    repo_root = os.path.abspath(os.path.join(os.path.dirname(robovast.__file__), "..", ".."))
    if not os.path.isfile(os.path.join(repo_root, "pyproject.toml")):
        logger.warning("No pyproject.toml at %s; using the controller image's baseline robovast.",
                       repo_root)
        return None
    dist_dir = tempfile.mkdtemp(prefix="robovast_wheel_")
    try:
        subprocess.run(  # nosec - poetry on a known repo root
            ["poetry", "build", "-f", "wheel", "-o", dist_dir],
            cwd=repo_root, check=True, capture_output=True, text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        logger.warning("Could not build dev wheel (%s); using the image's baseline robovast.", exc)
        return None
    wheels = [f for f in os.listdir(dist_dir) if f.endswith(".whl")]
    if not wheels:
        return None
    return os.path.join(dist_dir, wheels[0])


def _controller_pod_manifest(pod_name, namespace, image, control_node_labels=None):
    spec = {
        "restartPolicy": "Never",
        "serviceAccountName": CONTROLLER_SERVICE_ACCOUNT,
        "containers": [{
            "name": "controller",
            "image": image,
            # Always re-pull: dev images commonly reuse a mutable tag (e.g. :dev),
            # for which the node's default IfNotPresent policy would serve a stale
            # cached layer.
            "imagePullPolicy": "Always",
            # Idle so the host can copy in the dev wheel + inputs and exec the controller.
            "command": ["sleep", "infinity"],
            "volumeMounts": [{"name": "workspace", "mountPath": _POD_WORKSPACE}],
        }],
        "volumes": [{"name": "workspace", "emptyDir": {}}],
    }
    if control_node_labels:
        spec["nodeSelector"] = dict(control_node_labels)
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name,
            "namespace": namespace,
            "labels": {"app": "robovast-controller"},
        },
        "spec": spec,
    }


def launch_search_controller(*, config_path, config_name, setup_kwargs, namespace,
                             runs, kube_context, log_tree=False,
                             control_node_labels=None):
    """Launch the controller pod and run the search campaign in it.

    Results (the canonical campaign, including campaign.db + _execution) are
    published to the storage bucket by the in-pod controller; retrieve them with
    the usual ``upload-to-share`` + ``download`` flow.

    Args:
        config_path: Path to the local ``.vast`` file.
        config_name: Cluster-config plugin name (e.g. ``rke2``) — injected so the
            pod reconstructs the same cluster config.
        setup_kwargs: Persisted cluster-config setup kwargs (injected as JSON).
        namespace: Kubernetes namespace.
        runs: Optional runs override.
        kube_context: Host kube context (also forwarded for per-cluster resource
            resolution inside the pod).
        log_tree: Forward the live scenario tree.
        control_node_labels: Optional nodeSelector for the controller pod.
    """
    ctx_args = ["--context", kube_context] if kube_context else []
    image = resolve_controller_image()
    pod_name = f"robovast-controller-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    config_dir = os.path.dirname(os.path.abspath(config_path))
    vast_in_pod = f"{_POD_CAMPAIGN_DIR}/{os.path.basename(config_path)}"

    click_echo = logger.info
    wheel = build_dev_wheel()

    # Reap leftovers from a previous run that could not clean up (e.g. a
    # hard-killed CLI, where the finally below never ran): stale controller pods
    # and any scenario jobs/workloads they orphaned. Controller pods are per-run
    # and single-user, so any existing one is a leftover.
    reap_orphaned_runs(namespace=namespace, kube_context=kube_context)

    manifest = _controller_pod_manifest(pod_name, namespace, image, control_node_labels)
    click_echo(f"Creating controller pod '{pod_name}' (image {image})...")
    _kubectl(ctx_args, "apply", "-f", "-", input_text=yaml.safe_dump(manifest))
    try:
        _kubectl(ctx_args, "wait", "--for=condition=Ready", f"pod/{pod_name}",
                 "-n", namespace, "--timeout=300s")

        pod_wheel = None
        if wheel:
            click_echo("Copying dev wheel into the controller pod...")
            pod_wheel = f"{_POD_WHEEL_DIR}/{os.path.basename(wheel)}"
            _kubectl(ctx_args, "exec", pod_name, "-n", namespace, "-c", "controller",
                     "--", "mkdir", "-p", _POD_WHEEL_DIR)
            _kubectl(ctx_args, "cp", wheel, f"{namespace}/{pod_name}:{pod_wheel}",
                     "-c", "controller")
        click_echo("Copying campaign inputs into the controller pod...")
        _kubectl(ctx_args, "cp", config_dir, f"{namespace}/{pod_name}:{_POD_CAMPAIGN_DIR}",
                 "-c", "controller")

        # Build the in-pod command: install the dev wheel over the baseline, then
        # run the controller with the KubernetesBackend.
        env_exports = [
            f"export ROBOVAST_CLUSTER_CONFIG_NAME={_sh_quote(config_name)}",
            f"export ROBOVAST_CLUSTER_CONFIG_KWARGS={_sh_quote(json.dumps(setup_kwargs or {}))}",
            f"export ROBOVAST_NAMESPACE={_sh_quote(namespace)}",
        ]
        if kube_context:
            env_exports.append(f"export ROBOVAST_KUBE_CONTEXT={_sh_quote(kube_context)}")

        controller_cmd = [
            "python", "-m", "robovast.execution.controller",
            "--vast", vast_in_pod,
            "--results-dir", _POD_RESULTS_DIR,
            "--namespace", namespace,
        ]
        if runs is not None:
            controller_cmd += ["--runs", str(runs)]
        if kube_context:
            controller_cmd += ["--kube-context", kube_context]
        if log_tree:
            controller_cmd += ["--log-tree"]

        install = (f"pip install --no-deps --force-reinstall --quiet {pod_wheel} && "
                   if pod_wheel else "")
        script = " && ".join(env_exports) + " && " + install + " ".join(
            _sh_quote(c) for c in controller_cmd)

        click_echo("Starting the campaign controller in-cluster (streaming logs)...")
        result = _kubectl(ctx_args, "exec", "-i", pod_name, "-n", namespace,
                          "-c", "controller", "--", "bash", "-lc", script,
                          stream=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"Controller exited with code {result.returncode}")

        # Results live in the canonical campaign bucket (the controller published
        # campaign.db + _execution there). Retrieve them the same way as batch runs.
        click_echo("Campaign finished. Retrieve results with: "
                   "'vast exec cluster upload-to-share' then 'vast results download'.")
    finally:
        _kubectl(ctx_args, "delete", "pod", pod_name, "-n", namespace,
                 "--ignore-not-found", "--grace-period=0", check=False)
        if wheel:
            shutil.rmtree(os.path.dirname(wheel), ignore_errors=True)


def _sh_quote(value: str) -> str:
    import shlex
    return shlex.quote(str(value))
