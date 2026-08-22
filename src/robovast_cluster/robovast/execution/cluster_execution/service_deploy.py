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

"""Deploy the persistent ``robovast-service`` into a cluster (mode 3).

This is the in-cluster counterpart of ``vast serve``: a long-lived Deployment
running the same FastAPI app (:mod:`robovast.service.app`), reached over a
ClusterIP Service via ``kubectl port-forward`` (Ingress later). It generalizes
the ephemeral, per-campaign control channel
(:mod:`robovast.execution.control_server`) into a campaign-spanning service that
launches and monitors controller pods on behalf of thin clients.

The manifests are pure dicts so they can be **server-side dry-run validated**
against a real API server without scheduling anything (see
``deploy_service(dry_run=True)``), and applied idempotently with the kubernetes
Python client — the same style as
:func:`robovast.execution.cluster_execution.cluster_setup.apply_controller_rbac`.

Note on image currency: the Deployment runs
:func:`robovast.common.execution.resolve_controller_image` with a service command. That
image must contain the ``robovast.service`` package; publish a service image (or layer the
current wheel) before a real rollout. Point ``ROBOVAST_PROJECT`` at your own registry to
run a dev build — it moves the whole image family, this one included.
"""

import datetime
import logging
import pathlib

from robovast.common.config_plugins import GIT_TOKEN_ENVS
from robovast.service.interface import DEFAULT_PORT

from .cluster_execution import BLOCKED_GRACE_SECONDS

logger = logging.getLogger(__name__)

SERVICE_NAME = "robovast-service"
SERVICE_ACCOUNT = "robovast-service"
#: Re-exported under the deployment's own name; the value lives with the
#: address space in ``service/interface.py``.
SERVICE_PORT = DEFAULT_PORT

#: Pod-template annotation stamped on every deploy, so the Deployment spec this run
#: submits always differs from the one already in the cluster and Kubernetes has to roll.
#:
#: Without it an upgrade that does not change the image *string* is a silent no-op: a
#: re-pushed floating ``:latest``, or a change confined to the Secrets, patches a
#: byte-identical spec, no new ReplicaSet is created, and ``wait_for_service_ready``
#: then sees the OLD pod — still Ready — and reports "upgraded and ready" while nothing
#: rolled. ``imagePullPolicy: Always`` does not save it: that governs a container that is
#: *starting*, and no container starts. It matters most for the env Secrets, which the
#: pod reads through ``envFrom`` exactly once, at container start.
#:
#: kubectl's own key rather than a private one: a hand-run ``kubectl rollout restart`` and
#: an upgrade are the same kind of event, and nothing here needs to tell them apart.
RESTART_ANNOTATION = "kubectl.kubernetes.io/restartedAt"

#: Where the service keeps its workspaces inside the pod, and the volume backing it.
#:
#: Mounted rather than left on the container's writable layer for the same reason the
#: registry is (see ``registry_deploy.registry_volume``): every upgrade restarts the pod,
#: so an unmounted workspace store is discarded on each version bump — every project a
#: user had pushed, gone, while the upgrade reports success. Campaign *results* live in
#: the object store and are unaffected, which is what made the loss easy to miss: the
#: data survives and the sources it was produced from do not.
#:
#: An explicit path plus ``ROBOVAST_WORKSPACES_ROOT`` rather than mounting over the
#: default ``~/.robovast/workspaces``: the default is resolved from ``HOME`` inside the
#: container (see ``robovast.service.workspaces.default_workspaces_root``), so a mount
#: hard-coding today's home directory would silently stop covering the store if that
#: ever changed. Here the manifest names the path it mounts.
WORKSPACES_VOLUME_NAME = "workspaces-data"
WORKSPACES_DATA_DIR = "/var/lib/robovast-workspaces"
DEFAULT_WORKSPACES_HOST_PATH = "/var/lib/robovast-workspaces"
WORKSPACES_ROOT_ENV = "ROBOVAST_WORKSPACES_ROOT"


def workspaces_volume(storage_path="", storage_class=""):
    """The volume backing the workspace store: a PVC when provisionable, else hostPath.

    Mirrors :func:`registry_deploy.registry_volume`, including its reason for defaulting
    to hostPath: a stock RKE2 cluster ships no StorageClass, so a PVC there stays Pending
    forever. hostPath pins the workspaces to one node's disk, the same constraint the
    registry already imposes on this pod.

    ``emptyDir`` is deliberately not offered — it would survive nothing that matters here,
    since the restart is exactly the event this volume exists to outlive.
    """
    if storage_class:
        return {"name": WORKSPACES_VOLUME_NAME,
                "persistentVolumeClaim": {"claimName": WORKSPACES_VOLUME_NAME}}
    return {"name": WORKSPACES_VOLUME_NAME,
            "hostPath": {"path": storage_path or DEFAULT_WORKSPACES_HOST_PATH,
                         "type": "DirectoryOrCreate"}}


def workspaces_pvc_manifest(namespace, storage_class, size="20Gi"):
    """The PVC for :func:`workspaces_volume`, or ``None`` when backed by hostPath."""
    if not storage_class:
        return None
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": WORKSPACES_VOLUME_NAME, "namespace": namespace,
                     "labels": {"app": SERVICE_NAME}},
        "spec": {"accessModes": ["ReadWriteOnce"],
                 "storageClassName": storage_class,
                 "resources": {"requests": {"storage": size}}},
    }


def _service_rbac_manifests(namespace):
    """ServiceAccount + Role/RoleBinding letting the service launch controllers.

    The service creates and monitors **controller pods** (and their logs) in its
    namespace — the host's role today — so it needs pod create/read/delete plus
    the ``pods/log`` subresource. It does not itself create scenario Jobs (the
    controllers do), so no ``batch`` verbs here.

    Plus a cluster-scoped read-only ClusterRole (nodes + pods) backing the
    ``/usage`` endpoint — see the ClusterRole manifest below.
    """
    role_name = SERVICE_ACCOUNT
    cluster_role_name = f"{SERVICE_ACCOUNT}-usage-{namespace}"
    return [
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": SERVICE_ACCOUNT, "namespace": namespace},
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": {"name": role_name, "namespace": namespace},
            # The service drives campaigns in-process now (there is no controller
            # pod), so it needs everything that pod's ServiceAccount used to hold.
            "rules": [
                # Scenario runs and the rosbag→CSV postprocessing are Jobs the
                # service creates, watches and reaps.
                {"apiGroups": ["batch"], "resources": ["jobs"],
                 "verbs": ["create", "get", "list", "watch", "delete", "deletecollection"]},
                # read_namespaced_job_status hits the jobs/status subresource,
                # which is a distinct RBAC resource from jobs.
                {"apiGroups": ["batch"], "resources": ["jobs/status"],
                 "verbs": ["get", "list", "watch"]},
                # Job pods (+ logs), and the per-campaign auxiliary-container pods
                # the service creates and tears down.
                {"apiGroups": [""], "resources": ["pods", "pods/log"],
                 "verbs": ["create", "get", "list", "watch", "delete", "deletecollection"]},
                # The registry push Secret: read to authenticate the "is this image
                # already pushed?" probe (see ClusterService._resolve_registry_objects).
                # Read-only, by name -- nothing here ever writes a Secret.
                {"apiGroups": [""], "resources": ["secrets"], "verbs": ["get"]},
                # ConfigMaps are NOT read-only, unlike the Secret above: besides reading
                # the private-CA ConfigMap, postprocessing ships its scripts into the
                # postprocess Job as a ConfigMap it creates, replaces on a re-run and
                # deletes afterwards (see postprocess_job.py). Granting only "get"
                # alongside the Secret let every campaign RUN and then fail its
                # postprocessing with a 403 -- after the compute was already spent.
                {"apiGroups": [""], "resources": ["configmaps"],
                 "verbs": ["create", "get", "list", "update", "patch", "delete"]},
                # Variations that declare an auxiliary container run their commands
                # in that campaign's aux pod via the pods/exec subresource (see
                # cluster_execution.container_runner.ClusterContainerRunner).
                {"apiGroups": [""], "resources": ["pods/exec"],
                 "verbs": ["create", "get"]},
                # Stopping a campaign tears down its Kueue Workloads: list to find
                # the ones owned by the campaign's jobs, delete/deletecollection to
                # remove them, and patch to strip finalizers off any stuck ones
                # (see cluster_execution.kubernetes_kueue.cleanup_kueue_workloads).
                {"apiGroups": ["kueue.x-k8s.io"], "resources": ["workloads"],
                 "verbs": ["get", "list", "watch", "delete", "deletecollection", "patch"]},
                # The admission preflight reads the LocalQueue every job is labeled
                # into, to fail loudly instead of letting Kueue suspend the batch
                # forever (kubernetes_kueue.verify_kueue_admission_ready).
                {"apiGroups": ["kueue.x-k8s.io"], "resources": ["localqueues"],
                 "verbs": ["get", "list"]},
            ],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {"name": role_name, "namespace": namespace},
            "subjects": [{"kind": "ServiceAccount", "name": SERVICE_ACCOUNT,
                          "namespace": namespace}],
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role",
                        "name": role_name},
        },
        # Cluster-scoped reads: nodes for the /usage endpoint (cluster resources, not
        # grantable via a namespaced Role) with cluster-wide pod requests for the true
        # "used" figure across tenants, and the ClusterQueue behind the LocalQueue for
        # the admission preflight. Read-only (get/list). The ClusterRole name is
        # namespaced so parallel robovast deployments don't collide.
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": cluster_role_name},
            "rules": [
                {"apiGroups": [""], "resources": ["nodes", "pods"],
                 "verbs": ["get", "list"]},
                # /usage also reads each kubelet's Summary API (stats/summary) through the
                # nodes/proxy subresource, for the disk meter. Also granted by the
                # controller-nodes ClusterRole for its configz read, but /usage's own
                # dependency belongs in /usage's own role -- a pruned controller role must
                # not silently take the disk meter with it.
                {"apiGroups": [""], "resources": ["nodes/proxy"], "verbs": ["get"]},
                {"apiGroups": ["kueue.x-k8s.io"], "resources": ["clusterqueues"],
                 "verbs": ["get", "list"]},
            ],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRoleBinding",
            "metadata": {"name": cluster_role_name},
            "subjects": [{"kind": "ServiceAccount", "name": SERVICE_ACCOUNT,
                          "namespace": namespace}],
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole",
                        "name": cluster_role_name},
        },
    ]


