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

    storage = "the service's results volume"

    def build(self, *, in_pod: bool, store, workspace_dir=None, results_dir=None):
        """In-pod only: the config comes from the pod env, and the campaigns live on the
        results volume the deployment mounts (``service_deploy.RESULTS_DATA_DIR``).

        A driver outside the cluster is refused. Every pod a campaign runs delivers its
        outputs to the service's data plane over the cluster network, and a process on a
        developer's machine is not reachable from there. The developer loop that keeps a
        local debugger against a real cluster is to run this same process *inside* the
        cluster's network with its Service's traffic steered to it::

            mirrord exec --target deployment/robovast-service --steal -- \\
                vast serve --backend cluster

        which needs no cluster-side install and resolves the pods' address to this process.
        """
        import click  # pylint: disable=import-outside-toplevel

        from .cluster_service import ClusterService  # pylint: disable=import-outside-toplevel
        if not in_pod:
            raise click.ClickException(
                "the cluster backend runs inside the cluster: a campaign's pods deliver "
                "their outputs to this service over the cluster network, which cannot reach "
                "a process on this host. To debug the driver against a real cluster, run it "
                "in the cluster's network with the Service's traffic steered to it: "
                "'mirrord exec --target deployment/robovast-service --steal -- vast serve "
                "--backend cluster'.")
        return ClusterService(store=store, results_dir=results_dir)
