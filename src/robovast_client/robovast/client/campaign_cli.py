# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""``vast campaign`` -- act on a campaign, whichever lane it runs on.

Every verb here goes through the **robovast-service**, which drives the campaign in
process. That is why they are the client's: acting on a campaign is an HTTP verb and
nothing else -- no kubeconfig, no Kubernetes client, no Docker.

Not under ``vast exec cluster``, which would say something untrue about them.
``CreateCampaignRequest.backend`` is vestigial -- "one service runs one lane, chosen by
the serve command's --backend" -- so the lane belongs to the service and never to the
verb: ``stop`` against a local service stops a local campaign, and a ``cluster`` in the
path would name a choice the request cannot express. The campaign is what these act on,
so the campaign is what names them.

Starting one is not here. A campaign does not exist until it is created, so it cannot be
the address; the project's location can -- ``vast workspace run WORKSPACE [VAST]``. What
IS here is ``rerun``, which starts a campaign from a *past campaign's* record: the other
thing a campaign can be created from, named after its source exactly as ``workspace run``
is.
"""

import sys

import click

from robovast.client.errors import handle_cli_exception
from robovast.client.service_target import echo_target as _echo_target
from robovast.client.service_target import service_client, target_options
from robovast.client.tail import tail_chunks


@click.group()
def campaign():
    """Act on a campaign: list, watch, stop, read its log, re-run it.

    Every verb drives the robovast-service, so these work the same whether the campaign
    runs on a local Docker lane or in a cluster.

    To *start* a campaign from a project, use ``vast workspace run``.
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
            f"{len(live)} campaigns are running ({names}); pass CAMPAIGN to choose one.")
    return live[0].campaign_id


@campaign.command()
@click.argument('campaign', metavar='[CAMPAIGN]', required=False, default=None)
@target_options
def stop(campaign, namespace, context):
    """Ask a running campaign to stop gracefully (after the current batch).

    Goes through the robovast-service, which drives the campaign in-process: the
    loop ends once the in-flight batch finishes and the campaign is published as
    usual. A no-op if nothing is running.
    """
    try:
        with service_client(namespace, context) as (client, target):
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


@campaign.command('stop-job')
@click.argument('job_name')
@click.argument('campaign', metavar='[CAMPAIGN]', required=False, default=None)
@click.option('--reason', default=None, metavar='TEXT',
              help='Why you stopped it — stored with the run and shown in the results')
@target_options
def stop_job(job_name, campaign, reason, namespace, context):
    """Kill ONE running job; the rest of the campaign keeps going.

    For a job that is visibly wedged and will not exit on its own. This is not how a
    campaign is ended -- that is ``vast campaign stop``, which stops all of it.

    Only a *running* job can be stopped. The kill is permanent and recorded: the runs it
    cut short report ``status='killed'`` in the campaign data, with the reason, and count
    as neither passes nor failures.
    """
    try:
        with service_client(namespace, context) as (client, target):
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


@campaign.command()
@click.argument('campaign', metavar='[CAMPAIGN]', required=False, default=None)
@click.option('--follow', '-f', is_flag=True,
              help='Stream new output until the campaign finishes')
@target_options
def log(campaign, follow, namespace, context):
    """Print a campaign's unified infrastructure log.

    The same divider-separated stream the web UI and MCP show — the variation
    (config-generation), run (controller) and postprocessing phases in order, each
    under a ``===== PHASE =====`` divider.

    One reader, over HTTP, and no fallback to assembling the log from a campaign
    directory when no service answers -- that needs the core installed, takes a path
    where every other verb takes a campaign id, and is a second implementation of
    "read the log" that a client-only install could not reach anyway.
    """
    try:
        with service_client(namespace, context) as (client, target):
            _echo_target(target)
            campaign_id = campaign or _sole_running_campaign(client)
            if campaign_id is None:
                click.echo("No running campaign found; pass CAMPAIGN.")
                return
            tail_chunks(lambda o: client.get_campaign_logs(campaign_id, o),
                        lambda text: click.echo(text, nl=False), follow=follow)
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


