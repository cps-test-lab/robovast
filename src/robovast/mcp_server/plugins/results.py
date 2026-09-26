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

"""MCP plugin: reading what a campaign did — read-only SQL, plus the run artifacts a
program has to be run over.

Two flat views (``run_view``, ``config_view``) plus the metric tables answer the per-run,
per-configuration and aggregate questions, rather than a tool each. A per-scope metadata
tool would parse ``metadata.yaml``, which only postprocessing writes, so it would report
"run postprocessing first" about campaigns whose outcomes are already recorded in
``campaign.db``.

What stays a dedicated tool is the campaign listing (it spans campaigns) and the one
aggregate asked constantly, itself computed over the same SQL. Campaign **files** are read
through the address space (``/results/<campaign_id>/<path>``) — with one exception, which is
why this module is not only SQL: **looking at** a run means decoding a recording, and a
decoder takes a path rather than an address. Those tools return an image, so they *raise*
where the SQL ones return ``{"error": ...}`` — an image response has no dict to carry one.
"""

import json
import logging
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import quote

from fastmcp import Context, FastMCP
from fastmcp.utilities.types import Image

from robovast.mcp_server import data_access, run_artifacts, service_access
from robovast.mcp_server.lacks import lacks

logger = logging.getLogger(__name__)

#: Page size used when the whole list has to be walked (``running_only``). The service
#: pages *before* the filter can be applied, so asking for the caller's ``limit`` would
#: filter a window instead of the list — a long-running campaign started last week would
#: drop out of "what is running now" simply for not being among the 20 newest.
_WALK_PAGE = 200


def _summary_to_dict(summary) -> dict:
    """Render a service ``CampaignSummary`` into the MCP listing entry.

    ``description`` and ``finished_at`` are omitted when empty rather than reported as
    ``""``/null: a campaign started without a description has none, which is not the same
    fact as "the description is the empty string".

    ``paused`` and ``priority`` are carried the same way -- only when they are not the
    default -- because a held campaign is the one case where no progress is not a fault.
    Without them a campaign somebody parked is indistinguishable here from one that is
    wedged, and the reasonable next move (diagnose it, or start it again) is the wrong one.

    ``mode`` is carried because this listing is the only view an agent has: without it a
    search and a sweep are indistinguishable here, and a search is read with different
    queries (``run_view``'s ``batch``/``objective``/``paramset_id``). ``num_composition_failed``
    and ``num_no_sample`` come along for the same reason — a search whose draws never
    composed, or never scored, has ``num_runs`` telling only part of that.
    """
    entry = {
        "campaign_id": summary.campaign_id,
        "status": summary.phase,
        "mode": summary.mode,
        "started_at": summary.started_at,
        "postprocessed": summary.postprocessed,
        "num_runs": summary.num_runs,
        "num_passed": summary.num_passed,
        "num_failed": summary.num_failed,
        "num_composition_failed": summary.num_composition_failed,
        "num_no_sample": summary.num_no_sample,
    }
    # Omitted when not recorded, like ``finished_at``: a running or unmeasured campaign has
    # no size, which is a different fact from a size of 0.
    if summary.results_bytes is not None:
        entry["results_bytes"] = summary.results_bytes
    if summary.description:
        entry["description"] = summary.description
    if summary.finished_at:
        entry["finished_at"] = summary.finished_at
    if summary.paused:
        entry["paused"] = True
    if summary.priority:
        entry["priority"] = summary.priority
    return entry


def _walk_all(client, sort: str, order: str) -> list:
    """Every campaign summary the service knows, in the service's order (live first,
    then by *sort*/*order*).

    Only for ``running_only``. The service now leads with the live campaigns, so the
    first page usually holds them all — but "usually" is not an answer to "which are
    running", and nothing bounds their number, so this still walks every page.
    """
    from robovast.service.interface import ListCampaignsRequest
    out: list = []
    offset = 0
    while True:
        page = client.list_campaigns(
            ListCampaignsRequest(limit=_WALK_PAGE, offset=offset, sort=sort, order=order))
        out.extend(page.campaigns)
        offset += _WALK_PAGE
        if offset >= page.total or not page.campaigns:
            return out


def list_campaigns(limit: int = 20, offset: int = 0,
                   running_only: bool = False,
                   sort: Literal["recent", "size"] = "recent",
                   order: Literal["desc", "asc"] = "desc") -> dict:
    """What has been run? Live campaigns first, then newest first.

    Args:
        limit: Maximum campaigns to return.
        offset: Campaigns to skip (campaign index).
        running_only: Only live campaigns, however old; ``total`` counts
            them.
        sort: ``size`` orders by ``results_bytes``, unknown last; live ones lead.

    Returns:
        ``{campaigns, total, offset, source}`` — each campaign ``{campaign_id, status,
        mode, started_at, postprocessed, num_runs, num_passed, num_failed,
        num_composition_failed, num_no_sample}`` plus ``description``, ``finished_at`` and
        ``results_bytes`` where recorded, and ``paused``/``priority`` where either is not the default (a
        held campaign makes no progress on purpose) — or ``{error}``. ``mode`` is ``search`` or ``batch``; a search is
        read by its cells (``run_view``'s ``batch``/``paramset_id``/``objective``).

        ``description`` is what its launcher said the run was for, and is usually the
        only thing telling two same-day ``campaign-<timestamp>`` ids apart.
        ``postprocessed`` says whether the metric tables exist; per-run *outcomes* are
        queryable either way (``run_view``). ``source`` names who answered — the service,
        or this host's results root when none is reachable, since "no campaigns" means
        different things from the two.
    """
    from pydantic import ValidationError

    from robovast.service.interface import ListCampaignsRequest
    try:
        # Built first so a value outside the vocabulary is refused before anything is asked.
        request = ListCampaignsRequest(limit=limit, offset=offset, sort=sort, order=order)
    except ValidationError as e:
        return {"error": str(e)}
    client = service_access.service_client()
    if client is None:
        return {"error": service_access.NO_SERVICE}
    source = "service"
    try:
        if running_only:
            from robovast.execution.control_server import is_running
            matched = [c for c in _walk_all(client, request.sort, request.order)
                       if is_running(c.phase)]
            total = len(matched)
            window = matched[offset:offset + limit]
        else:
            page = client.list_campaigns(request)
            total = page.total
            window = page.campaigns
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    return {
        "campaigns": [_summary_to_dict(c) for c in window],
        "total": total,
        "offset": offset,
        "source": source,
    }


