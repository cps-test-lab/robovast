# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""``vast exec cluster`` — running and maintaining campaigns on Kubernetes.

Lives in the cluster package, not beside ``vast exec local``, because everything here
needs a cluster: a kubeconfig, an API server, or a deployed robovast-service to talk to.
Keeping it in the shared CLI module meant the core could not be installed without the
Kubernetes client, and every ``vast`` invocation paid to import it.

It attaches to ``vast exec`` through the ``robovast.exec_plugins`` entry-point group, and
loads only when someone actually types ``vast exec cluster`` -- see the group class in
``execution_utils/cli.py``. So ``vast exec --help`` still lists it without importing any
of it.
"""

import datetime
import logging
import os
import sys
import time

import click

from robovast.client.errors import handle_cli_exception
from robovast.client.project_config import get_project_config, get_vast_file_override
from robovast.client.service_target import detected_service_url
from robovast.client.service_target import echo_target as _echo_target
from robovast.client.service_target import service_client, target_options
from robovast.client.status import Status, stall_report

logger = logging.getLogger(__name__)


@click.group()
def cluster():
    """Execute scenarios on a Kubernetes cluster.

    Run scenario configurations as Kubernetes jobs with bind mounts
    for configuration and output data.

    The command that executes a *campaign* (``run``) needs a project
    (``vast init``, or ``-V <file>``) to know which ``.vast`` to run. The ones that
    act on the *cluster* — ``setup``, ``cleanup``, ``monitor``, ``stop``,
    ``run-cleanup``, ``download-cleanup`` — do not: they read what they need from the
    cluster itself and work from any directory.
    """


def _sole_running_campaign(client):
    """The one running campaign's id, or None; errors if several are running.

    Campaigns run in parallel now, so a bare ``stop`` is only unambiguous when
    exactly one is live.
    """
    from robovast.execution.control_server import is_running
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
def run(config, runs, log_tree, namespace, context, wait_and_download,
        poll_interval, campaign_name, upload_to_share,
        description, workspace_name):  # pylint: disable=function-redefined,redefined-outer-name
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
    once the campaign is launched. Track it with 'vast exec cluster monitor'.

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
        from robovast.execution.execution_utils.cluster_run import \
            wait_for_cluster_campaign  # pylint: disable=import-outside-toplevel
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
                workspace_name=workspace_name or "", on_exists=_confirm_overwrite)
            if not wait_and_download:
                click.echo(f"Launched cluster campaign '{cid}' via robovast-service. "
                           "Track it with 'vast exec cluster monitor' or the service.")
                return

            click.echo(f"Launched cluster campaign '{cid}'. Waiting for it to finish...")
            outcome = wait_for_cluster_campaign(
                cid, client=client, interval=poll_interval, feedback=click.echo)
            if outcome == "failed":
                raise click.ClickException(
                    f"Campaign '{cid}' failed. Inspect with 'vast exec cluster "
                    "monitor' (the failure reason is on its status).")

            click.echo(f"Campaign '{cid}' finished. Downloading results...")
            # The service streams the campaign from the object store — no external
            # share needed for delivery.
            download_campaign_via_service(client, cid, os.getcwd(), feedback=click.echo)
    except (click.UsageError, click.ClickException):
        raise
    except Exception as e:
        handle_cli_exception(e)


def _progress_bar(done, total, width=20):
    """Return ``(bar, pct)`` — the ``█``/``░`` progress bar used across the monitor."""
    frac = max(0.0, min(1.0, done / total)) if total and total > 0 else 0.0
    filled = int(width * frac)
    return "█" * filled + "░" * (width - filled), 100.0 * frac


def _fmt_size(n):
    """Format a byte count as MiB (matches the upload progress display)."""
    return f"{n / 1024 / 1024:.1f} MiB"


def _fmt_duration(seconds):
    """Format an elapsed time with an adaptive unit ("42s", "7m 12s", "1h 04m")."""
    total = int(max(0, seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60:02d}s"
    return f"{total // 3600}h {(total % 3600) // 60:02d}m"


def _fmt_rate(bps):
    """Format a transfer rate (bytes/s) with an adaptive unit."""
    if bps >= 1024 * 1024:
        return f"{bps / 1024 / 1024:.1f} MiB/s"
    if bps >= 1024:
        return f"{bps / 1024:.1f} KiB/s"
    return f"{bps:.0f} B/s"


def _monitor_via_service(namespace, kube_context, interval, once):
    """Monitor campaigns through the robovast-service.

    The service drives every campaign in-process, so its ``get_status`` *is* the
    controller's live state — no controller pod to find and no ``port-forward`` to
    open (both are gone). Handles **multiple concurrent campaigns**: each live one
    is its own block, driven by that campaign's ``phase`` — the authoritative
    "done" signal, which is what keeps the monitor from exiting in the gap between
    search generations (when live Jobs momentarily drop to zero). Campaigns started
    while monitoring are picked up on the next tick.

    Returns ``True`` if it handled the monitoring, ``False`` if no service is
    configured so the caller can fall back to the Kubernetes-only view.
    """
    from robovast.service.client import RobovastClient
    from robovast.service.interface import ListCampaignsRequest

    url = detected_service_url()
    if not url:
        logging.debug("No robovast-service detected; falling back to K8s view.")
        return False
    client = RobovastClient(url)

    def _live():
        """(campaign_id, phase) for everything the service is tracking."""
        try:
            resp = client.list_campaigns(ListCampaignsRequest(limit=100))
        except Exception:  # pylint: disable=broad-except
            logging.debug("Could not list campaigns from the service.")
            return None
        return [(c.campaign_id, c.phase) for c in resp.campaigns]

    campaigns = _live()
    if campaigns is None:
        return False
    if not campaigns:
        click.echo("No campaigns known to the robovast-service.")
        return True

    cursor_up, clear_line = "\033[A", "\033[2K"
    prev = [0]

    def _live_counts(campaign_id):
        """Current batch's live job counts (running/pending) for this campaign.

        Read from the service so it works in every deployment mode — including a
        remote/in-cluster service the CLI host has no Kubernetes access to.
        """
        if not campaign_id:
            return {}
        try:
            counts = client.list_jobs(campaign_id).counts
        except Exception:  # pylint: disable=broad-except
            return {}
        return {"running": counts.running, "pending": counts.pending,
                "waiting": counts.waiting, "blocked": counts.blocked}

    def _campaign_lines(status):
        c = _live_counts(status.get("campaign_id"))
        runs = status.get("runs") or {}
        phase_label = status.get("phase", "?")
        if status.get("stage"):
            phase_label += f" / {status['stage']}"
        if status.get("phase") == "uploading" and status.get("share_provider"):
            phase_label += f" via {status['share_provider']}"
        lines = [f"Campaign {status.get('campaign_id', '?')}  [{phase_label}]"]
        line2 = f"  Batch {status.get('batch', 0)} (done {status.get('batches_done', 0)})"
        if status.get("best_objective") is not None:
            line2 += f"   best={status['best_objective']:.4g}"
        lines.append(line2)
        if status.get("budget"):
            lines.append("  Budget: " + " | ".join(
                f"{b['label']} {b.get('current')}/{b.get('limit')}" for b in status["budget"]))
        completed, total = runs.get('completed', 0), runs.get('total', 0)
        bar_str, pct = _progress_bar(completed, total)
        run_line = f"  Runs (this batch): [{bar_str}] {pct:5.1f}%  {completed}/{total}"
        if c:
            run_line += f"   Running: {c.get('running', 0)}  Pending: {c.get('pending', 0)}"
            if c.get("waiting"):
                # Queued for cluster capacity — normal, so no reason is printed; it is
                # on each job's detail (`list_campaign_jobs`) when one is needed.
                run_line += f"  Waiting: {c['waiting']}"
            if c.get("blocked"):
                # Jobs that cannot start (e.g. ImagePullBackOff). The reason rides on
                # each job's detail; the campaign fails with it after a grace window.
                run_line += f"  Blocked: {c['blocked']}"
        lines.append(run_line)
        # How long since a run last completed, and the stall verdict when the campaign
        # declared a per-run budget. A campaign spends its whole life in one `running`
        # phase, so without this a wedged run and a slow one are the same picture.
        stall = stall_report(Status.model_validate(status))
        if stall.get("progress_age_s") is not None and (completed or total):
            age_line = f"  Last run completed: {_fmt_duration(stall['progress_age_s'])} ago"
            if stall.get("stalled"):
                age_line += "   *** STALLED ***"
            elif stall.get("stalled") is None:
                # Tri-state: no declared execution.timeout, so silence here would read
                # as "fine". Say the verdict is unavailable instead.
                age_line += "   (no execution.timeout — stall unjudged)"
            lines.append(age_line)
        if stall.get("stall_reason"):
            lines.append(f"  Stalled: {stall['stall_reason']}")
        up = (status.get("extra") or {}).get("upload")
        if status.get("phase") == "uploading" and up:
            u_bar, u_pct = _progress_bar(up.get("sent", 0), up.get("total", 0))
            up_line = (f"  Upload: [{u_bar}] {u_pct:5.1f}%  "
                       f"{_fmt_size(up.get('sent', 0))}/{_fmt_size(up.get('total', 0))}")
            if up.get("rate") is not None:
                up_line += f"   {_fmt_rate(up['rate'])}"
            lines.append(up_line)
        if status.get("stop"):
            lines.append(f"  Stop: {status['stop'].get('reason', '')}")
        if status.get("error"):
            # Indent the (possibly multi-line) failure reason under the campaign.
            first, *rest = str(status["error"]).splitlines()
            lines.append(f"  Error: {first}")
            lines.extend(f"         {ln}" for ln in rest)
        return lines

    def _render(blocks):
        lines = [line for block in blocks for line in block]
        for _ in range(prev[0]):
            sys.stdout.write(cursor_up)
        for line in lines:
            sys.stdout.write("\r" + clear_line + line + "\n")
        for _ in range(len(lines), prev[0]):
            sys.stdout.write("\r" + clear_line + "\n")
        prev[0] = len(lines)
        sys.stdout.flush()

    def _blocks_for(ids):
        """Render blocks for *ids*, and report which have reached a terminal phase."""
        from robovast.execution.control_server import is_terminal
        blocks, finished = [], set()
        for cid in ids:
            try:
                # `Status` is a pydantic model; _campaign_lines reads it as a dict.
                status = client.get_status(cid).model_dump()
            except Exception:  # pylint: disable=broad-except
                blocks.append([f"Campaign {cid}  [status unavailable]"])
                continue
            blocks.append(_campaign_lines(status))
            if is_terminal(status.get("phase")):
                finished.add(cid)
        return blocks, finished

    try:
        if once:
            blocks, _ = _blocks_for([cid for cid, _ph in campaigns])
            _render(blocks)
            return True

        click.echo(f"Monitoring {len(campaigns)} campaign(s) (press Ctrl+C to stop)...")
        sys.stdout.write("\n")
        sys.stdout.flush()
        while True:
            current = _live()
            if current is None:
                return False
            ids = [cid for cid, _ph in current]
            blocks, finished = _blocks_for(ids)
            _render(blocks)
            if ids and finished >= set(ids):
                click.echo("\nAll campaigns finished.")
                return True
            time.sleep(interval)
    except Exception:  # pylint: disable=broad-except
        logging.debug("Service monitor failed; falling back to K8s view.")
        return False


@cluster.command()
@click.option('--interval', '-i', type=float, default=2.0, show_default=True,
              help='Polling interval in seconds')
@click.option('--once', is_flag=True,
              help='Print job status once and exit')
@click.option('--context', '-x', 'kube_context', default=None,
              help='Kubernetes context to use (default: active context in kubeconfig)')
@click.option('--namespace', '-n', default='default', show_default=True,
              help='Kubernetes namespace the scenario Jobs run in.')
def monitor(interval, once, kube_context, namespace):
    """Monitor scenario execution jobs on the cluster.

    Displays progress per run: how many jobs have finished (completed or failed),
    how many are running, and how many are pending for each run.

    By default, monitors only the contexts referenced in the .vast config file.
    Falls back to the active kubeconfig context when no per-cluster config is
    defined. Use --context to restrict monitoring to a single cluster.
    Only contexts with active or past jobs are shown.

    This is intended for monitoring jobs created by
    ``vast execution cluster run``.
    """
    # Deferred: these reach the Kubernetes client, and this module is a CLI
    # plugin `load_plugins()` imports on every `vast` invocation -- at module
    # level they made `vast login` and `vast wait` pay for the cluster stack.
    from .cluster_context import (  # pylint: disable=import-outside-toplevel
        get_active_kube_context, get_config_context_names)
    try:
        cursor_up = "\033[A"
        clear_line = "\033[2K"
        bar_width = 20
        pct_width = 7

        # Build list of (label, kube_context_name) to monitor
        if not kube_context:
            # Use contexts referenced in the .vast config file
            # --vast-file, else the project's .vast if run inside one; monitoring a
            # cluster works without either (it then watches the active context).
            from robovast.client.project_config import \
                resolve_vast_file  # pylint: disable=import-outside-toplevel
            config_path = resolve_vast_file()

            config_names = get_config_context_names(config_path) if config_path else set()
            if config_names:
                contexts_to_monitor = sorted((n, n) for n in config_names)
            else:
                # No per-cluster config — fall back to active context
                active = get_active_kube_context()
                contexts_to_monitor = [(active or "(active)", active)]
        else:
            contexts_to_monitor = [(kube_context, kube_context)]

        multi = len(contexts_to_monitor) > 1

        # Prefer the robovast-service (single-context campaigns): it drives the
        # campaigns, so its status reports loop phase/batch/run progress and is
        # authoritative for "done" — the monitor never exits in the gap between
        # search generations. Falls through to the Kubernetes-only view below when
        # no service is configured (multi-cluster, or partial setups).
        if not multi:
            if _monitor_via_service(namespace, contexts_to_monitor[0][1], interval, once):
                return

        # Per-context state (keyed by kube_context_name)
        initial_total: dict[str, dict] = {}        # ctx -> {campaign: total}
        max_ok: dict[str, dict] = {}               # ctx -> {campaign: max_ok}
        max_fail: dict[str, dict] = {}             # ctx -> {campaign: max_fail}
        last_per_run: dict[str, dict] = {}         # ctx -> last known per_run
        run_first_finished: dict[str, dict] = {}   # ctx -> {campaign: (timestamp, finished_count)}
        all_jobs_seen: dict[str, dict] = {}        # ctx -> {campaign: bool} — True once all jobs visible
        prev_line_count = [0]

        def _build_run_lines(label, ctx, per_run):
            """Return (lines, all_done) for a single context."""
            ctx_initial = initial_total.setdefault(ctx, {})
            ctx_ok = max_ok.setdefault(ctx, {})
            ctx_fail = max_fail.setdefault(ctx, {})
            ctx_first = run_first_finished.setdefault(ctx, {})
            ctx_all_seen = all_jobs_seen.setdefault(ctx, {})

            all_campaigns = sorted(set(ctx_initial.keys()) | set(per_run.keys()))
            lines = []
            all_done = True
            indent = "  " if multi else ""
            now = time.time()

            for campaign in all_campaigns:
                c = per_run.get(campaign, {"completed": 0, "failed": 0, "running": 0, "pending": 0,
                                           "total_job_num": None})
                current_total = c["completed"] + c["failed"] + c["running"] + c["pending"]
                if campaign not in ctx_initial:
                    ctx_initial[campaign] = current_total
                # Prefer annotation-based total so the monitor shows the full run size
                # even while many jobs are still pending / not yet visible in the API.
                annotated_total = c.get("total_job_num")
                total = annotated_total if annotated_total else ctx_initial[campaign]
                ctx_ok[campaign] = max(ctx_ok.get(campaign, 0), c["completed"])
                ctx_fail[campaign] = max(ctx_fail.get(campaign, 0), c["failed"])
                still_in_cluster = c["running"] + c["pending"]
                # Once all jobs have been seen in the cluster at least once, it's safe
                # to infer finished count from total - still_in_cluster (which handles
                # TTL-deleted Job objects). Before that point, jobs are still being
                # submitted and still_in_cluster underestimates, causing finished to be
                # wildly overestimated and ok to appear inflated.
                if current_total >= total:
                    ctx_all_seen[campaign] = True
                if ctx_all_seen.get(campaign) or annotated_total:
                    finished = total - still_in_cluster if total > 0 else 0
                else:
                    finished = c["completed"] + c["failed"]
                if still_in_cluster > 0:
                    all_done = False
                ok = ctx_ok[campaign]
                fail = ctx_fail[campaign]
                remainder = finished - ok - fail
                if remainder > 0:
                    ok += remainder
                pct = 100.0 * finished / total if total > 0 else 100.0
                filled = int(bar_width * finished / total) if total > 0 else bar_width
                progress_bar = "█" * filled + "░" * (bar_width - filled)
                pct_str = f"{pct:.1f}%".rjust(pct_width)

                # Track first observed completion for this run (to compute rate/ETA)
                if finished > 0 and campaign not in ctx_first:
                    ctx_first[campaign] = (now, finished)

                # Compute rate (jobs/min) and ETA
                rate_str = ""
                eta_str = ""
                if campaign in ctx_first and still_in_cluster > 0:
                    first_ts, first_finished = ctx_first[campaign]
                    elapsed = now - first_ts
                    jobs_since = finished - first_finished
                    if elapsed >= 10 and jobs_since > 0:
                        rate_per_min = jobs_since / (elapsed / 60.0)
                        rate_str = f"  {rate_per_min:.1f} jobs/min"
                        remaining = total - finished
                        if remaining > 0 and rate_per_min > 0:
                            eta_secs = remaining / (rate_per_min / 60.0)
                            eta_dt = datetime.datetime.fromtimestamp(now + eta_secs)
                            eta_str = f"  ETA ~{eta_dt.strftime('%H:%M')}"

                lines.append(
                    f"{indent}{campaign}  [{progress_bar}]  {pct_str}  "
                    f"{finished}/{total}  ({ok} ok, {fail} fail)  "
                    f"Running: {c['running']}  Pending: {c['pending']}"
                    f"{rate_str}{eta_str}"
                )
            if not lines:
                lines.append(f"{indent}No scenario run jobs found.")
            return lines, all_done

        def _print_status_lines():
            from .cluster_execution import \
                get_cluster_job_counts_per_campaign  # pylint: disable=import-outside-toplevel
            all_lines = []
            everything_done = True
            for label, ctx in contexts_to_monitor:
                unreachable = False
                try:
                    # Suppress urllib3 retry warnings for unreachable contexts — this
                    # display reports reachability itself, one line below.
                    from .kube_client import quiet_urllib3_retries
                    with quiet_urllib3_retries():
                        per_run = get_cluster_job_counts_per_campaign(namespace, context=ctx)
                except Exception as exc:
                    # Keep displaying even if one context is unreachable
                    per_run = {}
                    unreachable = True
                    logging.debug(f"Could not query context {ctx!r}: {exc}")
                # Use last known data when unreachable so bars stay meaningful
                if unreachable and ctx in last_per_run:
                    per_run = last_per_run[ctx]
                elif not unreachable:
                    last_per_run[ctx] = per_run
                # Skip contexts that have no jobs at all (and never had any)
                if not per_run and ctx not in initial_total:
                    if unreachable:
                        indent = "  " if multi else ""
                        if multi:
                            all_lines.append(f"[{label}]")
                        all_lines.append(f"{indent}(unreachable)")
                        everything_done = False
                    continue
                if multi:
                    ctx_label_str = f"[{label}]" + (" (unreachable)" if unreachable else "")
                    all_lines.append(ctx_label_str)
                elif unreachable:
                    all_lines.append("(unreachable - showing last known state)")
                run_lines, done = _build_run_lines(label, ctx, per_run)
                all_lines.extend(run_lines)
                if not done:
                    everything_done = False

            # Erase previous output and redraw
            for _ in range(prev_line_count[0]):
                sys.stdout.write(cursor_up)
            for line in all_lines:
                sys.stdout.write("\r" + clear_line + line + "\n")
            for _ in range(len(all_lines), prev_line_count[0]):
                sys.stdout.write("\r" + clear_line + "\n")
            prev_line_count[0] = len(all_lines)
            sys.stdout.flush()
            return everything_done

        if once:
            _print_status_lines()
            return

        ctx_label = "configured contexts" if multi else f"context '{contexts_to_monitor[0][0]}'"
        click.echo(f"Monitoring scenario run jobs on {ctx_label} (press Ctrl+C to stop)...")
        sys.stdout.write("\n")
        sys.stdout.flush()

        while True:
            all_done = _print_status_lines()
            if all_done:
                sys.stdout.write("\n")
                sys.stdout.flush()
                click.echo("All jobs finished.")
                break
            time.sleep(interval)

    except KeyboardInterrupt:
        sys.stdout.write("\n")
        sys.stdout.flush()
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

    Only a *running* job can be stopped (``vast exec cluster jobs`` lists them with their
    status). The kill is permanent and recorded: the runs it cut short report
    ``status='killed'`` in the campaign data, with the reason, and count as neither passes
    nor failures.
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
    otherwise reads the campaign from disk (the local project's results dir, or an
    absolute campaign path).
    """
    try:
        from robovast.service.client import HTTPTransport
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

            # No service reachable: read the campaign directory directly.
            from robovast.common.campaign_logs import assemble_log_from_dir
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
    except (click.UsageError, click.ClickException):
        raise
    except Exception as e:
        handle_cli_exception(e)