def _deployment_manifest(namespace, image, env=None, git_secret=False,
                         env_secret_names=(), pull_secret="", restarted_at=None,
                         registry_storage_path="", registry_storage_class="",
                         workspaces_storage_path="", workspaces_storage_class="",
                         registry_node=""):
    """The robovast-service Deployment (1 replica, stateless — no PVC).

    Binds ``0.0.0.0`` inside the pod (reachable only via the ClusterIP Service +
    port-forward/Ingress — the pod network is the boundary). Runs ``vast serve``.

    When *git_secret* is set, the GitHub token Secret is mounted **read-only as a
    file** (never exposed as an env var, so it is not inherited by child processes
    or visible to composition code) at :data:`GIT_TOKEN_MOUNT_DIR`.

    *env_secret_names* are the env-secret Secrets (share creds, ntfy creds — see
    :data:`ENV_SECRET_SOURCES`) pulled in via ``envFrom``. Those *must* be env
    vars, because the in-driver upload / notifier read them straight from
    ``os.environ`` (see ``in_pod_upload.load_provider_from_env`` and
    ``Notifier.from_env``).

    *pull_secret* is the dockerconfigjson Secret authenticating the pull of this
    service's OWN image. It is easy to forget because the service is the thing that
    hands that same secret to every campaign pod it creates (see
    ``KubernetesBackend`` and ``ROBOVAST_REGISTRY_PULL_SECRET``) — so a cluster whose
    controller image sits in a private registry deployed a service that could pull
    images for everyone except itself, and setup reported success while the pod sat in
    ImagePullBackOff.

    *restarted_at* is the :data:`RESTART_ANNOTATION` value; it defaults to now, which is
    what makes every deploy roll. Pass a fixed value to compare two manifests without the
    timestamp being the difference.

    The ``registry_*`` arguments configure the container registry that runs beside the
    service (see :mod:`.registry_deploy`) — it is a second container in this pod rather
    than its own Deployment, so that one restart covers both and the Ingress can reach it
    on the same Service.
    """
    from . import registry_deploy  # pylint: disable=import-outside-toplevel

    if restarted_at is None:
        restarted_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    container = {
        "name": SERVICE_NAME,
        "image": image,
        "imagePullPolicy": "Always",
        "command": ["vast", "serve",
                    "--host", "0.0.0.0", "--port", str(SERVICE_PORT)],
        "ports": [{"containerPort": SERVICE_PORT, "name": "http"}],
        "env": list(env or []),
        "readinessProbe": {
            "httpGet": {"path": "/healthz", "port": SERVICE_PORT},
            "initialDelaySeconds": 5, "periodSeconds": 10},
        "livenessProbe": {
            "httpGet": {"path": "/healthz", "port": SERVICE_PORT},
            "initialDelaySeconds": 15, "periodSeconds": 20},
    }
    if env_secret_names:
        container["envFrom"] = [{"secretRef": {"name": n}} for n in env_secret_names]
    # Point the store at the mount, unless the caller already set it explicitly.
    if not any(e.get("name") == WORKSPACES_ROOT_ENV for e in container["env"]):
        container["env"].append({"name": WORKSPACES_ROOT_ENV,
                                 "value": WORKSPACES_DATA_DIR})
    container["volumeMounts"] = [{"name": WORKSPACES_VOLUME_NAME,
                                  "mountPath": WORKSPACES_DATA_DIR}]
    pod_spec = {
        "serviceAccountName": SERVICE_ACCOUNT,
        "containers": [container, registry_deploy.registry_container()],
        "volumes": [registry_deploy.registry_volume(
            registry_storage_path, registry_storage_class),
            workspaces_volume(workspaces_storage_path, workspaces_storage_class)],
    }
    node_selector = registry_deploy.registry_node_selector(registry_node)
    if node_selector:
        pod_spec["nodeSelector"] = node_selector
    if pull_secret:
        pod_spec["imagePullSecrets"] = [{"name": pull_secret}]
    if git_secret:
        container["volumeMounts"].append({
            "name": "git-credentials", "mountPath": GIT_TOKEN_MOUNT_DIR,
            "readOnly": True})
        pod_spec["volumes"].append({
            "name": "git-credentials",
            "secret": {"secretName": GIT_SECRET_NAME, "defaultMode": 0o400}})
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": SERVICE_NAME, "namespace": namespace,
                     "labels": {"app": SERVICE_NAME}},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": SERVICE_NAME}},
            "template": {
                "metadata": {"labels": {"app": SERVICE_NAME},
                             "annotations": {RESTART_ANNOTATION: restarted_at}},
                "spec": pod_spec,
            },
        },
    }


def _service_manifest(namespace, ingress_class=""):
    """ClusterIP Service exposing the Deployment on :data:`SERVICE_PORT`.

    *ingress_class* only matters for GKE's built-in ``gce`` controller, which — unlike
    ingress-nginx — **cannot route to a plain ClusterIP**. It needs either a NodePort
    backend or container-native load balancing, which is what the ``neg`` annotation
    below asks for. Without it a GKE Ingress created against this Service simply never
    becomes healthy, and the reason appears in the load balancer rather than anywhere a
    RoboVAST user would look.

    ``nginx`` is the tested path (rke2 ships ingress-nginx); ``gce`` is supported here
    but has not been exercised on a real GKE cluster.
    """
    from . import registry_deploy  # pylint: disable=import-outside-toplevel

    annotations = {}
    if ingress_class == "gce":
        annotations["cloud.google.com/neg"] = '{"ingress": true}'
    metadata = {"name": SERVICE_NAME, "namespace": namespace,
                "labels": {"app": SERVICE_NAME}}
    if annotations:
        metadata["annotations"] = annotations
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": metadata,
        "spec": {
            "type": "ClusterIP",
            "selector": {"app": SERVICE_NAME},
            # Two ports on one Service because the registry is a container in the same
            # pod: the Ingress routes "/" to http and "/v2" to registry, so both halves
            # answer on one hostname with one certificate.
            "ports": [
                {"port": SERVICE_PORT, "targetPort": SERVICE_PORT, "name": "http"},
                {"port": registry_deploy.REGISTRY_PORT,
                 "targetPort": registry_deploy.REGISTRY_PORT, "name": "registry"},
            ],
        },
    }


def wait_for_service_ready(namespace="default", kube_context=None, timeout_s=180.0):
    """Block until the service Deployment has a Ready replica, or say why it has not.

    Setup used to return the moment the Deployment was *created* and print
    "✓ Cluster setup completed successfully!", so an image that cannot be pulled
    surfaced one command later as a connection failure — pointing at the network
    rather than at the ImagePullBackOff that actually happened. The pod's own reason is
    right there; reporting it here is the difference between a five-second fix and a
    debugging session.

    Raises:
        RuntimeError: not Ready within *timeout_s*, carrying the pod's pending reason.
    """
    import time  # pylint: disable=import-outside-toplevel

    from kubernetes import client  # pylint: disable=import-outside-toplevel

    from .kube_client import pod_pending_reason  # pylint: disable=import-outside-toplevel

    _load_kube_config(kube_context)
    apps = client.AppsV1Api()
    core = client.CoreV1Api()
    from kubernetes.client.rest import ApiException  # pylint: disable=import-outside-toplevel

    deadline = time.monotonic() + timeout_s
    reason = ""
    while time.monotonic() < deadline:
        try:
            status = apps.read_namespaced_deployment_status(SERVICE_NAME, namespace).status
        except ApiException as exc:
            if exc.status != 404:
                raise
            # Raw, this is a 404 with a page of HTTP headers — the unhelpful failure
            # this whole path exists to replace. It happens when a caller waits on a
            # namespace nothing was deployed into, or when the Deployment is removed
            # mid-wait.
            raise RuntimeError(
                f"no {SERVICE_NAME} Deployment in namespace {namespace!r} to wait for. "
                "Deploy it with 'vast exec cluster setup <flavor>'.") from exc
        if (status.ready_replicas or 0) >= 1:
            return
        pods = core.list_namespaced_pod(namespace,
                                        label_selector=f"app={SERVICE_NAME}").items
        # The newest pod: a rollout leaves the old one Running while the new one fails,
        # and the old one's contented status is not the answer to "why is this stuck?".
        if pods:
            newest = max(pods, key=lambda p: p.metadata.creation_timestamp)
            reason = pod_pending_reason(newest) or (newest.status.phase or "")
        time.sleep(2)
    raise RuntimeError(
        f"the {SERVICE_NAME} pod was not ready within {int(timeout_s)}s"
        f"{f': {reason}' if reason else ''}. "
        f"Inspect it with 'kubectl -n {namespace} describe pod -l app={SERVICE_NAME}'.")


class RolloutNotConverged(RuntimeError):
    """The upgrade's new pod never took over. Carries the pod's own reason for it."""

    #: The pod's reason is the whole diagnosis; a traceback would only bury it.
    include_traceback = False


#: How long the incoming pod may stay unhealthy before the rollout is called failed.
#:
#: A grace window rather than an immediate abort because the two signals watched here are
#: not certainly terminal: kubelet pull back-off does clear on its own once a rotated
#: credential lands, and a pod that restarts once and then stabilises is a fine outcome.
#: Matches ``kubernetes_backend.KubernetesBackend._BLOCKED_GRACE_SECONDS``, which makes the
#: same trade for a campaign's jobs, and with the image-build status read -- so all three
#: take the value from one place (:data:`~.cluster_execution.BLOCKED_GRACE_SECONDS`) rather
#: than each restating it and drifting apart.
UNHEALTHY_GRACE_SECONDS = BLOCKED_GRACE_SECONDS

#: How often to say something while the rollout is still in progress. The complaint this
#: answers is not that an upgrade takes minutes -- pulling a controller image legitimately
#: does -- but that it did so in total silence, which is indistinguishable from a hang.
_HEARTBEAT_SECONDS = 15.0


def _rollout_pod_state(core, namespace):
    """``(pod, unhealthy)`` for the incoming pod: the newest one and why it is not fine.

    Only the newest pod is judged, for the reason ``wait_for_service_ready`` gives: during
    a rolling update the outgoing pod is still Ready, and its contentment is not the answer
    to "why has this not rolled?".

    It also makes a restart meaningful. On a pod created seconds ago, inside the rollout
    window, ``restart_count >= 1`` *is* a crash-loop -- which it would not be for the
    long-lived pod of a steady-state Deployment.

    ``unhealthy`` is ``(reason, message)`` or ``None``. Raises whatever the API raises;
    the caller decides what an unreadable cluster means.
    """
    # Deferred: this module is imported by the client-side CLI, and cluster_execution
    # pulls in the batch lane.
    from .cluster_execution import pod_block_reason  # pylint: disable=import-outside-toplevel
    from .cluster_execution import pod_restarted_containers

    pods = core.list_namespaced_pod(namespace,
                                    label_selector=f"app={SERVICE_NAME}").items
    if not pods:
        return None, None
    newest = max(pods, key=lambda p: p.metadata.creation_timestamp)
    return newest, (pod_block_reason(newest) or pod_restarted_containers(newest))


