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

Building derived images lives here too. A build is part of a campaign's driven work
(``start_campaign`` performs one when a container in ``execution.containers`` adds
packages), not a separate lifecycle stage, so its tools belong beside the run they
serve.
"""

import logging
import os
import time
from typing import Optional

from fastmcp import FastMCP

from robovast.common.log_summary import DEFAULT_TOP
from robovast.client.status import Phase, is_terminal, stall_report
from robovast.mcp_server import results_resolver, service_access
from robovast.mcp_server.service_access import NO_SERVICE
from robovast.service.interface import Routes

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
    # Only when it happened, but then always: a killed run is inside ``no_result``, so
    # without this the count reads as a run that vanished on its own rather than one
    # somebody deliberately ended — and the reader goes looking for a fault there is none.
    if st.runs and st.runs.killed:
        result["batch_runs_killed"] = st.runs.killed
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


def _wait_next_step(campaign_id: str) -> str:
    """The literal command to run next, id already filled in.

    In-band rather than in the tool description: a launch that hands back only an id
    leaves "and now wait for it" to be remembered, and the reported bug behind this whole
    seam is precisely that it was not.

    A **shell command, not an MCP tool**, and deliberately so. Waiting inside a tool call
    occupies the caller for as long as the campaign runs — minutes for a pilot, days for
    a sweep — whereas a backgrounded command lets an agent harness get on with other work
    and be notified when it exits. Same poll loop either way (``execution.campaign_wait``);
    only who holds the wait differs, and the caller is the wrong place to hold it.
    """
    return (f"run in the background: vast wait {campaign_id} --interval 10 "
            f"(exit 0 finished, 1 failed/stopped)")


def start_campaign(config_filter: str = "", runs: int = 0,
                   workspace_id: str = "", config_path: str = "",
                   campaign_name: str = "", upload_to_share: bool = False,
                   show_gui: bool = False, description: str = "",
                   from_campaign: str = "") -> dict:
    """**Run the experiment.** Launches a campaign in containers and returns immediately.

    This is how a RoboVAST experiment is executed — not a ``docker compose`` or a script on
    this host, which produce no pinned image, no provenance and no repetitions, so their
    output compares with nothing. Size the lane first with ``get_resource_usage``, and pilot
    one configuration before the full sweep (``config_filter`` + ``runs=1``).

    **It is not over when this returns** — background the ``next_step`` command to be told
    when it truly is. Not waiting is fine if you *say* so (ntfy announces the end); stopping
    silently is not.

    Args:
        workspace_id: **Required unless ``from_campaign``** — the workspace holding the
            project. There is no server-side "current project".
        from_campaign: Re-run a past campaign from its own record (frozen config + the image
            its runs used): a NEW campaign, source untouched. **Takes no other argument** —
            the record supplies them, so a pilot stays a pilot. Refused if it recorded no
            usable image. Re-expands, so stochastic generators redraw.
        config_path: Which ``.vast``, when the workspace holds several.
        config_filter: Glob selecting which configurations to run.
        runs: Runs per configuration; ``0`` uses the ``.vast`` value.
        campaign_name: Override the name; the id becomes ``<name>-<timestamp>``.
        upload_to_share: Deliver a raw archive to the configured share when it finishes.
        show_gui: Watch **one** run in the simulator's window (never a sweep). Local
            ``vast serve`` on local Docker only; the window opens on that machine, not
            yours, and needs ``execution.local.gui.parameter_overrides`` (``note`` says so
            when missing). **Do not close it** — the run then never returns.
        description: **Set this every time.** One line (≤200 chars) saying what the run is
            *for* — what tells two same-day ids apart. Good: "pilot: 5 reps DWB vs MPPI on
            open_space, new inflation radius".

    Returns:
        ``{campaign_id, next_step}``, or ``{campaign_id, retriggered_from}`` for a
        ``from_campaign`` launch (``campaign_id`` is the NEW one). Plus ``note`` when the
        launch was accepted but will not do what was asked, or ``{error}`` — including no
        service reachable, which means **stop and say so**, not run it another way.
    """
    try:
        client = service_access.service_client()
        if client is None:
            return {"error": NO_SERVICE}
        from robovast.service.interface import (DESCRIPTION_MAX_LEN,
                                                CreateCampaignRequest)
        # Some clients HTML-escape prompt text, and the entity would be stored verbatim.
        # Decoded before the length check so the description is measured as the text that
        # will actually be stored.
        from robovast.mcp_server.client_text import unescape_client_text
        description = unescape_client_text(description)
        # Checked here rather than left to the request model's validator: this returns
        # the actionable "shorten it and call again" instead of a pydantic traceback
        # string, and it refuses before anything is launched.
        if len(description) > DESCRIPTION_MAX_LEN:
            return {"error": f"description is {len(description)} characters; the limit "
                             f"is {DESCRIPTION_MAX_LEN} — shorten it to one line"}
        if from_campaign:
            # Named rather than dropped: a retrigger takes these from what the source
            # campaign recorded, so accepting them here would answer a different question
            # than the caller asked and look like it had worked.
            supplied = [name for name, value in (
                ("workspace_id", workspace_id), ("config_path", config_path),
                ("config_filter", config_filter), ("runs", runs),
                ("campaign_name", campaign_name), ("upload_to_share", upload_to_share),
                ("show_gui", show_gui), ("description", description)) if value]
            if supplied:
                return {"error":
                        f"from_campaign={from_campaign!r} replays what that campaign "
                        f"recorded, so {', '.join(supplied)} cannot be set at the same time "
                        f"— drop them, or start from a workspace instead. The retriggered "
                        f"campaign's description is derived from the source's."}
            ref = client.retrigger_campaign(from_campaign)
            out = {"campaign_id": ref.campaign_id, "retriggered_from": from_campaign,
                   "next_step": _wait_next_step(ref.campaign_id)}
            if ref.note:
                out["note"] = ref.note
            return out
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
            upload_to_share=upload_to_share, show_gui=show_gui))
        out = {"campaign_id": ref.campaign_id,
               "next_step": _wait_next_step(ref.campaign_id)}
        if ref.note:
            out["note"] = ref.note
        return out
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def get_campaign_status(campaign_id: str) -> dict:
    """Is it progressing, is it wedged, and are there results? One read, no waiting.

    To *wait* for a campaign, background ``vast wait <campaign_id>`` — it exits when
    the campaign is genuinely over. This is a single look at one you are not waiting on.

    Two fields decide what to do next, and ``status`` is neither of them.

    ``stalled`` — a campaign holds ``running`` for its whole life whether or not anything
    is happening. ``true``: nothing completed for longer than one run may take
    (``progress_age_s`` vs ``progress_deadline_s``); ``stall_reason`` names the next call.
    ``false``: inside the declared budget. ``null``: the ``.vast`` declares no
    ``execution.timeout``, so **no verdict is possible** — this is not "healthy"; judge
    ``progress_age_s`` yourself. The local lane does not enforce the timeout, so a stalled
    local run stays alive to inspect: end it with ``stop_campaign``.

    ``postprocessed`` — ``status: "finished"`` does not imply results. The runs are the
    deliverable, so a campaign whose trials passed but whose postprocessing failed still
    finishes, with ``postprocessing_error`` and no CSVs or ``data.db``.
    ``run_postprocessing`` fixes that without re-running anything.

    Args:
        campaign_id: The id from ``start_campaign``.

    Returns:
        ``{campaign_id, backend, status, mode, stage, progress, phase_age_s,
        progress_age_s, stalled, postprocessed, batch_runs_done, batch_runs_total,
        batch_runs_failed, batch_runs_no_result}``, plus ``progress_deadline_s`` +
        ``stall_reason`` or ``stall_verdict``, plus search fields (``best_objective``,
        ``budget``, ``batches_done``, ``stop``) when they apply; or ``{error}``.

        Run counts are batch-scoped; ``progress`` is overall (``null`` when a search's
        completion cannot honestly be known). ``phase_age_s`` is the only signal for a
        phase with no run counter — ``initializing``, ``building``.
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