@cluster.command()
@click.option('--list', 'list_configs', is_flag=True,
              help='List available cluster configuration plugins')
@click.option('--namespace', '-n', default='default', show_default=True,
              help='Kubernetes namespace for execution (used by cluster run)')
@click.option('--option', '-o', 'options', multiple=True,
              help='Cluster-specific option in key=value format (can be used multiple times)')
@click.option('--force', '-f', is_flag=True,
              help='Force re-setup even if cluster is already set up')
@click.option('--context', '-x', 'kube_context', default=None,
              help='Kubernetes context to use (default: active context in kubeconfig)')
@click.option('--ingress-host', default='', metavar='HOST',
              help='Publish the service at this hostname, so users reach it in a '
                   'browser without kubectl. Needs TLS (see --issuer/--tls-secret) '
                   'and an access token; both are refused otherwise.')
@click.option('--ingress-class', default='', metavar='NAME',
              help='IngressClass to use (e.g. nginx). Default: the cluster default.')
@click.option('--issuer', default='', metavar='NAME',
              help='cert-manager ClusterIssuer to obtain the certificate from. '
                   'tools/setup_ingress_tls.py creates one.')
@click.option('--tls-secret', default='', metavar='NAME',
              help='Existing TLS Secret to serve, instead of a cert-manager issuer.')