def wait_for_rollout(namespace="default", kube_context=None, timeout_s=180.0,
                     unhealthy_grace_s=UNHEALTHY_GRACE_SECONDS, report=None) -> None:
    """Block until the Deployment's *new* pod is the one running, or say why it never was.

    ``wait_for_service_ready`` returns as soon as one replica is Ready -- which the
    **old** pod satisfies for the whole of a rolling update. Anything reading the cluster
    right after it can therefore be looking at the generation being replaced. That is
    exactly what made ``upgrade`` report "image unchanged" across a genuine image change:
    it read the outgoing pod both times.

    Convergence is the Deployment's own account of it: the controller has observed this
    spec (``observedGeneration``), every replica is on the new template
    (``updatedReplicas``), and none of the old ones are left (``replicas``).

    Those counters alone, however, cannot fail. This used to watch nothing else and return
    False on timeout, so an incoming pod in ``ImagePullBackOff`` -- a reason the kubelet
    already had -- was three minutes of silence followed by a caller that printed
    "✓ upgraded and ready" anyway. So the incoming pod is watched too, and this raises
    rather than returning a verdict a caller has to remember to check.

    Args:
        report: optional ``callable(str)`` for progress lines. A callback rather than
            ``click.echo`` because only the CLI in this package speaks click.

    Raises:
        RolloutNotConverged: the incoming pod stayed unhealthy for *unhealthy_grace_s*,
            or nothing converged within *timeout_s*.
    """
    import time  # pylint: disable=import-outside-toplevel

    from kubernetes import client  # pylint: disable=import-outside-toplevel

    _load_kube_config(kube_context)
    apps = client.AppsV1Api()
    core = client.CoreV1Api()
    say = report or (lambda _message: None)

    started = time.monotonic()
    deadline = started + timeout_s
    unhealthy_since = None
    last_signal = ""
    last_heartbeat = started
    while time.monotonic() < deadline:
        dep = apps.read_namespaced_deployment_status(SERVICE_NAME, namespace)
        want = dep.spec.replicas or 1
        st = dep.status
        if ((st.observed_generation or 0) >= (dep.metadata.generation or 0)
                and (st.updated_replicas or 0) == want
                and (st.replicas or 0) == want
                and (st.available_replicas or 0) == want):
            return

        try:
            pod, unhealthy = _rollout_pod_state(core, namespace)
        except Exception as exc:  # noqa: BLE001 - one failed probe must not end the wait
            # "Unknown", NOT "healthy": clearing the timer here would reset the grace
            # window on every unreadable poll and let a permanently blocked rollout run to
            # the full timeout. Keep whatever state we had.
            logger.warning("Could not check the %s pod this cycle: %s", SERVICE_NAME, exc)
            pod, unhealthy = None, None
        else:
            if unhealthy:
                reason, message = unhealthy
                signal = f"{reason}: {message}" if message else reason
                if unhealthy_since is None:
                    unhealthy_since = time.monotonic()
                    last_signal = signal
                    say(f"⚠ the new pod is not starting: {signal}")
                    # debug, not warning: `say` has just put this in front of the operator
                    # and the raise below repeats it. At warning level the console printed
                    # the same 300-character kubelet message twice, back to back.
                    logger.debug("Rollout of %s in %s: %s", SERVICE_NAME, namespace, signal)
                elif time.monotonic() - unhealthy_since >= unhealthy_grace_s:
                    raise RolloutNotConverged(
                        _blocked_message(namespace, signal, unhealthy_grace_s,
                                         kube_context))
                else:
                    last_signal = signal
            else:
                # A clean probe is the only thing that clears the timer, so a transient
                # blip does not accumulate toward the deadline across separate stalls.
                unhealthy_since = None
                last_signal = ""

        now = time.monotonic()
        if now - last_heartbeat >= _HEARTBEAT_SECONDS:
            last_heartbeat = now
            say(_progress_line(pod, unhealthy_since, now, started, timeout_s,
                               unhealthy_grace_s))
        time.sleep(1)

    raise RolloutNotConverged(_timeout_message(namespace, last_signal, timeout_s,
                                               kube_context))


def _progress_line(pod, unhealthy_since, now, started, timeout_s, grace_s) -> str:
    """One heartbeat line: what the incoming pod is doing, and how long it has left.

    The reason itself is deliberately not repeated. It was already reported in full when it
    first appeared, and kubelet alternates ``ErrImagePull`` with ``ImagePullBackOff`` while
    it backs off, so echoing it each time buried the run in five copies of the same
    300-character message.

    The budget counts toward whichever deadline will actually fire: once the pod is
    unhealthy that is the grace window, not the overall timeout. Showing "46s/180s" of a run
    that then died at 60s was simply wrong.
    """
    if unhealthy_since is not None:
        return (f"still not starting ({int(now - unhealthy_since)}s/{int(grace_s)}s "
                f"before this is called failed)")
    phase = (getattr(pod.status, "phase", None) or "?") if pod is not None else "no pod yet"
    return f"waiting for the new pod ({int(now - started)}s/{int(timeout_s)}s, {phase})"


def _vast_doctor(namespace, kube_context) -> str:
    """``vast doctor`` for this cluster, as a command that can be pasted."""
    context = f" -x {kube_context}" if kube_context else ""
    namespace_flag = f" -n {namespace}" if namespace != "default" else ""
    return f"vast doctor{context}{namespace_flag}"


def _kubectl(namespace, kube_context) -> str:
    """The ``kubectl`` prefix for *this* cluster, as a command that can be pasted.

    ``--context`` whenever one was given, because the point of a suggested command is that
    it runs. Without it the command silently targets whatever the kubeconfig's
    current-context happens to be -- and on a host that also talks to a remote cluster,
    that is a command which hangs against the wrong one and blames this cluster for it.
    """
    context = f" --context {kube_context}" if kube_context else ""
    return f"kubectl{context} -n {namespace}"


def _next_step(signal, namespace, kube_context) -> str:
    """The one thing to do about *signal*, as a runnable command.

    Four states, four different actions -- the same reason ``_status_next_step`` in the MCP
    layer branches rather than offering one generic hint: a credential fault wants the
    config checked, a crash-loop wants the dead container's log, and telling either to
    "inspect the pod" is a dead end for the reader who already did.

    A ``vast`` command leads wherever one covers the state, since running an upgrade proves
    the reader has ``vast`` but not that they have a kubeconfig pointed at this cluster.
    ``kubectl`` is offered as the deeper look, not as the instruction.
    """
    pods = f"-l app={SERVICE_NAME}"
    kubectl = _kubectl(namespace, kube_context)
    if "Image" in signal:
        return (f"Next: '{_vast_doctor(namespace, kube_context)}' checks the registry config "
                f"and credentials this pull uses. With cluster access, "
                f"'{kubectl} describe pod {pods}' shows the kubelet's own account.")
    if "Unschedulable" in signal or "SchedulerError" in signal:
        # The scheduler's message is already quoted above and names the resource
        # ("0/1 nodes are available: 1 Insufficient nvidia.com/gpu"), so this is about
        # capacity, not configuration -- there is nothing to check on this host.
        return ("Next: the scheduler's message above names what no node could satisfy. Free "
                "that resource, or deploy where it exists.")
    if "Restarted" in signal:
        return (f"Next: with cluster access, '{kubectl} logs {pods} --previous --tail=50' -- "
                f"the container that died is the *previous* one, so the current pod's log "
                f"does not hold the crash.")
    return (f"Next: with cluster access, '{kubectl} logs {pods} --tail=50' and "
            f"'{kubectl} rollout status deploy/{SERVICE_NAME}'.")


def _pull_credential_hint(signal) -> str:
    """The credential paragraph, for a reason that is about fetching the image.

    Only for image reasons: on an ``Unschedulable`` or a crash-loop it would send the
    reader to audit credentials that are working fine.
    """
    if "Image" not in signal:
        return ""
    return ("\n\nAn image reason points at the pull credentials: ROBOVAST_REGISTRY_SERVER, "
            "ROBOVAST_REGISTRY_USERNAME and ROBOVAST_REGISTRY_PASSWORD, read from './.env' "
            "in the CURRENT directory only, then '~/.config/robovast/env'. A tag that was "
            "never pushed looks identical from here, so check ROBOVAST_PROJECT_TAG too.")


#: Closing paragraph for both failures. It answers the two questions the reason itself does
#: not: is the service down (no -- with one replica Kubernetes keeps the old pod until the
#: new one is Available), and does a retry start over (no -- the Deployment is already
#: patched, so the next upgrade re-rolls the same spec). Without them a failed upgrade
#: reads as an outage.
_STILL_SERVING = (
    "\n\nThe previous pod is still serving -- with a single replica Kubernetes keeps it "
    "until the new one is Available -- so the API is up on the old version. The Deployment "
    "has already been patched, so another 'upgrade' re-rolls the same spec rather than "
    "starting over.")


def _blocked_message(namespace, signal, grace_s, kube_context=None) -> str:
    return (f"the new {SERVICE_NAME} pod did not start within {int(grace_s)}s and will not "
            f"recover on its own. Kubernetes reports: {signal}."
            f"{_pull_credential_hint(signal)}{_STILL_SERVING}\n\n"
            f"{_next_step(signal, namespace, kube_context)}")


def _timeout_message(namespace, signal, timeout_s, kube_context=None) -> str:
    # No signal means the pod never reported anything wrong -- it is simply still coming
    # up, so the logs are where the answer is, not the pod's status.
    detail = (f" Kubernetes last reported: {signal}." if signal else
              " The pod reported no error, so it was still starting: a large image pull, or "
              "a container that is up but never becomes Ready.")
    return (f"the {SERVICE_NAME} rollout did not converge within {int(timeout_s)}s.{detail}"
            f"{_pull_credential_hint(signal)}{_STILL_SERVING}\n\n"
            f"{_next_step(signal, namespace, kube_context)} If this cluster simply needs "
            f"longer than {int(timeout_s)}s, raise it with '--timeout'.")


