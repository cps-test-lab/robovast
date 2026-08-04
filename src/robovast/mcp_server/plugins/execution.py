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
    """**Run the experiment.** Launches a campaign in containers and returns immediately.

    This is how a RoboVAST experiment is executed — not a local ``docker compose`` or a
    script on this host, which produce no pinned image, no recorded provenance and no
    repetitions, so their output is not comparable with anything. Poll
    ``get_campaign_status``; size the lane first with ``get_resource_usage``.

    Pilot one configuration before the full sweep (``config_filter`` + ``runs=1``).

    Args:
        workspace_id: **Required** — the workspace holding the project to run. There is
            no server-side "current project". From ``list_workspaces``, or
            ``create_workspace`` + ``update_workspace``.
        config_path: Which ``.vast``, when the workspace holds several.
        config_filter: Glob selecting which configurations to run.
        runs: Runs per configuration; ``0`` uses the ``.vast`` value.
        backend: ``"local"`` (Docker on the serve host) or ``"cluster"`` (Kubernetes), on
            a service offering both. Empty uses its default lane.
        campaign_name: Override the name; the id becomes ``<name>-<timestamp>``.
        upload_to_share: Deliver a raw archive to the configured share when it finishes.
        description: **Set this every time.** One line (≤200 chars) saying what the run
            is *for* — it is what tells two same-day ``campaign-<timestamp>`` ids apart in
            ``list_campaigns`` and the web UI. Not the id, filter or run count, which are
            already recorded. Good: "pilot: 5 reps DWB vs MPPI on open_space, new
            inflation radius". Bad: "campaign run".

    Returns:
        ``{campaign_id, backend}``, or ``{error}`` — including when no service is
        reachable, which means **stop and say so**, not run the experiment another way.
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
    """Is it progressing, is it wedged, and are there results? Poll this after starting.

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