#: Hard ceiling on a single blocking wait, whatever the caller asks for. A wait holds a
#: worker thread, and the MCP client kills the call at its own timeout anyway.
_WAIT_MAX_S = int(os.environ.get("ROBOVAST_MCP_WAIT_MAX_S", "600"))


def wait_for_image_build(build_id: str, timeout_s: int = 240,
                         poll_interval_s: int = 5) -> dict:
    """Block until an image build finishes. Call this after ``build_experiment_image``.

    Blocking is right here, unlike for a campaign: a build takes minutes and always has
    work behind it in the same turn, so nothing is gained by handing the wait to a shell.
    On ``done: false`` the build is still going and ``next_step`` is the call to repeat.
    On failure read ``error_detail`` — it names what to change — before reaching for the
    builder log.

    Args:
        build_id: One id from ``build_experiment_image``.
        timeout_s: How long this call blocks before returning ``done: false``.
        poll_interval_s: Seconds between status reads.

    Returns:
        ``get_image_build_status``'s fields plus ``done`` and ``next_step``, or
        ``{error}``.
    """
    deadline = time.monotonic() + max(1, min(int(timeout_s), _WAIT_MAX_S))
    interval = max(1, int(poll_interval_s))
    while True:
        result = get_image_build_status(build_id)
        if result.get("error"):
            return result
        if result.get("done"):
            result["next_step"] = (
                f"get_image_build_log(build_id={build_id!r})"
                if result.get("error_detail") else "start_campaign(...)")
            return result
        if time.monotonic() >= deadline:
            result["next_step"] = f"wait_for_image_build(build_id={build_id!r})"
            return result
        time.sleep(interval)


