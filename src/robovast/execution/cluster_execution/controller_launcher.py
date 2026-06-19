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
"""Host-side launcher for the in-cluster campaign controller.

Every cluster run — batch **and** search — is driven by a **controller pod** that
runs the :class:`~robovast.execution.controller.CampaignController` in-cluster (so
all per-batch storage traffic stays in-cluster). The launch is **fire-and-forget**:

1. build a wheel of the current dev source (``poetry build``),
2. create the controller pod (the ``robovast-controller`` image, bound to the
   controller ServiceAccount created at ``cluster setup``), whose entrypoint waits
   for a start sentinel so the host can stage inputs first,
3. ``kubectl cp`` the wheel + the campaign inputs + the in-pod run script,
4. ``touch`` the sentinel — the controller becomes the pod's main process and runs
   to completion (so the pod's phase reflects completion), then
5. **return immediately**. The controller publishes the canonical campaign
   (``campaign.db`` + ``_execution`` + results) to the storage bucket; retrieve it
   with ``vast exec cluster upload-to-share`` + ``vast results download`` and watch
   progress with ``vast exec cluster monitor``.

The completed pod is left in place (``kubectl logs`` works post-mortem) and reaped
on the next run or by ``run-cleanup`` / ``cleanup``. ``kubectl`` is used for cp/exec
(the same transport :mod:`.archiver` uses); the controller talks to storage and the
K8s API from inside the cluster via its ServiceAccount.
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

from .cluster_execution import _label_safe_campaign

logger = logging.getLogger(__name__)

# ServiceAccount granting the controller pod permission to create/monitor jobs.
# Created by setup_cluster() (see cluster_setup.apply_controller_rbac).
CONTROLLER_SERVICE_ACCOUNT = "robovast-controller"

_POD_WORKSPACE = "/workspace"
_POD_CAMPAIGN_DIR = f"{_POD_WORKSPACE}/campaign"
_POD_RESULTS_DIR = f"{_POD_WORKSPACE}/results"
_POD_RUN_SCRIPT = f"{_POD_WORKSPACE}/run.sh"
_POD_START_SENTINEL = f"{_POD_WORKSPACE}/.start"
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


def cleanup_controller_pods(namespace="default", kube_context=None, campaign=None):
    """Delete controller pods (label ``app=robovast-controller``).

    With *campaign* given, deletes only that campaign's controller pod
    (``campaign-id=<label-safe>``) so concurrent runs are left untouched;
    otherwise deletes every controller pod. Best-effort.
    """
    ctx_args = ["--context", kube_context] if kube_context else []
    selector = "app=robovast-controller"
    if campaign is not None:
        selector += f",campaign-id={_label_safe_campaign(campaign)}"
    _kubectl(ctx_args, "delete", "pod", "-n", namespace, "-l", selector,
             "--ignore-not-found", "--grace-period=0", check=False)


def reap_orphaned_runs(namespace="default", kube_context=None):
    """Delete **completed/failed** controller pods left from previous runs.

    Runs are fire-and-forget, so finished controller pods persist (for
    ``kubectl logs``) until the next launch reaps them here. Pods that are still
    Running are a concurrent campaign and are deliberately left alone. Scenario
    jobs orphaned by a crashed controller are cleaned via ``run-cleanup`` rather
    than swept here, so a concurrent run's jobs are never disturbed.
    """
    ctx_args = ["--context", kube_context] if kube_context else []
    listed = _kubectl(
        ctx_args, "get", "pods", "-n", namespace, "-l", "app=robovast-controller",
        "-o", "jsonpath={range .items[*]}{.metadata.name}{\" \"}{.status.phase}{\"\\n\"}{end}",
        check=False)
    if listed.returncode != 0:
        return

    running = 0
    for line in (listed.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        name, _, phase = line.partition(" ")
        if phase in ("Succeeded", "Failed"):
            _kubectl(ctx_args, "delete", "pod", name, "-n", namespace,
                     "--ignore-not-found", "--grace-period=0", check=False)
        else:
            running += 1
    if running:
        logger.info("Leaving %d running controller pod(s) untouched.", running)


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


def _controller_pod_manifest(pod_name, namespace, image, campaign_label,
                             control_node_labels=None):
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
            # Wait for the host to stage inputs + the run script, then exec the
            # controller as the pod's main process so the pod phase reflects
            # completion (and the host can detach).
            "command": ["bash", "-lc",
                        f"until [ -f {_POD_START_SENTINEL} ]; do sleep 1; done; "
                        f"exec bash -l {_POD_RUN_SCRIPT}"],
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
            "labels": {
                "app": "robovast-controller",
                "campaign-id": campaign_label,
            },
        },
        "spec": spec,
    }


def launch_controller(*, config_path, config_name, setup_kwargs, namespace,
                      runs, kube_context, config_filter=None, log_tree=False,
                      control_node_labels=None):
    """Launch the controller pod for a campaign (batch or search) and detach.

    The in-pod controller drives the whole campaign and publishes the canonical
    campaign (``campaign.db`` + ``_execution`` + results) to the storage bucket;
    retrieve it with the usual ``upload-to-share`` + ``download`` flow and watch
    progress with ``vast exec cluster monitor``.

    Args:
        config_path: Path to the local ``.vast`` file.
        config_name: Cluster-config plugin name (e.g. ``rke2``) — injected so the
            pod reconstructs the same cluster config.
        setup_kwargs: Persisted cluster-config setup kwargs (injected as JSON).
        namespace: Kubernetes namespace.
        runs: Optional runs override.
        kube_context: Host kube context (also forwarded for per-cluster resource
            resolution inside the pod).
        config_filter: Optional glob; run only matching configurations (batch only).
        log_tree: Forward the live scenario tree to the job logs.
        control_node_labels: Optional nodeSelector for the controller pod.

    Returns:
        The campaign id (host-generated) the controller runs under.
    """
    from robovast.common.common import load_config  # pylint: disable=import-outside-toplevel
    from robovast.common.config import validate_config  # pylint: disable=import-outside-toplevel
    from robovast.execution.controller import campaign_id_for  # pylint: disable=import-outside-toplevel

    ctx_args = ["--context", kube_context] if kube_context else []
    image = resolve_controller_image()
    # Generate the campaign id on the host so we can label the pod and tell the
    # user what to monitor/retrieve; the controller is told to use the same id.
    campaign_config = validate_config(load_config(config_path))
    campaign_id = campaign_id_for(campaign_config)
    campaign_label = _label_safe_campaign(campaign_id)
    pod_name = f"robovast-controller-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    config_dir = os.path.dirname(os.path.abspath(config_path))
    vast_in_pod = f"{_POD_CAMPAIGN_DIR}/{os.path.basename(config_path)}"

    click_echo = logger.info
    wheel = build_dev_wheel()

    # Reap finished controller pods from previous runs (running ones are left
    # alone — they belong to a concurrent campaign).
    reap_orphaned_runs(namespace=namespace, kube_context=kube_context)

    manifest = _controller_pod_manifest(pod_name, namespace, image, campaign_label,
                                        control_node_labels)
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

        # Build the in-pod run script: install the dev wheel over the baseline,
        # then run the controller with the KubernetesBackend under the
        # host-generated campaign id.
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
            "--campaign-id", campaign_id,
        ]
        if runs is not None:
            controller_cmd += ["--runs", str(runs)]
        if config_filter:
            controller_cmd += ["--config", config_filter]
        if kube_context:
            controller_cmd += ["--kube-context", kube_context]
        if log_tree:
            controller_cmd += ["--log-tree"]

        install = (f"pip install --no-deps --force-reinstall --quiet "
                   f"--root-user-action=ignore --disable-pip-version-check {pod_wheel} && "
                   if pod_wheel else "")
        script = " && ".join(env_exports) + " && " + install + " ".join(
            _sh_quote(c) for c in controller_cmd) + "\n"

        # Stage the run script, then drop the start sentinel — the pod entrypoint
        # is waiting on it and will exec the controller as its main process.
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
            fh.write(script)
            host_script = fh.name
        try:
            _kubectl(ctx_args, "cp", host_script,
                     f"{namespace}/{pod_name}:{_POD_RUN_SCRIPT}", "-c", "controller")
        finally:
            os.unlink(host_script)
        _kubectl(ctx_args, "exec", pod_name, "-n", namespace, "-c", "controller",
                 "--", "touch", _POD_START_SENTINEL)
    except Exception:
        # Setup failed before the controller started — don't leave a pod hanging
        # on the sentinel forever.
        _kubectl(ctx_args, "delete", "pod", pod_name, "-n", namespace,
                 "--ignore-not-found", "--grace-period=0", check=False)
        raise
    finally:
        if wheel:
            shutil.rmtree(os.path.dirname(wheel), ignore_errors=True)

    click_echo("")
    click_echo(f"✓ Controller started in-cluster (campaign id: {campaign_id}).")
    click_echo(f"  Controller pod: {pod_name}")
    click_echo("")
    click_echo("The campaign now runs in the cluster. Track and retrieve it with:")
    click_echo("  vast exec cluster monitor")
    click_echo("  vast exec cluster upload-to-share   # once finished")
    click_echo("  vast results download")
    return campaign_id


def _sh_quote(value: str) -> str:
    import shlex
    return shlex.quote(str(value))
