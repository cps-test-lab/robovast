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

**Why a separate pod (and what that costs).** The aux container used to be a
*sidecar of the per-campaign controller pod*, which shared an ``emptyDir`` with
the controller — that is how the plugin contract's "workspace visible at the same
absolute path on both sides" was satisfied for free. The controller pod is gone:
the driver now runs inside the long-lived ``robovast-service`` pod, and a pod's
container set is immutable, so a *campaign-specific* sidecar of the service pod is
impossible. Instead each campaign gets its own aux Pod, and this module **emulates
the shared workspace** by tar-copying it into the pod before every ``run()`` and
copying the results back afterwards, at the *same absolute path*.

Consequences to know:

* The plugin contract is preserved for the *stage inputs → run → read outputs*
  pattern (what ``FloorplanVariation`` does). It is **narrowed** in one respect:
  the two sides no longer share a live filesystem, so a command that expects the
  caller to observe its writes *while it is still running* (or vice versa) will
  not see them — only the state at copy-in/copy-out boundaries.
* The aux image must provide ``tar`` and ``base64`` (both are in any normal
  userland image). Transfers are base64-framed because the exec channel is
  text-oriented and would corrupt raw binary.
* Aux compute is scheduled by Kubernetes as its own pod, so it never competes
  with the service (the control plane) for resources.

Lifecycle: the pod is labelled with its campaign, owned by the service pod (so
Kubernetes garbage-collects it if the service is replaced), carries an
``activeDeadlineSeconds`` backstop, and is deleted when the campaign ends.
"""

import base64
import io
import logging
import os
import subprocess
import tarfile
import tempfile
import time

logger = logging.getLogger(__name__)

#: Default wall-clock cap for an aux pod, so a leaked one always dies by itself.
DEFAULT_AUX_DEADLINE_SECONDS = 12 * 60 * 60

#: Label selector identifying every aux pod (one per campaign that needs one).
AUX_LABEL = "app=robovast-aux"


def aux_pod_name(campaign_id: str) -> str:
    """Deterministic aux-pod name for *campaign_id*."""
    from robovast.execution.cluster_execution.cluster_execution import \
        _label_safe_campaign
    return f"robovast-aux-{_label_safe_campaign(campaign_id)}"


def required_container_specs(config_path):
    """Collect the distinct auxiliary ContainerSpecs a campaign's variations need.

    Asks each declared variation (via ``get_required_container``) whether it needs a
    helper image while it runs, and returns a list of ``ContainerSpec`` deduplicated
    by container name. Best-effort: a plugin that fails to load is skipped (the run
    surfaces the real error later), so a launch is never blocked by discovery.

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
    try:
        parameters = load_config(config_path)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Could not inspect '%s' for aux containers: %s", config_path, exc)
        return []

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
    try:
        parameters = load_config(config_path)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Could not inspect '%s' for aux containers: %s", config_path, exc)
        return []

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


