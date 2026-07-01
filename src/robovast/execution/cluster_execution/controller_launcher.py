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
import shlex
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

# Port the in-pod control server (state + RPC) listens on; the host reaches it via
# `kubectl port-forward` (see control_client). Kept in sync with the server default.
_CONTROL_PORT = 8099

# In-pod directory the controller writes/reads the campaign tar.gz during
# upload-to-share (replaces the archiver sidecar's /data volume).
_POD_ARCHIVE_DIR = f"{_POD_WORKSPACE}/archive"


def _share_env_exports():
    """Build ``export`` lines carrying the share config into the controller pod.

    Reads ``ROBOVAST_SHARE_TYPE`` + the provider's env from the host environment
    (the ``.env`` is already loaded by ``cluster run``) and resolves it to the
    pod-side values via the provider's ``build_pod_env`` — so a GCS key *file* is
    read on the host and shipped as inline JSON, etc. Returns ``[]`` when no
    share is configured. Raises (click.UsageError) when required vars are missing,
    failing the launch before a pod is created.
    """
    share_type = os.environ.get("ROBOVAST_SHARE_TYPE", "").strip()
    if not share_type:
        raise ValueError(
            "No share destination configured: ROBOVAST_SHARE_TYPE is not set.\n"
            "The controller uploads every finished campaign to a share, so a run "
            "is refused when there is nowhere to deliver the results.\n"
            "Add it to a .env file in your project directory, e.g.:\n"
            "  ROBOVAST_SHARE_TYPE=webdav\n"
            "  ROBOVAST_WEBDAV_URL=https://nas.example.com/dav/results/\n"
            "  ROBOVAST_WEBDAV_USER=myuser\n"
            "  ROBOVAST_WEBDAV_PASSWORD=secret\n"
            "Supported types: nextcloud, gcs, sftp, webdav.")

    from .share_providers import \
        load_share_provider_plugins  # pylint: disable=import-outside-toplevel

    providers = load_share_provider_plugins()
    if share_type not in providers:
        available = ", ".join(sorted(providers)) or "(none installed)"
        raise ValueError(f"Unknown share type '{share_type}'. Available: {available}")

    provider = providers[share_type]()           # validates required env vars
    pod_env = {"ROBOVAST_SHARE_TYPE": share_type}
    pod_env.update(provider.build_pod_env())      # resolved creds (key file → JSON)
    share_url = os.environ.get("ROBOVAST_SHARE_URL", "").strip()
    if share_url:
        pod_env["ROBOVAST_SHARE_URL"] = share_url
    return [f"export {k}={_sh_quote(v)}" for k, v in pod_env.items()]


def _ntfy_env_exports():
    """Pass ntfy config from the host .env into the controller pod (best-effort).

    Notifications are optional, so unlike :func:`_share_env_exports` this never
    raises — when ``ROBOVAST_NTFY_TOPIC`` is unset the controller simply doesn't
    notify. Each var is forwarded only when present.
    """
    out = []
    for var in ("ROBOVAST_NTFY_TOPIC", "ROBOVAST_NTFY_SERVER", "ROBOVAST_NTFY_TOKEN"):
        val = os.environ.get(var, "").strip()
        if val:
            out.append(f"export {var}={_sh_quote(val)}")
    return out


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


