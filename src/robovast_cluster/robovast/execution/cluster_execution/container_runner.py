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
"""Cluster backend for auxiliary variation containers (aux Pod + ``pods/exec``).

When a variation plugin declares a
:class:`~robovast.common.variation.container_runner.ContainerSpec`, the service
starts **one aux Pod per campaign** holding a kept-alive container per spec, and
runs the plugin's commands inside it via the Kubernetes ``pods/exec`` subresource
— the in-cluster equivalent of ``docker exec``.

**Why a separate pod (and what that costs).** A *sidecar* sharing an ``emptyDir``
with the driver would satisfy the plugin contract's "workspace visible at the same
absolute path on both sides" for free. It is not available: the driver runs inside
the long-lived ``robovast-service`` pod, and a pod's container set is immutable, so
a *campaign-specific* sidecar of it is impossible. Instead each aux container gets
its own Pod, created the first time something asks for it, and this module **emulates
the shared workspace** by moving it into the pod before every ``run()`` and
copying the results back afterwards, at the *same absolute path*.

**Created on demand, never predicted.** :class:`AuxPodSession` is entered for a span
knowing nothing about what that span will need, and builds a pod when the factory is
first called for a spec. Three kinds of thing ask -- a variation plugin, an
``execution.generate`` input generator, and the simulator backend's query that resolves
what a world is made of -- and the list is open. Deciding in advance instead, by reading
the ``.vast``, is a second implementation of an enumeration composition already performs:
whatever it does not cover is a campaign that fails while composing, on a container it
declared. Asking is the only enumeration that cannot be incomplete.

**How the workspace moves.** Through the service's **data plane**, the same transport a
campaign Job, an image-build context and the container-exec runner use
(:mod:`.pod_access`): the runner's workspace *is* a staged tree on the service's disk,
and a ``transfer`` container from the sidecar image fetches it into the pod as one tar
stream before a command and delivers it back as one afterwards. That container shares
the pod's emptyDirs with the aux container and carries the slot's token, so nothing is
assumed about the aux image -- it belongs to a plugin author and is not ours to add tools
to -- and no credential reaches it.

The workspace sits under the pod's staged slot on the service's disk and is mounted in
the pod at that same absolute path, so the plugin contract's "one path on both sides"
holds with no copy in between: the fetch extracts onto the mount, the delivery extracts
onto the service's own directory. Nothing goes through the ``pods/exec`` channel but the
commands themselves; that channel is a text websocket the client cannot half-close, so a
receiver waiting for stdin EOF there waits forever.

Consequences to know:

* The plugin contract is preserved for the *stage inputs → run → read outputs*
  pattern (what ``FloorplanVariation`` does). It is **narrowed** in one respect:
  the two sides do not share a live filesystem, so a command that expects the
  caller to observe its writes *while it is still running* (or vice versa) will
  not see them — only the state at copy-in/copy-out boundaries.
* An **empty workspace transfers nothing**: a generator whose inputs all live in its own
  image stages no files, and a round trip per ``run()`` for zero bytes is pure latency.
* Composition needs the data plane to be reachable from the pod. That is not a new
  dependency in practice — the campaign being composed cannot run without it either —
  but it is a dependency *at composition time*, and it fails loudly rather than falling
  back.
* Aux compute is scheduled by Kubernetes as its own pod, so it never competes
  with the service (the control plane) for resources.

Lifecycle: each pod is labeled with its campaign, owned by the service pod (so
Kubernetes garbage-collects it if the service is replaced), carries an
``activeDeadlineSeconds`` backstop, and is deleted when the span that created it ends.
"""

import contextlib
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from urllib.parse import quote

from robovast.common.errors import CampaignConfigError, ExecTargetGone

from . import pod_access

logger = logging.getLogger(__name__)

#: Default wall-clock cap for an aux pod, so a leaked one always dies by itself.
DEFAULT_AUX_DEADLINE_SECONDS = 12 * 60 * 60

#: Cap on a single command exec'd into an aux container. Deliberately generous — a
#: floorplan turning into a mesh is legitimately slow, and a cap that fires on real work
#: would be worse than none. The point is only that the wait *terminates*: without it a
#: helper that hangs hangs the campaign's worker thread forever, and a campaign stuck in
#: composition with no log line is indistinguishable from one that is merely slow.
AUX_EXEC_LIMIT_S = 2 * 60 * 60

#: The limit a *held* aux container is started with, which becomes its pod's hard deadline.
#: :data:`AUX_EXEC_LIMIT_S`, because the thing that has to fit inside it is one command from
#: a variation and that is already what caps one — a shorter deadline here would kill a
#: composition the runner was still willing to wait for. Idleness, not this, is what
#: normally ends a held pod; this is the backstop for a service that dies holding one.
AUX_HOLD_LIMIT_S = AUX_EXEC_LIMIT_S

#: Label selector identifying every aux pod (one per aux container a span asked for).
AUX_LABEL = "app=robovast-aux"

#: The staged slot every aux pod's workspaces live under, on the service's disk.
AUX_WORKSPACE_PREFIX = "aux-workspaces"

#: The container from the sidecar image that moves the workspace in and out of the pod.
#: Every file operation the runner needs -- the fetch, the delivery, filling an exposed
#: mount, removing a finished workspace -- is exec'd here, so the aux image is asked for
#: nothing but the plugin's own command.
TRANSFER_CONTAINER = "transfer"