def get_campaign_log(campaign_id: str, limit: int = 200, offset: int = 0,
                     grep: str = "", tail: int = 0, min_severity: str = "",
                     summarize: bool = False, top: int = DEFAULT_TOP,
                     phase: str = "", hide_shutdown: bool = True) -> dict:
    """What is the campaign doing? Its infrastructure log, in phases.

    **On a stalled or failed run, start with ``summarize=True``** — filtering cannot
    diagnose a flood, because the flood *is* the finding.

    Phases, concatenated under ``===== PHASE =====`` dividers and **all returned by
    default**: ``build`` (where a campaign that failed before it ever ran explains itself),
    ``plugin install``, ``variation``, ``run`` (the controller, plus compose output locally),
    ``postprocessing``. A build is large and comes first, so on a campaign that has run,
    narrow instead of paging: ``phase="run"``, or ``phase="build", summarize=True``.

    Args:
        campaign_id: The id from ``start_campaign``.
        limit: Maximum lines to return. Ignored with ``summarize``.
        offset: First line to return (for paging the matches).
        hide_shutdown: Stop at each run's scenario verdict — default true, and normally what
            you want: past it a run is only tearing down, and the lifecycle/TF errors that
            produces are noise. Applied first, so the other filters describe the trial;
            ``shutdown_dropped`` says what it cut.
        grep: Keep lines matching this regex (case-insensitive), before offset/limit.
        tail: Keep only the last N of what survived the filters. Ignored with ``summarize``.
        min_severity: ``"warn"`` or ``"error"``, by RoboVAST's own classifier — the same
            definition the campaign status uses, so prefer it to a severity ``grep``.
        summarize: Return distinct **patterns with counts** instead of lines — timestamps,
            coordinates and ids are normalized so equal shapes group.
        top: With ``summarize``, maximum patterns (``0`` = all).
        phase: Read only one phase. Empty and ``"all"`` both read every phase.

    Returns:
        Lines: ``{file_name, phases, total_lines, returned_lines, offset, content, dropped,
        shutdown_dropped}``. With ``summarize``: the same minus ``content``, plus
        ``{patterns, patterns_total, severity_counts, matched_lines}``, each pattern
        ``{pattern, count, severity, example}``. Or ``{error}``. ``phases`` always lists every
        section as ``{name, lines, included}``, so what a read left out is stated.
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
        view = view_log(text, grep=grep, tail=tail, min_severity=min_severity,
                        summarize=summarize, top=top, hide_shutdown=hide_shutdown)
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
                "total_lines": view["lines_total"], "dropped": view["dropped"],
                **_shutdown_report(view)}
    all_lines = view["content"].splitlines()
    selected = all_lines[offset:offset + limit]
    result = {
        "file_name": name,
        "phases": phases,
        "total_lines": len(all_lines),
        "returned_lines": len(selected),
        "offset": offset,
        "content": "\n".join(selected),
        "dropped": view["dropped"],
        **_shutdown_report(view),
    }
    # Point at the whole thing only when this page is a sample of it. Paging a 20k-line
    # log through here costs a fortune in context and reads worse than the summary; the
    # URL is for a human, and summarize=True is for the caller.
    if selected and len(all_lines) > len(selected) * 2:
        url = service_access.web_url(client, Routes.campaign_logs(campaign_id))
        if url:
            result["url"] = url
            result["stream_url"] = service_access.web_url(
                client, Routes.campaign_logs_stream(campaign_id))
    return result


def _select_phases(text: str, phase: str) -> "tuple[str, list[dict]]":
    """Narrow an assembled campaign log to *phase*; also describe every phase present.

    Returns ``(text, phases)`` where ``phases`` names each section with its line count
    and whether this read includes it — so a section that was left out is *reported*,
    which is the same contract ``view_log`` keeps for the lines it filters.

    ``phase=""`` reads **every** phase, ``"all"`` is its explicit synonym, and a phase
    name includes only that one. ``BUILD`` used to be held back from a default read as
    an aside — shared, content-addressed work rather than this campaign's narrative —
    but a campaign still waiting for its image has no other section, so that default
    answered "what is this campaign doing?" with nothing at all. Narrowing is the
    caller's move (``phase="run"``), made with the same controls every other log tool
    has, rather than a default that decides for them.

    Raises:
        ValueError: *phase* is not a known phase — a silently ignored selector would
            read as "that phase produced nothing".
    """
    from robovast.common.campaign_logs import (  # noqa: PLC0415
        INFRA_PHASES, phase_banner, split_phases)

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
        # Empty and "all" both mean every phase; only a named one narrows.
        included = not wanted or wanted == "all" or name.lower() == wanted
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
    """The campaign's current-batch jobs, live — one run locally, one Kubernetes Job each
    on the cluster. Pair with ``get_job_log`` to read a running one.

    To end a single ``running`` job that will not finish on its own, ``stop_job`` — it
    leaves the rest of the campaign running and records that run as ``killed``.

    Args:
        campaign_id: The id from ``start_campaign``.

    Returns:
        ``{jobs, counts}`` where each job is ``{job_name, status, display_name, detail}``
        and counts tallies ``running/pending/waiting/completed/failed/blocked/total``.
        Or ``{error}``.

        ``blocked`` cannot start and will not recover on its own (an unpullable image,
        say) — ``detail`` carries the reason. ``waiting`` is queued for cluster capacity
        by Kueue: healthy, not stuck.
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