def _discover_specs_subprocess(config_path):
    """Run :func:`_discover_specs` in a fresh subprocess and rebuild the specs.

    Isolates plugin imports from the long-lived service. Best-effort: if the worker
    cannot start, fails, or yields no result, discovery is skipped (empty list) so a
    launch is never blocked — the run surfaces any real plugin error at compose time.
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
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Aux-container discovery subprocess could not start: %s", exc)
            return []
        if proc.returncode != 0:
            tail = (proc.stdout + proc.stderr)[-1000:]
            logger.warning("Aux-container discovery failed (exit %s); proceeding "
                           "without an aux pod:\n%s", proc.returncode, tail)
            return []
        try:
            with open(result_path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Aux-container discovery produced no result: %s", exc)
            return []

    from robovast.common.variation.container_runner import ContainerSpec
    return [ContainerSpec(**spec) for spec in raw]


def cleanup_aux_pods(namespace="default", kube_context=None, campaign=None):
    """Delete aux pods (label ``app=robovast-aux``). Best-effort.

    With *campaign* given, deletes only that campaign's aux pod so concurrent
    campaigns are left untouched; otherwise deletes every aux pod. Backs
    ``vast exec cluster run-cleanup`` (the successor to the controller-pod reap).
    """
    from kubernetes import client, config

    from robovast.execution.cluster_execution.cluster_execution import \
        _label_safe_campaign

    selector = AUX_LABEL
    if campaign is not None:
        selector += f",campaign-id={_label_safe_campaign(campaign)}"
    try:
        if kube_context:
            config.load_kube_config(context=kube_context)
        else:
            try:
                config.load_incluster_config()
            except Exception:  # noqa: BLE001 - host fallback
                config.load_kube_config()
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
                           deadline_seconds: int = DEFAULT_AUX_DEADLINE_SECONDS) -> dict:
    """Manifest for a campaign's aux Pod: one kept-alive container per spec.

    Each container runs the aux image with its one-shot entrypoint overridden by
    the spec's ``keep_alive_command``, so it stays up for the whole campaign and
    every ``run()`` pays only the exec setup (no per-call create or image re-pull).

    *owner_ref* should be the **service pod** so Kubernetes garbage-collects this
    pod when the service is replaced — the same "dies with its parent" guarantee
    the old controller-pod sidecar had.
    """
    from robovast.execution.cluster_execution.cluster_execution import \
        _label_safe_campaign

    containers = []
    for spec in specs:
        container = {
            "name": spec.container_name(),
            "image": spec.image,
            "imagePullPolicy": "IfNotPresent",
            "command": list(spec.keep_alive_command),
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

    metadata = {
        "name": aux_pod_name(campaign_id),
        "namespace": namespace,
        "labels": {"app": "robovast-aux",
                   "campaign-id": _label_safe_campaign(campaign_id)},
    }
    if owner_ref:
        metadata["ownerReferences"] = [owner_ref]
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": metadata,
        "spec": {
            "restartPolicy": "Never",
            # Backstop: even if teardown and the reaper both miss it, it dies.
            "activeDeadlineSeconds": int(deadline_seconds),
            "containers": containers,
        },
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
                 ready_timeout: float = 300.0):
        self.campaign_id = campaign_id
        self.specs = list(specs or [])
        self.namespace = namespace
        self.pod_name = aux_pod_name(campaign_id)
        self._core_v1 = core_v1
        self._ready_timeout = ready_timeout
        self._created = False

    def _client(self):
        if self._core_v1 is None:
            from kubernetes import client, config
            try:
                config.load_incluster_config()
            except Exception:  # noqa: BLE001 - dev/local fallback
                config.load_kube_config()
            self._core_v1 = client.CoreV1Api()
        return self._core_v1

    def __enter__(self):
        if not self.specs:
            return self
        from kubernetes.client.rest import ApiException
        core = self._client()
        manifest = build_aux_pod_manifest(
            self.campaign_id, self.specs, self.namespace,
            owner_ref=service_pod_owner_reference(core, self.namespace))
        try:
            core.create_namespaced_pod(self.namespace, manifest)
            self._created = True
        except ApiException as e:
            if e.status != 409:  # already exists → reuse it
                raise RuntimeError(
                    f"could not create aux pod {self.pod_name}: {e.reason}") from e
            self._created = True
        logger.info("Aux pod %s created (%d container(s)) for campaign %s",
                    self.pod_name, len(self.specs), self.campaign_id)
        self._wait_running(core)
        return self

    def _wait_running(self, core) -> None:
        deadline = time.time() + self._ready_timeout
        while time.time() < deadline:
            pod = core.read_namespaced_pod(self.pod_name, self.namespace)
            phase = pod.status.phase
            if phase == "Running":
                return
            if phase in ("Failed", "Succeeded"):
                raise RuntimeError(
                    f"aux pod {self.pod_name} reached {phase} before it could be used")
            time.sleep(2)
        raise RuntimeError(
            f"aux pod {self.pod_name} was not Running within {self._ready_timeout}s")

    def runner_factory(self):
        """A ``factory(spec) -> ClusterContainerRunner`` bound to this campaign's pod."""
        def factory(spec):
            return ClusterContainerRunner(
                spec, self.pod_name, self.namespace, self._client())
        return factory

    def __exit__(self, exc_type, exc, tb):
        if not self._created:
            return False
        try:
            self._client().delete_namespaced_pod(self.pod_name, self.namespace)
            logger.info("Aux pod %s deleted", self.pod_name)
        except Exception as e:  # pylint: disable=broad-except - GC/reaper is the backstop
            logger.warning("Could not delete aux pod %s: %s", self.pod_name, e)
        return False