def get_campaign_summary(campaign_id: str) -> dict:
    """Did it pass? Configuration/run counts, pass-fail tallies, and provenance.

    The one aggregate worth a dedicated tool; works before postprocessing. For anything
    more specific — a run, a configuration's parameters, a metric — write SQL.

    Args:
        campaign_id: Campaign name, e.g. ``campaign-2026-03-04-152130``.

    Returns:
        ``{campaign_id, num_configs, num_runs, num_success, num_failed, num_unknown,
        num_killed, worst_configs, advice}`` plus the execution provenance (which
        robovast, image, backend) once the campaign has produced it; or ``{error}``.

        ``retrigger`` says whether this campaign can be re-run, per axis (config version,
        container protocol, images, plugins, providers). Read it before
        ``start_campaign(from_campaign=…)``: a ``blocking`` axis names what is missing,
        and ``unknown`` means the campaign predates that record, not that it failed.

        ``num_configs`` is the configurations the campaign was **composed with**, not the
        ones that came back: a declared configuration that produced no runs is counted
        here and named in ``missing_configs``, with ``num_missing_configs`` and a ``note``
        saying the design is short. Without that a lost cell is indistinguishable from a
        smaller campaign that ran perfectly, and every aggregate below is over a partial
        design while looking complete. ``num_runs`` counts runs, so those cells add
        nothing to it.

        ``num_killed`` counts runs an operator stopped by hand (``stop_job``). They are
        **not** in ``num_failed``: nobody learned anything about the system under test
        from them, so they are missing measurements rather than negative results, and a
        config is not ranked ``worst`` for carrying them. ``num_invalid`` counts runs the
        runner discarded after a container restarted under them, on the same terms and for
        a sharper reason: such a run may have written a *passing* verdict, against a
        process that had lost its state.

        ``container_failures`` appears when a container died and was restarted: what died,
        on which node, of what signal. It is reported **even when there is no run data at
        all**, because a campaign that dies mid-batch records no runs and never
        postprocesses -- and that is precisely the campaign whose failure needs explaining.
        ``query_campaign_data_sql`` over ``container_failure_view`` has the detail,
        including the dead container's own last log lines.

        ``advice`` is what this campaign's measurements say the next one should reserve --
        cpu and memory, per container and per pod, with the evidence behind each item.
        Empty when there is nothing worth saying. Each item carries a plain-text ``title``
        and ``detail``, so it can be reported as-is without knowing its ``kind``.
    """
    # COUNT(run_id), not COUNT(*): run_view carries one run-less row for each cell that
    # produced nothing, so that a declared configuration is in the record whether or not it
    # ran. Counting rows would report those as runs and hide the very shortfall they exist
    # to show.
    per_config = data_access.rows(campaign_id, """
        SELECT config_name,
               COUNT(run_id)                                   AS num_runs,
               SUM(CASE WHEN status = 'passed' THEN 1 ELSE 0 END) AS success,
               SUM(CASE WHEN status IN ('failed', 'error') THEN 1 ELSE 0 END) AS failed,
               SUM(CASE WHEN status = 'unknown' THEN 1 ELSE 0 END) AS unknown,
               SUM(CASE WHEN status = 'killed' THEN 1 ELSE 0 END) AS killed,
               SUM(CASE WHEN status = 'invalid' THEN 1 ELSE 0 END) AS invalid,
               SUM(CASE WHEN status = 'missing' THEN 1 ELSE 0 END) AS missing,
               SUM(CASE WHEN status = 'composition_failed' THEN 1 ELSE 0 END)
                                                               AS composition_failed
        FROM run_view GROUP BY config_name ORDER BY config_name
    """)
    container_failures = _container_failures(campaign_id)
    if not per_config:
        # A campaign that died mid-batch has no run rows -- and is exactly the campaign
        # someone is asking this about. Answering "no run data" there is a refusal at the
        # moment the tool is most useful, so say what killed it instead.
        if container_failures:
            return {"campaign_id": campaign_id, "num_configs": 0, "num_runs": 0,
                    "num_container_failures": len(container_failures),
                    "container_failures": container_failures,
                    "note": "This campaign recorded no runs, but a container died under "
                            "it. See container_failure_view for the full evidence."}
        return {"error": f"No run data for campaign {campaign_id!r}. It may not have "
                         "started, or have no campaign.db — check list_campaigns() and "
                         "get_campaign_status()."}

    def _int(value: Any) -> int:
        return int(value or 0)

    configs_info = [{
        "name": c.get("config_name"),
        "num_runs": _int(c.get("num_runs")),
        "success": _int(c.get("success")),
        "failed": _int(c.get("failed")),
        "unknown": _int(c.get("unknown")),
        **({"killed": _int(c.get("killed"))} if _int(c.get("killed")) else {}),
        **({"invalid": _int(c.get("invalid"))} if _int(c.get("invalid")) else {}),
        **({"missing": True} if _int(c.get("missing")) else {}),
        **({"composition_failed": True} if _int(c.get("composition_failed")) else {}),
    } for c in per_config]

    result: dict[str, Any] = {
        "campaign_id": campaign_id,
        "num_configs": len(configs_info),
        "num_runs": sum(c["num_runs"] for c in configs_info),
        "num_success": sum(c["success"] for c in configs_info),
        "num_failed": sum(c["failed"] for c in configs_info),
        "num_unknown": sum(c["unknown"] for c in configs_info),
        # Ranked on failures and lost results only. A deliberate kill is not evidence
        # against a config, so a config someone intervened in must not be promoted into
        # the list of the ones worth investigating.
        "worst_configs": sorted(
            configs_info, key=lambda c: (-(c["failed"] + c["unknown"]), c["name"]))[:3],
    }
    num_killed = sum(c.get("killed", 0) for c in configs_info)
    if num_killed:
        # Omitted when zero, like every other optional field on this surface: a campaign
        # nobody intervened in should not carry a key saying so.
        result["num_killed"] = num_killed
    num_invalid = sum(c.get("invalid", 0) for c in configs_info)
    if num_invalid:
        result["num_invalid"] = num_invalid
    # Named as a shortfall rather than left for the reader to notice that a count is
    # smaller than the design: the two look identical from here, and only one of them is
    # a campaign to re-run.
    missing = [c["name"] for c in configs_info if c.get("missing")]
    if missing:
        result["num_missing_configs"] = len(missing)
        result["missing_configs"] = missing[:20]
        result["note"] = (
            f"{len(missing)} of {len(configs_info)} declared configurations produced no "
            "runs at all. They were composed with the campaign and never reached the "
            "results tree, so every measurement here is over a partial design. "
            "SELECT config_name FROM run_view WHERE status = 'missing' lists them.")
    if container_failures:
        result["num_container_failures"] = len(container_failures)
        result["container_failures"] = container_failures

    # What this campaign's own measurements say the NEXT one should reserve. Advice rather
    # than data, so it is additive: an agent that ignores the key loses nothing, and one that
    # reads it gets the same numbers the web UI's Details panel shows a human -- see
    # results_processing/advice.py, which is the authority for the sizing rules.
    from robovast.results_processing.advice import campaign_advice
    result.update(campaign_advice(lambda sql: data_access.rows(campaign_id, sql)))

    provenance = data_access.rows(campaign_id, """
        SELECT robovast_version, execution_type, image, image_revision,
               execution_started_at, elapsed_s
        FROM campaign.campaign LIMIT 1
    """)
    if provenance:
        # Omit rather than report null: these are NULL until execution produces
        # _execution/execution.yaml, and a null "image" reads as "no image" instead of
        # "not known yet".
        result.update({k: v for k, v in provenance[0].items() if v is not None})

    # Where the campaign came from -- the same kind of question as the block above, asked of
    # the same row, but read SEPARATELY on purpose: these columns arrived in store schema 7,
    # and `rows()` turns any error (including "no such column" on an older or downloaded
    # campaign) into []. Folded into the query above, one old campaign would silently lose
    # its robovast_version and image as well. Two reads fail independently, so an old
    # campaign loses only what it genuinely does not have.
    #
    # Deliberately NOT added to list_campaigns: that listing is for triage, already drops
    # created_by and mode, and a `running_only` walk renders hundreds of entries.
    origin = data_access.rows(campaign_id, """
        SELECT origin_kind, origin_workspace_id, origin_workspace_name,
               origin_config_path, origin_from_campaign
        FROM campaign.campaign LIMIT 1
    """)
    if origin:
        result.update({k: v for k, v in origin[0].items() if v is not None})

    # Which config version a re-run read. Its own read for the reason the block above is one:
    # these columns arrived in store schema 13, so folding them in would cost every earlier
    # campaign the origin it does have. `origin_config_version_from` is written on every
    # re-run, so an empty step list says the frozen config was read exactly as written, where
    # an absent key says nothing recorded it -- and a re-run that read a different config
    # version than the campaign it reproduces is not repeating the same experiment.
    migration = data_access.rows(campaign_id, """
        SELECT origin_config_version_from, origin_config_migration_steps
        FROM campaign.campaign LIMIT 1
    """)
    if migration and migration[0]["origin_config_version_from"] is not None:
        steps = migration[0]["origin_config_migration_steps"]
        result["origin_config_version_from"] = migration[0]["origin_config_version_from"]
        result["origin_config_migration_steps"] = json.loads(steps) if steps else []

    # Whether this campaign can be re-run. Additive, like `advice` above: an agent that
    # ignores the key loses nothing, and one that reads it can decide whether to call
    # start_campaign(from_campaign=...) instead of burning a launch to find out. Extended
    # here rather than added as a tool because this is already the campaign-provenance
    # surface -- it answers "which robovast, which image, which backend" three lines up.
    result.update(_retrigger_view(campaign_id))
    return result