@click.option('--insecure-http', is_flag=True,
              help='Publish over plain HTTP. The shared token then crosses the '
                   'network in clear text; only for a trusted network.')
@click.option('--rotate-token', is_flag=True,
              help='Issue a new access token, logging everyone out. Without this an '
                   'already-deployed token is preserved across re-runs.')
@click.option('--registry-storage-class', default='', metavar='NAME',
              help='Back the built-in container registry with a PVC from this '
                   'StorageClass instead of a hostPath. Preferred where the cluster can '
                   'provision volumes; stock RKE2 cannot, which is why hostPath is the '
                   'default.')
@click.option('--registry-storage-path', default='', metavar='PATH',
              help='Host directory backing the registry when using hostPath '
                   '(default: /var/lib/robovast-registry).')
@click.option('--registry-node', default='', metavar='NODE',
              help='Pin the service pod to this node. Needed with hostPath storage on a '
                   'multi-node cluster: the registry blobs live on one node\'s disk, so '
                   'a pod rescheduled elsewhere comes up with an empty registry.')
@click.argument('cluster_config', required=False)
def setup(list_configs, namespace, options, force, kube_context, ingress_host,
          ingress_class, issuer, tls_secret, insecure_http, rotate_token,
          registry_storage_class, registry_storage_path, registry_node,
          cluster_config):
    """Set up the Kubernetes cluster for execution.

    Deploys a MinIO S3 server in the Kubernetes cluster. The server is used
    to store run configurations and results for individual scenario execution jobs.

    This command should be run once before executing scenarios
    on the cluster for the first time.

    If the cluster is already set up, this command will exit with an error.
    Run 'vast execution cluster cleanup' first to clean up the existing setup,
    or use ``--force`` to force re-setup.

    Use ``--list`` to see available cluster configuration plugins.

    Cluster-specific options can be passed using ``--option key=value``.

    Ignores projects entirely: this deploys into a cluster and runs from any
    directory, so a ``.robovast_project`` is neither required nor read here — it is
    found by walking up to the filesystem root, and a project above an unrelated CWD
    has no business deciding where a cluster's pods may run.

    Node label selectors for job and control pods are therefore read only from a
    ``.vast`` you name explicitly, under
    ``execution.kubernetes.jobs.node_labels`` and
    ``execution.kubernetes.control.node_labels``::

        vast -V my_campaign.vast exec cluster setup rke2

    Without ``-V`` no node labels are applied (logged at INFO) and pods schedule
    wherever Kubernetes puts them. A named ``.vast`` that cannot be read is an error
    rather than a silent "no labels".

    Share credentials (``ROBOVAST_SHARE_TYPE`` and its provider variables — e.g.
    ``ROBOVAST_GCS_BUCKET`` / ``ROBOVAST_GCS_KEY_FILE``) are read from the host
    environment / project ``.env`` at setup and handed to the in-cluster service
    as a Secret, so ``--upload-to-share`` campaigns work from the cluster. A key
    *file* is inlined into the Secret; nothing else is needed on the host.
    """
    # Deferred: these reach the Kubernetes client, and this module is a CLI
    # plugin `load_plugins()` imports on every `vast` invocation -- at module
    # level they made `vast login` and `vast wait` pay for the cluster stack.
    from .cluster_context import \
        require_context_for_multi_cluster  # pylint: disable=import-outside-toplevel
    from .cluster_setup import setup_server  # pylint: disable=import-outside-toplevel
    if list_configs:
        try:
            setup_server(config_name=None, list_configs=True)
            return
        except Exception as e:
            handle_cli_exception(e)

    if not cluster_config:
        click.echo("Error: CLUSTER_CONFIG argument is required when not using --list", err=True)
        sys.exit(1)

    try:
        # Only an explicitly named config, never an ambient project — see setup_server.
        require_context_for_multi_cluster(kube_context, get_vast_file_override())
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    # Parse cluster-specific options
    cluster_kwargs = {"namespace": namespace}
    if kube_context is not None:
        cluster_kwargs["kube_context"] = kube_context
    for option in options:
        if '=' not in option:
            click.echo(f"Error: Invalid option format '{option}'. Expected key=value", err=True)
            sys.exit(1)
        key, value = option.split('=', 1)
        cluster_kwargs[key] = value

    service_kwargs = {
        'ingress_host': ingress_host, 'ingress_class': ingress_class,
        'issuer': issuer, 'tls_secret': tls_secret,
        'insecure_http': insecure_http, 'rotate_token': rotate_token,
        'registry_storage_class': registry_storage_class,
        'registry_storage_path': registry_storage_path,
        'registry_node': registry_node,
    }
    try:
        setup_server(config_name=cluster_config, list_configs=False, force=force,
                     service_kwargs=service_kwargs, **cluster_kwargs)
        click.echo("✓ Cluster setup completed successfully!")
        if ingress_host:
            scheme = 'http' if insecure_http else 'https'
            click.echo(f"  RoboVAST is at {scheme}://{ingress_host}")
            click.echo("  Run 'vast exec cluster token' for the URL and access token "
                       "to hand your users.")

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
    except (click.UsageError, click.ClickException):
        raise
    except Exception as e:
        handle_cli_exception(e)