#: How a verdict is shown. The symbols exist so a five-line report can be scanned at a glance;
#: the words stay because a symbol alone is not something anyone can act on or search for.
_VERDICT_MARKS = {
    "ok": ("ok", "green"),
    "upgradable": ("upgradable", "yellow"),
    "unknown": ("unknown", "yellow"),
    "blocked": ("BLOCKED", "red"),
}


def _report_rerunnable(client, label, campaign_id, *, exit_when_blocked):
    """Print the pre-flight for *campaign_id*; return True when it is re-runnable.

    Answers before a retrigger rather than after: config version, host/container protocol,
    recorded images, third-party plugins and asset providers -- all five at once, because they
    fail independently and fixing one to discover the next is the thing this replaces.

    ``unknown`` is not a failure. A campaign recorded before a given field existed cannot say
    what it used, and refusing it on that basis would refuse exactly the old campaigns worth
    re-running.
    """
    _echo_target(label)
    report = client.check_retrigger(campaign_id)

    click.echo(f"campaign: {report.campaign_id}")
    for name, axis in sorted(report.axes.items()):
        word, colour = _VERDICT_MARKS.get(axis.verdict, (axis.verdict, None))
        click.echo(f"  {name:<10} {click.style(word, fg=colour):<20} {axis.detail}")
    click.echo("")
    if report.runnable:
        click.echo(click.style("re-runnable", fg="green")
                   + f" -- vast campaign rerun {report.campaign_id}")
        return True
    click.echo(click.style(f"NOT re-runnable: {', '.join(report.blocking)}", fg="red"))
    if exit_when_blocked:
        sys.exit(1)
    return False


@campaign.command('rerun')
@click.argument('campaign_id')
@click.option('--check', 'check_only', is_flag=True,
              help='Report whether this campaign can be re-run, and launch nothing. '
                   'Costs nothing: it answers from what the campaign recorded. Exits '
                   'non-zero when an axis is genuinely blocked.')
@click.option('--force', is_flag=True,
              help='Launch even when the pre-flight reports a blocking axis.')
@click.option('--to-workspace', 'to_workspace', default='', metavar='NAME',
              help='Do not launch. Materialise the campaign as a workspace with its config '
                   'migrated as far as it could be and a marker at every decision left, to '
                   'finish by hand. For a config no ladder step can carry forward.')
@target_options
def rerun(campaign_id, check_only, force, to_workspace,  # pylint: disable=redefined-outer-name
          namespace, context):
    """Launch a NEW campaign from what CAMPAIGN_ID recorded. The source is not modified.

    Reuses the frozen config and the image the source recorded, so it runs the same code rather
    than today's. A config older than the current version is migrated into the staging copy;
    the archived one is left exactly as its author wrote it.

    The pre-flight runs first, because launching to discover the image is gone wastes the launch
    -- and its refusal names what is missing. ``--force`` proceeds anyway, which is worth having
    for an axis you have decided you understand.
    """
    try:
        with service_client(namespace, context) as (client, label):
            if check_only:
                _report_rerunnable(client, label, campaign_id, exit_when_blocked=True)
                return
            _echo_target(label)
            if to_workspace:
                _materialize_work_order(client, campaign_id, to_workspace)
                return
            report = client.check_retrigger(campaign_id)
            if not report.runnable and not force:
                for name in report.blocking:
                    click.echo(click.style(f"  {name}: ", fg="red")
                               + report.axes[name].detail, err=True)
                click.echo("", err=True)
                click.echo(f"refusing to re-run. Run 'vast campaign rerun --check "
                           f"{campaign_id}' for the full report, or --force to proceed anyway.",
                           err=True)
                sys.exit(1)
            for name, axis in sorted(report.axes.items()):
                if axis.verdict in ("upgradable", "unknown"):
                    click.echo(f"note: {name}: {axis.detail}", err=True)
            ref = client.retrigger_campaign(campaign_id)
    except Exception as e:  # noqa: BLE001
        handle_cli_exception(e)
        return

    click.echo(f"re-ran {campaign_id} as {ref.campaign_id}")
    if getattr(ref, "note", ""):
        click.echo(f"note: {ref.note}")
    click.echo(f"  next: vast campaign wait {ref.campaign_id}")


