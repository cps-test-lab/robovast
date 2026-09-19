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

"""``ClusterService`` — the in-cluster service core (the cluster mode).

Runs inside the ``robovast-service`` Deployment and drives every cluster campaign
**in this process**, exactly as :class:`~robovast.service.client.LocalTransport`
already does for Docker: one worker thread per campaign runs the unified
``CampaignController`` against a :class:`KubernetesBackend`, which creates the
scenario Jobs. Cluster and local therefore share the whole driver-hosting shape —
only the backend differs — and everything below is expressed as overrides of
``LocalTransport``'s launch hooks.

There is **no per-campaign controller pod**: the service hosts the driver, and live
status is a read of the in-process ``ControllerState``.

What still runs as its own Kubernetes workload — because each genuinely needs to:

* **scenario runs** and the **rosbag→CSV postprocessing** — Jobs (scheduled, queued);
* **auxiliary variation containers** — one aux Pod per campaign the driver execs
  into (see :mod:`..execution.cluster_execution.container_runner`).

A campaign's home is the service's results volume, exactly as on the local lane, so
every file, scene, config and query path is the inherited one and nothing here reads
results from anywhere but the campaign directory. Pods reach that directory through
the data plane (:mod:`.pod_access`): a Job fetches its inputs as one tar stream and
delivers its outputs as one, so a finished Job's results are already home. Finished
campaigns survive a service restart untouched; a campaign still *running* when the
service restarts is picked up again (:mod:`.campaign_resume`).
"""

import contextlib
import dataclasses
import json
import logging
import os
import shlex
import tempfile
import threading
import time
from pathlib import Path

from robovast.common.config import SCENARIO_CONTAINER
from robovast.execution.control_server import (STOP_ALREADY_OVER, STOP_RUNS,
                                               STOP_SCOPE_MESSAGES, Phase,
                                               stop_scope_for_phase)
from robovast.common.campaign_data import update_launch_scheduling
from robovast.service.client import LocalTransport
from robovast.service.local_transport import require_scheduling_change
from robovast.service.interface import (ActionResult, JobCounts, JobKind,
                                        JobSummary, JobUsage, ListJobsResponse, LogChunk,
                                        ResourceUsage, DiskSpace, UpgradeInfo, VersionInfo)

from .manifests import CALIBRATION_JOB_KIND, JOB_KIND_LABEL

logger = logging.getLogger(__name__)


def _tree_bytes(path: Path) -> int:
    """Bytes of the files under *path*. A file removed mid-walk is not counted, not an error."""
    total = 0
    for dirpath, _dirs, names in os.walk(path):
        for name in names:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return total

AUX_LABEL = "app=robovast-aux"


#: Container-state waiting reasons that mean the node is still *fetching the image*. Kubelet reports
#: the pull itself as part of creating the container, and each way it can fail under its own name, so
#: all of them describe one wait: the bytes are not on this node. Anything else a container waits for
#: -- a pull secret that names no Secret, most of all -- is the image having arrived and the container
#: still not coming up, which a reader has to fix somewhere else entirely.
_IMAGE_WAIT_REASONS = ("ContainerCreating", "PodInitializing", "ErrImagePull", "ImagePullBackOff",
                      "ImageInspectError", "ErrImageNeverPull", "RegistryUnavailable")


#: How often :func:`_aux_pending_logger` repeats a reason that has not changed. Long enough
#: that a pod which comes up normally says its reason once, short enough that a reader
#: deciding whether to wait or intervene gets a second line before they give up.
_AUX_WAIT_REPEAT_S = 30.0


def _aux_pending_logger(tag):
    """Say what a composition's aux pod is waiting on, while it is still waiting.

    An on-demand pod pays its schedule and its image pull inside the first command that asks
    for one, so a campaign can sit for minutes in the middle of composing with nothing said.
    The scene build reports that to a caller watching a build; a campaign has no such caller,
    and its worker thread's log is the channel — which is where the reason belongs anyway,
    since it outlives the run.

    A change is logged at once; an unchanged reason is repeated every
    :data:`_AUX_WAIT_REPEAT_S` with how long the wait has run. ``wait_pod_ready`` polls
    every two seconds, so logging every poll is how a log stops being read — but logging
    only changes is what makes a pull of several minutes indistinguishable from a hung
    campaign, since the reason arrives once, seconds in, and then nothing until it ends.
    The elapsed figure is the part that separates the two.
    """
    started = time.monotonic()
    state = {"reason": "", "said": 0.0}

    def report(reason: str) -> None:
        current = reason or "pending"
        now = time.monotonic()
        if current == state["reason"] and now - state["said"] < _AUX_WAIT_REPEAT_S:
            return
        state.update(reason=current, said=now)
        logger.info("Aux pod for %s is not ready yet after %ds: %s",
                    tag, int(now - started), current)

    return report


def _pod_wait_reporter(on_wait):
    """Turn a scene build's ``on_wait`` into an :class:`AuxPodSession` pending callback.

    ``None`` when nobody is listening, which is what stops the session reading a reason no one
    will be shown.

    The pod's reason is passed on verbatim as the detail, and only *classified* here: whoever is
    waiting needs to know that a two-minute pull is a pull (it will end) and that an unreachable
    image is not (it will not), and the kubelet's own wording is the only thing that can say which.
    """
    if on_wait is None:
        return None

    from robovast.service import scene_cache  # pylint: disable=import-outside-toplevel

    def report(reason: str) -> None:
        if not reason:
            # No container state at all: the pod has not been placed on a node yet, so what it
            # waits for is capacity rather than bytes.
            on_wait(scene_cache.STAGE_QUEUED, "")
        elif reason.split(":", 1)[0].strip() in _IMAGE_WAIT_REASONS:
            on_wait(scene_cache.STAGE_PULLING, reason)
        else:
            on_wait(scene_cache.STAGE_STARTING, reason)

    return report


def _metrics_failure_reason(exc, resource: str) -> "str | None":
    """Why a ``metrics.k8s.io`` read failed, or ``None`` when this cannot tell.

    A string is a settled fact about the cluster -- an add-on nobody installed, a role nobody
    reconciled -- so a caller may remember it and stop asking for a while. ``None`` says the
    failure looks transient (a timeout, an aggregated API restarting mid-read); a caller
    reports it but must not remember it, or one hiccup blinds the reading for the whole memo.

    *resource* names the sub-resource in the message because the two grants are given
    independently: a role may carry ``nodes`` and not ``pods``, and a reason naming the wrong
    one sends a reader to reconcile something that is already there.
    """
    status = getattr(exc, "status", None)
    if status == 403:
        return (f"the service's ClusterRole does not grant metrics.k8s.io/{resource} -- "
                "run `vast service upgrade` to reconcile RBAC")
    if status == 404:
        return ("metrics.k8s.io is not served -- install metrics-server on the cluster "
                "to measure real cpu/memory use")
    return None


