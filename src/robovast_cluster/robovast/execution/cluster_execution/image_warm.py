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

"""Pull an image onto a node before anything waits for it.

A built image lives in the registry and on **no node**, so the first pod to run it pays
the whole multi-GB pull -- and that pod is usually the diagnostic ``exec_in_container``,
whose entire justification is answering "is the package installed?" in seconds. The local
lane does not have this problem: ``buildx --load`` puts the image straight into the daemon
store the runner uses (see ``robovast.service.image_store``). This restores that property
on the cluster lane.

The mechanism is a throwaway Job whose one container *is* the target image, running
``/bin/true``. **The pull is the work.** Nothing reads the Job's result, so a broken
entrypoint, a missing shell or a nonzero exit warm the node just as well as a clean run --
which is what makes it safe to point at an arbitrary experiment image.

Three properties every caller relies on:

* **Idempotent**, by the deterministic name :func:`warm_id_for` derives (the same trick
  ``build_id_for`` uses): a duplicate create is a 409 meaning "already warming", so no
  in-process record is needed and a service restart changes nothing.
* **Self-collecting**, and ``ttlSecondsAfterFinished`` alone does not achieve that -- see
  :data:`WARM_DEADLINE_SECONDS`.
* **Never fatal.** A prewarm that fails costs a slow pod later, which is exactly what the
  situation was before it existed, so callers log and move on.

**Two shapes, because "warm" means two different things here.** A Job schedules one pod and
therefore warms exactly one node, which is the right answer for an experiment image: the pod
that wants it is the next one to run, and it is the only pod that will. The *family* images
are the opposite case -- any node may run any cell of a sweep -- so one warmed node leaves
every other one to pay the pull. Those get a DaemonSet instead (:func:`warm_daemonset_manifest`),
which is the only way Kubernetes expresses "on every node". It gives up the self-collection
below in exchange: a DaemonSet is meant to persist, so it is removed explicitly at teardown
(:func:`delete_warm_daemonset`) rather than by a TTL.

Deliberately **not** put through admission: a prewarm queued behind a full sweep warms the
node after the thing that needed it, which is worse than not warming at all. A DaemonSet is
also the wrong shape for a queue that admits one pod at a time against free capacity. Its
footprint is :data:`WARM_CPU_REQUEST` / :data:`WARM_MEMORY_REQUEST` for at most the deadline,
and admission counts it like any other pod once it is bound, so it is never double-counted.
"""

import hashlib
import logging
import re

logger = logging.getLogger(__name__)

#: Hard stop for a prewarm pod, and the reason this Job is *not* a build Job in miniature.
#:
#: ``ttlSecondsAfterFinished`` only starts once a Job is **terminal**. With
#: ``backoffLimit: 0`` and no deadline, a pod wedged in ``ImagePullBackOff`` leaves both of
#: the Job's counters at zero, so it stays ``active`` forever and the TTL never applies --
#: the build path documents exactly this (``ClusterService.get_image_build_status``) and
#: carries a whole ``blocked``-phase probe to work around it. A prewarm has nobody waiting
#: on it and so must not need that machinery: the deadline makes it terminal no matter what
#: the pod does, and the TTL then collects it. Generous enough for a genuinely slow pull of
#: a multi-GB simulator image.
WARM_DEADLINE_SECONDS = 900

#: How long a finished prewarm Job sticks around. Long enough that a repeated call lands on
#: the existing object (a cheap 409) instead of recreating it -- ``get_image_build_status``
#: has one path that re-fires per poll by design.
#:
#: The flip side bounds what ``Always`` below can do for a *Job*: a repeat inside this window
#: is a 409, so nothing is re-pulled even if the tag moved. Harmless where the Job is used --
#: an experiment ref is content-addressed, so the same ref is the same bytes by construction --
#: and it is why the family images, whose refs float, are declared by a DaemonSet whose restart
#: stamp forces a roll instead. A time-varying Job name would not be the fix: idempotency by
#: name is what removes the need for any in-process record at all.
WARM_TTL_SECONDS = 600

