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

import logging
import os
from dataclasses import dataclass

import yaml

logger = logging.getLogger(__name__)


@dataclass
class RegistryConfig:
    """Container-registry settings for agent-built experiment images.

    Registry details live **only** here (server-side) — a registry endpoint,
    credential, or fully-qualified ref never crosses the client interface. The
    service prepends :attr:`registry_prefix` to a project's bare ``build.tag`` and
    resolves the concrete ref; the client only ever sees the symbolic ``build:<tag>``.
    """

    #: e.g. ``"registry.local:5000/robovast"`` or ``"ghcr.io/cps-test-lab"``.
    registry_prefix: str = ""
    #: k8s ``dockerconfigjson`` Secret mounted into the build Job for ``docker push``.
    push_secret_name: str = ""
    #: k8s ``imagePullSecret`` added to campaign Job pods so they can pull the image.
    pull_secret_name: str = ""
    #: Default ``FROM`` when ``build.base_image`` is omitted (or is an alias).
    base_experiment_image: str = ""
    #: Push to the registry over plain HTTP / an untrusted cert (e.g. a
    #: cluster-internal registry). The BuildKit push output gets
    #: ``registry.insecure=true``. Prefer :attr:`ca_configmap_name` for real registries.
    insecure: bool = False
    #: ConfigMap (key ``ca.pem``) holding the registry's CA, mounted into the build
    #: Job so BuildKit trusts a self-signed / private-CA registry. Pull-side trust
    #: for such a registry is node-level (containerd), configured by the operator.
    ca_configmap_name: str = ""

    def enabled(self) -> bool:
        """True when a registry is configured (in-cluster builds are possible)."""
        return bool(self.registry_prefix)

    def why_disabled(self) -> str:
        """Why in-cluster builds are unavailable, and both commands that fix it.

        Empty when :meth:`enabled`. The **shared half only**: each caller prefixes what it
        was doing when it found out, because that differs usefully — a campaign author needs
        to hear "this campaign builds an image", and a request caller does not.

        **Both remedies, because this process cannot tell the two states apart.** The prefix
        is baked from the service's Ingress host at setup/upgrade time and read back here out
        of the environment; the in-pod service has no RBAC to read its own Ingress and is
        deliberately not given any. So "published, but the prefix was dropped" and "never
        published" are indistinguishable from in here, and they have different fixes.
        Naming only one sent an operator to re-run ``setup --ingress-host`` on a deployment
        that was already published, where ``upgrade`` was the answer.

        Carries no registry host, prefix or credential: registry details never cross the
        client interface (see this class's docstring), and this string reaches a client.
        """
        if self.enabled():
            return ""
        # "push it" rather than "push a built image": each caller's opener has already
        # named the image, so repeating it reads as a stutter in both compositions.
        return ("this cluster has nowhere to push it. RoboVAST runs its own "
                "registry in the service pod, reached over the service's own Ingress, and "
                "this service's registry prefix is unset. If the service is published, "
                "'vast service upgrade' re-bakes the prefix from the live Ingress; if "
                "it is not published at all, re-run 'vast cluster setup' with "
                "--ingress-host. 'vast doctor -n <namespace>' says which.")


#: The keyword arguments :meth:`BaseConfig.setup_cluster` hands to
#: :func:`~robovast.execution.cluster_execution.store_pod.attach_infrastructure`. Named so the
#: provider ``-o`` options that travel in the same ``kwargs`` (and are persisted as the
#: cluster's recorded config) are not mistaken for placement.
STORE_POD_PLACEMENT_KEYS = (
    "registry_storage_path", "registry_storage_class",
    "registry_authenticated", "ingress_class",
)