def _retrigger_view(campaign_id: str) -> dict:
    """``{"retrigger": {...}}`` for *campaign_id*, or ``{}`` when it cannot be determined.

    Omitted rather than reported empty on failure, by the same rule the provenance merge
    above follows: an absent key reads as "not known", where a present-but-empty one reads
    as "checked, nothing to say" -- and those are different answers for a campaign nobody
    has the records for.
    """
    try:
        client = service_access.service_client()
        if client is None:
            return {}
        report = client.check_retrigger(campaign_id)
    except Exception:  # noqa: BLE001 - a pre-flight must not fail the summary it rides on
        return {}
    return {"retrigger": {
        "runnable": report.runnable,
        "blocking": report.blocking,
        # Verdict and detail only. The structured per-axis findings (every pinned image, every
        # resolved plugin) belong to the dedicated call: repeating them here would make the one
        # aggregate tool the largest response in the surface.
        "axes": {name: {"verdict": axis.verdict, "detail": axis.detail}
                 for name, axis in sorted(report.axes.items())},
    }}


async def describe_campaign_data(campaign_id: str, ctx: Context | None = None) -> dict:
    """The schema to write SQL against. Call this before ``query_campaign_data_sql``.

    Read the returned ``note`` first — it carries ready-made queries for the common
    questions. Lists the flat views (``run_view``, ``config_view``), then the tables and
    the ``campaign`` schema, each column as ``"name TYPE"``: a TEXT column orders
    lexicographically, so ``CAST(col AS DOUBLE)`` before comparing it. A table is built the
    first time a query names it; ``built`` of ``runs`` says for how many runs it already is,
    and its ``columns`` are empty until it is built for one.

    Args:
        campaign_id: Campaign identifier, or an absolute campaign path.

    Returns:
        ``{campaign_id, tables, note}`` — each table
        ``{schema, table, columns, rows, kind, runs?, built?, description}``. Or ``{error}``.
    """
    import anyio
    del ctx
    # Off the event loop: the read goes to the service, or to the campaign's files.
    return await anyio.to_thread.run_sync(lambda: data_access.describe(campaign_id))


