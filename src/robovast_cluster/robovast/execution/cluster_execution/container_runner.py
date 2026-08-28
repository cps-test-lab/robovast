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
a *campaign-specific* sidecar of it is impossible. Instead each campaign gets its
own aux Pod, and this module **emulates
the shared workspace** by mirroring it into the pod before every ``run()`` and
copying the results back afterwards, at the *same absolute path*.

**How the workspace is mirrored.** Through the **object store**, the same transport a
campaign Job, an image-build context and the container-exec lane use: the service
uploads the workspace to a prefix, the container runs ``mc mirror`` to pull it down, and
the reverse afterwards. ``mc`` is injected into the pod from the sidecar image at
creation, into an ``emptyDir`` at ``/tools``, because the aux image belongs to a plugin
author and is not ours to add tools to — the trick the rosbag postprocess Job already
uses to run ``mc`` inside the system-under-test's own image.

This replaced piping a base64 tarball through the ``pods/exec`` channel. That worked,
but the channel is a text websocket the client **cannot half-close**, so a receiver
waiting for EOF waited forever — observed against a live pod for 2m47s, on a workspace
with nothing in it. It was fixed by framing the read with ``head -c <n>``; going through
the store removes the need for stdin at all, so the failure mode is gone by construction,
along with the ~1.33x base64 inflation and buffering whole tarballs in the service.

Consequences to know:

* The plugin contract is preserved for the *stage inputs → run → read outputs*
  pattern (what ``FloorplanVariation`` does). It is **narrowed** in one respect:
  the two sides do not share a live filesystem, so a command that expects the
  caller to observe its writes *while it is still running* (or vice versa) will
  not see them — only the state at copy-in/copy-out boundaries.
* An **empty workspace transfers nothing**: a generator whose inputs all live in its own
  image stages no files, and a round trip per ``run()`` for zero bytes is pure latency.
* Composition now needs the object store to be reachable. That is not a new dependency in
  practice — the campaign being composed cannot run without it either — but it is a new
  dependency *at composition time*, and it fails loudly rather than falling back.
* Aux compute is scheduled by Kubernetes as its own pod, so it never competes
  with the service (the control plane) for resources.