@cluster.command(name='run-cleanup')
@click.option('--campaign', '-i', default=None,
              help='Clean only jobs for this campaign (e.g. campaign-2025-02-27-123456). Without this, cleans all scenario-runs jobs.')
@click.option('--data', is_flag=True,
              help='Also delete the campaign result bucket(s) from the object store (via the service).')
@click.option('--force', is_flag=True,
              help='With --data: delete a named campaign even if the service still considers it live.')
@target_options
def run_cleanup(campaign, data, force, namespace, context):
    """Clean up jobs and pods from a cluster run.

    Removes scenario execution Jobs and their pods directly (using your kubeconfig
    — the ``-x`` context, ``-n`` namespace). By default removes all campaigns; use
    ``--campaign`` for one.

    Use ``--data`` to **also** delete the campaign result bucket(s) from the object
    store. That step goes **through the robovast-service** (which holds the
    object-store credentials), resolved on the conventional local port or from the
    ``vast login`` record — no local credentials needed.

    Usage: vast execution cluster run-cleanup
    Usage: vast execution cluster run-cleanup --campaign campaign-2025-02-27-123456
    Usage: vast execution cluster run-cleanup --campaign campaign-2025-02-27-123456 --data
    """
    # Deferred: these reach the Kubernetes client, and this module is a CLI
    # plugin `load_plugins()` imports on every `vast` invocation -- at module
    # level they made `vast login` and `vast wait` pay for the cluster stack.
    from .cluster_context import \
        require_context_for_multi_cluster  # pylint: disable=import-outside-toplevel
    from .cluster_execution import _label_safe_campaign  # pylint: disable=import-outside-toplevel
    from .cluster_execution import cleanup_cluster_campaign, get_cluster_job_counts_per_campaign
    from .kubernetes import check_kubernetes_access  # pylint: disable=import-outside-toplevel
    from .kubernetes import get_kubernetes_client
    try:
        require_context_for_multi_cluster(context, get_vast_file_override())
        k8s_client = get_kubernetes_client(context=context)
        click.echo("Checking Kubernetes cluster access...")
        k8s_ok, k8s_msg = check_kubernetes_access(k8s_client, namespace=namespace)
        if not k8s_ok:
            click.echo(f"✗ Error: {k8s_msg}", err=True)
            sys.exit(1)

        skip_job_cleanup = False
        if campaign:
            per_run = get_cluster_job_counts_per_campaign(namespace, context=context)
            label_safe = _label_safe_campaign(campaign)
            if label_safe not in per_run:
                available = sorted(per_run.keys())
                if data:
                    # Jobs already gone — warn but continue to bucket cleanup
                    click.echo(f"Campaign '{campaign}' not found in cluster (jobs already cleaned up).", err=True)
                    skip_job_cleanup = True
                else:
                    if available:
                        click.echo(f"Campaign '{campaign}' not found in cluster.", err=True)
                        click.echo("Available campaign-ids:", err=True)
                        for rid in available:
                            click.echo(f"  - {rid}", err=True)
                    else:
                        click.echo("No scenario run jobs in cluster.", err=True)
                    sys.exit(1)
            if not skip_job_cleanup:
                click.echo(f"Cleaning up jobs and pods for campaign '{campaign}'...")
        else:
            click.echo("Cleaning up all scenario run jobs and pods...")

        if not skip_job_cleanup:
            cleanup_cluster_campaign(namespace=namespace, campaign=campaign, context=context)
            click.echo("✓ Job/pod cleanup completed successfully!")

        if data:
            # Bucket cleanup runs server-side: the service owns the object-store
            # credentials and the authoritative live-campaign guard.
            from robovast.service.interface import CleanupDataRequest
            with service_client(namespace, context,
                                require_service=True) as (client, target):
                _echo_target(target)
                res = client.cleanup_campaign_data(
                    CleanupDataRequest(campaign_id=campaign, force=force))
                if not res.ok:
                    raise click.ClickException(res.message or "cleanup-data failed")
                click.echo(f"✓ {res.message}")

    except (click.UsageError, click.ClickException):
        raise
    except Exception as e:
        handle_cli_exception(e)


