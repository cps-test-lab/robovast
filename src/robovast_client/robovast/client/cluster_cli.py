# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""``vast cluster`` -- the Kubernetes substrate, and nothing that is not it.

What is here needs a kubeconfig, an API server or a cluster Secret.

What is deliberately not here: launching (``vast workspace run`` -- a campaign runs a
workspace's project, never a property of the cluster), and ``stop``/``stop-job``/``log``,
which only drive the service and so are ``vast campaign`` verbs.

What stays in ``robovast-cluster`` is the half that genuinely needs a cluster: ``setup``,
``cleanup``, ``upgrade``, ``token``, ``jobs-cleanup``, and ``monitor``. They attach here
through the ``robovast.cluster_plugins`` entry-point group, so they are listed without
being imported and an install without that package is short a subcommand rather than
broken.

``monitor`` is the one that could have come along and deliberately did not. Its
service-driven view is pure client code, but its kubeconfig view is not, and the two are
one command chosen at runtime -- so moving it would split a single function's body across
two distributions. Everything it offers a *client* user (runs done/total, pending job
counts, a search campaign's best objective) is already on the web UI; what it adds over the
UI is the kubeconfig view of the Jobs, which is operator work by definition. A client user
gets ``vast campaign wait`` and the UI instead. The two are not substitutes and should not be
described as such: ``wait`` is phase-level, one campaign, and blocks with an exit-code
contract built for scripts; ``monitor`` is job-level, every campaign, and a live dashboard.
"""


import click

from robovast.client.lazy_group import LazyPluginGroup

#: Entry-point group for subcommands that attach to ``vast cluster``.
CLUSTER_PLUGIN_GROUP = "robovast.cluster_plugins"


@click.group(cls=LazyPluginGroup, plugin_group=CLUSTER_PLUGIN_GROUP)
def cluster():
    """Set up and maintain the Kubernetes substrate campaigns run on.

    These read what they need from the cluster itself, so they work from any directory.
    To act on a *campaign*, use ``vast campaign``; on the service, ``vast service``.

    Every verb needs a kubeconfig and arrives with ``robovast-cluster``, so what this
    lists depends on what is installed.
    """