#: Absolute DIRECTORIES an aux container can be asked to expose a staged input at, via
#: :meth:`ClusterContainerRunner.expose`. A fixed list rather than anything a caller picks,
#: because the mount has to be declared when the *pod* is built, long before a runner knows
#: what it will stage -- and a path nobody mounted is not writable in an arbitrary image.
#: Each one becomes an emptyDir, mounted on every aux container and made world-writable by
#: the transfer container as it starts, so a new entry here is all a new fixed mount needs.
#:
#: ``/config`` is where a job mounts a campaign's ``run_files``, so a world's own
#: ``/config/...`` references resolve there for a rebuild exactly as they did for the run.
#: ``/aux`` is the neutral one, for a single file that has to appear at a fixed path and does
#: NOT belong to a campaign tree -- the scene build's world-overrides document, which cannot
#: travel on argv (a nested tree does not survive ``--set``) and must not be nested inside
#: ``/config``, where it would have to be copied into another input's mount. It is a path of
#: our own rather than ``/tmp`` on purpose: an emptyDir over ``/tmp`` would shadow whatever
#: the aux image keeps there, and the aux image is not ours.
AUX_MOUNTABLE_PATHS = ("/config", "/aux")


#: The volume holding the runners' workspaces. A fixed name rather than one derived from its
#: mount path: that path is the service's own scratch directory, mirrored so a generator sees
#: one path on both sides, and it is as deep as the deployment's results root makes it -- far
#: past the 63 characters a volume name may have.
WORKSPACE_VOLUME = "aux-workspace"


def _mount_volume_name(path: str) -> str:
    """A DNS-label volume name for a mountable absolute path (``/config`` -> ``aux-config``).

    Only for :data:`AUX_MOUNTABLE_PATHS`, a fixed list of short paths; the workspace root has
    :data:`WORKSPACE_VOLUME`.
    """
    return "aux-" + re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-")


def aux_slot(pod_name: str) -> str:
    """The staged slot holding every workspace moved in and out of *pod_name*.

    Keyed on the pod, because the pod is what carries the token and a token reaches one
    slot. Each runner's workspace is a directory of its own inside it -- a runner is
    built per *variation*, and two variations sharing one pod must not share a tree,
    or whichever finished first would remove the other's files in
    :meth:`ClusterContainerRunner.close`.
    """
    return f"{AUX_WORKSPACE_PREFIX}/{pod_name}"


def aux_workspace_root(stage_dir, pod_name: str) -> Path:
    """Where *pod_name*'s workspaces live: the slot's directory on the service's disk,
    and the path the pod mounts its emptyDir at, so a workspace has one address on both
    sides. *stage_dir* is the service's ``staged_dir(slot)``."""
    return Path(stage_dir(aux_slot(pod_name)))


def aux_pod_name(campaign_id: str, container: str = "") -> str:
    """Deterministic pod name for *campaign_id*'s aux container.

    One pod per aux *container*, because a pod's container set is fixed when it is
    created and nothing knows the set in advance -- see the module docstring. *container*
    is a spec's :meth:`~robovast.common.variation.container_runner.ContainerSpec.container_name`
    and is appended (its ``aux-`` prefix dropped, already in the name); omitted, this is
    the campaign's base name, which is what a caller sweeping by name uses.

    Deterministic so a retried span addresses the pod its predecessor left behind rather
    than accumulating one per attempt; the collision that name causes is handled where the
    pod is created.
    """
    from .cluster_execution import _label_safe_campaign
    base = f"robovast-aux-{_label_safe_campaign(campaign_id)}"
    if not container:
        return base
    suffix = container[len("aux-"):] if container.startswith("aux-") else container
    return f"{base}-{suffix}" if suffix else base


def cleanup_aux_pods(namespace="default", kube_context=None, campaign=None):
    """Delete aux pods (label ``app=robovast-aux``). Best-effort.

    With *campaign* given, deletes only that campaign's aux pods so concurrent
    campaigns are left untouched; otherwise deletes every aux pod. Selected by the
    campaign LABEL rather than by name, so it catches every pod a span created whatever
    each is called. Backs ``vast cluster jobs-cleanup`` (the successor to the
    controller-pod reap).
    """
    from kubernetes import client

    from .cluster_execution import _label_safe_campaign

    selector = AUX_LABEL
    if campaign is not None:
        selector += f",campaign-id={_label_safe_campaign(campaign)}"
    from .kube_client import load_kube_config
    try:
        load_kube_config(context=kube_context)
        core = client.CoreV1Api()
        pods = core.list_namespaced_pod(namespace, label_selector=selector).items
    except Exception as e:  # noqa: BLE001
        logger.debug("Could not list aux pods for cleanup: %s", e)
        return 0
    deleted = 0
    for pod in pods:
        try:
            core.delete_namespaced_pod(pod.metadata.name, namespace,
                                       grace_period_seconds=0)
            deleted += 1
        except Exception as e:  # noqa: BLE001
            logger.debug("Could not delete aux pod %s: %s", pod.metadata.name, e)
    return deleted


def _aux_image(image: str, *, project: str | None = None, tag: str | None = None) -> str:
    """An aux container's image as a Pod may carry it: a ``family:`` ref resolved, anything else
    left exactly as written.

    Left verbatim on purpose when it is not a family ref: an image a campaign names is used as
    written everywhere else in RoboVAST, and an aux container is no place to start rewriting one.

    *project* and *tag* are the campaign's (``--image-project``); without them the family
    resolves from the service's environment, which is what a preview's held pod wants.
    """
    from robovast.common.execution import is_family_image_ref, resolve_family_image

    if not is_family_image_ref(image):
        return image
    return resolve_family_image(image, project=project, tag=tag,
                                role="image for an auxiliary container")