def _materialize_work_order(client, campaign_id: str, workspace_name: str):
    """Hand the campaign over as a workspace to finish by hand.

    Separate from the launch path because it is the opposite outcome: nothing is started, and what
    the caller gets is a file with work left in it. Printing the markers here rather than only in
    the service log is the point -- each one is a decision somebody has to make, and they are
    usually in different places in the file.
    """
    result = client.materialize_retrigger_workspace(campaign_id, workspace_name)
    click.echo(f"workspace '{result.workspace_id}' created from {campaign_id}")
    click.echo(f"  {result.config_path}: migrated as far as version {result.reached}")
    if result.capability:
        click.echo(f"  stopped at: {result.capability}")
    if not result.markers:
        click.echo(click.style("  no unresolved markers — validate and launch it normally",
                               fg="green"))
        return
    click.echo(click.style(f"  {len(result.markers)} decision(s) left:", fg="yellow"))
    for marker in result.markers:
        click.echo(f"    {marker.path}: {marker.reason}")
    click.echo("")
    click.echo("This will NOT validate until every marker is resolved, which is deliberate: a "
               "partly-migrated config that loaded would run a different experiment.")
    click.echo(f"  next: edit {result.config_path}, then 'vast configuration validate'")




@campaign.command()
@click.argument('campaign')
@click.option('--interval', default=10.0, show_default=True,
              help='Seconds between status polls.')
@click.option('--timeout', type=float, default=None,
              help='Give up after this many seconds (default: wait indefinitely).')
