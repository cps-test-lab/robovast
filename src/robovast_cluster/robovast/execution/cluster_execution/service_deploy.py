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

"""Deploy the persistent ``robovast-service`` into a cluster (the cluster mode).

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
# Re-exported rather than spelled again: the env var is the service's contract with
# common.index_db, and two spellings of it would drift into a service that cannot find its
# own index while both halves look correct.
from robovast.common.index_db import DSN_ENV as INDEX_DSN_ENV
from robovast.service.interface import DEFAULT_PORT

from .cluster_execution import BLOCKED_GRACE_SECONDS

logger = logging.getLogger(__name__)

SERVICE_NAME = "robovast-service"
SERVICE_ACCOUNT = "robovast-service"
#: Re-exported under the deployment's own name; the value lives with the
#: address space in ``service/interface.py``.
SERVICE_PORT = DEFAULT_PORT

#: How long the service may take to bind its port before Kubernetes calls it dead, as
#: ``(periodSeconds, failureThreshold)`` for the startupProbe.
#:
#: Large on purpose, and not a value to tidy down. ``ClusterService.__init__`` resumes every
#: interrupted campaign *before* ``vast serve`` binds the port (see
#: ``ClusterService.resume_interrupted_campaigns``), so a service that comes up owing work does
#: not answer ``/healthz`` until that resume returns. Under the liveness probe alone -- 15 s of
#: grace, then three 20 s strikes -- a restart with live campaigns was SIGKILLed at ~75 s, every
#: time, forever: each attempt was killed mid-restore and the next one started over.
#:
#: A startupProbe is the mechanism that fits: Kubernetes suspends BOTH liveness and readiness
#: until it passes, so a slow start stops reading as a hung one without weakening the
#: steady-state checks by a single second. The remaining wait is a prefix listing, which scales
#: with the campaign's object count even when no bytes move.
STARTUP_PROBE_PERIOD_SECONDS = 10
STARTUP_PROBE_FAILURE_THRESHOLD = 180  # 30 minutes

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
#: user had pushed, gone, while the upgrade reports success.
#:
#: The campaign results root needed the same treatment and did not get it for longer, because
#: it was believed to be a cache of the object store. It is not, for a campaign the service is
#: *driving* — see :data:`RESULTS_VOLUME_NAME`.
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

#: Where the service keeps the working root of the campaigns it drives, and the volume
#: backing it.
#:
#: Not a cache, and expensive to treat as one. A cluster campaign's
#: *durable* home is the object store, but the one being driven has a local tree all the
#: same: each batch downloads its own results into it, per-run extraction reads it through a
#: path (``search.extractor.Extractor.extract``), and postprocessing reads it to derive
#: the campaign's results. Unmounted it landed on the container's writable layer — as ``/var/lib/results``,
#: the *sibling* of the workspaces mount, one directory outside what was covered — so every
#: restart discarded it. Combined with a resume that rebuilds it before the port is bound,
#: that was a restart loop no ``startupProbe`` could have saved: each attempt was killed
#: mid-restore and the next began again from an empty directory.
#:
#: Named on the command line via ``vast serve --results-dir`` rather than left to
#: ``local_results_root``'s ``<workspaces_root>/../results``, for the reason
#: :data:`WORKSPACES_ROOT_ENV` gives: a manifest must name the path it mounts, or a later
#: change to the workspaces mount silently un-mounts this one again.
RESULTS_VOLUME_NAME = "results-data"
RESULTS_DATA_DIR = "/var/lib/robovast-results"
DEFAULT_RESULTS_HOST_PATH = "/var/lib/robovast-results"

#: What the scheduler reserves for the service container, and what it may grow to.
#:
#: A request rather than nothing, because a container with no request is scheduled as if it
#: needed nothing -- and this one drives every campaign on the cluster. Kept to a floor
#: (0.1 core, 512Mi) because it is subtracted from the capacity campaign jobs are admitted
#: against; it is a chosen floor, not a measurement, and what would refine it is reading the
#: pod's own usage across a full sweep.
#:
#: **No limits at all, deliberately.** The campaign controller and the in-memory admission
#: queue live in this process: an OOMKill here does not degrade a campaign, it drops the
#: only thing tracking one, and a CPU cap throttles the loop that admits and monitors every
#: job on the cluster. Neither failure would be attributed to a cgroup by anyone reading the
#: campaign log.
SERVICE_RESOURCES = {"requests": {"cpu": "100m", "memory": "512Mi"}}

#: The origin clients reach this service on, which the service reports as
#: ``VersionInfo.web_base`` so a caller that cannot be handed bytes can be handed a link.
#: Derived from the Ingress at setup/upgrade time, exactly like the build registry's prefix
#: (:func:`registry_deploy.registry_prefix`) and for the same reason: the in-pod service is
#: deliberately given no RBAC to read its own Ingress. Empty without an Ingress -- an
#: unpublished service has no origin to declare, and a link nobody can open is worse than
#: no link. Read back by ``LocalTransport._declared_web_base``, which also takes it from
#: ``serve`` for a service started by hand -- one input either way.
PUBLIC_URL_ENV = "ROBOVAST_PUBLIC_URL"


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


def _results_host_path(workspaces_storage_path=""):
    """Where the results hostPath goes: literally beside the workspaces one.

    A deployer who moved the workspaces store to another disk meant to move the service's
    node-local data, and results is the larger half of it. Pinning results to
    :data:`DEFAULT_RESULTS_HOST_PATH` regardless would quietly leave it on the node's root
    filesystem — the disk they were moving off.
    """
    if not workspaces_storage_path:
        return DEFAULT_RESULTS_HOST_PATH
    return str(pathlib.PurePosixPath(workspaces_storage_path).parent / "robovast-results")


def results_volume(storage_path="", storage_class=""):
    """The volume backing the campaign results root.

    Deliberately takes no configuration of its own: the caller passes the *workspaces*
    volume's backing, so results is a PVC exactly where workspaces is one and a hostPath
    beside it otherwise. Three reasons that is right rather than merely smaller.

    It stays portable with no new flags: a stock RKE2 cluster ships no StorageClass and gets
    a hostPath (a PVC there would sit Pending forever); a cluster that can provision gets a
    dynamically-provisioned claim.

    It keeps :func:`_resolve_data_node` correct for free. That function pins the pod to a node
    whenever a volume it carries is a hostPath; because this volume's backing is a function of
    one it already tests, results cannot become node-local behind its back and be abandoned by
    a pod that was free to move.

    And it commits no public API to an arrangement that may not last. The driver mirrors a
    campaign whose durable home is the object store -- it downloads Job output it did not
    produce and re-uploads it whole (``KubernetesBackend.finalize_campaign``) -- and what keeps
    that necessary is only that per-run extraction runs driver-side against a local path
    (``search.extractor.Extractor.extract``). Move extraction into a Job, as rosbag conversion
    already is, and this volume stops being needed. Give it its own ``--results-storage-*``
    flags when someone actually needs to split it from the workspaces store, not before.
    """
    if storage_class:
        return {"name": RESULTS_VOLUME_NAME,
                "persistentVolumeClaim": {"claimName": RESULTS_VOLUME_NAME}}
    return {"name": RESULTS_VOLUME_NAME,
            "hostPath": {"path": storage_path or DEFAULT_RESULTS_HOST_PATH,
                         "type": "DirectoryOrCreate"}}


def results_pvc_manifest(namespace, storage_class, size="500Gi"):
    """The PVC for :func:`results_volume`, or ``None`` when backed by hostPath.

    An order of magnitude larger than the workspaces claim's default, because this is the
    largest store the service keeps: one measured campaign held 4.0 GB of run artifacts
    against 0.4 MB of anything the driver itself produced, and a service drives many at once.
    """
    if not storage_class:
        return None
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": RESULTS_VOLUME_NAME, "namespace": namespace,
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

    Plus a cluster-scoped read-only ClusterRole (nodes + pods + node metrics) backing the
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
            # The service drives campaigns in-process (there is no controller pod), so it
            # needs everything such a pod's ServiceAccount would hold.
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
                # The service rolls ITSELF, from the web UI's Admin page and from
                # 'vast service restart': it stamps its own Deployment's restart
                # annotation, which with imagePullPolicy: Always lands the new pod on the
                # newest bytes at the resolved tag. Scoped by resourceNames to this one
                # object -- resourceNames restricts exactly the named-object verbs, which
                # is all that is asked for, so the grant cannot reach another Deployment.
                #
                # `get` is not decoration: it is how the page reads the image ref to ask
                # the registry whether anything newer exists, and a 403 on it is how a
                # deployment predating this grant tells the page to run
                # 'vast service upgrade --no-restart' -- which reconciles RBAC
                # without rolling, and is therefore available mid-campaign.
                #
                # Deliberately NOT create/delete, and deliberately not the `apps` group at
                # large: this permits one thing, restarting itself. Anything wider would
                # let a web-reachable service rewrite its own spec.
                {"apiGroups": ["apps"], "resources": ["deployments"],
                 "verbs": ["get", "patch"], "resourceNames": [SERVICE_NAME]},
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
        # Cluster-scoped grants: nodes for the /usage endpoint (cluster resources, not
        # grantable via a namespaced Role) with cluster-wide pod requests for the true
        # "used" figure across tenants. The ClusterRole name is namespaced so parallel
        # robovast deployments don't collide.
        #
        # Entirely read-only. Admission is the in-process
        # controller's (node_admission.py), so the service writes nothing
        # cluster-scoped. A deployment older than that removal still tries the create and
        # gets a 403 here -- upgrade it rather than restoring the grant.
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
                # And metrics-server, for the MEASURED cpu/memory beside the request sum --
                # one list for the whole node set per usage window. Optional in effect: a
                # cluster not serving metrics.k8s.io, or a deployment whose RBAC predates
                # this rule, reports why it cannot measure and still answers everything
                # else, so /usage never depends on an add-on being installed.
                {"apiGroups": ["metrics.k8s.io"], "resources": ["nodes"],
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
                         workspaces_storage_path="", workspaces_storage_class="",
                         node_selector=None):
    """The robovast-service Deployment (1 replica).

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

    **One container.** The registry and the campaign index used to ride along here and now
    live in the ``robovast`` pod (:mod:`.store_pod`): they are cluster-lifetime
    infrastructure, and this Deployment is rolled by every upgrade. What is left in this
    pod is the service and the two volumes only the service uses.
    """
    if restarted_at is None:
        restarted_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    container = {
        "name": SERVICE_NAME,
        "image": image,
        "imagePullPolicy": "Always",
        # --results-dir names the mount below rather than letting local_results_root derive
        # <workspaces_root>/../results, which resolved one directory OUTSIDE the only mount
        # this pod had. See RESULTS_VOLUME_NAME.
        "command": ["vast", "serve",
                    "--host", "0.0.0.0", "--port", str(SERVICE_PORT),
                    "--results-dir", RESULTS_DATA_DIR],
        "ports": [{"containerPort": SERVICE_PORT, "name": "http"}],
        "env": list(env or []),
        "resources": SERVICE_RESOURCES,
        "readinessProbe": {
            "httpGet": {"path": "/healthz", "port": SERVICE_PORT},
            "initialDelaySeconds": 5, "periodSeconds": 10},
        "livenessProbe": {
            "httpGet": {"path": "/healthz", "port": SERVICE_PORT},
            "initialDelaySeconds": 15, "periodSeconds": 20},
        # Holds the two probes above off until the service actually answers; see
        # STARTUP_PROBE_PERIOD_SECONDS for why the budget is measured in minutes.
        "startupProbe": {
            "httpGet": {"path": "/healthz", "port": SERVICE_PORT},
            "initialDelaySeconds": 5,
            "periodSeconds": STARTUP_PROBE_PERIOD_SECONDS,
            "failureThreshold": STARTUP_PROBE_FAILURE_THRESHOLD},
    }
    if env_secret_names:
        container["envFrom"] = [{"secretRef": {"name": n}} for n in env_secret_names]
    # Point the store at the mount, unless the caller already set it explicitly.
    if not any(e.get("name") == WORKSPACES_ROOT_ENV for e in container["env"]):
        container["env"].append({"name": WORKSPACES_ROOT_ENV,
                                 "value": WORKSPACES_DATA_DIR})
    container["volumeMounts"] = [{"name": WORKSPACES_VOLUME_NAME,
                                  "mountPath": WORKSPACES_DATA_DIR},
                                 {"name": RESULTS_VOLUME_NAME,
                                  "mountPath": RESULTS_DATA_DIR}]
    pod_spec = {
        "serviceAccountName": SERVICE_ACCOUNT,
        "containers": [container],
        "volumes": [
            workspaces_volume(workspaces_storage_path, workspaces_storage_class),
            # Backed like the workspaces store, deliberately -- see results_volume().
            results_volume(_results_host_path(workspaces_storage_path),
                           workspaces_storage_class)],
    }
    if node_selector:
        pod_spec["nodeSelector"] = dict(node_selector)
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
            # Load-bearing for admission correctness, not just a sizing choice. The job
            # admission queue lives in this process's memory (`node_admission`), so a second
            # replica would be a second queue spending the same free capacity against the same
            # cluster -- and the failure is silent: over-admission and pods that cannot be
            # placed, never an error. RoboVAST is the sole scheduler of its own work by design;
            # making the queue cluster-wide state is what a second replica would first require.
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
            # One port. The registry's `/v2` used to be the second one here; it now
            # answers on the store pod's Service (:mod:`.store_pod`), which is what the
            # Ingress rule points at. The published hostname and every image ref built
            # from it are unchanged -- one Ingress may front two Services.
            "ports": [
                {"port": SERVICE_PORT, "targetPort": SERVICE_PORT, "name": "http"},
            ],
        },
    }


def wait_for_service_ready(namespace="default", kube_context=None, timeout_s=180.0):
    """Block until the service Deployment has a Ready replica, or say why it has not.

    Returning the moment the Deployment is *created* and printing "✓ Cluster setup
    completed successfully!" surfaces an image that cannot be pulled one command later,
    as a connection failure — pointing at the network rather than at the
    ImagePullBackOff that actually happened. The pod's own reason is right there;
    reporting it here is the difference between a five-second fix and a debugging
    session.

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
                "Deploy it with 'vast cluster setup <flavor>'.") from exc
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
    right after it can therefore be looking at the generation being replaced -- which makes
    ``upgrade`` report "image unchanged" across a genuine image change, having read the
    outgoing pod both times.

    Convergence is the Deployment's own account of it: the controller has observed this
    spec (``observedGeneration``), every replica is on the new template
    (``updatedReplicas``), and none of the old ones are left (``replicas``).

    Those counters alone, however, cannot fail. Watching nothing else and returning False
    on timeout turns an incoming pod in ``ImagePullBackOff`` -- a reason the kubelet already
    has -- into three minutes of silence followed by a caller that prints "✓ upgraded and
    ready" anyway. So the incoming pod is watched too, and this raises rather than returning
    a verdict a caller has to remember to check.

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