def build_aux_pod_manifest(campaign_id, specs, namespace, owner_ref=None, *,
                           stage_dir, token_for,
                           deadline_seconds: int = DEFAULT_AUX_DEADLINE_SECONDS,
                           pull_secret: str = "", pod_name: str = "",
                           container_names=None, extra_labels: dict | None = None,
                           images: dict | None = None, sidecar_image: str | None = None) -> dict:
    """Manifest for an aux Pod: one kept-alive container per spec, plus the transfer one.

    Each container runs the aux image with its one-shot entrypoint overridden by
    the spec's ``keep_alive_command``, so it stays up for the whole span and
    every ``run()`` pays only the exec setup (no per-call create or image re-pull).

    Two spans build one manifest here, which is the point: a *campaign*'s pod (named after
    the campaign and the container it holds, deleted when the span ends) and a *held* one
    owned by the service's exec manager (named after its slot, reaped on idleness).
    *pod_name*, *container_names* and *extra_labels* are how each says which — a held pod's
    name and container have to be the ones the exec runner already addresses and sweeps by —
    and everything else about an aux pod is identical. Forking a second builder for that
    would have put the transfer container, the mountable emptyDirs and the pull secret in
    two places. The campaign label is set either way, so a sweep by campaign finds every
    pod a span created whatever each is named.

    *container_names* maps a spec's ``container_name()`` to the name to use instead;
    unnamed specs keep their own.

    *images* (``{container_name(): ref}``) and *sidecar_image* are what a campaign's pod runs:
    the digests its launch fixed (:class:`CampaignImagePins`), so the pod runs what the launch
    record names. Left out -- a held pod, which serves no campaign -- the spec's image and the
    sidecar are resolved from the service's environment.

    *owner_ref* should be the **service pod** so Kubernetes garbage-collects this
    pod when the service is replaced — the same "dies with its parent" guarantee
    the old controller-pod sidecar had.

    *pull_secret* names an image-pull secret, which a spec naming the *campaign's own*
    image needs: that one points at a private registry, while an aux image may equally be
    a public one (``ghcr.io/secorolab/scenery_builder``) that needs none.

    *stage_dir* and *token_for* are the service's ``staged_dir(slot)`` and
    ``scoped_token(scope)``. The pod's slot is :func:`aux_slot` of its name; the
    ``transfer`` container from the sidecar image carries a token scoped to it
    (:func:`pod_access.staged_pod_env`) and mounts the slot's directory at the path the
    service has it, beside every :data:`AUX_MOUNTABLE_PATHS` emptyDir, so a runner's
    workspace is one address on both sides and the aux image needs no tool of ours.
    The token is a plain value: the pod lives as long as its slot, and the slot holds
    nothing but scratch trees deleted with it.
    """
    from robovast.common.execution import resolve_sidecar_image

    from .cluster_execution import _label_safe_campaign
    from .kubernetes_backend import pull_policy_for

    name = pod_name or aux_pod_name(campaign_id)
    workspace_root = str(aux_workspace_root(stage_dir, name))
    # One emptyDir per shared path, on every container of the pod: the workspace root the
    # runner fetches into, and each mountable path a runner may expose a tree at. Empty
    # unless a runner stages into it, so a generator that never asks pays a volume and
    # nothing else; and declared here rather than at ``expose`` time because a Pod's
    # mounts are fixed when it is created.
    shared_paths = (workspace_root, *AUX_MOUNTABLE_PATHS)
    shared_mounts = [{"name": WORKSPACE_VOLUME, "mountPath": workspace_root}] + [
        {"name": _mount_volume_name(path), "mountPath": path} for path in AUX_MOUNTABLE_PATHS]

    containers = []
    for spec in specs:
        image = (images[spec.container_name()] if images is not None
                 else _aux_image(spec.image))
        container = {
            "name": (container_names or {}).get(spec.container_name(),
                                                spec.container_name()),
            # A `family:<member>` ref is SYMBOLIC and must be resolved before it reaches a Pod --
            # kubelet reads an unresolved one as `docker.io/library/family:<member>` and fails the
            # pull with `insufficient_scope`, which reads like a credentials problem rather than an
            # unresolved reference. For a campaign it is the digest its launch fixed
            # (*images*); for a held pod it is resolved here, in the service, for the same
            # reason the transfer container below is: this process is the one carrying the
            # deployment's project and tag.
            "image": image,
            # From the ref, like every other pod this package writes: see
            # ``pull_policy_for``. A campaign's pod runs digests; a held pod runs what the spec
            # names, the deployment's own floating tag whenever it is a `family:` member.
            "imagePullPolicy": pull_policy_for(image),
            "command": list(spec.keep_alive_command),
            "volumeMounts": list(shared_mounts),
        }
        if spec.env:
            container["env"] = [{"name": k, "value": str(v)} for k, v in spec.env.items()]
        if spec.run_as_user:
            uid = spec.run_as_user.split(":", 1)[0]
            try:
                container["securityContext"] = {"runAsUser": int(uid)}
            except ValueError:
                pass
        containers.append(container)

    # Every emptyDir gets the same treatment and for the same reason: it belongs to root,
    # and a spec's ``run_as_user`` means the container that has to write into it may be
    # nobody in particular. Done by the transfer container as it starts, which is before
    # anything execs into the pod.
    chmods = " && ".join(f"chmod 0777 {shlex.quote(path)}" for path in shared_paths)
    sidecar = sidecar_image if sidecar_image is not None else resolve_sidecar_image()
    containers.append({
        "name": TRANSFER_CONTAINER, "image": sidecar,
        "imagePullPolicy": pull_policy_for(sidecar),
        # Idle for the pod's whole deadline: the pod's own ``activeDeadlineSeconds`` ends
        # it at the same moment, and a bounded sleep needs nothing of the image's sleep.
        "command": ["sh", "-c", f"{chmods} && exec sleep {int(deadline_seconds)}"],
        "env": pod_access.staged_pod_env(namespace, token_for(
            pod_access.staged_scope(aux_slot(name)))),
        "volumeMounts": list(shared_mounts),
    })

    # *extra_labels* last, and it may legitimately replace ``app``: a held pod is the exec
    # manager's, so the exec runner's stray sweep must be the one that finds it. Exactly one
    # sweep should own a pod — a held one answering to `cleanup_aux_pods` as well would let
    # a campaign's cleanup delete a container somebody's preview is composing against.
    metadata = {
        "name": name,
        "namespace": namespace,
        "labels": {"app": "robovast-aux",
                   "campaign-id": _label_safe_campaign(campaign_id),
                   **(extra_labels or {})},
    }
    if owner_ref:
        metadata["ownerReferences"] = [owner_ref]
    spec = {
        "restartPolicy": "Never",
        # Backstop: even if teardown and the reaper both miss it, it dies.
        "activeDeadlineSeconds": int(deadline_seconds),
        "containers": containers,
        "volumes": [{"name": mount["name"], "emptyDir": {}} for mount in shared_mounts],
    }
    if pull_secret:
        spec["imagePullSecrets"] = [{"name": pull_secret}]
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": metadata,
        "spec": spec,
    }


