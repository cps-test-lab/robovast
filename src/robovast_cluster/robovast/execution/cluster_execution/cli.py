# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""``vast cluster`` / ``vast service`` — the verbs that need a cluster of one's own.

What is left here is the **operator** half: ``setup``, ``cleanup``, ``upgrade``, ``token``
and ``jobs-cleanup`` each want a kubeconfig, an API server or a cluster Secret, and
``monitor`` wants one for its fallback view. The verbs that merely *drive* a deployed
service — ``run``, ``stop``, ``stop-job``, ``log``, ``download-cleanup`` — moved to
``robovast.client.cluster_cli``, because launching a campaign is four HTTP verbs and
requiring this distribution for it meant installing the full core to send a POST.

``monitor`` stayed even though its service-driven view is pure client code, because its
kubeconfig view is not and the two are one command chosen at runtime: moving it would split
one function's body across two distributions. A client-only user gets ``vast campaign wait`` and the
web UI, which cover everything the service view showed.

These attach to the client's ``cluster`` group through the ``robovast.cluster_plugins``
entry-point group, so each loads only when typed — ``vast cluster --help`` lists them
without importing any of the Kubernetes client.
"""

import datetime
import logging
import sys
import time

import click

from robovast.client.errors import handle_cli_exception
from robovast.client.service_target import detected_service_url
from robovast.client.service_target import echo_target as _echo_target
from robovast.client.service_target import service_client, target_options
from robovast.client.status import Phase, Status, stall_report

logger = logging.getLogger(__name__)


def _progress_bar(done, total, width=20):
    """Return ``(bar, pct)`` — the ``█``/``░`` progress bar used across the monitor."""
    frac = max(0.0, min(1.0, done / total)) if total and total > 0 else 0.0
    filled = int(width * frac)
    return "█" * filled + "░" * (width - filled), 100.0 * frac


def _fmt_size(n):
    """Format a byte count as MiB (matches the upload progress display)."""
    return f"{n / 1024 / 1024:.1f} MiB"


def _fmt_rate(bps):
    """Format a transfer rate (bytes/s) with an adaptive unit."""
    if bps >= 1024 * 1024:
        return f"{bps / 1024 / 1024:.1f} MiB/s"
    if bps >= 1024:
        return f"{bps / 1024:.1f} KiB/s"
    return f"{bps:.0f} B/s"


def _live_campaigns():
    """``(campaign_id, phase)`` for campaigns the service is still driving, or ``None``.

    Asks the **service**, never Kubernetes. The service drives campaigns in-process, so its
    own list is the authority; going to the API server would answer a nearby but different
    question -- Jobs exist -- and would miss a campaign between batches, in variation or in
    postprocessing, which are precisely the phases where a roll is most destructive. It also
    keeps this and the web UI's Admin page answering out of one source.

    ``None`` is not an empty list: it means the question could not be asked at all, and the
    two must not collapse. "No campaigns are running" permits a silent roll; "I could not
    find out" does not.
    """
    from robovast.service.http_client import RobovastClient
    from robovast.service.interface import ListCampaignsRequest
    from robovast.client.status import is_terminal

    url = detected_service_url()
    if not url:
        return None
    try:
        resp = RobovastClient(url).list_campaigns(ListCampaignsRequest(limit=100))
    except Exception:  # pylint: disable=broad-except
        return None
    return [(c.campaign_id, c.phase) for c in resp.campaigns if not is_terminal(c.phase)]


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
    # `robovast.service.http_client`, not the core's `robovast.service.client` compat
    # re-export: this view is pure client code, and importing it through the core made
    # `monitor` need a distribution it otherwise does not.
    from robovast.service.http_client import RobovastClient
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
        # A campaign spends its whole life in one `running` phase, so the only thing worth
        # saying about its clock is the verdict: this run is wedged, not merely slow. The
        # bare age of the last completion used to ride here too and said nothing a reader
        # could act on -- and it could not be judged at all without a declared per-run
        # budget, which is now reported once by `validate_project` instead.
        stall = stall_report(Status.model_validate(status))
        if stall.get("stall_reason"):
            lines.append(f"  Stalled: {stall['stall_reason']}")
        up = (status.get("extra") or {}).get("upload")
        # `Phase.SHARING`, spelled as the enum spells it. This read `"uploading"` -- a
        # phase no part of RoboVAST ever sets -- so the bar never drew once, for anyone.
        if status.get("phase") == Phase.SHARING and up:
            # The archive is gzipped on the fly, so the wire total is unknown; the bar
            # tracks the campaign bytes going in and `sent` reports what has left.
            done = up.get("source_done", 0)
            total = up.get("source_total", 0)
            up_line = "  Upload: "
            if total:
                u_bar, u_pct = _progress_bar(done, total)
                up_line += (f"[{u_bar}] {u_pct:5.1f}%  "
                            f"{_fmt_size(done)}/{_fmt_size(total)}")
            else:
                up_line += "in progress"
            up_line += f"   sent {_fmt_size(up.get('sent', 0))}"
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


@click.command()
@click.option('--interval', '-i', type=float, default=2.0, show_default=True,
              help='Polling interval in seconds')
@click.option('--once', is_flag=True,
              help='Print job status once and exit')
@click.option('--context', '-x', 'kube_context', default=None,
              help='Kubernetes context to use (default: active context in kubeconfig)')
@click.option('--namespace', '-n', default='default', show_default=True,
              help='Kubernetes namespace the scenario Jobs run in.')
@click.option('--vast', 'vast', default=None, metavar='FILE',
              type=click.Path(exists=True, dir_okay=False),
              help='Watch every context this .vast names, instead of only the active '
                   'one. Ignored when --context is given, which names one directly.')
def monitor(interval, once, kube_context, namespace, vast):
    """Monitor scenario execution jobs on the cluster.

    Displays progress per run: how many jobs have finished (completed or failed),
    how many are running, and how many are pending for each run.

    By default, monitors only the contexts referenced in the .vast config file.
    Falls back to the active kubeconfig context when no per-cluster config is
    defined. Use --context to restrict monitoring to a single cluster.
    Only contexts with active or past jobs are shown.

    This is intended for monitoring jobs created by
    a campaign launch.
    """
    # Deferred: these reach the Kubernetes client, and this module is a CLI
    # plugin `load_plugins()` imports on every `vast` invocation -- at module
    # level they made `vast login` and `vast campaign wait` pay for the cluster stack.
    from .cluster_context import (  # pylint: disable=import-outside-toplevel
        get_active_kube_context, get_config_context_names)
    try:
        cursor_up = "\033[A"
        clear_line = "\033[2K"
        bar_width = 20
        pct_width = 7

        # Build list of (label, kube_context_name) to monitor
        if not kube_context:
            # Only a .vast the caller named: monitoring a cluster works without one, and
            # then watches the active context. There is deliberately no ambient project
            # to fall back on -- a file in some parent directory of the CWD deciding
            # which clusters to watch is a surprise, not a convenience.
            config_names = get_config_context_names(vast) if vast else set()
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
            from .cluster_execution import \
                JOB_PHASE_COUNTERS  # pylint: disable=import-outside-toplevel
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
                empty = dict.fromkeys(JOB_PHASE_COUNTERS, 0)
                empty["total_job_num"] = None
                c = per_run.get(campaign, empty)
                # A job that is blocked (cannot pull its image) or waiting (no cluster capacity
                # yet) is submitted and unfinished, so it belongs in both sums below --
                # otherwise this loop reaches "All jobs finished" while such jobs sit in
                # the cluster, and stops watching them.
                unstarted = c["pending"] + c.get("blocked", 0) + c.get("waiting", 0)
                current_total = c["completed"] + c["failed"] + c["running"] + unstarted
                if campaign not in ctx_initial:
                    ctx_initial[campaign] = current_total
                # Prefer annotation-based total so the monitor shows the full run size
                # even while many jobs are still pending / not yet visible in the API.
                annotated_total = c.get("total_job_num")
                total = annotated_total if annotated_total else ctx_initial[campaign]
                ctx_ok[campaign] = max(ctx_ok.get(campaign, 0), c["completed"])
                ctx_fail[campaign] = max(ctx_fail.get(campaign, 0), c["failed"])
                still_in_cluster = c["running"] + unstarted
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

                # Blocked and waiting are named only when non-zero: waiting is every
                # cluster batch's normal first state, and a permanent "Waiting: 0" is how
                # a reader learns to stop reading the line. A blocked count is the one
                # number here that means someone has to do something.
                extra = ""
                if c.get("waiting"):
                    extra += f"  Waiting: {c['waiting']}"
                if c.get("blocked"):
                    extra += f"  Blocked: {c['blocked']}"
                lines.append(
                    f"{indent}{campaign}  [{progress_bar}]  {pct_str}  "
                    f"{finished}/{total}  ({ok} ok, {fail} fail)  "
                    f"Running: {c['running']}  Pending: {c['pending']}{extra}"
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


def _echo_placement(placement):
    """Say where this deployment's node-local data went, and how that was decided."""
    reason = {
        "label": "kept, from the existing node label",
        "auto": "chosen automatically: most free disk",
        "requested": "as requested",
    }
    data, build = placement.get("data_node"), placement.get("build_node")
    if not data:
        click.echo("  node-local data: nothing pinned (a StorageClass or bucket backs it)")
        return
    click.echo(f"  workspaces, registry and store on {data} "
               f"({reason.get(placement.get('data_source'), 'decided')})")
    if build and build != data:
        click.echo(f"  build cache on {build}")
    elif build:
        click.echo(f"  build cache alongside it on {build} "
                   "(--buildkit-node puts it on another disk)")
    # The one line the operator cannot reconstruct afterwards: once the label is off the
    # old node, nothing in the deployment names it again, and the bytes are still there.
    moved = sorted({n for n in (placement.get("data_previous"),
                                placement.get("build_previous")) if n})
    if moved:
        click.echo(f"  moved here from {', '.join(moved)}; the bytes written there are NOT "
                   "migrated")
        click.echo("  so this deployment starts with an empty registry and rebuilds what "
                   "it needs")
    click.echo("  recorded as a node label, so a later cleanup + setup returns here "
               "without any flag")


