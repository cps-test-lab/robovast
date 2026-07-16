#!/usr/bin/env python3
# Copyright (C) 2025 Frederik Pasch
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

"""Shared cluster-campaign launch helper.

Extracted from the ``vast exec cluster run`` command so both the CLI and the
MCP ``campaign_control`` plugin launch cluster campaigns through one code path
(same preflight, same host-generated campaign id).
"""

import logging
import os
import tempfile

from dotenv import load_dotenv


def launch_cluster_campaign(*, config_filter=None, runs=None, log_tree=False,
                            kube_context=None, campaign_id=None, feedback=None):
    """Run the full cluster-launch preflight and start the controller pod.

    Performs ``.env`` discovery, context/access checks, cluster-config
    resolution and readiness verification, validates the ``--config`` filter,
    then launches the (fire-and-forget) controller pod.

    Args:
        config_filter: Optional glob; run only matching configurations (batch).
        runs: Optional runs override.
        log_tree: Forward the live scenario tree to the job logs.
        kube_context: Kubernetes context (default: active context).
        campaign_id: Optional campaign id to launch under (default: generated).
        feedback: Optional ``str -> None`` progress sink (e.g. ``click.echo``).

    Returns:
        The host-generated campaign id the controller runs under.

    Raises:
        ValueError: on any preflight failure (bad context, no cluster access,
            missing cluster config, cluster not ready, or an invalid
            ``--config`` filter).
    """
    from robovast.common.cli import get_project_config
    from robovast.common.cli.project_config import ProjectConfig
    from robovast.common.cluster_context import require_context_for_multi_cluster
    from robovast.common.common import load_config
    from robovast.common.config import validate_config
    from robovast.execution.cluster_execution.cluster_setup import (
        get_cluster_config_for_context, get_cluster_namespace,
        get_kubernetes_node_labels_from_config, load_cluster_setup_info)
    from robovast.execution.cluster_execution.controller_launcher import launch_controller
    from robovast.execution.cluster_execution.kubernetes import (
        check_kubernetes_access, get_kubernetes_client)
    from robovast.execution.controller import build_campaign_data

    say = feedback or (lambda _msg: None)

    require_context_for_multi_cluster(kube_context)  # raises ValueError
    context_key = kube_context

    # Load .env before touching cluster config / credentials so that
    # ROBOVAST_GCS_KEY_FILE, ROBOVAST_GCS_KEY_JSON, etc. are available. Respect
    # a global --vast-file override when invoked from within the CLI context.
    _vast_override = None
    try:
        import click  # pylint: disable=import-outside-toplevel
        _click_ctx = click.get_current_context(silent=True)
        if _click_ctx and _click_ctx.obj:
            _vast_override = _click_ctx.obj.get('vast_file')
    except Exception:  # pragma: no cover - click not on the call stack
        pass
    if _vast_override:
        load_dotenv(os.path.join(os.path.dirname(_vast_override), ".env"), override=False)
    project_file = ProjectConfig.find_project_file()
    if project_file:
        project_dir = os.path.dirname(os.path.abspath(project_file))
        loaded = ProjectConfig.load()
        if loaded and loaded.config_path and not _vast_override:
            load_dotenv(os.path.join(os.path.dirname(loaded.config_path), ".env"),
                        override=False)
        load_dotenv(os.path.join(project_dir, ".env"), override=False)
    else:
        load_dotenv(override=False)

    project_config = get_project_config()

    # Validate the --config filter on the host *before* launching the controller.
    # The controller runs fire-and-forget in-cluster, so without this check a typo
    # only surfaces in the controller pod log. (Search campaigns ignore --config.)
    if config_filter:
        campaign_config = validate_config(load_config(project_config.config_path))
        if campaign_config.search is None:
            with tempfile.TemporaryDirectory(prefix="robovast_cfgcheck_") as tmp:
                build_campaign_data(project_config.config_path, tmp, config_filter)

    # Check Kubernetes access (namespace-scoped so RBAC namespace-only users succeed).
    k8s_client = get_kubernetes_client(context=kube_context)
    namespace = get_cluster_namespace(context_key)
    say("Checking Kubernetes cluster access...")
    k8s_ok, k8s_msg = check_kubernetes_access(k8s_client, namespace=namespace)
    if not k8s_ok:
        raise ValueError(
            f"{k8s_msg}\nKubernetes cluster is required for RoboVAST execution.")
    logging.debug(k8s_msg)

    # Resolve the cluster config from the saved flag file (no pod needed).
    try:
        cluster_config = get_cluster_config_for_context(context_key)
    except Exception as e:
        raise ValueError(
            f"Failed to get cluster config: {e}\n"
            "To set up the cluster run: vast exec cluster setup <cluster-config>") from e
    if not cluster_config:
        raise ValueError(
            "No cluster config specified and no saved config found. "
            "Use --config <name> to select a config, or run setup first.")
    logging.debug("Auto-detected cluster config (credentials restored from flag file)")

    # Let the cluster config verify its own storage prerequisites (e.g. rke2
    # checks its MinIO pod; GCS is a no-op).
    cluster_config.verify_cluster_ready(
        k8s_client=k8s_client, namespace=namespace, kube_context=kube_context)

    # Both batch and search campaigns run via an in-cluster controller pod
    # (fire-and-forget): the controller drives the campaign and launches the
    # per-batch scenario jobs from inside the cluster, then publishes the
    # canonical campaign to storage. The host returns immediately.
    cfg_name, setup_kwargs = load_cluster_setup_info(context_key)
    _, control_node_labels = get_kubernetes_node_labels_from_config(
        project_config.config_path)
    return launch_controller(
        config_path=project_config.config_path, config_name=cfg_name,
        setup_kwargs=setup_kwargs, namespace=namespace, runs=runs,
        config_filter=config_filter, kube_context=kube_context,
        log_tree=log_tree, control_node_labels=control_node_labels,
        campaign_id=campaign_id)