#: Infrastructure, not workload: enough to schedule, not enough to matter against a node's
#: capacity. The container does nothing but exist.
WARM_CPU_REQUEST = "10m"
WARM_MEMORY_REQUEST = "32Mi"

#: Label every prewarm Job carries. A *different* value from the build Job's
#: ``image-builds``, because ``ClusterService._sweep_build_contexts`` selects on that one to
#: decide which staged contexts are still live -- a prewarm stages nothing, and appearing in
#: that list would make it look like a build in flight.
WARM_JOBGROUP = "image-warm"


def warm_id_for(image_ref: str) -> str:
    """Deterministic, DNS-1123-safe Job name for prewarming *image_ref*.

    Derived from the ref rather than taken from the caller, because the callers do not share
    one id: an experiment image has a ``build_id``, a family image has none, and a recorded
    per-role image is a digest. Keying on the ref is the one thing all three have, and it is
    also the right key -- two callers wanting the same bytes on the node should collapse onto
    one Job.

    The ref is far too unconstrained to use directly (a registry host, a path, ``:`` or
    ``@sha256:``, up to 60+ characters), so a readable stem is kept for a human reading
    ``kubectl get jobs`` and a hash of the **full** ref carries the identity.
    """
    stem = re.sub(r"[^a-z0-9]+", "-", image_ref.rsplit("/", 1)[-1].lower()).strip("-")
    digest = hashlib.sha256(image_ref.encode()).hexdigest()[:12]
    return f"imgwarm-{stem[:32].strip('-')}-{digest}" if stem else f"imgwarm-{digest}"


def warm_job_manifest(*, image_ref: str, namespace: str,
                      pull_secret_name: str = "") -> dict:
    """A Job that exists only to make the kubelet pull *image_ref*.

    ``pull_secret_name`` is the deployment's registry pull credential. It is not optional in
    practice on a private registry: without it the pod sits in ``ImagePullBackOff`` and the
    prewarm silently does nothing on exactly the deployment that needed it most.

    No ``hostAliases``. They would be pointless here -- the image pull is performed by the
    container runtime on the node, which reads neither pod specs nor CoreDNS, so a registry
    resolvable only via ``hostAliases`` cannot be pulled from at all (the same node-level
    scope as registry TLS trust).
    """
    labels = {"jobgroup": WARM_JOBGROUP}
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": warm_id_for(image_ref),
            "namespace": namespace,
            "labels": labels,
        },
        "spec": {
            "backoffLimit": 0,
            # Both, and neither is redundant: see WARM_DEADLINE_SECONDS.
            "activeDeadlineSeconds": WARM_DEADLINE_SECONDS,
            "ttlSecondsAfterFinished": WARM_TTL_SECONDS,
            "template": {
                "metadata": {"labels": dict(labels)},
                "spec": {
                    "restartPolicy": "Never",
                    **({"imagePullSecrets": [{"name": pull_secret_name}]}
                       if pull_secret_name else {}),
                    "containers": [{
                        "name": "warm",
                        "image": image_ref,
                        # The pull is the work; this just has to be something the kubelet
                        # will start.
                        "command": ["/bin/true"],
                        # `Always`, and this is the only container here that wants it. A
                        # floating tag is never re-pulled once a node holds bytes for it, so
                        # `IfNotPresent` would make this confirm a stale cache rather than
                        # refresh it -- and since every campaign pod runs `IfNotPresent` so
                        # that a sweep does not depend on the registry, this Job is the only
                        # place a re-pushed `:latest` reaches a node at all. The cost is one
                        # manifest check: `Always` re-transfers no layer whose digest the
                        # node already has. Affordable *here* because a prewarm never fails
                        # its caller, so an unreachable registry leaves exactly the situation
                        # that held before the feature existed.
                        "imagePullPolicy": "Always",
                        "resources": {"requests": {"cpu": WARM_CPU_REQUEST,
                                                   "memory": WARM_MEMORY_REQUEST}},
                    }],
                },
            },
        },
    }