@click.command()
@click.option('--list', 'list_configs', is_flag=True,
              help='List available cluster configuration plugins')
@click.option('--namespace', '-n', default='default', show_default=True,
              help='Kubernetes namespace for execution (used by cluster run)')
@click.option('--option', '-o', 'options', multiple=True,
              help='Cluster-specific option in key=value format (can be used multiple times)')
@click.option('--force', '-f', is_flag=True,
              help='Force re-setup even if cluster is already set up')
@click.option('--gpu-replicas', type=int, default=None, metavar='N',
              help='Advertise N time-slicing replicas per physical GPU, so N pods can '
                   'share one card. Without this flag a GPU that is present is used '
                   'anyway (with a sensible default) and a cluster without one is left '
                   'alone; passing it makes GPU support a requirement, so a cluster that '
                   'cannot provide it becomes an error. N caps concurrency and does NOT '
                   'partition VRAM: all N renderers allocate from the same card.')
@click.option('--no-gpu', is_flag=True,
              help='Set the cluster up without GPU scheduling, even if it has a GPU.')
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
@click.option('--data-node', default='', metavar='NODE',
              help='Hold this deployment\'s node-local data on this node: the workspaces, '
                   'the registry, (where it is an emptyDir) the results store and, unless '
                   '--buildkit-node says otherwise, the build cache. Rarely needed -- setup '
                   'picks the node with the most free space the first time and records the '
                   'choice as a node label, so later runs stay put without any flag. Naming '
                   'a node moves the placement off whatever node holds it now and says so; '
                   'the bytes are NOT migrated, so the new node starts empty.')