def running_image_digest(namespace="default", kube_context=None,
                         container=SERVICE_NAME) -> str:
    """The digest of the image the service pod is *running*, or "" if there is none.

    Not the Deployment's image ref: that is what was asked for, and with a floating tag
    it says nothing about which bytes arrived. ``imageID`` is what the kubelet resolved
    the ref to when it pulled, so it answers the only question an upgrade actually
    raises -- did this roll onto new code? -- and it answers it identically whether the
    ref was ``:latest`` or already a digest.

    Empty rather than raising: this is reporting, and an upgrade that worked must not
    fail because the thing describing it could not read a field.
    """
    from kubernetes import client  # pylint: disable=import-outside-toplevel

    try:
        _load_kube_config(kube_context)
        pods = client.CoreV1Api().list_namespaced_pod(
            namespace, label_selector=f"app={SERVICE_NAME}").items
        running = [p for p in pods if (p.status.phase or "") == "Running"]
        if not running:
            return ""
        # The newest Running pod. During a rollout both generations are up, and the old
        # one's digest is precisely the wrong answer to "what is running now?".
        newest = max(running, key=lambda p: p.metadata.creation_timestamp)
        for status in (newest.status.container_statuses or []):
            if status.name == container:
                # docker-shim era kubelets prefix this with the repo and a "docker://"
                # scheme; the digest is the part anyone compares.
                image_id = status.image_id or ""
                _, sep, digest = image_id.partition("@")
                return digest if sep else image_id
        return ""
    except Exception:  # pylint: disable=broad-except
        return ""


class IngressRefused(RuntimeError):
    """An Ingress was asked for in a configuration that would publish an open service."""

    #: A caller error with a self-contained message; a traceback would only obscure it.
    include_traceback = False


def validate_ingress_options(ingress_host="", tls_secret="", issuer="",
                             insecure_http=False, have_token=True):
    """Refuse an unpublishable combination **before** anything is changed.

    A pure argument check, so it belongs at the very start of setup. It used to live
    only inside :func:`_ingress_manifest`, which runs after Kueue has been installed and
    the cluster's storage deployed — so an operator who forgot ``--issuer`` discovered it
    only once the cluster had already been modified. The check costs nothing; doing it
    late costs a half-finished setup.

    Raises:
        IngressRefused: naming which combination and why.
    """
    if not ingress_host:
        return
    if not have_token:
        raise IngressRefused(
            "refusing to create an Ingress with no access token configured: it would "
            "publish an unauthenticated RoboVAST, and a campaign names its own "
            "container image. Set ROBOVAST_AUTH_TOKEN, or let setup generate one.")
    if not (tls_secret or issuer) and not insecure_http:
        raise IngressRefused(
            f"refusing to publish {ingress_host} over plain HTTP: the shared token would "
            "cross the network in clear text, and the session cookie is Secure so the "
            "login would not work at all. Pass a TLS secret or a cert-manager issuer "
            "(tools/setup_ingress_tls.py sets one up), or --insecure-http to accept "
            "this on a trusted network.")


def _ingress_manifest(namespace, host, ingress_class="", tls_secret="",
                      issuer="", *, auth_token="", insecure=False):
    """Ingress publishing the service at *host*, or ``None`` when none was asked for.

    **Refuses two configurations rather than documenting them as dangerous.**

    Without an access token, an Ingress publishes an unauthenticated UI whose campaigns
    name their own container image — anyone who reaches it can run containers in the
    cluster. And over plain HTTP the shared secret crosses the network in clear text
    while the session cookie's ``Secure`` flag makes it unusable anyway, so the login
    would not merely be insecure, it would not work.

    This is the one place the code is deliberately opinionated: both mistakes are
    invisible once made, and both are made by omission.
    """
    if not host:
        return None
    # The same check setup runs up front; repeated here so the manifest cannot be built
    # by a caller that skipped it.
    validate_ingress_options(host, tls_secret, issuer, insecure, have_token=bool(auth_token))

    from . import registry_deploy  # pylint: disable=import-outside-toplevel

    # The registry's upload limits ride on this Ingress: it publishes /v2, and nginx's
    # 1m default body size would 413 every image layer.
    annotations = dict(registry_deploy.REGISTRY_INGRESS_ANNOTATIONS)
    if issuer:
        annotations["cert-manager.io/cluster-issuer"] = issuer

    spec = {
        "rules": [{
            "host": host,
            # `/v2` first: the service's own UI is mounted at `/`, which matches
            # everything, so the registry rule has to be the more specific one. nginx
            # picks by path before either backend sees the request, and the service
            # registers no `/v2` route of its own, so the two do not collide.
            "http": {"paths": [
                registry_deploy.registry_ingress_path(),
                {
                    "path": "/",
                    "pathType": "Prefix",
                    "backend": {"service": {"name": SERVICE_NAME,
                                            "port": {"number": SERVICE_PORT}}},
                },
            ]},
        }],
    }
    if ingress_class:
        spec["ingressClassName"] = ingress_class
    if tls_secret or issuer:
        # cert-manager fills a secret named here when an issuer is annotated; naming it
        # explicitly is what tells the controller where to put (or find) the cert.
        spec["tls"] = [{"hosts": [host],
                        "secretName": tls_secret or f"{SERVICE_NAME}-tls"}]
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {"name": SERVICE_NAME, "namespace": namespace,
                     "labels": {"app": SERVICE_NAME},
                     **({"annotations": annotations} if annotations else {})},
        "spec": spec,
    }


#: Secret + key holding the GitHub token that lets the service install a
#: private-repo (``git+https``) variation plugin declared in a ``.vast``'s
#: ``plugins:``. Sourced from the host env at setup; never reaches a controller pod.
#: **Mounted read-only as a file** (not an env var) so it is not inherited by any
#: child process/command — the path must match ``config_plugins.GIT_TOKEN_FILE``.
GIT_SECRET_NAME = "robovast-git-credentials"
GIT_SECRET_KEY = "token"
GIT_TOKEN_MOUNT_DIR = "/var/run/secrets/robovast-git"
# Host env vars a GitHub token may come from at setup. Shared with the
# compose-time reader (``config_plugins.GIT_TOKEN_ENVS``) so the cluster and a
# local run accept the *same* names — one source of truth, no drift.
_GIT_TOKEN_HOST_ENVS = GIT_TOKEN_ENVS


def _git_token_from_host_env():
    """Return a GitHub token from the host environment, or ``None``.

    A ``ROBOVAST_GIT_TOKEN=…`` line in the project ``.env`` is already part of that
    environment: the ``vast`` CLI loads ``./.env`` once before any command runs.
    """
    import os
    for var in _GIT_TOKEN_HOST_ENVS:
        val = os.environ.get(var, "").strip()
        if val:
            return val
    return None


def _git_secret_manifest(namespace, token):
    """A Secret holding the GitHub token for private-repo plugin installs."""
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": GIT_SECRET_NAME, "namespace": namespace,
                     "labels": {"app": SERVICE_NAME}},
        "type": "Opaque",
        "stringData": {GIT_SECRET_KEY: token},
    }


#: Secret holding the resolved share-provider credentials (bucket + inline key
#: JSON/PEM, URL, user, password, …) that the in-cluster service reads from its
#: own environment to stream finished campaigns to the configured share
#: (``--upload-to-share``). Sourced from the host env / ``.env`` at setup and
#: injected into the service pod via ``envFrom`` — the driver's provider reads
#: them straight from ``os.environ`` (see ``in_pod_upload.load_provider_from_env``).
SHARE_SECRET_NAME = "robovast-share-credentials"


def _share_env_from_host():
    """Resolve the configured share provider's pod env from the host, or ``None``.

    Reads ``ROBOVAST_SHARE_TYPE`` (and the provider's own vars) from the host
    environment / project ``.env`` — the same source ``vast serve`` uses locally
    — and asks the provider to materialise its **pod** environment via
    :meth:`~robovast.execution.share_providers.base.BaseShareProvider.build_pod_env`, which
    resolves host credential *files* (a GCS key file, an SFTP key file) into the
    inline values a pod can carry. ``ROBOVAST_SHARE_TYPE`` is included so the
    service picks the same provider back up.

    Returns ``None`` when no share is configured (``ROBOVAST_SHARE_TYPE`` unset).
    Raises :class:`click.UsageError` when a share type is set but unknown, or its
    required credentials are missing/unreadable — fail fast at setup, never
    silently mid-campaign.
    """
    import os

    from robovast.execution.share_providers import \
        load_share_provider_plugins  # pylint: disable=import-outside-toplevel

    share_type = os.environ.get("ROBOVAST_SHARE_TYPE", "").strip()
    if not share_type:
        return None

    providers = load_share_provider_plugins()
    if share_type not in providers:
        import click  # pylint: disable=import-outside-toplevel
        available = ", ".join(sorted(providers)) or "(none installed)"
        raise click.UsageError(
            f"ROBOVAST_SHARE_TYPE='{share_type}' has no registered provider. "
            f"Available: {available}.")

    provider = providers[share_type]()  # constructor validates required env vars
    return {"ROBOVAST_SHARE_TYPE": share_type, **provider.build_pod_env()}


#: Secret holding the ntfy.sh push-notification config (``ROBOVAST_NTFY_TOPIC`` and
#: optional ``ROBOVAST_NTFY_SERVER`` / ``ROBOVAST_NTFY_TOKEN``) the in-service driver's
#: :class:`~robovast.execution.notify.Notifier` reads from its own environment to push
#: per-campaign lifecycle notifications. Sourced from the host env / ``.env`` at setup
#: and injected into the service pod via ``envFrom`` — same shape as the share Secret.
NTFY_SECRET_NAME = "robovast-ntfy-credentials"


def _ntfy_env_from_host():
    """Resolve the ntfy notification env from the host, or ``None`` when disabled.

    Collects whichever of ``ROBOVAST_NTFY_TOPIC`` / ``ROBOVAST_NTFY_SERVER`` /
    ``ROBOVAST_NTFY_TOKEN`` are present in the host environment / project ``.env``
    (the same source ``vast serve`` uses locally). Notifications are **optional**, so
    — unlike :func:`_share_env_from_host` — this never raises: it returns ``None``
    when ``ROBOVAST_NTFY_TOPIC`` is unset, leaving the in-pod ``Notifier`` a no-op.
    """
    import os
    if not os.environ.get("ROBOVAST_NTFY_TOPIC", "").strip():
        return None
    out = {}
    for var in ("ROBOVAST_NTFY_TOPIC", "ROBOVAST_NTFY_SERVER", "ROBOVAST_NTFY_TOKEN"):
        val = os.environ.get(var, "").strip()
        if val:
            out[var] = val
    return out


#: Secret holding the registry *config* the in-cluster service reads from its own
#: env (``envFrom``) to build/ship agent-built experiment images: the registry
#: prefix, the names of the push/pull dockerconfigjson Secrets, and an optional
#: default base image. Read back by ``BaseConfig.get_registry_config()``.
REGISTRY_CONFIG_SECRET_NAME = "robovast-registry-config"