@cluster.command()
@click.option('--namespace', '-n', default='default', show_default=True,
              help='Namespace the robovast-service runs in')
@click.option('--context', '-x', 'kube_context', default=None,
              help='Kubernetes context to use (default: active context in kubeconfig)')
def upgrade(namespace, kube_context):
    """Move a running instance to a new RoboVAST version.

    Rolls the Deployment onto the resolved image, reconciles RBAC, and waits for the
    pod to be Ready before reporting anything.

    Always restarts the pod, even when nothing appears to have changed. That is the
    point: an image ref that is a floating tag, or a change confined to the Secrets,
    leaves the Deployment spec byte-identical, and Kubernetes then rolls nothing while
    this command reports success. It also makes the restart the *only* way the env
    Secrets are re-read -- the pod loads them through ``envFrom`` at container start and
    never again. The cost is a few seconds during which the API is unavailable.

    RBAC reconciliation is not decoration: a version needing a permission the last one
    did not — as ``/usage`` once needed a cluster-scoped ClusterRole — would otherwise
    deploy and then fail at runtime with a 403, which reads as a bug rather than as a
    missed migration.

    Reads ``./.env`` from the current directory, like every ``vast`` command, so run it
    from the checkout whose ``.env`` describes this deployment. That file is also where
    every image ref comes from -- there is deliberately no ``--image``. A flag would have
    been one-shot, because nothing reads the deployed image back, so a later bare
    ``upgrade`` would silently revert a digest pinned that way to whatever the floating
    tag points at -- the exact failure pinning exists to prevent, arriving through the
    command that looks safe. For a one-off, a real environment variable already beats a
    ``.env`` line::

        ROBOVAST_CONTROLLER_IMAGE=repo@sha256:... vast exec cluster upgrade

    and ``make image-digests`` writes the durable form into ``.env``.

    Distinct from ``setup --force``, deliberately:

    \b
      upgrade        the image, RBAC, and the credential Secrets it can rebuild
                     from the environment (git, share, ntfy, registry). The access
                     token is preserved, so nobody is logged out.
      setup --force  additionally re-mints the access token when asked
                     (--rotate-token), which does log everyone out.

    Campaign data lives in the object store and survives both.
    """
    from .cluster_setup import apply_controller_rbac
    from .service_deploy import (deploy_service, published_host, read_service_config_from_cluster,
                                 reconcile_registry_ingress_path, running_image_digest,
                                 wait_for_rollout, wait_for_service_ready)

    try:
        config_name, config_kwargs = read_service_config_from_cluster(
            namespace, kube_context)
        if not config_name:
            raise click.ClickException(
                f"no robovast-service found in namespace {namespace!r}. "
                "Run 'vast exec cluster setup <flavor>' first.")
        # Recovered from the live Ingress, not remembered: an upgrade is run by whoever
        # is upgrading, not necessarily by whoever set the cluster up. It doubles as the
        # registry prefix, so passing it is what stops an upgrade from rebuilding the
        # registry config without one and silently disabling in-cluster builds.
        ingress_host = published_host(namespace, kube_context)

        click.echo(f"Upgrading robovast-service in {namespace}...")
        before = running_image_digest(namespace, kube_context)
        apply_controller_rbac(namespace=namespace, kube_context=kube_context)
        if reconcile_registry_ingress_path(namespace=namespace, kube_context=kube_context):
            click.echo("  added the registry's /v2 route to the existing Ingress")
        deploy_service(namespace=namespace, kube_context=kube_context,
                       config_name=config_name, config_kwargs=config_kwargs,
                       registry_host=ingress_host)
        wait_for_service_ready(namespace=namespace, kube_context=kube_context)
        # With a floating tag the Deployment spec is byte-identical either way, so this
        # is the only thing distinguishing "rolled onto new code" from "restarted the
        # same image" -- the question every upgrade actually asks.
        #
        # Only once the rollout has converged, though: readiness is satisfied by the
        # *old* pod for the whole of a rolling update, and reading there reported
        # "unchanged" across a real image change.
        if wait_for_rollout(namespace=namespace, kube_context=kube_context):
            after = running_image_digest(namespace, kube_context)
            if before and after and before != after:
                click.echo(f"  image {before[:19]} -> {after[:19]}")
            elif after:
                click.echo(f"  image unchanged: {after[:19]}")
        else:
            click.echo("  (the rollout had not settled; check "
                       f"'kubectl -n {namespace} rollout status deploy/robovast-service')")
        click.echo("✓ upgraded and ready")
    except click.ClickException:
        raise
    except Exception as e:
        handle_cli_exception(e)


