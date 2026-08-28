# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""The Kubernetes lane, as a registered ``vast serve`` backend.

Everything Kubernetes-shaped that ``vast serve`` needs lives behind this one class, so
the core never imports the cluster package to start a service -- it resolves this by
name. When the cluster lane becomes its own distribution, this module is what carries
``vast serve --backend cluster`` into it.
"""

from __future__ import annotations


class ClusterServeBackend:
    """Service driving Kubernetes Jobs, in-pod or from a developer's machine."""

    storage = "object store"

    def build(self, *, in_pod: bool, context: str | None, namespace: str, store,
              workspace_dir=None,
              results_dir=None):  # noqa: ARG002 - pinning and a results dir are local-lane affordances
        """In-pod the config comes from the pod env; off-cluster it is read from the
        deployed service, which is the authoritative record -- so no local setup is
        needed, on any host with kubeconfig access.

        ``results_dir`` is ignored: a cluster campaign's results live in the object store,
        which is the whole reason this lane needs no local directory.
        """
        import click  # pylint: disable=import-outside-toplevel

        from .cluster_service import ClusterService  # pylint: disable=import-outside-toplevel
        if in_pod:
            return ClusterService(kube_context=context, store=store)

        from .service_deploy import \
            read_service_config_from_cluster  # pylint: disable=import-outside-toplevel
        name, kwargs = read_service_config_from_cluster(namespace, context)
        if not name:
            for_ctx = f" in context {context!r}" if context else ""
            raise click.ClickException(
                f"no robovast-service found{for_ctx} (namespace {namespace!r}) to read "
                f"the cluster config from — deploy one with 'vast cluster setup "
                f"<cluster-config>{f' -x {context}' if context else ''}', or check "
                "--context/--namespace.")
        # Off-cluster the driver reaches the cluster's object store through a kubectl
        # port-forward, which is fragile under the large per-file result transfers a big
        # campaign produces. This mode is a dev convenience; the deployed in-cluster
        # service reads the store directly (no tunnel).
        click.secho(
            "WARNING: running the cluster backend off-cluster — campaigns are "
            "driven from this host through a kubectl port-forward to the cluster "
            "object store, which is fragile under large result transfers. This "
            "mode is a dev convenience; run large campaigns via the deployed "
            "in-cluster robovast-service.",
            fg="yellow")
        return ClusterService(namespace=namespace, cluster_config_name=name,
                              cluster_config_kwargs=kwargs, kube_context=context,
                              store=store)