#: dockerconfigjson Secret holding credentials for an **external** registry, created
#: when ``ROBOVAST_REGISTRY_SERVER``/``_USERNAME``/``_PASSWORD`` are set at setup.
#:
#: Purely a *pull* credential now. Experiment images are built into the registry that
#: runs in this pod (:mod:`.registry_deploy`), which is open, so nothing needs a push
#: credential any more. What still needs one is a ``.vast`` naming an image in a private
#: registry — the campaign pods, the aux/exec pods and the service's own image all pull
#: through this Secret. The old name is kept so an existing deployment's Secret is
#: replaced rather than orphaned beside a new one.
REGISTRY_PUSH_SECRET_NAME = "robovast-registry-push"


def _registry_env(ingress_host=""):
    """The registry config the in-pod service reads back from its own env.

    Two unrelated registries meet here, and conflating them was the old bug:

    * the **build target** — the registry in this pod. Its prefix is just the service's
      own Ingress host (see :func:`registry_deploy.registry_prefix`), so it is derived,
      never configured; a site does not get to point builds somewhere the cluster cannot
      pull from. Without an Ingress there is no reachable registry and no prefix, which
      is the honest answer rather than a ref that fails at pull time.
    * the **pull credential** — for images a ``.vast`` names in someone else's private
      registry. Configured, because only the operator knows those credentials.

    Returns ``None`` when neither applies, which drops the Secret from the manifest set.
    """
    import os

    from . import registry_deploy  # pylint: disable=import-outside-toplevel

    env = {}
    prefix = registry_deploy.registry_prefix(ingress_host)
    if prefix:
        env["ROBOVAST_REGISTRY_PREFIX"] = prefix
    base = os.environ.get("ROBOVAST_BASE_EXPERIMENT_IMAGE", "").strip()
    if base:
        env["ROBOVAST_BASE_EXPERIMENT_IMAGE"] = base
    if (os.environ.get("ROBOVAST_REGISTRY_USERNAME", "").strip()
            and os.environ.get("ROBOVAST_REGISTRY_PASSWORD", "").strip()):
        env["ROBOVAST_REGISTRY_PULL_SECRET"] = REGISTRY_PUSH_SECRET_NAME
    # Carried through so a deployed (in-pod) service resolves an unresolvable host the
    # same way a local one does — read from the service's env when it builds a Job spec
    # (BaseConfig.get_host_aliases). Note this never affects an image *pull*, which the
    # node's runtime performs.
    aliases = os.environ.get("ROBOVAST_EXTRA_HOST_ALIASES", "").strip()
    if aliases:
        env["ROBOVAST_EXTRA_HOST_ALIASES"] = aliases
    return env or None


#: ConfigMap (key ``ca.pem``) holding the registry CA, created when
#: ``ROBOVAST_REGISTRY_CA_FILE`` is set at setup. Mounted into the build Job.
REGISTRY_CA_CONFIGMAP_NAME = "robovast-registry-ca"


def _registry_ca_manifest(namespace):
    """A ConfigMap holding the registry CA, or ``None`` when no CA file is set."""
    import os
    ca_path = os.environ.get("ROBOVAST_REGISTRY_CA_FILE", "").strip()
    if not ca_path:
        return None
    try:
        ca = open(ca_path, encoding="utf-8").read()
    except OSError as e:
        import click  # pylint: disable=import-outside-toplevel
        raise click.UsageError(
            f"ROBOVAST_REGISTRY_CA_FILE='{ca_path}' is unreadable: {e}") from e
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": REGISTRY_CA_CONFIGMAP_NAME, "namespace": namespace,
                     "labels": {"app": SERVICE_NAME}},
        "data": {"ca.pem": ca},
    }


def _registry_dockerconfig_manifest(namespace):
    """A ``kubernetes.io/dockerconfigjson`` Secret for registry push/pull, or ``None``.

    Created only when ``ROBOVAST_REGISTRY_SERVER`` + ``ROBOVAST_REGISTRY_USERNAME`` +
    ``ROBOVAST_REGISTRY_PASSWORD`` are set at setup (an existing external registry —
    the Phase-1 path). The credentials never leave the cluster and never cross the
    client interface.
    """
    import base64
    import json
    import os
    server = os.environ.get("ROBOVAST_REGISTRY_SERVER", "").strip()
    user = os.environ.get("ROBOVAST_REGISTRY_USERNAME", "").strip()
    password = os.environ.get("ROBOVAST_REGISTRY_PASSWORD", "").strip()
    if not (server and user and password):
        return None
    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
    dockercfg = {"auths": {server: {"username": user, "password": password,
                                    "auth": auth}}}
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": REGISTRY_PUSH_SECRET_NAME, "namespace": namespace,
                     "labels": {"app": SERVICE_NAME}},
        "type": "kubernetes.io/dockerconfigjson",
        "stringData": {".dockerconfigjson": json.dumps(dockercfg)},
    }


def _env_secret_manifest(namespace, name, env):
    """A Secret holding credentials the service reads from its own env (``envFrom``)."""
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": namespace,
                     "labels": {"app": SERVICE_NAME}},
        "type": "Opaque",
        "stringData": dict(env),
    }


#: Env-based credential sources injected into the service pod as Secrets pulled in via
#: ``envFrom`` (see :func:`_deployment_manifest`). Each is ``(secret_name, resolver)``
#: where the resolver returns the pod env dict, or ``None`` when that credential is not
#: configured. Adding another env-based credential is a one-line registration here — the
#: deploy, redeploy and teardown paths all iterate this list. (The git token is
#: deliberately NOT here: it is a read-only file mount, not env, so it is never inherited
#: by child processes.)
#:
#: Resolvers take ``ingress_host``. Only the registry one uses it — the build registry's
#: prefix *is* the service's published host — but they share a signature so the deploy
#: loop stays a loop rather than a special case per entry.
ENV_SECRET_SOURCES = (
    (SHARE_SECRET_NAME, lambda ingress_host="": _share_env_from_host()),
    (NTFY_SECRET_NAME, lambda ingress_host="": _ntfy_env_from_host()),
    (REGISTRY_CONFIG_SECRET_NAME, _registry_env),
)

#: The shared secret every client authenticates with. Deliberately **not** in
#: ``ENV_SECRET_SOURCES``: those are all replaced together on ``setup --force``, so
#: rotating the access token would also churn the git, share, ntfy and registry
#: credentials and reconcile RBAC — four unrelated changes to alter one password. Its own
#: Secret makes rotation a Secret update plus a rollout, and nothing else.
AUTH_SECRET_NAME = "robovast-auth"


def auth_secret_manifest(namespace, token):
    """The Secret holding the service's shared access token."""
    return _env_secret_manifest(namespace, AUTH_SECRET_NAME,
                                {"ROBOVAST_AUTH_TOKEN": token})


def deployed_registry_prefix(namespace="default", kube_context=None) -> str:
    """The registry prefix the deployed service actually *reads*, or ``""``.

    **Both halves are checked, because the failure that motivated this leaves one intact.**
    A ``setup`` re-run without ``--ingress-host`` made the registry env resolve to nothing,
    so the Secret was neither refreshed nor listed in the Deployment's ``envFrom`` -- but a
    Secret created by an earlier setup *stays in the namespace*. Reading only the Secret
    therefore reports a prefix the pod has no way to see, which is worse than reporting
    none: it says builds work when they cannot.

    So: the container must list the Secret in ``envFrom`` **and** the Secret must carry a
    non-empty prefix. ``""`` when either is missing, or when there is no Deployment.
    """
    import base64  # pylint: disable=import-outside-toplevel

    from kubernetes import client  # pylint: disable=import-outside-toplevel
    from kubernetes.client.rest import ApiException  # pylint: disable=import-outside-toplevel

    _load_kube_config(kube_context)
    try:
        dep = client.AppsV1Api().read_namespaced_deployment(SERVICE_NAME, namespace)
    except ApiException as exc:
        if exc.status == 404:
            return ""
        raise
    containers = (getattr(getattr(dep.spec, "template", None), "spec", None)
                  and dep.spec.template.spec.containers) or []
    listed = any(
        getattr(getattr(src, "secret_ref", None), "name", None) == REGISTRY_CONFIG_SECRET_NAME
        for c in containers for src in (getattr(c, "env_from", None) or []))
    if not listed:
        return ""
    try:
        secret = client.CoreV1Api().read_namespaced_secret(
            REGISTRY_CONFIG_SECRET_NAME, namespace)
    except ApiException as exc:
        if exc.status == 404:
            return ""
        raise
    encoded = (secret.data or {}).get("ROBOVAST_REGISTRY_PREFIX", "")
    return base64.b64decode(encoded).decode().strip() if encoded else ""


def existing_auth_token(namespace, kube_context=None):
    """The token already deployed in *namespace*, or ``""``.

    Setup reads it back rather than minting a new one on every run: re-running setup to
    change an unrelated setting must not silently log out all four users, so a token is
    generated once and then left alone unless ``--rotate-token`` asks for a new one.
    """
    import base64  # pylint: disable=import-outside-toplevel

    from kubernetes import client  # pylint: disable=import-outside-toplevel
    from kubernetes.client.rest import ApiException  # pylint: disable=import-outside-toplevel

    _load_kube_config(kube_context)
    try:
        secret = client.CoreV1Api().read_namespaced_secret(AUTH_SECRET_NAME, namespace)
    except ApiException as exc:
        if exc.status == 404:
            return ""
        raise
    encoded = (secret.data or {}).get("ROBOVAST_AUTH_TOKEN", "")
    return base64.b64decode(encoded).decode() if encoded else ""


def published_url(namespace="default", kube_context=None):
    """The URL the Ingress publishes, or ``""`` when the service is not published.

    Read back from the cluster rather than remembered from setup, so it stays right for
    an operator who did not run that setup -- which, with one operator per cluster and
    several clusters, is the normal case rather than the exception.
    """
    from kubernetes import client  # pylint: disable=import-outside-toplevel
    from kubernetes.client.rest import ApiException  # pylint: disable=import-outside-toplevel

    _load_kube_config(kube_context)
    try:
        ingress = client.NetworkingV1Api().read_namespaced_ingress(SERVICE_NAME, namespace)
    except ApiException as exc:
        if exc.status == 404:
            return ""
        raise
    rules = getattr(ingress.spec, "rules", None) or []
    host = getattr(rules[0], "host", "") if rules else ""
    if not host:
        return ""
    # A tls block naming this host is what makes the session cookie's Secure flag
    # usable, so it decides the scheme rather than an assumption about the port.
    tls = getattr(ingress.spec, "tls", None) or []
    secure = any(host in (getattr(entry, "hosts", None) or []) for entry in tls)
    return f"{'https' if secure else 'http'}://{host}"