class ClusterContainerRunner:
    """Runs a plugin's commands in a campaign's aux Pod via ``pods/exec``.

    ``workspace`` is a **local** directory in the service; it is mirrored into the
    aux container at the identical absolute path around each :meth:`run`, so the
    plugin's absolute paths stay valid on both sides (see the module docstring for
    the one way this differs from the old shared-volume behaviour).
    """

    def __init__(self, spec, pod_name, namespace, core_v1=None):
        self._spec = spec
        self._pod = pod_name
        self._namespace = namespace
        self._core_v1 = core_v1
        self._container = spec.container_name()
        self.workspace = tempfile.mkdtemp(prefix="robovast_aux_")

    def _client(self):
        if self._core_v1 is None:
            from kubernetes import client, config
            try:
                config.load_incluster_config()
            except Exception:  # noqa: BLE001 - dev/local fallback
                config.load_kube_config()
            self._core_v1 = client.CoreV1Api()
        return self._core_v1

    # -- exec plumbing ------------------------------------------------------

    def _exec(self, command, stdin_data=None, progress_update_callback=None):
        """Exec *command* in the aux container; return collected stdout.

        Raises ``subprocess.CalledProcessError`` on a non-zero exit, so callers
        (and plugins) see the same failure type as the local ``docker run`` path.
        """
        from kubernetes.stream import stream

        resp = stream(
            self._client().connect_get_namespaced_pod_exec,
            self._pod, self._namespace,
            container=self._container,
            command=command,
            stderr=True, stdin=stdin_data is not None, stdout=True, tty=False,
            _preload_content=False,
        )
        out_chunks: list[str] = []
        if stdin_data is not None:
            resp.write_stdin(stdin_data)
        while resp.is_open():
            resp.update(timeout=1)
            if resp.peek_stdout():
                chunk = resp.read_stdout()
                out_chunks.append(chunk)
                if progress_update_callback:
                    for line in chunk.splitlines():
                        progress_update_callback(line)
            if resp.peek_stderr():
                chunk = resp.read_stderr()
                if progress_update_callback:
                    for line in chunk.splitlines():
                        progress_update_callback(line)
                else:
                    logger.debug("aux stderr: %s", chunk.rstrip())
        returncode = resp.returncode
        resp.close()
        if returncode != 0:
            raise subprocess.CalledProcessError(returncode, command)
        return "".join(out_chunks)

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

    def _copy_in(self) -> None:
        """Mirror the local workspace into the container at the same path."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(self.workspace, arcname=".")
        payload = base64.b64encode(buf.getvalue()).decode("ascii")
        self._retrying_exec(
            ["sh", "-c",
             f"mkdir -p '{self.workspace}' && base64 -d | tar xzf - -C '{self.workspace}'"],
            stdin_data=payload)

    def _copy_out(self) -> None:
        """Mirror the container's workspace back over the local one."""
        out = self._retrying_exec(
            ["sh", "-c", f"tar czf - -C '{self.workspace}' . | base64"])
        raw = base64.b64decode("".join(out.split()))
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
            tar.extractall(self.workspace)  # noqa: S202 - our own aux container

    def run(self, command, progress_update_callback=None) -> None:
        progress_update_callback = progress_update_callback or logger.debug
        full_cmd = list(self._spec.command_prefix) + list(command)
        self._copy_in()
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
        # The aux pod is torn down with the campaign by AuxPodSession; the local
        # temp workspace is small and reaped with the pod's scratch.
        pass
