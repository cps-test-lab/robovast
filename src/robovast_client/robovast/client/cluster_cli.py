# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""``vast exec cluster`` -- the group shell, and the verbs that only drive a service.

Every cluster campaign is driven by the **robovast-service**, which runs it in-process and
creates the scenario Jobs itself. Acting on one is therefore an HTTP verb against that
service and nothing else: no kubeconfig, no Kubernetes client, no Docker. That is why
``stop``, ``stop-job``, ``log`` and ``download-cleanup`` live in the client -- the
audience that *drives* a cluster is not the audience that *owns* one, and making them
install ``robovast-cluster`` (which depends on the full core) to stop a campaign meant
installing 290 MB to send an HTTP POST.

Launching is **not** here: a campaign runs a workspace's project, which is not a property
of the cluster and never was. ``vast workspace run`` is the one way to start one, and it is
addressed the way the service accepts it -- a workspace and a path.

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
gets ``vast wait`` and the UI instead. The two are not substitutes and should not be
described as such: ``wait`` is phase-level, one campaign, and blocks with an exit-code
contract built for scripts; ``monitor`` is job-level, every campaign, and a live dashboard.
"""

import os

import click

from robovast.client.errors import handle_cli_exception
from robovast.client.lazy_group import LazyPluginGroup
from robovast.client.service_target import echo_target as _echo_target
from robovast.client.service_target import service_client, target_options
from robovast.client.tail import tail_chunks

#: Entry-point group for subcommands that attach to ``vast exec cluster``.
CLUSTER_PLUGIN_GROUP = "robovast.cluster_plugins"


@click.group(cls=LazyPluginGroup, plugin_group=CLUSTER_PLUGIN_GROUP)
def cluster():
    """Act on a Kubernetes cluster and the campaigns running on it.

    These verbs read what they need from the cluster or the service, so they work from
    any directory. To *start* a campaign, use ``vast workspace run``: a campaign runs a
    workspace's project, which is not a property of the cluster.

    The verbs that only drive the service ship with ``robovast-client``; the ones that
    need a kubeconfig arrive with ``robovast-cluster``, so what this lists depends on
    what is installed.
    """


def _sole_running_campaign(client):
    """The one running campaign's id, or None; errors if several are running.

    Campaigns run in parallel now, so a bare ``stop`` is only unambiguous when
    exactly one is live.
    """
    from robovast.client.status import is_running
    from robovast.service.interface import ListCampaignsRequest
    live = [c for c in client.list_campaigns(ListCampaignsRequest(limit=100)).campaigns
            if is_running(c.phase)]
    if not live:
        return None
    if len(live) > 1:
        names = ", ".join(c.campaign_id for c in live)
        raise ValueError(
            f"{len(live)} campaigns are running ({names}); pass --campaign to choose one.")
    return live[0].campaign_id


@cluster.command()
@click.option('--campaign', '-i', default=None,
              help='Campaign to stop (default: the only running one)')
@target_options
def stop(campaign, namespace, context):
    """Ask a running campaign to stop gracefully (after the current batch).

    Goes through the robovast-service, which drives the campaign in-process: the
    loop ends once the in-flight batch finishes and the campaign is published as
    usual. A no-op if nothing is running.
    """
    try:
        with service_client(namespace, context,
                            require_service=True) as (client, target):
            _echo_target(target)
            campaign_id = campaign or _sole_running_campaign(client)
            if campaign_id is None:
                click.echo("No running campaign found.")
                return
            result = client.stop(campaign_id)
            if result.ok:
                click.echo(f"Stop requested for '{campaign_id}'. "
                           "The campaign will end after the current batch.")
            else:
                click.echo(f"Stop failed: {result.message}")
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


@cluster.command('stop-job')
@click.argument('job_name')
@click.option('--campaign', '-i', default=None,
              help='Campaign the job belongs to (default: the only running one)')
@click.option('--reason', default=None, metavar='TEXT',
              help='Why you stopped it — stored with the run and shown in the results')
@target_options
def stop_job(job_name, campaign, reason, namespace, context):
    """Kill ONE running job; the rest of the campaign keeps going.

    For a job that is visibly wedged and will not exit on its own. This is not how a
    campaign is ended -- that is ``vast exec cluster stop``, which stops all of it.

    Only a *running* job can be stopped. The kill is permanent and recorded: the runs it
    cut short report ``status='killed'`` in the campaign data, with the reason, and count
    as neither passes nor failures.
    """
    try:
        with service_client(namespace, context,
                            require_service=True) as (client, target):
            _echo_target(target)
            campaign_id = campaign or _sole_running_campaign(client)
            if campaign_id is None:
                click.echo("No running campaign found.")
                return
            result = client.stop_job(campaign_id, job_name, reason, "cli")
            if result.ok:
                click.echo(f"Stopped job '{job_name}' of '{campaign_id}'. {result.message}")
            else:
                click.echo(f"Stop failed: {result.message}")
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


@cluster.command()
@click.option('--campaign', '-i', default=None,
              help='Campaign to show the log for (default: the only running one)')
@click.option('--follow', '-f', is_flag=True,
              help='Stream new output until the campaign finishes')
@target_options
def log(campaign, follow, namespace, context):
    """Print a campaign's unified infrastructure log.

    The same divider-separated stream the web UI and MCP show — the variation
    (config-generation), run (controller) and postprocessing phases in order, each
    under a ``===== PHASE =====`` divider. Goes through the robovast-service when
    one is reachable (the conventional local port, else the ``vast login`` record);
    otherwise reads the campaign from disk, which needs the full ``robovast``
    distribution.
    """
    try:
        from robovast.service.http_client import HTTPTransport
        with service_client(namespace, context) as (client, target):
            _echo_target(target)
            if isinstance(client, HTTPTransport):
                campaign_id = campaign or _sole_running_campaign(client)
                if campaign_id is None:
                    click.echo("No running campaign found; pass --campaign.")
                    return
                tail_chunks(lambda o: client.get_campaign_logs(campaign_id, o),
                            lambda text: click.echo(text, nl=False), follow=follow)
                return

            # No service reachable: read the campaign directory directly. That reader is
            # part of the core, so on a client-only install this is a capability that is
            # not installed rather than a failure -- say so, and name what provides it.
            try:
                from robovast.common.campaign_logs import assemble_log_from_dir
            except ImportError as exc:
                raise click.ClickException(
                    "No robovast-service reachable, and reading a campaign off disk "
                    "needs the full 'robovast' distribution. Run 'vast login <url>' to "
                    "use a service instead.") from exc
            if not campaign:
                raise ValueError(
                    "No robovast-service reachable; pass --campaign (name or path) "
                    "to read a campaign on disk.")
            if os.path.isabs(campaign):
                campaign_dir = campaign
            else:
                from robovast.client.project_config import ProjectConfig
                cfg = ProjectConfig.load()
                if cfg is None or not cfg.results_dir:
                    raise ValueError(
                        "Project not initialized; run 'vast init' or pass an "
                        "absolute campaign path.")
                campaign_dir = os.path.join(cfg.results_dir, campaign)
            text, _, _ = assemble_log_from_dir(campaign_dir, offset=0, eof=True)
            click.echo(text, nl=False)
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


@cluster.command()
@click.option('--yes', '-y', is_flag=True,
              help='Do not ask before rolling over live campaigns. For scripts; without it '
                   'a non-interactive run aborts rather than rolling silently.')
@click.option('--wait', is_flag=True,
              help='Block until the new pod is the one serving, instead of returning once '
                   'the roll has been asked for.')
@target_options
def restart(yes, wait, namespace, context):
    """Roll the deployed service onto the newest image at its tag, and nothing else.

    Asks the service to restart itself, so this needs only a URL and a token -- which is the
    point: ``vast exec cluster upgrade`` needs a kubeconfig, so somebody who reached the
    deployment through ``vast login`` had the web UI's button and no command at all.

    \b
    restart  the image, by stamping the Deployment's restart annotation. With
             imagePullPolicy: Always that is what moves a floating tag onto new bytes.
    upgrade  that, plus RBAC, the Kueue queues, the registry ingress route, the credential
             Secrets and the build daemon.

    **This reconciles none of those.** A version needing a permission the last one did not
    will deploy and then fail at runtime with a 403, which reads as a bug rather than as a
    missed migration. So this is the command for "new bytes are published and nothing else
    changed"; for a version bump, a missed RBAC migration, a rotated Secret or a registry
    move, use ``vast exec cluster upgrade``. The Secrets in particular are rebuilt from the
    operator's environment, which the pod does not have, so they can never be done from in
    there at any level of privilege.

    Refuses while campaigns are live -- their controller runs in the pod being replaced --
    and names them. ``--yes`` skips the question.
    """
    try:
        with service_client(namespace, context, require_service=True) as (client, target):
            _echo_target(target)
            info = client.upgrade_info()
            if not info.supported:
                raise click.ClickException(info.unsupported_reason)
            live = info.active_campaigns
            if live and not yes:
                click.echo(f"  {len(live)} campaign(s) are live, and the pod this replaces "
                           f"is where their controller runs:")
                for campaign in live:
                    click.echo(f"    {campaign.campaign_id}  [{campaign.phase}]")
                # abort=True and no --force twin: on a non-TTY click.confirm aborts by
                # itself, so a script that did not pass --yes fails loudly rather than
                # rolling over a campaign nobody was watching.
                click.confirm("  roll anyway?", abort=True)
            before = info.running_digest
            # force only carries the answer already given above. Sending it unconditionally
            # would make the service's own refusal unreachable, leaving this the only place
            # the guard exists -- and the web UI would then be guarding on its own copy.
            result = client.upgrade_service(bool(live))
            if not result.ok:
                raise click.ClickException(result.message or "restart failed")
            click.echo(f"✓ {result.message}")
            if wait:
                _wait_for_handover(client, before)
    # pylint: disable-next=try-except-raise
    except (click.UsageError, click.ClickException):
        raise
    except Exception as e:
        handle_cli_exception(e)


#: How long to watch for the new pod. Matches ``vast exec cluster upgrade --timeout``'s
#: default, so the two give up at the same point and an operator comparing them is not
#: told two different stories.
_HANDOVER_TIMEOUT_S = 180.0


def _wait_for_handover(client, before):
    """Block until the running digest changes, or say we could not tell.

    Watches the digest rather than trusting the call that asked for the roll: that returns
    as soon as the Deployment is patched. ``running_image_digest`` reads the newest Running
    pod, so this answers the same whichever pod the Service happens to route a poll to.
    """
    import time
    deadline = time.time() + _HANDOVER_TIMEOUT_S
    while time.time() < deadline:
        time.sleep(3.0)
        try:
            now = client.upgrade_info().running_digest
        except Exception:  # pylint: disable=broad-except
            continue  # expected once or twice at the handover; a blip is not a failure
        if now and now != before:
            click.echo(f"✓ the new pod is serving ({now[:19]})")
            return
    # Not phrased as a failure: the roll may simply be slow, and the command that can say
    # why is the one named here.
    click.echo("  the new pod has not taken over yet. 'vast exec cluster upgrade' reports "
               "the reason Kubernetes gave -- an image it cannot pull, a node it cannot "
               "schedule on, a crash-loop.")


@cluster.command(name='download-cleanup')
@click.option('--campaign', '-i', default=None,
              help='Only remove this campaign\'s bucket (e.g. campaign-2025-02-27-123456). Without this, removes all campaign buckets.')
@click.option('--force', is_flag=True,
              help='Delete a named campaign even if the service still considers it live.')
@target_options
def download_cleanup(campaign, force, namespace, context):
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