@lacks(offset="write OFFSET in the sql; a capped reply's csv_url has the full result")
async def query_campaign_data_sql(campaign_id: str, sql: str, limit: int = 500,
                                  ctx: Context | None = None) -> dict:
    """Run one read-only ``SELECT`` over a campaign's data.

    Get the schema from ``describe_campaign_data`` first; ``run_view`` and ``config_view``
    are the entry points and are queried unqualified. Join ``run_view`` (or ``runs``) to
    any metric table on ``(config_name, run_id)`` — ``run_id`` restarts at 0 in every
    configuration, so filtering on it alone silently spans configurations.

    When the result is capped, a ``csv_url`` comes back with it: the same query, streamed
    uncapped over HTTP. Follow it (or give it to the user) instead of paging thousands of
    rows through this interface.

    **A tool that answers the question comes first.** An installed plugin's analysis
    tools (the server instructions name them) reduce the same tables over every recorded
    row; the same question in SQL gets a different number over a thinned or unjoined
    result. Reach for SQL when no tool fits.

    Args:
        campaign_id: Campaign identifier or absolute path (schema ``main``).
        sql: A single ``SELECT``.
        limit: Maximum rows (clamped to 1..5000); ``truncated`` marks when more matched.

    Returns:
        ``{campaign_id, columns, rows, row_count, truncated[, csv_url]}`` or ``{error}``.

    Examples::

        SELECT r.param_wind_strength, AVG(m.error) AS mean_error
        FROM runs r JOIN landing_error m
          ON r.config_name = m.config_name AND r.run_id = m.run_id
        GROUP BY r.param_wind_strength

        -- a JSON field of a TEXT column
        SELECT config_name, sysinfo_json::JSON ->> 'cpu_name' AS cpu FROM run_view

    The query sees *campaign_id*'s data and no other campaign's; compare campaigns with one
    query each.
    """
    import anyio
    del ctx
    result = await anyio.to_thread.run_sync(lambda: data_access.query(campaign_id, sql, limit))
    # Only when it was actually capped: an uncapped result needs no second way to get it,
    # and offering one anyway trains a reader to ignore the field.
    if result.get("truncated"):
        from robovast.service.interface import Routes
        url = service_access.web_url(
            service_access.service_client(), Routes.campaign_query_csv(campaign_id))
        if url:
            result["csv_url"] = f"{url}?sql={quote(sql)}"
    return result


def list_campaign_plots(campaign_id: str) -> dict:
    """What the campaign's author thought worth looking at — start an analysis here.

    Each plot pairs a **runnable** ``query`` (feed it to ``query_campaign_data_sql``)
    with a Vega-Lite spec for charting the result. Declared plots only; write your own
    SQL beyond them.

    Args:
        campaign_id: Campaign identifier or an absolute campaign path.

    Returns:
        ``{campaign_id, plots}`` of ``{title, query, vega_lite}``, or ``{error}``.
    """
    # This stays a call to the interface rather than a query over ``config_view``, and
    # that was checked rather than assumed: the service reads the campaign's immutable
    # ``_config/<name>.vast`` snapshot (``ServiceBase.list_campaign_plots``), which
    # exists from t=0, whereas ``config_view`` is built from
    # ``campaign.campaign.config_json`` and has nothing until the store has a campaign
    # row. Moving to SQL would make a just-started campaign's plots unreadable and would
    # duplicate a reader the service already owns for the web UI.
    try:
        client = service_access.require_service()
        return client.list_campaign_plots(campaign_id).model_dump()
    except Exception as e:  # noqa: BLE001 - surface resolution/parse errors to the client
        return {"error": str(e)}