@cluster.command(name='token')
@click.option('--namespace', '-n', default='default', show_default=True,
              help='Namespace the robovast-service runs in')
@click.option('--context', '-x', 'kube_context', default=None,
              help='Kubernetes context to use (default: active context in kubeconfig)')
@click.option('--quiet', '-q', is_flag=True,
              help='Print only the token, for piping into something else.')
def cluster_token(namespace, kube_context, quiet):
    """Show the access token, and what to hand users along with it.

    Setup deliberately prints the token only once, so reading it back meant a
    ``kubectl get secret ... | base64 -d`` incantation -- which every operator then
    keeps in their shell history, and which needs kubectl syntax to answer a RoboVAST
    question.

    The token is **per cluster**: an instance mints its own, and one instance's token is
    simply wrong at another. That is the failure this command is most likely to prevent,
    since the mistake looks identical to a mistyped password.

    \b
      vast exec cluster token            what to send a user
      vast exec cluster token -q         the token alone
      vast exec cluster token -x prod    a specific cluster
    """
    from .service_deploy import existing_auth_token, published_url

    try:
        token = existing_auth_token(namespace, kube_context)
        if not token:
            raise click.ClickException(
                f"no access token in namespace {namespace!r} — either nothing is "
                "deployed there, or it predates authentication. "
                "Run 'vast exec cluster setup <flavor>' to create one.")
        if quiet:
            click.echo(token)
            return

        url = published_url(namespace, kube_context)
        if not url:
            # Reachable but unpublished is a real state (no --ingress-host), and the
            # token is still the right answer -- just not one a user can use yet.
            click.echo("The service has no Ingress, so there is no URL to give out. "
                       "Re-run setup with --ingress-host to publish it.")
            click.echo(f"\nAccess token: {token}")
            return

        click.echo(f"RoboVAST is at {url}")
        click.echo(f"Access token: {token}")
        click.echo("")
        click.echo("Command line:  pip install robovast && "
                   f"vast login {url}")
        # No name here: the operator prints this for somebody else, and only that person
        # can declare theirs -- 'vast login' prints the same command with it filled in.
        from robovast.client.login import mcp_add_command
        click.echo("Claude Code:   " + " \\\n                 ".join(
            mcp_add_command(url, token)))
        click.echo("Browser:       open the URL and paste the token.")
    except click.ClickException:
        raise
    except Exception as e:
        handle_cli_exception(e)