def get_campaign_log(campaign_id: str, limit: int = 200, offset: int = 0,
                     grep: str = "", tail: int = 0, min_severity: str = "",
                     summarize: bool = False, top: int = DEFAULT_TOP,
                     phase: str = "") -> dict:
    """What is the campaign doing? Its infrastructure log, in phases.

    **On a stalled or failed run, start with ``summarize=True``** — filtering cannot
    diagnose a flood, because the flood *is* the finding. One wedged run matched a
    severity filter 18226 times and the returned lines read as ordinary noise; summarized
    it is one pattern with its count.

    Phases, concatenated under ``===== PHASE =====`` dividers: ``variation`` (config
    generation), ``run`` (the controller, plus docker compose output locally),
    ``postprocessing``, and ``build`` — the image this campaign waited for.
    ``build`` is **excluded from a default read** (it is shared, content-addressed work
    and usually the largest section) but always listed in ``phases``; it is where a
    campaign that failed before it ever ran explains itself.

    Args:
        campaign_id: The id from ``start_campaign``.
        limit: Maximum lines to return. Ignored with ``summarize``.
        offset: First line to return (line offset, for paging the matches).
        grep: Keep lines matching this regex (case-insensitive), before offset/limit.
        tail: Keep only the last N of what survived the filters. Ignored with
            ``summarize``.
        min_severity: ``"warn"`` or ``"error"``, by RoboVAST's own classifier. Use this
            instead of a hand-written severity ``grep``: it is the definition the
            campaign status uses, and two patterns mean two answers to "is this healthy?".
        summarize: Return distinct **patterns with counts** instead of lines — timestamps,
            coordinates and ids are normalized so equal shapes group. Reads a 20k-line log
            for a few dozen tokens.
        top: With ``summarize``, maximum patterns (``0`` = all).
        phase: One of ``build``/``variation``/``run``/``postprocessing``/``plugin
            install``, or ``"all"``. Empty reads the campaign's own phases.
            ``phase="build", summarize=True`` reads a noisy image build cheaply.

    Returns:
        Lines: ``{file_name, phases, total_lines, returned_lines, offset, content,
        dropped}``. With ``summarize``: ``{file_name, phases, patterns, patterns_total,
        severity_counts, matched_lines, total_lines, dropped}`` — no ``content``, each
        pattern ``{pattern, count, severity, example}``. Or ``{error}``.

        ``phases`` always lists every section as ``{name, lines, included}``, so what a
        read left out is stated rather than absent.
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
    selected = all_lines[offset:offset + limit]
    result = {
        "file_name": name,
        "phases": phases,
        "total_lines": len(all_lines),
        "returned_lines": len(selected),
        "offset": offset,
        "content": "\n".join(selected),
        "dropped": view["dropped"],
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
    """The campaign's current-batch jobs, live — one run locally, one Kubernetes Job each
    on the cluster. Pair with ``get_job_log`` to read a running one.

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
    """What is one **running** job doing? Its containers' live stdout/stderr.

    **This is what a stalled status points at. Call it with ``summarize=True`` first:**
    a run that cannot reach its goal usually says so by repeating one message thousands
    of times, which is a single line here.

    Live source only — a finished job whose pod was garbage-collected has none; read the
    campaign log instead. On the cluster every container in the pod is merged in
    timestamp order, each line tagged ``[<container>]`` when there is more than one.

    Args:
        campaign_id: The id from ``start_campaign``.
        job_name: A ``job_name`` from ``list_campaign_jobs``.
        offset: **Byte** offset to resume from — pass back the previous call's
            ``next_offset`` to poll incrementally. It indexes the *unfiltered* stream, so
            filtering never breaks a poll loop.
        grep, tail, min_severity, summarize, top: The filters ``get_campaign_log``
            documents, applied in that order.

    Returns:
        Lines: ``{text, next_offset, eof, lines, lines_total, dropped, truncated}``.
        With ``summarize``: the same minus ``text``, plus ``{patterns, patterns_total,
        severity_counts}``. Or ``{error}``.
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


def get_resource_usage(backend: str = "") -> dict:
    """Can this lane run my sweep, and how long will it take? Capacity, usage, parallelism.

    Also the way to confirm a lane is actually reachable — it reads the cluster's nodes,
    so it fails when the cluster does, which ``get_service_info``'s configured
    ``backends`` list cannot tell you.

    Size a run: ``free = capacity - used``; concurrency is ``1`` when ``parallel_runs``
    is false, else ``min(⌊free_cpu / run_cpu⌋, ⌊free_mem / run_mem⌋)`` from the ``.vast``
    per-run reservations. Then ``wall_time ≈ ⌈num_runs / concurrency⌉ × per_run_time``.

    Args:
        backend: ``"local"`` or ``"cluster"`` on a service offering both; empty uses its
            default lane.

    Returns:
        ``{backend, cpu_capacity, cpu_used, memory_capacity_bytes, memory_used_bytes,
        parallel_runs}`` — cores and bytes — or ``{error}``.
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
    """Bake new code or system packages into the experiment image, from ``build:``.

    Needed only when the experiment needs something *in the container* — a new
    ``sim_suite`` package, an apt dependency. Files shipped to ``/config`` at runtime
    (``run_files``, ``scenario_file``) never need a build.

    **Optional**: ``start_campaign`` (re)builds a ``build:<tag>`` image as its first step.
    Call this to build ahead of time. Idempotent — a no-op cache hit when nothing changed.
    Poll ``get_image_build_status``. You never handle a registry ref or credentials.

    Declare it in the ``.vast``::

        build:
          system_packages: [ros-jazzy-nav2-smac-planner]  # apt
          python_packages: [packages/sim_suite_mobile]    # source dir / pip spec / wheel
          tag: sim-suite-mobile
        execution:
          image: build:sim-suite-mobile

    ``python_packages`` is a list of **install groups**: a flat list is one pip pass, so
    its order does not matter (a local wheel resolves its siblings from the same pass).
    Nest — ``[a, b]`` — to split it into layers, with what changes most often **last**;
    a change then only rebuilds that group onward.

    Args:
        workspace_id: **Required** — whose project to build (as ``start_campaign``).
        config_path: Which ``.vast``, when the workspace holds several.
        backend: Build for the lane you will run on — ``"local"`` or ``"cluster"``.

    Returns:
        ``{build_id, tag, cached}`` or ``{error}``.
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
    """Poll an image build. On failure, ``error_detail`` says what to change.

    ``error_detail`` names the ``phase`` (apt / pip / source-build / base-pull / push /
    resource), the offending ``build:`` ``entry``, a ``message``, and ``fixable_by`` —
    ``agent`` (edit the ``build:`` section) or ``infra`` (a registry/base problem no
    ``.vast`` edit will fix). Read this before reaching for the builder log.

    Args:
        build_id: The id from ``build_experiment_image``.

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


# -- Plugin class ------------------------------------------------------------

_TOOLS = [
    start_campaign,
    get_campaign_status,
    get_campaign_log,
    list_campaign_jobs,
    get_job_log,
    stop_campaign,
    get_resource_usage,
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