def warm_image(k8s_batch, namespace: str, image_ref: str,
               pull_secret_name: str = "") -> bool:
    """Create the prewarm Job for *image_ref*. True if this call created it.

    False means it was already there -- another caller got here first, or this is the same
    caller polling again -- which is a success, not a failure.

    Raises whatever the API raises, apart from the 409 that idempotency is built on. The
    callers are the ones that decide a prewarm must never break them, and they say so with
    their own ``except``; swallowing everything here would also hide a misconfigured
    namespace or a revoked token from the log.
    """
    from kubernetes import client

    manifest = warm_job_manifest(image_ref=image_ref, namespace=namespace,
                                 pull_secret_name=pull_secret_name)
    name = manifest["metadata"]["name"]
    try:
        k8s_batch.create_namespaced_job(namespace, manifest)
    except client.exceptions.ApiException as e:
        if e.status == 409:
            logger.debug("prewarm %s already exists for %s", name, image_ref)
            return False
        raise
    logger.info("prewarming %s on the cluster (Job %s)", image_ref, name)
    return True


#: The family members worth pulling onto a node ahead of time, and the reason each is here.
#:
#: ``robovast`` is the ``FROM`` of every experiment image and what a campaign with no
#: simulator runs, so warming it is also what makes the per-build base prewarm a
#: one-second no-op most of the time. ``robovast-roqsim`` is the largest image in the
#: family and what both roqsim shapes run. ``robovast-sidecar`` is tiny but sits on the
#: critical path of every exec pod and build Job as their init container.
#:
#: ``robovast-controller`` is deliberately absent: it *is* the service Deployment, which
#: runs ``imagePullPolicy: Always``, so the kubelet pulls it during the rollout that
#: ``setup``/``upgrade`` already performs. Warming it would duplicate that pull.
#:
WARM_FAMILY_MEMBERS = ("robovast", "robovast-roqsim", "robovast-sidecar")

#: Name of the DaemonSet holding the family images on the nodes. Fixed, not derived: it is one
#: object per deployment, and a stable name is what makes an ``upgrade`` patch the existing one
#: instead of standing a second one up beside it.
WARM_DAEMONSET_NAME = "robovast-image-warm"

#: What the resident containers sleep for. A plain integer of seconds rather than
#: ``sleep infinity``: the latter is a GNU coreutils extension, and ``robovast-sidecar`` is
#: alpine, where busybox rejects it and the pod would crash-loop on every node. ~68 years,
#: which also fits a 32-bit int.
WARM_SLEEP_SECONDS = 2147483647

def _warm_tolerations() -> list:
    """What the warm pod must tolerate, and it is not optional: a pod that does not tolerate
    what campaign pods tolerate skips exactly the nodes worth warming -- and reports success
    while doing it. Read from where the ResourceFlavor granting it is written rather than
    restated here, so there is one place to change if the taint ever does.
    """
    from .node_placement import CAMPAIGN_NODE_TOLERATIONS as KUEUE_JOB_TOLERATIONS
    return [dict(t) for t in KUEUE_JOB_TOLERATIONS]


def family_refs_to_warm() -> list:
    """The concrete refs :data:`WARM_FAMILY_MEMBERS` resolve to, for this environment.

    Resolved from the caller's own ``ROBOVAST_PROJECT`` / ``ROBOVAST_PROJECT_TAG``, which is
    correct precisely because ``setup``/``upgrade`` is the command that bakes those same
    values into the service pod -- so this warms the set the deployment is being pointed at,
    not the set it is being pointed away from.
    """
    from robovast.common.execution import family_image_ref, resolve_family_image
    return [resolve_family_image(family_image_ref(member),
                                 role=f"prewarm of {member}")
            for member in WARM_FAMILY_MEMBERS]