def service_pod_owner_reference(core_v1, namespace):
    """An ownerReference to *this* (service) pod, or None if it can't be resolved.

    Owning aux pods by the current service **Pod** (not the Deployment) is
    deliberate: a service restart abandons its in-flight campaigns, so their aux
    pods should be collected with it rather than outlive it.
    """
    pod_name = os.environ.get("HOSTNAME")
    if not pod_name:
        return None
    try:
        pod = core_v1.read_namespaced_pod(pod_name, namespace)
    except Exception as exc:  # pylint: disable=broad-except - not fatal, reaper covers it
        logger.debug("Could not resolve service pod %s for ownerReference: %s",
                     pod_name, exc)
        return None
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "name": pod.metadata.name,
        "uid": pod.metadata.uid,
        "controller": False,
        "blockOwnerDeletion": False,
    }


class CampaignImagePins:
    """The images one campaign's pods run, fixed to digests before the first of them exists.

    One per campaign, entered with its span (``ClusterService._aux_runner_context``), and
    reading and writing the campaign's own :class:`~robovast.execution.backends.RunOptions`:
    the sidecar and the helper images it fixes land in ``options.sidecar_image`` and
    ``options.aux_images``, which the batch runner reads too, so every pod of the campaign --
    aux or Job, first batch or fiftieth -- runs the same bytes. Each is recorded in the launch
    record the moment it is fixed, which is before the pod that runs it is created.

    On a replay (``options.images_fixed``) nothing is resolved: the record's digests are
    what the options already hold, and an image they do not fix is refused, naming it.

    *read_digest* is ``ref -> (digest, why)``: the deployment's registry, asked with the pull
    credential, since that is what the kubelet will pull with.
    """

    def __init__(self, campaign_root, options, read_digest):
        self._root = Path(campaign_root)
        self._options = options
        self._read_digest = read_digest
        # A composition may ask for two helper images from two threads, and the sidecar is
        # asked by every pod: one resolution each, never two that could disagree.
        self._lock = threading.Lock()

    def sidecar(self) -> str:
        """The sidecar's digest, fixed and recorded on the first ask."""
        from robovast.common.campaign_data import update_launch_images
        from robovast.common.execution import resolve_sidecar_image

        with self._lock:
            if self._options.sidecar_image:
                return self._options.sidecar_image
            if self._options.images_fixed:
                raise CampaignConfigError(
                    "the launch record this campaign replays fixes no digest for the sidecar "
                    "image, and a replay runs only recorded digests.")
            digest = self._fix(resolve_sidecar_image(
                project=self._options.image_project, tag=self._options.image_project_tag),
                "the sidecar image")
            update_launch_images(self._root, sidecar=digest)
            self._options.sidecar_image = digest
            return digest

    def aux(self, spec) -> str:
        """The digest *spec*'s aux container runs, fixed and recorded on the first ask."""
        from robovast.common.campaign_data import update_launch_images

        name = spec.container_name()
        with self._lock:
            if self._options.aux_images.get(name):
                return self._options.aux_images[name]
            if self._options.images_fixed:
                raise CampaignConfigError(
                    f"the launch record this campaign replays fixes no digest for its "
                    f"auxiliary container {name!r} ({spec.image}), and a replay runs only "
                    f"recorded digests. Its source never asked for that container, so the "
                    f"replay is not composing what its source composed.")
            digest = self._fix(_aux_image(spec.image, project=self._options.image_project,
                                          tag=self._options.image_project_tag),
                               f"auxiliary container {name!r}")
            update_launch_images(self._root, aux={name: digest})
            self._options.aux_images[name] = digest
            return digest

    def _fix(self, ref: str, what: str) -> str:
        digest, why = self._read_digest(ref)
        if not digest:
            raise CampaignConfigError(
                f"refusing to launch: every image a campaign runs is fixed to a digest before "
                f"any pod starts, and the digest of {what} {ref} could not be read: {why}. An "
                f"image the registry does not have has never been pushed; one it did not answer "
                f"for is retried by launching again.")
        return digest