def _shutdown_report(view: dict) -> dict:
    """What ``hide_shutdown`` cut, as something a caller can act on.

    ``shutdown_dropped`` is always reported, ``0`` included: a key that appeared only
    when it fired would teach nothing on the call where it did not. But a count is a
    number — an agent that has never seen the parameter cannot tell from it that there
    is one — so when the cut actually fires the response also names the way back.
    """
    dropped = view.get("shutdown_dropped", 0)
    if not dropped:
        return {"shutdown_dropped": 0}
    return {
        "shutdown_dropped": dropped,
        "note": f"{dropped} lines after the scenario's verdict were skipped (the run's "
                "shutdown phase — lifecycle and TF errors from nodes being killed, "
                "almost never what you are looking for). Pass hide_shutdown=false to "
                "include them.",
    }


def _log_response(base: dict, view: dict, *, report_shutdown: bool = False) -> dict:
    """Merge a :func:`view_log` result onto a transport chunk, in whichever shape it is.

    The two shapes differ by one key, and ``text`` must be *dropped* from a summary
    rather than left over from ``base`` — a response carrying both would read as "here
    are the patterns, and here are the lines", when the lines were never selected.

    ``report_shutdown`` is set by the tools that expose ``hide_shutdown``. A build log
    has no scenario, so reporting ``shutdown_dropped: 0`` there would spend a key
    advertising a control that tool does not have.
    """
    merged = {**base, "lines": view["lines"], "lines_total": view["lines_total"],
              "dropped": view["dropped"],
              **(_shutdown_report(view) if report_shutdown else {})}
    if "patterns" in view:
        merged.pop("text", None)
        return {**merged, "patterns": view["patterns"],
                "patterns_total": view["patterns_total"],
                "severity_counts": view["severity_counts"]}
    return {**merged, "text": view["content"], "truncated": view["truncated"]}