def warm_daemonset_manifest(*, image_refs: list, namespace: str, pull_secret_name: str = "",
                            stamp: str = "") -> dict:
    """A DaemonSet that holds every ref in *image_refs* on every node, one container each.

    One container per image, each one asleep. That is the whole design, and the
    two obvious economies are both worse: init containers that exit leave their images
    collectable again, and a single container can only pin one image. Running them all costs
    :data:`WARM_CPU_REQUEST` / :data:`WARM_MEMORY_REQUEST` per image per node -- millicores
    against a node sized for a simulator -- and buys the property the Job cannot have, that an
    image in use by a running container is an image the kubelet will not garbage-collect. It
    also keeps :data:`WARM_FAMILY_MEMBERS` a plain list where no entry's position means
    anything.

    Sleeping rather than the Job's ``/bin/true`` is the one place in this module
    where the image's *contents* matter: a DaemonSet pod may only carry
    ``restartPolicy: Always``, so a container that exits is restarted forever. The Job can
    point at an arbitrary experiment image precisely because it does not care; this cannot,
    which is why it is used only for the family images we build.

    *stamp* goes into the pod template's restart annotation, and without it this mechanism
    silently does nothing in the case it exists for. A floating tag makes every field here
    byte-identical across pushes, so patching would change no field, roll no pod and re-pull
    nothing -- the trap ``service_deploy`` documents at
    :data:`~.service_deploy.RESTART_ANNOTATION`. The stamp forces the roll; the roll plus
    ``imagePullPolicy: Always`` is what makes the bytes current.

    No ``nodeSelector``. Every node that can run a pod is a node that may run a cell, and an
    untainted node the campaigns happen not to use costs one pull it never reads -- while a
    missing selector cannot make it *skip* a node, which is the failure that matters.
    """
    from .service_deploy import RESTART_ANNOTATION

    refs = list(image_refs)
    if not refs:
        # Never a silent no-op: an empty set means the family failed to resolve, and the API
        # server's own complaint about a container-less pod names none of that.
        raise ValueError("a warm DaemonSet needs at least one image ref")

    labels = {"jobgroup": WARM_JOBGROUP, "name": WARM_DAEMONSET_NAME}
    pod_spec = {
        "containers": [{
            "name": _warm_container_name(ref),
            "image": ref,
            "command": ["sleep", str(WARM_SLEEP_SECONDS)],
            # `Always`, for the reason the Job's own comment gives.
            "imagePullPolicy": "Always",
            "resources": {"requests": {"cpu": WARM_CPU_REQUEST,
                                       "memory": WARM_MEMORY_REQUEST}},
        } for ref in refs],
        "tolerations": _warm_tolerations(),
    }
    if pull_secret_name:
        pod_spec["imagePullSecrets"] = [{"name": pull_secret_name}]

    return {
        "apiVersion": "apps/v1",
        "kind": "DaemonSet",
        "metadata": {"name": WARM_DAEMONSET_NAME, "namespace": namespace,
                     "labels": {"jobgroup": WARM_JOBGROUP}},
        "spec": {
            "selector": {"matchLabels": {"name": WARM_DAEMONSET_NAME}},
            # Nothing consumes these pods, so there is no availability to preserve and rolling
            # one node at a time would only make the refresh slower than what wants it.
            "updateStrategy": {"type": "RollingUpdate",
                               "rollingUpdate": {"maxUnavailable": "100%"}},
            "template": {
                "metadata": {"labels": labels,
                             **({"annotations": {RESTART_ANNOTATION: stamp}} if stamp else {})},
                "spec": pod_spec,
            },
        },
    }


def _warm_container_name(image_ref: str) -> str:
    """Readable, DNS-1123-safe container name for *image_ref*.

    Derived from the repository alone, with the tag deliberately dropped: a container name is
    a merge key to Kubernetes, so a name carrying the tag would make a tag bump look like a
    *different* container -- adding one beside the old rather than replacing it. Unlike
    :func:`warm_id_for`, which needs the full ref because two tags of one repo are two
    different things to warm, this one wants exactly the opposite.
    """
    repo = image_ref.rsplit("/", 1)[-1].split(":")[0]
    stem = re.sub(r"[^a-z0-9]+", "-", repo.lower()).strip("-")
    return f"pull-{stem[:40].strip('-')}" if stem else "pull"