class AuxPodSession:
    """Provides a span's auxiliary containers on demand, and deletes them after.

    Used as a context manager by the service's per-campaign worker thread, so a pod's
    lifetime is exactly the span's and the runner factory it installs is scoped to that
    worker (see ``config_generation.set_container_runner_factory``).

    **Nothing is created on entry.** The factory builds a pod the first time it is asked
    for a spec, and a span that asks for none creates nothing -- so a campaign pays for a
    helper image exactly when something reaches for one, and no caller has to state in
    advance what a composition will want (see the module docstring).

    One pod per spec, because a pod's container set is fixed when it is created and the
    second spec is not known when the first arrives.

    **Not the exec manager's hold**, which provides the same thing on demand for the preview
    span and owns its lifetime already. Its held containers are a bounded pool
    (:data:`~robovast.service.container_exec.QUERY_POOL_MAX`), so a concurrent preview can
    evict a slot -- acceptable for a question that can be asked again, not for a campaign
    that is midway through composing against the container. A campaign's pod is therefore
    span-scoped and outside the pool. What the two must not diverge on is the pod itself:
    both build it through :func:`build_aux_pod_manifest`, and both delete one that never
    became ready rather than leaving it to the deadline.
    """

    def __init__(self, campaign_id, namespace, core_v1=None, *, stage_dir, discard_staged,
                 token_for, ready_timeout: float = 300.0, pull_secret: str = "",
                 kube_context: str | None = None, on_pending=None,
                 should_stop=None, image_pins=None):
        self.campaign_id = campaign_id
        # The campaign's :class:`CampaignImagePins`: every pod this span creates runs the
        # digests they fix, recorded before the pod exists. ``None`` for a span that serves no
        # campaign -- a scene build -- whose pod resolves its images as they are named.
        self._image_pins = image_pins
        self.pull_secret = pull_secret
        self.namespace = namespace
        self._core_v1 = core_v1
        # Only consulted when no client was handed in. It must still be the *service's*
        # context: falling back to the kubeconfig's current one puts this campaign's aux
        # pod in whichever cluster the host happens to point at, while looking perfectly
        # valid — the same failure the container-exec runner has a regression test for.
        self._kube_context = kube_context
        self._ready_timeout = ready_timeout
        # Reported on every poll of the ready wait, so a caller with somebody watching can name
        # what the pod is stuck on rather than only what it timed out on. See `wait_pod_ready`.
        self._on_pending = on_pending
        # Polled by the same wait. A pull this span is waiting out is the one stretch of a
        # composition long enough for an operator to stop the campaign in, and a stop that
        # is answered only when the pull ends is one nothing distinguishes from a hang.
        self._should_stop = should_stop
        #: Container name -> the READY pod serving it. The memo that keeps a second command
        #: in the same span from paying a second create and image pull, so only a pod that
        #: can be exec'd into belongs here: a second ask after a failed create must repeat
        #: the create rather than be handed a name that never came up.
        self._pods: dict = {}
        #: Every pod name this session created, ready or not -- the delete list, kept apart
        #: from the memo for that reason. A pod whose image this cluster cannot pull exists
        #: and holds a node's capacity while it backs off, so the span still owns its death;
        #: leaving it to the deadline backstop is minutes of nothing waiting for it.
        self._created: set = set()
        # A variation and a generator compose in the same thread today, but nothing in the
        # contract says two runners cannot be asked for at once -- and two creates of the
        # same pod name is a 409 that would be handled as a leftover from a previous span.
        self._lock = threading.Lock()
        # The service's ``staged_dir``, ``discard_staged`` and ``scoped_token``: where a
        # runner's workspace lives, how the slot is dropped, and what the pod is given to
        # reach it. Required, because a pod built without them fails at the first
        # ``run()``, deep inside a plugin, instead of here where the cause is legible.
        self._stage_dir = stage_dir
        self._discard = discard_staged
        self._token_for = token_for

    def _client(self):
        if self._core_v1 is None:
            from .kube_client import core_v1_client
            self._core_v1 = core_v1_client(self._kube_context)
        return self._core_v1

    def __enter__(self):
        return self

    def runner_factory(self):
        """A ``factory(spec) -> ClusterContainerRunner`` over this span's pods.

        Creating the pod is part of *answering*, not of arranging: the factory blocks on
        the pull and the ready wait the first time a spec is asked for, and returns the
        runner when there is a container to exec into. A caller that never calls this never
        waits for anything.
        """
        def factory(spec):
            return ClusterContainerRunner(
                spec, self._pod_for(spec), self.namespace, self._client(),
                stage_dir=self._stage_dir, kube_context=self._kube_context,
                reprovision=self.replace, should_stop=self._should_stop)
        return factory

    def provision(self, spec):
        """Create *spec*'s pod now, and return its name.

        For a caller that already holds the spec — the scene cache builds one known image —
        so the pull and the schedule happen where it can report them rather than inside the
        first command. Idempotent, and the same pod the factory would have made: a caller
        that skips this loses nothing but the timing.

        This is not the prediction the module docstring warns about. The difference is who
        knows: a caller naming the spec it is about to use, against a caller reading a
        ``.vast`` to guess what a composition will ask for.
        """
        return self._pod_for(spec)

    def _pod_for(self, spec):
        """The name of the running pod holding *spec*'s container, creating it if needed."""
        name = spec.container_name()
        with self._lock:
            existing = self._pods.get(name)
            if existing:
                return existing
            pod_name = self._create_pod(spec, aux_pod_name(self.campaign_id, name))
            self._pods[name] = pod_name
            return pod_name

    def replace(self, spec):
        """Make *spec*'s pod again, after the one handed out went away. Returns its name.

        The memo exists so a second ask in one span costs no create and no image pull, and
        that is exactly what makes it wrong once the pod it names has ended: every later
        ask is handed a container nothing can exec into. Forgetting the entry is what turns
        the next ask back into a create.

        The name is derived from the campaign, so the create meets the remains of the old
        pod as a 409 — which :meth:`_create_pod` already waits out rather than adopting.
        """
        name = spec.container_name()
        with self._lock:
            self._pods.pop(name, None)
        return self._pod_for(spec)

    def _record_created(self, pod_name):
        """Note that *pod_name* now exists, before anything asks whether it works.

        Called between the create and the ready wait, so a pod that never becomes Running is
        deleted with the span rather than left to the deadline. Under ``_lock`` already, via
        the one caller.
        """
        self._created.add(pod_name)

    def _create_pod(self, spec, pod_name):
        """Create *pod_name* holding *spec*'s container and wait for it to be ready."""
        from kubernetes.client.rest import ApiException

        from .kube_client import wait_pod_gone, wait_pod_ready
        core = self._client()
        fixed = {}
        if self._image_pins is not None:
            # Before the create: a campaign's pod runs only digests its launch record holds.
            fixed = {"images": {spec.container_name(): self._image_pins.aux(spec)},
                     "sidecar_image": self._image_pins.sidecar()}
        manifest = build_aux_pod_manifest(
            self.campaign_id, [spec], self.namespace,
            owner_ref=service_pod_owner_reference(core, self.namespace),
            stage_dir=self._stage_dir, token_for=self._token_for,
            pull_secret=self.pull_secret, pod_name=pod_name, **fixed)
        try:
            core.create_namespaced_pod(self.namespace, manifest)
        except ApiException as e:
            if e.status != 409:
                raise RuntimeError(
                    f"could not create aux pod {pod_name}: {e.reason}") from e
            # A 409 is not "already exists → reuse it". The name is derived from the
            # campaign id, so the pod it collides with is this campaign's previous one —
            # usually still Terminating, and a Terminating pod never becomes Running
            # again. Adopting it means waiting out the full ready timeout for a corpse.
            # Wait for the delete to land, then create ours.
            logger.info("Aux pod %s still exists; waiting for it to go before recreating",
                        pod_name)
            with contextlib.suppress(ApiException):
                core.delete_namespaced_pod(pod_name, self.namespace,
                                           grace_period_seconds=0)
            wait_pod_gone(core, self.namespace, pod_name, timeout_s=self._ready_timeout)
            core.create_namespaced_pod(self.namespace, manifest)
        self._record_created(pod_name)
        logger.info("Aux pod %s created for %s of campaign %s",
                    pod_name, spec.container_name(), self.campaign_id)
        # Shared with the container-exec runner so a stuck pod names its reason
        # (ImagePullBackOff, say) instead of timing out with only an elapsed time —
        # which matters now that a spec may name the campaign's own private image.
        wait_pod_ready(core, self.namespace, pod_name,
                       timeout_s=self._ready_timeout, on_pending=self._on_pending,
                       should_stop=self._should_stop)
        return pod_name

    def _sweep_workspaces(self) -> None:
        """Drop every pod's staged slot, with whatever its runners left in it.

        Each runner removes its own workspace in ``close()``; this catches the ones whose
        close never ran — a composition that raised — and the slot directory itself.
        Best-effort, because a leftover copy of a workspace must not fail a campaign that
        otherwise finished.
        """
        for pod_name in sorted(self._created):
            try:
                removed = self._discard(aux_slot(pod_name))
            except Exception as e:  # noqa: BLE001 - cleanup never fails the campaign
                logger.warning("Could not sweep the aux workspaces of %s: %s", pod_name, e)
                continue
            if removed:
                logger.info("Swept leftover aux workspaces of %s", pod_name)

    def __exit__(self, exc_type, exc, tb):
        if not self._created:
            return False
        for pod_name in sorted(self._created):
            try:
                self._client().delete_namespaced_pod(pod_name, self.namespace)
                logger.info("Aux pod %s deleted", pod_name)
            except Exception as e:  # pylint: disable=broad-except - GC/reaper is the backstop
                logger.warning("Could not delete aux pod %s: %s", pod_name, e)
        self._sweep_workspaces()
        return False