@click.option('--buildkit-storage-class', default='', metavar='NAME',
              help='Back the shared build daemon\'s cache with a PVC from this '
                   'StorageClass instead of a hostPath. Prefer an SSD class: BuildKit\'s '
                   'snapshotter is small-file heavy and a slow disk becomes the bottleneck '
                   'the cache was meant to remove.')
@click.option('--buildkit-storage-path', default='', metavar='PATH',
              help='Host directory backing the build cache when using hostPath '
                   '(default: /data/robovast-buildkit). This is what makes a base image '
                   'pulled once stay pulled.')
@click.option('--buildkit-storage-size', default='', metavar='SIZE',
              help='Size of the build cache PVC (default: 200Gi). Ignored with hostPath, '
                   'where the node\'s disk and the daemon\'s GC ceiling bound it instead.')
@click.option('--buildkit-node', default='', metavar='NODE',
              help='Hold the build cache on this node. Defaults to the data node, because '
                   'auto-separating would put a 150 GB cache on whichever node was left '
                   'over. Separate them where the disk is tight: these are the '
                   'deployment\'s two large on-disk tenants, and a full builder disk on '
                   'the service\'s node becomes DiskPressure evictions of the API.')
@click.option('--buildkit-cache-max', default='', metavar='SIZE',
              help='Ceiling on the build cache, e.g. 150GB or 70%. Sized for the disk it '
                   'lands on: the default suits a large one, and a deployment whose /data is '
                   'smaller should say so here rather than rely on --buildkit-cache-min-free '
                   'to hold the line.')
@click.option('--buildkit-cache-min-free', default='', metavar='SIZE',
              help='Free space to keep on the cache\'s filesystem, e.g. 50GB. Measured '
                   'against the disk rather than the cache, so it is what keeps any ceiling '
                   'safe on a disk smaller than the ceiling -- and what stops a full builder '
                   'disk from becoming DiskPressure evictions on that node.')
