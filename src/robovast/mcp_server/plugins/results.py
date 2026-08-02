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

"""MCP plugin: reading what a campaign did — read-only SQL.

Two flat views (``run_view``, ``config_view``) plus the metric tables answer the per-run,
per-configuration and aggregate questions that used to need a tool each. The per-scope
metadata tools they replaced parsed ``metadata.yaml``, which only postprocessing writes,
so each of them reported "run postprocessing first" about campaigns whose outcomes were
already recorded in ``campaign.db``.

What stays a dedicated tool is the campaign listing (it spans campaigns) and the one
aggregate asked constantly, itself computed over the same SQL. Campaign **files** are read
through the address space (``/results/<campaign_id>/<path>``).
"""

import logging
from typing import Any
from urllib.parse import quote

from fastmcp import Context, FastMCP

from robovast.mcp_server import data_access, service_access

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
    """
    entry = {
        "campaign_id": summary.campaign_id,
        "status": summary.phase,
        "started_at": summary.started_at,
        "postprocessed": summary.postprocessed,
        "num_runs": summary.num_runs,
        "num_passed": summary.num_passed,
        "num_failed": summary.num_failed,
    }
    if summary.description:
        entry["description"] = summary.description
    if summary.finished_at:
        entry["finished_at"] = summary.finished_at
    return entry


def _walk_all(client) -> list:
    """Every campaign summary the service knows, newest first.

    Only for ``running_only``, which cannot be answered from one page.
    """
    from robovast.service.interface import ListCampaignsRequest
    out: list = []
    offset = 0
    while True:
        page = client.list_campaigns(
            ListCampaignsRequest(limit=_WALK_PAGE, offset=offset))
        out.extend(page.campaigns)
        offset += _WALK_PAGE
        if offset >= page.total or not page.campaigns:
            return out


def list_campaigns(limit: int = 20, offset: int = 0,
                   running_only: bool = False) -> dict:
    """What has been run? Campaigns newest first — the first page answers "what did I
    just run?".

    Args:
        limit: Maximum campaigns to return.
        offset: Campaigns to skip (campaign index).
        running_only: Only the campaigns the service considers live, across all lanes.
            The whole list is walked before filtering, so a long-running campaign started
            days ago still appears; ``total`` then counts the live ones.

    Returns:
        ``{campaigns, total, offset, source}`` — each campaign ``{campaign_id, status,
        started_at, postprocessed, num_runs, num_passed, num_failed}`` plus
        ``description`` and ``finished_at`` where recorded — or ``{error}``.

        ``description`` is what its launcher said the run was for, and is usually the
        only thing telling two same-day ``campaign-<timestamp>`` ids apart.
        ``postprocessed`` says whether the metric tables exist; per-run *outcomes* are
        queryable either way (``run_view``). ``source`` names who answered — the service,
        or this host's results root when none is reachable, since "no campaigns" means
        different things from the two.
    """
    from robovast.service.interface import ListCampaignsRequest
    client = service_access.service_client()
    source = "service"
    if client is None:
        from robovast.service.local_transport import LocalTransport
        client = LocalTransport()
        source = "local results root"
    try:
        if running_only:
            from robovast.execution.control_server import is_running
            matched = [c for c in _walk_all(client) if is_running(c.phase)]
            total = len(matched)
            window = matched[offset:offset + limit]
        else:
            page = client.list_campaigns(
                ListCampaignsRequest(limit=limit, offset=offset))
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
        worst_configs}`` plus the execution provenance (which robovast, image, lane)
        once the campaign has produced it; or ``{error}``.
    """
    per_config = data_access.rows(campaign_id, """
        SELECT config_name,
               COUNT(*)                                        AS num_runs,
               SUM(CASE WHEN status = 'passed' THEN 1 ELSE 0 END) AS success,
               SUM(CASE WHEN status IN ('failed', 'error') THEN 1 ELSE 0 END) AS failed,
               SUM(CASE WHEN status = 'unknown' THEN 1 ELSE 0 END) AS unknown
        FROM run_view GROUP BY config_name ORDER BY config_name
    """)
    if not per_config:
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
    } for c in per_config]

    result: dict[str, Any] = {
        "campaign_id": campaign_id,
        "num_configs": len(configs_info),
        "num_runs": sum(c["num_runs"] for c in configs_info),
        "num_success": sum(c["success"] for c in configs_info),
        "num_failed": sum(c["failed"] for c in configs_info),
        "num_unknown": sum(c["unknown"] for c in configs_info),
        "worst_configs": sorted(
            configs_info, key=lambda c: (-(c["failed"] + c["unknown"]), c["name"]))[:3],
    }

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
    return result


async def _announced(ctx, campaign_id: str, call):
    """Run ``call(preflight)`` off the event loop, saying first if it must fetch.

    The announcement has to precede the wait to be worth anything, so it goes out as an MCP
    log notification *before* the call starts; the call then runs in a worker thread so that
    notification actually reaches the client instead of sitting behind a blocked loop.
    ``ctx`` is None for an in-process caller, which just means no live notification — the
    reason still arrives with the result, via the warning middleware.

    The probe made here is handed to *call* rather than repeated inside it: probing twice
    would log the warning twice and, worse, read the post-fetch state as if it were the
    pre-fetch one.
    """
    import anyio
    preflight = data_access.announce_pending_fetch(campaign_id)
    if preflight[1] and ctx is not None:
        await ctx.info(preflight[1])
    return await anyio.to_thread.run_sync(lambda: call(preflight))