def registry_ingress_defects(ingress) -> list:
    """Which parts of the registry's Ingress contract are missing. ``[]`` when intact.

    The route and the annotations are equally load-bearing and fail differently, so both
    are named: without the ``/v2`` path the node has no address to pull from, and without
    ``proxy-body-size`` every layer push dies on nginx's 1 MiB default with a 413 — an
    Ingress that has the route and not the annotation is still a broken registry, and a
    ``200`` from ``GET /v2/`` cannot see that.

    Split out so :func:`reconcile_registry_ingress_path` and ``vast doctor`` share one
    definition of "what a healthy registry Ingress looks like". They had no reason to
    disagree, and every reason to drift: one patches, the other reports, and a
    re-implemented comparison in the reporting half would describe a contract the
    patching half no longer enforces.

    Takes the Ingress object rather than a namespace: pure, so the reporting caller
    decides whether reading one is even possible.
    """
    from . import registry_deploy  # pylint: disable=import-outside-toplevel

    rules = getattr(getattr(ingress, "spec", None), "rules", None) or []
    if not rules:
        return ["the Ingress has no rules at all"]
    paths = getattr(getattr(rules[0], "http", None), "paths", None) or []
    defects = []
    if not any(getattr(p, "path", "") == registry_deploy.REGISTRY_INGRESS_PATH
               for p in paths):
        defects.append(f"no {registry_deploy.REGISTRY_INGRESS_PATH} route to the registry")
    have = getattr(getattr(ingress, "metadata", None), "annotations", None) or {}
    missing = sorted(k for k, v in registry_deploy.REGISTRY_INGRESS_ANNOTATIONS.items()
                     if have.get(k) != v)
    if missing:
        defects.append("missing annotations: " + ", ".join(missing))
    return defects


def reconcile_registry_ingress_path(namespace="default", kube_context=None):
    """Add the registry's ``/v2`` rule to an Ingress that predates it. Returns True if added.

    An ``upgrade`` deliberately does not rebuild the Ingress -- it has none of the TLS
    arguments that Ingress was created with, so recreating it would refuse. But a
    deployment upgraded across the version that introduced the in-pod registry would
    otherwise get the registry container and no route to it: a registry that exists,
    accepts pushes from inside the pod, and cannot be reached by the node that has to pull
    from it. Silently, because nothing about the pod looks wrong.

    So the path is reconciled in place, for the same reason RBAC is: a version needing
    something the previous one did not is a migration, and an upgrade that skips it turns
    into a runtime failure that reads like a bug.
    """
    from kubernetes import client  # pylint: disable=import-outside-toplevel
    from kubernetes.client.rest import ApiException  # pylint: disable=import-outside-toplevel

    from . import registry_deploy  # pylint: disable=import-outside-toplevel

    _load_kube_config(kube_context)
    networking = client.NetworkingV1Api()
    try:
        ingress = networking.read_namespaced_ingress(SERVICE_NAME, namespace)
    except ApiException as exc:
        if exc.status == 404:
            return False  # not published; nothing to route
        raise
    rules = getattr(ingress.spec, "rules", None) or []
    if not rules:
        return False
    paths = getattr(rules[0].http, "paths", None) or []
    have = getattr(ingress.metadata, "annotations", None) or {}
    missing_annotations = {k: v for k, v in registry_deploy.REGISTRY_INGRESS_ANNOTATIONS.items()
                           if have.get(k) != v}
    if not registry_ingress_defects(ingress):
        return False
    if missing_annotations:
        # Without proxy-body-size a push dies on nginx's 1m default with a 413, so an
        # Ingress that has the route but not the annotations is still a broken registry.
        networking.patch_namespaced_ingress(
            SERVICE_NAME, namespace, {"metadata": {"annotations": missing_annotations}})
    if any(getattr(p, "path", "") == registry_deploy.REGISTRY_INGRESS_PATH for p in paths):
        logger.info("Updated the %s Ingress annotations for registry uploads", SERVICE_NAME)
        return True

    # Rebuild the path list as plain dicts and put /v2 first: "/" is a Prefix rule that
    # matches everything, so the registry rule has to be the more specific one.
    existing = [{"path": p.path, "pathType": p.path_type,
                 "backend": {"service": {
                     "name": p.backend.service.name,
                     "port": {"number": p.backend.service.port.number}}}}
                for p in paths]
    patch = {"spec": {"rules": [{
        "host": rules[0].host,
        "http": {"paths": [registry_deploy.registry_ingress_path(), *existing]},
    }]}}
    networking.patch_namespaced_ingress(SERVICE_NAME, namespace, patch)
    logger.info("Added %s to the %s Ingress", registry_deploy.REGISTRY_INGRESS_PATH,
                SERVICE_NAME)
    return True


def published_host(namespace="default", kube_context=None):
    """The bare hostname the Ingress publishes, or ``""``.

    Read back from the cluster for the same reason as :func:`published_url`, and needed
    separately because it doubles as the container registry's prefix: an ``upgrade``
    knows nothing about the host it was originally set up with, and rebuilding the
    registry config without it would quietly leave the deployment unable to build.
    """
    url = published_url(namespace, kube_context)
    return url.split("://", 1)[-1] if url else ""


def _cluster_env(namespace, config_name, config_kwargs, kube_context=None):
    """Env that tells the in-cluster ClusterService how to reach the object store.

    The service (mode 3) reconstructs the same cluster config the controller
    uses, so ``create_campaign`` can stage inputs and controllers can pull them.

    ``kube_context`` records the context this service was deployed with, so the
    in-pod driver can resolve per-cluster resource lists (keyed by context name)
    — in-cluster there is no kubeconfig context to fall back on.
    """
    import json
    env = [{"name": "ROBOVAST_NAMESPACE", "value": namespace}]
    if config_name:
        env.append({"name": "ROBOVAST_CLUSTER_CONFIG_NAME", "value": config_name})
        env.append({"name": "ROBOVAST_CLUSTER_CONFIG_KWARGS",
                    "value": json.dumps(config_kwargs or {})})
    if kube_context:
        env.append({"name": "ROBOVAST_KUBE_CONTEXT", "value": kube_context})
    return env


#: ``(/etc/localtime, /etc/timezone)`` — where :func:`_host_timezone` reads the setup
#: host's zone from. A constant so a test can point it at a fixture tree instead of
#: needing the machine running the suite to be in some particular zone.
_TZ_PATHS = (pathlib.Path("/etc/localtime"), pathlib.Path("/etc/timezone"))


# A campaign id is minted from wall-clock time in whatever process names the campaign
# (``controller.campaign_id_for`` -> ``datetime.now()``, which is naive/process-local).
# For a cluster campaign that process is this pod, so with no zone set every campaign
# directory is named in UTC while the people reading those names are not. The zone of the
# host that ran setup is the best available stand-in for where those people are.
#
# This pod only: campaign Job pods get an env list built explicitly by
# ``KubernetesBackend`` and inherit nothing from here, so their logs stay UTC. Recorded
# times are unaffected either way -- ``store`` keeps epoch seconds and renders them UTC.
def _host_timezone():
    """IANA zone name of the host running setup, or ``""`` when it cannot be determined.

    ``/etc/localtime``'s symlink target first (any systemd distro, and macOS),
    ``/etc/timezone`` second (Debian/Ubuntu only, and the only one left when
    ``/etc/localtime`` is a copy rather than a link).

    Validated against the local tz database before it is written into a manifest, because
    a name the pod cannot resolve is not an error there: libc falls back to UTC, which
    would leave a configured-looking ``TZ`` producing exactly the UTC names it was meant
    to replace. An empty return is the honest version of that fallback. That check is also
    the sanitiser -- an absolute or non-normalised key is a ``ValueError`` -- so nothing
    odd on disk reaches the manifest.
    """
    import zoneinfo  # pylint: disable=import-outside-toplevel

    localtime, timezone_file = _TZ_PATHS
    candidates = []
    if localtime.is_symlink():
        parts = localtime.resolve().parts
        if "zoneinfo" in parts:
            candidates.append("/".join(parts[parts.index("zoneinfo") + 1:]))
    try:
        candidates.append(timezone_file.read_text(encoding="utf-8").strip())
    except OSError:
        pass

    for name in candidates:
        try:
            zoneinfo.ZoneInfo(name)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            continue  # not a zone this tz database knows, so not one the pod would either
        return name
    logger.warning("Could not determine this host's timezone; the service will name "
                   "campaigns in UTC. Set TZ in the service Deployment's env to override.")
    return ""


