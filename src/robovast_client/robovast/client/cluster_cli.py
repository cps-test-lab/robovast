# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""``vast exec cluster`` -- the group shell, and the verbs that only drive a service.

Every cluster campaign is driven by the **robovast-service**, which runs it in-process and
creates the scenario Jobs itself. Launching one is therefore four HTTP verbs against that
service and nothing else: no kubeconfig, no Kubernetes client, no Docker. That is why
``run``, ``stop``, ``stop-job``, ``log`` and ``download-cleanup`` live in the client -- the
audience that *drives* a cluster is not the audience that *owns* one, and making them
install ``robovast-cluster`` (which depends on the full core) to start a campaign meant
installing 290 MB to send an HTTP POST.

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
import sys
import time

import click

from robovast.client.errors import handle_cli_exception
from robovast.client.lazy_group import LazyPluginGroup
from robovast.client.project_config import get_project_config
from robovast.client.service_target import echo_target as _echo_target
from robovast.client.service_target import service_client, target_options

#: Entry-point group for subcommands that attach to ``vast exec cluster``.
CLUSTER_PLUGIN_GROUP = "robovast.cluster_plugins"


@click.group(cls=LazyPluginGroup, plugin_group=CLUSTER_PLUGIN_GROUP)
def cluster():
    """Execute scenarios on a Kubernetes cluster.

    Run scenario configurations as Kubernetes jobs with bind mounts
    for configuration and output data.

    The command that executes a *campaign* (``run``) needs a project
    (``vast init``, or ``-V <file>``) to know which ``.vast`` to run. The ones that
    act on the *cluster* — ``setup``, ``cleanup``, ``monitor``, ``stop``,
    ``run-cleanup``, ``download-cleanup`` — do not: they read what they need from the
    cluster itself and work from any directory.

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


def _confirm_overwrite(name, workspace_id):
    """Ask before a launch overwrites the workspace the project already has.

    Same shape as ``vast workspace init``'s collision prompt: the default is yes, so
    Enter is enough for the common case of re-launching the project you just edited.
    Off a TTY there is nobody to ask and blocking would hang a scripted launch, so it
    proceeds with that default — announced, never silent.

    It says *what* the overwrite can disturb because nothing else can: a campaign is
    workspace-independent by design (``_execution/launch.yaml`` deliberately does not
    record which workspace it came from), so neither this command nor the service can
    tell whether one is still reading these files.
    """
    question = (f"workspace {name!r} ({workspace_id}) already holds this project — "
                "overwrite its files (a campaign still running from them would see "
                "the change)?")
    if not sys.stdin.isatty():
        click.echo(f"note: {question} yes (not a terminal)")
        return True
    return click.confirm(question, default=True)


@cluster.command()
@click.option('--config', '-c', default=None,
              help='Run only configurations matching this name or glob pattern (e.g. hall*)')
@click.option('--runs', '-r', type=int, default=None,
              help='Override execution.runs (default: the value in the .vast).')
@click.option('--log-tree', '-t', is_flag=True,
              help='Log scenario execution live tree')
@target_options
@click.option('--wait-and-download', 'wait_and_download', is_flag=True,
              help='Block until the campaign finishes and its results are uploaded, '
                   'then download them into the project results directory — making a '
                   'cluster run as transparent as a local run.')
@click.option('--poll-interval', type=float, default=5.0, show_default=True,
              help='Seconds between status polls when --wait-and-download is set.')
@click.option('--campaign-name', default=None,
              help='Override the campaign name; the id becomes <name>-<timestamp>.')
@click.option('--upload-to-share', 'upload_to_share', is_flag=True,
              help='Stream a raw (pre-postprocess) archive to the configured share '
                   'when the campaign finishes.')
@click.option('--description', default=None, metavar='TEXT',
              help='One line saying what this run is for. It is what tells two '
                   'same-day <name>-<timestamp> campaigns apart in the monitor and '
                   'the web UI.')
@click.option('--workspace', 'workspace_name', default=None, metavar='NAME',
              help="Workspace to push the project into (default: the .vast's "
                   'directory name). Reused when it already exists.')
@click.option('--image-project', 'image_project', default=None, metavar='PROJECT',
              help='Registry/namespace to take the RoboVAST images from for this run '
                   '(e.g. freeedlabs), overriding ROBOVAST_PROJECT. Affects only images '
                   'RoboVAST publishes — a container image your .vast names is run as '
                   'written. Per campaign: no cluster redeploy.')
@click.option('--image-project-tag', 'image_project_tag', default=None, metavar='TAG',
              help='Tag to take those images at (default: ROBOVAST_PROJECT_TAG, else '
                   'latest).')
@click.option('--allow-opaque-image', is_flag=True,
              help='Launch even though a container names an image that declares no '
                   "provenance:. Refused by default because nothing in the results "
                   'could then say what ran; the exemption is recorded on the campaign.')
def run(config, runs, log_tree, namespace, context, wait_and_download,
        allow_opaque_image,
        poll_interval, campaign_name, upload_to_share,
        description, workspace_name, image_project,
        image_project_tag):  # pylint: disable=function-redefined,redefined-outer-name
    """Execute a campaign (batch or search) on a Kubernetes cluster.

    \b
    There is no ``--campaign-id`` here: the service names the campaign and
    ``CreateCampaignRequest`` carries no id, so nothing could honour one. The id it
    chose is returned by the launch. ``vast exec local run`` drives the controller
    directly and does take one.

    Runs through the robovast-service, which drives the campaign in-process and
    creates the per-batch scenario Jobs. The service is the one answering on the
    conventional local port, or the deployed one ``vast login`` recorded — either
    way this needs **no flags**. By default the command is fire-and-forget: it returns
    once the campaign is launched. Track it with ``vast wait <campaign-id>``, or the
    web UI (``vast exec cluster monitor`` needs ``robovast-cluster``).

    Pass ``--wait-and-download`` to instead block until the campaign finishes and
    its results have been uploaded, then download them into the project results
    directory automatically — one command, results on local disk, like a local run.

    Use --config to run only matching configurations (batch campaigns).

    Names a project with ``vast init``, or directly: ``vast -V my.vast exec cluster
    run``. The project is pushed into a workspace named after its directory, which is
    **reused** on later launches (overwritten, after asking) rather than accumulating
    one workspace per run — ``--workspace`` picks a different name.
    """
    try:
        from robovast.execution.campaign_wait import \
            wait_for_campaign_outcome  # pylint: disable=import-outside-toplevel
        from robovast.service.interface import \
            DESCRIPTION_MAX_LEN  # pylint: disable=import-outside-toplevel
        from robovast.service.project_push import (  # pylint: disable=import-outside-toplevel
            download_campaign_via_service, run_project_via_service)

        # Checked here rather than left to the request model: this says what to do
        # instead of surfacing a pydantic validation string, and it refuses before the
        # project is pushed.
        if description and len(description) > DESCRIPTION_MAX_LEN:
            raise click.ClickException(
                f"--description is {len(description)} characters; the limit is "
                f"{DESCRIPTION_MAX_LEN} — shorten it to one line.")

        project = get_project_config()
        with service_client(namespace, context,
                            require_service=True) as (client, target):
            _echo_target(target)
            cid = run_project_via_service(
                client, project.config_path, config_filter=config or "",
                # 0, not 1: the service reads a non-positive count as "use the .vast's
                # execution.runs", and a substitute for "unset" would shrink the
                # campaign without failing anything.
                runs=runs or 0, feedback=click.echo, upload_to_share=upload_to_share,
                campaign_name=campaign_name or "", description=description or "",
                workspace_name=workspace_name or "", on_exists=_confirm_overwrite,
                allow_opaque_image=allow_opaque_image,
                # The flag beats ROBOVAST_PROJECT; unset here means "whatever the .env
                # said", which project_push reads. Resolved client-side into the request
                # because the images are resolved *service*-side — a client that could
                # only set its own env var could not reach them at all.
                image_project=image_project, image_project_tag=image_project_tag)
            if not wait_and_download:
                click.echo(f"Launched cluster campaign '{cid}' via robovast-service. "
                           f"Track it with 'vast wait {cid}' or the web UI.")
                return

            click.echo(f"Launched cluster campaign '{cid}'. Waiting for it to finish...")
            outcome = wait_for_campaign_outcome(
                cid, client=client, interval=poll_interval, feedback=click.echo)
            if outcome == "failed":
                raise click.ClickException(
                    f"Campaign '{cid}' failed. Its status carries the failure reason: "
                    f"see 'vast exec cluster log {cid}' or the web UI.")

            click.echo(f"Campaign '{cid}' finished. Downloading results...")
            # The service streams the campaign from the object store — no external
            # share needed for delivery.
            download_campaign_via_service(client, cid, os.getcwd(), feedback=click.echo)
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
                offset = 0
                while True:
                    chunk = client.get_campaign_logs(campaign_id, offset)
                    if chunk.text:
                        click.echo(chunk.text, nl=False)
                        offset = chunk.next_offset
                    if chunk.eof or not follow:
                        break
                    time.sleep(1.5)
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