def wait_for_cluster_campaign(campaign_id, *, kube_context=None, interval=5.0,
                              timeout=None, feedback=None):
    """Block until a cluster campaign reaches a terminal state.

    The controller uploads the finished campaign to the external share
    automatically, so "done" means the upload completed — ``phase == "finished"``
    with ``stage == "uploaded"`` (or the controller pod reaching ``Succeeded``).
    A plain ``phase == "finished"`` is *not* terminal: it also fires before the
    upload starts.

    Args:
        campaign_id: The campaign to wait for.
        kube_context: Kubernetes context (default: active context).
        interval: Poll interval in seconds.
        timeout: Optional overall timeout in seconds (None = wait forever).
        feedback: Optional ``str -> None`` progress sink (e.g. ``click.echo``).

    Returns:
        ``"succeeded"`` once the campaign is uploaded, or ``"failed"`` if the
        campaign or its upload failed.

    Raises:
        TimeoutError: if ``timeout`` elapses first.
        ValueError: if no controller pod can be found for the campaign.
    """
    import time  # pylint: disable=import-outside-toplevel

    from robovast.execution.cluster_execution import control_client
    from robovast.execution.cluster_execution.cluster_setup import get_cluster_namespace

    say = feedback or (lambda _msg: None)
    namespace = get_cluster_namespace(kube_context)
    deadline = None if timeout is None else (time.monotonic() + timeout)
    last_report = None
    seen_pod = False

    while True:
        pod, pod_phase = control_client.find_controller_pod(
            namespace=namespace, kube_context=kube_context, campaign=campaign_id)
        if pod is None:
            if not seen_pod:
                # Controller may not have registered its pod yet right after launch.
                if deadline is not None and time.monotonic() > deadline:
                    raise TimeoutError(
                        f"No controller pod appeared for {campaign_id!r}")
                time.sleep(interval)
                continue
            # The pod existed before and is gone now — treat as finished.
            say(f"Controller pod for {campaign_id} no longer present; assuming finished.")
            return "succeeded"
        seen_pod = True

        phase, stage = pod_phase, None
        try:
            with control_client.port_forward(
                    pod, namespace=namespace, kube_context=kube_context) as base_url:
                status = control_client.get_status(base_url)
            phase = status.get("phase", pod_phase)
            stage = status.get("stage")
        except Exception:  # noqa: BLE001 - channel not up yet / transient
            status = None

        report = f"{phase}" + (f"/{stage}" if stage else "")
        if report != last_report:
            say(f"{campaign_id}: {report}")
            last_report = report

        # Success: upload completed, or the pod exited cleanly.
        if (phase == "finished" and stage == "uploaded") or pod_phase == "Succeeded":
            return "succeeded"
        # Failure: campaign failed, upload failed (pod parked), or pod failed.
        if phase == "failed" or stage == "upload-failed" or pod_phase == "Failed":
            return "failed"

        if deadline is not None and time.monotonic() > deadline:
            raise TimeoutError(
                f"Campaign {campaign_id!r} did not finish within {timeout}s "
                f"(last state: {report})")
        time.sleep(interval)