def deployment_image_ref(namespace="default", kube_context=None,
                         container=SERVICE_NAME) -> "tuple[str, bool]":
    """``(image_ref, permission_denied)`` for our Deployment's service container.

    The ref the Deployment *asks for* -- which is what a registry can be asked about --
    as opposed to :func:`running_image_digest`, which is what arrived.

    The 403 comes back as a flag rather than being swallowed into ``""`` because "I may
    not look" and "there is nothing newer" are opposite answers with opposite advice: the
    first is a missed RBAC migration with a one-command fix, the second is a service that
    is up to date. A deployment set up before the ``apps/deployments`` grant above hits
    exactly the first, so it is the case a caller most needs told apart.
    """
    from kubernetes import client  # pylint: disable=import-outside-toplevel

    try:
        _load_kube_config(kube_context)
        dep = client.AppsV1Api().read_namespaced_deployment(SERVICE_NAME, namespace)
    except Exception as e:  # pylint: disable=broad-except
        return "", getattr(e, "status", None) == 403
    for c in (dep.spec.template.spec.containers or []):
        if c.name == container:
            return c.image or "", False
    return "", False


def patch_restart_annotation(namespace="default", kube_context=None) -> str:
    """Stamp :data:`RESTART_ANNOTATION` with now, and return what was stamped.

    This is the whole of the in-cluster roll. Kubernetes rolls a Deployment when its pod
    template changes, and with ``imagePullPolicy: Always`` the new pod pulls the tag
    afresh -- so one changed annotation is what moves a floating tag onto new bytes.

    Deliberately **not** :func:`deploy_service`, even though that is what
    ``vast service upgrade`` calls. That re-renders the entire manifest set from the
    caller's environment, and the caller here is the pod itself -- whose environment is
    whatever was baked in at the last setup. Re-rendering from it would look like an
    upgrade and would quietly revert anything an operator changed out of band since, and
    it could not rebuild the credential Secrets at all (those come from the operator's
    ``.env``, which is not in here). So this rolls, and says that it only rolls; the CLI
    command remains the one that reconciles.
    """
    from kubernetes import client  # pylint: disable=import-outside-toplevel

    _load_kube_config(kube_context)
    stamped = datetime.datetime.now(datetime.timezone.utc).isoformat()
    client.AppsV1Api().patch_namespaced_deployment(
        SERVICE_NAME, namespace,
        {"spec": {"template": {"metadata": {
            "annotations": {RESTART_ANNOTATION: stamped}}}}})
    return stamped