class ClusterContainerRunner:
    """Runs a plugin's commands in a campaign's aux Pod via ``pods/exec``.

    ``workspace`` is a directory in the service, inside the pod's staged slot; it is
    moved into the aux pod at the identical absolute path around each :meth:`run`, so the
    plugin's absolute paths stay valid on both sides (see the module docstring for the
    one way this differs from a shared volume).

    *stage_dir* is the service's ``staged_dir(slot)``: it places the workspace where the
    pod's transfer container fetches it from and delivers it back to.
    """

    def __init__(self, spec, pod_name, namespace, core_v1=None,
                 exec_limit_s: float = AUX_EXEC_LIMIT_S, *, stage_dir,
                 kube_context: str | None = None, container: str = "",
                 reprovision=None, should_stop=None):
        self._spec = spec
        # The span's own flag, read by every exec this runner makes: mirroring the
        # workspace in, running the command, mirroring it back. A command in a pod is
        # bounded only by ``exec_limit_s``, which is hours, so without this a campaign
        # stopped while a variation is composing waited for it.
        self._should_stop = should_stop
        self._pod = pod_name
        self._namespace = namespace
        self._core_v1 = core_v1
        # See AuxPodSession: only used when no client was handed in, and it must be the
        # service's context rather than whatever the host kubeconfig points at.
        self._kube_context = kube_context
        # A campaign's aux pod names each container after its spec, so the spec is the
        # default. A pod held by the exec manager names its single container the way that
        # runner names every held container, and passes it — the pod name and the container
        # name have to come from the same place or one of them addresses nothing.
        self._container = container or spec.container_name()
        self._exec_limit_s = exec_limit_s
        # Inside the pod's slot on the service's disk, which the pod mounts at the same
        # path: the tree the transfer container fetches is this directory, and what it
        # delivers lands back in it. A name of its own per runner, because two runners
        # may share one pod (see ``aux_slot``).
        root = aux_workspace_root(stage_dir, pod_name)
        root.mkdir(parents=True, exist_ok=True)
        self.workspace = tempfile.mkdtemp(prefix="robovast_aux_", dir=root)
        self._exposed: dict = {}
        #: ``reprovision(spec) -> pod name``: make this spec's container again, for a
        #: :meth:`run` that found the one it was handed gone. Whoever owns the pod's
        #: lifetime provides it, because only they can replace it; without one a vanished
        #: container is simply reported, which is what a caller that cannot recover needs.
        self._reprovision = reprovision
        #: True once the workspace has been placed in the pod, so ``close`` knows
        #: whether there is anything there to drop. A runner that never transferred must not
        #: pay an exec -- and must not need a live pod -- just to remove nothing.
        self._staged = False

    def expose(self, host_path: str, container_path: str) -> None:
        """Also make *host_path* visible at the fixed *container_path* in the aux container.

        The tree still travels as part of the workspace -- there is one transport and this
        does not add a second. It is copied across inside the container, into the emptyDir
        the Pod already mounts, which is why only :data:`AUX_MOUNTABLE_PATHS` can be asked
        for: a path the Pod does not mount is not writable in an arbitrary image, and
        discovering that inside the tool would look like the tool's own failure.

        A *file* target is allowed when its DIRECTORY is one of those paths, because that is
        the shape a staged single file has: ``mount_at`` names the exact path the command was
        written for, filename included (``/aux/roqsim_scene_overrides.yaml``), and only the
        directory around it can be a volume. Without this the scene build failed on the
        cluster, at the one moment it is least diagnosable -- the run view asking for
        geometry -- while working under a bind mount, which does not care.

        A *host_path* outside the workspace is copied into it first (:meth:`_stage_into`), so
        the single transport is a property of this method rather than of every caller's
        discipline. A local ``docker`` runner bind-mounts whatever it is handed, so nothing
        there ever needed the tree to be in one particular place.
        """
        container_path = str(container_path)
        if container_path not in AUX_MOUNTABLE_PATHS \
                and os.path.dirname(container_path) not in AUX_MOUNTABLE_PATHS:
            raise ValueError(
                f"an aux container can only expose a staged input at one of "
                f"{list(AUX_MOUNTABLE_PATHS)}, or at a file directly inside one of them, "
                f"not {container_path!r}; a new path has to be added to "
                f"AUX_MOUNTABLE_PATHS so the Pod declares a volume for it.")
        self._exposed[container_path] = self._stage_into(str(host_path), container_path)

    def _stage_into(self, source: str, container_path: str) -> str:
        """*source* if the workspace already carries it, otherwise a copy inside it.

        Only ``workspace`` travels, so a tree anywhere else on this host is a path the
        container does not have: :meth:`_place_exposed` copies *inside* the container, and a
        source that never arrived fails there with a host path in the message and nothing to
        say why. Copying it in is what makes the exposure a copy of something that exists
        on both sides.

        The copy is named after the mount rather than after the source, so the same exposure
        repeated overwrites its own tree instead of accumulating one per call, and two
        exposures cannot collide.
        """
        workspace = os.path.abspath(self.workspace)
        absolute = os.path.abspath(source)
        if absolute == workspace or absolute.startswith(workspace + os.sep):
            return absolute
        holder = os.path.join(workspace, "exposed",
                             re.sub(r"[^A-Za-z0-9]+", "-", container_path).strip("-") or "aux")
        if os.path.isdir(absolute):
            shutil.rmtree(holder, ignore_errors=True)
            shutil.copytree(absolute, holder)
            return holder
        os.makedirs(holder, exist_ok=True)
        staged = os.path.join(holder, os.path.basename(absolute))
        shutil.copy2(absolute, staged)
        return staged

    def _client(self):
        if self._core_v1 is None:
            from .kube_client import core_v1_client
            self._core_v1 = core_v1_client(self._kube_context)
        return self._core_v1

    # -- exec plumbing ------------------------------------------------------

    def _exec(self, command, stdin_data=None, progress_update_callback=None,
              container: str = ""):
        """Exec *command* in the aux container (or *container*); return collected stdout.

        Raises ``subprocess.CalledProcessError`` on a non-zero exit, so callers
        (and plugins) see the same failure type as the local ``docker run`` path. A
        command that runs past :data:`AUX_EXEC_LIMIT_S` is one of those failures: an
        unbounded loop here lets a helper that hangs take the campaign's worker thread
        with it, with nothing in the log to say what it was waiting for.

        A stopped span raises ``CampaignStopped`` out of the exec instead, which is not a
        failure and must not be dressed as one.
        """
        from .kube_client import exec_stream

        stderr_sink = progress_update_callback or (
            lambda line: logger.debug("aux stderr: %s", line))
        code, out, err, timed_out = exec_stream(
            self._pod, self._namespace, container or self._container, command,
            limit_s=self._exec_limit_s, stdin_data=stdin_data,
            on_stdout_line=progress_update_callback, on_stderr_line=stderr_sink,
            should_stop=self._should_stop)
        if timed_out:
            raise subprocess.CalledProcessError(
                code, command,
                output=f"aux container command exceeded {self._exec_limit_s}s")
        if code != 0:
            # Both streams, and stderr last: a Python tool's traceback goes there, and it is
            # the whole reason a caller wants this attached rather than only logged.
            raise subprocess.CalledProcessError(
                code, command, output="\n".join(part for part in (out, err) if part))
        return out

    def _transfer(self, script: str) -> str:
        """Run *script* in the pod's transfer container: every file operation goes there.

        The aux image is a plugin author's and may lack a shell tool, a writable home or
        a user that can read what root wrote; the sidecar has what the transfer needs and
        runs as root, so what it fetches it can also make writable for whoever the aux
        container runs as.
        """
        return self._exec(["sh", "-c", script], container=TRANSFER_CONTAINER)

    # -- workspace transfer ---------------------------------------------------

    @property
    def _route(self) -> str:
        """The data-plane route of the pod's slot, after the data prefix."""
        return f"/staged/{aux_slot(self._pod)}"

    @property
    def _name(self) -> str:
        """This workspace's directory name inside the slot."""
        return os.path.basename(self.workspace)

    def _copy_in(self) -> None:
        """Fetch the workspace into the pod at the same path, through the data plane.

        The bytes do not travel through the exec channel. That channel is a text
        websocket the client cannot half-close, so a transfer over stdin has to frame
        its own end -- a receiver waiting for EOF there waits forever. A fetch needs no
        stdin at all, so that failure mode is gone by construction, and the size ceiling
        with it.

        **A workspace with nothing in it transfers nothing.** A generator whose inputs all
        live inside its image (a world installed from a wheel, say) stages nothing, and a
        round trip per ``run()`` for zero bytes is pure latency. The directory itself still
        has to exist in the pod, because the generator was handed that path. Emptiness is
        measured in entries: a tar carries an empty directory, so a workspace holding only
        the output directory a two-step generator stages travels whole, and the directory
        the command was told to write into is there when it runs.

        What the transfer container extracts it owns as root, so the tree is made
        writable for everyone afterwards: the aux container may run as any user the spec
        names, and the command is about to write into it.
        """
        workspace = shlex.quote(self.workspace)
        if not os.listdir(self.workspace):
            self._transfer(f"mkdir -p {workspace} && chmod 0777 {workspace}")
            self._staged = True
            return
        fetch = pod_access.fetch_command(self._route, self.workspace,
                                         f"path={quote(self._name, safe='')}")
        self._transfer(f"{fetch} && chmod -R a+rwX {workspace}")
        self._staged = True

    def _copy_out(self) -> None:
        """Deliver the pod's workspace back onto this one, through the data plane.

        Delivered under its own name (see :func:`pod_access.deliver_command`), so it lands
        in the slot exactly where it was fetched from -- this directory -- and beside any
        other runner's tree in the same pod. A tar extracts over what is there, so a
        regenerated file of the same size replaces the stale one.
        """
        self._transfer(pod_access.deliver_command(self.workspace, self._route,
                                                  keep_name=True))

    def _place_exposed(self) -> None:
        """Copy each exposed input from the fetched workspace into its declared mount.

        After ``_copy_in``, so the source is already in the pod, and in the transfer
        container, which shares every mount. Two shapes, told apart by the target rather
        than by looking at the filesystem (the source is only guaranteed to exist in the
        *pod*): a tree exposed AT a mountable path fills that mount -- the trailing ``/.``
        is what keeps it from nesting a directory inside it -- and anything exposed at a
        path INSIDE one is copied to that exact path, which is the only thing that works
        for a single staged file (``cp -R 'file/.'`` copies nothing).

        ``-R``, deliberately not ``-a``: only the content is wanted here; the tree is read
        by the aux container, not re-published, so its timestamps and ownership carry
        nothing -- and a mode copied from a private source would be one the aux user
        cannot read.
        """
        for container_path, staged in sorted(self._exposed.items()):
            if container_path in AUX_MOUNTABLE_PATHS:
                script = (f"mkdir -p '{container_path}' && "
                          f"cp -R '{staged}/.' '{container_path}/'")
            else:
                script = (f"mkdir -p '{os.path.dirname(container_path)}' && "
                          f"cp -R '{staged}' '{container_path}'")
            self._transfer(script)

    def run(self, command, progress_update_callback=None) -> None:
        """Run *command* in the aux container, making that container again if it is gone.

        A pod can end without the span that created it ending — an eviction, a drained
        node — and then the exec that notices is the one in the middle of a composition
        that may have been running for hours. Reporting it loses all of that over a
        container that costs seconds to replace, so the attempt is made a second time
        against a fresh one.

        **Repeating the whole attempt is what makes it correct**, not just the exec: a new
        container has an empty workspace and empty mounts, so the transfer has to happen
        again. And repeating the *command* is sound for this cause alone — the container it
        ran in no longer exists, so nothing it did survived to be done twice. A command
        that ran and failed is the caller's answer and is never retried.

        Once. A second vanishing is no longer something to sit out.
        """
        progress_update_callback = progress_update_callback or logger.debug
        full_cmd = list(self._spec.command_prefix) + list(command)
        try:
            self._attempt(full_cmd, progress_update_callback)
        except ExecTargetGone as gone:
            if self._reprovision is None:
                raise
            logger.warning("Aux container %s/%s is gone (%s); making it again and "
                           "repeating the command", self._pod, self._container, gone)
            replacement = self._reprovision(self._spec)
            # The workspace lives in the pod's slot, so a replacement has to answer to the
            # same name -- which both providers derive from the spec and the span, never
            # from the attempt. A different name would leave the tree behind in the old
            # slot, and the first fetch would report a slot that holds nothing.
            if replacement != self._pod:
                raise RuntimeError(
                    f"aux pod {self._pod} was replaced under the name {replacement}; a "
                    "runner's workspace is staged in its pod's slot, so a pod can only be "
                    "made again under its own name") from gone
            self._staged = False
            self._attempt(full_cmd, progress_update_callback)

    def _attempt(self, full_cmd, progress_update_callback) -> None:
        """One transfer-run-transfer against whichever container :attr:`_pod` names."""
        self._copy_in()
        self._place_exposed()
        try:
            self._exec(full_cmd, progress_update_callback=progress_update_callback)
        finally:
            # Bring back whatever the command produced, even on failure — partial
            # output is often what makes the failure diagnosable.
            try:
                self._copy_out()
            except Exception as e:  # pylint: disable=broad-except
                logger.warning("Could not copy aux workspace back: %s", e)

    def close(self):
        """Drop this runner's copy in the pod and its workspace on the service.

        The aux pod itself is torn down with the campaign by ``AuxPodSession``. Both of
        these are per-*variation*, though — a search campaign builds a runner per
        configuration it composes — so leaving them would accumulate for the campaign's
        whole life.

        The pod's copy is the one that accumulates where nothing is watching. Each
        runner's workspace has a path of its own, so the next one does not overwrite it;
        that path is on an emptyDir, which is ephemeral storage the Pod reserves none of.
        A long composition therefore fills the node it landed on and the kubelet evicts
        the aux Pod out from under the very campaign that is composing against it.

        Best-effort, and for a reason beyond the usual one: the pod may be gone by now
        (that is one of the ways a composition ends), and a teardown that raised over a
        container it cannot reach would replace the real failure with its own.
        """
        if self._staged:
            try:
                self._transfer(f"rm -rf {shlex.quote(self.workspace)}")
            except Exception as e:  # noqa: BLE001 - cleanup never fails a variation
                logger.warning("Could not drop the aux pod's copy of %s: %s",
                               self.workspace, e)
        shutil.rmtree(self.workspace, ignore_errors=True)
