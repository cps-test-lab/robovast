# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""``vast cluster`` -- the Kubernetes substrate, and nothing that is not it.

What is here needs a kubeconfig, an API server or a cluster Secret -- except
``store-cleanup``, which deletes result buckets *through the service* and so needs neither,
and ships with the client for the same reason the campaign verbs do: the audience that
drives a cluster is not the audience that owns one.

What used to be here and is not: launching (``vast workspace run`` -- a campaign runs a
workspace's project, never a property of the cluster), and ``stop``/``stop-job``/``log``,
which only ever drove the service and work identically against a local one. Their old path
named a lane the request cannot express, so ``vast campaign stop`` against a local
service said "cluster" and stopped a Docker campaign. They are ``vast campaign`` verbs now.

What stays in ``robovast-cluster`` is the half that genuinely needs a cluster: ``setup``,
``cleanup``, ``upgrade``, ``token``, ``run-cleanup``, and ``monitor``. They attach here
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

from robovast.client.errors import handle_cli_exception
from robovast.client.lazy_group import LazyPluginGroup
from robovast.client.service_target import echo_target as _echo_target
from robovast.client.service_target import service_client, target_options

#: Entry-point group for subcommands that attach to ``vast cluster``.
CLUSTER_PLUGIN_GROUP = "robovast.cluster_plugins"


@click.group(cls=LazyPluginGroup, plugin_group=CLUSTER_PLUGIN_GROUP)
def cluster():
    """Set up and maintain the Kubernetes substrate campaigns run on.

    These read what they need from the cluster itself, so they work from any directory.
    To act on a *campaign*, use ``vast campaign``; on the service, ``vast service``.

    ``store-cleanup`` ships with ``robovast-client``; everything else needs a kubeconfig
    and arrives with ``robovast-cluster``, so what this lists depends on what is installed.
    """


@cluster.command(name='store-cleanup')
@click.option('--campaign', '-i', default=None,
              help='Only remove this campaign\'s bucket (e.g. campaign-2025-02-27-123456). Without this, removes all campaign buckets.')
@click.option('--force', is_flag=True,
              help='Delete a named campaign even if the service still considers it live.')
@target_options
def store_cleanup(campaign, force, namespace, context):
    """Remove result buckets from the cluster object store (via the service).

    Deletes run result buckets (``campaign-*``) from the object store. This runs
    **through the robovast-service**, which holds the object-store credentials and
    the authoritative live-campaign set — so no local credentials are needed and a
    bulk delete never removes a campaign that is still running.

    The service is resolved the usual way: the conventional local port if one
    answers, otherwise the one ``vast login`` stored. Use ``--campaign`` to remove a
    single one.
    """
    try:
        from robovast.service.interface import CleanupDataRequest
        with service_client(namespace, context,
                            require_service=True) as (client, target):
            _echo_target(target)
            res = client.cleanup_campaign_data(
                CleanupDataRequest(campaign_id=campaign, force=force))
            if not res.ok:
                raise click.ClickException(res.message or "cleanup-data failed")
            click.echo(f"✓ {res.message}")
    # The bare re-raise is deliberate: click handles UsageError/ClickException itself, printing
    # usage and setting the exit code, so they must pass the broad handler below rather than be
    # folded into handle_cli_exception. pylint calls it redundant only because super-linter lints
    # with none of the project's dependencies installed, leaving click's types unresolvable --
    # the same reason .pylintrc already disables import-error.
    # pylint: disable-next=try-except-raise
    except (click.UsageError, click.ClickException):
        raise
    except Exception as e:
        handle_cli_exception(e)