class BaseConfig(object):
    """A cluster provider: the ``robovast`` pod, and what the cluster can say about itself.

    Every cluster config plugin subclasses this. A provider does two things. It deploys the
    deployment's setup-lifetime pod -- the container registry
    (:mod:`~robovast.execution.cluster_execution.store_pod`) -- which is the same on every
    provider and is therefore done here, with the provider deciding only what its README
    says about backing the volumes. And it answers the scheduling questions setup and
    admission ask of a cluster: how a node reports its instance type, the registry
    configuration, host aliases for the pods RoboVAST creates, and how much the cluster
    can grow to.

    Campaigns are not a provider's concern. They live on the service's results volume
    (:data:`~robovast.execution.cluster_execution.service_deploy.RESULTS_VOLUME_NAME`),
    which the service Deployment carries and places.
    """

    # ------------------------------------------------------------------
    # Cluster lifecycle
    # ------------------------------------------------------------------

    def store_pod_manifest(self, **kwargs) -> list:
        """The ``robovast`` pod, its Service and its claims, placed as *kwargs* say.

        *kwargs* is what :meth:`setup_cluster` receives: ``namespace``, the placement
        arguments in :data:`STORE_POD_PLACEMENT_KEYS`, and ``control_node_labels`` -- the
        data node's selector ANDed with the operator's control pool, which the pod takes
        because its registry blobs are hostPath-backed unless a class says otherwise, and an
        unpinned pod would come back on another node with an empty registry.
        """
        from ..cluster_execution import store_pod  # pylint: disable=import-outside-toplevel

        docs = store_pod.attach_infrastructure(
            [], kwargs.get("namespace", "default"),
            **{k: kwargs[k] for k in STORE_POD_PLACEMENT_KEYS if k in kwargs})
        return self._apply_pod_node_selector(docs, kwargs.get("control_node_labels"))

    def setup_cluster(self, **kwargs):
        """Deploy the ``robovast`` pod (the registry) and its Service.

        A live pod is kept as it is (``apply_manifests`` tolerates a 409), so a placement
        the live pod does not match is refused before anything is applied rather than
        reported and never applied.

        Args:
            **kwargs: ``namespace``, ``kube_context``, ``control_node_labels``, the
                placement arguments in :data:`STORE_POD_PLACEMENT_KEYS`, and the
                provider's own ``-o`` options.
        """
        from kubernetes import client  # pylint: disable=import-outside-toplevel

        from ..cluster_execution import store_pod  # pylint: disable=import-outside-toplevel
        from ..cluster_execution.kube_client import \
            load_kube_config  # pylint: disable=import-outside-toplevel
        from ..cluster_execution.kubernetes import \
            apply_manifests  # pylint: disable=import-outside-toplevel

        namespace = kwargs.get("namespace", "default")
        docs = self.store_pod_manifest(**kwargs)
        load_kube_config(context=kwargs.get("kube_context"))
        store_pod.refuse_a_pod_on_the_wrong_node(namespace, kwargs.get("control_node_labels"))
        try:
            apply_manifests(client.ApiClient(), iter(docs), namespace=namespace)
        except Exception as e:
            raise RuntimeError(
                f"Error applying the {store_pod.STORE_POD_NAME} pod manifest: {e}") from e
        logger.info("The %s pod (the registry) is deployed in namespace %s",
                    store_pod.STORE_POD_NAME, namespace)

    def cleanup_cluster(self, **kwargs):
        """Remove the ``robovast`` pod, its Service and the claims setup may have created.

        The registry is re-derivable and goes with the pod. The campaigns are on the
        service's results volume, which this does not touch.

        Args:
            **kwargs: ``namespace``, ``kube_context``, and the provider's own options.
        """
        from kubernetes import client  # pylint: disable=import-outside-toplevel

        from ..cluster_execution import store_pod  # pylint: disable=import-outside-toplevel
        from ..cluster_execution.kube_client import \
            load_kube_config  # pylint: disable=import-outside-toplevel
        from ..cluster_execution.kubernetes import \
            delete_manifests  # pylint: disable=import-outside-toplevel

        namespace = kwargs.get("namespace", "default")
        load_kube_config(context=kwargs.get("kube_context"))
        delete_manifests(
            client.CoreV1Api(),
            store_pod.infrastructure_claims(namespace)
            + store_pod.attach_infrastructure([], namespace),
            namespace=namespace)
        logger.debug("The %s pod is removed from namespace %s",
                     store_pod.STORE_POD_NAME, namespace)

    def prepare_setup_cluster(self, output_dir, **kwargs):
        """Write what a manual setup needs: the pod manifest and a README for this provider.

        The README says where campaigns live and how this provider's operator should back
        the volumes, which is the one thing that differs between providers.

        Args:
            output_dir (str): Directory where setup files will be written
            **kwargs: The same options :meth:`setup_cluster` takes
        """
        raise NotImplementedError("prepare_setup_cluster method must be implemented by subclasses.")

    def write_store_pod_manifest(self, output_dir, **kwargs) -> str:
        """Write the manifest :meth:`setup_cluster` would apply, for a manual ``kubectl apply``.

        The same documents, from the same arguments: a manifest written for hand-applying
        must describe the pod this deployment actually uses, or following the README
        produces a different cluster from running the command.
        """
        path = f"{output_dir}/robovast-manifest.yaml"
        with open(path, "w") as f:
            f.write("---\n".join(yaml.dump(d, default_flow_style=False)
                                for d in self.store_pod_manifest(**kwargs)))
        return path

    def get_instance_type_command(self):
        """Get command to retrieve instance type of the current node."""
        raise NotImplementedError("get_instance_type_command method must be implemented by subclasses.")

    def get_registry_config(self) -> RegistryConfig:
        """Return the registry config for agent-built experiment images.

        Configured at ``vast cluster setup`` (registry prefix + push/pull
        Secrets). The default is **disabled** (no registry) — in-cluster image
        builds are unavailable until a deployment provides one. Environment
        overrides ease dev/minikube setups (and CI):
        ``ROBOVAST_REGISTRY_PREFIX``, ``ROBOVAST_REGISTRY_PUSH_SECRET``,
        ``ROBOVAST_REGISTRY_PULL_SECRET``, ``ROBOVAST_BASE_EXPERIMENT_IMAGE``.

        Registry details never cross the client interface (see
        :class:`RegistryConfig`).
        """
        return RegistryConfig(
            registry_prefix=os.environ.get("ROBOVAST_REGISTRY_PREFIX", ""),
            push_secret_name=os.environ.get("ROBOVAST_REGISTRY_PUSH_SECRET", ""),
            pull_secret_name=os.environ.get("ROBOVAST_REGISTRY_PULL_SECRET", ""),
            base_experiment_image=os.environ.get("ROBOVAST_BASE_EXPERIMENT_IMAGE", ""),
            insecure=os.environ.get("ROBOVAST_REGISTRY_INSECURE", "").strip().lower()
            in ("1", "true", "yes"),
            ca_configmap_name=os.environ.get("ROBOVAST_REGISTRY_CA_CONFIGMAP", ""),
        )

    def get_host_aliases(self) -> list:
        """Return Kubernetes ``hostAliases`` entries for the pods RoboVAST creates.

        For a host the cluster's DNS cannot resolve — typically a registry whose name
        lives only in ``/etc/hosts`` on the operator's workstation, where a push fails
        with ``dial tcp: lookup <host>: no such host``. Declare it once instead of
        editing CoreDNS::

            ROBOVAST_EXTRA_HOST_ALIASES=harbor.example.org=10.0.0.9,other.example=10.0.0.10

        Applies to the build Job and campaign Jobs. It does **not** affect the *image
        pull*: that is done by the container runtime on the node, which reads neither
        pod specs nor CoreDNS, so an unresolvable registry still needs the name in each
        node's own resolver (same node-level scope as registry trust). A real DNS record
        remains the fix that covers both.

        Returns:
            list: ``[{"ip": …, "hostnames": [...]}, …]`` — empty when unset.

        Raises:
            ValueError: on a malformed entry; a silently dropped alias would surface
                far away as an unexplained DNS failure inside a pod.
        """
        raw = os.environ.get("ROBOVAST_EXTRA_HOST_ALIASES", "").strip()
        if not raw:
            return []
        by_ip: dict = {}
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            host, sep, ip = item.partition("=")
            host, ip = host.strip(), ip.strip()
            if not sep or not host or not ip:
                raise ValueError(
                    f"ROBOVAST_EXTRA_HOST_ALIASES entry {item!r} is not '<hostname>=<ip>' "
                    "(comma-separated for several)")
            # Grouped by IP because that is the shape of the k8s field: one entry per
            # address, carrying all its names.
            by_ip.setdefault(ip, [])
            if host not in by_ip[ip]:
                by_ip[ip].append(host)
        return [{"ip": ip, "hostnames": hosts} for ip, hosts in by_ip.items()]

    def get_cluster_allocatable_resources(self, kube_context=None):
        """Return the total CPU and memory capacity admission should size against.

        Called by ``ClusterBudgetProvider`` to decide how large the cluster can
        get.  The default implementation returns
        ``(None, None)``, which instructs the caller to fall back to querying
        the Kubernetes node API (total allocatable across all current nodes).

        Subclasses should override this when the cluster supports autoscaling so
        that the quota reflects the *maximum* possible capacity rather than the
        currently provisioned capacity.

        Args:
            kube_context: Kubernetes context name.  ``None`` uses the active
                context.

        Returns:
            tuple: ``(cpu_quota: int, memory_quota: str)`` e.g. ``(64, "256Gi")``,
                   or ``(None, None)`` to fall back to the K8s node query.
        """
        return None, None

    def restore_from_setup_kwargs(self, kwargs: dict) -> None:
        """Restore config state from the kwargs saved during ``setup_cluster``.

        The default implementation is a no-op. A subclass whose scheduling answers
        depend on options given at ``setup`` overrides this to re-populate its instance
        state from the stored kwargs, so a freshly instantiated config answers the same.

        Args:
            kwargs: The ``setup_kwargs`` dict recorded at ``setup`` in the deployed
                    robovast-service's env and read back by
                    :func:`~robovast.execution.cluster_execution.service_deploy.read_service_config_from_cluster`.
        """

    #: Whether this provider's nodes can have their CPU governor set at all.
    #:
    #: ``True`` is the safe default: setup attempts it, and a cluster that cannot take it
    #: is reported rather than assumed. A provider whose nodes are virtual machines sets
    #: this ``False`` -- their kernels expose no cpufreq policy, because the hypervisor
    #: owns the clock -- so setup does not spend a readiness wait per run discovering that
    #: again, and says once why it is not trying.
    #:
    #: It is a **default**, never an override: ``--performance-governor`` is obeyed and still
    #: fails loudly, because a supplied argument may not be overruled by provider policy.
    governor_is_settable = True

    @staticmethod
    def _apply_pod_node_selector(yaml_objects, node_labels):
        """Inject ``nodeSelector`` into all ``Pod`` objects.

        Args:
            yaml_objects: Iterable of parsed YAML dicts (from ``yaml.safe_load_all``).
            node_labels: ``dict`` of ``{label_key: label_value}`` to apply as
                ``spec.nodeSelector``.  When ``None`` or empty the objects are
                returned unchanged.

        Returns:
            list: The (possibly modified) list of YAML dicts.
        """
        docs = list(yaml_objects)
        if not node_labels:
            return docs
        for doc in docs:
            if doc and doc.get('kind') == 'Pod':
                # Merged, not replaced. Two intents reach here -- the operator's node pool
                # from `control.node_labels` and the placement label -- and replacing would
                # silently drop whichever arrived first.
                selector = doc.setdefault('spec', {}).setdefault('nodeSelector', {})
                selector.update(node_labels)
        return docs