def service_manifests(namespace="default", image=None, env=None,
                      config_name=None, config_kwargs=None, git_token=None,
                      share_env=None, kube_context=None, pull_secret="",
                      auth_token="", ingress_host="", ingress_class="",
                      tls_secret="", issuer="", insecure_http=False,
                      registry_host="", registry_storage_path="",
                      workspaces_storage_path="", workspaces_storage_class="",
                      registry_storage_class="", registry_node=""):
    """Return all robovast-service manifests (RBAC [+ git/share Secrets] + Deployment + Service).

    *pull_secret* names the dockerconfigjson Secret for the service's own image; it is
    resolved by :func:`deploy_service`, which can see whether that Secret already exists
    in the namespace. Passing it in keeps this function pure.

    The ``registry_*`` arguments configure the in-pod container registry
    (:mod:`.registry_deploy`).

    *registry_host* is the published host the registry answers on — the build prefix.
    Separate from *ingress_host*, which additionally *creates* the Ingress and so refuses
    a combination without TLS. An ``upgrade`` has to supply the first without triggering
    the second: it knows the host only by reading it back from the live Ingress, and has
    none of the TLS arguments that Ingress was created with. Defaults to *ingress_host*,
    which is what setup passes.
    """
    registry_host = registry_host or ingress_host
    from robovast.common.execution import resolve_controller_image
    image = image or resolve_controller_image()
    if env is None:
        env = _cluster_env(namespace, config_name, config_kwargs, kube_context)
    # No ROBOVAST_CONTROLLER_IMAGE in the pod env, deliberately. It was carried in for the
    # postprocessing Job, whose initContainer used to copy robovast out of the controller
    # image -- but the conversion scripts come from a per-campaign ConfigMap built in the
    # driver's own process now (postprocess_job.scripts_configmap_manifest, precisely so
    # there is no controller-image version skew), and the conversion container runs the
    # *campaign's* recorded execution image. Nothing in the pod reads the variable, so
    # setting it there says something untrue about what this deployment uses.
    #
    # Every RoboVAST image except this one is resolved *in this pod* -- the scenario image
    # for a campaign, the simulator's, the sidecar for every init container, the build
    # base. So the project they resolve from has to be carried in, or an operator who
    # configured one gets it honoured everywhere except the place it is actually read.
    #
    # That was the old bug, and it was worse than it sounds: of the five per-image
    # variables that used to exist, only two were ever propagated, so `setup --force`
    # appeared to move the images and moved only the controller. One variable for the
    # whole family means there is one thing to carry rather than five to forget -- and
    # this is the *site default*: a campaign may override it on its own request
    # (CreateCampaignRequest.image_project), which is what makes a dev run need no deploy.
    # Carried UNCONDITIONALLY, empty value and all, and that is the point rather than an
    # oversight. The Deployment is applied with a strategic-merge patch, whose merge key for
    # `containers[].env` is the variable NAME -- so a variable the patch omits is not removed,
    # it is preserved. While these were emitted only when set, they were write-only: an
    # operator who set ROBOVAST_PROJECT_TAG once could never unset it again, because deleting
    # it from ./.env (or from their shell) simply left it out of the next patch and the old
    # value stayed in the pod. That cost an afternoon: a deployment kept resolving the family
    # at a tag nobody could find in any file, and every campaign's build failed pulling an
    # image at it.
    #
    # An empty value is safe because it is exactly what "unset" already means to every reader:
    # they all do `os.environ.get(var, "").strip() or <default>` (see execution.default_image_
    # project / default_image_tag), so "" resolves to the default rather than to an empty
    # image ref. Emitting it turns removal into a reset instead of a no-op.
    #
    # A caller-supplied `env` still wins: setup passes what it composed, and this must not
    # overwrite a value that was decided deliberately upstream.
    import os  # pylint: disable=import-outside-toplevel
    for var in ("ROBOVAST_PROJECT", "ROBOVAST_PROJECT_TAG"):
        if not any(e["name"] == var for e in env):
            env = [*env, {"name": var, "value": os.environ.get(var, "").strip()}]

    # The pod's timezone (see _host_timezone), carried unconditionally for the same reason
    # as the family env above: "" is UTC to libc -- what an unset TZ already means -- so an
    # empty value resets the pod to UTC instead of being a value the merge patch preserves.
    if not any(e["name"] == "TZ" for e in env):
        env = [*env, {"name": "TZ", "value": _host_timezone()}]

    extra = []
    if git_token is None:
        git_token = _git_token_from_host_env()
    have_git_secret = bool(git_token)
    if have_git_secret:
        # Provide the token to the service as a read-only mounted file (never an
        # env var — that would be inherited by every child process/command). The
        # service hands it to git only for the plugin-install subprocess.
        extra.append(_git_secret_manifest(namespace, git_token))

    # Env-based credential Secrets (share creds, ntfy config — see ENV_SECRET_SOURCES).
    # Each resolver reads the host env / .env; when configured it becomes a Secret the
    # service pulls in via envFrom. Unlike the git token these *are* env vars, because
    # the in-driver upload / notifier read them from os.environ. ``share_env`` overrides
    # that one source when given (used by tests / callers passing it explicitly).
    env_secret_names = []
    for name, resolver in ENV_SECRET_SOURCES:
        resolved = share_env if (name == SHARE_SECRET_NAME and share_env is not None) \
            else resolver(registry_host)
        if resolved:
            extra.append(_env_secret_manifest(namespace, name, resolved))
            env_secret_names.append(name)

    # The access token. Its own Secret rather than an ENV_SECRET_SOURCES entry (see
    # AUTH_SECRET_NAME), but still an envFrom source: the service reads it from
    # os.environ like any other. Passing it in is what lets setup preserve an already
    # deployed token instead of logging everyone out on an unrelated re-run.
    if auth_token:
        extra.append(auth_secret_manifest(namespace, auth_token))
        env_secret_names.append(AUTH_SECRET_NAME)

    # Registry push/pull credentials (dockerconfigjson) — created only when an
    # external registry's auth is configured at setup. Not an envFrom secret: it is
    # mounted into the build Job and referenced as an imagePullSecret by campaign
    # pods (both by name via the registry config env above).
    registry_secret = _registry_dockerconfig_manifest(namespace)
    if registry_secret:
        extra.append(registry_secret)
        # Created right here, so the Deployment can reference it without a lookup.
        pull_secret = pull_secret or REGISTRY_PUSH_SECRET_NAME
    registry_ca = _registry_ca_manifest(namespace)
    if registry_ca:
        extra.append(registry_ca)

    from . import registry_deploy  # pylint: disable=import-outside-toplevel
    registry_pvc = registry_deploy.registry_pvc_manifest(namespace, registry_storage_class)
    if registry_pvc:
        extra.append(registry_pvc)

    ingress = _ingress_manifest(namespace, ingress_host, ingress_class,
                                tls_secret, issuer, auth_token=auth_token,
                                insecure=insecure_http)
    return [
        *_service_rbac_manifests(namespace),
        *extra,
        _deployment_manifest(namespace, image, env=env, git_secret=have_git_secret,
                             env_secret_names=env_secret_names,
                             pull_secret=pull_secret,
                             registry_storage_path=registry_storage_path,
                             workspaces_storage_path=workspaces_storage_path,
                             workspaces_storage_class=workspaces_storage_class,
                             registry_storage_class=registry_storage_class,
                             registry_node=registry_node),
        _service_manifest(namespace, ingress_class),
        *([ingress] if ingress else []),
    ]


#: Credential objects this module owns: present when configured, and — the point of the
#: list — removed when the configuration that created them is taken away. Names only,
#: because the check is "did this deploy build one?", not "what is in it".
_OWNED_SECRETS = (REGISTRY_PUSH_SECRET_NAME, GIT_SECRET_NAME)
_OWNED_CONFIGMAPS = (REGISTRY_CA_CONFIGMAP_NAME,)


def _delete_unconfigured_credentials(core, namespace, secrets, configmaps, *,
                                     dry_run=False):
    """Delete an owned credential object this deploy did **not** build.

    Removing a variable from ``.env`` used to leave the Secret it had created in place,
    and — worse — ``deploy_service`` rediscovers the registry credential *by existence*,
    so it stayed wired to the Deployment as an imagePullSecret. An operator deleting a
    password to revoke access got a successful "upgraded" while the credential remained
    deployed and in use. Rotation worked; only removal was silently ignored.

    Not a general reconciler: it touches exactly the objects this module creates, so a
    Secret someone else put in the namespace is none of its business.
    """
    from kubernetes.client.rest import ApiException  # pylint: disable=import-outside-toplevel

    built = {m["metadata"]["name"] for m in secrets}
    built_cms = {m["metadata"]["name"] for m in configmaps}
    dr = "All" if dry_run else None
    for name in _OWNED_SECRETS:
        if name in built:
            continue
        try:
            core.delete_namespaced_secret(name, namespace, dry_run=dr)
            logger.info("Removed %s: its configuration is gone", name)
        except ApiException as exc:
            if exc.status != 404:
                raise
    for name in _OWNED_CONFIGMAPS:
        if name in built_cms:
            continue
        try:
            core.delete_namespaced_config_map(name, namespace, dry_run=dr)
            logger.info("Removed %s: its configuration is gone", name)
        except ApiException as exc:
            if exc.status != 404:
                raise


def deploy_service(namespace="default", kube_context=None, image=None, env=None,
                   config_name=None, config_kwargs=None, dry_run=False,
                   rotate_token=False, ingress_host="", ingress_class="",
                   tls_secret="", issuer="", insecure_http=False,
                   registry_host="", registry_storage_path="",
                   workspaces_storage_path="", workspaces_storage_class="",
                   registry_storage_class="", registry_node=""):
    """Create/update the robovast-service (idempotent). Returns the manifest list.

    ``dry_run=True`` performs a **server-side** dry run (validates against the
    real API server / admission, persists nothing) — useful to check the
    manifests without an image or scheduling.

    The access token is **preserved** across re-runs unless *rotate_token* is set:
    re-running setup to change something unrelated must not silently log out
    everyone who is using the service. A cluster that has none yet gets one minted
    here, so there is no deployment without authentication.
    """
    from kubernetes import client  # pylint: disable=import-outside-toplevel
    from kubernetes.client.rest import ApiException  # pylint: disable=import-outside-toplevel

    from .kube_client import load_kube_config  # pylint: disable=import-outside-toplevel

    load_kube_config(context=kube_context)
    core = client.CoreV1Api()
    rbac = client.RbacAuthorizationV1Api()
    apps = client.AppsV1Api()
    dr = "All" if dry_run else None

    # The service's own image may need registry auth. The Secret is usually NOT created
    # by this run: setup only writes it when ROBOVAST_REGISTRY_* are in the environment,
    # so on a re-run -- the normal case -- it is simply already there. Looking it up is
    # therefore the difference between a service that starts and one that sits in
    # ImagePullBackOff while setup prints "completed successfully". Same lookup the
    # campaign pods do (see KubernetesBackend.build_job_manifest).
    try:
        core.read_namespaced_secret(REGISTRY_PUSH_SECRET_NAME, namespace)
        pull_secret = REGISTRY_PUSH_SECRET_NAME
    except ApiException:
        pull_secret = ""

    # Reuse the deployed token unless asked for a new one; mint one the first time.
    from robovast.service.auth import generate_token  # pylint: disable=import-outside-toplevel
    auth_token = "" if rotate_token else existing_auth_token(namespace, kube_context)
    auth_token = auth_token or generate_token()

    manifests = service_manifests(
        namespace=namespace, image=image, env=env,
        config_name=config_name, config_kwargs=config_kwargs,
        kube_context=kube_context, pull_secret=pull_secret,
        auth_token=auth_token, ingress_host=ingress_host,
        ingress_class=ingress_class, tls_secret=tls_secret, issuer=issuer,
        insecure_http=insecure_http, registry_host=registry_host,
        registry_storage_path=registry_storage_path,
        workspaces_storage_path=workspaces_storage_path,
        workspaces_storage_class=workspaces_storage_class,
        registry_storage_class=registry_storage_class,
        registry_node=registry_node)
    by_kind = {m["kind"]: m for m in manifests}
    sa = by_kind["ServiceAccount"]
    role = by_kind["Role"]
    binding = by_kind["RoleBinding"]
    cluster_role = by_kind["ClusterRole"]
    cluster_binding = by_kind["ClusterRoleBinding"]
    deployment = by_kind["Deployment"]
    service = by_kind["Service"]
    # There may be several Secrets (git token, share/ntfy/registry creds) and
    # ConfigMaps (registry CA); by_kind collapses same-kind entries, so collect
    # these from the full list.
    secrets = [m for m in manifests if m["kind"] == "Secret"]
    configmaps = [m for m in manifests if m["kind"] == "ConfigMap"]
    _delete_unconfigured_credentials(core, namespace, secrets, configmaps, dry_run=dry_run)

    # ServiceAccount
    _create_or_ok(lambda: core.create_namespaced_service_account(namespace, sa, dry_run=dr))
    # Role (replace rules on conflict so verb changes apply)
    _create_or_replace(
        lambda: rbac.create_namespaced_role(namespace, role, dry_run=dr),
        lambda: rbac.patch_namespaced_role(role["metadata"]["name"], namespace,
                                           {"rules": role["rules"]}, dry_run=dr))
    # RoleBinding
    _create_or_ok(lambda: rbac.create_namespaced_role_binding(namespace, binding, dry_run=dr))
    # Cluster-scoped read for /usage (replace rules on conflict so grants stay in step)
    _create_or_replace(
        lambda: rbac.create_cluster_role(cluster_role, dry_run=dr),
        lambda: rbac.patch_cluster_role(cluster_role["metadata"]["name"],
                                        {"rules": cluster_role["rules"]}, dry_run=dr))
    _create_or_ok(lambda: rbac.create_cluster_role_binding(cluster_binding, dry_run=dr))
    # Git-token / share-credential Secrets (each present only when configured at
    # setup). Replace on conflict so rotated credentials take effect on re-setup.
    for secret in secrets:
        name = secret["metadata"]["name"]
        _create_or_replace(
            lambda s=secret: core.create_namespaced_secret(namespace, s, dry_run=dr),
            lambda s=secret, n=name: core.replace_namespaced_secret(
                n, namespace, s, dry_run=dr))
    # ConfigMaps (registry CA). Replace on conflict so a rotated CA takes effect.
    for cm in configmaps:
        name = cm["metadata"]["name"]
        _create_or_replace(
            lambda c=cm: core.create_namespaced_config_map(namespace, c, dry_run=dr),
            lambda c=cm, n=name: core.replace_namespaced_config_map(
                n, namespace, c, dry_run=dr))
    # Deployment (patch spec on conflict, so a `setup --force` over a live
    # service updates it in place instead of failing)
    _create_or_replace(
        lambda: apps.create_namespaced_deployment(namespace, deployment, dry_run=dr),
        lambda: apps.patch_namespaced_deployment(SERVICE_NAME, namespace, deployment, dry_run=dr))
    # Service. Patched on conflict, not tolerated: its spec stopped being stable when the
    # registry added a second port, and an untouched Service meant the Ingress' /v2 rule
    # pointed at a port the Service did not publish -- nginx answered 503 while the
    # registry itself was healthy inside the pod, which looks like a broken registry
    # rather than a missing port. Only `ports` is patched, because clusterIP is immutable
    # and a full replace would be rejected.
    _create_or_replace(
        lambda: core.create_namespaced_service(namespace, service, dry_run=dr),
        lambda: core.patch_namespaced_service(
            SERVICE_NAME, namespace, {"spec": {"ports": service["spec"]["ports"]}},
            dry_run=dr))

    # Ingress, when one was asked for. Replaced rather than tolerated: unlike the
    # Service, its spec is exactly what the operator is changing when they re-run
    # setup with a different host, class or issuer.
    ingress = by_kind.get("Ingress")
    if ingress is not None:
        networking = client.NetworkingV1Api()
        _create_or_replace(
            lambda: networking.create_namespaced_ingress(namespace, ingress, dry_run=dr),
            lambda: networking.replace_namespaced_ingress(
                ingress["metadata"]["name"], namespace, ingress, dry_run=dr))

    logger.info("Deployed robovast-service in namespace %s (dry_run=%s)", namespace, dry_run)
    return manifests


