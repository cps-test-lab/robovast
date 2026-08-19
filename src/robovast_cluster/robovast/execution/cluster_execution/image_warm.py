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

Deliberately **not** submitted to Kueue: a prewarm admitted behind a full sweep warms the
node after the thing that needed it, which is worse than not warming at all. That follows
the build Job, which carries no queue label either; campaign and postprocessing Jobs are
the ones that do. Its footprint is :data:`WARM_CPU_REQUEST` / :data:`WARM_MEMORY_REQUEST`
for at most the deadline, and it creates no Workload object, so Kueue's quotas are
untouched.
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
                        # will start. `IfNotPresent` so an already-warm node does nothing.
                        "command": ["/bin/true"],
                        "imagePullPolicy": "IfNotPresent",
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
WARM_FAMILY_MEMBERS = ("robovast", "robovast-roqsim", "robovast-sidecar")


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


def warm_family_images(namespace: str, kube_context=None) -> list:
    """Prewarm the family images a campaign will run. Returns the refs it asked for.

    Called from ``setup``/``upgrade`` because that is the moment every node is cold for the
    whole family -- a tag bump or a moved project means the next campaign pays a full pull of
    ``robovast-roqsim``, the largest image there is -- and the moment it costs nothing, since
    the pod is being restarted anyway so no campaign is mid-flight.

    Fire-and-forget: it creates Jobs and returns. An ``upgrade`` must not block for minutes
    on a pull, and there is nothing to report back if it did -- nothing reads a prewarm.

    Returns the refs regardless of whether each Job was created or already existed, because
    the caller uses them to say what it warmed, and "already warming" is not a different
    answer to that question. An empty list means the cluster could not be reached at all,
    which is not this function's business to escalate: the deployment it belongs to has
    already succeeded by the time it runs.
    """
    from kubernetes import client

    from .kube_client import load_kube_config
    from .service_deploy import REGISTRY_PUSH_SECRET_NAME

    refs = family_refs_to_warm()
    try:
        load_kube_config(kube_context)
        batch = client.BatchV1Api()
        core = client.CoreV1Api()
        # Looked for rather than assumed, the same way the service resolves it: naming a
        # Secret that does not exist keeps the pod from starting, so a deployment with a
        # public registry must warm *without* a credential rather than not at all.
        try:
            core.read_namespaced_secret(REGISTRY_PUSH_SECRET_NAME, namespace)
            pull_secret = REGISTRY_PUSH_SECRET_NAME
        except client.exceptions.ApiException:
            pull_secret = ""
        for ref in refs:
            warm_image(batch, namespace, ref, pull_secret)
    except Exception as e:  # noqa: BLE001 - a prewarm must never fail a finished deployment
        logger.warning("could not prewarm the family images: %s", e)
        return []
    return refs
