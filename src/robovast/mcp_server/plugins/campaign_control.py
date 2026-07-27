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

"""MCP plugin for driving campaigns: validate, start, monitor, and stop.

Unlike the read-only result plugins, these tools launch and kill real compute.
They are a **strict client of a running ``robovast-service``** (a ``vast serve``
locally, or a tunnel / ``vast serve --attach`` to a remote VM or cluster): the
service is the single execution authority and owns run-state tracking, so there
is no separate ``vast exec`` subprocess path here. When no service is reachable
the control tools fail loudly rather than silently running a divergent local
lane. (Users who want a serviceless local run still have ``vast exec local run``.)

.. warning::

   The MCP server has no authentication. These tools are registered
   unconditionally, so only expose the server on a trusted network (it binds
   ``127.0.0.1`` by default for this reason).
"""

import logging

from fastmcp import FastMCP

from robovast.mcp_server import results_resolver

logger = logging.getLogger(__name__)

#: Canonical failure when no ``robovast-service`` answers on the conventional local
#: port. The control tools drive the service, so without one they cannot act.
_NO_SERVICE = ("no robovast-service reachable — start a 'vast serve' (local) or "
               "'vast serve --attach' (cluster), or open a tunnel to one, so the "
               "MCP has an execution authority to drive")


# -- Helpers -----------------------------------------------------------------


def _service_client():
    """Return a ``RobovastClient`` bound to a reachable robovast-service, or None.

    A service answers on the conventional local port when a ``vast serve`` runs
    locally, or a tunnel / ``vast serve --attach`` reaches a remote VM or cluster.
    The control tools drive that service through the
    :class:`~robovast.service.interface.RobovastInterface` contract; when nothing
    answers they return :data:`_NO_SERVICE` (fail loudly — there is no local
    subprocess fallback).
    """
    from robovast.common.cli.service_target import detected_service_url
    url = detected_service_url()
    if not url:
        return None
    from robovast.service.client import RobovastClient
    return RobovastClient(url)


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
    return result


# -- Tool functions ----------------------------------------------------------


def validate_project(config_path: str) -> dict:
    """Validate a RoboVAST project (``.vast`` file), reporting ALL problems at once.

    A ``.vast`` file defines a *project* (a campaign is one execution of it). This
    checks the whole file — YAML, schema, the scenario file, scenario-parameter
    references, and every plugin reference (variation types and their parameters,
    the ``results_processing``/``search`` postprocessing commands, and the search
    strategy/extractor), whether installed entry-point names or local
    ``./path.py:Class`` file refs — and returns **every** problem it finds in one
    pass, each tagged with the config block and field, so the file can be fixed in
    as few iterations as possible. When valid, it also returns the config/run
    counts (same math as ``vast config info``). Same collect-all core as the
    ``vast configuration validate`` CLI command.

    Reads the ``.vast`` straight off disk — no workspace, no service, and no
    initialized project needed, so it works before anything else exists.

    Args:
        config_path: Path to the ``.vast`` file. Required: there is no server-side
            "current project" to fall back to, and guessing one would validate a
            different file than the caller named.

    Returns:
        ``{valid, configs, runs_per_config, total_trials, problems}`` where each
        problem is ``{stage, config, field, message}``.
    """
    from robovast.common.config_validation import validate_project_file
    try:
        return validate_project_file(config_path)
    except Exception as e:  # noqa: BLE001 - surface any resolution error to the client
        return {"valid": False, "configs": 0, "runs_per_config": 0,
                "total_trials": 0,
                "problems": [{"stage": "project", "config": None,
                              "field": None, "message": str(e)}]}


def preview_configurations(config_path: str, max_configs: int = 0) -> dict:
    """Preview the resolved configurations a ``.vast`` would generate — WITHOUT running.

    ``validate_project`` returns only the counts; this returns the actual resolved
    per-configuration parameter sets, so you can eyeball what each variation cell
    expands to before starting a campaign (the read-only, in-memory equivalent of
    ``vast configuration generate`` / ``vast exec local prepare-run``, which stage
    the same tree to disk). Nothing is executed and nothing is written.

    Reads the ``.vast`` straight off disk — no workspace, service, or initialized
    project needed.

    Args:
        config_path: Path to the ``.vast`` file. Required, for the same reason as
            ``validate_project``: there is no server-side "current project".
        max_configs: Cap the number of configurations returned (``0`` = all). The
            ``configs`` count always reflects the true total; ``truncated`` marks
            when the returned list was shortened.

    Returns:
        ``{configs, runs_per_config, total_trials, configurations, truncated}``
        where each configuration is ``{name, parameters}`` and ``parameters`` is
        the resolved parameter-name → value mapping for that cell. On failure,
        ``{error}``.
    """
    from robovast.common.config_generation import generate_scenario_variations
    try:
        campaign_data, _ = generate_scenario_variations(
            variation_file=config_path, output_dir=None)
        configs = campaign_data["configs"]
        runs = campaign_data.get("execution", {}).get("runs", 1)
        items = [{"name": c["name"], "parameters": c.get("config", {})}
                 for c in configs]
        truncated = bool(max_configs) and len(items) > max_configs
        if truncated:
            items = items[:max_configs]
        return {
            "configs": len(configs),
            "runs_per_config": runs,
            "total_trials": len(configs) * runs,
            "configurations": items,
            "truncated": truncated,
        }
    except Exception as e:  # noqa: BLE001 - surface any resolution error to the client
        return {"error": str(e)}