@target_options
def wait(campaign, interval, timeout, namespace, context):
    """Block until CAMPAIGN is over: exit 0 (finished), 1 (failed/stopped), 2 (stopped
    waiting: --timeout, or the service stopped answering), 3 (no phase), 4 (stalled --
    still running, but no longer being waited on), 5 (a running job's simulator reported
    something wrong -- likewise still running).

    The lane-agnostic wait: the service drives every campaign, so its phase *is* the
    campaign's whichever backend the runs execute on. Prints each phase change as it
    happens and exits when the campaign reaches a terminal one — which now means past
    postprocessing, not merely past the last run.

    Exists so a *caller* can wait without holding a request open, and is why the MCP
    offers no campaign-wait tool: an agent harness can background this command and be
    notified when it exits, hours or days later, where a blocking tool call would occupy
    the conversation for as long as the campaign ran — and still not outlive the session.
    The loop itself is :func:`~robovast.execution.campaign_wait.wait_for_campaign_status`,
    shared with every other surface that waits.

    **A stall ends the wait too** (exit 4), because a stalled campaign never reaches a
    terminal phase: it holds ``running`` for its whole life, so a waiter that stopped only
    on terminality would never return and nobody would be told. The verdict is
    :func:`~robovast.client.status.stall_report`'s, not a second opinion computed here.

    **An ``error``-level health finding ends it too** (exit 5), and earlier: a stall is only
    visible once a run is past its declared budget, and needs one to have been declared at
    all, while a simulator saying "sim time is not advancing" is true within a minute and
    needs no budget. Whatever a finding means is the simulator's business -- this reads one
    word, ``level``, and passes the rest through.

    Only a **new** stall or a **new** finding exits. A campaign already stalled when this
    command starts is not news -- whoever ran it has just been told -- and exiting on the
    first poll would make ``vast campaign wait`` unusable for exactly the state it reports: the message
    says to re-run it after diagnosing, and a fresh waiter would exit immediately, forever. So
    the first observation is recorded and only a rising edge stops the wait; for findings the
    edge is per ``check``, so one check firing repeatedly is one exit and not a stream.
    """
    from robovast.client.status import (HEALTH_NEXT_STEP, Phase, error_findings,
                                        finding_summary, is_terminal, stall_report)
    from robovast.execution.campaign_wait import wait_for_campaign_status
    from robovast.execution.poll_health import PollsStopped

    seen = {"stalled": None, "checks": None}
    fired: dict = {}

    def stop_when(current):
        # Both edges are taken every poll, whichever ends up firing: a baseline that moved only on
        # the branch that was reached would let the other one exit on a condition it inherited.
        stalled = stall_report(current).get("stalled") is True
        previous, seen["stalled"] = seen["stalled"], stalled
        findings = error_findings(current)
        baseline = seen["checks"]
        if baseline is None:
            # The baseline poll. Whatever a simulator is already complaining about is the state the
            # caller was just told to come back from, so it cannot be what sends them away again.
            seen["checks"] = {f.check for f in findings}
        else:
            # A finding first when both are true, matching `_campaign_next_step`: it names a fault
            # class ("sim time is not advancing") where a stall says only "nothing finished in
            # time".
            fresh = [f for f in findings if f.check not in baseline]
            if fresh:
                fired["finding"] = fresh[0]
                return True
        return stalled and previous is False

    try:
        with service_client(namespace, context) as (client, label):
            _echo_target(label)
            status = wait_for_campaign_status(
                campaign, client=client, interval=interval, timeout=timeout,
                feedback=click.echo, stop_when=stop_when)
    except TimeoutError as e:
        # Not a failure of the campaign, which is still running: the caller asked to stop
        # waiting. A distinct exit code keeps the two apart for a script branching on it.
        click.echo(str(e), err=True)
        raise SystemExit(2) from e
    except PollsStopped as e:
        # Same category -- the wait ended, the campaign did not -- so the same code, but
        # the message must not read as a campaign problem: nothing is known about the
        # campaign here, because nothing answered.
        click.echo(str(e), err=True)
        raise SystemExit(2) from e
    except Exception as e:  # noqa: BLE001
        handle_cli_exception(e)
        return
    if not is_terminal(status.phase):
        # stop_when fired: the campaign is alive, and something about it is worth stopping a
        # wait for. Distinct from every other exit here, all of which mean the campaign is over
        # or unreachable -- and the message has to say so, or "the waiter returned" reads as
        # "the run ended".
        finding = fired.get("finding")
        if finding is not None:
            click.echo(f"{campaign}: {finding_summary(finding)}", err=True)
            # Said before anything else about what to do: a waiter stopping must never read as
            # a run stopping, and this exit is reached by *reading* a run, with a fixed
            # read-only command the service chose. Nothing touched the job.
            click.echo(f"{campaign}: the run was NOT touched — this is what the run's own "
                       f"simulator reported about itself.", err=True)
        else:
            click.echo(f"{campaign}: {stall_report(status).get('stall_reason', 'no progress')}",
                       err=True)
        click.echo(
            f"{campaign}: the campaign is STILL RUNNING and nothing is waiting on it now. "
            f"When you are done diagnosing, background `vast campaign wait {campaign}` again, or "
            f"end it with stop_campaign.", err=True)
        if finding is not None:
            # A check the simulator says it did NOT run, reported here and only here in the wait:
            # this exit means one check fired, and a reader is entitled to know which others
            # reached no verdict before concluding that the rest of the run is fine.
            for note in status.health_skipped or []:
                click.echo(f"{campaign}: check did not run — {note}", err=True)
            # The stall message carries its ladder inside `stall_reason`; a finding has no such
            # sentence of its own. Deliberately NOT the stall's step, which reused to send a
            # reader off to ask what the job was doing -- the question this finding just answered.
            click.echo(f"{campaign}: next: {HEALTH_NEXT_STEP}", err=True)
            raise SystemExit(5)
        raise SystemExit(4)
    click.echo(f"{campaign}: {status.phase}")
    if status.phase == Phase.UNKNOWN:
        # `unknown` is terminal, so the wait ends -- but it does not mean the campaign
        # failed. The service has no phase for this id at all: either it is a typo, or the
        # campaign died before it ever wrote to the store. Exiting 1 made both read as "the
        # campaign ran and failed", sending the caller to look for a failure that never
        # happened. A distinct code, because 0/1/2 are taken and a script branches on it.
        click.echo(
            f"{campaign}: the service knows no phase for this campaign — check the id, "
            f"or see 'vast campaign log {campaign}' if it died before recording one.",
            err=True)
        raise SystemExit(3)
    if status.error:
        click.echo(f"{campaign}: {status.error}", err=True)
    if status.postprocessing_error:
        # A campaign whose runs passed but whose postprocessing failed still *finished*;
        # saying only "finished" here would send the caller looking for CSVs that a
        # successful exit code promised and nothing produced.
        click.echo(f"{campaign}: postprocessing failed: {status.postprocessing_error}",
                   err=True)
    raise SystemExit(0 if status.phase == Phase.FINISHED else 1)




