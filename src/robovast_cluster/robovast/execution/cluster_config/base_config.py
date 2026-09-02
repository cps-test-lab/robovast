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

import os
import tarfile
from dataclasses import dataclass
from typing import Optional


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


class BaseConfig(object):
    """Base class for cluster configurations.

    Every cluster config plugin must subclass this and implement the abstract
    methods.  The default implementations assume an **embedded MinIO** server
    deployed inside the Kubernetes cluster.  Subclasses that use an external
    S3-compatible service (e.g. Google Cloud Storage) should override the
    ``uses_embedded_s3``, ``get_s3_*``, and ``get_host_s3_endpoint`` methods.
    """

    # ------------------------------------------------------------------
    # Cluster lifecycle
    # ------------------------------------------------------------------

    def setup_cluster(self, **kwargs):
        """Set up the S3-compatible storage infrastructure in the cluster.

        For embedded-MinIO configs this deploys the MinIO pod.  For external-S3
        configs this may deploy only supporting pods (archiver, HTTP server)
        and validate connectivity to the external service.

        Args:
            **kwargs: Cluster-specific configuration options
        """
        raise NotImplementedError("setup_cluster method must be implemented by subclasses.")

    def get_instance_type_command(self):
        """Get command to retrieve instance type of the current node."""
        raise NotImplementedError("get_instance_type_command method must be implemented by subclasses.")

    def cleanup_cluster(self, **kwargs):
        """Tear down the storage infrastructure from the cluster.

        For embedded-MinIO configs this removes the MinIO pod.  For external-S3
        configs this removes supporting pods.  External buckets are **not**
        deleted (user-managed).

        Args:
            **kwargs: Cluster-specific configuration options
        """
        raise NotImplementedError("cleanup_cluster method must be implemented by subclasses.")

    def prepare_setup_cluster(self, output_dir, **kwargs):
        """Prepare the cluster for the run.

        Args:
            output_dir (str): Directory where setup files will be written
            **kwargs: Cluster-specific configuration options
        """
        raise NotImplementedError("prepare_setup_cluster method must be implemented by subclasses.")

    def campaign_object_bytes(self, campaign_id: str, exclude_prefixes=()) -> int:
        """Sum the stored bytes :meth:`add_campaign_members` would put into a tar.

        The denominator for a streamed upload's progress bar. A listing pass only —
        object sizes come back with the keys, so this moves no data and costs one
        request per 1000 objects, which is nothing beside the transfer it measures.
        """
        from robovast.execution.cluster_execution import \
            in_pod_storage  # pylint: disable=import-outside-toplevel
        bucket, prefix = in_pod_storage.campaign_storage_location(self, campaign_id)
        access_key, secret_key = self.get_s3_credentials()
        return _s3_campaign_bytes(
            bucket, endpoint=self.get_driver_s3_endpoint(),
            access_key=access_key, secret_key=secret_key,
            prefix=prefix or None, region=self.get_s3_region(),
            exclude_prefixes=exclude_prefixes)

    def add_campaign_members(self, tar, campaign_id: str, exclude_prefixes=(),
                             on_member=None) -> None:
        """Stream this campaign's stored objects into the open *tar* (no local copy).

        Powers the postprocessed-download stream (``/campaigns/{id}/archive``): each
        object is fetched from storage and added to the streaming tar on the fly, so
        **no scratch is used on the service during or after the download**.
        *exclude_prefixes* drops internal staging (e.g. ``_postproc/``) so the archive
        is the clean campaign layout. The default reads the S3/MinIO backend via the
        driver endpoint, so an off-cluster service reaches MinIO through the same
        host-reachable resolver (port-forward) as every other driver storage client;
        configs backed by a different store (e.g. GCS) override this.
        """
        from robovast.execution.cluster_execution import \
            in_pod_storage  # pylint: disable=import-outside-toplevel
        bucket, prefix = in_pod_storage.campaign_storage_location(self, campaign_id)
        access_key, secret_key = self.get_s3_credentials()
        _s3_add_members(
            tar, bucket, campaign_id,
            endpoint=self.get_driver_s3_endpoint(),
            access_key=access_key, secret_key=secret_key,
            prefix=prefix or None, region=self.get_s3_region(),
            exclude_prefixes=exclude_prefixes, on_member=on_member)

    def verify_cluster_ready(self, k8s_client=None, namespace="default", kube_context=None):
        """Verify the storage infrastructure is ready before launching a run.

        Called by a campaign launch after the cluster config is resolved.
        Configs that deploy in-cluster storage (e.g. the embedded MinIO pod for
        ``rke2``) override this to confirm it is running and raise a
        :class:`RuntimeError` with a remediation hint otherwise.

        The default is a no-op: external-storage configs (e.g. GCS) need no
        in-cluster helper.
        """
        del k8s_client, namespace, kube_context

    # ------------------------------------------------------------------
    # S3 storage configuration
    # ------------------------------------------------------------------

    def uses_embedded_s3(self) -> bool:
        """Return ``True`` if this config runs an embedded MinIO server.

        When ``True`` (the default), host-side tools use ``kubectl port-forward``
        to reach the S3 API.  When ``False``, host-side tools connect directly
        to the endpoint returned by :meth:`get_host_s3_endpoint`.

        Returns:
            bool
        """
        return True

    def get_s3_endpoint(self) -> str:
        """Return the **cluster-internal** S3 endpoint URL.

        Used by init containers and job pods running inside the cluster.

        For embedded MinIO this is ``http://robovast:9000``.
        For external services (e.g. GCS) this may be
        ``https://storage.googleapis.com``.

        Returns:
            str: S3 endpoint URL
        """
        return "http://robovast:9000"

    def get_host_s3_endpoint(self) -> Optional[str]:
        """Return the S3 endpoint URL reachable from the **host** machine.

        * ``None`` (default) – host-side tools open a ``kubectl port-forward``
          to the embedded MinIO pod.
        * A URL string – host-side tools connect directly to this endpoint,
          skipping port-forward.

        Returns:
            str | None
        """
        return None

    def get_driver_s3_endpoint(self, force_reconnect: bool = False,
                               current: Optional[str] = None) -> str:
        """Return the S3 endpoint the in-process driver's **own** storage client
        should use (see :func:`..cluster_execution.in_pod_storage.storage_client_for`).

        Defaults to the cluster-internal endpoint (:meth:`get_s3_endpoint`),
        correct when the driver runs in-cluster. When the driver runs
        **off-cluster** (the service on the host), that host installs a resolver via
        :meth:`set_driver_s3_endpoint_resolver` (typically wrapping
        :meth:`resolve_driver_s3_endpoint`). Job / init-container manifests keep
        using :meth:`get_s3_endpoint`.

        The resolver is consulted **here**, lazily, so a host that opens a
        port-forward pays for it only when the driver actually builds a storage
        client — not on every config build.

        Returns:
            str: S3 endpoint URL
        """
        resolver = getattr(self, "_driver_s3_endpoint_resolver", None)
        endpoint = resolver(force_reconnect, current) if resolver is not None else None
        return endpoint or self.get_s3_endpoint()

    def set_driver_s3_endpoint_resolver(self, resolver) -> None:
        """Install a callable ``resolver(force_reconnect=False)`` returning the driver
        endpoint (or ``None``).

        See :meth:`get_driver_s3_endpoint`. ``None`` clears any resolver, restoring
        the cluster-internal default.
        """
        self._driver_s3_endpoint_resolver = resolver

    def resolve_driver_s3_endpoint(self, open_port_forward,
                                   force_reconnect: bool = False,
                                   current: Optional[str] = None) -> Optional[str]:
        """Policy: the host-reachable S3 endpoint for an off-cluster driver.

        This is where each config declares **how its storage is reachable from the
        host**, so the off-cluster host (the service) needs no per-provider
        knowledge:

        * embedded MinIO (the default) has no host route, so it calls
          *open_port_forward* — a zero-arg callback that opens a ``kubectl
          port-forward`` and returns a ``http://localhost:<port>`` URL;
        * external S3 / GCS-over-S3 return :meth:`get_host_s3_endpoint` (directly
          reachable, no tunnel).

        Native-GCS configs never reach here — ``storage_client_for`` builds a GCS
        client for them, which talks to ``storage.googleapis.com`` from anywhere.

        Args:
            open_port_forward: Callable ``(force_restart=False)`` opening the tunnel
                on demand and returning the resulting host URL. Only invoked when a
                tunnel is needed. *force_restart* tears down a stalled forward and
                opens a fresh one (the driver's storage client requests this after a
                network timeout).
            force_reconnect: Forwarded to *open_port_forward* as *force_restart*.
            current: The endpoint the caller was using; forwarded so a shared forward
                is torn down only once per stall (concurrent callers that already
                rotated past *current* get the fresh endpoint back untouched).
        """
        if self.uses_embedded_s3():
            return open_port_forward(force_reconnect, current)
        return self.get_host_s3_endpoint()

    def get_s3_credentials(self) -> tuple:
        """Return the ``(access_key, secret_key)`` pair for the S3 service.

        Returns:
            tuple[str, str]: (access_key, secret_key)
        """
        return ("minioadmin", "minioadmin")

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

    def get_s3_bucket(self) -> Optional[str]:
        """Return a fixed/shared S3 bucket name, or ``None``.

        * ``None`` (default) – each campaign creates its own bucket
          (embedded-MinIO mode).
        * A bucket name string – all campaigns share this single bucket and
          are separated by key prefixes (external-S3 mode).  The bucket must
          be pre-created by the user.

        Returns:
            str | None
        """
        return None

    def get_s3_region(self) -> str:
        """Return the S3 region to use.

        Returns:
            str: AWS/S3 region name (default ``'us-east-1'`` for MinIO).
        """
        return "us-east-1"

    def get_storage_backend(self) -> str:
        """Return the storage backend identifier: ``'s3'`` or ``'gcs'``.

        The default implementation returns ``'s3'``, which covers both
        embedded MinIO and any external S3-compatible service.  Subclasses
        that use native Google Cloud Storage should override this and return
        ``'gcs'``.

        Returns:
            str: ``'s3'`` (default) or ``'gcs'``.
        """
        return "s3"

    def get_store_usage(self, node_summaries, namespace="default"):
        """``(used_bytes, capacity_bytes)`` for the campaign results store, or ``(None, None)``.

        Called by the service's ``/usage`` endpoint to draw the results-store meter. The
        default ``(None, None)`` means **this provider cannot say**, and the caller reports
        no store figure rather than guessing. That is also the honest answer for a provider
        backed by a cloud bucket: object storage has no capacity to fill, so there is no
        meter to draw -- not a failure to draw one.

        ``node_summaries`` is ``{node_name: kubelet stats/summary dict}``, already fetched
        by the caller, so an override needs no cluster round-trip of its own.
        """
        del node_summaries, namespace
        return None, None

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

        The default implementation is a no-op.  Subclasses that need
        persistent credentials (e.g. :class:`GcpClusterConfig`) should
        override this to re-populate their instance state from the stored
        kwargs so that methods like :meth:`get_s3_credentials` work correctly
        on a freshly instantiated config object.

        Args:
            kwargs: The ``setup_kwargs`` dict recorded at ``setup`` in the deployed
                    robovast-service's env and read back by
                    :func:`~robovast.execution.cluster_execution.service_deploy.read_service_config_from_cluster`.
        """

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


# ---------------------------------------------------------------------------
# S3/MinIO object streaming, used by BaseConfig.add_campaign_members to stream a
# campaign's stored objects into an open tar on the fly (the postprocessed
# download). The GCS variant lives in the gcp config.
# ---------------------------------------------------------------------------

def _s3_add_job_link_entries(tar, s3, bucket_name, prefix, archive_label):
    """Add ``<config>/<run>/job`` symlink members to the streaming tar.

    Reads the ``_transient/job_links.yaml`` manifest object (written by robovast
    for packed campaigns) and adds one real symlink member per entry so the
    tar.gz is navigable. No-op when the manifest object is absent.
    """
    import yaml  # pylint: disable=import-outside-toplevel
    manifest_key = (prefix or "") + "_transient/job_links.yaml"
    try:
        resp = s3.get_object(Bucket=bucket_name, Key=manifest_key)
    except Exception:  # pylint: disable=broad-except
        return  # no manifest → nothing to link
    links = yaml.safe_load(resp["Body"].read()) or {}
    for link_rel, target in links.items():
        tarinfo = tarfile.TarInfo(name=f"{archive_label}/{link_rel}")
        tarinfo.type = tarfile.SYMTYPE
        tarinfo.linkname = target
        tarinfo.mode = 0o777
        tar.addfile(tarinfo)


def _s3_client(endpoint, access_key, secret_key, region):
    """Return a driver-side S3 client (shared by the streaming and sizing passes)."""
    import boto3  # pylint: disable=import-outside-toplevel
    from botocore.config import Config  # pylint: disable=import-outside-toplevel

    return boto3.client(
        "s3", endpoint_url=endpoint,
        aws_access_key_id=access_key, aws_secret_access_key=secret_key,
        region_name=region,
        config=Config(signature_version="s3v4",
                      s3={"addressing_style": "path"},
                      request_checksum_calculation="when_required",
                      response_checksum_validation="when_required"))


def _s3_campaign_bytes(bucket, *, endpoint, access_key, secret_key,
                       prefix=None, region="us-east-1", exclude_prefixes=()) -> int:
    """Sum the sizes of the objects :func:`_s3_add_members` would add. Listing only."""
    prefix = prefix.rstrip("/") + "/" if prefix else None
    excluded = tuple(p.rstrip("/") + "/" for p in exclude_prefixes)
    s3 = _s3_client(endpoint, access_key, secret_key, region)
    paginate_kwargs = {"Bucket": bucket}
    if prefix:
        paginate_kwargs["Prefix"] = prefix
    total = 0
    for page in s3.get_paginator("list_objects_v2").paginate(**paginate_kwargs):
        for obj in page.get("Contents", []):
            relative_key = obj["Key"][len(prefix):] if prefix else obj["Key"]
            if excluded and relative_key.startswith(excluded):
                continue
            total += obj["Size"]
    return total


def _s3_add_members(tar, bucket, archive_name, *, endpoint, access_key,
                    secret_key, prefix=None, region="us-east-1",
                    exclude_prefixes=(), on_member=None) -> None:
    """Stream every object of *bucket*/*prefix* into the open *tar*.

    Each object is fetched and added on the fly (no local copy). The archive's single
    top-level folder is *archive_name* (the campaign id), so it expands to
    ``<campaign>/<config>/<run>/...``. *exclude_prefixes* are campaign-relative path
    prefixes to skip (e.g. ``"_postproc"`` — internal staging that is not part of the
    clean campaign layout).
    """
    prefix = prefix.rstrip("/") + "/" if prefix else None
    excluded = tuple(p.rstrip("/") + "/" for p in exclude_prefixes)

    s3 = _s3_client(endpoint, access_key, secret_key, region)

    paginate_kwargs = {"Bucket": bucket}
    if prefix:
        paginate_kwargs["Prefix"] = prefix
    paginator = s3.get_paginator("list_objects_v2")

    for page in paginator.paginate(**paginate_kwargs):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            relative_key = key[len(prefix):] if prefix else key
            if excluded and relative_key.startswith(excluded):
                continue
            tarinfo = tarfile.TarInfo(name=f"{archive_name}/{relative_key}")
            tarinfo.size = obj["Size"]
            response = s3.get_object(Bucket=bucket, Key=key)
            tarinfo.mode = (
                0o755 if response.get("Metadata", {}).get("executable") == "yes"
                else 0o644)
            tar.addfile(tarinfo, response["Body"])
            if on_member is not None:
                on_member(tarinfo.size)
    _s3_add_job_link_entries(tar, s3, bucket, prefix, archive_name)