def start_campaign(config_filter: str = "", runs: int = 0, backend: str = "",
                   workspace_id: str = "", config_path: str = "",
                   campaign_name: str = "", upload_to_share: bool = False) -> dict:
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

    Returns:
        ``{campaign_id, backend}`` on success; ``{error}`` when no service is reachable
        or the start is refused.
    """
    try:
        client = _service_client()
        if client is None:
            return {"error": _NO_SERVICE}
        if backend and backend not in ("local", "cluster"):
            return {"error": f"unknown backend {backend!r}; use 'local' or 'cluster'"}
        from robovast.service.interface import CreateCampaignRequest
        ref = client.create_campaign(CreateCampaignRequest(
            workspace_id=workspace_id, config_path=config_path,
            config_filter=config_filter, campaign_name=campaign_name,
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
    """Report the service's live status and progress for a campaign.

    Args:
        campaign_id: The id returned by :func:`start_campaign`.

    Returns:
        ``{campaign_id, backend, status, mode, batch_runs_done, batch_runs_total,
        progress, ...}`` — plus search-only fields (``best_objective``, ``budget``,
        ``batches_done``, ``stop``) when applicable; ``{error}`` when no service is
        reachable or the campaign is unknown. Run counts are **batch-scoped**;
        ``progress`` is overall and mode-aware (``None`` when a search's completion
        cannot be known yet).
    """
    try:
        client = _service_client()
        if client is None:
            return {"error": _NO_SERVICE}
        st = client.get_status(campaign_id)
        result = _status_to_dict(campaign_id, "service", st)
        result["stage"] = st.stage or ""  # a live marker string, not a log tail
        return result
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def get_campaign_log(campaign_id: str, lines: int = 200, offset: int = 0,
                     grep: str = "") -> dict:
    """Read a campaign's unified infrastructure log.

    Returns the campaign's whole infrastructure log — the same divider-separated
    stream the web UI log panel shows — assembled from the per-phase files under
    ``_execution/`` in phase order, each under a ``===== PHASE =====`` divider:

    * **VARIATION** — config generation / composition (incl. plugin subprocess output).
    * **RUN** — the controller driving batches/runs (``controller.log``). For local
      Docker campaigns this also includes the ``run.sh`` / ``docker compose`` output.
    * **POSTPROCESSING** — rosbag→CSV→``data.db`` (on the cluster, the separate
      conversion Job's output followed by the host stage).

    A phase's section is absent until that phase has produced output.

    Filtered by :func:`~robovast.mcp_server.log_view.view_log` — the same ``grep``
    control every log tool takes. Each line of a run's output arrives stamped with the
    relay prefix of whatever forwarded it (``robovast  | [INFO] [<ts>]
    [scenario_execution_ros]: ``); that prefix is dropped where the payload carries its
    own level and timestamp, which is most of them.

    Args:
        campaign_id: The id returned by :func:`start_campaign`.
        lines: Maximum number of lines to return (default 200).
        offset: Line offset to start reading from (default 0), for pagination.
        grep: Keep only lines matching this regex (case-insensitive). Applied before
            ``offset``/``lines``, so paging walks the matches.

    Returns:
        ``{file_name, total_lines, returned_lines, offset, content, dropped}``;
        ``{error}`` if the campaign is unknown or ``grep`` is not a valid regex.
    """
    from robovast.common.campaign_logs import assemble_log_from_dir  # noqa: PLC0415
    from robovast.mcp_server.log_view import view_log  # noqa: PLC0415
    campaign_dir = results_resolver.resolve_campaign_path(campaign_id)
    # Assemble the full unified text (byte offset 0), then paginate by lines — the
    # MCP tool's ``offset`` is a line offset, unlike the service's byte offset.
    text, _, _ = assemble_log_from_dir(campaign_dir, offset=0, eof=True)
    try:
        view = view_log(text, grep=grep)
    except ValueError as e:
        return {"error": str(e)}
    all_lines = view["content"].splitlines()
    selected = all_lines[offset:offset + lines]
    return {
        "file_name": f"{campaign_id} (infrastructure log)",
        "total_lines": len(all_lines),
        "returned_lines": len(selected),
        "offset": offset,
        "content": "\n".join(selected),
        "dropped": view["dropped"],
    }


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
        pending, completed, failed, blocked, total}}`` where ``status`` is one of
        ``running`` / ``pending`` / ``completed`` / ``failed`` / ``blocked``. A
        ``blocked`` job cannot start and will not recover on its own (e.g. an image
        that can't be pulled); ``detail`` carries the Kubernetes reason + message.
        Returns ``{error}`` if no service is reachable.
    """
    client = _service_client()
    if client is None:
        return {"error": "no robovast-service reachable (bring up a 'vast serve' or "
                         "a tunnel before starting MCP); live job listing is served "
                         "by the service"}
    try:
        return client.list_jobs(campaign_id).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def get_job_log(campaign_id: str, job_name: str, offset: int = 0,
                grep: str = "", tail: int = 0) -> dict:
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

    ``grep`` / ``tail`` filter the returned text (see
    :func:`~robovast.mcp_server.log_view.view_log`); ``next_offset`` still refers to the
    **unfiltered** stream, so incremental polling stays correct.

    Args:
        campaign_id: The id returned by :func:`start_campaign`.
        job_name: A ``job_name`` from :func:`list_campaign_jobs`.
        offset: Byte offset to resume from (default 0).
        grep: Keep only lines matching this regex (case-insensitive).
        tail: Keep only the last N matching lines (``0`` = all).

    Returns:
        ``{text, next_offset, eof, lines, lines_total, dropped, truncated}``;
        ``{error}`` if no service is reachable, the job's live log source is gone, or
        ``grep`` is not a valid regex.
    """
    from robovast.mcp_server.log_view import view_log  # noqa: PLC0415
    client = _service_client()
    if client is None:
        return {"error": "no robovast-service reachable (bring up a 'vast serve' or "
                         "a tunnel before starting MCP); live job logs are served "
                         "by the service"}
    try:
        chunk = client.get_job_log(campaign_id, job_name, offset).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    try:
        view = view_log(chunk.get("text", ""), grep=grep, tail=tail)
    except ValueError as e:
        return {"error": str(e)}
    return {**chunk, "text": view["content"], "lines": view["lines"],
            "lines_total": view["lines_total"], "dropped": view["dropped"],
            "truncated": view["truncated"]}


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
        client = _service_client()
        if client is None:
            return {"error": _NO_SERVICE}
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
        client = _service_client()
        if client is None:
            return {"error": _NO_SERVICE}
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
    client = _service_client()
    if client is None:
        return {"error": _NO_SERVICE}
    try:
        return client.resource_usage(backend or None).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


# -- Plugin class ------------------------------------------------------------

def get_postprocessing(campaign_id: str) -> dict:
    """Show a campaign's effective analysis-postprocessing entries + edit history.

    Raw rosbags are always preserved, so postprocessing can be edited and re-run
    to compute *different* metrics later without re-executing the campaign. The
    immutable ``_config/`` snapshot is never changed; edits are versioned
    overrides. Pair with :func:`update_postprocessing` + :func:`run_postprocessing`.

    Returns:
        ``{campaign_id, source, entries, revisions}`` or ``{error}``.
    """
    from robovast.common.cli.service_target import detected_service_url
    from robovast.service.client import RobovastClient
    try:
        return RobovastClient(detected_service_url()) \
            .get_postprocessing(campaign_id).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def update_postprocessing(campaign_id: str, entries: list) -> dict:
    """Replace a campaign's analysis-postprocessing entries (a new versioned override).

    ``entries`` is a list of postprocessing commands — a bare plugin name
    (``"rosbags_to_csv"``) or a single-key dict with params
    (``{"command": {"script": "postprocess.sh"}}``). Validated before writing;
    the ``_config/`` snapshot is untouched. Call :func:`run_postprocessing` to apply.

    Returns:
        ``{campaign_id, revision, entries}`` or ``{error}``.
    """
    from robovast.common.cli.service_target import detected_service_url
    from robovast.service.client import RobovastClient
    from robovast.service.interface import UpdatePostprocessingRequest
    try:
        return RobovastClient(detected_service_url()) \
            .update_postprocessing(UpdatePostprocessingRequest(
                campaign_id=campaign_id, entries=entries)).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def run_postprocessing(campaign_id: str, force: bool = False,
                       skip: list | None = None) -> dict:
    """(Re)run analysis postprocessing for one campaign, rebuilding ``data.db``.

    **Dispatched in the background** — returns as soon as the run is started (it can take
    minutes to hours). The campaign enters the ``postprocessing`` phase; poll
    :func:`get_campaign_status` for progress and the outcome (``postprocessed`` /
    ``postprocessing_error``). Reprocesses just this campaign (not its siblings), reading
    its own ``_config/<name>.vast``. Returns ``{ok, message}`` where *message* confirms
    the dispatch, or ``ok=false`` if an operation is already running for the campaign.

    Args:
        campaign_id: The campaign to (re)process.
        force: Bypass per-rosbag caches and reprocess all bags.
        skip: Plugin names to skip (e.g. ``["rosbags_to_webm"]``).
    """
    from robovast.common.cli.service_target import detected_service_url
    from robovast.service.client import RobovastClient
    from robovast.service.interface import RunPostprocessingRequest
    try:
        return RobovastClient(detected_service_url()) \
            .run_postprocessing(RunPostprocessingRequest(
                campaign_id=campaign_id, force=force, skip=skip or [])).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def run_share(campaign_id: str) -> dict:
    """(Re)trigger the upload-to-share of one finished campaign's raw archive.

    **Dispatched in the background** — returns as soon as the upload is started; the
    campaign enters the ``sharing`` phase, so poll :func:`get_campaign_status` for the
    outcome (``share_error`` on failure). Works from disk with no live campaign (usable
    after a `vast serve` restart). The target provider comes from the service environment
    (``ROBOVAST_SHARE_TYPE`` + credentials): adjust it and re-trigger to upload to a
    different provider. Fails loudly if no share provider is configured.

    Args:
        campaign_id: The finished campaign to (re)upload.
    """
    from robovast.common.cli.service_target import detected_service_url
    from robovast.service.client import RobovastClient
    from robovast.service.interface import RunShareRequest
    try:
        return RobovastClient(detected_service_url()) \
            .run_share(RunShareRequest(campaign_id=campaign_id)).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def cleanup_campaign_data(campaign_id: str = "", force: bool = False) -> dict:
    """Delete campaign result data (object-store bucket(s)) for a cluster campaign.

    Frees storage once results have been downloaded or published and are no longer
    needed. This goes **through the robovast-service**, which owns the object-store
    credentials and knows which campaigns are still live — so there is **no
    infrastructure to deal with** here: no kubeconfig, no S3 keys, no namespaces.

    Args:
        campaign_id: The campaign whose data to delete. Empty string deletes **all**
            finished campaigns' data (campaigns still running are always skipped).
        force: Delete a named campaign even if the service still considers it live.

    Returns:
        ``{ok, message}`` (``message`` reports how many buckets were removed), or
        ``{error}`` if no service is reachable / the backend has no object store.
    """
    from robovast.service.interface import CleanupDataRequest
    client = _service_client()
    if client is None:
        return {"error": "no robovast-service reachable (bring up a 'vast serve' or "
                         "a tunnel before starting MCP); campaign data lives in the "
                         "service's object store"}
    try:
        res = client.cleanup_campaign_data(
            CleanupDataRequest(campaign_id=campaign_id or None, force=force))
        return {"ok": res.ok, "message": res.message}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def delete_campaign(campaign_id: str) -> dict:
    """Permanently delete **one** campaign wholesale, through the robovast-service.

    Removes the campaign's durable home — its local directory on a local service,
    or its object-store data (plus any leftover Kubernetes Jobs and the service's
    cache) on a cluster service. This is the full "forget this campaign" action, as
    opposed to :func:`cleanup_campaign_data`, which only frees object-store buckets.

    The service refuses a campaign that is still running — stop it first with
    :func:`stop_campaign`. The external share copy (if any) is never touched. This
    is irreversible.

    Args:
        campaign_id: The campaign to delete.

    Returns:
        ``{ok, message}`` on success, or ``{error}`` if no service is reachable or
        the campaign is still running.
    """
    client = _service_client()
    if client is None:
        return {"error": "no robovast-service reachable (bring up a 'vast serve' or "
                         "a tunnel before starting MCP)"}
    try:
        res = client.delete_campaign(campaign_id)
        return {"ok": res.ok, "message": res.message}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def get_campaign_download(campaign_id: str) -> dict:
    """Return **where to download** a campaign — a web link, not a file on this host.

    Downloading is a browser action: the campaign archive is served by the
    robovast-service (and its web UI) at a fixed path, so this returns that URL for
    **you** to open where your robovast web UI runs — it never writes a file onto the
    MCP-server host (which you may not be able to reach if the server runs elsewhere).

    Args:
        campaign_id: The campaign id to download.

    Returns:
        For a **cluster** service: ``{campaign_id, url, path, note}`` — ``url`` is the
        postprocessed ``tar.gz`` (full campaign, incl. derived data) streamed from the
        object store. For a **local** service: ``{campaign_id, note}`` — the results
        already live on the service host's filesystem, so there is no HTTP download.
        ``{error}`` when no service is reachable.
    """
    client = _service_client()
    if client is None:
        return {"error": "no robovast-service reachable; bring up a 'vast serve' or a "
                         "tunnel (the campaign lives in the service, not on this host)"}
    try:
        backend = client.version().backend
    except Exception as e:  # noqa: BLE001
        return {"error": f"could not reach the service: {e}"}

    path = f"/campaigns/{campaign_id}/archive"
    if backend == "kubernetes":
        return {
            "campaign_id": campaign_id,
            "url": f"{client.base_url}{path}",
            "path": path,
            "note": ("Open this in the browser where your robovast web UI runs "
                     "(or Monitor → Download), or run "
                     f"'vast results download -i {campaign_id}' on your own machine "
                     "('--variant raw' for the pre-postprocess archive from the share)."),
        }
    return {
        "campaign_id": campaign_id,
        "note": ("This is a local service — the campaign results are already on the "
                 "service host's filesystem; there is no HTTP download."),
    }


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
    client = _service_client()
    if client is None:
        return {"error": _NO_SERVICE}
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
    client = _service_client()
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
                        tail: int = 200) -> dict:
    """Return the builder log from byte *offset* onward, filtered for reading.

    **For a failure, read :func:`get_image_build_status` first** — its ``error`` names
    the phase and its ``log_tail`` usually contains the whole story. Come here when you
    need more than that tail.

    A builder log is dominated by per-layer byte counters, so this defaults to the last
    ``tail`` lines rather than the entire stream (which runs to tens of thousands of
    lines). Narrow it with ``grep`` — e.g. ``grep="error|x509|denied"`` — or page with
    ``offset``; ``grep`` / ``tail`` are the same controls the other log tools take (see
    :func:`~robovast.mcp_server.log_view.view_log`).

    Streaming: poll from ``0``, append ``text``, resume from the returned
    ``next_offset`` (a byte offset into the **unfiltered** stream, so filtering never
    breaks a poll loop); ``eof`` is true once the build is done.

    Args:
        build_id: The id returned by :func:`build_experiment_image`.
        offset: Byte offset to resume from.
        grep: Keep only lines matching this regex (case-insensitive).
        tail: Keep only the last N matching lines (default 200; ``0`` = all).

    Returns:
        ``{text, next_offset, eof, lines, lines_total, dropped, truncated}``;
        ``{error}`` if no service is reachable or ``grep`` is not a valid regex.
    """
    from robovast.mcp_server.log_view import view_log  # noqa: PLC0415
    client = _service_client()
    if client is None:
        return {"error": "no robovast-service reachable"}
    try:
        chunk = client.get_image_build_log(build_id, offset)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    try:
        view = view_log(chunk.text, grep=grep, tail=tail)
    except ValueError as e:
        return {"error": str(e)}
    return {"text": view["content"], "next_offset": chunk.next_offset,
            "eof": chunk.eof, "lines": view["lines"],
            "lines_total": view["lines_total"], "dropped": view["dropped"],
            "truncated": view["truncated"]}


_TOOLS = [
    validate_project,
    preview_configurations,
    start_campaign,
    build_experiment_image,
    get_image_build_status,
    get_image_build_log,
    get_campaign_status,
    get_campaign_log,
    list_campaign_jobs,
    get_job_log,
    stop_campaign,
    list_running_campaigns,
    resource_usage,
    get_postprocessing,
    update_postprocessing,
    run_postprocessing,
    run_share,
    cleanup_campaign_data,
    delete_campaign,
    get_campaign_download,
]


class CampaignControlPlugin:
    """Expose campaign control (validate/start/status/stop/list) as MCP tools."""

    name = "campaign_control"

    def register(self, mcp: FastMCP) -> None:
        """Register all tool functions with the MCP server."""
        for fn in _TOOLS:
            mcp.tool()(fn)
