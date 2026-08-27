# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""``vast service`` -- act on the robovast-service itself.

Not on a campaign it runs and not on the cluster it runs in: on the deployed process.
``log`` is its own log rather than a campaign's, and several failures in RoboVAST are
diagnosable only from it. ``restart`` and ``upgrade`` move it onto new bytes.

The group spans two distributions, and that is the point rather than an accident: it is
defined by its *object*, not by what you installed. ``restart`` needs only a URL and a
token, so it ships with the client; ``upgrade`` and ``token`` reach an API server, so they
attach from ``robovast-cluster``. What ``--help`` lists therefore depends on the install.

Some verbs do not apply to every deployment, and say so rather than pretending. A service
started in a venv is "however it was installed and started", so it has no image to roll:
``upgrade`` refuses, naming that, instead of reporting a capability it does not have.
"""


import click

from robovast.client.errors import handle_cli_exception
from robovast.client.lazy_group import LazyPluginGroup
from robovast.client.service_target import echo_target as _echo_target
from robovast.client.service_target import service_client, target_options
from robovast.client.tail import tail_chunks

#: Entry-point group for subcommands that attach to ``vast service``.
SERVICE_PLUGIN_GROUP = "robovast.service_plugins"


@click.group(cls=LazyPluginGroup, plugin_group=SERVICE_PLUGIN_GROUP)
def service():
    """Act on the robovast-service: read its log, roll it onto new bytes.

    The verbs that need only a URL and a token ship with ``robovast-client``; the ones
    that reach a Kubernetes API server arrive with ``robovast-cluster``.
    """


@service.command('log')
@click.option('--follow', '-f', is_flag=True,
              help='Keep printing new output until interrupted.')
@target_options
def service_log(follow, namespace, context):
    """Print what the robovast-service itself has been doing.

    Not a campaign's log -- this is the service process: what it decided, what it refused,
    and the reason behind a failure whose visible half was one terse line. Several failures
    say so in as many words ("the real reason is only in the service log"), and until now
    there was no way to read it short of ``kubectl logs``.

    The service keeps the last few hundred kilobytes in memory, so this covers what it has
    been doing recently, not its whole life, and a restart clears it. A container that has
    already died is only in ``kubectl logs -p deploy/robovast-service``: a buffer inside a
    process cannot outlive the process.
    """
    try:
        # require_service, because there is no such thing as the log of a service that is
        # not running: with no URL this layer yields an in-process transport, which has no
        # serving process whose stderr this would be.
        with service_client(namespace, context, require_service=True) as (client, target):
            _echo_target(target)
            tail_chunks(lambda o: client.get_service_log(o),  # pylint: disable=unnecessary-lambda
                        lambda text: click.echo(text, nl=False), follow=follow)
    # pylint: disable-next=try-except-raise
    except (click.UsageError, click.ClickException):
        raise
    except Exception as e:
        handle_cli_exception(e)




@service.command()
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
    point: ``vast service upgrade`` needs a kubeconfig, so somebody who reached the
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
    move, use ``vast service upgrade``. The Secrets in particular are rebuilt from the
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


#: How long to watch for the new pod. Matches ``vast service upgrade --timeout``'s
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
    click.echo("  the new pod has not taken over yet. 'vast service upgrade' reports "
               "the reason Kubernetes gave -- an image it cannot pull, a node it cannot "
               "schedule on, a crash-loop.")




@service.command('info')
@target_options
def info(namespace, context):
    """Which service is answering, which code it runs, and which lane it drives.

    Call this first when something behaves unexpectedly. A service loads robovast **once,
    at startup**, so after an edit a reachable service may still be running the old code;
    comparing ``revision`` with your tree is how you find that out.

    When ``revision`` is absent the check is unavailable and there is no substitute --
    ``version`` is the package semver, so it stays the same across every edit and reading
    it as a revision is a live trap.
    """
    try:
        with service_client(namespace, context, require_service=True) as (client, label):
            _echo_target(label)
            version = client.version()
    except Exception as e:  # noqa: BLE001
        handle_cli_exception(e)
        return

    click.echo(f"  version   {version.robovast_version}")
    click.echo(f"  revision  {version.code_revision or '(unavailable — cannot compare with your tree)'}")
    click.echo(f"  api       {version.api_version}")
    if version.backend:
        click.echo(f"  lane      {version.backend}")
    if version.kube_context:
        source = f" ({version.kube_context_source})" if version.kube_context_source else ""
        click.echo(f"  context   {version.kube_context}{source}")


@service.command('resources')
@target_options
def resources(namespace, context):
    """Does this lane have room, and is it reachable?

    Ask before a sweep. The numbers are backend-neutral: the local/cluster difference is
    resolved inside the service, so the same fields mean the same thing either way.

    ``pending`` is work the backend has accepted but is not executing, which is why it is
    counted apart from usage rather than folded into it -- counting queued work as *used*
    reported more cores in use than the cluster had.
    """
    try:
        with service_client(namespace, context, require_service=True) as (client, label):
            _echo_target(label)
            usage = client.resource_usage()
    except Exception as e:  # noqa: BLE001
        handle_cli_exception(e)
        return

    def _gib(value):
        return f"{value / (1024 ** 3):.1f} GiB"

    click.echo(f"  lane      {usage.backend}"
               f" ({'parallel runs' if usage.parallel_runs else 'one run at a time'})")
    click.echo(f"  cpu       {usage.cpu_used:.1f} / {usage.cpu_capacity:.1f} cores")
    click.echo(f"  memory    {_gib(usage.memory_used_bytes)} /"
               f" {_gib(usage.memory_capacity_bytes)}")
    click.echo(f"  runs      {usage.jobs_running} running, {usage.jobs_pending} pending")