def get_job_log(campaign_id: str, job_name: str, offset: int = 0,
                grep: str = "", tail: int = 0, min_severity: str = "",
                summarize: bool = False, top: int = DEFAULT_TOP,
                hide_shutdown: bool = True) -> dict:
    """What is one **running** job doing? Its containers' live stdout/stderr.

    **This is what a stalled status points at. Call it with ``summarize=True`` first:**
    a wedged run repeats one message thousands of times, which summarizes to one line.

    Live source only — a finished job whose pod was garbage-collected has none; read the
    campaign log instead. Every container the job runs is merged into one stream, each
    line tagged ``[<container>]`` when there is more than one.

    Args:
        campaign_id: The id from ``start_campaign``.
        job_name: A ``job_name`` from ``list_campaign_jobs``.
        offset: **Byte** offset to resume from — pass back the previous call's
            ``next_offset`` to poll incrementally. It indexes the *unfiltered* stream, so
            filtering never breaks a poll loop.
        hide_shutdown, grep, tail, min_severity, summarize, top: The filters
            ``get_campaign_log`` documents, applied in that order.

    Returns:
        Lines: ``{text, next_offset, eof, lines, lines_total, dropped,
        shutdown_dropped, truncated}``. With ``summarize``: the same minus ``text``,
        plus ``{patterns, patterns_total, severity_counts}``. Or ``{error}``.
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
                        min_severity=min_severity, summarize=summarize, top=top,
                        hide_shutdown=hide_shutdown)
    except ValueError as e:
        return {"error": str(e)}
    return _log_response(chunk, view, report_shutdown=True)


def stop_campaign(campaign_id: str) -> dict:
    """Stop a running campaign. The service owns the teardown (containers, cluster Jobs).

    On a campaign still waiting for an image this detaches it rather than cancelling a
    build a sibling campaign may also be waiting on.

    Args:
        campaign_id: The id from ``start_campaign``.

    Returns:
        ``{campaign_id, stopped, status, note}`` or ``{error}``.
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