async def describe_campaign_data(campaign_id: str, preflight_only: bool = False,
                                 ctx: Context | None = None) -> dict:
    """The schema to write SQL against. Call this before ``query_campaign_data_sql``.

    Read the returned ``note`` first — it carries ready-made queries for the common
    questions. Lists the flat views (``run_view``, ``config_view``), then the metric
    tables and the attached ``campaign`` schema, each column as ``"name TYPE"``: a TEXT
    column orders lexicographically, so ``CAST(col AS REAL)`` before comparing it.

    Args:
        campaign_id: Campaign identifier, or an absolute campaign path.
        preflight_only: Return just the ``fetch`` verdict (two metadata lookups, no
            schema read). Worth it before a **batch** of queries against a cluster
            campaign you have not touched yet, so a slow first call is explainable
            rather than looking like a hang.

    Returns:
        ``{campaign_id, tables, note, fetch}`` — each table
        ``{schema, table, columns, rows, description}``. With ``preflight_only``,
        ``{campaign_id, source, fetch_required, cached, transfer, db_bytes,
        fetch_in_progress, last_fetch_seconds, last_fetch_bytes, note}``. Or ``{error}``.

        ``fetch`` is what this call cost: the first read of a cluster campaign transfers
        its two databases from the object store, and ``transfer`` separates
        ``cluster-network`` (fast) from ``port-forward`` (slow). ``fetch_required: false``
        means the campaign is local and the question does not apply.
    """
    if preflight_only:
        status = data_access.data_status(campaign_id)
        if status is None:
            return {"error": (
                "no robovast-service answered, so there is nothing to fetch from: "
                "campaign data is read from local disk in this process. (A service too "
                "old to serve /data-status reports the same.)")}
        return status
    return await _announced(
        ctx, campaign_id,
        lambda pf: data_access.describe(campaign_id, preflight=pf))


async def query_campaign_data_sql(campaign_id: str, sql: str, limit: int = 500,
                                  extra_campaign_ids: list | None = None,
                                  ctx: Context | None = None) -> dict:
    """Run one read-only ``SELECT`` over a campaign's data. This answers most questions.

    Get the schema from ``describe_campaign_data`` first; ``run_view`` and ``config_view``
    are the entry points and are queried unqualified. Join ``run_view`` (or ``runs``) to
    any metric table on ``(config_name, run_id)`` — ``run_id`` restarts at 0 in every
    configuration, so filtering on it alone silently spans configurations.

    When the result is capped, a ``csv_url`` comes back with it: the same query, streamed
    uncapped over HTTP. Follow it (or give it to the user) instead of paging thousands of
    rows through this interface.

    Args:
        campaign_id: Campaign identifier or absolute path (schema ``main``).
        sql: A single ``SELECT``.
        limit: Maximum rows (clamped to 1..5000); ``truncated`` marks when more matched.
        extra_campaign_ids: Campaigns to attach as ``c1``, ``c2``, … (their
            ``campaign.db`` as ``c1_campaign``, …) so one query can compare campaigns.

    Returns:
        ``{campaign_id, columns, rows, row_count, truncated, fetch[, attached, csv_url]}``
        or ``{error}``. See ``describe_campaign_data`` for what ``fetch`` costs.

    Examples::

        SELECT r.param_wind_strength, AVG(m.error) AS mean_error
        FROM runs r JOIN landing_error m
          ON r.config_name = m.config_name AND r.run_id = m.run_id
        GROUP BY r.param_wind_strength

        -- with extra_campaign_ids=["campaign-B"]
        SELECT 'A' AS campaign, AVG(objective) FROM runs
        UNION ALL SELECT 'B', AVG(objective) FROM c1.runs
    """
    result = await _announced(
        ctx, campaign_id,
        lambda pf: data_access.query(campaign_id, sql, limit, extra_campaign_ids,
                                     preflight=pf))
    # Only when it was actually capped: an uncapped result needs no second way to get it,
    # and offering one anyway trains a reader to ignore the field.
    if result.get("truncated"):
        from robovast.service.interface import Routes
        url = service_access.web_url(
            service_access.service_client(), Routes.campaign_query_csv(campaign_id))
        if url:
            result["csv_url"] = f"{url}?sql={quote(sql)}" + (
                "&extra_campaign_ids=" + quote(",".join(extra_campaign_ids))
                if extra_campaign_ids else "")
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
    # Both transports implement this, so a reachable service answers for a cluster
    # campaign and LocalTransport answers from disk otherwise. Resolved explicitly
    # rather than through ``RobovastClient(detected_service_url())``: an empty URL there
    # silently yields the local transport, so "no service answered" would read as a local
    # answer instead of being reported.
    #
    # This stays a call to the interface rather than a query over ``config_view``, and
    # that was checked rather than assumed: the service reads the campaign's immutable
    # ``_config/<name>.vast`` snapshot (``local_transport.list_campaign_plots``), which
    # exists from t=0, whereas ``config_view`` is built from
    # ``campaign.campaign.config_json`` and has nothing until the store has a campaign
    # row. Moving to SQL would make a just-started campaign's plots unreadable and would
    # duplicate a reader the service already owns for the web UI.
    from robovast.service.local_transport import LocalTransport  # noqa: PLC0415
    try:
        client = data_access.service_client() or LocalTransport()
        return client.list_campaign_plots(campaign_id).model_dump()
    except Exception as e:  # noqa: BLE001 - surface resolution/parse errors to the client
        return {"error": str(e)}


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
]


class ResultsPlugin:
    """MCP plugin: reading what a campaign did — read-only SQL."""

    name = "results"

    def register(self, mcp: FastMCP) -> None:
        """Register all tool functions with the MCP server."""
        for fn in _TOOLS:
            mcp.tool()(fn)