def apply_warm_daemonset(k8s_apps, namespace: str, manifest: dict) -> bool:
    """Create the warm DaemonSet, or replace the existing one with *manifest*. True if created.

    An update rather than a delete-and-recreate: the DaemonSet controller then rolls the pods
    itself, so a refresh never leaves the nodes with nothing warm in between.

    A **replace** rather than a patch, and the difference is not cosmetic. A patch is a
    strategic merge, which merges the container list by name -- so a family member whose set
    changed would be added beside the old entry instead of superseding it, and a removed member
    would linger forever. A replace says what this function means: make it look like this.
    ``spec.selector`` is the one immutable field and it is a constant here, so there is nothing
    a replace can be rejected for.
    """
    from kubernetes import client

    try:
        k8s_apps.create_namespaced_daemon_set(namespace, manifest)
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise
        k8s_apps.replace_namespaced_daemon_set(WARM_DAEMONSET_NAME, namespace, manifest)
        logger.info("refreshed the image-warm DaemonSet in %s", namespace)
        return False
    logger.info("created the image-warm DaemonSet in %s", namespace)
    return True


def delete_warm_daemonset(namespace: str, kube_context=None) -> bool:
    """Remove the warm DaemonSet. True if one was there to remove.

    Teardown deletes named objects rather than the namespace, so without this the warm pods
    outlive the deployment they belong to -- on every node, indefinitely. Never raises: this
    runs inside a teardown, where a failure to clean up one object must not abandon the rest.
    """
    from kubernetes import client

    from .kube_client import load_kube_config

    try:
        load_kube_config(kube_context)
        client.AppsV1Api().delete_namespaced_daemon_set(
            WARM_DAEMONSET_NAME, namespace,
            body=client.V1DeleteOptions(propagation_policy="Foreground"))
    except Exception as e:  # noqa: BLE001 - absent is the common case, and is success
        logger.debug("no image-warm DaemonSet to remove from %s: %s", namespace, e)
        return False
    logger.info("removed the image-warm DaemonSet from %s", namespace)
    return True


def warm_family_images(namespace: str, kube_context=None) -> list:
    """Hold the family images on every node. Returns the refs covered.

    Called from ``setup``/``upgrade`` because that is the moment every node is cold for the
    whole family -- a tag bump or a moved project means the next campaign pays a full pull of
    ``robovast-roqsim``, the largest image there is -- and the moment it costs nothing, since
    the pod is being restarted anyway so no campaign is mid-flight.

    Fire-and-forget: it declares the DaemonSet and returns. An ``upgrade`` must not block for
    minutes on a pull, and there is nothing to report back if it did -- nothing reads a
    prewarm. Which is also why the return value is what was *declared*, not what has landed:
    the DaemonSet controller is what makes it true, node by node, after this returns.

    One object rather than a Job per member, so unlike the Job path there is no partial
    outcome to report: the whole family is declared or none of it is. That is the trade for
    covering every node, and the cost is bounded by this never being able to fail its caller.
    """
    from datetime import datetime, timezone

    from kubernetes import client

    from .kube_client import load_kube_config
    from .service_deploy import REGISTRY_PUSH_SECRET_NAME

    try:
        load_kube_config(kube_context)
        apps = client.AppsV1Api()
        core = client.CoreV1Api()
        refs = family_refs_to_warm()
    except Exception as e:  # noqa: BLE001 - a prewarm must never fail a finished deployment
        logger.warning("could not prewarm the family images: %s", e)
        return []

    # Looked for rather than assumed, the same way the service resolves it: naming a Secret
    # that does not exist keeps the pod from starting, so a deployment on a public registry
    # must warm *without* a credential rather than not at all.
    try:
        core.read_namespaced_secret(REGISTRY_PUSH_SECRET_NAME, namespace)
        pull_secret = REGISTRY_PUSH_SECRET_NAME
    except Exception:  # noqa: BLE001 - absent, or unreadable; either way, no credential
        pull_secret = ""

    try:
        apply_warm_daemonset(apps, namespace, warm_daemonset_manifest(
            image_refs=refs, namespace=namespace, pull_secret_name=pull_secret,
            stamp=datetime.now(timezone.utc).isoformat()))
    except Exception as e:  # noqa: BLE001 - as above: a slow first pod, not a failed deploy
        logger.warning("could not prewarm the family images: %s", e)
        return []
    return refs
