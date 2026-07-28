# Copyright (C) 2026 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""MCP plugin: running a campaign, and watching it run.

A strict client of a running ``robovast-service`` — the single execution authority. There
is no local subprocess path: when no service answers these tools fail loudly rather than
silently running a divergent lane.

Building the experiment image lives here too. A build is part of a campaign's driven work
(``start_campaign`` performs one when the ``.vast`` has a ``build:`` section), not a
separate lifecycle stage, so its tools belong beside the run they serve.
"""

import logging
import time

from fastmcp import FastMCP

from robovast.common.log_summary import DEFAULT_TOP
from robovast.common.status import stall_report
from robovast.mcp_server import results_resolver, service_access
from robovast.mcp_server.service_access import NO_SERVICE

logger = logging.getLogger(__name__)


def _progress_from_status(st) -> float | None:
    """Overall progress in ``[0, 1]``, or ``None`` when it cannot be known honestly.

    - **batch** mode: ``completed / total`` — the total is known up front.
    - **search** mode: the loop ends when a stopping criterion fires, so progress is
      the closest criterion, ``max(current / limit)`` over the ``budget``. A search's
      per-batch run ratio is deliberately **not** used — it would read as overall
      completion when it is only progress through one batch of an open-ended search.

    Returns ``None`` (never a misleading number) when a search has no usable budget
    value yet.
    """
    if st.budget:
        fracs = [max(0.0, min(1.0, b.current / b.limit))
                 for b in st.budget if b.current is not None and b.limit]
        return max(fracs) if fracs else None
    mode = (st.mode or "").lower()
    if mode in ("", "batch") and st.runs and st.runs.total:
        return max(0.0, min(1.0, st.runs.completed / st.runs.total))
    return None


def _status_to_dict(campaign_id: str, backend, st) -> dict:
    """Render a controller :class:`Status` into the MCP status dict.

    Faithful to both batch and search campaigns: run counts are **batch-scoped**
    (``batch_runs_*``) and ``progress`` is computed mode-aware (see
    :func:`_progress_from_status`), while the search-only fields (best objective,
    budget, batches done, stop reason) are surfaced when present.
    """
    result: dict = {
        "campaign_id": campaign_id,
        "backend": backend,
        "status": st.phase,
        "mode": st.mode,
        "batch_runs_done": st.runs.completed if st.runs else 0,
        "batch_runs_total": st.runs.total if st.runs else 0,
        # Two distinct outcomes, because a run can deliver nothing *or* deliver a
        # failing trial, and reporting only the former made a sweep with a failed
        # trial look clean. See RunProgress.
        "batch_runs_no_result": st.runs.no_result if st.runs else 0,
        "batch_runs_failed": st.runs.failed if st.runs else 0,
        "progress": _progress_from_status(st),
    }
    # How long the campaign has held this phase. A phase alone cannot separate slow
    # from wedged: an image build and a build that will never finish both read
    # "building", and a pre-run step that hangs is otherwise invisible until someone
    # notices the run count has not moved.
    if getattr(st, "phase_since", None):
        result["phase_age_s"] = round(max(0.0, time.time() - st.phase_since), 1)
    # Progress age and the stall verdict, derived once in the status contract so the
    # CLI monitor and this tool cannot disagree about whether a run is wedged.
    result.update(stall_report(st))
    if st.batches_done:
        result["batches_done"] = st.batches_done
    if st.best_objective is not None:
        result["best_objective"] = st.best_objective
    if st.budget:
        result["budget"] = [b.model_dump() for b in st.budget]
    if st.stop:
        result["stop"] = st.stop
    if st.error:
        result["error"] = st.error
    # Postprocessing is a separate fact from ``phase`` on purpose (see Status): a
    # campaign whose runs all passed but whose postprocessing failed stays
    # ``finished``, because the runs are the deliverable. That only works if the fact
    # is *reported* — folded into ``stage`` it reads like a progress note, and a
    # campaign with no metrics at all looks as green as a complete one.
    result["postprocessed"] = st.postprocessed
    if st.postprocessing_error:
        result["postprocessing_error"] = st.postprocessing_error
    if st.share_error:
        result["share_error"] = st.share_error
    return result


def start_campaign(config_filter: str = "", runs: int = 0, backend: str = "",
                   workspace_id: str = "", config_path: str = "",
                   campaign_name: str = "", upload_to_share: bool = False,
                   description: str = "") -> dict:
    """Start a campaign through the robovast-service and return immediately.

    The service is the execution authority — there is **no** local subprocess path —
    so a service must be reachable (a ``vast serve`` locally, or ``vast serve
    --attach`` / a tunnel to a cluster); otherwise this fails loudly. Results land
    wherever the service keeps them (local disk, or the cluster object store); poll
    :func:`get_campaign_status`. For a serviceless local run use the ``vast exec
    local run`` CLI instead.

    Args:
        config_filter: Optional glob to run only matching configurations.
        runs: Runs per configuration; ``0`` uses the value from the ``.vast`` file.
        backend: On a multi-backend service (``vast serve --backend local+cluster``),
            which lane to run on — ``"local"`` (pilot: Docker on the serve host) or
            ``"cluster"`` (scaled: Kubernetes). Empty uses the service's **default
            lane (cluster when available)**. Single-backend services ignore it.
        workspace_id: **Required** — the workspace whose project to run. A campaign
            always runs a workspace's ``.vast``; there is no server-side "current
            project". Get an id from ``list_workspaces()`` (a directory the operator
            pinned with ``vast serve --workspace-dir``) or by uploading one with
            ``create_workspace`` + ``update_workspace``.
        config_path: Which ``.vast`` when the workspace has several (empty = the sole
            one, and an error naming the candidates when it is ambiguous).
        campaign_name: Override the campaign name; the id becomes ``<name>-<timestamp>``.
            Empty uses the ``.vast`` ``metadata.name``.
        upload_to_share: When true, a raw (pre-postprocess) archive is delivered to the
            configured share when the campaign finishes. Target/credentials come from
            the service config, not this call.
        description: **Set this on every start** — one line (max 200 characters) saying
            what this run is *for*, in the words you would use to answer "why did we run
            this?" a week later: the question it answers, what changed since the previous
            campaign, whether it is a pilot or the full sweep. It is stored with the
            campaign and shown in ``list_campaigns`` and the web UI, so it is what
            distinguishes one ``campaign-<timestamp>`` from the next; without it a listing
            of a dozen runs is unreadable. Do not restate the id, the config filter, or the
            run count — those are already listed. Good: "pilot: 5 reps DWB vs MPPI on
            open_space, checking the new inflation radius". Bad: "campaign run".

    Returns:
        ``{campaign_id, backend}`` on success; ``{error}`` when no service is reachable
        or the start is refused.
    """
    try:
        client = service_access.service_client()
        if client is None:
            return {"error": NO_SERVICE}
        if backend and backend not in ("local", "cluster"):
            return {"error": f"unknown backend {backend!r}; use 'local' or 'cluster'"}
        from robovast.service.interface import (DESCRIPTION_MAX_LEN,
                                                CreateCampaignRequest)
        # Checked here rather than left to the request model's validator: this returns
        # the actionable "shorten it and call again" instead of a pydantic traceback
        # string, and it refuses before anything is launched.
        if len(description) > DESCRIPTION_MAX_LEN:
            return {"error": f"description is {len(description)} characters; the limit "
                             f"is {DESCRIPTION_MAX_LEN} — shorten it to one line"}
        ref = client.create_campaign(CreateCampaignRequest(
            workspace_id=workspace_id, config_path=config_path,
            config_filter=config_filter, campaign_name=campaign_name,
            description=description,
            # Pass the "unset" value through instead of substituting 1: the service maps a
            # non-positive count to None and falls back to the .vast's execution.runs,
            # which is what this tool documents. Substituting 1 here silently shrank every
            # campaign started without an explicit count to one run per configuration — a
            # 25-trial sweep finished "successfully" with 5 trials.
            runs=runs if runs and runs > 0 else 0,
            upload_to_share=upload_to_share, backend=backend or None))
        return {"campaign_id": ref.campaign_id, "backend": backend or "service-default"}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def get_campaign_status(campaign_id: str) -> dict:
    """Report a campaign's live status, progress, and whether it is wedged.

    **Neither terminal state is self-explanatory — two fields decide what to do next.**

    ``stalled`` answers "is this run broken?" while it is still ``running``, which
    ``status`` cannot: a campaign holds ``running`` for its whole life whether or not
    anything is happening.

    * ``true`` — nothing has completed for longer than one run is allowed to take
      (``progress_age_s`` vs ``progress_deadline_s``). Not merely slow.
      ``stall_reason`` names the exact next call.
    * ``false`` — inside the declared budget.
    * ``null`` — the ``.vast`` declares no ``execution.timeout``, so **no verdict is
      possible**; ``stall_verdict`` says so. This is not "healthy": read
      ``progress_age_s`` (seconds since a run last completed) and judge it against how
      long one run should take.

    ``postprocessed`` answers "are there results?", which ``status: "finished"`` does
    not imply: the runs are the deliverable, so a campaign whose trials all passed but
    whose postprocessing failed still finishes — ``postprocessed: false`` with
    ``postprocessing_error``. No postprocessing means no CSVs and no ``data.db``;
    ``run_postprocessing`` fixes that without re-running the campaign.

    Args:
        campaign_id: The id returned by :func:`start_campaign`.

    Returns:
        ``{campaign_id, backend, status, mode, batch_runs_done, batch_runs_total,
        batch_runs_failed, batch_runs_no_result, progress, phase_age_s,
        progress_age_s, stalled, postprocessed, stage}``, plus
        ``progress_deadline_s`` / ``stall_reason`` or ``stall_verdict`` per the
        tri-state above, plus search-only fields (``best_objective``, ``budget``,
        ``batches_done``, ``stop``) when applicable; ``{error}`` when no service is
        reachable or the campaign is unknown. Run counts are **batch-scoped**;
        ``progress`` is overall and mode-aware (``None`` when a search's completion
        cannot be known yet).

        ``phase_age_s`` is how long the current phase has been held — the pre-run
        equivalent of ``progress_age_s``, and the only signal for a phase
        (``initializing``, ``building``) that has no run counter to watch.

        Note the local lane does not *enforce* ``execution.timeout``, so a stalled
        local run stays alive to be inspected; end it with ``stop_campaign``.
    """
    try:
        client = service_access.service_client()
        if client is None:
            return {"error": NO_SERVICE}
        st = client.get_status(campaign_id)
        result = _status_to_dict(campaign_id, "service", st)
        result["stage"] = st.stage or ""  # a live marker string, not a log tail
        return result
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def get_campaign_log(campaign_id: str, lines: int = 200, offset: int = 0,
                     grep: str = "", min_severity: str = "",
                     summarize: bool = False, top: int = DEFAULT_TOP,
                     phase: str = "") -> dict:
    """Read a campaign's unified infrastructure log.

    Returns the campaign's whole infrastructure log — the same divider-separated
    stream the web UI log panel shows — assembled from the per-phase files under
    ``_execution/`` in phase order, each under a ``===== PHASE =====`` divider:

    * **VARIATION** — config generation / composition (incl. plugin subprocess output).
    * **RUN** — the controller driving batches/runs (``controller.log``). For local
      Docker campaigns this also includes the ``run.sh`` / ``docker compose`` output.
    * **POSTPROCESSING** — rosbag→CSV→``data.db`` (on the cluster, the separate
      conversion Job's output followed by the host stage).
    * **BUILD** — the experiment image this campaign waited for, when it has a
      ``build:`` section. **Not in a default read** — ask for it with ``phase="build"``.
      It is the output of a content-addressed build that may be shared with other
      campaigns rather than this campaign's own narrative, and it is routinely the
      largest section by far, so leading with it would spend a whole read on docker
      layers. It is always listed in ``phases`` with its line count, so it is announced
      rather than hidden — and it is the place to look when a campaign failed before it
      ever ran.

    A phase's section is absent until that phase has produced output; ``phases`` reports
    which are present and how large each is.

    Served by the robovast-service, which knows where the log lives for its backend —
    on the cluster the durable copy is in the object store and the live one is pod
    scratch, neither of them on this host. With no service reachable it falls back to
    reading a local results directory, so an archived campaign is still readable
    offline.

    Filtered by :func:`~robovast.mcp_server.log_view.view_log` — the same four controls
    every log tool takes. Each line of a run's output arrives stamped with the relay
    prefix of whatever forwarded it (``robovast  | [INFO] [<ts>]
    [scenario_execution_ros]: ``); that prefix is dropped where the payload carries its
    own level and timestamp, which is most of them.

    **When a run looks stuck, start with ``summarize=True``.** Filtering cannot
    diagnose a flood, because the flood *is* the signal: a wedged run matched a
    severity ``grep`` 18226 times and the returned lines read as ordinary noise. The
    summary shows that as one line with its count.

    Args:
        campaign_id: The id returned by :func:`start_campaign`.
        lines: Maximum number of lines to return (default 200). Ignored when
            ``summarize`` is set — a summary is already small.
        offset: Line offset to start reading from (default 0), for pagination.
        grep: Keep only lines matching this regex (case-insensitive). Applied before
            ``offset``/``lines``, so paging walks the matches.
        min_severity: Keep only lines this severe — ``"warn"`` or ``"error"``. Uses
            RoboVAST's own classifier (a line's ``[WARN]``/``[ERROR]`` marker, else
            :data:`~robovast.common.log_summary.DEFAULT_SEVERITY_PATTERN`), so do
            **not** hand-write a severity ``grep``: this is the same definition the
            campaign status uses, and two different patterns give two different
            answers to "is this run healthy?".
        summarize: Return distinct **patterns with counts** instead of lines: each
            line is normalized (timestamps, coordinates and ids replaced) and equal
            shapes are grouped. This is how you read a 20k-line log for ~20 tokens.
        top: With ``summarize``, the maximum number of patterns to return (default
            ``DEFAULT_TOP``; ``0`` = all). ``patterns_total`` always states the true
            number.
        phase: Read one phase only — ``"build"``, ``"variation"``, ``"run"``,
            ``"postprocessing"``, ``"plugin install"`` — or ``"all"`` for every phase
            including the asides. Empty (the default) reads the campaign's own phases.
            Combine ``phase="build"`` with ``summarize=True`` to read a noisy image
            build for a handful of tokens.

    Returns:
        Lines: ``{file_name, phases, total_lines, returned_lines, offset, content,
        dropped}``. With ``summarize``: ``{file_name, phases, patterns, patterns_total,
        severity_counts, matched_lines, total_lines, dropped}`` — no ``content``, and
        each pattern is ``{pattern, count, severity, example}``.
        ``phases`` is always the full list of sections the log has —
        ``[{name, lines, included}, ...]`` — so a section left out of this read is
        reported, never silently absent.
        ``{error}`` if the campaign is unknown, ``grep`` is not a valid regex,
        ``min_severity`` is not a known severity, or ``phase`` is not a known phase.
    """
    from robovast.mcp_server.log_view import view_log  # noqa: PLC0415

    # Ask the service, which knows where this campaign's log actually lives: on the
    # cluster the durable copy is in the object store and the live one is pod scratch
    # (ClusterService.get_campaign_logs serves both), neither of which is on this
    # filesystem. Reading the local results dir here reported an empty log for every
    # cluster campaign. The local disk path stays as the serviceless fallback so an
    # archived results tree is still readable with no service running.
    client = service_access.service_client()
    if client is not None:
        try:
            # The service pages by *byte* offset; this tool pages by lines, so take
            # the whole text (offset 0) and slice lines below, as before.
            text = client.get_campaign_logs(campaign_id, offset=0).text
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}
    else:
        from robovast.common.campaign_logs import \
            assemble_log_from_dir  # noqa: PLC0415
        try:
            campaign_dir = results_resolver.resolve_campaign_path(campaign_id)
            text, _, _ = assemble_log_from_dir(campaign_dir, offset=0, eof=True)
        except ValueError as e:
            return {"error": str(e)}
    try:
        text, phases = _select_phases(text, phase)
    except ValueError as e:
        return {"error": str(e)}
    try:
        view = view_log(text, grep=grep, min_severity=min_severity,
                        summarize=summarize, top=top)
    except ValueError as e:
        return {"error": str(e)}
    name = f"{campaign_id} (infrastructure log)"
    if summarize:
        # ``offset``/``lines`` page through lines and have no meaning over grouped
        # patterns; omitting them keeps the response from implying a page exists.
        return {"file_name": name, "phases": phases, "patterns": view["patterns"],
                "patterns_total": view["patterns_total"],
                "severity_counts": view["severity_counts"],
                "matched_lines": view["lines"],
                "total_lines": view["lines_total"], "dropped": view["dropped"]}
    all_lines = view["content"].splitlines()
    selected = all_lines[offset:offset + lines]
    return {
        "file_name": name,
        "phases": phases,
        "total_lines": len(all_lines),
        "returned_lines": len(selected),
        "offset": offset,
        "content": "\n".join(selected),
        "dropped": view["dropped"],
    }


def _select_phases(text: str, phase: str) -> "tuple[str, list[dict]]":
    """Narrow an assembled campaign log to *phase*; also describe every phase present.

    Returns ``(text, phases)`` where ``phases`` names each section with its line count
    and whether this read includes it — so a section that was left out is *reported*,
    which is the same contract ``view_log`` keeps for the lines it filters.

    ``phase=""`` reads the campaign's own phases: the asides
    (:data:`~robovast.common.campaign_logs.ASIDE_PHASES` — today just ``BUILD``) are
    excluded, because they are large and belong to shared work rather than to this
    campaign. ``"all"`` includes everything; a phase name includes only that one.

    Raises:
        ValueError: *phase* is not a known phase — a silently ignored selector would
            read as "that phase produced nothing".
    """
    from robovast.common.campaign_logs import (  # noqa: PLC0415
        ASIDE_PHASES, INFRA_PHASES, phase_banner, split_phases)

    known = {name.lower(): name for name, _ in INFRA_PHASES}
    wanted = phase.strip().lower()
    if wanted and wanted != "all" and wanted not in known:
        raise ValueError(
            f"unknown phase {phase!r}; use one of "
            f"{', '.join(sorted(known))} — or 'all'")

    out, phases = [], []
    for name, section in split_phases(text):
        if not name:
            out.append(section)  # pre-divider remainder; never dropped
            continue
        if wanted and wanted != "all":
            included = name.lower() == wanted
        else:
            included = wanted == "all" or name not in ASIDE_PHASES
        # Content lines only — the banner is a divider, not log output — so the count is
        # what a caller would actually receive for this phase.
        body = section.replace(phase_banner(name), "", 1)
        phases.append({"name": name, "lines": len(body.strip("\n").splitlines()),
                       "included": included})
        if included:
            out.append(section)
    # The sections tile the input, so an all-included read returns it byte for byte:
    # asking for the log must not change how many lines the log has.
    return "".join(out), phases


def list_campaign_jobs(campaign_id: str) -> dict:
    """List a campaign's current-batch jobs (live) with aggregate status counts.

    A "job" is one execution unit of the campaign: a single **run** on the local
    Docker backend (sequential, so at most one is ``running``), or a **Kubernetes
    Job** on the cluster backend. Reports live status only — pair with
    :func:`get_job_log` to read a running job's log.

    Requires a reachable robovast-service (bring up a ``vast serve`` or a tunnel).

    Args:
        campaign_id: The id returned by :func:`start_campaign`.

    Returns:
        ``{jobs: [{job_name, status, display_name, detail}], counts: {running,
        pending, waiting, completed, failed, blocked, total}}`` where ``status`` is one
        of ``running`` / ``pending`` / ``waiting`` / ``completed`` / ``failed`` /
        ``blocked``. A ``blocked`` job cannot start and will not recover on its own
        (e.g. an image that can't be pulled); ``detail`` carries the Kubernetes reason
        + message. A ``waiting`` job is queued for cluster capacity by Kueue — healthy,
        not stuck — with Kueue's own wait message as ``detail``.
        Returns ``{error}`` if no service is reachable.
    """
    client = service_access.service_client()
    if client is None:
        return {"error": "no robovast-service reachable (bring up a 'vast serve' or "
                         "a tunnel before starting MCP); live job listing is served "
                         "by the service"}
    try:
        return client.list_jobs(campaign_id).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def _log_response(base: dict, view: dict) -> dict:
    """Merge a :func:`view_log` result onto a transport chunk, in whichever shape it is.

    The two shapes differ by one key, and ``text`` must be *dropped* from a summary
    rather than left over from ``base`` — a response carrying both would read as "here
    are the patterns, and here are the lines", when the lines were never selected.
    """
    merged = {**base, "lines": view["lines"], "lines_total": view["lines_total"],
              "dropped": view["dropped"]}
    if "patterns" in view:
        merged.pop("text", None)
        return {**merged, "patterns": view["patterns"],
                "patterns_total": view["patterns_total"],
                "severity_counts": view["severity_counts"]}
    return {**merged, "text": view["content"], "truncated": view["truncated"]}


def get_job_log(campaign_id: str, job_name: str, offset: int = 0,
                grep: str = "", tail: int = 0, min_severity: str = "",
                summarize: bool = False, top: int = DEFAULT_TOP) -> dict:
    """Read a **running** job's live log (its containers' stdout/stderr).

    Streams the live log of one job from :func:`list_campaign_jobs` — the running
    pod's log on the cluster, or the live ``logs/system.log`` file locally. On the
    cluster all of the pod's containers are merged into one stream: with more than
    one container each line is tagged ``[<container>] `` (the main ``robovast``
    container plus any secondary sim/SUT servers) and merged in timestamp order.
    Live source only: a finished job whose pod has been garbage-collected has no
    live log. Poll incrementally by passing the previous call's ``next_offset`` back
    as ``offset``.

    Requires a reachable robovast-service.

    ``grep`` / ``min_severity`` / ``tail`` / ``summarize`` filter the returned text
    (see :func:`~robovast.mcp_server.log_view.view_log`); ``next_offset`` still refers
    to the **unfiltered** stream, so incremental polling stays correct.

    **This is the tool a stalled campaign status points at.** Call it with
    ``summarize=True`` first: a run that cannot reach its goal usually says so by
    repeating one message thousands of times, and that is a single line here.

    Args:
        campaign_id: The id returned by :func:`start_campaign`.
        job_name: A ``job_name`` from :func:`list_campaign_jobs`.
        offset: Byte offset to resume from (default 0).
        grep: Keep only lines matching this regex (case-insensitive).
        tail: Keep only the last N matching lines (``0`` = all). Ignored when
            ``summarize`` is set.
        min_severity: Keep only lines this severe — ``"warn"`` or ``"error"``. Uses
            RoboVAST's own classifier, so do **not** hand-write a severity ``grep``
            (see :func:`get_campaign_log`).
        summarize: Return distinct **patterns with counts** instead of lines.
        top: With ``summarize``, the maximum number of patterns
            (default ``DEFAULT_TOP``; ``0`` = all).

    Returns:
        Lines: ``{text, next_offset, eof, lines, lines_total, dropped, truncated}``.
        With ``summarize``: ``{patterns, patterns_total, severity_counts, next_offset,
        eof, lines, lines_total, dropped}`` — no ``text``, and each pattern is
        ``{pattern, count, severity, example}``.
        ``{error}`` if no service is reachable, the job's live log source is gone, or
        a filter argument is invalid.
    """
    from robovast.mcp_server.log_view import view_log  # noqa: PLC0415
    client = service_access.service_client()
    if client is None:
        return {"error": "no robovast-service reachable (bring up a 'vast serve' or "
                         "a tunnel before starting MCP); live job logs are served "
                         "by the service"}
    try:
        chunk = client.get_job_log(campaign_id, job_name, offset).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    try:
        view = view_log(chunk.get("text", ""), grep=grep, tail=tail,
                        min_severity=min_severity, summarize=summarize, top=top)
    except ValueError as e:
        return {"error": str(e)}
    return _log_response(chunk, view)


def stop_campaign(campaign_id: str) -> dict:
    """Stop a running campaign (cooperative stop via the service).

    The service drives the campaign in-process and owns the teardown (terminating a
    local Docker container or the cluster's scenario Jobs), so this is a single
    interface call.

    Args:
        campaign_id: The id returned by :func:`start_campaign`.

    Returns:
        ``{campaign_id, stopped, status, note}`` or ``{error}`` when no service is
        reachable.
    """
    try:
        client = service_access.service_client()
        if client is None:
            return {"error": NO_SERVICE}
        res = client.stop(campaign_id)
        return {"campaign_id": campaign_id, "stopped": res.ok,
                "status": "stopping", "note": res.message}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def list_running_campaigns() -> dict:
    """List campaigns the service currently reports as live (all lanes).

    Returns:
        ``{count, running: [entry, ...]}`` where each entry carries ``campaign_id``,
        ``backend``, and ``status``; ``{error}`` when no service is reachable.
    """
    from robovast.execution.control_server import is_running  # noqa: PLC0415
    try:
        client = service_access.service_client()
        if client is None:
            return {"error": NO_SERVICE}
        resp = client.list_campaigns()
        running = [{"campaign_id": c.campaign_id, "backend": "service",
                    "status": c.phase}
                   for c in resp.campaigns if is_running(c.phase)]
        return {"count": len(running), "running": running}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def resource_usage(backend: str = "") -> dict:
    """Report an execution lane's CPU/memory capacity, usage, and parallelism.

    Use this to size a ``.vast`` run against free capacity and estimate its runtime:
    ``free_cpu = cpu_capacity - cpu_used`` (same for memory). If ``parallel_runs`` is
    False, runs execute one at a time (concurrency = 1); if True, they run in
    parallel and ``concurrency = min(floor(free_cpu / run_cpu_request),
    floor(free_mem / run_mem_request))`` using the per-run reservations declared in
    the ``.vast``. Then ``wall_time ~= ceil(num_runs / concurrency) * per_run_time``.

    Requires a reachable robovast-service (a ``vast serve`` or a tunnel).

    Args:
        backend: On a multi-backend service, which lane to size — ``"local"`` or
            ``"cluster"``. Empty uses the service's default lane (cluster when
            available). Single-backend services ignore it.

    Returns:
        ``{backend, cpu_capacity, cpu_used, memory_capacity_bytes, memory_used_bytes,
        parallel_runs}`` — CPU in cores, memory in bytes; ``{error}`` when no service
        is reachable.
    """
    if backend and backend not in ("local", "cluster"):
        return {"error": f"unknown backend {backend!r}; use 'local' or 'cluster'"}
    client = service_access.service_client()
    if client is None:
        return {"error": NO_SERVICE}
    try:
        return client.resource_usage(backend or None).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def build_experiment_image(workspace_id: str = "", config_path: str = "",
                           backend: str = "") -> dict:
    """Build the experiment container image declared by the project's ``build:`` section.

    Use this when the experiment needs new *code or system packages baked into the
    image* — e.g. a new/updated ``sim_suite`` package, or an Ubuntu (apt) dependency.
    Files that ship to ``/config`` at runtime (``run_files``, ``scenario_file``) never
    need a build.

    Declarative and registry-free. Put a ``build:`` section in the ``.vast``:

        build:
          system_packages: [ros-jazzy-nav2-smac-planner]   # apt
          python_packages: [packages/sim_suite_mobile]      # source dir / pip spec / wheel
          tag: sim-suite-mobile
        execution:
          image: build:sim-suite-mobile                     # symbolic ref

    On success the image is wired in automatically — you never handle a registry ref
    or credentials. **Idempotent**: safe to always call; if nothing changed it is a
    no-op cache hit. Poll :func:`get_image_build_status` until ``done``; on failure the
    structured error names the offending ``build:`` entry (or use
    :func:`get_image_build_log`). You may also skip this and just ``start_campaign`` —
    a ``build:<tag>`` image is (re)built automatically as the campaign's first step.

    **Order ``python_packages`` so what changes often comes last.** Each entry is
    copied and installed in its own layers, so a change to one entry only rebuilds the
    entries after it — putting a large, stable asset package before the small code
    package you keep editing turns a full rebuild into a few seconds. Dependencies come
    first regardless (an entry's deps must already be installed when it runs).

    Requires a reachable robovast-service (a local ``vast serve`` or a tunnel).

    Args:
        workspace_id: **Required** — which workspace's project to build. Same rule as
            ``start_campaign``: there is no server-side "current project".
        config_path: Which ``.vast`` when the workspace has several (empty = the sole
            one, and an error naming the candidates when it is ambiguous).
        backend: On a multi-backend service, which lane to build for — ``"local"``
            (Docker on the serve host) or ``"cluster"`` (a cluster build Job). Build
            for the same lane you will ``start_campaign`` on. Empty uses the service's
            default lane (cluster when available). Single-backend services ignore it.

    Returns:
        ``{build_id, tag, cached}`` on submit; ``{error}`` when no service is reachable
        or the ``build:`` section is missing/invalid.
    """
    if backend and backend not in ("local", "cluster"):
        return {"error": f"unknown backend {backend!r}; use 'local' or 'cluster'"}
    client = service_access.service_client()
    if client is None:
        return {"error": NO_SERVICE}
    from robovast.service.interface import BuildImageRequest
    try:
        ref = client.build_image(BuildImageRequest(
            workspace_id=workspace_id, config_path=config_path,
            backend=backend or None))
        return {"build_id": ref.build_id, "tag": ref.tag, "cached": ref.cached}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def get_image_build_status(build_id: str) -> dict:
    """Return an image build's status: ``phase``, ``done``, and a structured error.

    On failure ``error_detail`` names the ``phase`` (apt / pip / source-build /
    base-pull / push / resource), the offending ``build:`` ``entry``, a ``message``,
    and ``fixable_by`` — ``agent`` (edit the ``build:`` section) or ``infra``
    (server-side registry/base issue, not fixable by editing the ``.vast``). Use
    :func:`get_image_build_log` for the raw builder output.

    Args:
        build_id: The id returned by :func:`build_experiment_image`.
    """
    client = service_access.service_client()
    if client is None:
        return {"error": "no robovast-service reachable"}
    try:
        s = client.get_image_build_status(build_id)
        out = {"build_id": s.build_id, "tag": s.tag, "phase": s.phase,
               "done": s.done, "cached": s.cached, "image_ref": s.image_ref}
        if s.error is not None:
            out["error_detail"] = s.error.model_dump()
        return out
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def get_image_build_log(build_id: str, offset: int = 0, grep: str = "",
                        tail: int = 200, min_severity: str = "",
                        summarize: bool = False, top: int = DEFAULT_TOP) -> dict:
    """Return the builder log from byte *offset* onward, filtered for reading.

    **For a failure, read :func:`get_image_build_status` first** — its ``error`` names
    the phase and its ``log_tail`` usually contains the whole story. Come here when you
    need more than that tail.

    A builder log is dominated by per-layer byte counters, so this defaults to the last
    ``tail`` lines rather than the entire stream (which runs to tens of thousands of
    lines). Narrow it with ``min_severity="error"``, or ``grep`` — e.g.
    ``grep="x509|denied"`` — or page with ``offset``; all four controls are the same
    ones the other log tools take (see
    :func:`~robovast.mcp_server.log_view.view_log`). ``summarize=True`` is the cheapest
    way to see what a long build was actually doing, since those byte counters collapse
    into one pattern.

    Streaming: poll from ``0``, append ``text``, resume from the returned
    ``next_offset`` (a byte offset into the **unfiltered** stream, so filtering never
    breaks a poll loop); ``eof`` is true once the build is done.

    Args:
        build_id: The id returned by :func:`build_experiment_image`.
        offset: Byte offset to resume from.
        grep: Keep only lines matching this regex (case-insensitive).
        tail: Keep only the last N matching lines (default 200; ``0`` = all). Ignored
            when ``summarize`` is set.
        min_severity: Keep only lines this severe — ``"warn"`` or ``"error"`` (see
            :func:`get_campaign_log`).
        summarize: Return distinct **patterns with counts** instead of lines.
        top: With ``summarize``, the maximum number of patterns
            (default ``DEFAULT_TOP``; ``0`` = all).

    Returns:
        Lines: ``{text, next_offset, eof, lines, lines_total, dropped, truncated}``.
        With ``summarize``: ``{patterns, patterns_total, severity_counts, next_offset,
        eof, lines, lines_total, dropped}`` — no ``text``.
        ``{error}`` if no service is reachable or a filter argument is invalid.
    """
    from robovast.mcp_server.log_view import view_log  # noqa: PLC0415
    client = service_access.service_client()
    if client is None:
        return {"error": "no robovast-service reachable"}
    try:
        chunk = client.get_image_build_log(build_id, offset)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    try:
        view = view_log(chunk.text, grep=grep, tail=tail,
                        min_severity=min_severity, summarize=summarize, top=top)
    except ValueError as e:
        return {"error": str(e)}
    return _log_response({"next_offset": chunk.next_offset, "eof": chunk.eof}, view)


# -- Plugin class ------------------------------------------------------------

_TOOLS = [
    start_campaign,
    get_campaign_status,
    get_campaign_log,
    list_campaign_jobs,
    get_job_log,
    stop_campaign,
    list_running_campaigns,
    resource_usage,
    build_experiment_image,
    get_image_build_status,
    get_image_build_log,
]


class ExecutionPlugin:
    """MCP plugin: running a campaign, and watching it run."""

    name = "execution"

    def register(self, mcp: FastMCP) -> None:
        """Register all tool functions with the MCP server."""
        for fn in _TOOLS:
            mcp.tool()(fn)