@campaign.command('list')
@click.option('--limit', type=int, default=20, show_default=True,
              help='How many to show, newest first.')
@target_options
def list_cmd(limit, namespace, context):
    """List campaigns this service knows about, newest first.

    ``description`` is what tells two same-day ``<name>-<timestamp>`` ids apart, which is
    why the launch verbs ask for one.
    """
    try:
        from robovast.service.interface import \
            ListCampaignsRequest  # pylint: disable=import-outside-toplevel

        with service_client(namespace, context) as (client, label):
            _echo_target(label)
            listed = client.list_campaigns(ListCampaignsRequest(limit=limit)).campaigns
    except Exception as e:  # noqa: BLE001
        handle_cli_exception(e)
        return

    if not listed:
        click.echo("no campaigns")
        return
    width = max(len(c.campaign_id) for c in listed)
    for summary in listed:
        click.echo(f"  {summary.campaign_id:<{width}}  {summary.phase:<12} "
                   f"{summary.description}")


@campaign.command('status')
@click.argument('campaign', metavar='[CAMPAIGN]', required=False, default=None)
@target_options
def status_cmd(campaign, namespace, context):  # pylint: disable=redefined-outer-name
    """Print a campaign's phase and progress once, and exit.

    The single-read counterpart to ``vast campaign wait``: use this for a campaign you are
    not waiting on. Waiting is a separate verb because it can take days, and holding a
    request open for that is a different thing from asking once.
    """
    try:
        with service_client(namespace, context) as (client, label):
            _echo_target(label)
            campaign_id = campaign or _sole_running_campaign(client)
            if not campaign_id:
                raise ValueError("no campaign is running; pass CAMPAIGN.")
            status = client.get_status(campaign_id)
    except Exception as e:  # noqa: BLE001
        handle_cli_exception(e)
        return

    click.echo(f"  campaign  {campaign_id}")
    click.echo(f"  phase     {status.phase}")
    if getattr(status, "total_runs", 0):
        click.echo(f"  runs      {getattr(status, 'completed_runs', 0)}"
                   f" / {status.total_runs}")


@campaign.command('import')
@click.argument('archive', type=click.Path(exists=True, dir_okay=False))
@click.option('--force', is_flag=True, help='Replace a campaign of the same id already there.')
@click.option('--rebuild-store', is_flag=True,
              help='Reconstruct campaign.db from the results tree (the recovery for a corrupt one).')