@cluster.command()
@click.option('--cluster-config', '-c', 'config_name', default=None,
              help='Cluster configuration plugin to use (auto-detects if not specified)')
@click.option('--namespace', '-n', default=None,
              help='Kubernetes namespace to clean up (required when using --cluster-config without prior setup)')
@click.option('--option', '-o', 'options', multiple=True,
              help='Cluster-specific option in key=value format (can be used multiple times)')
@click.option('--context', '-x', 'kube_context', default=None,
              help='Kubernetes context to use (default: active context in kubeconfig)')
def cleanup(config_name, namespace, options, kube_context):
    """Clean up the Kubernetes cluster setup.

    Removes the NFS server pod and service from the Kubernetes cluster
    by deleting the NFS manifest configuration.

    This command can be run after completing all scenario executions
    to clean up cluster infrastructure resources (different from run-cleanup
    which only cleans up job pods).

    If ``--cluster-config`` is not specified, it will automatically detect which
    cluster configuration was used during setup by reading it back from the deployed
    ``robovast-service`` — so this works from any host and needs no project.
    When specifying ``--cluster-config`` explicitly, pass ``-n <namespace>`` if the
    setup was done in a non-default namespace.
    """
    # Deferred: these reach the Kubernetes client, and this module is a CLI
    # plugin `load_plugins()` imports on every `vast` invocation -- at module
    # level they made `vast login` and `vast wait` pay for the cluster stack.
    from .cluster_context import \
        require_context_for_multi_cluster  # pylint: disable=import-outside-toplevel
    from .cluster_setup import delete_server  # pylint: disable=import-outside-toplevel
    try:
        require_context_for_multi_cluster(kube_context, get_vast_file_override())
        cluster_kwargs = {}
        if namespace is not None:
            cluster_kwargs["namespace"] = namespace
        if kube_context is not None:
            cluster_kwargs["kube_context"] = kube_context
        for option in options:
            if '=' not in option:
                click.echo(f"Error: Invalid option format '{option}'. Expected key=value", err=True)
                sys.exit(1)
            key, value = option.split('=', 1)
            cluster_kwargs[key] = value
        delete_server(config_name=config_name, **cluster_kwargs)
        click.echo("✓ Cluster cleanup completed successfully!")

    except Exception as e:
        handle_cli_exception(e)