class IngressRefused(RuntimeError):
    """An Ingress was asked for in a configuration that would publish an open service."""

    #: A caller error with a self-contained message; a traceback would only obscure it.
    include_traceback = False


def validate_ingress_options(ingress_host="", tls_secret="", issuer="",
                             insecure_http=False, have_token=True):
    """Refuse an unpublishable combination **before** anything is changed.

    A pure argument check, so it belongs at the very start of setup. Inside
    :func:`_ingress_manifest` instead -- which runs after the cluster has been changed and
    the cluster's storage deployed -- an operator who forgot ``--issuer`` discovers it only
    once the cluster has already been modified. The check costs nothing; doing it late
    costs a half-finished setup.

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


def public_url(ingress_host="", insecure_http=False) -> str:
    """The origin this service is published on, or ``""`` when it is not published.

    The scheme is not a guess: :func:`validate_ingress_options` refuses to create an
    Ingress over plain HTTP unless ``--insecure-http`` says so explicitly, so these two
    arguments already decide it.
    """
    if not ingress_host:
        return ""
    return f"{'http' if insecure_http else 'https'}://{ingress_host}"


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

    from robovast.execution.share_providers import (  # pylint: disable=import-outside-toplevel
        load_share_provider_plugins, unavailable_share_type_message)

    share_type = os.environ.get("ROBOVAST_SHARE_TYPE", "").strip()
    if not share_type:
        return None

    providers = load_share_provider_plugins()
    if share_type not in providers:
        import click  # pylint: disable=import-outside-toplevel
        raise click.UsageError(unavailable_share_type_message(share_type, providers))

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


def existing_index_password(namespace, kube_context=None):
    """The index password already deployed in *namespace*, or ``""``.

    Read back rather than regenerated, and the reason is harsher than the auth token's. A
    Postgres ``POSTGRES_PASSWORD`` is applied by ``initdb`` and then ignored: on any restart
    with an existing data directory the role keeps whatever password it was created with. So
    minting a new one on upgrade would leave a Secret and a database that disagree -- the
    deploy would report success, the sidecar would come up healthy, and every query would
    fail authentication against data that is perfectly intact.

    Worse, it would look like a first-deploy success: a fresh volume initialises with
    whatever password it is handed, so the fault appears only on the *second* deploy, which
    is where nobody is looking for it.

    Two pods read this one Secret now: the store pod's Postgres takes it as
    ``POSTGRES_PASSWORD``, this service takes it inside its DSN. That makes never rotating
    it load-bearing twice over -- the two are deployed by different commands at different
    times, so a rotation would not even be simultaneous.
    """
    import base64  # pylint: disable=import-outside-toplevel

    from kubernetes import client  # pylint: disable=import-outside-toplevel
    from kubernetes.client.rest import ApiException  # pylint: disable=import-outside-toplevel

    from . import index_deploy  # pylint: disable=import-outside-toplevel

    _load_kube_config(kube_context)
    try:
        secret = client.CoreV1Api().read_namespaced_secret(
            index_deploy.INDEX_SECRET_NAME, namespace)
    except ApiException as exc:
        if exc.status == 404:
            return ""
        raise
    encoded = (secret.data or {}).get(index_deploy.INDEX_PASSWORD_KEY, "")
    return base64.b64decode(encoded).decode() if encoded else ""


def ensure_index_secret(namespace="default", kube_context=None):
    """Create the index password Secret if it is not there yet; return the password.

    Called by ``cluster setup`` **before** the store pod is applied, because that pod's
    Postgres container references this Secret and a pod created without it sits in
    CreateContainerConfigError. ``deploy_service`` then reads the very same value back
    (:func:`existing_index_password`) instead of minting a second one.

    Never overwrites an existing Secret, for the reason
    :func:`existing_index_password` gives: ``initdb`` applied the first password to a data
    directory that still exists, and a new one would leave the Secret and the database
    disagreeing while every surface reported success.
    """
    from kubernetes import client  # pylint: disable=import-outside-toplevel
    from kubernetes.client.rest import ApiException  # pylint: disable=import-outside-toplevel

    from robovast.service.auth import generate_token  # pylint: disable=import-outside-toplevel

    from . import index_deploy  # pylint: disable=import-outside-toplevel

    existing = existing_index_password(namespace, kube_context)
    if existing:
        return existing
    password = generate_token()
    core = client.CoreV1Api()
    try:
        core.create_namespaced_secret(
            namespace, index_deploy.index_secret_manifest(namespace, password))
    except ApiException as exc:
        if exc.status != 409:
            raise
        # Raced with another deploy; theirs is the one initdb will see.
        return existing_index_password(namespace, kube_context)
    return password


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
    ``proxy-body-size`` every layer push dies on nginx's 1 MIB default with a 413 — an
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
    from . import registry_deploy, store_pod  # pylint: disable=import-outside-toplevel

    rules = getattr(getattr(ingress, "spec", None), "rules", None) or []
    if not rules:
        return ["the Ingress has no rules at all"]
    paths = getattr(getattr(rules[0], "http", None), "paths", None) or []
    defects = []
    route = next((p for p in paths
                  if getattr(p, "path", "") == registry_deploy.REGISTRY_INGRESS_PATH), None)
    if route is None:
        defects.append(f"no {registry_deploy.REGISTRY_INGRESS_PATH} route to the registry")
    elif getattr(getattr(getattr(route, "backend", None), "service", None),
                 "name", "") != store_pod.STORE_SERVICE_NAME:
        # A deployment published before the registry moved out of the service pod routes
        # /v2 at the service's own Service, which now has no registry port at all. Every
        # push and every pull of a newly built image 404s against the UI, and nothing about
        # the pods looks wrong -- so this is a defect to repair, not a variant to tolerate.
        defects.append(
            f"{registry_deploy.REGISTRY_INGRESS_PATH} still routes to the service pod; "
            f"the registry now runs in the {store_pod.STORE_POD_NAME} pod")
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
    # Rebuild the path list as plain dicts and put /v2 first: "/" is a Prefix rule that
    # matches everything, so the registry rule has to be the more specific one. Any
    # existing /v2 rule is dropped rather than kept, because the only reason to be here
    # with one present is that it names the backend the registry has moved away from.
    existing = [{"path": p.path, "pathType": p.path_type,
                 "backend": {"service": {
                     "name": p.backend.service.name,
                     "port": {"number": p.backend.service.port.number}}}}
                for p in paths
                if getattr(p, "path", "") != registry_deploy.REGISTRY_INGRESS_PATH]
    patch = {"spec": {"rules": [{
        "host": rules[0].host,
        "http": {"paths": [registry_deploy.registry_ingress_path(), *existing]},
    }]}}
    networking.patch_namespaced_ingress(SERVICE_NAME, namespace, patch)
    logger.info("Pointed %s of the %s Ingress at the registry",
                registry_deploy.REGISTRY_INGRESS_PATH, SERVICE_NAME)
    return True


#: The container an embedded object store runs in, and the path campaigns live under inside
#: it. Spelled here rather than imported from a cluster config because this module must not
#: depend on which provider is deployed -- and the question it asks is about the live pod,
#: which answers for whichever provider created it.
STORE_CONTAINER_NAME = "minio"
STORE_DATA_MOUNT = "/data"


def store_backing(pod):
    """What holds this deployment's campaigns, as ``(kind, detail)``.

    ``("emptyDir", None)`` when the store lives and dies with its pod, ``("hostPath", path)``
    or ``("claim", name)`` when it outlives one, and ``(None, None)`` where the pod runs no
    embedded store -- the campaigns are then in a bucket, which no pod holds.

    Resolved from the container's own mount rather than from a volume name, so it stays true
    for every provider that embeds a store without this module knowing which one is deployed.
    """
    container = next((c for c in (pod.spec.containers or [])
                      if c.name == STORE_CONTAINER_NAME), None)
    if container is None:
        return None, None
    mount = next((m for m in (container.volume_mounts or [])
                  if m.mount_path == STORE_DATA_MOUNT), None)
    if mount is None:
        return None, None
    volume = next((v for v in (pod.spec.volumes or []) if v.name == mount.name), None)
    if volume is None:
        return None, None
    if volume.host_path is not None:
        return "hostPath", volume.host_path.path
    if volume.persistent_volume_claim is not None:
        return "claim", volume.persistent_volume_claim.claim_name
    if volume.empty_dir is not None:
        return "emptyDir", None
    return None, None


def verify_store_pod_infrastructure(namespace="default", kube_context=None):
    """Raise unless the live ``robovast`` pod runs the registry and the index.

    Both moved out of this Deployment and into the pod ``vast cluster setup`` creates
    (:mod:`.store_pod`). That pod is created once and **kept** on a re-run -- a 409 is
    tolerated so a setup does not destroy the campaign store -- so a cluster set up before
    the move does not gain the two containers by re-running setup or upgrading. It would
    then run a service whose Ingress routes ``/v2`` at a container that does not exist and
    whose DSN names a port nothing listens on: an ImagePullBackOff on the next campaign's
    job pods, and an IndexUnreachableError on the next query, both far from here.

    Checked before the service is deployed, because the remedy recreates the store pod
    (``vast cluster cleanup`` then ``vast cluster setup``) and what that costs depends on
    what backs the store -- so the operator must choose it knowingly rather than discover it
    from a failing pilot run.
    """
    from kubernetes import client  # pylint: disable=import-outside-toplevel
    from kubernetes.client.rest import ApiException  # pylint: disable=import-outside-toplevel

    from . import store_pod  # pylint: disable=import-outside-toplevel

    _load_kube_config(kube_context)
    try:
        pod = client.CoreV1Api().read_namespaced_pod(store_pod.STORE_POD_NAME, namespace)
    except ApiException as exc:
        if exc.status == 404:
            # No store pod at all is a different failure with a different message, and the
            # providers that deploy one already report it (`verify_cluster_ready`).
            return
        raise
    missing = store_pod.missing_infrastructure(pod)
    if not missing:
        return
    kind, detail = store_backing(pod)
    if kind == "emptyDir":
        cost = ("Its campaign store lives in the pod, so recreating it DISCARDS every "
                "campaign the store holds, and the index cannot be re-ingested afterwards "
                "because its source goes at the same moment. Archive what matters first -- "
                "'vast share <campaign>' or 'vast campaign download <campaign>' -- because "
                "nothing else holds a complete copy.")
    elif kind in ("hostPath", "claim"):
        where = f"the node directory {detail}" if kind == "hostPath" else f"the {detail} volume"
        cost = (f"The campaigns are in {where} and outlive the pod, so the new one finds "
                f"them again and the index is re-ingested from them.")
    else:
        cost = ("The campaigns are in this deployment's bucket rather than in the pod, so "
                "nothing it holds is lost, and the index is re-ingested from them.")
    raise RuntimeError(
        f"the {store_pod.STORE_POD_NAME} pod in namespace {namespace} does not run "
        f"{', '.join(missing)}. This deployment predates their move out of the "
        f"{SERVICE_NAME} pod, and setup keeps an existing store pod as it is, so re-running "
        f"setup cannot add them. The remedy is 'vast cluster cleanup' then "
        f"'vast cluster setup', which recreates the pod; built images are rebuilt on "
        f"demand. {cost}")


def published_host(namespace="default", kube_context=None):
    """The bare hostname the Ingress publishes, or ``""``.

    Read back from the cluster for the same reason as :func:`published_url`, and needed
    separately because it doubles as the container registry's prefix: an ``upgrade``
    knows nothing about the host it was originally set up with, and rebuilding the
    registry config without it would quietly leave the deployment unable to build.
    """
    url = published_url(namespace, kube_context)
    return url.split("://", 1)[-1] if url else ""


def _cluster_env(namespace, config_name, config_kwargs, kube_context=None,
                 job_node_labels=None,):
    """Env that tells the in-cluster ClusterService how to reach the object store.

    The service (the cluster mode) reconstructs the same cluster config the controller
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
    # Written on every deploy, empty included, so dropping the option CLEARS a previously
    # configured pool instead of leaving it silently in force.
    #
    # Threaded in here rather than handed to `deploy_service(env=...)`, which is the whole
    # environment and not an addition to it -- passing a one-element list there replaced
    # this function's output wholesale and left a deployed service with no
    # ROBOVAST_CLUSTER_CONFIG_NAME, so setup reported success and every campaign then failed
    # with "the service must be deployed by 'vast exec cluster setup'".
    from .node_placement import JOB_NODE_POOL_ENV  # noqa: PLC0415
    env.append({"name": JOB_NODE_POOL_ENV,
                "value": json.dumps(job_node_labels) if job_node_labels else ""})
    # What a container asks for under `execution.sizing: calibrated` before its node has
    # been measured. Written explicitly so the deployment states its own configuration
    # rather than depending on the absence of a variable: an operator reading the Deployment
    # can see what a calibrated campaign starts from. See node_calibration.
    import os  # noqa: PLC0415

    from .node_calibration import BOOTSTRAP_CPU_ENV, BOOTSTRAP_MEMORY_ENV  # noqa: PLC0415
    # Carried from the operator's own environment -- a `.env` line, like the git token and
    # the ntfy topic -- rather than a setup flag. The `vast` CLI loads `./.env` before any
    # command runs, so `setup` already has it, and the value belongs to the CLUSTER rather
    # than to a campaign. Written even when empty, so the Deployment states that no override
    # was configured rather than leaving the variable absent and ambiguous.
    for var in (BOOTSTRAP_CPU_ENV, BOOTSTRAP_MEMORY_ENV):
        env.append({"name": var, "value": os.environ.get(var, "").strip()})
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
                      job_node_labels=None,
                      config_name=None, config_kwargs=None, git_token=None,
                      share_env=None, kube_context=None, pull_secret="",
                      auth_token="", ingress_host="", ingress_class="",
                      tls_secret="", issuer="", insecure_http=False,
                      public_origin=None, registry_host="",
                      workspaces_storage_path="", workspaces_storage_class="",
                      node_selector=None, index_password=""):
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

    *public_origin* is :data:`PUBLIC_URL_ENV`, and it is three-valued because the callers
    differ in what they can know. ``None`` means "not stated": rendered from *ingress_host*
    and *insecure_http* if those name one, and otherwise left exactly as the pod has it. A
    string is authoritative, including ``""`` for "this service is not published" — which is
    what lets an ``upgrade``, reading the live Ingress, both set the origin and clear it.
    """
    registry_host = registry_host or ingress_host
    from robovast.common.execution import resolve_controller_image
    image = image or resolve_controller_image()
    if env is None:
        env = _cluster_env(namespace, config_name, config_kwargs, kube_context,
                           job_node_labels=job_node_labels,)
    # No ROBOVAST_CONTROLLER_IMAGE in the pod env, deliberately: nothing in the pod reads
    # it, so setting it would say something untrue about what this deployment uses. The
    # conversion scripts come from a per-campaign ConfigMap built in the driver's own
    # process (postprocess_job.scripts_configmap_manifest, precisely so there is no
    # controller-image version skew), and the conversion container runs the *campaign's*
    # recorded execution image.
    #
    # Every RoboVAST image except this one is resolved *in this pod* -- the scenario image
    # for a campaign, the simulator's, the sidecar for every init container, the build
    # base. So the project they resolve from has to be carried in, or an operator who
    # configured one gets it honoured everywhere except the place it is actually read.
    #
    # One variable for the whole family means there is one thing to carry rather than
    # several to forget, and a per-image variable read in-pod but propagated nowhere is a
    # setting that appears to take and does not: `setup --force` looks like it moved the
    # images and moved only the controller. It is also the *site default*: a campaign may
    # override it on its own request
    # (CreateCampaignRequest.image_project), which is what makes a dev run need no deploy.
    # Carried UNCONDITIONALLY, empty value and all, and that is the point rather than an
    # oversight. The Deployment is applied with a strategic-merge patch, whose merge key for
    # `containers[].env` is the variable NAME -- so a variable the patch omits is not removed,
    # it is preserved. Emitting these only when set makes them write-only: an operator who
    # sets ROBOVAST_PROJECT_TAG once could never unset it again, because deleting it from
    # ./.env (or from their shell) simply leaves it out of the next patch and the old value
    # stays in the pod -- a deployment resolving the family at a tag nobody can find in any
    # file, and every campaign's build failing to pull an image at it.
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

    # The published origin. Written when somebody knows it, and left alone when nobody
    # does -- a merge patch preserves what it does not mention, which is the only safe
    # answer for a caller that would otherwise overwrite a correct value with a guess.
    #
    # Knowing it means knowing the *scheme* as well as the host. A `setup` that publishes
    # the service has that in its own arguments. An `upgrade` has neither: it reads the
    # host back from the live Ingress and holds none of the TLS arguments that Ingress was
    # created with -- so it reads the whole origin instead (`published_url`, whose scheme
    # comes from the live TLS block) and states it here. Deriving `https://<host>` from the
    # host it passes as `registry_host` would have published the wrong scheme for anything
    # set up with --insecure-http, and rendering from `ingress_host` -- which an upgrade
    # must never pass, since that also *creates* the Ingress -- would have erased the
    # origin on every upgrade of a published deployment.
    declared = public_origin if public_origin is not None else (
        public_url(ingress_host, insecure_http) or None)
    if declared is not None and not any(e["name"] == PUBLIC_URL_ENV for e in env):
        env = [*env, {"name": PUBLIC_URL_ENV, "value": declared}]

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

    from . import index_deploy  # pylint: disable=import-outside-toplevel
    # Both node-local stores claim through the same class. `workspaces_pvc_manifest` existed
    # but was never emitted, so passing --workspaces-storage-class produced a Deployment
    # referencing a PVC nothing created and a pod that stayed Pending with no explanation.
    # `results_volume` is backed by that same class, so it would have inherited the fault.
    for pvc in (workspaces_pvc_manifest(namespace, workspaces_storage_class),
                results_pvc_manifest(namespace, workspaces_storage_class)):
        if pvc:
            extra.append(pvc)

    # The index credential, and the DSN that reaches it. The Secret is emitted here AND at
    # cluster setup, because both pods need it: the store pod's Postgres reads it as
    # POSTGRES_PASSWORD, this Deployment carries it inside the DSN. `deploy_service` reads
    # the deployed value back rather than minting one -- see `existing_index_password` on
    # why regenerating it would break only the second deploy.
    if index_password:
        extra.append(index_deploy.index_secret_manifest(namespace, index_password))
    env = list(env or [])
    if not any(e.get("name") == INDEX_DSN_ENV for e in env):
        # Built from the Service name and this namespace, never from a configured host:
        # the index is in another pod now, so the DSN is a `<service>.<namespace>.svc`
        # name that resolves in any cluster (see `store_pod.store_host`).
        env.append({"name": INDEX_DSN_ENV,
                    "value": index_deploy.index_dsn(index_password, namespace)})

    ingress = _ingress_manifest(namespace, ingress_host, ingress_class,
                                tls_secret, issuer, auth_token=auth_token,
                                insecure=insecure_http)
    return [
        *_service_rbac_manifests(namespace),
        *extra,
        _deployment_manifest(namespace, image, env=env, git_secret=have_git_secret,
                             env_secret_names=env_secret_names,
                             pull_secret=pull_secret,

                             workspaces_storage_path=workspaces_storage_path,
                             workspaces_storage_class=workspaces_storage_class,

                             node_selector=node_selector),
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

    Removing a variable from ``.env`` must remove the Secret it created: ``deploy_service``
    rediscovers the registry credential *by existence*, so a Secret left in place stays
    wired to the Deployment as an imagePullSecret. An operator deleting a password to revoke
    access would get a successful "upgraded" while the credential remained deployed and in
    use — rotation working, removal silently ignored.

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


def _resolve_data_node(core, *, workspaces_storage_class="",
                       deployed_selector=None):
    """The data-node selector for the service pod, preserving whatever is already deployed.

    Node-local when **either** volume is a hostPath: the pod carries both, so one provisioned
    volume does not free it to move. With both on a StorageClass there is nothing on a node to
    keep and pinning would only make the pod harder to schedule.
    """
    from .node_placement import (  # pylint: disable=import-outside-toplevel
        DATA_NODE_LABEL, resolve_placement)

    # Only the workspaces class matters now: both volumes left in this pod (workspaces and
    # results) are backed by it. The registry's class used to be ANDed in here and moved
    # to the store pod with the registry itself.
    node_local = not workspaces_storage_class
    if not node_local:
        return {}
    placement = resolve_placement(core, DATA_NODE_LABEL, node_local=True,
                                  allow_auto_pick=False)
    if placement is not None:
        return placement.selector
    # No label anywhere. Keep whatever the live pod has rather than dropping to unpinned:
    # this path is an upgrade, and an upgrade must not move data it was asked to leave alone.
    return dict(deployed_selector or {})


def service_storage_from_cluster(namespace="default", kube_context=None) -> dict:
    """The service pod's storage settings, read back from the live Deployment.

    The mirror of :func:`buildkitd_deploy.buildkitd_storage_from_cluster`, and it exists for
    the same reason: ``--registry-storage-class`` / ``--workspaces-storage-class`` and their
    paths arrive as ``setup`` flags and are recorded nowhere but the Deployment they produced.
    An ``upgrade`` re-rendering from defaults therefore handed a PVC-backed registry a
    ``hostPath`` -- a new empty registry on whichever node the pod next landed on, while the
    old claim still held its space and nothing said so.

    Returns ``{}`` when there is no Deployment yet; the caller then uses its own defaults. A
    cluster that *fails* to answer is not that answer and is not swallowed -- defaulting there
    is precisely the silent migration above.
    """
    from kubernetes import client  # pylint: disable=import-outside-toplevel

    from .kube_client import load_kube_config  # pylint: disable=import-outside-toplevel

    load_kube_config(kube_context)
    try:
        dep = client.AppsV1Api().read_namespaced_deployment(SERVICE_NAME, namespace)
    except client.exceptions.ApiException as e:
        if e.status != 404:
            raise
        logger.debug("no %s Deployment in %s; nothing to recover", SERVICE_NAME, namespace)
        return {}

    pod_spec = dep.spec.template.spec
    settings = {}
    # The registry volume was recovered here too until the registry moved to the store pod
    # (:mod:`.store_pod`). It is created once by `vast cluster setup` now, so an upgrade
    # neither re-renders nor needs to recover it.
    for volume_name, prefix in ((WORKSPACES_VOLUME_NAME, "workspaces"),):
        volume = next((v for v in (pod_spec.volumes or []) if v.name == volume_name), None)
        if volume is None:
            continue
        if volume.host_path is not None:
            settings[f"{prefix}_storage_path"] = volume.host_path.path or ""
        elif volume.persistent_volume_claim is not None:
            claim = client.CoreV1Api().read_namespaced_persistent_volume_claim(
                volume.persistent_volume_claim.claim_name, namespace)
            storage_class = claim.spec.storage_class_name
            if not storage_class:
                # `workspaces_volume` takes the PVC branch only for a
                # non-empty class, so re-rendering from an empty one silently becomes a
                # hostPath -- the exact migration this reader exists to prevent.
                raise RuntimeError(
                    f"the {prefix} volume is a PersistentVolumeClaim whose StorageClass "
                    f"cannot be determined, so re-rendering it would silently fall back to "
                    f"a hostPath and abandon the claim. Set its StorageClass explicitly on "
                    f"the claim, or re-run 'vast cluster setup' with the storage flags.")
            settings[f"{prefix}_storage_class"] = storage_class
    if pod_spec.node_selector:
        settings["node_selector"] = dict(pod_spec.node_selector)
    return settings


def deploy_service(namespace="default", kube_context=None, image=None, env=None,
                   job_node_labels=None,
                   config_name=None, config_kwargs=None, dry_run=False,
                   rotate_token=False, ingress_host="", ingress_class="",
                   tls_secret="", issuer="", insecure_http=False,
                   public_origin=None, registry_host="",
                   workspaces_storage_path="", workspaces_storage_class="",
                   node_selector=None):
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

    # Anything the caller did not state is RECOVERED, never defaulted. `upgrade` passes none
    # of the storage or placement arguments, so reading "not passed" as "unpinned, on a
    # hostPath" lets one upgrade unpin the service pod and revert a PVC-backed registry --
    # silently, on the deployment whose data was the reason those flags were given.
    deployed = service_storage_from_cluster(namespace, kube_context)
    workspaces_storage_path = (workspaces_storage_path
                               or deployed.get("workspaces_storage_path", ""))
    workspaces_storage_class = (workspaces_storage_class
                                or deployed.get("workspaces_storage_class", ""))
    if node_selector is None:
        # `None` is "resolve", `{}` is "explicitly unpinned". A caller cannot lose the pin by
        # forgetting to pass it, which is the whole point of the constant label. Auto-picking
        # is refused here: only `setup` decides a placement, and it passes one in.
        node_selector = _resolve_data_node(
            core, workspaces_storage_class=workspaces_storage_class,
            deployed_selector=deployed.get("node_selector"))

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

    # The index password is ALWAYS reused when one is deployed -- never rotated, not even by
    # --rotate-token. `initdb` applied it once to a data directory that still exists, so a
    # new one would leave the Secret and the database disagreeing while every surface
    # reported success. Rotating it would mean re-initialising the volume, i.e. discarding
    # the index; that is a deliberate operation, not a side effect of a token rotation.
    index_password = existing_index_password(namespace, kube_context) or generate_token()

    manifests = service_manifests(
        namespace=namespace, image=image, env=env, job_node_labels=job_node_labels,
        config_name=config_name, config_kwargs=config_kwargs,
        kube_context=kube_context, pull_secret=pull_secret,
        auth_token=auth_token, index_password=index_password,
        ingress_host=ingress_host,
        ingress_class=ingress_class, tls_secret=tls_secret, issuer=issuer,
        insecure_http=insecure_http, public_origin=public_origin,
        registry_host=registry_host,
        workspaces_storage_path=workspaces_storage_path,
        workspaces_storage_class=workspaces_storage_class,
        node_selector=node_selector)
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
    elif not dry_run:
        # NO Ingress was rendered -- and this is the common case, because rendering one
        # needs --ingress-host plus the TLS arguments, which a re-run of setup and every
        # upgrade are not given. The live Ingress is then simply left alone, which is right
        # for the host, class and certificate (the operator's configuration) and WRONG for
        # the one field that is ours: the backend of the /v2 rule. When the registry moved
        # to the store pod, an untouched Ingress went on routing pushes at a Service with
        # no registry port, and nginx answered 503 in the middle of a campaign's build --
        # after cleanup + setup had reported success minutes earlier.
        #
        # Reconciling the backend in place, rather than deleting the Ingress at cleanup:
        # deleting it would unpublish the deployment and demand the original TLS arguments
        # to get it back, for a field the operator never set.
        reconcile_registry_ingress_path(namespace=namespace, kube_context=kube_context)

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
        # here would send the caller off to suggest `vast cluster setup` for a
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