@lacks(run_id="the contribution belongs to the configuration, the same in every run")
def get_config_contribution(campaign_id: str, config_name: str) -> dict:
    """What this configuration's variations placed: planned path, goals, obstacles.

    The markers its config view draws, and the files its panels read.

    Args:
        campaign_id: Campaign identifier.
        config_name: Configuration name.

    Returns:
        ``{markers, files, errors}``. Markers are in world metres/radians, ``kind`` one of
        ``box/cylinder/sphere/pose/path/point``. ``files`` maps a role to a path under
        ``/results/<campaign_id>/``. ``errors`` names a variation that could not contribute:
        an empty view with errors is not an empty configuration. Or ``{error}``.
    """
    try:
        client = service_access.require_service()
        return client.get_config_contribution(campaign_id, config_name).model_dump(
            exclude_none=True)
    except Exception as e:  # noqa: BLE001 - surface resolution/parse errors to the client
        return {"error": str(e)}


def get_track_deviation(campaign_id: str, config_name: str, run_id: int,
                        source: str = "poses", frame: str = "base_link",
                        marker_label: str | None = None) -> dict:
    """How closely did it follow the plan? Every pose of a track to the nearest point of a
    ``path`` marker of its configuration (``get_config_contribution``), over the whole
    recording, with the driven length beside the path's.

    Args:
        campaign_id: Campaign identifier.
        config_name: Configuration name.
        run_id: Run index within that configuration.
        source: Pose table (``poses`` from a bag, ``sim_poses`` from the simulator).
        frame: Tracked entity; an unrecorded one is refused with those recorded.
        marker_label: Which path, when there are several; none named is then refused.

    Returns:
        ``{points, mean_m, max_m, path_length_m, planar, ...}``; ``planar`` when the path
        has no heights. Or ``{error}``.
    """
    try:
        client = service_access.require_service()
        return client.get_track_deviation(
            campaign_id, config_name, run_id, source=source, frame=frame,
            marker_label=marker_label).model_dump()
    except Exception as e:  # noqa: BLE001 - surface resolution/parse errors to the client
        return {"error": str(e)}


#: Track points a drawing asks for: an even stride over the whole run beyond that.
_DRAWN_TRACK_POINTS = 2000


def _drawn_track(campaign_id: str, config_name: str, run_id: int, source: str,
                 frame: str) -> tuple[list, str]:
    """An evenly strided track spanning the whole run, and a label saying how it was thinned.

    Refuses rather than drawing a prefix: a reply cut at its size ceiling would be the start
    of the run shown as all of it.
    """
    def lit(value):
        return "'" + str(value).replace("'", "''") + "'"
    run_scope = f"config_name = {lit(config_name)} AND run_id = {int(run_id)}"
    described = data_access.describe(campaign_id)
    if "error" in described:
        raise ValueError(described["error"])
    listed = next((t.get("columns") or [] for t in described.get("tables", [])
                   if t.get("table") == source), None)
    if listed is None:
        raise ValueError(f"{source!r} is not a table of {campaign_id!r}")
    if listed:
        columns = {c.split(" ", 1)[0] for c in listed}
    else:
        # Listed but not built for any run yet: an empty page of this run builds it and
        # names its columns.
        page = data_access.query(
            campaign_id, f'SELECT * FROM "{source}" WHERE {run_scope} LIMIT 0', max_rows=1)
        if "error" in page:
            raise ValueError(page["error"])
        columns = set(page.get("columns") or [])
    if "position.x" not in columns:
        raise ValueError(f"{source!r} is not a pose table of {campaign_id!r}")
    from robovast_data.views import pose_clock  # noqa: PLC0415
    clock = pose_clock(columns)
    z = 'CAST("position.z" AS DOUBLE)' if "position.z" in columns else "0.0"
    scope = f"{run_scope} AND \"{clock}\" IS NOT NULL"
    n = _DRAWN_TRACK_POINTS
    result = data_access.query(campaign_id, f"""
        WITH src AS (SELECT CAST("{clock}" AS DOUBLE) AS t,
                            CAST("position.x" AS DOUBLE) AS x,
                            CAST("position.y" AS DOUBLE) AS y, {z} AS z
                     FROM "{source}" WHERE {scope} AND frame = {lit(frame)}),
             idx AS (SELECT *, ROW_NUMBER() OVER (ORDER BY t) - 1 AS _i,
                            COUNT(*) OVER () AS _n FROM src)
        SELECT x, y, z, _n FROM idx
        WHERE _n <= {n} OR _i % GREATEST(1, (_n + {n} - 1) // {n}) = 0 OR _i = _n - 1
        ORDER BY _i""", max_rows=n + 1, max_bytes=8 * 1024 * 1024)
    if "error" in result:
        raise ValueError(result["error"])
    if result.get("truncated"):
        raise ValueError("the track reply was cut short, and a prefix would be drawn as the "
                         "whole run")
    rows = result.get("rows") or []
    if not rows:
        frames = data_access.query(
            campaign_id, f'SELECT DISTINCT frame FROM "{source}" WHERE {scope}', max_rows=200)
        raise ValueError(f"no {source} poses of frame {frame!r} in run {run_id} of "
                         f"{config_name!r}; recorded frames: "
                         f"{', '.join(sorted(r['frame'] for r in frames.get('rows') or []))}")
    total = rows[0]["_n"]
    label = f"run {run_id} {frame} ({source}"
    label += f", {len(rows)} of {total} poses)" if total > len(rows) else ")"
    return [(r["x"], r["y"], r["z"]) for r in rows], label


