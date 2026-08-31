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
              workspace_dir=None, results_dir=None):
        """In-pod the config comes from the pod env; off-cluster it is read from the
        deployed service, which is the authoritative record -- so no local setup is
        needed, on any host with kubeconfig access.

        ``results_dir`` is honoured here, not ignored. A cluster campaign's *durable* home is
        the object store, but the one being driven has a local working root all the same: each
        batch downloads its own results into it, per-run extraction reads it through a path
        (``search.extractor.Extractor.extract``), and postprocessing derives ``data.db`` from
        it. Leaving that on the container's writable layer meant every restart discarded it --
        and, because resume rebuilds it before the port is bound, a restart could never finish.
        The deployment names the directory it mounts (``service_deploy.RESULTS_DATA_DIR``).
        """
        import click  # pylint: disable=import-outside-toplevel

        from .cluster_service import ClusterService  # pylint: disable=import-outside-toplevel
        if in_pod:
            return ClusterService(kube_context=context, store=store,
                                  results_dir=results_dir)

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
                              store=store, results_dir=results_dir)