def stop_job(campaign_id: str, job_name: str, reason: str = "") -> dict:
    """Kill ONE ``running`` job that will not finish; the campaign continues without it.

    Not how you end a campaign (``stop_campaign``). Refused unless the job is ``running``,
    naming its phase. **Permanent:** cut-short runs report ``status='killed'`` with *reason*
    in ``failure_message`` and count as neither pass nor failure — exclude them in your SQL
    (``status <> 'killed'``).

    Args:
        campaign_id: The id from ``start_campaign``.
        job_name: As ``list_campaign_jobs`` reports it.
        reason: Why — the only record explaining the kill later.

    Returns:
        ``{campaign_id, job_name, stopped, note}`` or ``{error}``.
    """
    from robovast.mcp_server.client_text import unescape_client_text
    try:
        client = service_access.service_client()
        if client is None:
            return {"error": NO_SERVICE}
        res = client.stop_job(campaign_id, job_name,
                              unescape_client_text(reason) or None, "mcp")
        return {"campaign_id": campaign_id, "job_name": job_name,
                "stopped": res.ok, "note": res.message}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def get_resource_usage() -> dict:
    """Can this lane run my sweep, and how long will it take? Capacity, usage, parallelism.

    Capacity **now** — what an executed run consumed is a table in its campaign's data
    (``describe_campaign_data``), not here.

    Also the way to confirm the lane is actually reachable — it reads the cluster's
    nodes, so it fails when the cluster does, which ``get_service_info``'s reported
    ``backend`` cannot tell you.

    Size a run: ``free = capacity - used``; concurrency is ``1`` when ``parallel_runs``
    is false, else ``min(⌊free_cpu / run_cpu⌋, ⌊free_mem / run_mem⌋)`` from the ``.vast``
    per-run reservations. Then ``wall_time ≈ ⌈num_runs / concurrency⌉ × per_run_time``.

        Returns:
        ``{backend, cpu_capacity, cpu_used, memory_capacity_bytes, memory_used_bytes,
        parallel_runs, jobs_running, jobs_pending}`` — cores and bytes — or ``{error}``.
        ``jobs_running``/``jobs_pending`` are what the lane is *already* busy with across
        every campaign (executing, and accepted-but-not-executing), so a lane with free
        cores and a long pending queue is not as free as it looks. ``exec_container``
        appears while an ``exec_in_container`` container is held: it can hold a stack's
        worth of memory, so a full lane names it rather than leaving you to guess.
    """
    client = service_access.service_client()
    if client is None:
        return {"error": NO_SERVICE}
    try:
        return client.resource_usage().model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def build_experiment_image(workspace_id: str = "", config_path: str = "",
                           container: str = "") -> dict:
    """Bake new code or system packages into a container's image.

    Needed only when a container needs something *inside* it. Files shipped to
    ``/config`` at runtime never need a build.

    **Optional**: ``start_campaign`` builds what it needs as its first step. Call this to
    build ahead of time. Idempotent — a no-op cache hit when nothing changed. Poll
    ``get_image_build_status``. You never handle a registry ref or credentials.

    A campaign may build **several** images, one per container that adds packages, and
    this starts them all. ``image`` is what a container starts FROM; packages are what
    it adds::

        execution:
          containers:
            sut: {image: ghcr.io/…/robovast_jazzy,
                  system_packages: [ros-jazzy-nav2-smac-planner]}

    ``python_packages`` is a list of **install groups**: a flat list is one pip pass, so
    order does not matter; nest — ``[a, b]`` — to split into layers, volatile last.

    Args:
        workspace_id: **Required** — whose project to build (as ``start_campaign``).
        config_path: Which ``.vast``, when the workspace holds several.
        container: Build only this one's image. Omit to build every one that needs it.

    Returns:
        ``{build_id, tag, cached, builds}`` or ``{error}``. ``builds`` maps each container
        to its build id; ``build_id`` names only one, so poll the rest through there.
    """
    client = service_access.service_client()
    if client is None:
        return {"error": NO_SERVICE}
    from robovast.service.interface import BuildImageRequest
    try:
        ref = client.build_image(BuildImageRequest(
            workspace_id=workspace_id, config_path=config_path,
            container=container or None))
        return {"build_id": ref.build_id, "tag": ref.tag, "cached": ref.cached,
                "builds": ref.builds}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def get_image_build_status(build_id: str) -> dict:
    """Poll an image build. On failure, ``error_detail`` says what to change.

    ``error_detail`` names the ``phase`` (apt / pip / base-image / source-build /
    base-pull / push / resource), the offending package ``entry``, a ``message``, and
    ``fixable_by`` — ``agent`` (a ``.vast`` edit fixes it) or ``infra`` (a registry
    problem no edit will fix). Read this before reaching for the builder log.

    ``phase`` says *which* field to edit, and it is not always a package list:

    \b
      pip / apt   the entry is one you declared -- fix that container's
                  ``python_packages`` / ``system_packages``.
      base-image  the entry is a dependency of something you install, missing from
                  the image you build on. Adding it to ``python_packages`` would
                  paper over the real problem -- re-pin
                  ``execution.containers.<name>.image`` instead.

    Args:
        build_id: One id from ``build_experiment_image`` — its ``build_id``, or any value
            in its ``builds`` map when the campaign builds several images.

    Returns:
        ``{build_id, tag, phase, done, cached, image_ref[, error_detail]}`` or ``{error}``.
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
    """The raw builder log. **Read ``get_image_build_status`` first** — its
    ``error_detail`` usually contains the whole story; come here for more.

    Dominated by per-layer byte counters, so it defaults to the last ``tail`` lines
    rather than the tens of thousands there are. ``summarize=True`` is the cheapest read:
    those counters collapse into one pattern. Note BuildKit writes unmarked lines, which
    the classifier rates ``warn`` — ``min_severity="error"`` is **not** how you find a
    build failure; the status is.

    Available only while the build exists (a build Job is reaped an hour after it
    finishes). The same output survives as the campaign log's ``build`` phase.

    Args:
        build_id: The id from ``build_experiment_image``.
        offset: **Byte** offset to resume from; poll with the returned ``next_offset``,
            which indexes the unfiltered stream.
        grep, tail, min_severity, summarize, top: The filters ``get_campaign_log``
            documents; ``grep="x509|denied"`` is the usual registry-failure read.
            ``tail`` defaults to 200 here, not 0.

    Returns:
        Lines: ``{text, next_offset, eof, lines, lines_total, dropped, truncated}``.
        With ``summarize``: the same minus ``text``, plus ``{patterns, patterns_total,
        severity_counts}``. Or ``{error}``.
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