Lifecycle: the pod is labeled with its campaign, owned by the service pod (so
Kubernetes garbage-collects it if the service is replaced), carries an
``activeDeadlineSeconds`` backstop, and is deleted when the campaign ends.
"""

import contextlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time

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

#: Label selector identifying every aux pod (one per campaign that needs one).
AUX_LABEL = "app=robovast-aux"

#: Key prefix every mirrored aux workspace lives under, inside the deployment's bucket.
AUX_WORKSPACE_PREFIX = "aux-workspaces"

#: The ``mc`` alias the aux containers address the store by. Same name the campaign job's
#: init and the build context's fetch use, so all three read alike.
_MC_ALIAS = "mystore"
#: Where the sidecar's ``mc`` — and a config dir it can actually write — are injected.
_TOOLS_MOUNT = "/tools"
_MC = f"{_TOOLS_MOUNT}/mc"
_MC_CONFIG = f"{_TOOLS_MOUNT}/mc-config"

#: Absolute DIRECTORIES an aux container can be asked to expose a staged input at, via
#: :meth:`ClusterContainerRunner.expose`. A fixed list rather than anything a caller picks,
#: because the mount has to be declared when the *pod* is built, long before a runner knows
#: what it will stage -- and a path nobody mounted is not writable in an arbitrary image.
#: Each one becomes an emptyDir, mounted on every aux container and chmod'ed by the init
#: container, so a new entry here is all a new fixed mount needs.
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


def _mount_volume_name(path: str) -> str:
    """A DNS-label volume name for a mountable absolute path (``/config`` -> ``aux-config``)."""
    return "aux-" + re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-")


def aux_workspace_prefix(owner_id: str, workspace_name: str) -> str:
    """Where one runner's workspace is mirrored.

    Keyed on the runner's own temp-directory name rather than on its container, because
    a runner is built per *variation*: two variations sharing one aux container would
    otherwise share a prefix, and whichever finished first would delete the other's
    files in :meth:`ClusterContainerRunner.close`.
    """
    return f"{aux_owner_prefix(owner_id)}/{workspace_name}"


def aux_owner_prefix(owner_id: str) -> str:
    """Everything mirrored on behalf of one campaign (or scene build), for the sweep."""
    from .cluster_execution import _label_safe_campaign
    return f"{AUX_WORKSPACE_PREFIX}/{_label_safe_campaign(owner_id)}"


def mc_host_env(endpoint: str, access_key: str, secret_key: str) -> dict:
    """``MC_HOST_<alias>``, so no ``mc alias set`` has to run in the aux container.

    An alias command would write to ``$HOME/.mc``, and the aux image is not ours: its
    ``HOME`` may not exist, may not be writable, and ``run_as_user`` can change who is
    asking. Credentials in the environment match what every campaign pod already carries.
    """
    from urllib.parse import quote, urlsplit
    parts = urlsplit(endpoint)
    creds = f"{quote(access_key, safe='')}:{quote(secret_key, safe='')}"
    return {f"MC_HOST_{_MC_ALIAS}":
            f"{parts.scheme}://{creds}@{parts.netloc}{parts.path}".rstrip("/")}


def aux_pod_name(campaign_id: str) -> str:
    """Deterministic aux-pod name for *campaign_id*."""
    from .cluster_execution import _label_safe_campaign
    return f"robovast-aux-{_label_safe_campaign(campaign_id)}"


class AuxDiscoveryError(RuntimeError):
    """Aux-container discovery could not determine what a campaign needs.

    Raised instead of silently returning an empty spec list, so a campaign that
    requires a helper image aborts before dispatch rather than launching with no
    aux pod. An *empty* result is only ever "this campaign declares no aux
    container", never "we could not tell".
    """


def required_container_specs(config_path):
    """Collect the distinct auxiliary ContainerSpecs a campaign's variations need.

    Asks each declared variation (via ``get_required_container``) whether it needs a
    helper image while it runs, and returns a list of ``ContainerSpec`` deduplicated
    by container name. An empty list means the campaign genuinely declares no aux
    container. Discovery never *silently* yields an empty list: any failure to load
    the ``.vast``, resolve a variation, or run the discovery subprocess propagates,
    so a launch that needs a helper image fails loudly instead of running without it.

    When the ``.vast`` declares ``plugins:`` (or a staged ``.robovast_plugins/`` is
    present), the variation names resolve only through the plugin's entry points, and
    those must be imported to call ``get_required_container``. Importing plugin code
    in this long-lived service is forbidden (its pinned deps — e.g. a forked
    ``rdflib`` — would win over the service's), so discovery for that case runs in a
    fresh subprocess (parity with ``config_generation._compose_isolated``). A pure
    built-in ``.vast`` needs no plugin import and is resolved in-process.
    """
    from robovast.common.common import load_config
    from robovast.common.config_plugins import PLUGIN_DIRNAME

    vast_dir = os.path.dirname(os.path.abspath(config_path))
    parameters = load_config(config_path)

    needs_plugins = bool(parameters.get("plugins")) or \
        os.path.isdir(os.path.join(vast_dir, PLUGIN_DIRNAME))
    if needs_plugins:
        return _discover_specs_subprocess(config_path)
    return _discover_specs(config_path)


def _discover_specs(config_path):
    """Resolve the campaign's aux ContainerSpecs in the current process.

    Prepends any declared/staged variation plugins to ``sys.path`` first, so plugin
    variation names resolve via their entry points. Only safe to call in-process for
    a built-in-only ``.vast`` (see :func:`required_container_specs`); otherwise it is
    the body run inside the discovery subprocess (``aux_discovery_worker``).
    """
    from robovast.common.common import load_config
    from robovast.common.config_generation import _get_variation_classes
    from robovast.common.config_plugins import ensure_workspace_plugins

    vast_dir = os.path.dirname(os.path.abspath(config_path))
    parameters = load_config(config_path)

    # Put the .vast's variation plugins on sys.path (no-op for a built-in-only .vast:
    # no ``plugins:`` and no staged dir). A matching ``.installed`` marker makes this
    # sys.path-only — no pip, no network.
    ensure_workspace_plugins(vast_dir, parameters.get("plugins"))

    # Batch campaigns declare variations under top-level ``configuration`` blocks;
    # search campaigns declare them once as ``search.variations`` (compose expands
    # that template into configuration blocks per generation). Inspect both so the
    # aux pod is created regardless of campaign type. Unsubstituted ``$name`` search
    # markers in the template are harmless: get_required_container ignores values.
    blocks = list(parameters.get("configuration", []) or [])
    search_variations = (parameters.get("search", {}) or {}).get("variations")
    if search_variations:
        blocks.append({"variations": search_variations})

    # A failure to resolve a variation or compute its container requirement aborts
    # discovery (propagates): we cannot know whether the campaign needs a helper
    # image, so we must not proceed as if it needs none.
    specs = {}
    for config_block in blocks:
        classes = _get_variation_classes(config_block, vast_dir)
        for variation_class, variation_parameters in classes:
            spec = variation_class.get_required_container(variation_parameters)
            if spec is not None:
                specs.setdefault(spec.container_name(), spec)
    return list(specs.values())


def _discover_specs_subprocess(config_path):
    """Run :func:`_discover_specs` in a fresh subprocess and rebuild the specs.

    Isolates plugin imports from the long-lived service. If the worker cannot start,
    exits non-zero, or yields no readable result, that is raised as
    :class:`AuxDiscoveryError` — the campaign then fails loudly instead of launching
    with no aux pod. The worker's captured traceback is included so the real plugin
    error is visible at the failure site, not deferred to compose time.
    """
    import json
    import sys

    with tempfile.TemporaryDirectory(prefix="robovast_aux_discovery_") as jobdir:
        result_path = os.path.join(jobdir, "result.json")
        job_path = os.path.join(jobdir, "job.json")
        with open(job_path, "w", encoding="utf-8") as f:
            json.dump({"config_path": os.path.abspath(config_path),
                       "result_path": result_path}, f)

        cmd = [sys.executable, "-m",
               "robovast.execution.cluster_execution.aux_discovery_worker", job_path]
        try:
            # nosec B603 - fixed module, config-derived job file
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        except Exception as exc:
            raise AuxDiscoveryError(
                f"aux-container discovery subprocess could not start: {exc}") from exc
        if proc.returncode != 0:
            tail = (proc.stdout + proc.stderr)[-2000:]
            raise AuxDiscoveryError(
                f"aux-container discovery failed (exit {proc.returncode}):\n{tail}")
        try:
            with open(result_path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            raise AuxDiscoveryError(
                f"aux-container discovery produced no readable result: {exc}") from exc

    from robovast.common.variation.container_runner import ContainerSpec
    return [ContainerSpec(**spec) for spec in raw]


def cleanup_aux_pods(namespace="default", kube_context=None, campaign=None):
    """Delete aux pods (label ``app=robovast-aux``). Best-effort.

    With *campaign* given, deletes only that campaign's aux pod so concurrent
    campaigns are left untouched; otherwise deletes every aux pod. Backs
    ``vast cluster jobs-cleanup`` (the successor to the controller-pod reap).
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


def build_aux_pod_manifest(campaign_id, specs, namespace, owner_ref=None,
                           deadline_seconds: int = DEFAULT_AUX_DEADLINE_SECONDS,
                           pull_secret: str = "", s3: tuple | None = None,
                           pod_name: str = "", container_names=None,
                           extra_labels: dict | None = None) -> dict:
    """Manifest for an aux Pod: one kept-alive container per spec.

    Each container runs the aux image with its one-shot entrypoint overridden by
    the spec's ``keep_alive_command``, so it stays up for the whole span and
    every ``run()`` pays only the exec setup (no per-call create or image re-pull).

    Two spans build one manifest here, which is the point: a *campaign*'s pod (named after
    the campaign, deleted when it ends) and a *held* one owned by the service's exec
    manager (named after its slot, reaped on idleness). *pod_name*, *container_names* and
    *extra_labels* are what the second needs — its name and container have to be the ones
    the exec lane already addresses and sweeps by, and everything else about an aux pod is
    identical. Forking a second builder for that would have put the ``mc`` init container,
    the mountable emptyDirs and the pull secret in two places.

    *container_names* maps a spec's ``container_name()`` to the name to use instead;
    unnamed specs keep their own.

    *owner_ref* should be the **service pod** so Kubernetes garbage-collects this
    pod when the service is replaced — the same "dies with its parent" guarantee
    the old controller-pod sidecar had.

    *pull_secret* names an image-pull secret. Aux images were originally public
    (``ghcr.io/secorolab/scenery_builder``), so none was needed; a spec naming the
    *campaign's own* image points at a private registry, and ``imagePullPolicy:
    IfNotPresent`` hides that until the first node that has not cached it.

    *s3* is ``(endpoint, access_key, secret_key)``. Given, the pod gains an init
    container that injects ``mc`` into an ``emptyDir`` and every aux container mounts it,
    so the workspace can be mirrored through the object store rather than piped through
    the exec channel. The binary comes from the sidecar image because the aux image is
    not ours to add tools to — the same trick the rosbag postprocess Job uses to run
    ``mc`` inside the system-under-test's own image.
    """
    from robovast.common.execution import resolve_sidecar_image

    from .cluster_execution import _label_safe_campaign

    tools_mount = {"name": "aux-tools", "mountPath": _TOOLS_MOUNT}
    # One emptyDir per mountable path, on every aux container. Empty unless a runner stages
    # into it, so a generator that never asks pays a volume and nothing else; and it has to
    # be here rather than at ``expose`` time because a Pod's mounts are fixed when it is
    # created. Tied to *s3* like the rest of the mirroring: without a store nothing can be
    # staged into one, so an unusable mount would only be noise in the manifest.
    config_mounts = [{"name": _mount_volume_name(path), "mountPath": path}
                     for path in AUX_MOUNTABLE_PATHS] if s3 else []
    host_env = mc_host_env(*s3) if s3 else {}

    containers = []
    for spec in specs:
        container = {
            "name": (container_names or {}).get(spec.container_name(),
                                                spec.container_name()),
            "image": spec.image,
            "imagePullPolicy": "IfNotPresent",
            "command": list(spec.keep_alive_command),
        }
        env = dict(spec.env or {})
        env.update(host_env)
        if env:
            container["env"] = [{"name": k, "value": str(v)} for k, v in env.items()]
        if s3:
            container["volumeMounts"] = [tools_mount] + list(config_mounts)
        if spec.run_as_user:
            uid = spec.run_as_user.split(":", 1)[0]
            try:
                container["securityContext"] = {"runAsUser": int(uid)}
            except ValueError:
                pass
        containers.append(container)

    # *extra_labels* last, and it may legitimately replace ``app``: a held pod is the exec
    # manager's, so the exec lane's stray sweep must be the one that finds it. Exactly one
    # sweep should own a pod — a held one answering to `cleanup_aux_pods` as well would let
    # a campaign's cleanup delete a container somebody's preview is composing against.
    metadata = {
        "name": pod_name or aux_pod_name(campaign_id),
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
    }
    if s3:
        spec["volumes"] = [{"name": "aux-tools", "emptyDir": {}}] + [
            {"name": mount["name"], "emptyDir": {}} for mount in config_mounts]
        # Every emptyDir gets the same treatment and for the same reason: it belongs to
        # root, and a spec's ``run_as_user`` means the container that has to write into it
        # may be nobody in particular.
        chmods = " && ".join(f'chmod 0777 {mount["mountPath"]}' for mount in config_mounts)
        spec["initContainers"] = [{
            "name": "mc-tools", "image": resolve_sidecar_image(),
            "imagePullPolicy": "IfNotPresent",
            "command": ["sh", "-c",
                        f'cp "$(command -v mc)" {_MC} && chmod 0755 {_MC} && '
                        f'mkdir -p {_MC_CONFIG} && chmod 0777 {_MC_CONFIG}'
                        + (f' && {chmods}' if chmods else '')],
            "volumeMounts": [tools_mount] + list(config_mounts),
        }]
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


class AuxPodSession:
    """Creates a campaign's aux Pod, yields a runner factory, deletes it after.

    Used as a context manager by the service's per-campaign worker thread, so the
    pod's lifetime is exactly the campaign's and the runner factory it installs is
    scoped to that worker (see ``config_generation.set_container_runner_factory``).
    A campaign with no aux specs creates nothing.
    """

    def __init__(self, campaign_id, specs, namespace, core_v1=None,
                 ready_timeout: float = 300.0, pull_secret: str = "",
                 storage=None, bucket: str = "", s3: tuple | None = None,
                 kube_context: str | None = None):
        self.campaign_id = campaign_id
        self.pull_secret = pull_secret
        self.specs = list(specs or [])
        self.namespace = namespace
        self.pod_name = aux_pod_name(campaign_id)
        self._core_v1 = core_v1
        # Only consulted when no client was handed in. It must still be the *service's*
        # context: falling back to the kubeconfig's current one puts this campaign's aux
        # pod in whichever cluster the host happens to point at, while looking perfectly
        # valid — the same failure the container-exec lane has a regression test for.
        self._kube_context = kube_context
        self._ready_timeout = ready_timeout
        self._created = False
        # All three or none: a pod built with ``mc`` but no client to stage through (or
        # the reverse) fails at the first ``run()``, deep inside a plugin, instead of
        # here where the cause is legible.
        if bool(storage) != bool(bucket) or bool(storage) != bool(s3):
            raise ValueError(
                "aux workspace mirroring needs storage, bucket and s3 together; "
                f"got storage={bool(storage)} bucket={bool(bucket)} s3={bool(s3)}")
        self._storage = storage
        self._bucket = bucket
        self._s3 = s3

    def _client(self):
        if self._core_v1 is None:
            from kubernetes import client

            from .kube_client import load_kube_config
            load_kube_config(context=self._kube_context)
            self._core_v1 = client.CoreV1Api()
        return self._core_v1

    def __enter__(self):
        if not self.specs:
            return self
        from kubernetes.client.rest import ApiException

        from .kube_client import wait_pod_gone, wait_pod_ready
        core = self._client()
        manifest = build_aux_pod_manifest(
            self.campaign_id, self.specs, self.namespace,
            owner_ref=service_pod_owner_reference(core, self.namespace),
            pull_secret=self.pull_secret, s3=self._s3)
        try:
            core.create_namespaced_pod(self.namespace, manifest)
        except ApiException as e:
            if e.status != 409:
                raise RuntimeError(
                    f"could not create aux pod {self.pod_name}: {e.reason}") from e
            # A 409 is not "already exists → reuse it". The name is derived from the
            # campaign id, so the pod it collides with is this campaign's previous one —
            # usually still Terminating, and a Terminating pod never becomes Running
            # again. Adopting it means waiting out the full ready timeout for a corpse.
            # Wait for the delete to land, then create ours.
            logger.info("Aux pod %s still exists; waiting for it to go before recreating",
                        self.pod_name)
            with contextlib.suppress(ApiException):
                core.delete_namespaced_pod(self.pod_name, self.namespace,
                                           grace_period_seconds=0)
            wait_pod_gone(core, self.namespace, self.pod_name,
                          timeout_s=self._ready_timeout)
            core.create_namespaced_pod(self.namespace, manifest)
        self._created = True
        logger.info("Aux pod %s created (%d container(s)) for campaign %s",
                    self.pod_name, len(self.specs), self.campaign_id)
        # Shared with the container-exec lane so a stuck pod names its reason
        # (ImagePullBackOff, say) instead of timing out with only an elapsed time —
        # which matters now that a spec may name the campaign's own private image.
        wait_pod_ready(core, self.namespace, self.pod_name,
                       timeout_s=self._ready_timeout)
        return self

    def runner_factory(self):
        """A ``factory(spec) -> ClusterContainerRunner`` bound to this campaign's pod."""
        def factory(spec):
            return ClusterContainerRunner(
                spec, self.pod_name, self.namespace, self._client(),
                storage=self._storage, bucket=self._bucket,
                owner_id=self.campaign_id, kube_context=self._kube_context)
        return factory

    def _sweep_workspaces(self) -> None:
        """Drop anything this campaign's runners mirrored.

        Each runner deletes its own prefix in ``close()``; this catches the ones whose
        close never ran — a composition that raised, or a service that died mid-campaign.
        Best-effort, because a leftover copy of a workspace must not fail a campaign that
        otherwise finished.
        """
        if not self._storage:
            return
        try:
            removed = self._storage.delete_prefix(self._bucket,
                                                  aux_owner_prefix(self.campaign_id))
        except Exception as e:  # noqa: BLE001 - cleanup never fails the campaign
            logger.warning("Could not sweep aux workspaces for %s: %s",
                           self.campaign_id, e)
            return
        if removed:
            logger.info("Swept %d leftover aux workspace object(s) for %s",
                        removed, self.campaign_id)

    def __exit__(self, exc_type, exc, tb):
        if not self._created:
            return False
        try:
            self._client().delete_namespaced_pod(self.pod_name, self.namespace)
            logger.info("Aux pod %s deleted", self.pod_name)
        except Exception as e:  # pylint: disable=broad-except - GC/reaper is the backstop
            logger.warning("Could not delete aux pod %s: %s", self.pod_name, e)
        self._sweep_workspaces()
        return False


class ClusterContainerRunner:
    """Runs a plugin's commands in a campaign's aux Pod via ``pods/exec``.

    ``workspace`` is a **local** directory in the service; it is mirrored into the
    aux container at the identical absolute path around each :meth:`run`, so the
    plugin's absolute paths stay valid on both sides (see the module docstring for
    the one way this differs from the old shared-volume behaviour).
    """

    def __init__(self, spec, pod_name, namespace, core_v1=None,
                 exec_limit_s: float = AUX_EXEC_LIMIT_S, storage=None,
                 bucket: str = "", owner_id: str = "",
                 kube_context: str | None = None, container: str = ""):
        self._spec = spec
        self._pod = pod_name
        self._namespace = namespace
        self._core_v1 = core_v1
        # See AuxPodSession: only used when no client was handed in, and it must be the
        # service's context rather than whatever the host kubeconfig points at.
        self._kube_context = kube_context
        # A campaign's aux pod names each container after its spec, so the spec is the
        # default. A pod held by the exec manager names its single container the way that
        # lane names every held container, and passes it — the pod name and the container
        # name have to come from the same place or one of them addresses nothing.
        self._container = container or spec.container_name()
        self._exec_limit_s = exec_limit_s
        self._storage = storage
        self._bucket = bucket
        self.workspace = tempfile.mkdtemp(prefix="robovast_aux_")
        self._prefix = aux_workspace_prefix(owner_id or namespace,
                                            os.path.basename(self.workspace))
        self._exposed: dict = {}

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
        geometry -- while working on the local lane, where a bind mount does not care.
        """
        container_path = str(container_path)
        if container_path not in AUX_MOUNTABLE_PATHS \
                and os.path.dirname(container_path) not in AUX_MOUNTABLE_PATHS:
            raise ValueError(
                f"an aux container can only expose a staged input at one of "
                f"{list(AUX_MOUNTABLE_PATHS)}, or at a file directly inside one of them, "
                f"not {container_path!r}; a new path has to be added to "
                f"AUX_MOUNTABLE_PATHS so the Pod declares a volume for it.")
        self._exposed[container_path] = str(host_path)

    def _client(self):
        if self._core_v1 is None:
            from kubernetes import client

            from .kube_client import load_kube_config
            load_kube_config(context=self._kube_context)
            self._core_v1 = client.CoreV1Api()
        return self._core_v1

    # -- exec plumbing ------------------------------------------------------

    def _exec(self, command, stdin_data=None, progress_update_callback=None):
        """Exec *command* in the aux container; return collected stdout.

        Raises ``subprocess.CalledProcessError`` on a non-zero exit, so callers
        (and plugins) see the same failure type as the local ``docker run`` path. A
        command that runs past :data:`AUX_EXEC_LIMIT_S` is one of those failures: an
        unbounded loop here lets a helper that hangs take the campaign's worker thread
        with it, with nothing in the log to say what it was waiting for.
        """
        from .kube_client import exec_stream

        stderr_sink = progress_update_callback or (
            lambda line: logger.debug("aux stderr: %s", line))
        code, out, err, timed_out = exec_stream(
            self._client(), self._pod, self._namespace, self._container, command,
            limit_s=self._exec_limit_s, stdin_data=stdin_data,
            on_stdout_line=progress_update_callback, on_stderr_line=stderr_sink)
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

    def _retrying_exec(self, command, **kwargs):
        """Exec, retrying transient 'container not ready yet' failures."""
        last_exc = None
        for attempt in range(10):
            try:
                return self._exec(command, **kwargs)
            except subprocess.CalledProcessError:
                raise  # a real non-zero exit — don't retry
            except Exception as exc:  # pylint: disable=broad-except
                last_exc = exc
                logger.debug("exec into %s not ready yet (attempt %d): %s",
                             self._container, attempt + 1, exc)
                time.sleep(1)
        raise RuntimeError(
            f"Could not exec into aux container '{self._container}': {last_exc}")

    # -- workspace mirroring ------------------------------------------------

    def _require_store(self):
        if self._storage is None or not self._bucket:
            raise RuntimeError(
                "the aux container's workspace is mirrored through the object store, "
                "and this runner was built without one. That is a service configuration "
                "problem, not something a plugin can work around.")
        return self._storage

    def _mirror(self, *, down: bool) -> str:
        """The ``mc mirror`` argv running inside the aux container.

        ``--overwrite`` because the transport this replaced extracted a tar over the
        destination: without it ``mc`` skips a file whose size and time already match,
        which would silently keep a stale copy on a regenerated artifact.
        """
        remote = f"{_MC_ALIAS}/{self._bucket}/{self._prefix}/"
        local = f"{self.workspace}/"
        src, dst = (remote, local) if down else (local, remote)
        return (f"mkdir -p '{self.workspace}' && "
                f"{_MC} --config-dir {_MC_CONFIG} mirror --overwrite --quiet "
                f"'{src}' '{dst}'")

    def _copy_in(self) -> None:
        """Mirror the local workspace into the container at the same path, via the store.

        The bytes do not travel through the exec channel. That channel is a text
        websocket the client cannot half-close, so the tar-over-stdin version this
        replaced had to base64-encode its payload (~1.33x) and frame the read by length
        (``head -c <n>``) — because a receiver waiting for EOF waited forever, which is
        exactly what it did, for 2m47s against a live pod, on an *empty* workspace.
        Mirroring needs no stdin at all, so that whole failure mode is gone by
        construction, and the size ceiling with it.

        **A workspace with no files copies nothing.** A generator whose inputs all live
        inside its image (a world installed from a wheel, say) stages no files, and a
        round trip per ``run()`` for zero bytes is pure latency.

        Emptiness is measured in *files*, matching what ``upload_dir`` actually ships, not
        in directory entries: ``stage_for_container`` always creates an output directory, so
        a no-input generator's workspace holds one empty dir and no files. Counting entries
        would call that non-empty, upload nothing, and then mirror *from a prefix that does
        not exist* — ``mc`` exits 1, and building a scene descriptor fails with an object
        storage error.

        **The staged directory skeleton is created in the container either way**, because an
        object store has no empty directories: ``mc mirror`` recreates only the ones that
        hold files. Doing it only on the no-files path — on the reasoning that with files
        present ``mc`` makes the directories itself, true only of the directories it has
        something to put in — breaks every two-step generator, which stages inputs AND an
        empty output directory: the workspace arrives missing exactly the directory the
        command was told to write into, so ``floorplan generate`` validates its ``-o`` path
        and exits 2 with "Path ... does not exist" while step 1 passed, ``transform``
        creating its own output directory. The ``mkdir`` rides along with the mirror's own
        exec, so this costs no extra round trip.
        """
        staged_dirs = [self.workspace]
        staged_files = 0
        for root, dirs, names in os.walk(self.workspace):
            staged_dirs.extend(os.path.join(root, name) for name in dirs)
            staged_files += len(names)
        quoted = " ".join(f"'{path}'" for path in staged_dirs)
        if not staged_files:
            self._retrying_exec(["sh", "-c", f"mkdir -p {quoted}"])
            return
        self._require_store().upload_dir(self.workspace, self._bucket, self._prefix)
        self._retrying_exec(["sh", "-c", f"mkdir -p {quoted} && " + self._mirror(down=True)])

    def _copy_out(self) -> None:
        """Mirror the container's workspace back over the local one, via the store.

        ``force=True`` on the download for the same reason ``--overwrite`` is set going
        up: a same-size regenerated file is a real case, and the default size check would
        keep the stale one.
        """
        self._retrying_exec(["sh", "-c", self._mirror(down=False)])
        self._require_store().download_prefix(self._bucket, self._prefix,
                                              self.workspace, force=True)

    def _place_exposed(self) -> None:
        """Copy each exposed input from the mirrored workspace into its declared mount.

        After ``_copy_in``, so the source is already in the container. Two shapes, told apart
        by the target rather than by looking at the filesystem (the source is only guaranteed
        to exist in the *container*): a tree exposed AT a mountable path fills that mount --
        the trailing ``/.`` is what keeps it from nesting a directory inside it -- and
        anything exposed at a path INSIDE one is copied to that exact path, which is the
        only thing that works for a single staged file (``cp -R 'file/.'`` copies nothing).

        The mount is an emptyDir the init container made world-writable.

        ``-R``, deliberately not ``-a``: preserving attributes means setting them on the
        destination *mount point* too, and that inode belongs to root while the aux container
        may be anyone -- ``cp: preserving times for '/config/.': Operation not permitted``,
        which fails the whole copy. Only the content is wanted here; the tree is read, not
        re-published, so its timestamps and ownership carry nothing.
        """
        for container_path, staged in sorted(self._exposed.items()):
            if container_path in AUX_MOUNTABLE_PATHS:
                script = (f"mkdir -p '{container_path}' && "
                          f"cp -R '{staged}/.' '{container_path}/'")
            else:
                script = (f"mkdir -p '{os.path.dirname(container_path)}' && "
                          f"cp -R '{staged}' '{container_path}'")
            self._retrying_exec(["sh", "-c", script])

    def run(self, command, progress_update_callback=None) -> None:
        progress_update_callback = progress_update_callback or logger.debug
        full_cmd = list(self._spec.command_prefix) + list(command)
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
        """Drop this runner's mirrored prefix and its local scratch.

        The aux pod itself is torn down with the campaign by ``AuxPodSession``. Both of
        these are per-*variation*, though — a search campaign builds a runner per
        generation — so leaving them would accumulate for the campaign's whole life.
        """
        if self._storage is not None and self._bucket:
            try:
                self._storage.delete_prefix(self._bucket, self._prefix)
            except Exception as e:  # noqa: BLE001 - cleanup never fails a variation
                logger.warning("Could not drop the aux workspace mirror %s: %s",
                               self._prefix, e)
        shutil.rmtree(self.workspace, ignore_errors=True)