def _build_wheel(project_root, package_label):
    """Build one project's wheel into a fresh temp dir. Returns its path, or None."""
    dist_dir = tempfile.mkdtemp(prefix="robovast_wheel_")
    try:
        subprocess.run(  # nosec - poetry on a known project root
            ["poetry", "build", "-f", "wheel", "-o", dist_dir],
            cwd=project_root, check=True, capture_output=True, text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        logger.warning("Could not build dev wheel for %s (%s); using the image's baseline.",
                       package_label, exc)
        shutil.rmtree(dist_dir, ignore_errors=True)
        return None
    wheels = [f for f in os.listdir(dist_dir) if f.endswith(".whl")]
    if not wheels:
        shutil.rmtree(dist_dir, ignore_errors=True)
        return None
    return os.path.join(dist_dir, wheels[0])


def build_dev_wheels():
    """Build wheels of the current robovast (+ robovast_nav) source.

    Returns a list of wheel paths (possibly empty). The wheels carry the
    *current* dev code, which is ``pip install --no-deps``'d over the baseline
    robovast(-nav) baked into the controller image. robovast_nav is only built
    when its source tree is present (it's a path dependency, so editable dev
    checkouts always have it; sdist/PyPI installs of robovast may not).
    """
    import robovast  # pylint: disable=import-outside-toplevel
    repo_root = os.path.abspath(os.path.join(os.path.dirname(robovast.__file__), "..", ".."))
    if not os.path.isfile(os.path.join(repo_root, "pyproject.toml")):
        logger.warning("No pyproject.toml at %s; using the controller image's baseline robovast.",
                       repo_root)
        return []
    wheels = []
    wheel = _build_wheel(repo_root, "robovast")
    if wheel:
        wheels.append(wheel)
    nav_root = os.path.join(repo_root, "src", "robovast_nav")
    if os.path.isfile(os.path.join(nav_root, "pyproject.toml")):
        nav_wheel = _build_wheel(nav_root, "robovast-nav")
        if nav_wheel:
            wheels.append(nav_wheel)
    return wheels


# Shared emptyDir mount point for auxiliary variation sidecars. Must match
# cluster_execution.container_runner.AUX_WORKSPACE so the controller and each
# sidecar see the workspace at the same absolute path.
_AUX_WORKSPACE = "/aux"


def _required_container_specs(config_path):
    """Collect the distinct auxiliary ContainerSpecs the campaign's variations need.

    Loads the ``.vast`` file and asks each declared variation plugin (via
    ``get_required_container``) whether it needs a helper image while it runs.
    Returns a list of ``ContainerSpec`` deduplicated by sidecar container name.
    Best-effort: a plugin that fails to load is skipped (the run will surface the
    real error later), so a launch is never blocked by container discovery.
    """
    from robovast.common.common import load_config  # pylint: disable=import-outside-toplevel
    from robovast.common.config_generation import \
        _get_variation_classes  # pylint: disable=import-outside-toplevel

    vast_dir = os.path.dirname(os.path.abspath(config_path))
    try:
        parameters = load_config(config_path)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Could not inspect '%s' for aux containers: %s", config_path, exc)
        return []

    # Batch campaigns declare variations under top-level ``configuration`` blocks;
    # search campaigns declare them once as ``search.variations`` (compose expands
    # that template into configuration blocks in-pod). Inspect both so the sidecar
    # is injected regardless of campaign type. Unsubstituted ``$name`` search
    # markers in the template are harmless here: get_required_container ignores
    # the parameter values.
    blocks = list(parameters.get("configuration", []) or [])
    search_variations = (parameters.get("search", {}) or {}).get("variations")
    if search_variations:
        blocks.append({"variations": search_variations})

    specs = {}
    for config_block in blocks:
        try:
            classes = _get_variation_classes(config_block, vast_dir)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Skipping aux-container discovery for a config block: %s", exc)
            continue
        for variation_class, variation_parameters in classes:
            try:
                spec = variation_class.get_required_container(variation_parameters)
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning("get_required_container failed for %s: %s",
                               getattr(variation_class, "__name__", variation_class), exc)
                continue
            if spec is not None:
                specs.setdefault(spec.container_name(), spec)
    return list(specs.values())


def _aux_sidecar_containers(specs):
    """Build sidecar container dicts for the given ContainerSpecs.

    Each sidecar runs the aux image with its one-shot entrypoint overridden by
    the spec's ``keep_alive_command`` so it stays up for the whole campaign, and
    mounts the shared ``aux`` volume so files staged by the controller are
    visible to the exec'd commands (and vice versa).
    """
    sidecars = []
    for spec in specs:
        container = {
            "name": spec.container_name(),
            "image": spec.image,
            "imagePullPolicy": "IfNotPresent",
            "command": list(spec.keep_alive_command),
            "volumeMounts": [{"name": "aux", "mountPath": _AUX_WORKSPACE}],
        }
        if spec.env:
            container["env"] = [{"name": k, "value": str(v)} for k, v in spec.env.items()]
        if spec.run_as_user:
            uid = spec.run_as_user.split(":", 1)[0]
            try:
                container["securityContext"] = {"runAsUser": int(uid)}
            except ValueError:
                pass
        sidecars.append(container)
    return sidecars


def _controller_pod_manifest(pod_name, namespace, image, campaign_label,
                             control_node_labels=None, aux_specs=None):
    aux_specs = aux_specs or []
    controller_mounts = [{"name": "workspace", "mountPath": _POD_WORKSPACE}]
    volumes = [{"name": "workspace", "emptyDir": {}}]
    if aux_specs:
        # Shared workspace between the controller and every aux sidecar.
        controller_mounts.append({"name": "aux", "mountPath": _AUX_WORKSPACE})
        volumes.append({"name": "aux", "emptyDir": {}})
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
            # Control channel (state + RPC). Declared for readability / Service
            # readiness; `kubectl port-forward` does not require it.
            "ports": [{"name": "control", "containerPort": _CONTROL_PORT}],
            "volumeMounts": controller_mounts,
        }] + _aux_sidecar_containers(aux_specs),
        "volumes": volumes,
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
    wheels = build_dev_wheels()

    # Reap finished controller pods from previous runs (running ones are left
    # alone — they belong to a concurrent campaign).
    reap_orphaned_runs(namespace=namespace, kube_context=kube_context)

    # Discover auxiliary containers declared by the campaign's variation plugins
    # and add them as kept-alive sidecars sharing a workspace with the controller.
    aux_specs = _required_container_specs(config_path)
    if aux_specs:
        click_echo("Adding auxiliary variation sidecar(s): "
                   + ", ".join(f"{s.container_name()} ({s.image})" for s in aux_specs))
    manifest = _controller_pod_manifest(pod_name, namespace, image, campaign_label,
                                        control_node_labels, aux_specs=aux_specs)
    click_echo(f"Creating controller pod '{pod_name}' (image {image})...")
    _kubectl(ctx_args, "apply", "-f", "-", input_text=yaml.safe_dump(manifest))
    try:
        _kubectl(ctx_args, "wait", "--for=condition=Ready", f"pod/{pod_name}",
                 "-n", namespace, "--timeout=300s")

        pod_wheels = []
        if wheels:
            click_echo("Copying dev wheel(s) into the controller pod...")
            _kubectl(ctx_args, "exec", pod_name, "-n", namespace, "-c", "controller",
                     "--", "mkdir", "-p", _POD_WHEEL_DIR)
            for wheel in wheels:
                pod_wheel = f"{_POD_WHEEL_DIR}/{os.path.basename(wheel)}"
                _kubectl(ctx_args, "cp", wheel, f"{namespace}/{pod_name}:{pod_wheel}",
                         "-c", "controller")
                pod_wheels.append(pod_wheel)
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
            f"export ROBOVAST_CONTROL_PORT={_CONTROL_PORT}",
            f"export ROBOVAST_ARCHIVE_DIR={_sh_quote(_POD_ARCHIVE_DIR)}",
        ]
        if kube_context:
            env_exports.append(f"export ROBOVAST_KUBE_CONTEXT={_sh_quote(kube_context)}")
        # Share-target credentials (upload-to-share now runs in the controller).
        # Resolved from the host .env; validated here so a missing var fails the
        # launch instead of the campaign.
        env_exports += _share_env_exports()
        # ntfy push-notification config (optional; topic enables it).
        env_exports += _ntfy_env_exports()

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
                   f"--root-user-action=ignore --disable-pip-version-check "
                   f"{' '.join(pod_wheels)} && "
                   if pod_wheels else "")
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
        for wheel in wheels:
            shutil.rmtree(os.path.dirname(wheel), ignore_errors=True)

    click_echo("")
    click_echo(f"✓ Controller started in-cluster (campaign id: {campaign_id}).")
    click_echo(f"  Controller pod: {pod_name}")
    click_echo("")
    ctx_suffix = f" -x {kube_context}" if kube_context else ""
    click_echo("The campaign now runs in the cluster. Track and retrieve it with:")
    click_echo(f"  vast exec cluster monitor{ctx_suffix}            # live loop state (batches, runs, budget)")
    click_echo(f"  vast exec cluster stop{ctx_suffix}               # graceful stop after the current batch")
    click_echo(f"  vast results download -i {campaign_id}")
    click_echo("")
    click_echo("The controller uploads the finished campaign to the configured share "
               "automatically. If the upload fails it stays alive; retry with:")
    click_echo(f"  vast exec cluster upload-to-share{ctx_suffix}    # retry a failed upload")
    return campaign_id


def _sh_quote(value: str) -> str:
    return shlex.quote(str(value))