@click.option('--buildkit-cache-reserved', default='', metavar='SIZE',
              help='Cache kept even when old, e.g. 100GB. A floor, not a target: it is what '
                   'stops a quiet week from evicting the base image the cache exists to hold.')
@click.argument('cluster_config', required=False)
@click.option('--vast', 'vast', default=None, metavar='FILE',
              type=click.Path(exists=True, dir_okay=False),
              help='Read node-label selectors for job and control pods from this .vast '
                   '(execution.kubernetes.{jobs,control}.node_labels), and refuse if it '
                   'declares per-cluster resource lists for several contexts while '
                   '--context is unset. Omitted, no node labels are applied.')
def setup(list_configs, namespace, options, force, gpu_replicas, no_gpu, kube_context, vast,
          ingress_host, ingress_class, issuer, tls_secret, insecure_http, rotate_token,
          registry_storage_class, registry_storage_path, data_node,
          buildkit_storage_class, buildkit_storage_path, buildkit_storage_size,
          buildkit_node, buildkit_cache_max, buildkit_cache_min_free,
          buildkit_cache_reserved, cluster_config):
    """Set up the Kubernetes cluster for execution.

    Deploys a MinIO S3 server in the Kubernetes cluster. The server is used
    to store run configurations and results for individual scenario execution jobs.

    This command should be run once before executing scenarios
    on the cluster for the first time.

    If the cluster is already set up, this command will exit with an error.
    Run 'vast cluster cleanup' first to clean up the existing setup,
    or use ``--force`` to force re-setup.

    Use ``--list`` to see available cluster configuration plugins.

    Cluster-specific options can be passed using ``--option key=value``.

    Reads no project: this deploys into a cluster and runs from any directory. Node
    label selectors for job and control pods therefore come only from a ``.vast`` you
    name explicitly, under ``execution.kubernetes.jobs.node_labels`` and
    ``execution.kubernetes.control.node_labels``::

        vast cluster setup rke2 --vast my_campaign.vast

    Without ``--vast`` no node labels are applied (logged at INFO) and pods schedule
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
    # level they made `vast login` and `vast campaign wait` pay for the cluster stack.
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
        require_context_for_multi_cluster(kube_context, vast)
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
    }
    # Its own channel, not `service_kwargs`: the build daemon is a workload beside the service
    # rather than part of it, and `deploy_service` cannot carry it anyway -- it dispatches one
    # manifest per kind, so a second Deployment would silently replace the service's own.
    buildkit_kwargs = {
        'storage_class': buildkit_storage_class,
        'storage_path': buildkit_storage_path,
        'storage_size': buildkit_storage_size,
        'gc_max_used': buildkit_cache_max,
        'gc_min_free': buildkit_cache_min_free,
        'gc_reserved': buildkit_cache_reserved,
    }
    try:
        # Named arguments, never folded into cluster_kwargs: that dict is the provider's
        # `-o` channel and is persisted as the cluster's recorded config, and it swallows
        # keys it does not know -- so a typo like `-o gpu_replica=24` would be accepted,
        # stored, and do nothing. A click option answers "no such option" instead.
        placement = setup_server(config_name=cluster_config, list_configs=False, force=force,
                                 service_kwargs=service_kwargs, gpu_replicas=gpu_replicas,
                                 no_gpu=no_gpu, buildkit_kwargs=buildkit_kwargs,
                                 data_node=data_node, buildkit_node=buildkit_node,
                                 vast_path=vast,
                                 **cluster_kwargs)
        click.echo("✓ Cluster setup completed successfully!")
        # Stated rather than only logged. No flag is the normal way to run this, so the
        # node holding the workspaces and the registry is chosen without the operator
        # naming it -- and a decision nobody sees is how this deployment ended up on a
        # node nobody picked, with the disk meter reporting a machine nobody expected.
        _echo_placement(placement or {})
        if ingress_host:
            scheme = 'http' if insecure_http else 'https'
            click.echo(f"  RoboVAST is at {scheme}://{ingress_host}")
            click.echo("  Run 'vast service token' for the URL and access token "
                       "to hand your users.")

    except Exception as e:
        handle_cli_exception(e)


@click.command('jobs-cleanup')
@click.option('--campaign', '-i', default=None,
              help='Clean only jobs for this campaign (e.g. campaign-2025-02-27-123456). Without this, cleans all scenario-runs jobs.')
@click.option('--data', is_flag=True,
              help='Also delete the campaign result bucket(s) from the object store (via the service).')
@click.option('--force', is_flag=True,
              help='With --data: delete a named campaign even if the service still considers it live.')
@target_options
@click.option('--vast', 'vast', default=None, metavar='FILE',
              type=click.Path(exists=True, dir_okay=False),
              help='A .vast to pre-flight against this cluster. Its only use here is to '
                   'refuse when it declares per-cluster resource lists for several '
                   'contexts and --context was not given -- which would otherwise pick '
                   'a cluster by accident. Optional, and read only for that check.')
def run_cleanup(campaign, data, force, namespace, context, vast):
    """Clean up jobs and pods from a cluster run.

    Removes scenario execution Jobs and their pods directly (using your kubeconfig
    — the ``-x`` context, ``-n`` namespace). By default removes all campaigns; use
    ``--campaign`` for one.

    Use ``--data`` to **also** delete the campaign result bucket(s) from the object
    store. That step goes **through the robovast-service** (which holds the
    object-store credentials), resolved on the conventional local port or from the
    ``vast login`` record — no local credentials needed.

    \b
    Usage: vast cluster jobs-cleanup
    Usage: vast cluster jobs-cleanup --campaign campaign-2025-02-27-123456
    Usage: vast cluster jobs-cleanup --campaign campaign-2025-02-27-123456 --data
    """
    # Deferred: these reach the Kubernetes client, and this module is a CLI
    # plugin `load_plugins()` imports on every `vast` invocation -- at module
    # level they made `vast login` and `vast campaign wait` pay for the cluster stack.
    from .cluster_context import \
        require_context_for_multi_cluster  # pylint: disable=import-outside-toplevel
    from .cluster_execution import _label_safe_campaign  # pylint: disable=import-outside-toplevel
    from .cluster_execution import cleanup_cluster_campaign, get_cluster_job_counts_per_campaign
    from .kubernetes import check_kubernetes_access  # pylint: disable=import-outside-toplevel
    from .kubernetes import get_kubernetes_client
    try:
        require_context_for_multi_cluster(context, vast)
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
            with service_client(namespace, context) as (client, target):
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


@click.command()
@click.option('--namespace', '-n', default='default', show_default=True,
              help='Namespace the robovast-service runs in')
@click.option('--context', '-x', 'kube_context', default=None,
              help='Kubernetes context to use (default: active context in kubeconfig)')
@click.option('--timeout', default=180.0, show_default=True, metavar='SECONDS',
              help='How long to wait for the new pod to take over before failing')
@click.option('--buildkit-cache-max', default='', metavar='SIZE',
              help='Resize the build cache ceiling. Without this the daemon keeps whatever '
                   'it was set up with -- an upgrade is not the place to quietly re-size a '
                   'store somebody bounded on purpose.')
@click.option('--buildkit-cache-min-free', default='', metavar='SIZE',
              help='Change the free space kept on the cache filesystem. See setup.')
@click.option('--buildkit-cache-reserved', default='', metavar='SIZE',
              help='Change the cache kept even when old. See setup.')
@click.option('--no-restart', is_flag=True, default=False,
              help='Reconcile only what does not need the pod rolled -- RBAC, the '
                   'queues, the registry ingress route -- then stop. For granting a '
                   'permission the RUNNING version is missing without a version change or '
                   'an API blip, e.g. while a campaign is in flight.')
@click.option('--yes', '-y', is_flag=True, default=False,
              help='Do not ask before rolling over live campaigns. For scripts; without it '
                   'a non-interactive run aborts rather than rolling silently.')
def upgrade(namespace, kube_context, timeout, buildkit_cache_max,
            buildkit_cache_min_free, buildkit_cache_reserved, no_restart, yes):
    """Move a running instance to a new RoboVAST version.

    Rolls the Deployment onto the resolved image, reconciles RBAC, and waits for the
    *new* pod to be the one serving before reporting anything.

    It fails, non-zero, if that pod does not take over: an image it cannot pull, a node it
    cannot be scheduled on, or a container that crash-loops. The reason Kubernetes gave is
    printed as soon as it appears, so a stuck upgrade names its cause in seconds instead of
    looking like a hang -- and "✓ upgraded and ready" now means it. Use ``--timeout`` for a
    registry slow enough to need longer.

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

    ``--no-restart`` reconciles just that part — RBAC, the registry
    ingress route — and stops before the Deployment is touched. All three are picked up by
    the *running* pod (the API server evaluates RBAC per request, and
    workload, a route is the gateway's own state), so a permission the running version is
    missing can be granted without a version change and without the API blip. That is the
    difference between fixing a missed migration and rolling a service: it is the only way
    to do the former while a campaign is in flight, because the campaign controller lives in
    the pod a roll would replace. It does *not* move the image and does *not* re-read the env
    Secrets — for either of those, run the command without the flag.

    Before the roll it asks the service which campaigns are live and names them, for the
    reason ``--no-restart`` exists: the pod being replaced is where their controller runs.
    ``--yes`` skips the question. A service that cannot be reached is reported and the roll
    goes ahead -- a wedged service is a reason to upgrade, not a reason to refuse -- and
    that is said out loud, so a silent roll never has to be read as "nothing was running".

    Reads the environment like every ``vast`` command -- ``./.env`` then
    ``~/.config/robovast/env`` -- so ``ROBOVAST_PROJECT`` and ``ROBOVAST_PROJECT_TAG``
    are where the images come from, and this is the command that moves them. Both are
    baked into the service pod as the *site default*; a campaign can still override them
    per launch (``vast workspace run --image-project``), which needs no upgrade at all.

    There is deliberately no ``--image``. A flag would have been one-shot, because nothing
    reads the deployed image back, so a later bare ``upgrade`` would silently revert it --
    arriving through the command that looks safe. For a one-off, a real environment
    variable already beats both files::

        ROBOVAST_PROJECT=ghcr.io/cps-test-lab vast service upgrade

    ``make image-digests PROJECT=...`` checks that a project holds a complete family
    before you point a cluster at it.

    **This is the command for rolling out an image**, not ``setup --force``:

    \b
      upgrade        the image, RBAC, and the credential Secrets it can rebuild from
                     the environment (git, share, ntfy, registry). Recovers this
                     cluster's config and ingress host from the cluster itself, so it
                     cannot lose them. The access token is preserved.
      setup --force  re-provisions: the object store, the registry storage. It
                     takes its options as *arguments*, so a re-run without the original
                     flags re-provisions with different ones. Also re-mints the access
                     token when asked (--rotate-token), logging everyone out.

    Campaign data lives in the object store and survives both.
    """
    from .cluster_setup import apply_controller_rbac
    from .service_deploy import (deploy_service, published_url, read_service_config_from_cluster,
                                 reconcile_registry_ingress_path, running_image_digest,
                                 wait_for_rollout, wait_for_service_ready)

    try:
        config_name, config_kwargs = read_service_config_from_cluster(
            namespace, kube_context)
        if not config_name:
            raise click.ClickException(
                f"no robovast-service found in namespace {namespace!r}. "
                "Run 'vast cluster setup <flavor>' first.")
        # Recovered from the live Ingress, not remembered: an upgrade is run by whoever
        # is upgrading, not necessarily by whoever set the cluster up. It doubles as the
        # registry prefix, so passing it is what stops an upgrade from rebuilding the
        # registry config without one and silently disabling in-cluster builds.
        # One read, two facts. The host doubles as the registry prefix; the whole URL is
        # what the service declares to its clients, and its scheme comes from the live TLS
        # block rather than from arguments this command does not have. Stating it is what
        # makes an upgrade enough on its own -- deriving it from the host would publish
        # https:// over a plain-HTTP deployment, and saying nothing would leave a service
        # that has just been unpublished still advertising an origin.
        public_origin = published_url(namespace, kube_context)
        ingress_host = public_origin.split("://", 1)[-1] if public_origin else ""

        click.echo(f"Upgrading robovast-service in {namespace}...")
        before = running_image_digest(namespace, kube_context)
        click.echo("  reconciling RBAC...")
        apply_controller_rbac(namespace=namespace, kube_context=kube_context)
        if reconcile_registry_ingress_path(namespace=namespace, kube_context=kube_context):
            click.echo("  added the registry's /v2 route to the existing Ingress")
        # --no-restart stops here, and everything above this line is why it can: RBAC is
        # evaluated by the API server per request, and
        # an Ingress route is the gateway's own state -- so the RUNNING pod picks all three
        # up with no roll. Only the image and the env Secrets need a restart, and this flag
        # promises neither.
        #
        # It exists because the alternative was telling an operator whose service is missing
        # one permission to roll the Deployment: a few seconds of unavailable API, a pod that
        # re-reads its Secrets, and a move onto whatever the resolved image now points at --
        # none of which they asked for, and all of which is unavailable to them anyway while
        # a campaign is in flight, since the campaign controller lives in that pod.
        if no_restart:
            click.echo("✓ reconciled RBAC and the ingress route")
            click.echo("  the pod was NOT restarted: the running version is unchanged and "
                       "its env Secrets were not re-read")
            click.echo("  run 'vast service upgrade' without --no-restart to move the "
                       "image or pick up changed Secrets")
            return
        # The last moment to ask. Everything above is picked up by the RUNNING pod, which
        # is exactly why --no-restart returns before here; below this line the pod is
        # replaced, and the campaign controller lives in it -- the same reasoning the
        # --no-restart block above states at length.
        live = _live_campaigns()
        if live is None:
            # Said, not swallowed. Refusing here would block the one case where the upgrade
            # IS the recovery -- a service too wedged to answer -- so it proceeds; but an
            # operator must not read a silent roll as "nothing was running".
            click.echo("  could not ask the service which campaigns are live; rolling "
                       "without that check")
        elif live and not yes:
            click.echo(f"  {len(live)} campaign(s) are live, and the pod this replaces is "
                       f"where their controller runs:")
            for campaign_id, phase in live:
                click.echo(f"    {campaign_id}  [{phase}]")
            # abort=True, and no --force twin: on a non-TTY click.confirm aborts by itself,
            # so a script that did not pass --yes fails loudly instead of rolling over a
            # campaign nobody was watching.
            click.confirm("  roll anyway?", abort=True)
        deploy_service(namespace=namespace, kube_context=kube_context,
                       config_name=config_name, config_kwargs=config_kwargs,
                       registry_host=ingress_host, public_origin=public_origin)
        # Converge the build daemon too, or an upgrade would leave the cluster running a
        # service that has nothing to build with.
        #
        # Its storage settings are recovered from the live Deployment rather than defaulted:
        # they arrived as `setup` flags, nothing records them, and re-rendering from defaults
        # would silently move a PVC-backed cache back to a hostPath. `deploy_service` now
        # recovers its own the same way (see `service_storage_from_cluster`), and both take
        # their node pin from the constant label rather than from an argument this call site
        # would have to remember to pass.
        from .buildkitd_deploy import (apply_buildkitd,  # pylint: disable=import-outside-toplevel
                                       buildkitd_storage_from_cluster)
        settings = buildkitd_storage_from_cluster(namespace, kube_context)
        # An explicitly passed budget wins over the recovered one -- otherwise the setting
        # would be write-once at setup, changeable only by tearing the daemon down. Recovery
        # is the default, not a lock: it exists so an upgrade that says nothing changes
        # nothing, which is a different thing from an upgrade that cannot change it.
        settings.update({k: v for k, v in (
            ("gc_max_used", buildkit_cache_max),
            ("gc_min_free", buildkit_cache_min_free),
            ("gc_reserved", buildkit_cache_reserved)) if v})
        apply_buildkitd(namespace, kube_context=kube_context, **settings)
        click.echo("  converged the shared build daemon")
        wait_for_service_ready(namespace=namespace, kube_context=kube_context,
                               timeout_s=timeout)
        # No branch here, deliberately: wait_for_rollout raises on every outcome that is
        # not convergence. It used to return a bool, and the caller printing
        # "✓ upgraded and ready" regardless of it is how an upgrade whose pod sat in
        # ImagePullBackOff still exited 0. Everything below this line is now reachable
        # only when the new pod really is the one serving.
        wait_for_rollout(namespace=namespace, kube_context=kube_context, timeout_s=timeout,
                         report=lambda message: click.echo(f"  {message}"))
        # With a floating tag the Deployment spec is byte-identical either way, so this
        # is the only thing distinguishing "rolled onto new code" from "restarted the
        # same image" -- the question every upgrade actually asks.
        #
        # Only once the rollout has converged, though: readiness is satisfied by the
        # *old* pod for the whole of a rolling update, and reading there reported
        # "unchanged" across a real image change.
        after = running_image_digest(namespace, kube_context)
        rolled_onto_new_bytes = bool(before and after and before != after)
        if rolled_onto_new_bytes:
            click.echo(f"  image {before[:19]} -> {after[:19]}")
        elif after:
            click.echo(f"  image unchanged: {after[:19]}")
        # After a tag bump or a moved project every node is cold for the whole family, so
        # the first campaign pays a full pull of robovast-roqsim. Here because the pod has
        # just been restarted anyway, so nothing is mid-campaign -- and after the success
        # line, since this must not be able to fail or stall an upgrade that has converged.
        # The echo is free: these refs were resolved in order to declare the DaemonSet.
        click.echo("✓ upgraded and ready")
        from .image_warm import warm_family_images  # pylint: disable=import-outside-toplevel
        warmed = warm_family_images(namespace, kube_context)
        if warmed:
            click.echo("  prewarming on the nodes: "
                       + ", ".join(ref.rsplit("/", 1)[-1] for ref in warmed))
        # The host already recovered above, not a fresh read: this runs *after* the upgrade
        # has succeeded, and a reporting line must not be able to fail or stall it -- the
        # rule `running_image_digest` states and follows. (An Ingress read here cost three
        # tests a 10s timeout each against an unreachable cluster, which is the same fault
        # arriving as a hang instead of an error.)
        if ingress_host:
            click.echo(f"  Published at {ingress_host}; "
                       f"'vast service token' prints the URL and access token.")
        if not rolled_onto_new_bytes and after:
            # Not a warning: an upgrade run to make the pod re-read a changed Secret is
            # *supposed* to land here, since that is the only thing that re-reads them
            # (envFrom is read once, at container start). But it is also what an operator
            # who expected new code sees, and those two need telling apart.
            click.echo("  Same bytes as before: the pod restarted, so changed Secrets and "
                       "config took effect. If you expected new code, nothing newer has "
                       "been pushed to the tag this resolves -- set ROBOVAST_PROJECT_TAG "
                       "to pin a specific one.")
    except click.ClickException:
        raise
    except Exception as e:
        handle_cli_exception(e)


@click.command('token')
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
      vast service token            what to send a user
      vast service token -q         the token alone
      vast service token -x prod    a specific cluster
    """
    from .service_deploy import existing_auth_token, published_url

    try:
        token = existing_auth_token(namespace, kube_context)
        if not token:
            raise click.ClickException(
                f"no access token in namespace {namespace!r} — either nothing is "
                "deployed there, or it predates authentication. "
                "Run 'vast cluster setup <flavor>' to create one.")
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
        # robovast-client, not robovast: this is handed to somebody who will *drive* the
        # service, and the full distribution is 88 packages and ~290 MB of simulator and
        # dataframe machinery they have no use for. Telling them to install it is exactly
        # what the split exists to stop.
        click.echo("Command line:  pip install robovast-client && "
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


@click.command()
@click.option('--cluster-config', '-c', 'config_name', default=None,
              help='Cluster configuration plugin to use (auto-detects if not specified)')
@click.option('--namespace', '-n', default=None,
              help='Kubernetes namespace to clean up (required when using --cluster-config without prior setup)')
@click.option('--option', '-o', 'options', multiple=True,
              help='Cluster-specific option in key=value format (can be used multiple times)')
@click.option('--context', '-x', 'kube_context', default=None,
              help='Kubernetes context to use (default: active context in kubeconfig)')
@click.option('--forget-placement', is_flag=True,
              help='Also remove the node labels recording where this deployment kept its '
                   'data. Without this the labels stay, so a later setup lands on the same '
                   'node with no flags -- which is the point of them. The on-disk data is '
                   'not removed either way.')
@click.option('--vast', 'vast', default=None, metavar='FILE',
              type=click.Path(exists=True, dir_okay=False),
              help='A .vast to pre-flight against this cluster. Its only use here is to '
                   'refuse when it declares per-cluster resource lists for several '
                   'contexts and --context was not given -- which would otherwise pick '
                   'a cluster by accident. Optional, and read only for that check.')
def cleanup(config_name, namespace, options, kube_context, forget_placement, vast):
    """Clean up the Kubernetes cluster setup.

    Removes the NFS server pod and service from the Kubernetes cluster
    by deleting the NFS manifest configuration.

    This command can be run after completing all scenario executions
    to clean up cluster infrastructure resources (different from jobs-cleanup
    which only cleans up job pods).

    If ``--cluster-config`` is not specified, it will automatically detect which
    cluster configuration was used during setup by reading it back from the deployed
    ``robovast-service`` — so this works from any host and needs no project.
    When specifying ``--cluster-config`` explicitly, pass ``-n <namespace>`` if the
    setup was done in a non-default namespace.
    """
    # Deferred: these reach the Kubernetes client, and this module is a CLI
    # plugin `load_plugins()` imports on every `vast` invocation -- at module
    # level they made `vast login` and `vast campaign wait` pay for the cluster stack.
    from .cluster_context import \
        require_context_for_multi_cluster  # pylint: disable=import-outside-toplevel
    from .cluster_setup import delete_server  # pylint: disable=import-outside-toplevel
    try:
        require_context_for_multi_cluster(kube_context, vast)
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
        delete_server(config_name=config_name, forget_placement=forget_placement,
                      **cluster_kwargs)
        click.echo("✓ Cluster cleanup completed successfully!")
        if forget_placement:
            click.echo("  placement labels removed; the next setup picks a node again")
            click.echo("  the data under the hostPaths is NOT removed by this")
        else:
            click.echo("  placement labels kept, so the next setup returns to the same "
                       "node (--forget-placement clears them)")

    except Exception as e:
        handle_cli_exception(e)
