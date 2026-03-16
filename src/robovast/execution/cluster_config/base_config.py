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

from typing import Optional


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

    def get_s3_credentials(self) -> tuple:
        """Return the ``(access_key, secret_key)`` pair for the S3 service.

        Returns:
            tuple[str, str]: (access_key, secret_key)
        """
        return ("minioadmin", "minioadmin")

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

    def get_cluster_allocatable_resources(self, kube_context=None):
        """Return the total CPU and memory quota for Kueue.

        Called by ``apply_kueue_queues`` to determine the ClusterQueue quota
        before submitting jobs.  The default implementation returns
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
            kwargs: The ``setup_kwargs`` dict that was persisted to the
                    cluster flag file by :func:`save_cluster_setup_info`.
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
                doc.setdefault('spec', {})['nodeSelector'] = dict(node_labels)
        return docs