def draw_config(campaign_id: str, config_name: str, run_id: Optional[int] = None,
                source: str = "poses", frame: str = "base_link",
                projection: str = "xy") -> Image:
    """Picture a configuration: its map, planned path, goals, obstacles, and a run's track.

    Draws what ``get_config_contribution`` returns -- what the config view shows -- plus, with
    ``run_id``, that run's track, evenly thinned over the whole run and labelled so.

    Args:
        campaign_id: Campaign identifier.
        config_name: Configuration name.
        run_id: Overlay this run's track.
        source: Pose table for the track (``poses``, ``sim_poses``).
        frame: Tracked entity.
        projection: ``xy`` (top-down, over a map), ``xz`` or ``yz`` (side views).

    Returns a PNG, so a failure **raises**. What could not be drawn is printed on the image.
    """
    from robovast.client.file_address import \
        RESULTS, format_address  # noqa: PLC0415
    from robovast.common.config_plot import draw  # noqa: PLC0415

    client = service_access.require_service()
    contribution = client.get_config_contribution(campaign_id, config_name).model_dump(
        exclude_none=True)
    track, label = (_drawn_track(campaign_id, config_name, run_id, source, frame)
                    if run_id is not None else (None, ""))
    png, _notes = draw(
        contribution, lambda path: client.read_file_bytes(
            format_address(RESULTS, campaign_id, path)),
        track=track, track_label=label, projection=projection,
        title=f"{campaign_id} / {config_name}")
    return Image(data=png, format="png")


def get_run_scene_status(campaign_id: str, config_name: str, run_id: int = 0) -> dict:
    """Whether a run view's 3D geometry is ready, being built, or **failed and why**.

    Read this when a run view shows no world. The build runs on a background thread, so its
    failure reason reaches the browser and nothing else — without this it is indistinguishable
    from a run nobody has opened yet.

    Read-only: it never starts a build.

    Args:
        campaign_id: The id from ``start_campaign``.
        config_name: Which configuration the run belongs to.
        run_id: Which run of that configuration.

    Returns:
        ``{cached, in_progress, stage, stage_detail, error, note, world, overrides_known,
        bytes}``, or ``{error}``. ``error`` carries the build's own reason. ``stage`` says which
        step a build in flight is on and ``stage_detail`` the cluster's own words for it — a pod's
        ``ImagePullBackOff``, which is how a build waiting on an image this host cannot pull is
        told apart from an ordinary cold start before it times out. ``overrides_known: false``
        means the recording carries no overrides, so geometry may miss per-config overrides.
    """
    try:
        client = service_access.service_client()
        if client is None:
            return {"error": service_access.NO_SERVICE}
        st = client.campaign_scene_status(campaign_id, config_name, str(run_id))
        return st.model_dump() if hasattr(st, "model_dump") else dict(st)
    except Exception as e:  # noqa: BLE001 - surface resolution/transport errors to the client
        return {"error": str(e)}


# -- Looking at a run --------------------------------------------------------

#: The table every video producer fills, one row per encoded camera topic
#: (:class:`robovast_decode.handlers.Videos`). The run view's ``camera`` panel reads the same
#: row, which is what keeps the two surfaces from disagreeing about where a video sits in time.
_VIDEOS_TABLE = "videos"


def _video_row(campaign_id: str, config_name: str, run_id: int, topic: Optional[str]) -> dict:
    """The ``videos`` row to read, or a :class:`RunArtifactError` explaining what is missing."""
    scope = f"config_name = {_lit(config_name)} AND run_id = {_lit(run_id)}"
    sql = (f"SELECT topic, file, t_start, t_end, fps, frames FROM {_VIDEOS_TABLE} "
           f"WHERE {scope}" + (f" AND topic = {_lit(topic)}" if topic else "")
           + " ORDER BY topic")
    rows = data_access.rows(campaign_id, sql, max_rows=50)
    if not rows:
        known = data_access.rows(
            campaign_id,
            f"SELECT DISTINCT topic FROM {_VIDEOS_TABLE} "
            f"WHERE config_name = {_lit(config_name)} AND run_id = {_lit(run_id)}",
            max_rows=50)
        if topic and known:
            raise run_artifacts.RunArtifactError(
                f"run {run_id} of {config_name!r} registered no video for {topic!r}. "
                f"It has: {', '.join(sorted(str(r['topic']) for r in known))}.")
        raise run_artifacts.RunArtifactError(
            f"run {run_id} of {config_name!r} registered no video. A video reaches this tool "
            f"through the `{_VIDEOS_TABLE}` table, which `rosbags_to_webm` writes — add it to "
            f"results_processing.postprocessing, naming the image topic the scenario records, "
            f"and re-run postprocessing.")
    if len(rows) > 1:
        topics = ", ".join(sorted(str(r["topic"]) for r in rows))
        raise run_artifacts.RunArtifactError(
            f"run {run_id} of {config_name!r} recorded several cameras ({topics}); "
            f"pass topic= to choose one.")
    return rows[0]