@target_options
def import_cmd(archive, force, rebuild_store, namespace, context):
    """Take a campaign archive into the service, and postprocess it if it needs it.

    ARCHIVE is a ``.tar.gz`` on *this* machine -- one ``vast campaign download`` or ``vast
    share download`` produced, or a colleague sent. It is uploaded to the service and
    imported there, so the campaign lands where the web UI and every other client can see
    it; the file itself is never deleted, it is yours.

    Importing is more than extracting: listings and the web UI answer from ``campaign.db``,
    not from the results tree, so a campaign that is only unpacked is invisible. And when
    the archive is a **raw** one -- carrying no postprocessing record, which is what the
    share holds -- postprocessing is chained automatically, because a campaign without its metric tables
    is not one you can ask anything.

    Long-running, so it returns once the import is under way: the campaign appears
    immediately at phase ``importing``. Watch it with ``vast campaign wait <campaign-id>``,
    or in the campaign view.

    It creates a campaign, which is why it is here and not in ``vast results``: an upload
    and one HTTP call, needing nothing of the local half of the tool. It sat in the group
    that ships only with the full distribution, so the install most likely to be talking to
    a remote service was the one that could not import into it.
    """
    from pathlib import Path  # pylint: disable=import-outside-toplevel

    from robovast.client.progress import fmt_size  # pylint: disable=import-outside-toplevel
    from robovast.service.interface import \
        ImportCampaignRequest  # pylint: disable=import-outside-toplevel
    from robovast.service.project_push import \
        push_campaign_archive  # pylint: disable=import-outside-toplevel

    path = Path(archive)
    try:
        with service_client(namespace, context) as (client, label):
            _echo_target(label)
            click.echo(f"uploading {path.name} ({fmt_size(path.stat().st_size)}) ...")
            staged = push_campaign_archive(client, path)
            ref = client.import_campaign(ImportCampaignRequest(
                archive_path=staged, force=force, rebuild_store=rebuild_store))
    except Exception as e:  # noqa: BLE001
        handle_cli_exception(e)
        return

    click.echo(f"✓ importing {ref.campaign_id}")
    if ref.note:
        click.echo(f"  {ref.note}")
    click.echo(f"  next: vast campaign wait {ref.campaign_id}")


@campaign.command('postprocess')
@click.argument('campaign', metavar='[CAMPAIGN]', required=False, default=None)
@click.option('--force', '-f', is_flag=True,
              help='Bypass per-rosbag caches and reprocess all bags.')
@click.option('--skip', 'skip_plugins', multiple=True, metavar='PLUGIN',
              help='Skip a postprocessing plugin (repeatable), e.g. --skip rosbags_to_webm.')
@target_options
def postprocess_cmd(campaign, force, skip_plugins, namespace, context):
    """(Re)run analysis postprocessing for CAMPAIGN.

    The campaign is the address, and the service is the lane: the rosbag->CSV step runs
    wherever that campaign's runs ran -- in-cluster for a cluster campaign -- and the
    campaign's derived data is rebuilt. Mirrors the web "Retrigger postprocessing" action and the MCP
    ``run_postprocessing`` tool, so all three drive one implementation.

    This was ``vast results reprocess``, beside a ``vast results postprocess`` that did the
    same job in-process against a results directory on this machine. Two postprocessing
    paths is the same split ``vast exec local run`` was: one of them could only ever see a
    local campaign, and it was the one the documentation reached for first.

    **Dispatched, not awaited.** Postprocessing takes minutes to hours, so the campaign
    re-enters its ``postprocessing`` phase and this returns -- follow it with ``vast
    campaign wait CAMPAIGN``, exactly as after ``vast workspace run``. A second run is
    refused while one is in flight.
    """
    from robovast.service.interface import \
        RunPostprocessingRequest  # pylint: disable=import-outside-toplevel
    try:
        with service_client(namespace, context) as (client, label):
            _echo_target(label)
            campaign_id = campaign or _sole_running_campaign(client)
            if not campaign_id:
                raise ValueError("no campaign is running; pass CAMPAIGN.")
            res = client.run_postprocessing(RunPostprocessingRequest(
                campaign_id=campaign_id, force=force, skip=list(skip_plugins)))
    except Exception as e:  # noqa: BLE001
        handle_cli_exception(e)
        return
    if not res.ok:
        raise click.ClickException(res.message or "postprocessing failed")
    click.echo(f"✓ {res.message or 'postprocessing started'}")
    click.echo(f"  next: vast campaign wait {campaign_id}")