def _load_kube_config(kube_context=None):
    """Load kube config through the shared loader, with a CLI-friendly error.

    Goes through :func:`~robovast.execution.cluster_execution.kube_client.load_kube_config`
    rather than calling
    ``kubernetes.config`` directly — that is what installs the process-wide **connect
    timeout**. Calling the generated client's loader here instead left every API call on
    this path with ``timeout=None``, so an off-cluster driver against an unreachable
    cluster hung on a TCP connect for minutes and then died in a urllib3 traceback,
    rather than failing in seconds saying which cluster it could not reach.

    The wrapping remains: the underlying loaders raise low-level exceptions
    (``ConfigException``/``FileNotFoundError``/``RuntimeError``) when ``KUBECONFIG`` is
    unset, points at a missing file, or names a context that is not there, and the CLI
    should print one actionable line instead of a stack.
    """
    import click  # pylint: disable=import-outside-toplevel
    from kubernetes.config.config_exception import \
        ConfigException  # pylint: disable=import-outside-toplevel

    from .kube_client import load_kube_config  # pylint: disable=import-outside-toplevel

    try:
        load_kube_config(context=kube_context)
    except (ConfigException, FileNotFoundError, RuntimeError) as exc:
        hint = (f" (does context {kube_context!r} exist?)"
                if kube_context else " (is KUBECONFIG set?)")
        raise click.ClickException(
            f"could not load Kubernetes config{hint}: {exc}") from exc


def read_service_config_from_cluster(namespace="default", kube_context=None):
    """Read ``(config_name, config_kwargs)`` from the deployed service's env.

    Setup writes the cluster config the service reconstructs into the Deployment's
    env (:func:`_cluster_env`), so **the cluster is the authoritative source** — no
    local flag file needed. This is what lets ``vast serve --backend cluster -x
    <ctx>`` and the cluster maintenance commands work from any host with kubeconfig
    access, including one that never ran ``setup``. Returns ``(None, {})`` when the
    Deployment (or the config env) is absent.
    """
    import json  # pylint: disable=import-outside-toplevel

    import click  # pylint: disable=import-outside-toplevel
    from kubernetes import client  # pylint: disable=import-outside-toplevel
    from kubernetes.client.rest import ApiException  # pylint: disable=import-outside-toplevel
    from urllib3.exceptions import HTTPError  # pylint: disable=import-outside-toplevel

    from .kube_client import CONNECT_TIMEOUT_SECONDS  # pylint: disable=import-outside-toplevel
    _load_kube_config(kube_context)
    # One attempt, explicitly bounded. The process-wide policy sets a connect timeout per
    # *attempt*, and urllib3 retries a failed connect three more times — so an
    # unreachable cluster took 4 x the limit to report, which is not what a caller told
    # "10 seconds" expects. This read is a startup preflight: if the cluster does not
    # answer the first time, waiting for three more identical failures tells nobody
    # anything. Campaign monitoring keeps the retries; it wants them.
    preflight = client.Configuration.get_default_copy()
    preflight.retries = 0
    apps = client.AppsV1Api(client.ApiClient(configuration=preflight))
    try:
        dep = apps.read_namespaced_deployment(
            SERVICE_NAME, namespace,
            _request_timeout=(CONNECT_TIMEOUT_SECONDS, CONNECT_TIMEOUT_SECONDS))
    except ApiException as e:
        if e.status == 404:
            return None, {}
        raise
    except HTTPError as exc:
        # An unreachable cluster is **not** "no service deployed": returning (None, {})
        # here would send the caller off to suggest `vast exec cluster setup` for a
        # cluster it cannot even connect to. Say which cluster, and that it did not
        # answer — one line, not a urllib3 stack.
        host = ""
        try:
            host = client.Configuration.get_default_copy().host or ""
        except Exception:  # noqa: BLE001 - only used to enrich the message
            pass
        where = f" at {host}" if host else ""
        for_ctx = f" (context {kube_context!r})" if kube_context else ""
        raise click.ClickException(
            f"the Kubernetes cluster{where}{for_ctx} did not answer within "
            f"{CONNECT_TIMEOUT_SECONDS:g}s — check the cluster is up and reachable "
            "(kubectl get nodes), or start a local-only service with "
            "'vast serve --backend local'. Raise the limit with "
            "ROBOVAST_KUBE_CONNECT_TIMEOUT=<seconds> if the cluster is simply slow."
        ) from exc
    containers = dep.spec.template.spec.containers or []
    env = {e.name: e.value for c in containers for e in (c.env or [])}
    name = env.get("ROBOVAST_CLUSTER_CONFIG_NAME")
    raw = env.get("ROBOVAST_CLUSTER_CONFIG_KWARGS")
    return name, (json.loads(raw) if raw else {})


def delete_service(namespace="default", kube_context=None):
    """Remove the robovast-service Deployment + Service + RBAC (best-effort).

    Never touches the object store (the durable data home), so the
    ``cluster cleanup`` → ``cluster setup`` cycle that updates the service
    keeps the campaign data it left behind.
    """
    from kubernetes import client  # pylint: disable=import-outside-toplevel
    from kubernetes.client.rest import ApiException  # pylint: disable=import-outside-toplevel

    from .kube_client import load_kube_config  # pylint: disable=import-outside-toplevel

    try:
        load_kube_config(context=kube_context)
    except Exception as exc:  # pragma: no cover - best-effort cleanup
        logger.warning("Failed to load kube config for service cleanup: %s", exc)
        return
    core = client.CoreV1Api()
    rbac = client.RbacAuthorizationV1Api()
    apps = client.AppsV1Api()
    cluster_role_name = f"{SERVICE_ACCOUNT}-usage-{namespace}"
    deletions = [
        ("Service", lambda: core.delete_namespaced_service(SERVICE_NAME, namespace)),
        ("Deployment", lambda: apps.delete_namespaced_deployment(SERVICE_NAME, namespace)),
        *[(f"Secret ({name})",
           lambda n=name: core.delete_namespaced_secret(n, namespace))
          for name, _ in ENV_SECRET_SOURCES],
        ("Secret (git)", lambda: core.delete_namespaced_secret(GIT_SECRET_NAME, namespace)),
        ("ClusterRoleBinding", lambda: rbac.delete_cluster_role_binding(cluster_role_name)),
        ("ClusterRole", lambda: rbac.delete_cluster_role(cluster_role_name)),
        ("RoleBinding", lambda: rbac.delete_namespaced_role_binding(SERVICE_ACCOUNT, namespace)),
        ("Role", lambda: rbac.delete_namespaced_role(SERVICE_ACCOUNT, namespace)),
        ("ServiceAccount", lambda: core.delete_namespaced_service_account(SERVICE_ACCOUNT, namespace)),
    ]
    for kind, call in deletions:
        try:
            call()
        except ApiException as exc:
            if exc.status != 404:
                logger.warning("Failed to delete service %s: %s", kind, exc)
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed to delete service %s: %s", kind, exc)


def _create_or_ok(create):
    """Run *create*, tolerating an already-exists (409)."""
    from kubernetes.client.rest import ApiException  # pylint: disable=import-outside-toplevel
    try:
        create()
    except ApiException as exc:
        if exc.status != 409:
            raise


def _create_or_replace(create, patch):
    """Run *create*; on 409 run *patch* so spec/rule changes take effect."""
    from kubernetes.client.rest import ApiException  # pylint: disable=import-outside-toplevel
    try:
        create()
    except ApiException as exc:
        if exc.status != 409:
            raise
        patch()