def _lit(value) -> str:
    """A SQL literal for a value this module controls the type of."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


_IMAGE_TYPES = ("sensor_msgs/msg/Image", "sensor_msgs/msg/CompressedImage")


def _image_topics(campaign_id: str, config_name: str, run_id: int) -> list:
    """The image topics the run recorded, from its ``_recording`` report; ``[]`` when the
    report is not built or names none."""
    types = ", ".join(_lit(t) for t in _IMAGE_TYPES)
    rows = data_access.rows(
        campaign_id,
        f"SELECT DISTINCT topic FROM _recording WHERE config_name = {_lit(config_name)} "
        f"AND run_id = {_lit(run_id)} AND type IN ({types}) ORDER BY topic", max_rows=50)
    return [str(r["topic"]) for r in rows]


def _frame_from_recording(campaign_id: str, config_name: str, run_id: int,
                          time: Optional[float], topic: Optional[str]) -> Optional[bytes]:
    """The frame as PNG from the run's recording through the service's frame route, or
    ``None`` when the run recorded no image topic (the video is then the source)."""
    topics = _image_topics(campaign_id, config_name, run_id)
    if topic is not None:
        topics = [t for t in topics if t == topic]
    if not topics:
        return None
    if len(topics) > 1:
        raise run_artifacts.RunArtifactError(
            f"run {run_id} of {config_name!r} recorded several cameras "
            f"({', '.join(topics)}); pass topic= to choose one.")
    client = service_access.service_client()
    if client is None:
        raise run_artifacts.RunArtifactError(service_access.NO_SERVICE)
    try:
        _stamp, jpeg = client.campaign_frame(campaign_id, f"{config_name}/{run_id}", topics[0],
                                             None if time is None else float(time))
    except KeyError as e:
        raise run_artifacts.RunArtifactError(f"could not read a frame of {topics[0]}: {e}") from e
    from PIL import Image as PILImage  # pylint: disable=import-outside-toplevel
    import io  # pylint: disable=import-outside-toplevel
    out = io.BytesIO()
    PILImage.open(io.BytesIO(jpeg)).save(out, format="PNG")
    return out.getvalue()


def get_camera_frame(campaign_id: str, config_name: str, run_id: int = 0,
                     time: Optional[float] = None,
                     topic: Optional[str] = None) -> Image:
    """One frame of a camera the run recorded, as a PNG: the frame at or before ``time``
    of an image topic, no wider than 640 px, or from the run's ``rosbags_to_webm`` video
    when it recorded no image topic. The viewpoint is the camera's;
    ``get_simulation_screenshot`` picks one. A failure **raises**: no camera and no video,
    an ambiguous ``topic``, an unreadable recording.

    Args:
        campaign_id: The id from ``start_campaign``.
        config_name: Which configuration the run belongs to.
        run_id: Which run of that configuration.
        time: Seconds on the run's timeline, the clock every table uses. Default: the
            newest frame of the recording, or the video's first.
        topic: Which camera, if the run recorded several. Omitted lists them.
    """
    png = _frame_from_recording(campaign_id, config_name, run_id, time, topic)
    if png is not None:
        return Image(data=png, format="png")
    row = _video_row(campaign_id, config_name, run_id, topic)
    name = str(row["file"])
    t_start = float(row["t_start"])
    offset = 0.0 if time is None else max(0.0, float(time) - t_start)

    t_end = row.get("t_end")
    if time is not None and t_end not in (None, "") and float(time) > float(t_end):
        # Clamped rather than wrapped: ffmpeg seeking past the end returns the *first* frame,
        # which is a picture of a different moment presented as this one.
        logger.warning("time %.3f is past the last frame of %s (%.3f); clamping",
                       float(time), name, float(t_end))
        offset = max(0.0, float(t_end) - t_start)

    address = run_artifacts.run_address(campaign_id, config_name, run_id, name)
    with run_artifacts.materialized(address, name.rsplit("/", 1)[-1]) as path:
        return Image(data=_decode_frame(path, offset), format="png")


def _decode_frame(path, offset_s: float) -> bytes:
    """One PNG frame *offset_s* into the recording at *path*, decoded with FFmpeg.

    FFmpeg rather than OpenCV: it is the tool that *wrote* this file (``rosbags_to_webm``
    pipes frames into it), so no second video dependency is added to read it back. ``-ss``
    before ``-i`` is the fast seek, which is accurate here because the encoder pins a
    keyframe interval.
    """
    import subprocess  # pylint: disable=import-outside-toplevel
    cmd = ["ffmpeg", "-loglevel", "error", "-ss", f"{offset_s:.6f}", "-i", str(path),
           "-frames:v", "1", "-c:v", "png", "-f", "image2pipe", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, check=False)
    except FileNotFoundError as e:
        raise run_artifacts.RunArtifactError(
            "ffmpeg is not on this host, and it is what decodes a recording. Install it "
            "(it is also what produced the file) or read the .webm with read_file instead."
        ) from e
    if proc.returncode != 0 or not proc.stdout:
        raise run_artifacts.RunArtifactError(
            f"could not decode a frame at {offset_s:.3f}s of {path.name}: "
            f"{proc.stderr.decode(errors='replace').strip() or 'ffmpeg wrote no frame'}")
    return proc.stdout


def get_simulation_screenshot(campaign_id: str, config_name: str, run_id: int = 0,
                              at: Optional[float] = None,
                              view: Optional[list] = None,
                              focus: Optional[list] = None,
                              camera: Optional[str] = None,
                              size: str = "960x720") -> Image:
    """Re-render one moment of a run from a viewpoint you choose, as a PNG.

    Renders the world again, so the camera is yours. Needs a simulator that can re-render
    (roqsim can; Gazebo cannot) and a run that recorded its state — written on a clean stop
    only. It runs a container in the campaign's simulation image: seconds if that image is on
    the node, minutes if it must be pulled. For a camera *mounted in the world during the run*
    use ``get_camera_frame`` instead — a cheap read of a recorded video, on any backend.

    Returns a PNG, so a failure **raises** rather than coming back as ``{error}``: no such
    capability, no recorded state, or a render that failed.

    Args:
        campaign_id: The id from ``start_campaign``.
        config_name: Which configuration the run belongs to.
        run_id: Which run of that configuration.
        at: Simulated seconds; snaps to the nearest sample. Default: the last one.
        view: ``key=value`` — ``lookat=x,y,z``, ``distance=``, ``azimuth=``/``elevation=`` in
            degrees (around the vertical / above the horizontal).
        focus: Entity or body names to frame on; the simulator picks a clear angle.
        camera: A camera the world defines. It owns its pose — not with ``view``/``focus``.
        size: ``WxH``, default ``960x720``.

    Raises:
        RunArtifactError: no such capability, no recorded state, or the render failed.
    """
    from robovast.common.simulators import parse_view  # pylint: disable=import-outside-toplevel
    from robovast.service import screenshot  # pylint: disable=import-outside-toplevel

    client = service_access.service_client()
    if client is None:
        raise run_artifacts.RunArtifactError(service_access.NO_SERVICE)
    try:
        parsed = parse_view(view)
    except ValueError as e:
        raise run_artifacts.RunArtifactError(str(e)) from e

    try:
        frame = client.campaign_screenshot(
            campaign_id, config_name, str(run_id), at=at, view=parsed,
            focus=list(focus or []), camera=camera, size=size)
    except Exception as e:  # noqa: BLE001 - the reason is the whole value of this failing
        raise run_artifacts.RunArtifactError(str(e)) from e

    path = Path(frame)
    try:
        return Image(data=path.read_bytes(), format="png")
    finally:
        # Ours to remove whichever client produced it: an in-process service renders into a
        # temp dir and the HTTP client writes the bytes into the same shape for exactly this.
        screenshot.discard(path)


# -- Plugin class ------------------------------------------------------------

# Deliberately few. The questions the retired per-scope tools answered are single-table
# queries an LLM composes itself, and ``describe_campaign_data`` carries the canonical form
# of each so none has to be guessed:
#   a run's outcome / host  -> SELECT * FROM run_view WHERE config_name=? AND run_id=?
#   a config's runs, params -> SELECT ... FROM run_view WHERE config_name=?
#   how it was executed     -> SELECT ... FROM campaign.campaign
#   how a metric was made   -> SELECT ... FROM main.postprocessing_steps
#   which configs exist     -> list_files("/results/<campaign>/"): the directories, which
#                              include configs composed but never run (SQL knows only
#                              configs that produced runs)
#   the .vast               -> config_view, or read_file for the file as authored

_TOOLS = [
    list_campaigns,
    get_campaign_summary,
    describe_campaign_data,
    query_campaign_data_sql,
    list_campaign_plots,
    get_config_contribution,
    get_track_deviation,
    draw_config,
    get_run_scene_status,
    get_camera_frame,
    get_simulation_screenshot,
]


class ResultsPlugin:
    """MCP plugin: reading what a campaign did — read-only SQL."""

    name = "results"

    def register(self, mcp: FastMCP) -> None:
        """Register all tool functions with the MCP server."""
        for fn in _TOOLS:
            mcp.tool()(fn)

def _container_failures(campaign_id: str) -> list:
    """One entry per container that died and was restarted, newest first.

    Read separately from the run rollup because it must answer on a campaign that has NO
    run rows -- one that died mid-batch never recorded any. Best-effort: a store that
    predates the table simply has nothing to say, and a summary must not fail because its
    post-mortem section could not be built.
    """
    try:
        rows = data_access.rows(campaign_id, """
            SELECT detected_at, job_name, node_label, container, role, reason, exit_code,
                   signal_name, memory_limit, cpu_limit, log_status, runs_json
            FROM campaign.container_failure ORDER BY detected_at DESC
        """)
    except Exception:  # noqa: BLE001 - no table, no store, nothing to report
        return []
    out = []
    for row in rows or ():
        entry = {k: row.get(k) for k in
                 ("detected_at", "job_name", "node_label", "container", "role", "reason",
                  "exit_code", "signal_name", "log_status")}
        # Surfaced explicitly rather than left NULL: "no memory limit was declared" is a
        # finding about the campaign, not a gap in the record.
        entry["memory_limit"] = row.get("memory_limit") or "none declared"
        try:
            entry["runs"] = json.loads(row.get("runs_json") or "[]")
        except (TypeError, ValueError):
            entry["runs"] = []
        out.append(entry)
    return out