@campaign.command('delete')
@click.argument('campaign', metavar='CAMPAIGN')
@click.option('--yes', '-y', is_flag=True, help='Skip the confirmation prompt.')
@target_options
def delete_cmd(campaign, yes, namespace, context):
    """Permanently delete one CAMPAIGN wholesale.

    Removes the campaign's durable home -- its directory under the results root on a local
    service, or its object-store data (plus any leftover Kubernetes Jobs and the service's
    cache) on a cluster service. This is the full "forget this campaign" action; ``vast
    cluster store-cleanup`` only frees object-store buckets, and ``vast share remove`` only
    touches the external share, which this command leaves untouched.

    The service refuses a campaign that is still running -- stop it first. This is
    irreversible.
    """
    if not yes and not click.confirm(
            f"Permanently delete campaign '{campaign}'? This cannot be undone."):
        click.echo("Aborted.")
        return
    try:
        with service_client(namespace, context) as (client, label):
            _echo_target(label)
            res = client.delete_campaign(campaign)
    except Exception as e:  # noqa: BLE001
        handle_cli_exception(e)
        return
    if not res.ok:
        raise click.ClickException(res.message or "delete failed")
    click.echo(f"✓ {res.message or f'Deleted {campaign}'}")


@campaign.command('download')
@click.argument('campaigns', metavar='CAMPAIGN...', nargs=-1, required=True)
@click.option('--output', '-o', 'output', default=None, type=click.Path(file_okay=False),
              help='Directory to write the archives into [default: the current directory]')
@click.option('--force', '-f', is_flag=True,
              help='Overwrite an archive of the same name that is already here')
@target_options
def download_cmd(campaigns, output, force, namespace, context):
    """Download campaign archives from the service, one ``.tar.gz`` each.

    That is the whole command: it fetches ``<campaign-id>.tar.gz`` and stops. Nothing is
    extracted, no results directory is written into, and no state is kept about what you
    already have -- the archive is yours, to keep, copy, unpack, or hand back with ``vast
    campaign import``.

    The archive is the campaign as the service holds it, postprocessing and all. The
    share's raw, pre-postprocess snapshot is a different system with different
    credentials: ``vast share download``.

    Writes into the current directory unless ``-o`` says otherwise -- an archive is a
    file, not a results tree, so a results directory is the wrong home for it.

    One campaign that fails does not stop the others: each is reported on its own line and
    the exit summary counts what landed. A thin single-archive copy cannot do several,
    resume past a failure, or show progress on a multi-gigabyte transfer, so this is the
    one implementation.
    """
    import time  # pylint: disable=import-outside-toplevel
    from pathlib import Path  # pylint: disable=import-outside-toplevel

    from robovast.client.progress import (  # pylint: disable=import-outside-toplevel
        fmt_size, make_transfer_progress_callback)
    from robovast.service.project_push import \
        download_campaign_archive  # pylint: disable=import-outside-toplevel

    out_dir = Path(output) if output else Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)

    written = skipped = 0
    try:
        with service_client(namespace, context) as (client, label):
            click.echo(f"Downloading {len(campaigns)} campaign archive(s) from {label} ...")
            for campaign_id in campaigns:
                dest = out_dir / f"{campaign_id}.tar.gz"
                if dest.exists() and not force:
                    click.echo(f"  {dest.name}  already here, skipping "
                               "(use --force to re-download)")
                    skipped += 1
                    continue
                start = time.monotonic()
                try:
                    download_campaign_archive(
                        client, campaign_id, str(dest),
                        progress_callback=make_transfer_progress_callback(
                            campaign_id, start))
                # Ahead of the broad handler below, which would otherwise swallow click's
                # own control flow and report a usage error as an unexpected failure.
                except (click.UsageError, click.ClickException):  # pylint: disable=try-except-raise
                    raise
                except Exception as exc:  # noqa: BLE001
                    sys.stdout.write("\n")
                    handle_cli_exception(exc)
                    continue
                finally:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                click.echo(f"  {campaign_id}  ✓  {fmt_size(dest.stat().st_size)} "
                           f"in {time.monotonic() - start:.0f}s  ->  {dest}")
                written += 1
    # pylint: disable-next=try-except-raise
    except (click.UsageError, click.ClickException):
        raise
    except Exception as e:
        handle_cli_exception(e)
        return

    click.echo()
    parts = [f"✓ Downloaded {written} archive(s)"]
    if skipped:
        parts.append(f"{skipped} skipped")
    click.echo("  ".join(parts))