def exec_in_container(command: str = "", workspace_id: str = "", config_path: str = "",
                      campaign_id: str = "", config_name: str = "",
                      keep_alive: bool = False, show_gui: bool = False,
                      tail: int = 200, container: str = "") -> dict:
    """**Test a container and its setup.** Runs a command in the experiment image.

    **Produces no campaign data** — nothing durable, no provenance, no repetitions, no
    entry in ``list_campaigns``, and no results to compare with anything. To run the
    experiment, use ``start_campaign``.

    Three questions it answers: is the image set up right (omit ``config_name`` — imports,
    ``ros2 pkg list``, file checks; build first if it declares packages); does one config run
    (name a ``config_name``; an empty ``command`` starts that config's scenario, detached);
    what does bring-up look like (the same, plus ``keep_alive`` and ``show_gui``).

    **At most one container exists at a time**, so ``reused: false`` means a fresh one —
    anything the previous container was running is gone. ``stop_container`` ends it. A started
    scenario logs to ``log_path`` *inside* the container, not ``stdout``: read it with a
    follow-up ``command="tail -200 <log_path>"``.

    What a *world* offers — its plugins, its overridable model values — is ``describe_world``
    rather than a command here.

    Args:
        command: Shell command; pipes and ``&&`` work. Empty needs ``config_name``.
        workspace_id, config_path: A workspace and which ``.vast`` in it.
        campaign_id: Use an existing campaign's ``_config/`` as the project instead — exactly
            one source, this or ``workspace_id``. A *running* campaign's container is never
            touched; to inspect a live stack, start it here.
        config_name: Stage this config. Omitted always means the bare image.
        container: ``scenario`` (default), ``simulation``, ``sut``, or an ad-hoc name.
            Asking for one this campaign lacks lists the ones it has.
        keep_alive: Leave the container running for follow-up calls.
        show_gui: Show the simulator's window on the serve host's display — **local ``vast
            serve`` on local Docker only** (see ``start_campaign`` for whose screen). Changing
            it between calls **replaces** the container, so ``reused`` is false and whatever
            the old one was running is gone.
        tail: Lines kept per stream.

    Returns:
        ``{exit_code, stdout, stderr, timed_out, duration_s, limit_s, limit_source,
        log_path, container}`` or ``{error}``. ``limit_source`` — ``command`` (fixed cap),
        ``execution.timeout``, or ``default`` (the project set none) — makes a
        ``timed_out`` result name its own remedy.
    """
    from robovast.mcp_server.log_view import view_log  # noqa: PLC0415
    from robovast.service.interface import ExecRequest  # noqa: PLC0415
    client = service_access.service_client()
    if client is None:
        return {"error": NO_SERVICE}
    try:
        result = client.exec_in_container(ExecRequest(
            command=command, workspace_id=workspace_id, config_path=config_path,
            campaign_id=campaign_id, config_name=config_name,
            keep_alive=keep_alive, show_gui=show_gui,
            container=container))
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    out = result.model_dump()
    # Trim through the same filter the log tools use, so "the last N lines" and the
    # dropped accounting mean one thing across the surface.
    for stream in ("stdout", "stderr"):
        try:
            view = view_log(out.get(stream) or "", tail=tail)
        except ValueError as e:
            return {"error": str(e)}
        out[stream] = view.get("content", "")
        if view.get("truncated"):
            out[f"{stream}_truncated"] = True
            out[f"{stream}_lines_total"] = view.get("lines_total")
    if not out.get("log_path"):
        out.pop("log_path", None)
    return out


def stop_container() -> dict:
    """Stop the held ``exec_in_container`` container. Frees the memory it holds.

    Returns:
        ``{stopped, target}`` — ``stopped: false`` when there was nothing to stop, which
        is an empty result, not an error. Or ``{error}``.
    """
    client = service_access.service_client()
    if client is None:
        return {"error": NO_SERVICE}
    try:
        return client.stop_exec_container().model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


# -- Plugin class ------------------------------------------------------------

_TOOLS = [
    start_campaign,
    get_campaign_status,
    get_campaign_log,
    list_campaign_jobs,
    get_job_log,
    stop_campaign,
    stop_job,
    get_resource_usage,
    build_experiment_image,
    wait_for_image_build,
    get_image_build_status,
    get_image_build_log,
    exec_in_container,
    stop_container,
]


class ExecutionPlugin:
    """MCP plugin: running a campaign, and watching it run."""

    name = "execution"

    def register(self, mcp: FastMCP) -> None:
        """Register all tool functions with the MCP server."""
        for fn in _TOOLS:
            mcp.tool()(fn)