class ClusterService(LocalTransport):
    """Interface implementation that drives campaigns in-process over Kubernetes."""

    #: A staged entrypoint must carry the *cluster* init and post-run blocks here, since
    #: that is where the exec actually runs. Copying a campaign's rendered entrypoint
    #: across lanes is what this flag exists to prevent.
    _EXEC_CLUSTER_LANE = True

    #: No screen to draw on: the work runs in pods, and the X socket a window would need
    #: belongs to whatever machine the service happens to sit on — never the caller's.
    #: ``_admit_show_gui`` turns this into an explicit refusal rather than a silent
    #: windowless run.
    _SUPPORTS_SHOW_GUI = False

    #: Campaigns here run against each other for the cluster, so there is a queue to order and
    #: a rank means something. ``_admit_scheduling`` accepts rather than refuses.
    _SUPPORTS_SCHEDULING = True

    #: How long a kubelet Summary reading is reused -- deliberately longer than
    #: ``_USAGE_CACHE_TTL``. One Summary payload carries every pod's stats on that node
    #: (hundreds of KB on a busy one), and a disk fills over minutes, not seconds.
    _DISK_CACHE_TTL = 60.0
    #: Per-node and total wall-clock ceilings. ``/usage`` is polled by every open tab and
    #: is the app's answer to "is the backend there?", so an unresponsive kubelet must not
    #: turn it into a hang, and N nodes must not make its cost proportional to N unbounded.
    _DISK_NODE_TIMEOUT = 2.0
    _DISK_BUDGET_SECONDS = 5.0

    #: Wall-clock ceiling on the metrics-server read, for the same reason as
    #: ``_DISK_NODE_TIMEOUT``: ``/usage`` answers "is the backend there?" and must not hang
    #: because an aggregated API is slow.
    _METRICS_TIMEOUT = 2.0
    #: How long "this cluster cannot answer metrics.k8s.io" is remembered. A missing
    #: metrics-server, or a ClusterRole that was never reconciled, changes only when someone
    #: installs a component or runs ``vast service upgrade`` -- so retrying every 10 s
    #: window would spend a round trip and an audit-log line six times a minute to learn the
    #: same thing. Only *failures* are memoised; a working cluster is read fresh each window.
    _METRICS_ABSENT_TTL = 600.0

    #: How long one pod-metrics snapshot is served before it is read again. The window each
    #: sample states is how often the cluster can *have* a new one, so that window is the TTL
    #: and these only bound it -- against a cluster that states none, or an absurd one. Reading
    #: faster than the window spends a round trip to be handed the same numbers back, and a
    #: campaign card polls its job list every couple of seconds.
    _POD_METRICS_TTL_DEFAULT = 15.0
    _POD_METRICS_TTL_MIN = 10.0
    _POD_METRICS_TTL_MAX = 60.0

    def __init__(self, namespace=None, cluster_config_name=None,
                 cluster_config_kwargs=None, store=None,
                 reap_on_start=True, kube_context=None, results_dir=None):
        # Where every campaign of this service lives. Not a cache: the driver writes into
        # it, pods deliver their outputs into it through the data plane, extraction reads
        # it through a path, and postprocessing derives data.db from it -- so it has to
        # outlive a container, and the deployment mounts the directory it names (see
        # serve_backend / service_deploy).
        super().__init__(store=store, results_dir=results_dir)
        self.namespace = namespace or os.environ.get("ROBOVAST_NAMESPACE", "default")
        # Which kubeconfig context to dispatch into. The in-cluster config is what the
        # API client uses, but the context *name* still resolves per-cluster resource
        # lists -- deploy stamps it into ROBOVAST_KUBE_CONTEXT for the in-pod driver.
        self.kube_context = kube_context or os.environ.get("ROBOVAST_KUBE_CONTEXT")
        # Which of the three sources won, reported in version(). Without it the
        # implicit case ("whatever kubectl points at") is indistinguishable from a
        # deliberate one, and that is the case that quietly targets another cluster.
        self._kube_context_source = (
            "constructor" if kube_context
            else "ROBOVAST_KUBE_CONTEXT" if os.environ.get("ROBOVAST_KUBE_CONTEXT")
            else "active kubeconfig context")
        # Built on first use rather than here: constructing it touches the Kubernetes client,
        # and a service must come up to report that the cluster is unreachable rather than
        # failing to start because it is.
        self._admission = None
        self._admission_lock = threading.Lock()
        self._config_name = cluster_config_name or os.environ.get(
            "ROBOVAST_CLUSTER_CONFIG_NAME")
        self._config_kwargs = cluster_config_kwargs
        if self._config_kwargs is None:
            raw = os.environ.get("ROBOVAST_CLUSTER_CONFIG_KWARGS")
            self._config_kwargs = json.loads(raw) if raw else {}
        # Last kubelet Summary reading behind the disk and results meters, as
        # ``(monotonic, fields)``. Its own TTL, longer than the usage cache's -- see
        # ``_DISK_CACHE_TTL``. Read under ``_usage_lock``, so it needs no lock of its own.
        self._disk_cache: "tuple[float, dict] | None" = None
        # Why metrics-server could not be read, as ``(monotonic, reason)`` -- a NEGATIVE memo
        # only. See ``_METRICS_ABSENT_TTL``: a cluster that does not serve metrics.k8s.io
        # must not be asked every usage window forever. Read under ``_usage_lock``.
        self._metrics_absent: "tuple[float, str] | None" = None
        # Last pod-metrics snapshot as ``(expires_at, {job: {container: (cores, bytes)}})``.
        # Named apart from the ``_pod_metrics`` method that fills it: an attribute assigned
        # here shadows a method of the same name on the instance, so the reader would become
        # uncallable the moment the service was constructed.
        # One read serves every campaign: the Jobs all carry the same ``jobgroup``, so a
        # second open campaign card costs nothing.
        self._pod_metrics_snapshot: "tuple[float, dict] | None" = None
        # Its own memo, never ``_metrics_absent``. The nodes and pods grants are given
        # independently, so a 403 on one says nothing about the other -- sharing would blank
        # the capacity meter over a missing job-usage grant, under a reason naming the wrong
        # sub-resource.
        self._pod_metrics_absent: "tuple[float, str] | None" = None
        # Not ``_usage_lock``: that one is held across a reading that talks to every kubelet
        # in turn, and the job listing must not wait behind it.
        self._pod_metrics_lock = threading.Lock()
        if reap_on_start:
            self.reap_orphans()
            self.resume_interrupted_campaigns()
            # After the resume, and separately from it: a campaign whose postprocess is
            # still running has recorded an ending, so the resume above passes over it by
            # design. Only a waiter writes what that Job did, so without this the previous
            # attempt's verdict stands over a conversion that succeeded.
            self.reattach_live_postprocessing()

    # -- version ------------------------------------------------------------

    def version(self) -> VersionInfo:
        v = super().version()
        v.backend = "kubernetes"
        v.kube_context = self.kube_context
        v.kube_context_source = self._kube_context_source
        v.namespace = self.namespace
        v.in_pod = bool(os.environ.get("KUBERNETES_SERVICE_HOST"))
        v.api_server = self._api_server_url()
        # No filesystem roots on this lane: the campaigns and the workspaces are on this
        # service's volumes, and that disk is the cluster's, not the caller's.
        v.results_root = None
        v.sources_root = None
        # Overrides the local lane's unconditional True: here a build needs somewhere to
        # push to, and this deployment may not have one. Read from the cached cluster
        # config, which is a plain `os.environ` lookup -- deliberately not
        # `_resolve_registry_objects`, which does Secret lookups and would put an API
        # call in the one call a client makes to find out where it is pointed.
        try:
            registry = self._cluster_config().get_registry_config()
            v.can_build_images = registry.enabled()
            v.build_unavailable = registry.why_disabled() or None
        except Exception:  # noqa: BLE001 - no config readable is not a build verdict
            # Leave both None: "I could not tell" is not "you cannot build", and a
            # consumer prints nothing for None. Reporting False here would send an
            # operator to fix a registry over a config-loading problem.
            pass
        return v

    # -- rolling this service onto newer bytes ------------------------------

    def upgrade_info(self) -> UpgradeInfo:
        """What is running here, what is published, and whether this pod can roll itself.

        Builds on the local answer -- which supplies the live campaign list and a refusal --
        and replaces the refusal only once every precondition actually holds. Written that
        way round so a lane that cannot roll always carries a *reason*, and never an empty
        ``supported=False`` a reader has to interpret.
        """
        info = super().upgrade_info()
        if not os.environ.get("KUBERNETES_SERVICE_HOST"):
            # A service driving the cluster from outside it: there is a Deployment, but it
            # is not this process, and rolling it would not update the thing the caller is
            # talking to.
            info.unsupported_reason = (
                "this service drives the cluster from outside it, so it has no Deployment "
                "of its own to roll. Restart it where it runs.")
            return info
        from .service_deploy import deployment_image_ref, running_image_digest
        ref, denied = deployment_image_ref(self.namespace, self.kube_context)
        if denied:
            # A 403 on the plain read is a missed migration, not a broken cluster: the
            # apps/deployments grant arrived with this feature, and a deployment set up
            # before it has a Role without it. Named with the fix, and the fix deliberately
            # does not need a roll -- which is what makes it available mid-campaign.
            info.unsupported_reason = (
                "this deployment's service account may not read its own Deployment. Run "
                "'vast service upgrade --no-restart' once, from somewhere with "
                "cluster access, to grant it -- that reconciles RBAC without rolling the "
                "pod, so it is safe while a campaign is in flight.")
            return info
        info.image_ref = ref
        info.running_digest = running_image_digest(self.namespace, self.kube_context)
        try:
            info.registry_digest = self._images.published_digest(ref) if ref else ""
        except Exception as e:  # noqa: BLE001 - a registry that will not answer is a fact
            logger.debug("could not read the published digest for %s: %s", ref, e)
            info.registry_digest = ""
        # Its own try: the date is an addition to the digest above, so a registry that
        # answers the HEAD but not the config blob must still report the digest it gave.
        try:
            info.registry_built_at = (
                self._images.published_created(ref) if info.registry_digest else "")
        except Exception as e:  # noqa: BLE001 - same fact, one question further in
            logger.debug("could not read the published build date for %s: %s", ref, e)
            info.registry_built_at = ""
        if info.running_digest and info.registry_digest:
            # The two sides spell a digest differently: the registry answers
            # ``repo@sha256:...`` while the kubelet's imageID is already reduced to the
            # bare ``sha256:...`` (see running_image_digest). Compare the part they share.
            # When either is missing this stays None -- "I could not tell" -- because
            # rendering that as "up to date" is the one wrong answer here.
            info.upgrade_available = (
                info.registry_digest.rpartition("@")[2] != info.running_digest)
        info.supported = True
        info.unsupported_reason = None
        return info

    def upgrade_service(self, force: bool = False) -> ActionResult:
        """Roll this service's own Deployment. See :meth:`upgrade_info` for what it is not.

        The live-campaign refusal is a ``RuntimeError`` (409, a conflict the caller can
        resolve) rather than the ``ValueError`` (400) an unsupported lane raises: one is
        "not now", the other is "not here".

        It refuses only for the campaigns that would actually be lost. A live campaign is not
        itself a reason: a replacement pod picks one back up (see
        :meth:`resume_interrupted_campaigns`). What is left is the campaigns that cannot be
        picked back up, and the refusal names
        each one's reason rather than its phase -- because the reason is what the operator
        would have to change.
        """
        info = self.upgrade_info()
        if not info.supported:
            raise ValueError(info.unsupported_reason)
        blocked = self._campaigns_a_restart_would_lose(info.active_campaigns)
        if blocked and not force:
            named = "; ".join(f"{cid}: {why}" for cid, why in blocked.items())
            raise RuntimeError(
                f"refusing to roll while {len(blocked)} live campaign(s) could not be "
                f"picked up again by the replacement — {named}. Stop them, wait for them, "
                f"or force the roll. Every other live campaign survives the roll: its Jobs "
                f"keep running and the new pod re-attaches to them.")
        from .service_deploy import patch_restart_annotation
        stamped = patch_restart_annotation(self.namespace, self.kube_context)
        return ActionResult(ok=True, message=(
            f"rolling robovast-service (restartedAt {stamped}). Kubernetes starts the new "
            f"pod before stopping this one, so the API stays up; watch the running digest "
            f"for the handover. RBAC, the registry route, the env "
            f"Secrets and the build daemon are NOT reconciled -- "
            f"'vast service upgrade' is what does that."))

    def _api_server_url(self) -> "str | None":
        """The API server this lane targets, read from config only — never dialled.

        ``version()`` is the call a client makes to find out *where* it is pointed,
        including when the cluster is unreachable; a probe here would make it hang
        exactly when the answer matters most. ``None`` when the config cannot be
        read at all, which is itself the answer to "which cluster?".
        """
        try:
            from kubernetes import client as k8s_client

            from .kube_client import load_kube_config
            load_kube_config(context=self.kube_context)
            return k8s_client.Configuration.get_default_copy().host
        except Exception:  # noqa: BLE001 - informational field, never fatal
            return None

    def _compute_resource_usage(self) -> ResourceUsage:
        """Cluster CPU/memory capacity + current usage from the Kubernetes API.

        Capacity is the sum of every node's ``allocatable`` (the same measure admission
        sizes against); ``cpu_reserved`` is the sum of resource *requests* of the pods
        **bound to those same nodes** — what the scheduler has actually committed,
        the number ``kubectl describe node`` calls "Allocated resources". Both are
        read behind :meth:`LocalTransport.resource_usage`'s TTL cache, and the pod
        list is filtered server-side to skip finished pods — so a poll costs at most
        one ``list_node`` + one filtered ``list_pod`` + one metrics-server ``nodes`` list
        per cache window.

        ``cpu_measured`` is what is actually being consumed, from metrics-server (see
        :meth:`_measured_cpu_mem`), and is ``None`` on a cluster that cannot answer for it.
        Reserved is the number that decides whether the next campaign *fits*; measured is
        the number that says whether the last one needed what it asked for.

        Summing over one node set keeps ``used <= capacity``, which a cluster-wide
        pod sum does not: a pod still waiting for a node (or left behind by one that
        was removed) requests resources nothing has granted, so a queue of pending
        scenario runs would otherwise report more cores in use than the cluster has —
        "29.7/24" on a 24-core workstation. Pending work is visible as
        ``jobs_pending`` instead, counted from Jobs by :meth:`_scenario_job_tally`.

        Requires the service's ClusterRole (nodes/pods get,list + nodes/proxy get +
        metrics.k8s.io/nodes get,list — see ``service_deploy._service_rbac_manifests``). The
        proxy grant is for the disk meter's kubelet read, which adds one Summary GET per node
        per ``_DISK_CACHE_TTL`` on top of the per-window list calls above; the metrics grant
        is for ``cpu_measured``, and a deployment that lacks it reports the reason rather
        than losing the rest of this reading.
        """
        from .kube_client import pod_workload_containers  # pylint: disable=import-outside-toplevel
        from .service_deploy import SERVICE_NAME  # pylint: disable=import-outside-toplevel
        from .kube_client import parse_resource as _parse_resource  # pylint: disable=import-outside-toplevel
        v1 = self._k8s()

        cpu_capacity = 0.0
        mem_capacity = 0
        node_names = set()
        for node in v1.list_node().items:
            alloc = node.status.allocatable or {}
            cpu_capacity += _parse_resource(alloc.get("cpu"))
            mem_capacity += int(_parse_resource(alloc.get("memory")))
            node_names.add(node.metadata.name)

        cpu_used = 0.0
        mem_used = 0
        service_node = None
        pods = v1.list_pod_for_all_namespaces(
            field_selector="status.phase!=Succeeded,status.phase!=Failed")
        for pod in pods.items:
            # Which node carries the service, taken from a list we already have rather than
            # a read of its own: the disk meter reports THAT node's filesystem, because the
            # workspaces volume is a hostPath there on a stock RKE2 and a cluster-wide sum
            # answers a question nobody asks.
            meta = pod.metadata
            if ((meta.labels or {}).get("app") == SERVICE_NAME
                    and meta.namespace == self.namespace):
                service_node = getattr(pod.spec, "node_name", None) or service_node
            if getattr(pod.spec, "node_name", None) in node_names:
                # Native sidecars included: Kubernetes adds their requests to the pod's
                # effective total rather than taking the max as it does for ordinary init
                # containers. Counting only spec.containers would therefore under-report a
                # scenario job by its simulator and its SUT -- the two biggest reservations
                # in a three-container campaign -- and this number is what sizes a sweep.
                for container in pod_workload_containers(pod):
                    requests = (container.resources.requests
                                if container.resources else None) or {}
                    cpu_used += _parse_resource(requests.get("cpu"))
                    mem_used += int(_parse_resource(requests.get("memory")))

        jobs_running, jobs_pending = self._scenario_job_tally()
        measured = self._disk_and_results(node_names, service_node)
        cpu_metric, mem_metric, metrics_reason = self._measured_cpu_mem(node_names)
        return ResourceUsage(
            backend="kubernetes",
            cpu_capacity=cpu_capacity,
            cpu_used=cpu_used,
            memory_capacity_bytes=mem_capacity,
            memory_used_bytes=mem_used,
            # The request sum, said in the field that means it. ``cpu_used`` above carries
            # the same number as the headline every existing consumer reads.
            cpu_reserved=cpu_used,
            memory_reserved_bytes=mem_used,
            cpu_measured=cpu_metric,
            memory_measured_bytes=mem_metric,
            metrics_unavailable=metrics_reason,
            parallel_runs=True,   # runs execute in parallel, bounded only by capacity
            jobs_running=jobs_running,
            jobs_pending=jobs_pending,
            disk=measured.get("disk"),
            disk_node=measured.get("disk_node"),
            results=measured.get("results"),
            disk_unavailable=measured.get("unavailable"),
        )

    def _measured_cpu_mem(self, node_names) -> tuple:
        """Real cpu/memory consumption from metrics-server: ``(cores, bytes, reason)``.

        One ``metrics.k8s.io/v1beta1/nodes`` list for the whole cluster -- not a per-node
        fan-out like :meth:`_disk_and_results`, which is why this needs no budget: the payload
        is one small item per node, next to a ``list_pod_for_all_namespaces`` in the same
        window that carries every non-terminal pod spec in the cluster.

        Summed over ``node_names`` -- the same node set ``cpu_capacity`` is summed over, so
        capacity, reserved and measured are all statements about one cluster.

        **Either all three numbers or none.** A node in the set with no metrics item, or one
        whose quantity does not parse, yields ``(None, None, reason)`` rather than a partial
        sum: :func:`kube_client.parse_resource` answers 0 for unparseable (load-bearing in
        the fit tests, where an unadvertised resource must read as none available), so
        summing blind would report a cluster at 60% of its cores as being at 10% -- a wrong
        answer that looks right. A freshly joined node is missing from metrics for ~15 s, and
        a gap in the chart is the truth for that window.

        Failures are memoised for ``_METRICS_ABSENT_TTL``; successes are not. Called with
        ``_usage_lock`` held (see :meth:`LocalTransport.resource_usage`), so both the memo
        and the read need no lock of their own.

        Requires ``metrics.k8s.io/nodes`` get+list in the service's usage ClusterRole (see
        ``service_deploy._service_rbac_manifests``). A deployment whose RBAC predates that
        grant keeps working: it gets a 403, and the reason says which command reconciles it.
        """
        from .kube_client import (CONNECT_TIMEOUT_SECONDS,  # pylint: disable=import-outside-toplevel
                                  parse_resource)

        now = time.monotonic()
        remembered = self._metrics_absent
        if remembered is not None and now - remembered[0] < self._METRICS_ABSENT_TTL:
            return None, None, remembered[1]

        def absent(reason):
            self._metrics_absent = (time.monotonic(), reason)
            return None, None, reason

        try:
            listed = self._k8s_custom().list_cluster_custom_object(
                "metrics.k8s.io", "v1beta1", "nodes",
                # A (connect, read) pair rather than a scalar: a scalar replaces BOTH, and
                # the connect default is the one thing `load_kube_config` installs
                # process-wide (see kube_client). Only the read is capped here.
                _request_timeout=(CONNECT_TIMEOUT_SECONDS, self._METRICS_TIMEOUT))
        except Exception as e:  # noqa: BLE001 - capacity must still be answerable
            settled = _metrics_failure_reason(e, "nodes")
            if settled is not None:
                return absent(settled)
            # Anything else (a timeout, an unavailable aggregated API mid-restart) is
            # transient as far as this can tell, so it is reported without being remembered.
            logger.debug("could not read node metrics: %s", e)
            return None, None, f"node metrics could not be read: {e}"

        by_node = {(item.get("metadata") or {}).get("name"): item
                   for item in listed.get("items") or []}
        cpu = 0.0
        mem = 0
        missing = 0
        for name in node_names:
            usage = (by_node.get(name) or {}).get("usage") or {}
            # Both quantities or neither: half a node's reading is not a reading.
            cores, byts = usage.get("cpu"), usage.get("memory")
            if not cores or not byts:
                missing += 1
                continue
            node_cpu = parse_resource(cores)          # nanocores ("123456789n")
            node_mem = int(parse_resource(byts))      # working set ("1234Ki")
            # A zero working set is how an unparseable quantity arrives here, because
            # ``parse_resource`` answers 0 rather than raising. It cannot be a real reading:
            # a node running a kubelet has a resident set. CPU is not checked the same way
            # -- an idle node's nanocores can legitimately round to nothing.
            if node_mem <= 0:
                missing += 1
                continue
            cpu += node_cpu
            mem += node_mem
        if missing:
            # Not memoised: a node that just joined is reported seconds later, and this is
            # the one reason that resolves itself.
            return None, None, (f"metrics for {missing} of {len(node_names)} nodes were "
                                "not reported")
        return cpu, mem, None

    def _disk_and_results(self, node_names, service_node=None) -> dict:
        """The kubelet-measured ``disk`` and ``results`` fields, and the node they came from.

        Read over the ``nodes/proxy`` subresource — the same channel
        :func:`robovast.common.execution._check_static_cpu_manager` reads ``configz`` on.
        There is no alternative source: metrics-server publishes cpu and memory only, and
        the pod-request sum behind ``cpu_used`` cannot answer disk because
        ``ephemeral-storage`` is almost never requested — it would report a few hundred MB
        used on a node that is nearly full, a wrong answer that looks right.

        Memoised on its own longer TTL; called with ``_usage_lock`` held (see
        :meth:`LocalTransport.resource_usage`), so the memo needs no lock of its own.
        """
        now = time.monotonic()
        cached = self._disk_cache
        if cached is None or now - cached[0] >= self._DISK_CACHE_TTL:
            cached = (now, self._read_disk_and_results(sorted(node_names), service_node))
            self._disk_cache = cached
        return cached[1]

    def _read_disk_and_results(self, node_names, service_node=None) -> dict:
        """The SERVICE's node filesystem, and the results volume mounted on its pod.

        Node-local, not summed, and that is the whole point at scale. A cluster-wide sum
        answers a question nobody asks: with twenty nodes it reports tens of terabytes while
        the only disk that decides whether a campaign can be written is the one under the
        service's workspaces -- a hostPath on a stock RKE2, so pinned to a single node and
        invisible in the kubelet's per-volume stats. Summing also could not survive the
        scale it claimed to serve: all-or-nothing across the node set, with a
        ``_DISK_BUDGET_SECONDS`` of 5 against a ``_DISK_NODE_TIMEOUT`` of 2, meant twenty
        nodes blew the budget and reported no disk at all.

        ``used / (used + available)``, not ``capacityBytes``: reserved blocks are in the
        capacity and cannot be written, so ``available`` is the only honest denominator --
        the same correction the results meter carries.

        ``node.fs`` (nodefs) only, **not** summed with ``node.runtime.imageFs``: on a
        single-disk node those are two views of the SAME device, so summing doubles
        capacity and used alike -- the ratio survives but the labelled numbers become
        fiction.

        The results volume is on the service's pod, so the service's node answers both
        figures and the walk stops there; the other nodes are read only when the service's
        could not be identified. On a hostPath deployment the kubelet reports no per-volume
        figure and ``disk`` is that same filesystem, so no ``results`` field is drawn.
        """
        from .kube_client import (  # pylint: disable=import-outside-toplevel
            read_node_summary, nodefs_used_available)
        v1 = self._k8s()
        deadline = time.monotonic() + self._DISK_BUDGET_SECONDS
        summaries = {}
        fields = {}
        # The service's node first: it is the one figure that must survive a short budget.
        ordered = ([service_node] if service_node in node_names else []) + [
            n for n in node_names if n != service_node]
        if service_node is not None and service_node not in node_names:
            logger.debug("service node %s is not in the node list", service_node)
        for name in ordered:
            if time.monotonic() > deadline:
                break
            try:
                summary = read_node_summary(v1, name, self._DISK_NODE_TIMEOUT)
                summaries[name] = summary
                if name == service_node and "disk" not in fields:
                    used, available = nodefs_used_available(summary)
                    if used is not None:
                        fields["disk"] = DiskSpace(capacity_bytes=used + available,
                                                   used_bytes=used)
                        fields["disk_node"] = name
            except Exception as e:  # noqa: BLE001 - the other figure must still be answerable
                # The node is named in the log, never in a returned reason: that string
                # crosses the interface to a UI and an MCP client.
                logger.debug("kubelet stats/summary unavailable on node %s: %s", name, e)
                if name == service_node:
                    fields["unavailable"] = self._summary_read_reason(e)
            results = self._results_volume_usage(summaries)
            if results is not None:
                fields["results"] = results
            if name == service_node:
                break
        if "disk" not in fields and "unavailable" not in fields:
            fields["unavailable"] = (
                "no node filesystem for the service's node"
                if service_node else "the service's node could not be identified")
        return fields

    @staticmethod
    def _summary_read_reason(e) -> str:
        """Why a kubelet Summary read failed, in words that cross to a UI.

        Only 403 is an RBAC verdict. Answering "the service needs `nodes/proxy` get; run
        `vast service upgrade` to reconcile RBAC" for EVERY exception -- a timeout, a TLS
        refusal, a summary missing a key -- leaves the real reason no further than a
        logger.debug, and reconciling RBAC then returns the identical message. That is the
        failure mode that makes a guess worse than no reason at all: a reader cannot tell a
        fix that did not work from a diagnosis that was never right.
        """
        status = getattr(e, "status", None)
        if status == 403:
            return ("the service may not read `nodes/proxy` (403) — run "
                    "`vast service upgrade --no-restart` to reconcile RBAC")
        detail = str(getattr(e, "reason", None) or e).strip().splitlines()
        detail = (detail[0] if detail else e.__class__.__name__)[:120]
        prefix = f"HTTP {status}: " if status else f"{e.__class__.__name__}: "
        return f"the kubelet Summary API did not answer: {prefix}{detail}"

    @staticmethod
    def _results_volume_usage(summaries) -> "DiskSpace | None":
        """The service pod's results volume out of the kubelet's per-pod stats, or ``None``.

        **The denominator is ``used + available``, not ``capacityBytes``.** A volume with no
        size limit reports the whole node filesystem as its capacity -- a filesystem it
        shares with the images, the containers and every other directory -- so
        ``capacityBytes`` reads as headroom that is not there. ``availableBytes`` is what
        the filesystem will actually still take.

        ``None`` for a hostPath: the kubelet reports no per-volume stats for one, and the
        Disk meter already reports that filesystem.
        """
        from .service_deploy import RESULTS_VOLUME_NAME, SERVICE_NAME  # pylint: disable=import-outside-toplevel
        for summary in (summaries or {}).values():
            for pod in (summary.get("pods") or []):
                if not ((pod.get("podRef") or {}).get("name") or "").startswith(SERVICE_NAME + "-"):
                    continue
                for volume in (pod.get("volume") or []):
                    if volume.get("name") != RESULTS_VOLUME_NAME:
                        continue
                    used = volume.get("usedBytes")
                    available = volume.get("availableBytes")
                    if used is None or available is None:
                        return None
                    return DiskSpace(capacity_bytes=int(used) + int(available),
                                     used_bytes=int(used))
        return None

    def _scenario_job_tally(self) -> "tuple[int, int]":
        """``(running, pending)`` over every scenario-run Job in this namespace.

        Counted from **Jobs**, not pods, because a Job whose pod is not bound yet has no
        pod at all (see :func:`list_jobs_with_phase`) — and that is the state every cluster
        batch *starts* in. Reading pods therefore reported a freshly launched 25-run
        sweep as ``0/0`` while its whole queue waited for quota, which is exactly the
        "nothing is happening" the sidebar's jobs bar is there to contradict.

        Classification is delegated rather than repeated: ``list_jobs_with_phase`` is
        the single place that turns Jobs + pods into a phase, and the previous
        hand-rolled pod check here was a consumer that had drifted from it.
        ``pending`` folds in ``waiting`` (queued for quota) and ``blocked`` (cannot
        start on its own) — both are accepted work that is not executing; the
        per-campaign :class:`JobCounts` keeps them apart for the campaign view, which
        is where a blocked job needs its own treatment. ``completed``/``failed`` are
        past work and belong in neither.

        Namespace-scoped, unlike the CPU/memory figures above: those must stay
        cluster-wide because the nodes are shared, but the job tally answers "what is
        *this* service running", the same question :meth:`list_jobs` answers per
        campaign. A read failure propagates — a silently zero tally is the bug this
        method exists to fix.
        """
        from .cluster_execution import \
            list_jobs_with_phase  # pylint: disable=import-outside-toplevel
        phases = [listed.phase for listed in list_jobs_with_phase(
            self._k8s_batch(), self._k8s(), self.namespace, "jobgroup=scenario-runs")]
        return (sum(1 for p in phases if p == "running"),
                sum(1 for p in phases if p in ("pending", "waiting", "blocked")))

    # -- helpers ------------------------------------------------------------

    def _cluster_config(self):
        from .cluster_setup import get_cluster_config
        if not self._config_name:
            raise ValueError(
                "cluster config not configured (ROBOVAST_CLUSTER_CONFIG_NAME); "
                "the service must be deployed by 'vast cluster setup'")
        cfg = get_cluster_config(self._config_name)
        if self._config_kwargs:
            cfg.restore_from_setup_kwargs(self._config_kwargs)
        return cfg

    def _load_kube(self):
        from .kube_client import load_kube_config
        load_kube_config(context=self.kube_context)

    def _k8s(self):
        from kubernetes import client
        self._load_kube()
        return client.CoreV1Api()

    def _k8s_batch(self):
        from kubernetes import client
        self._load_kube()
        return client.BatchV1Api()

    def _k8s_custom(self):
        """For ``metrics.k8s.io``, which has no generated typed client of its own."""
        from kubernetes import client
        self._load_kube()
        return client.CustomObjectsApi()

    # -- launch hooks (see LocalTransport.create_campaign) -------------------

    def _guard_new_campaign(self) -> None:
        """Cluster campaigns run in parallel.

        The local guard exists because Docker is single-flight; here each campaign
        is an I/O-bound driver thread whose compute lives in Kubernetes Jobs, so
        many run at once. Everything they touch is campaign-scoped: the
        container-runner factory is a ContextVar, ``controller.log`` is filtered to
        its worker thread, and each aux pod / result prefix is keyed by campaign id.
        """
        return None

    def _build_backend(self, state):
        from . import pod_access
        from .kubernetes_backend import KubernetesBackend
        # The campaign's data-plane token, minted here because this process holds the
        # secret the gate verifies. A backend built for no campaign (a share upload) carries
        # none: nothing it launches needs one.
        campaign_id = getattr(state, "campaign_id", None)
        token = self.scoped_token(pod_access.campaign_scope(campaign_id)) if campaign_id else ""
        return KubernetesBackend(cluster_config=self._cluster_config(),
                                 namespace=self.namespace,
                                 kube_context=self.kube_context,
                                 state=state,
                                 admission=self._admission_controller(),
                                 data_token=token)

    def _admission_controller(self):
        """The process-wide admission queue, built once.

        On the service because it is the one object with process lifetime that every campaign
        thread reaches, and because the queue only orders correctly if there is exactly one of
        it: a controller per campaign would order by whichever thread asked first, which is
        the behaviour it exists to replace.

        **Its own lock, never ``_usage_lock``.** That one is held across a resource reading
        that talks to every kubelet in turn (see ``_DISK_BUDGET_SECONDS``), and sharing it
        would let one unresponsive node block every campaign's job creation.

        **Raises rather than falling back.** Nothing gates job creation behind this, so a
        ``None`` here would mean creating a campaign's entire plan -- for a one-run-per-job
        sweep, upwards of a thousand Jobs -- in one unthrottled loop against a cluster sized
        for a few dozen. A service that cannot measure the cluster must refuse to submit
        to it.
        """
        with self._admission_lock:
            if self._admission is None:
                from kubernetes import client

                from .cluster_capacity import ClusterBudgetProvider
                from .kube_client import load_kube_config
                from .node_admission import AdmissionController

                def _core():
                    # Per call, not cached: the config load is idempotent and a client
                    # held across a service's lifetime outlives token rotation.
                    load_kube_config(self.kube_context)
                    return client.CoreV1Api()

                # The cluster config comes along so an autoscaling deployment is sized
                # by what it can become rather than by the nodes it currently has --
                # see ClusterBudgetProvider._declared_total.
                # The node pool campaign jobs are confined to. Counted here AND stamped
                # on every job pod: filtering capacity alone would leave kube-scheduler
                # free to place outside the pool, and stamping alone would have admission
                # promise room on nodes the pods may not use.
                from .node_placement import job_node_pool

                self._admission = AdmissionController(ClusterBudgetProvider(
                    _core, node_selector=job_node_pool(),
                    cluster_config=self._cluster_config(),
                    kube_context=self.kube_context))
            return self._admission

    def _run_options(self, request):
        from robovast.execution.backends import RunOptions

        # postprocess travels in the options (not the process env): one process
        # drives many campaigns, and an env var could not tell them apart.
        # gui stays False unconditionally — a show_gui request never reaches here,
        # _admit_show_gui having refused it.
        return RunOptions(gui=False,
                          postprocess=bool(request.postprocess),
                          upload_to_share=bool(getattr(request, "upload_to_share", False)),
                          namespace=self.namespace)

    def _postprocess_in_process(self) -> bool:
        """False: the builder chains postprocessing before its upload.

        ``_chain_postprocessing`` runs inside the builder (rosbag→CSV as a Job, then
        ``data.db`` here) *before* ``finalize_campaign``, so the derived data rides
        the campaign's existing upload instead of needing one of its own.
        """
        return False

    @contextlib.contextmanager
    def _aux_runner_context(self, tag, project, *, hold=False, should_stop=None):
        """The container-runner factory for this thread, over *tag*'s span.

        Entered inside the thread that composes, so the factory (a ContextVar) is scoped to
        exactly the composition that reads it — concurrent campaigns never clobber each
        other's aux target, and it is reset on the way out because a request thread is
        reused while a worker thread is not.

        Installed **unconditionally**, and it creates a container only when something asks
        for one: nothing here reads the project to decide whether a helper image will be
        wanted. Deciding that here is a second copy of what composition enumerates — a
        variation, an ``execution.generate`` generator, the simulator backend's input-files
        query — and whatever the copy does not cover refuses a campaign while composing, on
        a container it declared. A span that asks for nothing still creates nothing, which is
        what deciding in advance was for.

        The two spans differ only in who owns the container's death — see
        :meth:`LocalTransport._aux_runner_context`. A campaign's pods are deleted here;
        a held one is released to the exec manager's reaper.

        *should_stop* ends the pod's ready wait for a campaign that was stopped while it
        was waiting. That wait is the longest thing composition does on this lane — a
        helper image is pulled inside it — so without it a stop is not seen until the pull
        either finishes or times out, minutes later. A held span has no campaign to stop.
        """
        del project
        from robovast.common.config_generation import set_container_runner_factory
        from robovast.service.world_query import _reset_factory

        from .container_runner import AuxPodSession

        if hold:
            with self._held_aux_runners(tag) as factory:
                token = set_container_runner_factory(factory)
                try:
                    yield
                finally:
                    _reset_factory(token)
            return
        with AuxPodSession(tag, self.namespace, core_v1=self._k8s(),
                           kube_context=self.kube_context,
                           pull_secret=self._registry_pull_secret(),
                           on_pending=_aux_pending_logger(tag),
                           should_stop=should_stop,
                           **self._aux_staging_kwargs()) as session:
            token = set_container_runner_factory(session.runner_factory())
            try:
                yield
            finally:
                _reset_factory(token)

    @contextlib.contextmanager
    def _held_aux_runners(self, tag):
        """Yield a factory that holds an aux container through the exec manager on demand.

        The manager already owns every held container's lifetime — idle reap, a hard
        deadline baked into the pod, an LRU cap and a stray sweep after a restart — so this
        adds no second policy. A spec is held as a *query* slot because that policy is
        already this one: a warm image and nothing else, since a runner mirrors its
        workspace around each command and leaves nothing behind between them.

        Held when the factory is first called for a spec, like the campaign span's pods and
        for the same reason: an authoring loop that composes a sweep needing no helper image
        must not hold one, and nothing before the composition knows which it is.

        On the way out the slots are released, not stopped: the next preview of the same
        project reuses a warm pod, and two previews running at once cannot destroy each
        other's.
        """
        from robovast.service.container_exec import ExecSpec, container_name

        from .container_runner import AUX_HOLD_LIMIT_S, ClusterContainerRunner
        from .kube_exec_lane import HELD_CONTAINER
        slots = {}
        # For the reason ``AuxPodSession`` takes one: nothing in the contract says two
        # runners cannot be asked for at once, and two holds of one identity is a second
        # pod started over the first.
        lock = threading.Lock()

        def hold(spec):
            name = spec.container_name()
            with lock:
                if name not in slots:
                    # The image is what makes the pod worth reusing, and the project is
                    # what keeps two of them apart; the pod holds nothing else that could
                    # differ.
                    identity = ("aux", tag, name, spec.image)
                    held = ExecSpec(image=spec.image, command="",
                                    config_dir=tempfile.mkdtemp(prefix="robovast_aux_hold_"),
                                    env=dict(spec.env or {}), config_name=str(tag),
                                    image_identity=spec.image, aux_spec=spec)
                    slots[name] = self._exec_manager.hold(held, identity, AUX_HOLD_LIMIT_S)
                return slots[name]

        def rehold(spec):
            """``AuxPodSession.replace`` for this lane: drop the dead slot, hold again.

            Stopped rather than released, because release starts an idle window and the
            next hold of the same identity would reuse the record — which names the
            container that just went away.
            """
            name = spec.container_name()
            with lock:
                slot = slots.pop(name, None)
            if slot is not None:
                self._exec_manager.stop(slot)
            return container_name(hold(spec))

        try:
            def factory(spec):
                return ClusterContainerRunner(
                    spec, container_name(hold(spec)), self.namespace,
                    self._k8s(), stage_dir=self.staged_dir,
                    kube_context=self.kube_context,
                    container=HELD_CONTAINER, reprovision=rehold)

            yield factory
        finally:
            for slot in slots.values():
                self._exec_manager.release_hold(slot)

    def _aux_staging_kwargs(self) -> dict:
        """The data-plane wiring an aux pod's workspace transfer needs.

        The service's own staging: where a runner's workspace lives on this disk, how a
        pod's slot is dropped, and the token a pod is given to reach its slot -- the same
        three things an image build's context and an exec pod's ``/config`` use.
        """
        return {
            "stage_dir": self.staged_dir,
            "discard_staged": self.discard_staged,
            "token_for": self.scoped_token,
        }

    def list_jobs(self, campaign_id: str) -> ListJobsResponse:
        """List the Kubernetes Jobs a campaign has in flight, with live status.

        Selects Jobs by the campaign label the backend stamps on them and classifies each
        with :func:`list_jobs_with_phase` — the same pod-accurate logic the CLI
        monitor's aggregate counter uses, so the two never drift. ``display_name``
        is the pod template's ``job-name-full`` annotation (``<batch>-job-<index>``)
        for a readable label.

        **Two jobgroups, one listing and one selector.** The campaign's trials
        (``scenario-runs``) and its postprocessing conversion (``postprocessing``) are
        separate jobgroups, and both are the campaign doing its own work — a phase whose only
        job is not listed shows a reader an empty list while the cluster is busy on their
        behalf. A set-based requirement fetches them together because this is polled per live
        campaign every couple of seconds, and a second selector would double both the Job
        listing and the pod listing behind it for a row that is there at most once.

        **Neither probes nor postprocessing are tallied.** Both carry the campaign's labels —
        they are real work holding real capacity, and every selector that counts or cleans up
        has to keep seeing them — so they appear here, marked with their
        :class:`~robovast.service.interface.JobKind` and named for what they are. The counts
        stay the campaign's runs alone, because a reader takes them as facts about *runs*:
        see :attr:`~robovast.service.interface.JobCounts.calibration`.
        """
        from .cluster_execution import _label_safe_campaign, list_jobs_with_phase
        from .postprocess_job import POSTPROCESS_JOBGROUP
        label = (f"jobgroup in (scenario-runs,{POSTPROCESS_JOBGROUP}),"
                 f"campaign-id={_label_safe_campaign(campaign_id)}")
        # Phase is pod-accurate: a Job whose pod is still Pending (unscheduled or
        # image-pulling) reports pending, not running.
        usage_by_job, metrics_reason = self._pod_metrics()
        jobs = [
            JobSummary(job_name=listed.job.metadata.name, status=listed.phase,
                       kind=self._job_kind(listed.job),
                       display_name=self._job_display_name(campaign_id, listed.job),
                       detail=listed.detail, node=listed.node,
                       started_at=self._job_started_at(listed.job),
                       # Usage only while it runs. A sample outlives the pod that produced it,
                       # so a job that has just finished still has one, and a completed row
                       # carrying it reads as a job still burning cores.
                       usage=(self._job_usage(
                           listed.job, usage_by_job.get(listed.job.metadata.name))
                           if listed.phase == "running" else None))
            for listed in list_jobs_with_phase(
                self._k8s_batch(), self._k8s(), self.namespace, label)]
        # Planned jobs are the campaign's own by construction: probes queue under a separate
        # owner (see ``_PROBE_OWNER_SUFFIX``), so ``states(campaign_id)`` never yields one.
        jobs.extend(self._planned_jobs(campaign_id, {j.job_name for j in jobs}))
        runs = [j for j in jobs
                if j.kind not in (JobKind.CALIBRATION, JobKind.POSTPROCESSING)]
        counts = JobCounts(
            running=sum(1 for j in runs if j.status == "running"),
            pending=sum(1 for j in runs if j.status == "pending"),
            waiting=sum(1 for j in runs if j.status == "waiting"),
            completed=sum(1 for j in runs if j.status == "completed"),
            failed=sum(1 for j in runs if j.status == "failed"),
            blocked=sum(1 for j in runs if j.status == "blocked"),
            calibration=sum(1 for j in jobs if j.kind == JobKind.CALIBRATION),
            postprocessing=sum(1 for j in jobs if j.kind == JobKind.POSTPROCESSING),
            total=len(runs))
        return ListJobsResponse(jobs=jobs, counts=counts,
                                metrics_unavailable=metrics_reason)

    def _pod_metrics(self) -> tuple:
        """Live per-container cpu/memory for every campaign's job pods: ``(by_job, reason)``.

        ``by_job`` maps Job name to ``{container: (cores, bytes)}``, keyed off the pod's own
        ``job-name`` label. **One list for the whole namespace**, never one per campaign: every
        campaign's Jobs carry the same ``jobgroup``, so a single read serves all of them and a
        second open campaign card costs nothing. A Job that is not the caller's is simply never
        looked up.

        Joined by label rather than by pod name because that is what this listing has: the
        alternative is a pod listing of its own, a second call per window to learn a mapping
        the metrics item already states.

        Held for the window the samples themselves report, clamped by ``_POD_METRICS_TTL_*``.
        metrics-server resamples on its own schedule, so a read faster than that window is a
        round trip spent to be handed the same numbers back. The *shortest* window in the batch
        is taken, so no sample is served past its own life.

        Failures are memoised for ``_METRICS_ABSENT_TTL`` when they are settled facts about the
        cluster, and reported without being remembered when they are not -- see
        :func:`_metrics_failure_reason`.

        **Never queues behind another caller's read.** A refresh already in flight yields the
        snapshot in hand, because this decorates a listing that is polled every couple of
        seconds: a job list must not wait on an aggregated API to say which jobs exist.

        Requires ``metrics.k8s.io/pods`` get+list in the service's usage ClusterRole (see
        ``service_deploy._service_rbac_manifests``). A deployment whose RBAC predates that
        grant keeps working -- it gets a 403, and the reason names the command that fixes it.
        """
        from .kube_client import (CONNECT_TIMEOUT_SECONDS,  # pylint: disable=import-outside-toplevel
                                  parse_duration, parse_resource)
        from .postprocess_job import POSTPROCESS_JOBGROUP  # pylint: disable=import-outside-toplevel

        if not self._pod_metrics_lock.acquire(blocking=False):
            held = self._pod_metrics_snapshot
            return (held[1] if held is not None else {}), None
        try:
            now = time.monotonic()
            held = self._pod_metrics_snapshot
            if held is not None and now < held[0]:
                return held[1], None
            remembered = self._pod_metrics_absent
            if remembered is not None and now - remembered[0] < self._METRICS_ABSENT_TTL:
                return {}, remembered[1]
            try:
                listed = self._k8s_custom().list_namespaced_custom_object(
                    "metrics.k8s.io", "v1beta1", self.namespace, "pods",
                    label_selector=f"jobgroup in (scenario-runs,{POSTPROCESS_JOBGROUP})",
                    # A (connect, read) pair rather than a scalar, for the reason spelled out
                    # in ``_measured_cpu_mem``.
                    _request_timeout=(CONNECT_TIMEOUT_SECONDS, self._METRICS_TIMEOUT))
            except Exception as e:  # noqa: BLE001 - a job listing must still be answerable
                settled = _metrics_failure_reason(e, "pods")
                if settled is not None:
                    self._pod_metrics_absent = (time.monotonic(), settled)
                    return {}, settled
                logger.debug("could not read pod metrics: %s", e)
                return {}, f"pod metrics could not be read: {e}"
            by_job, window = {}, None
            for item in listed.get("items") or []:
                labels = (item.get("metadata") or {}).get("labels") or {}
                job = labels.get("batch.kubernetes.io/job-name") or labels.get("job-name")
                containers = {}
                for container in item.get("containers") or []:
                    usage = container.get("usage") or {}
                    cores, byts = usage.get("cpu"), usage.get("memory")
                    # Both quantities or neither, as in ``_measured_cpu_mem``: half a
                    # container's reading is not a reading.
                    if cores and byts:
                        containers[container.get("name")] = (parse_resource(cores),
                                                             int(parse_resource(byts)))
                if job and containers:
                    by_job[job] = containers
                sample = parse_duration(item.get("window"))
                if sample is not None and (window is None or sample < window):
                    window = sample
            ttl = min(max(window or self._POD_METRICS_TTL_DEFAULT,
                          self._POD_METRICS_TTL_MIN), self._POD_METRICS_TTL_MAX)
            self._pod_metrics_snapshot = (time.monotonic() + ttl, by_job)
            self._pod_metrics_absent = None
            return by_job, None
        finally:
            self._pod_metrics_lock.release()

    @staticmethod
    def _job_usage(job, measured) -> "JobUsage | None":
        """One job's :class:`JobUsage` from its *measured* ``{container: (cores, bytes)}``.

        ``None`` when nothing was measured: a record of zeros would read as an idle job rather
        than as an unmeasured one.

        The denominators are summed over **exactly the containers the measurement covers**, so
        numerator and denominator describe the same set. A metrics-server that reported a
        different container set than the template declares would otherwise put one container's
        usage under a whole pod's ceiling.
        """
        from .kube_client import workload_resources  # pylint: disable=import-outside-toplevel

        if not measured:
            return None
        given = workload_resources(getattr(getattr(job, "spec", None), "template", None),
                                   measured.keys())
        return JobUsage(cpu_cores=sum(v[0] for v in measured.values()),
                        memory_bytes=sum(v[1] for v in measured.values()),
                        cpu_request=given["cpu_request"], cpu_limit=given["cpu_limit"],
                        memory_request_bytes=given["memory_request"],
                        memory_limit_bytes=given["memory_limit"])

    @staticmethod
    def _job_started_at(job) -> "float | None":
        """When the Job started, epoch seconds, or ``None`` if the cluster has not said.

        The *Job's* start, which Kubernetes stamps before the pod is scheduled and before its
        inputs are staged. That makes it the answer to "how long has this trial been going"
        rather than "how long has it been executing", and it is the only one of the two that
        exists for a job still pending or blocked -- where the question is worth asking.
        """
        started = getattr(getattr(job, "status", None), "start_time", None)
        return started.timestamp() if started is not None else None

    def _planned_jobs(self, campaign_id, created) -> list:
        """The campaign's admitted-but-not-yet-created jobs, as ``waiting`` summaries.

        These have **no Kubernetes object at all**, so unlike every other status here they
        cannot come from a listing -- the controller is the only thing that knows they
        exist. Reporting them is what keeps ``waiting`` meaning "queued for capacity"
        rather than silently becoming a count that is always zero.

        Never *builds* the controller. This is a read path -- a job listing behind a web
        UI -- and a cluster that cannot be measured must degrade to "nothing planned"
        rather than raise; refusing is the submit path's job. If no campaign has submitted
        yet there is nothing planned either way, so the two answers agree.
        """
        from .node_admission import PLANNED

        with self._admission_lock:
            admission = self._admission
        if admission is None:
            return []
        return [JobSummary(job_name=name, status="waiting", display_name=None,
                           detail="queued for cluster capacity")
                for name, state in sorted(admission.states(campaign_id).items())
                if state == PLANNED and name not in created]

    @staticmethod
    def _job_kind(job) -> str:
        """Which kind of Job this is, from the labels the backend stamps.

        The ``jobgroup`` separates postprocessing from the campaign's batch; within the
        batch, an unlabelled Job is the campaign's own work, because
        :data:`~.manifests.JOB_KIND_LABEL` is stamped by
        :func:`~.kubernetes_backend.probe_manifest` and by nothing else, and a Job created
        before it existed is a run.
        """
        from .postprocess_job import POSTPROCESS_JOBGROUP
        try:
            labels = job.metadata.labels or {}
        except AttributeError:
            return JobKind.RUN
        if labels.get("jobgroup") == POSTPROCESS_JOBGROUP:
            return JobKind.POSTPROCESSING
        return (JobKind.CALIBRATION
                if labels.get(JOB_KIND_LABEL) == CALIBRATION_JOB_KIND else JobKind.RUN)

    @classmethod
    def _job_display_name(cls, campaign_id, job) -> "str | None":
        """The Job's ``job-name-full`` pod annotation, minus the campaign prefix.

        A postprocessing Job carries no such annotation — it is not one of the batch's
        indexed jobs — and its own name is the campaign id with a hash on it, which tells a
        reader nothing they are not already looking at. Named for the work instead: this is
        the conversion of the campaign's rosbags, run in the campaign's execution image
        because only there do its custom message types deserialize.
        """
        if cls._job_kind(job) == JobKind.POSTPROCESSING:
            return "rosbag conversion"
        try:
            full = job.spec.template.metadata.annotations.get("job-name-full")
        except AttributeError:
            return None
        if full and full.startswith(f"{campaign_id}-"):
            return full[len(campaign_id) + 1:]
        return full

    def _new_job_log_tail(self, campaign_id: str, job_name: str):
        """This lane's tail reads a pod's containers, not a job dir's files."""
        from .cluster_execution import PodLogTail
        return PodLogTail()

    def get_job_log(self, campaign_id: str, job_name: str, offset: int = 0) -> LogChunk:
        """Serve a Job's log from byte *offset* onward, live from its pod or from the campaign.

        Finds the Job's pod by the auto-added ``job-name`` label and streams *all* of
        its containers' logs merged into one stream (the main ``robovast`` container
        plus any sim/SUT sidecars; see :class:`PodLogTail`). Reads are
        incremental: a cached tail keeps the full assembled text so the byte offset
        still maps onto it, but each poll only pulls the delta from the kube API
        rather than the whole log. A pod that is gone is not an error: the log comes from
        the campaign directory instead (:meth:`_archived_job_log`), which is the ordinary
        state of every finished job.

        A ``Pending`` pod is read like any other, and must be: the sim/SUT sidecars are
        native sidecars, so kubelet runs them *during* the init phase, while the pod is
        still Pending. They are already logging -- and a simulator that cannot load its
        world says so there and then keeps the pod Pending forever. Short-circuiting on
        the phase, as this did, threw away exactly the output that explains the hang.
        A container with no log yet is handled a layer down, where ``PodLogTail._fetch``
        swallows the API's 400/404 and contributes nothing.
        """
        from kubernetes import client

        from .cluster_execution import _label_safe_campaign
        core = self._k8s()
        # Campaign + Job name, with no jobgroup term: the pair already identifies one pod,
        # and adding the group would decide which of the campaign's own jobs may be read
        # here. Every row :meth:`list_jobs` shows has to open, including its postprocessing
        # conversion -- a row whose log 404s is worse than no row.
        label = f"campaign-id={_label_safe_campaign(campaign_id)},job-name={job_name}"
        pods = core.list_namespaced_pod(self.namespace, label_selector=label)
        if not pods.items:
            return self._archived_job_log(campaign_id, job_name, offset)
        pod = pods.items[0]
        tail = self._job_log_tail(campaign_id, job_name)
        try:
            with tail.lock:
                terminal = tail.read(core, pod, self.namespace, time.time())
                text, next_offset = tail.merged.slice_from(offset)
        except client.exceptions.ApiException as e:
            if e.status == 404:
                return self._archived_job_log(campaign_id, job_name, offset)
            raise
        return LogChunk(text=text, next_offset=next_offset, eof=terminal)

    def _archived_job_log(self, campaign_id: str, job_name: str, offset: int) -> LogChunk:
        """A finished job's log, read from the campaign directory instead of its pod.

        A pod is deleted when its Job is cleaned up, so for most of a campaign's life the
        live source above is gone while the same output is in the campaign: the pod's
        uploader delivers ``/out`` as it ends, which is also what makes an already-finished
        run of a still-running campaign readable at all. Without this the log of every run
        but the executing one is a 404.

        Merged and tagged through the same :class:`MergedLogBuffer` as both live tails, so a
        reader sees one stream with the same ``[container]`` prefixes rather than a
        differently-shaped archive. The files are complete and immutable here, so ordering
        is per file rather than per poll, and the whole buffer is built on each call --
        there is no delta to track, and ``eof`` is unconditionally true.

        Raises:
            KeyError: When the campaign has no such job, or its artifacts were never
                delivered (a run killed before its uploader could). Reported as absent
                rather than as an empty log, which would read as a run that said nothing.
        """
        import yaml

        from robovast.common.execution import (
            JOB_LINKS_MANIFEST_REL, resolve_job_artifact_rel)
        from robovast.common.log_tail import (MAIN_LOG, MergedLogBuffer,
                                              container_of_log_file, is_sidecar_log,
                                              tag_width)

        campaign_dir = self.campaign_dir(campaign_id)
        try:
            manifest = (campaign_dir / JOB_LINKS_MANIFEST_REL).read_bytes()
        except FileNotFoundError:
            raise KeyError(
                f"campaign {campaign_id!r} has no job-link manifest: no archived log for "
                f"job {job_name!r}") from None
        try:
            job_rel = resolve_job_artifact_rel(yaml.safe_load(manifest) or {}, job_name)
        except FileNotFoundError as e:
            raise KeyError(f"{e} in campaign {campaign_id!r}") from None

        log_dir = campaign_dir / job_rel / "logs"
        names = sorted(p.name for p in log_dir.iterdir()) if log_dir.is_dir() else []
        # Main container first, then the sidecars in name order -- the local lane's order,
        # so the same job does not read differently depending on which lane served it.
        files = [n for n in names if n == MAIN_LOG]
        files += [n for n in names if is_sidecar_log(n)]
        if not files:
            raise KeyError(
                f"job {job_name!r} of campaign {campaign_id!r} uploaded no logs")

        multi = len(files) > 1
        containers = [container_of_log_file(n) for n in files]
        width = tag_width(containers) if multi else 0
        entries = []
        for file_order, (name, container) in enumerate(zip(files, containers)):
            raw = (log_dir / name).read_bytes()
            lines = raw.decode("utf-8", errors="replace").split("\n")
            # A file ending in a newline splits with a trailing "" that is not a line. Only
            # the last one: a blank line inside the log is the container's own output.
            if lines and lines[-1] == "":
                lines.pop()
            for line_order, line in enumerate(lines):
                entries.append(((file_order, line_order), container, line))

        merged = MergedLogBuffer()
        merged.append(entries, multi=multi, width=width)
        text, next_offset = merged.slice_from(offset)
        return LogChunk(text=text, next_offset=next_offset, eof=True)

    # -- image builds (in-cluster BuildKit Job) -----------------------------

    def _image_build_state(self) -> dict:
        state = getattr(self, "_image_builds_by_id", None)
        if state is None:
            state = {}
            self._image_builds_by_id = state
        return state

    def _build_context(self, request):
        """Resolve (specs, project_dir, cfg, registry) for a build request.

        *specs* maps container name → :class:`BuildSpec`: a campaign may build several
        images. Raises ``ValueError`` (→ 400) with an actionable message when nothing
        needs building, a container's package lists are invalid, or the deployment has
        no registry configured (registry details live only in the cluster config).
        """
        from robovast.common.common import load_config
        from robovast.common.config import validate_config
        from robovast.service.image_build import extract_build_specs, validate_build_spec
        project = self._resolve_project(request.workspace_id, request.config_path)
        campaign_config = validate_config(load_config(project.config_path))
        specs = extract_build_specs(campaign_config,
                                    Path(project.config_path).parent)
        if not specs:
            raise ValueError(
                "nothing to build: no container adds system_packages, "
                "python_packages or ros_packages, so every image is used as declared")
        wanted = getattr(request, "container", None)
        if wanted:
            if wanted not in specs:
                raise ValueError(
                    f"container '{wanted}' builds no image; the ones that do are: "
                    + ", ".join(sorted(specs)))
            specs = {wanted: specs[wanted]}
        project_dir = Path(project.config_path).resolve().parent
        for name, spec in specs.items():
            problems = validate_build_spec(spec, project_dir)
            if problems:
                raise ValueError(f"invalid execution.containers.{name}:\n  - "
                                 + "\n  - ".join(problems))
        cfg = self._cluster_config()
        registry = self._images.registry(require=False)
        if not registry.enabled():
            raise ValueError(f"cannot build an image: {registry.why_disabled()}")
        return project, campaign_config, specs, project_dir, cfg, registry

    @property
    def _images(self):
        """This lane's image store: the registry this deployment pushes to.

        Overriding this one factory is what makes every image question on this lane correct,
        including the ones nobody remembered to override before — ``_exec_image`` asked the
        *local* docker daemon from inside a service pod that has none, and reported every
        built image as unbuilt.
        """
        store = getattr(self, "_image_store", None)
        if store is None:
            from .registry_image_store import RegistryImageStore
            store = RegistryImageStore(self.namespace, self._cluster_config, self._k8s)
            self._image_store = store
        return store

    def build_image(self, request):
        from robovast.service.image_build import primary_build_ref
        self._admit_storage("build an image")
        (_project, _cc, specs, project_dir, cfg, registry) = \
            self._build_context(request)
        refs = {name: self._start_cluster_build(spec, project_dir, cfg, registry)
                for name, spec in specs.items()}
        return primary_build_ref(refs)

    def _start_cluster_build(self, spec, project_dir, cfg, registry):
        """Core (idempotent) launch shared by build_image + the campaign preflight."""
        from robovast.common.execution import BUILD_IMAGE_PREFIX, resolve_build_base_image
        from robovast.service.image_build import cache_scope, generate_dockerfile
        from robovast.service.interface import ImageBuildRef, ImageBuildStatus

        from robovast.common.errors import ImageBuildFailed

        from . import pod_access
        from .buildkitd_deploy import BUILDKITD_NAME, buildkitd_address, buildkitd_ready
        from .cluster_image_build import (build_job_manifest, cache_image_ref, context_slot,
                                          stage_context)

        # One resolution, from the store, so a submitted build and a later "is it there?"
        # cannot disagree about what this image is called. Deriving it separately is how a
        # built image comes to be reported as unbuilt.
        found = self._images.ref_for(spec, project_dir)
        image_ref, image_hash, build_id = found.ref, found.image_hash, found.build_id
        symbolic = f"{BUILD_IMAGE_PREFIX}{spec.tag}"
        state = self._image_build_state()

        # Before anything else, and on every path (a cache hit included, or a project
        # that only ever hits the cache would never sweep): retire the contexts no
        # status poll got to — a build submitted with --no-wait and never polled, or
        # one whose service restarted mid-build.
        self._sweep_build_contexts()

        # Idempotent, and the registry is asked first: a pushed manifest for this exact
        # input hash is durable proof the image exists, where the Job that produced it is
        # deleted after ttlSecondsAfterFinished (1 h) and the in-process record dies with
        # the service. Without this the same bit-identical image was rebuilt and re-pushed
        # an hour later.
        if self._registry_has_image(found):
            status = ImageBuildStatus(build_id=build_id, tag=spec.tag, phase="cached",
                                      done=True, cached=True, image_ref=symbolic,
                                      digest=image_hash)
            state[build_id] = {"tag": spec.tag, "image_ref": image_ref,
                               "hash": image_hash, "status": status}
            # Nothing will be built, so this is the only chance to warm. A cache hit is the
            # coldest case there is: the image may have been pushed weeks ago, by a service
            # that has restarted and onto a node that has rebooted since.
            self._warm(image_ref)
            return ImageBuildRef(build_id=build_id, tag=spec.tag, cached=True)

        # The Job is still consulted, but only for the case the registry cannot answer:
        # a build already in flight (nothing pushed yet) that this caller should join
        # rather than duplicate.
        existing = self._existing_build_job(build_id)
        if existing == "succeeded":
            status = ImageBuildStatus(build_id=build_id, tag=spec.tag, phase="cached",
                                      done=True, cached=True, image_ref=symbolic,
                                      digest=image_hash)
            state[build_id] = {"tag": spec.tag, "image_ref": image_ref,
                               "hash": image_hash, "status": status}
            # Nothing will be built, so this is the only chance to warm. A cache hit is the
            # coldest case there is: the image may have been pushed weeks ago, by a service
            # that has restarted and onto a node that has rebooted since.
            self._warm(image_ref)
            return ImageBuildRef(build_id=build_id, tag=spec.tag, cached=True)
        if existing == "running":
            return ImageBuildRef(build_id=build_id, tag=spec.tag, cached=False)
        if existing == "failed":
            # A retry after a failed build has the same content hash, hence the same
            # build_id, and the spent Job lingers for ttlSecondsAfterFinished (1 h).
            # Nothing is salvageable from it, and leaving it in place made the retry die
            # on an unhandled 409 AlreadyExists from create_namespaced_job — a 500 with
            # no message, for what is a perfectly reasonable "try again".
            self._delete_build_job(build_id)

        # Before staging anything: a build cannot happen without the daemon, and every step
        # from here on costs something (a full copy of the project tree, an upload, a Job).
        # Refusing loudly here is also the only way this failure gets named -- past this point
        # it surfaces as a gRPC dial error inside the build log, which reads like the project's
        # own build configuration being wrong and sends whoever hit it to edit a `.vast`.
        if not buildkitd_ready(self.namespace):
            raise ImageBuildFailed(
                f"the shared build daemon ({BUILDKITD_NAME}) has no ready pod in namespace "
                f"'{self.namespace}', so there is nothing to build with. Images are built by a "
                f"long-lived BuildKit daemon rather than per build, so this is a cluster fault "
                f"and not a problem with this project. Check it with "
                f"`kubectl -n {self.namespace} get deploy/{BUILDKITD_NAME}`; "
                f"`vast service upgrade` re-applies it if it is missing.")

        # And here for the same reason, one step further on: a build whose push will be
        # refused is a build that installs every package and then fails at its last step.
        # Nothing upstream could have said so -- `can_build_images` answers whether this
        # deployment has a registry configured, which is a property of how it was set up,
        # and the cache probe above is a manifest read, which a registry may serve while
        # refusing to receive one. So the credential is asked directly, once, before the
        # context is copied and staged.
        #
        # Only a registry that answered *and* refused stops a build; an unreachable one
        # does not (see `push_refused`). The verdict is the one the post-build classifier
        # already gives this failure -- infrastructure, not a `build:` entry to edit --
        # just arrived before the compute.
        if self._images.push_refused(image_ref):
            raise ImageBuildFailed(
                f"this deployment's image registry refused the credential it would push "
                f"'{spec.tag}' with, so the build would fail at its last step after "
                f"installing everything. That is an infrastructure problem and not "
                f"fixable by editing `build:`: the push Secret is minted at "
                f"`vast cluster setup` and re-read by `vast service upgrade`, so a "
                f"credential rotated since this deployment was set up needs one of those. "
                f"`vast doctor -n {self.namespace}` says which.")

        # Registered *before* staging so a concurrent build's context sweep can see
        # this build is in flight — its context sits in a staged slot for the whole
        # copy, while its Job does not exist yet.
        status = ImageBuildStatus(build_id=build_id, tag=spec.tag, phase="pending",
                                  image_ref=symbolic, digest=image_hash)
        # The spec rides along so a failure can be classified against what was actually
        # asked for: without it every missing distribution looks like a bad entry in
        # build.python_packages, including the ones the base image should have carried.
        state[build_id] = {"tag": spec.tag, "image_ref": image_ref,
                           "hash": image_hash, "status": status, "spec": spec}

        # Everything up to a created Job is undone on failure: the in-flight record
        # holds the sweep back, so a submit that dies here (staging error, rejected
        # Job) would otherwise strand its context for as long as the service lives.
        try:
            # Stage the context (project dir + generated Dockerfile) for the Job to fetch.
            base_ref = (spec.base_image or registry.base_experiment_image
                        or resolve_build_base_image())
            # Record the *resolved* base, not the declared one: spec.base_image is often
            # empty (the cluster default or the framework image supplied it), and an
            # error that cannot name the image it built on is the harder one to act on.
            # Guarded because this only sharpens a future error message -- failing the
            # submit itself over it would trade something that matters for something
            # that does not.
            if dataclasses.is_dataclass(spec):
                state[build_id]["spec"] = dataclasses.replace(spec, base_image=base_ref)
            # The same resolution the hash was taken over, so the Dockerfile installs the
            # commit rather than the branch. Rendering without it left the build installing
            # whatever the ref pointed at when the Job ran -- which the campaign's record then
            # could not name, the failure the resolution exists to prevent.
            dockerfile = generate_dockerfile(spec, project_dir, base_ref,
                                             resolved_vcs=self._images.resolve_vcs(spec))
            slot = context_slot(build_id)
            context_bytes = stage_context(self.staged_dir(slot), project_dir, dockerfile)

            # Scoped to this build's layer-chain shape, not just the container name:
            # otherwise every project's `sut` shares one tag and evicts the others' layers.
            # `base_ref` is the resolution the hash was taken over, so the scope and the
            # build agree on what they are built on.
            cache_ref = cache_image_ref(registry.registry_prefix, spec.tag,
                                        cache_scope(spec, base_ref))
            # The two fixed costs BuildKit's output never names — see the header
            # `_await_build_image` writes into the campaign's build.log.
            status.context_bytes, status.cache_ref = context_bytes or 0, cache_ref

            manifest = build_job_manifest(
                build_id=build_id, image_ref=image_ref, campaign_label=build_id,
                # Reaches this build's context and nothing else.
                token=self.scoped_token(pod_access.staged_scope(slot)),
                push_secret_name=registry.push_secret_name,
                namespace=self.namespace, insecure=registry.insecure,
                ca_configmap_name=registry.ca_configmap_name,
                cache_ref=cache_ref,
                host_aliases=cfg.get_host_aliases(),
                # Already resolved on this object (registry_image_store fills it from the
                # push Secret, which serves both directions), so no second lookup.
                pull_secret_name=registry.pull_secret_name or "",
                # The token a private `python_packages` git spec installs with. Looked up
                # rather than assumed: naming a Secret that does not exist would keep the
                # build pod from starting, which is a worse failure than building without
                # a credential no spec here needs.
                git_secret_name=self._images.git_secret_name(),
                # Which builder this client dials. A Service name, so a daemon replaced
                # between submit and start is still reachable at the same address.
                daemon_addr=buildkitd_address(self.namespace))
            self._k8s_batch().create_namespaced_job(self.namespace, manifest)
        except BaseException:
            status.phase, status.done = "failed", True
            self._discard_build_context(build_id)
            raise
        status.phase = "building"
        # The base is most of the built image: every experiment image is FROM a family
        # member, and containerd's content store is digest-addressed, so warming it now
        # means the pull after the build moves only this spec's own apt/pip layers. Free,
        # because a build takes minutes and nothing is waiting on the node yet.
        self._warm(base_ref)
        return ImageBuildRef(build_id=build_id, tag=spec.tag, cached=False)

    def _discard_build_context(self, build_id: str) -> None:
        """Drop *build_id*'s staged context. Best-effort: a leftover copy of the
        project dir is not worth failing a finished build over, but it is worth a
        warning, since the next sweep is the only thing that will retry it."""
        from .cluster_image_build import context_slot
        try:
            removed = self.discard_staged(context_slot(build_id))
        except Exception as e:  # noqa: BLE001 - cleanup must not fail the build
            logger.warning("could not discard the staged build context for %s: %s",
                           build_id, e)
            return
        if removed:
            logger.info("discarded the staged build context for %s", build_id)

    def _sweep_build_contexts(self) -> None:
        """Discard staged contexts whose build is over.

        A context is stale when no build Job owns it any more (Jobs self-destruct at
        ``ttlSecondsAfterFinished``, so an absent Job means the build ended at least
        that long ago — or died with a previous service instance). Builds this process
        still has in flight are held back explicitly: theirs is staged before their Job
        exists, so "no Job" alone would delete a context out from under a sibling
        request's init container.
        """
        from .cluster_image_build import BUILD_CONTEXT_PREFIX, staged_context_build_ids
        try:
            staged = staged_context_build_ids(self.staged_dir(BUILD_CONTEXT_PREFIX))
            jobs = self._k8s_batch().list_namespaced_job(
                self.namespace, label_selector="jobgroup=image-builds").items
        except Exception as e:  # noqa: BLE001 - cleanup must not fail the build
            logger.warning("could not sweep stale build contexts: %s", e)
            return
        live = {(job.metadata.labels or {}).get("build-id") for job in jobs}
        # Snapshot: a concurrent submit inserting into the state dict must not turn
        # this into "dictionary changed size during iteration".
        live |= {bid for bid, rec in list(self._image_build_state().items())
                 if not rec["status"].done}
        for build_id in sorted(staged - live):
            self._discard_build_context(build_id)

    def _registry_has_image(self, found) -> bool:
        """Is *found* already pushed? The **build** path's fail-closed view of the store.

        ``ImageBuildStore.present`` raises when the registry could not be asked, because a
        caller deciding whether it can *run* an image must never read that as "not built".
        The caller deciding whether to *build* one wants the opposite trade, and always did:
        uncertainty means rebuild, which costs a redundant push, where a wrong cache hit
        leaves the campaign's pods in ImagePullBackOff with the build long finished.
        """
        from robovast.common.errors import ImageStoreUnavailable
        try:
            return self._images.present(found)
        except ImageStoreUnavailable as e:
            logger.warning("treating %s as not yet pushed: %s", found.identity, e)
            return False

    def _delete_build_job(self, build_id: str, timeout_s: float = 60.0) -> None:
        """Delete a spent build Job (and its pods) and wait until it is really gone.

        The wait matters: ``create_namespaced_job`` right after a delete request still
        races the API server's cleanup and would 409 again.
        """
        from kubernetes import client
        batch = self._k8s_batch()
        try:
            batch.delete_namespaced_job(
                build_id, self.namespace,
                grace_period_seconds=0, propagation_policy="Background")
        except client.exceptions.ApiException as e:
            if e.status != 404:
                raise
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                batch.read_namespaced_job(build_id, self.namespace)
            except client.exceptions.ApiException as e:
                if e.status == 404:
                    logger.info("removed the previous failed build Job %s", build_id)
                    return
                raise
            time.sleep(1.0)
        raise RuntimeError(
            f"the previous failed build Job '{build_id}' did not disappear within "
            f"{timeout_s:.0f}s; delete it manually and retry")

    def _existing_build_job(self, build_id: str) -> "str | None":
        """Return 'succeeded' | 'failed' | 'running' for an existing Job, else None."""
        from kubernetes import client
        batch = self._k8s_batch()
        try:
            job = batch.read_namespaced_job(build_id, self.namespace)
        except client.exceptions.ApiException as e:
            if e.status == 404:
                return None
            raise
        st = job.status
        if st and st.succeeded:
            return "succeeded"
        if st and st.failed:
            return "failed"
        return "running"

    def get_image_build_status(self, build_id: str):
        from robovast.service.interface import ImageBuildStatus
        state = self._image_build_state()
        record = state.get(build_id)
        if record is None:
            # Not tracked in-process (e.g. after a restart): derive from the Job.
            phase = self._existing_build_job(build_id)
            if phase is None:
                raise KeyError(f"unknown build '{build_id}'")
            done = phase in ("succeeded", "failed")
            if done:
                # A build from a previous service instance: no record memoizes the
                # transition, so this repeats per poll — a no-op list once the prefix
                # is gone, and it beats waiting for the next build to sweep it.
                self._retire_build_context(build_id)
                # Deliberately no prewarm here, though this is a "the image exists now"
                # transition like the others. All this branch has is the build_id, and
                # `build_id_for` is not reversible into a ref: it lowercases and folds `_`
                # to `-`, which `concrete_image_ref` does not, so a tag like `my_sut` would
                # yield a ref that no registry serves. The prewarm would then sit in
                # ImagePullBackOff until its deadline, warming nothing and saying nothing,
                # since nothing reads a prewarm back. The next submit for this spec takes
                # the cache-hit path above and warms from a properly resolved ref.
                return ImageBuildStatus(
                    build_id=build_id, phase=phase, done=done,
                    cached=phase == "succeeded")
            # A restarted service must reach the same verdict about a stuck pod as the
            # one that submitted the build; the probe below needs no record to do it.
            blocked, failed = self._build_pod_verdict(build_id)
            if failed is not None:
                self._retire_build_context(build_id)
                return ImageBuildStatus(build_id=build_id, phase="failed", done=True,
                                        error=failed)
            return ImageBuildStatus(
                build_id=build_id, phase="blocked" if blocked else phase, done=False,
                error=blocked)
        status: ImageBuildStatus = record["status"]
        if status.done:
            return status
        phase = self._existing_build_job(build_id)
        if phase == "succeeded":
            status.phase = "succeeded"
            status.done = True
        elif phase == "failed":
            status.phase = "failed"
            status.done = True
            status.error = self._build_error(build_id, record.get("spec"))
        else:
            # Still active as far as the Job is concerned — which it will remain forever if
            # its pod cannot start, since `backoffLimit: 0` and no `activeDeadlineSeconds`
            # leave both counters at zero. That was a wait that never returned.
            blocked, failed = self._build_pod_verdict(build_id)
            if failed is not None:
                status.phase = "failed"
                status.done = True
                status.error = failed
            elif blocked is not None:
                status.phase = "blocked"
                status.error = blocked
            elif status.phase == "blocked":
                # It cleared on its own -- the transient blip the grace window is for.
                status.phase, status.error = "building", None
        if status.done:
            # This transition is the one moment we know the context is dead, for both
            # outcomes. Cheap (a prefix delete) and it runs once, since a done record
            # returns above.
            self._retire_build_context(build_id)
            if status.phase == "succeeded":
                # Same transition, and the fire point that earns the feature: the image
                # exists now, nobody has pulled it yet, and what needs pulling is precisely
                # the layers this build added on top of the base warmed at submit.
                self._warm(record["image_ref"])
        return status

    def _build_pod_verdict(self, build_id: str):
        """``(blocked, failed)`` for a build whose Job is still active, both ``ImageBuildError``.

        ``(None, None)`` — the pod is fine, or there is none yet. ``(blocked, None)`` — it
        cannot start, but not yet for long enough to call it. ``(None, failed)`` — it will
        not recover.

        **The grace window is the pod's own age, not a timer this method keeps.** Holding a
        ``blocked_since`` stamp across calls would make the verdict depend on how often
        someone polls, lose it whenever the service restarts, and require the "a failed probe
        must not clear the timer" discipline the campaign batch loop has to state explicitly.
        Kubernetes already records when the pod appeared, so asking it removes the state and
        the hazard together. ``pod_block_reason`` never fires on ``ContainerCreating`` or
        ``PodInitializing``, so age here does not punish a slow legitimate pull.
        """
        import datetime

        from .cluster_execution import BLOCKED_GRACE_SECONDS, pod_block_reason
        try:
            pod = self._build_pod(build_id)
        except Exception as e:  # noqa: BLE001 - one dropped read is not a verdict
            # Explicitly not "not blocked": saying so would end the build on the next
            # succeeded/failed check as if the pod were healthy. The next poll asks again.
            logger.warning("could not check whether build %s can start: %s", build_id, e)
            return None, None
        if pod is None:
            return None, None
        blocked = pod_block_reason(pod)
        if blocked is None:
            return None, None
        reason, message = blocked
        # ``start_time`` is set once the kubelet accepts the pod; ``creation_timestamp``
        # covers the window before that (an unschedulable pod never gets the former).
        started = (getattr(pod.status, "start_time", None)
                   or getattr(pod.metadata, "creation_timestamp", None))
        age = None
        if started is not None:
            now = datetime.datetime.now(datetime.timezone.utc)
            age = (now - started).total_seconds()
        detail = f"{reason}: {message}" if message else reason
        container = self._blocked_container(pod)
        # No timestamp at all (Kubernetes always sets one, so: a substrate we do not
        # recognise) means the window cannot be measured. Act on the reason rather than
        # granting an unmeasurable grace, which is the indefinite wait this replaces --
        # the block itself was observed either way.
        if age is not None and age < BLOCKED_GRACE_SECONDS:
            # Reported as `blocked`, with its diagnosis, rather than silently waited out --
            # so the reason reaches the caller on its first poll instead of a minute later.
            logger.warning("build %s cannot start yet (%s)", build_id, detail)
            return (self._blocked_build_error(build_id, reason, message, container,
                                              terminal=False),
                    None)
        logger.error("build %s cannot start and will not recover (%s)", build_id, detail)
        return None, self._blocked_build_error(build_id, reason, message, container,
                                               terminal=True)

    @staticmethod
    def _blocked_container(pod) -> str:
        """Which container of *pod* cannot pull, or ``""`` for an unschedulable pod.

        Named so the error can say *which* registry is unreachable: the sidecar and BuildKit
        come from different ones and are fixed in different places.

        This re-walks the statuses ``pod_block_reason`` just matched, because that function
        reports the reason and not where it came from -- a signature every campaign caller
        shares and none of them needs widened. The two agree by construction: same statuses,
        same order, same :data:`POD_BLOCKED_REASONS`. An unschedulable pod matches nothing
        here, which is the empty string, and the caller reads that as "not a container".
        """
        from .cluster_execution import POD_BLOCKED_REASONS
        statuses = list(getattr(pod.status, "init_container_statuses", None) or []) + \
            list(getattr(pod.status, "container_statuses", None) or [])
        for cs in statuses:
            state = getattr(cs, "state", None)
            waiting = getattr(state, "waiting", None) if state else None
            if waiting and getattr(waiting, "reason", None) in POD_BLOCKED_REASONS:
                return getattr(cs, "name", None) or ""
        return ""

    def _retire_build_context(self, build_id: str) -> None:
        """Discard a just-finished build's staged context."""
        self._discard_build_context(build_id)

    def _warm(self, image_ref: str) -> None:
        """Pull *image_ref* onto a node now, so the next pod to run it does not wait.

        A built image is in the registry and on no node, so whoever runs it first pays the
        whole pull -- and that is usually ``exec_in_container``, which exists to answer a
        question in seconds. See :mod:`.image_warm`; this method is only the seam that keeps
        the call sites one line each and makes the failure mode uniform.

        **Best-effort by construction, and that is not laziness.** A failed prewarm leaves
        exactly the situation that held before it existed: a slow first pod. Raising here
        would turn a missed optimization into a failed build, so the bare ``except`` is the
        correct trade -- but it warns, because a prewarm that never works is invisible
        otherwise (nothing reads it back, by design).
        """
        from .image_warm import warm_image
        try:
            warm_image(self._k8s_batch(), self.namespace, image_ref,
                       self._registry_pull_secret())
        except Exception as e:  # noqa: BLE001 - a prewarm must never fail its caller
            logger.warning("could not prewarm %s: %s", image_ref, e)

    def _build_error(self, build_id: str, spec=None):
        """Classify a failed build. *spec* is what the build was asked to install.

        Took a ``tag`` it never used; that slot now carries the spec, which the
        classifier does use -- without it a dependency missing from the base image is
        indistinguishable from a bad ``build.python_packages`` entry.
        """
        from robovast.service.image_build import classify_build_error
        log = self._build_log_text(build_id)
        return classify_build_error(log, spec)

    def _build_pod(self, build_id: str):
        """*build_id*'s builder pod, or ``None`` if it has none yet.

        One lookup for the two questions asked of that pod — what did the build print, and
        why can it not start — so they cannot disagree about which pod they mean. **Raises**
        on an API error rather than returning ``None``: a caller deciding whether the pod is
        blocked must not read "could not ask" as "not blocked".
        """
        pods = self._k8s().list_namespaced_pod(
            self.namespace, label_selector=f"build-id={build_id}")
        return pods.items[0] if pods.items else None

    #: Containers of the build pod, and what a failed pull of each one means. The reason
    #: Kubernetes reports is the same either way, but the fix is not, and naming the wrong
    #: one sends the reader to the wrong registry.
    _BUILD_CONTAINER_HINTS = {
        "context-fetch": (
            "the build infrastructure image (robovast-sidecar) could not be pulled. Either "
            "it is not in the registry this deployment points at, or the build Job has no "
            "credential for it -- check the image project/tag the service resolves "
            "(ROBOVAST_PROJECT / ROBOVAST_PROJECT_TAG) and the registry pull Secret. "
            "Nothing about the project's build: section is involved"),
        "buildkit": (
            "the BuildKit builder image could not be pulled, so the cluster has no path to "
            "the public registry it comes from (egress, or a misconfigured pull-through "
            "mirror). Nothing about the project's build: section is involved"),
    }

    def _blocked_build_error(self, build_id: str, reason: str, message: str,
                             container: str, terminal: bool):
        """The structured error for a builder pod that cannot start.

        Carried while the build is still ``blocked`` as well as once it has ``failed``, so the
        reason reaches the caller on its first poll rather than after the grace window: a
        status that says only "blocked" repeats the original complaint, which was an agent
        with no idea what had happened. *terminal* is what separates "not yet" from "not
        going to".

        Deliberately **not** ``classify_build_error``: that reads the builder's output, and a
        pod that never started produced none, so every such failure classified as the generic
        "the image build failed; see the log tail" — pointing an agent at ``build:``, which is
        the one thing that cannot be at fault here. Kubernetes' own message names the image
        and the registry error; the hint names the knob.
        """
        from robovast.service.interface import ImageBuildError

        from .cluster_execution import BLOCKED_GRACE_SECONDS
        hint = self._BUILD_CONTAINER_HINTS.get(container)
        if hint is None:
            # An unschedulable pod has no offending container -- the scheduler never got
            # that far -- and its message is the per-node accounting, which is the diagnosis.
            hint = ("the cluster could not place the build pod; the message above names the "
                    "resource no node can satisfy. This is capacity, not the project's "
                    "build: section")
        detail = f"{reason}: {message}" if message else reason
        if terminal:
            lead = f"the build pod cannot start -- {detail}"
            # The log is worth an extra read only here: the terminal error is what someone
            # reads, and during the grace window this would cost two API calls per poll.
            tail = self._build_log_text(build_id)
        else:
            lead = (f"the build pod cannot start yet -- {detail}. It fails if this has not "
                    f"cleared {BLOCKED_GRACE_SECONDS:.0f}s after the pod appeared")
            tail = ""
        return ImageBuildError(phase="builder-pod", fixable_by="infra",
                               message=f"{lead}. In short: {hint}", log_tail=tail)

    def _build_log_text(self, build_id: str) -> str:
        """The builder's own output, or the best available substitute.

        Falls back from the build container to the init container to the reason the pod
        cannot start, because the empty string is the one answer that is never useful: a
        failed build sends its reader here, and a pod that never ran ``buildctl`` has no
        ``buildkit`` log to give -- which is exactly the case where "read the log" was the
        advice and "" was the log.
        """
        from kubernetes import client

        from .cluster_execution import pod_block_reason
        core = self._k8s()
        try:
            pod = self._build_pod(build_id)
        except Exception as e:  # noqa: BLE001 - a log read must not fail a status poll
            logger.debug("could not find the build pod for %s: %s", build_id, e)
            return ""
        if pod is None:
            return ""
        for container in ("buildkit", "context-fetch"):
            try:
                text = core.read_namespaced_pod_log(
                    name=pod.metadata.name, namespace=self.namespace,
                    container=container)
            except client.exceptions.ApiException:
                continue
            if text:
                return text
        blocked = pod_block_reason(pod)
        if blocked:
            reason, message = blocked
            return f"{reason}: {message}\n" if message else f"{reason}\n"
        # Nothing from the client, so the reason is somewhere the client cannot see. Since the
        # solve happens in the shared daemon, a whole class of failure -- GC, a full store, a
        # solve killed for memory -- leaves the client's log empty and its own log holding the
        # only account of it. Without this the reader gets "" for a build that failed for a
        # reason that was written down.
        return self._daemon_log_tail()

    def _daemon_log_tail(self, lines: int = 50) -> str:
        """The build daemon's recent output, labelled as its own.

        Labelled because it is not this build's log and must not read as one: the daemon is
        shared, so what is in here may belong to a concurrent build. It is offered as the last
        resort it is -- a lead, not an account.
        """
        from kubernetes import client

        from .buildkitd_deploy import BUILDKITD_NAME

        try:
            pods = self._k8s().list_namespaced_pod(
                self.namespace, label_selector=f"app={BUILDKITD_NAME}")
            if not pods.items:
                return (f"no output from the build client, and no {BUILDKITD_NAME} pod to ask "
                        f"-- the shared build daemon is not running.\n")
            text = self._k8s().read_namespaced_pod_log(
                name=pods.items[0].metadata.name, namespace=self.namespace,
                container="buildkitd", tail_lines=lines)
        except client.exceptions.ApiException as e:
            logger.debug("could not read the build daemon's log: %s", e)
            return ""
        if not text:
            return ""
        return (f"--- no output from the build client; last {lines} lines from the shared "
                f"build daemon ({BUILDKITD_NAME}), which may include other builds ---\n{text}")

    def get_image_build_log(self, build_id: str, offset: int = 0):
        raw = self._build_log_text(build_id).encode("utf-8", "replace")
        record = self._image_build_state().get(build_id)
        done = bool(record and record["status"].done)
        return LogChunk(text=raw[offset:].decode("utf-8", "replace"),
                        next_offset=len(raw), eof=done)

    def _campaign_build_context(self, project, campaign_config, image_project=None,
                                image_project_tag=None):
        """``(spec, project_dir, cfg, registry)`` for a campaign's ``build:`` image, or
        ``None`` when it has none. Works from the already-resolved project/config, unlike
        :meth:`_build_context`, which resolves a standalone ``build_image`` request.

        Raises :class:`CampaignConfigError` — *not* ``ValueError`` as the request path
        does — because this runs on the campaign's worker thread, where the failure is
        recorded as the campaign's outcome rather than answered as a 400. An
        unconfigured registry and a broken ``build:`` section are both bad input with a
        self-contained message, so the campaign fails with that message alone; a
        ``ValueError`` fell through to the worker's catch-all and printed a stack trace,
        which reads as a RoboVAST bug rather than as something to go and configure.
        """
        from robovast.common.errors import CampaignConfigError
        from robovast.service.image_build import extract_build_specs, validate_build_spec
        project_dir = Path(project.config_path).resolve().parent
        # Same ordering as _build_specs_for: the campaign's own plugins may carry the
        # simulator backend that decides which container builds, so they have to be
        # resolvable before the specs are read, and base_dir has to be passed for a
        # file-ref backend to resolve at all.
        specs = extract_build_specs(campaign_config, base_dir=str(project_dir),
                                    image_project=image_project,
                                    image_project_tag=image_project_tag)
        if not specs:
            return None
        for name, spec in specs.items():
            problems = validate_build_spec(spec, project_dir)
            if problems:
                raise CampaignConfigError(
                    f"invalid execution.containers.{name}:\n  - " + "\n  - ".join(problems))
        cfg = self._cluster_config()
        registry = self._images.registry(require=False)
        if not registry.enabled():
            # The leading clause is this site's own: a campaign author needs to hear that
            # their *campaign* is what asked for a build. The rest is shared.
            raise CampaignConfigError(
                f"this campaign builds a container image, but {registry.why_disabled()}")
        return specs, project_dir, cfg, registry

    def _start_build_images(self, project, campaign_config, image_project=None,
                            image_project_tag=None) -> list:
        """Submit (or join) an in-cluster BuildKit Job per image this campaign builds.

        Returns as soon as each build has a handle; ``LocalTransport._await_build_image``
        waits on them over the interface, so both lanes share one wait loop.
        """
        resolved = self._campaign_build_context(
            project, campaign_config, image_project=image_project,
            image_project_tag=image_project_tag)
        if resolved is None:
            return []
        specs, project_dir, cfg, registry = resolved
        return [self._start_cluster_build(spec, project_dir, cfg, registry)
                for spec in specs.values()]

    def _resolve_built_images(self, project, campaign_config, image_project=None,
                              image_project_tag=None) -> dict:
        """Concrete registry refs to pin, by container name."""
        specs, project_dir, _cfg, registry = self._campaign_build_context(
            project, campaign_config, image_project=image_project,
            image_project_tag=image_project_tag)
        del registry            # the store carries the registry the refs are formed against
        return {name: self._images.ref_for(spec, project_dir).ref
                for name, spec in specs.items()}

    def _scheduling_for(self, campaign_id: str, *, live: bool) -> dict:
        """What the queue holds for this campaign, for a listing row.

        Only while it is live: the entry is dropped when the campaign ends, so a finished
        campaign would read as rank 0 either way -- and reading it from the queue rather than
        from the launch record keeps one answer to the question, the one admission uses.
        """
        if not live:
            return {"priority": 0, "paused": False}
        rank, held = self._admission_controller().scheduling(campaign_id)
        return {"priority": rank, "paused": held}

    def _register_scheduling(self, campaign_id: str, request) -> None:
        """Seed the queue with the rank and hold this campaign was launched (or adopted) with.

        The same call a restart adoption makes, because a resume *is* a launch: the request it
        rebuilds carries what the launch record kept, so a campaign that was demoted or held
        before the restart comes back that way rather than at the default.
        """
        self._admission_controller().set_scheduling(
            campaign_id, priority=request.priority, paused=request.paused)

    def set_campaign_scheduling(self, campaign_id: str, priority=None, paused=None) -> ActionResult:
        """Set a campaign's standing with the admission queue: its rank, its hold, or both.

        Two writes, and both are needed. The queue holds the live answer and is what the next
        admission pass reads; the launch record is what a **restart** re-launches the campaign
        from, so a change written only to the queue would be undone by the next service
        restart and the campaign would quietly go back to taking capacity somebody had already
        taken away from it.

        Ordering only, like everything else this queue does: a campaign demoted or held keeps
        the jobs it is already running and gives up only the slots they release. Nothing is
        preempted and no partial run is produced, which is what makes this usable on a
        campaign whose results matter.

        Refused for a campaign that is over -- there is nothing left to admit, and reporting
        a rank set on a finished campaign would be a change that means nothing.
        """
        require_scheduling_change(priority, paused)
        with self._lock:
            entry = self._campaigns.get(campaign_id)
        if entry is None:
            return ActionResult(
                ok=False, message=f"campaign {campaign_id} is not running here")
        if self._is_done(entry):
            phase = entry.state.snapshot().phase
            return ActionResult(
                ok=False,
                message=f"campaign {campaign_id} is {phase}; there is nothing left to queue")

        self._admission_controller().set_scheduling(
            campaign_id, priority=priority, paused=paused)

        # Best-effort, and deliberately after the queue: the change that matters now has
        # already taken effect, and a record that could not be written must not undo it.
        # It is logged loudly because what it costs is a restart quietly reverting the change.
        campaign_root = Path(entry.results_dir) / campaign_id
        try:
            update_launch_scheduling(campaign_root, priority=priority, paused=paused)
        except OSError as e:
            logger.warning("Could not record scheduling for %s, so a service restart would "
                           "return it to what it was launched with: %s", campaign_id, e)

        rank, held = self._admission_controller().scheduling(campaign_id)
        return ActionResult(ok=True, message=(
            f"priority {rank}" + (", paused" if held else "") +
            "; jobs already running are unaffected"))

    def stop(self, campaign_id: str) -> ActionResult:
        """Stop a campaign this process is driving.

        The driver is in this process, so the cooperative flag is a direct state
        write. That flag alone only ends a *search* between generations, though — a
        batch campaign's wait loop blocks until its Jobs finish on their own, so the
        flag would appear to do nothing. We therefore also tear down the campaign's
        cluster workloads (the same cleanup ``vast cluster
        jobs-cleanup`` performs): the running pods terminate now, the batch wait loop
        unblocks (``get_remaining_jobs`` treats a gone Job as finished), and the
        driver winds the campaign down.

        A campaign still in ``building`` is stopped by the flag alone: that teardown is
        label-scoped to ``jobgroup=scenario-runs`` and cannot reach the
        ``jobgroup=image-builds`` Job. That is deliberate and must stay true — an image
        build is content-addressed and therefore shared, so cancelling it could strand a
        sibling campaign waiting on the same image, and the image is a cache entry rather
        than this campaign's property. ``_await_build_image`` detaches instead.

        A campaign already **postprocessing** is likewise not reached by that teardown --
        its conversion Job is in ``jobgroup=postprocessing`` -- and is stopped by its own
        scope instead: ``run_conversion_job`` polls it and deletes the Job.

        Which unit of work a stop lands on is
        :func:`~robovast.execution.control_server.stop_scope_for_phase`'s to decide, shared
        with the local lane so the two cannot disagree, and the reply it carries says what
        that stop leaves behind.
        """
        with self._lock:
            entry = self._campaigns.get(campaign_id)
        if entry is None:
            return ActionResult(
                ok=False, message=f"campaign {campaign_id} is not running here")
        phase = entry.state.snapshot().phase
        scope = stop_scope_for_phase(phase)
        if scope is None:
            return ActionResult(ok=False, message=STOP_ALREADY_OVER.format(phase=phase))
        entry.state.request_stop(scope)
        if scope != STOP_RUNS:
            # The teardown is label-scoped to ``jobgroup=scenario-runs``, so it would not
            # reach a postprocessing Job or an upload anyway; not calling it keeps this
            # from reading as though it might.
            return ActionResult(ok=True, message=STOP_SCOPE_MESSAGES[scope])
        self._teardown_campaign_jobs(campaign_id)
        return ActionResult(ok=True, message="stop requested; in-flight jobs terminated")

    def _job_state_target(self, campaign_id: str, job_name: str, role: str) -> tuple:
        """The inherited reads, pointed at this job's pod instead of a local container.

        Everything that decides *what* is asked -- the simulator's own command, the scenario's own
        tree reader, which container each belongs in, the JSON passed through unreshaped, what
        "unavailable" means, and the TTL that makes one check serve every watcher -- is
        :class:`LocalTransport`'s and shared. Only the target differs, which is the whole reason
        ``exec_in`` takes one.

        Two lane facts shape it. ``job_name`` here is the **Kubernetes Job** name rather than a run
        key, so it cannot be turned into ``/out/<config>/<run>``; and a Job may pack several runs,
        so there is no single run dir to name even in principle. But ``/out`` is *this pod's own*
        emptyDir, holding only this job's runs -- so naming it is exact rather than vague, and every
        reader finds the run still being written underneath it, whichever container it is asked in.

        Raises ``KeyError`` for a job between scheduling and running, or already gone; the callers
        turn that into a stated reason rather than an empty answer.
        """
        return self._job_pod_target(campaign_id, job_name, role), "/out"

    #: The run dirs inside a Job, newest first. Only **real** run dirs: a campaign root holds
    #: ``_jobs/`` beside them (and the other names in
    #: :data:`~robovast.common.campaign_data.RESERVED_CAMPAIGN_DIRS`), and an earlier version of
    #: this took the newest file anywhere under the root -- which was reliably a job artifact, so
    #: it named the run ``_jobs/batch-0`` and pointed every reader at a subtree with no run in it.
    #:
    #: The shape is the filter: ``<config>/<run-number>``, the run number being digits. Matched on
    #: the layout rather than on a list of names to exclude, because the layout is what the readers
    #: below depend on and a new reserved name would silently pass an exclusion list.
    _LIVE_RUN_FIND = ("find {root} -mindepth 2 -maxdepth 2 -type d "
                      "-regex '.*/[^/]+/[0-9]+' -printf '%T@ %P\\n' "
                      "| sort -rn | head -1 | cut -d' ' -f2-")

    def _job_live_run(self, campaign_id: str, job_name: str, target, run_dir: str) -> tuple:
        """``(run_dir, run_key)`` for the run this Job is on. Always resolved, never delegated.

        **Which run a job is on is RoboVAST's question, and it gets answered here.** Answering it
        only for a Job that packs several runs hands an unpacked one ``/out`` and leaves the readers
        to find the run underneath it. Both of them can -- ``tree_state`` and ``roqsim health`` each
        search a couple of levels down -- and that is exactly the problem:

        * it is two other components modelling *this* layout, and a layout guessed in two places is
          free to disagree with the one place that owns it;
        * "the newest one below here" is a heuristic answering a question they cannot see the answer
          to, while the service can;
        * and it **masks** a wrong directory instead of failing on it. Pointed at ``_jobs/batch-0``,
          a reader searches around it and then reports that the scenario may have run without
          ``--bt-log`` -- a confident wrong cause for a path bug.

        So the exact run dir goes out, every time, and ``run`` names it in the reply. One ``find``
        over the pod's own emptyDir per read, which is nothing beside the reads it precedes -- one
        resolution in place of two heuristics.

        A discovery that finds nothing leaves ``/out`` in place: a job between starting and its
        first record is normal, and the readers' own "nothing here yet" is a better answer than a
        failure from the step that was only trying to be more precise.
        """
        del campaign_id
        from robovast.service.local_transport import _JOB_STATE_LIMIT_S
        command = self._LIVE_RUN_FIND.format(root=shlex.quote(run_dir))
        _code, stdout, _stderr, timed_out = self._exec_lane().exec_in(
            target, ["/bin/bash", "-c", command], _JOB_STATE_LIMIT_S)
        if timed_out:
            return run_dir, None
        run_key = (stdout or "").strip()
        if not run_key or run_key.count("/") != 1:
            return run_dir, None
        return f"{run_dir.rstrip('/')}/{run_key}", run_key

    def _job_output_dir(self, campaign_id: str, job_name: str, run_dir: str) -> str:
        """This Job's own ``OUTPUT_DIR``, which is where its resource samples and logs are.

        Read off the Job rather than resolved through the campaign manifest, and rather than
        derived from the layout: the backend **stamps it on the pod** as
        ``OUTPUT_DIR=/out/_jobs/<batch>/job-<idx>``, so reading it back is exact for a running job
        and needs no exec at all. :meth:`_job_artifact_dir` already does that read for the
        intervention ledger; this is the same fact, wanted for the same job, so it is the same
        read.
        """
        del run_dir
        rel = self._job_artifact_dir(job_name)
        return f"/out/{rel.strip('/')}" if rel else "/out"

    def _job_pod_target(self, campaign_id: str, job_name: str, role: str = SCENARIO_CONTAINER):
        """``(pod, container)`` for one running job's *role*, or raise saying why not.

        The scenario is the pod's first workload container and the sidecars follow it in
        declaration order, so it resolves to a position rather than to a name assembled here. Read
        from the live pod for the same reason the log tail reads it: the manifest owns those names.

        **Through :func:`~.kube_client.pod_workload_containers`, which is not optional.** The
        simulator and the system under test are *native sidecars* -- ``initContainers`` with
        ``restartPolicy: Always`` -- so ``pod.spec.containers`` holds the scenario container and
        nothing else. Asking it directly made this the fourth place to get that wrong in the same
        way (see that function's docstring for the other three): every role but ``scenario`` is
        refused as "this job runs no such container" on a pod that is visibly running three, and the
        refusal quotes a one-name list as its evidence.

        **The pod decides, and the plan is only consulted for a role the pod does not name.** That
        order is the whole point. Asking the plan first resolves ``simulation`` to the scenario
        container for a campaign whose archived config cannot be read -- or whose block simply names
        no simulator -- and the read then enters a container with no simulator in it. Confidently,
        and with no way for the caller to tell. A pod that *has* a container called ``simulation``
        is not a thing the config can outvote.

        The plan still answers the case the pod cannot: a simulator stepped in-process **is** the
        scenario container, so there is no container of that name to find and refusing the role
        would deny a read the campaign can answer.
        """
        from robovast.common.config import CONTAINER_ROLES

        from .cluster_execution import _label_safe_campaign
        from .kube_client import pod_workload_containers
        if role not in CONTAINER_ROLES:
            raise ValueError(f"unknown container role {role!r}; expected one of "
                             f"{', '.join(CONTAINER_ROLES)}")
        label = (f"jobgroup=scenario-runs,"
                 f"campaign-id={_label_safe_campaign(campaign_id)},job-name={job_name}")
        pods = self._k8s().list_namespaced_pod(self.namespace, label_selector=label)
        if not pods.items:
            raise KeyError(f"no pod for job {job_name!r} in campaign {campaign_id!r}: it is "
                           f"between scheduling and running, or already gone")
        pod = pods.items[0]
        names = [c.name for c in pod_workload_containers(pod)]
        if not names:
            raise KeyError(f"pod for job {job_name!r} declares no workload containers")
        # The scenario is the pod's first workload container, by position: the manifest owns what
        # it is called, so there is no name to match on.
        if role == SCENARIO_CONTAINER:
            return pod.metadata.name, names[0]
        if role in names:
            return pod.metadata.name, role
        if self._plan_role(campaign_id, role) == SCENARIO_CONTAINER:
            return pod.metadata.name, names[0]
        raise KeyError(f"this job runs no {role!r} container; it has: {', '.join(names)}")

    def exec_in_job(self, campaign_id: str, job_name: str, command: str,
                    container: str = "scenario", source: str = "api") -> "ExecResult":
        """The inherited probe, pointed at this job's pod.

        Same recording, same ordering, same refusal for a job that is not running, same environment
        -- only the target differs, which is what ``exec_in`` exists for. The container is resolved
        from the pod rather than from a name built here: a role maps to a *position* in the pod spec
        (the scenario runs first), and the concrete names are the manifest's business.
        """
        from robovast.common.campaign_data import KIND_PROBED, record_intervention
        from robovast.common.execution import in_run_env
        from robovast.service.interface import ExecResult

        if not (command or "").strip():
            raise ValueError("exec_in_job needs a command: there is no scenario to start here, "
                             "only a live job to look at.")
        self._require_running_job(campaign_id, job_name)
        campaign_root = self._campaigns_root() / campaign_id
        record_intervention(campaign_root, kind=KIND_PROBED,
                            job_dir=self._job_artifact_dir(job_name), job_name=job_name,
                            source=source, detail=command)
        # Mirrored at once, for the reason the kill is: postprocessing runs as its own in-cluster
        # Job before the campaign root is uploaded, so a probe recorded only on pod disk would be
        # lost exactly when the results are assembled.
        from robovast.service.local_transport import _PROBE_LIMIT_S
        pod, pod_container = self._job_pod_target(campaign_id, job_name, container)
        exit_code, stdout, stderr, timed_out = self._exec_lane().exec_in(
            (pod, pod_container), in_run_env(command), _PROBE_LIMIT_S)
        return ExecResult(exit_code=exit_code, stdout=stdout, stderr=stderr,
                          timed_out=timed_out, limit_s=_PROBE_LIMIT_S, limit_source="command")

    def stop_job(self, campaign_id: str, job_name: str,
                 reason: "str | None" = None, source: str = "api") -> ActionResult:
        """Delete one running scenario Job; its siblings and the batch keep going.

        Deliberately **not** ``request_stop()`` and **not** ``_teardown_campaign_jobs``:
        the flag ends the campaign and the teardown is label-scoped to *every* Job of it.
        A single ``delete_namespaced_job`` is enough because the batch wait loop treats a
        gone Job as finished (``get_remaining_jobs``), so the remaining Jobs run to
        completion and the batch still projects its results.

        ``Background`` propagation so the pod is collected with the Job, through its
        owner reference. Whatever the pod's uploader delivers within the termination grace
        survives: each Job delivers its own results.
        """
        from kubernetes import client

        from robovast.common.campaign_data import KIND_KILLED, record_intervention
        with self._lock:
            entry = self._campaigns.get(campaign_id)
        if entry is None:
            raise KeyError(f"campaign {campaign_id!r} is not running here")
        self._require_running_job(campaign_id, job_name)
        job_dir = self._job_artifact_dir(job_name)
        # Recorded before the delete: the pod dies asynchronously, and a failure in
        # between must not leave a cut-short run with no record of why.
        campaign_root = self._campaigns_root() / campaign_id
        record_intervention(campaign_root, kind=KIND_KILLED, job_dir=job_dir, job_name=job_name,
                            source=source, detail=reason)
        try:
            self._k8s_batch().delete_namespaced_job(
                job_name, self.namespace,
                grace_period_seconds=0, propagation_policy="Background")
        except client.exceptions.ApiException as e:
            if e.status != 404:
                raise
            # It finished between the precondition and here. The record stands: its runs
            # either delivered results (and keep their real verdict) or did not.
            return ActionResult(ok=True, message=f"job {job_name} was already gone")
        return ActionResult(
            ok=True,
            message=(f"deleted job {job_name}; the campaign continues with its remaining "
                     f"jobs and this job's unfinished runs are recorded as 'killed'"))

    def _job_artifact_dir(self, job_name: str) -> str:
        """The Job's campaign-relative artifact dir, read off the Job itself.

        The pod already carries it as ``OUTPUT_DIR=/out/_jobs/<batch>/job-<idx>`` (see
        ``kubernetes_backend.create_job_manifest``), so this reads back what the backend
        actually stamped rather than re-deriving the layout from the Job's *name* — a
        parse of ``<batch>-job-<idx>`` would be a second definition of that layout, free
        to drift from :func:`~robovast.common.execution.job_artifact_rel`.

        ``""`` when the Job carries no such variable, which keeps the kill recordable:
        the ledger's other resolution path (the job-link manifest) is what needs this, and
        a record without it is still a truthful record that a human stopped the job.
        """
        from kubernetes import client
        try:
            job = self._k8s_batch().read_namespaced_job(job_name, self.namespace)
        except client.exceptions.ApiException:
            return ""
        containers = (getattr(job.spec.template.spec, "containers", None) or []
                      if job.spec and job.spec.template and job.spec.template.spec else [])
        for container in containers:
            for var in getattr(container, "env", None) or []:
                if var.name == "OUTPUT_DIR" and var.value:
                    return var.value.removeprefix("/out/").lstrip("/")
        return ""

    def _teardown_campaign_jobs(self, campaign_id: str) -> None:
        """Delete one campaign's in-flight cluster workloads (label-scoped).

        Reuses ``cleanup_cluster_campaign`` — the same teardown ``vast cluster
        jobs-cleanup`` performs — so the running pods terminate now and the driver's
        batch wait loop unblocks. Label-scoped to this campaign's Jobs and pods, and
        nothing cluster-wide is paused for the duration, so a concurrent campaign keeps
        being admitted while this one is torn down.

        ``aux=False``: the driver is in *this* process and its composition span owns the
        campaign's aux pods, deleting them when it ends. Reaping them here removes a pod
        the span may be exec'ing into, and the exec then fails with a 404 on the
        ``pods/exec`` subresource — reported as a simulator that could not be asked about
        the world, on a campaign that was merely stopped. The reaper keeps that job (it
        collects a pod whose span is gone); a stop must not do it.
        """
        from .cluster_execution import cleanup_cluster_campaign
        cleanup_cluster_campaign(namespace=self.namespace, campaign=campaign_id,
                                 context=self.kube_context, aux=False)

    def _adopts_on_restart(self) -> bool:
        """True: this lane's campaigns outlive the process, and the next one adopts them.

        A cluster campaign's compute is its scenario Jobs. They are not children of this
        process, they deliver their own results to the campaign, and
        :mod:`~robovast.execution.cluster_execution.campaign_resume` re-attaches to them at
        startup -- so exiting is not a reason to destroy them, and a pod replacement
        (``vast service upgrade``, an eviction, a drain, an OOM) stops being a data-loss
        event.

        This is why there is no ``_terminate_running_campaigns`` override here any more.
        Stopping a campaign is :meth:`stop`, which tears its Jobs down through
        :meth:`_teardown_campaign_jobs` and records the stop; exiting the service is not,
        and never was a good way to say it -- the cooperative stop persists a terminal
        ``outcome.json``, and a campaign that has recorded an ending is one no successor
        will pick up again.

        Unconditional, and deliberately not a ``KUBERNETES_SERVICE_HOST`` test: an
        off-cluster service driving a cluster adopts on its next start exactly like an
        in-pod one, so keying on where the process runs would answer a different question.
        """
        return True

    # -- container exec -----------------------------------------------------

    def _exec_lane(self):
        """The in-cluster exec lane: one aux pod, driven through ``pods/exec``.

        Staging goes through the data plane, exactly as an image build's context does
        (see ``_start_cluster_build``): the tree is written on this disk, the pod fetches
        it with a token scoped to its slot, and the slot is dropped with the pod.
        """
        from .container_runner import service_pod_owner_reference
        from .kube_exec_lane import KubeExecLane
        owner = None
        try:
            owner = service_pod_owner_reference(self._k8s(), self.namespace)
        except Exception as e:  # noqa: BLE001 - off-cluster there is no service pod
            logger.debug("no service-pod owner reference for the exec pod: %s", e)
        return KubeExecLane(self.namespace, owner_ref=owner,
                            kube_context=self.kube_context,
                            # The exec pod runs the experiment image, which on this lane is
                            # in our own registry and may be private. Without this the pull
                            # succeeds only on a node that already cached it.
                            pull_secret=self._registry_pull_secret(),
                            stage_dir=self.staged_dir,
                            discard_staged=self.discard_staged,
                            token_for=self.scoped_token)

    def _reap_stray_exec_container(self) -> None:
        """Delete every exec pod and staged tree left by a previous service process.

        A sweep rather than ``stop_held()`` on the one fixed name: a query pod's name
        carries a hash of the identity it was started for, and nothing persists those
        across a restart, so the label is the only handle left on it.
        """
        try:
            deleted = self._exec_lane().sweep_held()
            if deleted:
                logger.info("removed %d stray exec pod(s) from a previous run: %s",
                            len(deleted), ", ".join(deleted))
        except Exception as e:  # noqa: BLE001 - a missing cluster must not break startup
            logger.debug("could not check for stray exec pods: %s", e)

    # -- orphan reaping -----------------------------------------------------

    def reap_orphans(self) -> int:
        """Delete campaign workloads left behind by a previous service instance.

        Aux pods only, and that is the whole scope: a restart leaves the campaigns'
        **scenario Jobs** running on purpose (see :meth:`_adopts_on_restart`), so this
        must never widen to them. Aux pods are owned by the service pod and thus
        garbage-collected by Kubernetes when it is replaced; this is the backstop for
        the cases GC misses (e.g. the ownerReference could not be resolved), and the
        successor to the old launcher-side ``reap_orphaned_runs``. Best-effort.
        """
        try:
            core = self._k8s()
            pods = core.list_namespaced_pod(
                self.namespace, label_selector=AUX_LABEL).items
        except Exception as e:  # noqa: BLE001 - never block startup
            logger.debug("Could not list aux pods to reap: %s", e)
            return 0
        reaped = 0
        for pod in pods:
            # Nothing in this fresh process is driving any campaign yet, so every
            # aux pod present at startup is by definition an orphan.
            try:
                core.delete_namespaced_pod(pod.metadata.name, self.namespace)
                reaped += 1
            except Exception as e:  # noqa: BLE001
                logger.debug("Could not delete orphaned aux pod %s: %s",
                             pod.metadata.name, e)
        if reaped:
            logger.info("Reaped %d orphaned aux pod(s) from a previous service instance",
                        reaped)
        return reaped

    def _campaigns_a_restart_would_lose(self, active) -> dict:
        """``{campaign_id: why}`` for the live campaigns a replacement could not pick up.

        Asked of :mod:`.campaign_resume` -- the same decision the successor will make, over the
        same campaign root -- so the warning before a roll and the behaviour after it cannot
        drift apart. A campaign this cannot answer for is treated as one that would be lost:
        the whole point of the refusal is to be wrong in the safe direction.
        """
        from . import campaign_resume
        blocked = {}
        for summary in active:
            cid = summary.campaign_id
            try:
                refusal = campaign_resume.would_be_lost(self, cid)
            except Exception as e:  # noqa: BLE001 - "cannot tell" is not "will survive"
                refusal = f"could not be checked ({e})"
            if refusal is not None:
                blocked[cid] = refusal
        return blocked

    def resume_interrupted_campaigns(self) -> dict:
        """Pick up the campaigns a previous service process was driving.

        Synchronously, and before this service answers anything. Not a background thread:
        ``_launch_campaign`` returns as soon as a campaign is named, so the blocking part is
        one directory scan — and registering the campaigns *here*
        is what stops a fresh launch arriving over the API and racing a campaign that is
        about to be adopted.

        Returns ``{campaign_id: None | refusal}``; see :mod:`.campaign_resume` for what is
        picked up and what is deliberately left alone. Never raises.
        """
        from . import campaign_resume
        outcomes = campaign_resume.resume_all(self)
        resumed = [cid for cid, refusal in outcomes.items() if refusal is None]
        if resumed:
            logger.info("Resumed %d campaign(s) interrupted by a service restart: %s",
                        len(resumed), ", ".join(resumed))
        return outcomes

    def delete_campaign(self, campaign_id: str) -> ActionResult:
        """Delete one cluster campaign wholesale: its directory, its leftover Jobs and its
        token Secret (see :meth:`RobovastInterface.delete_campaign`).

        The directory is the inherited delete; the Job reap catches anything a crashed or
        orphaned campaign left behind. The external share copy is untouched.
        """
        from . import pod_access
        from .cluster_execution import cleanup_cluster_campaign

        result = super().delete_campaign(campaign_id)
        try:
            cleanup_cluster_campaign(namespace=self.namespace, campaign=campaign_id,
                                     context=self.kube_context)
        except Exception:  # noqa: BLE001 - cleanup is best-effort
            logger.warning("Leftover-Job cleanup for %s failed", campaign_id,
                           exc_info=True)
        try:
            pod_access.delete_campaign_secret(self._k8s(), self.namespace, campaign_id)
        except Exception:  # noqa: BLE001 - a Secret with no campaign is a leftover, not a fault
            logger.warning("Could not remove the token Secret of %s", campaign_id,
                           exc_info=True)
        return result

    # -- data / results -----------------------------------------------------

    def _resolve_image_digest(self, ref: str):  # pylint: disable=useless-return
        """No tag→digest resolution on this lane. Refusing beats answering with the wrong bytes.

        Inherited, this would be ``docker inspect`` **on the service host**, which is either absent
        (in-pod) or -- worse, running off-cluster with ``-x`` -- present and answering with a bare
        local image id. No cluster node can pull such an id (``pullable_digest`` rejects it), so the
        aux pod would fail to start on an identity we had just declared trustworthy. A campaign here
        that recorded no per-role digest is therefore refused with the resolver's message.
        """
        del ref
        # Explicit, not incidental: None *is* the answer on this lane, and the
        # docstring above is about that. Falling off the end would read as an
        # unfinished function.
        return None

    def _scene_runner_context(self, campaign_id: str, identity: dict, on_wait=None):
        """A context manager yielding an aux-pod runner factory on the campaign's own image.

        Deliberately not ``AuxPodSession``'s campaign-scoped use: this build is not part of a campaign's
        lifecycle. It is a cache fill that may happen long after the campaign finished, and its result
        serves *every* campaign that used that world -- so the pod's lifetime is the build's, and the
        context manager is what guarantees it is torn down rather than left to
        ``activeDeadlineSeconds``.

        The pod is also the only thing that knows why a build has not started yet, so *on_wait*
        is reported from it: on this lane the wait before the exporter runs is a scheduling
        decision and an image pull, and an image this cluster cannot pull never gets past it.
        Created on entry rather than at the first command, unlike a composition's: this
        caller knows the one image it needs, so it can pay the pull where it is able to
        report it — which is what makes the wait a stage of the build rather than an
        unexplained pause inside it.
        """
        import hashlib

        from robovast.common.variation.container_runner import ContainerSpec

        from .container_runner import AuxPodSession

        del campaign_id
        image = identity["image"]
        # A pod name has to be label-safe and stable for this world, and an image digest is neither
        # short nor label-safe. `aux_pod_name` sanitises what it is given, so give it a digest of the
        # digest: same world -> same name, which also makes a duplicate create a 409 the session reuses.
        tag = f"scene-{hashlib.sha256(image.encode()).hexdigest()[:12]}"
        spec = ContainerSpec(image=image)
        pull_secret = self._registry_pull_secret()

        @contextlib.contextmanager
        def context():
            with AuxPodSession(tag, self.namespace, core_v1=self._k8s(),
                               pull_secret=pull_secret,
                               kube_context=self.kube_context,
                               on_pending=_pod_wait_reporter(on_wait),
                               **self._aux_staging_kwargs()) as session:
                session.provision(spec)
                yield session.runner_factory()

        return context

    def _registry_pull_secret(self) -> str:
        """The registry pull secret, so a pod of ours can pull a *private* built image.

        Aux images were public when that path was written, so it never needed one, and a node that has
        already cached the campaign image hides the omission (``imagePullPolicy: IfNotPresent``) -- which
        means it first fails on a fresh node, the worst place to discover it.

        Three callers -- the scene aux pod, a campaign's aux pod, and the diagnostic exec pod, which
        runs the same private images -- hence the name is not about scenes. The store answers it, because which Secret pulls
        from this registry is the registry's business. It answers directly rather than through an
        import guarded by a bare ``except``, which turns a wrong module path into a silent no-op.
        """
        return self._images.pull_secret_name()

    def _postprocess_campaign(self, campaign_id: str, campaign_dir, *,
                              force: bool = False, skip=(), state=None) -> tuple:
        """Postprocess through the Job, as this lane does everywhere else.

        The inherited version runs the whole pipeline in-process, which is right on a
        machine with Docker and wrong in a pod: the rosbag steps shell out to
        ``docker_exec.sh``, find no daemon, and fail against an image nobody chose.

        Overriding the seam rather than its callers is what makes the base docstring's
        promise true -- "one call for both callers, so a raw archive taken in is
        postprocessed exactly the way asking for it later would be". It was not: the
        retrigger went through ``postprocess_campaign`` and the import chain did not, so an
        imported raw campaign was the one case that took the local path on a cluster.
        """
        from robovast.execution.control_server import stop_checker  # noqa: PLC0415

        from . import pod_access  # noqa: PLC0415
        from .postprocess_job import postprocess_campaign  # noqa: PLC0415

        return postprocess_campaign(
            self._cluster_config(), campaign_id, str(campaign_dir), self.namespace,
            token=self.scoped_token(pod_access.campaign_scope(campaign_id)),
            force=force, skip=list(skip or []), kube_context=self.kube_context,
            state=state,
            # A postprocess is a tracked campaign while it runs, so ``stop`` reaches it --
            # and with this, ends it instead of leaving it to finish.
            should_stop=stop_checker(state),
            # The same queue the campaign's trials went through, so this pod waits for room
            # rather than being created against a cluster that has none.
            admission=self._admission_controller())

    def run_postprocessing(self, request) -> ActionResult:
        """(Re)run analysis postprocessing for a cluster campaign, as a monitored
        background operation (returns immediately; watch it in the campaign view).

        Both stages run in the postprocessing pod: it fetches the campaign once into a
        shared volume, converts the rosbags there in the campaign's own execution image,
        and runs the host stage -- metrics, provenance, the index ingest -- against the
        same volume, delivering what it derived back into the campaign. This process only
        submits the Job and records its outcome.

        No campaign log handler is attached around this, unlike the local lane: the pod's
        own output is what the POSTPROCESSING section shows, published into the campaign's
        ``postprocessing.log`` while the Job runs, so a handler streaming this process's
        lines into the same file would be overwritten by each publish. The failure path
        where no such file arrives authors one instead
        (``postprocess_job._write_failure_log``), which is what keeps a failed postprocess
        visible in the campaign log where a successful one is read.
        """
        self._admit_storage(f"postprocess {request.campaign_id}")
        campaign_dir = self.campaign_dir(request.campaign_id)

        def work(state):
            ok, message = self._postprocess_campaign(
                request.campaign_id, campaign_dir,
                force=request.force, skip=list(request.skip or []), state=state)
            self._record_postprocess_outcome(request.campaign_id, state, ok, message)

        return self._dispatch_background(
            request.campaign_id, phase=Phase.POSTPROCESSING, work=work)

    def _record_postprocess_outcome(self, campaign_id: str, state, ok: bool,
                                    message: str) -> None:
        """Write a postprocessing verdict into the campaign, and notify on it.

        One definition for the process that submitted the Job and the one that only waited
        for it (:meth:`reattach_postprocessing`): the campaign has a single record of what
        its postprocess did, so two writers of it would be two answers a reader cannot
        choose between.
        """
        from robovast.execution.status_recovery import record_step_outcome
        status = record_step_outcome(self.campaign_dir(campaign_id),
                                     postprocessing=(ok, message))
        state.update(postprocessed=status.postprocessed,
                     postprocessing_error=status.postprocessing_error)
        # The recorded phase, not `finished`: `record_step_outcome` preserves how the
        # campaign ended, and a live entry that disagreed with it would answer
        # differently until the next restart.
        state.set_phase(status.phase)
        # Same one-shot notifier as the local lane, and it matters more here: this is
        # the detached lane the push notifications exist for.
        notifier = self._notifier(campaign_id)
        if ok:
            notifier.postprocessed()
        elif ok is None:
            # No push at all while the outcome is open: both notifications are terminal
            # statements about the campaign, and ``None`` says the Job could not be read --
            # announcing a failure over a conversion that may be finishing is the one
            # message that cannot be taken back.
            logger.warning("Postprocessing outcome for %s is unknown, so no "
                           "notification is sent: %s", campaign_id, message)
        else:
            notifier.postprocessing_failed(message)

    def reattach_live_postprocessing(self):
        """Wait on the postprocessing Jobs a previous service process left running.

        Its own concern beside :meth:`resume_interrupted_campaigns`, not part of it: that
        one picks up campaigns owed work, and a retriggered postprocess runs on a campaign
        whose ``outcome.json`` is already terminal -- which resume excludes on purpose, so
        that a finished campaign is never restarted. What is owed here is a *verdict*, not
        work: the Job is running and will finish either way, and only a waiter writes what
        it did into the campaign.

        Started in the background rather than run here, because it lists the namespace's
        Jobs, and waiting on the API must not hold up a service that has to answer.
        Returns the thread, for a caller that needs to join it; see
        :mod:`.postprocess_reattach` for how the Jobs are found. Never raises.
        """
        from . import postprocess_reattach
        return postprocess_reattach.start_reattach(self)

    def reattach_postprocessing(self, campaign_id: str, job_name: str) -> bool:
        """Wait for *job_name* in the background and record its outcome. False if busy.

        Dispatched exactly as a retrigger is, so the campaign reads as POSTPROCESSING while
        the Job runs and its live log keeps being published -- and so a launch or a
        retrigger arriving over the API meets the same busy campaign it would meet if this
        process had submitted the Job itself.

        Nothing is submitted, created or replaced: the Job already mounts the scripts it
        was created with, and writing those again swaps the script out from under the
        running interpreter (see ``postprocess_job.reattach_conversion_job``). An outcome
        that could not be established is logged and left unwritten -- the previous record
        standing is wrong, but a verdict this process did not observe would be worse.
        """
        from .postprocess_job import reattach_conversion_job

        def work(state):
            from robovast.execution.control_server import stop_checker  # noqa: PLC0415
            ok, message = reattach_conversion_job(
                campaign_id, str(self.campaign_dir(campaign_id)), self.namespace,
                job_name, kube_context=self.kube_context,
                should_stop=stop_checker(state))
            if ok is None:
                logger.info("Left the postprocessing record of %s alone: %s",
                            campaign_id, message)
                state.set_phase(Phase.FINISHED)
                return
            self._record_postprocess_outcome(campaign_id, state, ok, message)

        result = self._dispatch_background(campaign_id, phase=Phase.POSTPROCESSING, work=work)
        return bool(result.ok)
