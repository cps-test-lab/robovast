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

from fastmcp import Context, FastMCP

from robovast.common.store import (read_campaign_created_at,
                                   read_campaign_description)
from robovast.mcp_server import data_access, results_resolver

logger = logging.getLogger(__name__)


def _newest_first(entry: dict) -> tuple:
    """Sort key ordering campaigns by recorded start time, unknown last.

    Used with ``reverse=True``. Deliberately not the campaign id: that is
    ``<name>-<timestamp>`` with a user-supplied name, so sorting on it orders
    alphabetically by name. The id only breaks ties between identical start times.
    """
    started = entry.get("started_at")
    return (started is not None, started or "", entry["campaign_id"])


def list_campaigns(limit: int = 20, offset: int = 0) -> dict:
    """List available campaigns, newest first.

    Ordered by the campaign's recorded start time (``campaign.created_at`` in its
    ``campaign.db``), so ``limit`` returns the most recent campaigns — the first page
    answers "what did I just run?". A campaign whose start time is unrecorded sorts
    last, with ``started_at`` null.

    Each entry carries the ``description`` its launcher gave (see ``start_campaign``),
    omitted for a campaign started without one — that text is the only thing telling two
    same-day ``campaign-<timestamp>`` ids apart.

    ``postprocessed`` says whether the campaign's metric tables exist yet. Either way its
    per-run outcomes are queryable (``run_view``); only per-run *metrics* need
    postprocessing.

    Args:
        limit: Maximum number of campaigns to return (default 20).
        offset: Number of campaigns to skip (default 0).
    """
    campaigns: list[dict] = []
    for d in results_resolver.list_campaigns():
        entry = {"campaign_id": d.name, "started_at": read_campaign_created_at(d)}
        description = read_campaign_description(d)
        if description:
            entry["description"] = description
        entry["postprocessed"] = (d / "_execution" / "data.db").is_file()
        campaigns.append(entry)

    campaigns.sort(key=_newest_first, reverse=True)
    return {
        "campaigns": campaigns[offset: offset + limit],
        "total": len(campaigns),
        "offset": offset,
    }


def get_campaign_summary(campaign_id: str) -> dict:
    """Get aggregated statistics about a campaign: pass/fail counts and provenance.

    Returns the number of configurations and runs, the pass/fail/unknown tallies, the 3
    configurations with the most non-passing runs, and the execution provenance (which
    robovast, which image, which lane). Works before postprocessing.

    For anything more specific — a single run, one configuration's parameters, a metric —
    use ``describe_campaign_data`` and ``query_campaign_data_sql``.

    Args:
        campaign_id: Campaign name (e.g. ``campaign-2026-03-04-152130``).
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


def campaign_data_status(campaign_id: str) -> dict:
    """Check whether querying this campaign will first have to fetch data — and how much.

    Cheap (two metadata lookups). Worth calling before a **batch** of queries against a
    campaign you have not touched yet, so you can tell the user why the first one is slow
    instead of appearing to hang; a single query does not need it, since it reports the same
    thing in its ``fetch`` field afterwards.

    Args:
        campaign_id: Campaign identifier or an absolute campaign path.

    Returns:
        ``{campaign_id, source, fetch_required, cached, transfer, db_bytes,
        fetch_in_progress, last_fetch_seconds, last_fetch_bytes, note}``.
        ``fetch_required: false`` (a local service) means the question does not apply.
        ``transfer`` distinguishes ``"cluster-network"`` (fast) from ``"port-forward"``
        (slow) — the same object store reached two very different ways. Or ``{error}``
        when no service answers.
    """
    status = data_access.data_status(campaign_id)
    if status is None:
        return {"error": (
            "no robovast-service answered, so there is nothing to fetch from: campaign "
            "data is read from local disk in this process. (A service too old to serve "
            "/data-status reports the same.)")}
    return status


async def describe_campaign_data(campaign_id: str, ctx: Context | None = None) -> dict:
    """Describe a campaign's queryable data — the schema to write SQL against.

    Call this before :func:`query_campaign_data_sql`, and read the returned ``note``: it
    carries ready-made queries for the common questions. Lists the flat views
    (``run_view``, ``config_view``) first, then the metric tables and the attached
    ``campaign`` schema, each with its columns as ``"name TYPE"`` — a ``TEXT`` column
    orders lexicographically and needs ``CAST(col AS REAL)``.

    The **first** call for a cluster campaign also transfers its databases from the object
    store, so it can take noticeably longer than the calls after it; the returned ``fetch``
    says what that cost. :func:`campaign_data_status` answers it in advance.

    Args:
        campaign_id: Campaign identifier or an absolute campaign path.

    Returns:
        ``{campaign_id, tables: [{schema, table, columns, rows, description}], note}``
        or ``{error}``; plus ``fetch`` when a service resolved the campaign.
    """
    return await _announced(
        ctx, campaign_id,
        lambda pf: data_access.describe(campaign_id, preflight=pf))


async def query_campaign_data_sql(campaign_id: str, sql: str, max_rows: int = 500,
                                  extra_campaign_ids: list | None = None,
                                  ctx: Context | None = None) -> dict:
    """Run a **read-only** SQL query over a campaign's data.

    Only ``SELECT`` is permitted. Discover the schema first with
    :func:`describe_campaign_data`; ``run_view`` and ``config_view`` are the entry points,
    queried unqualified.

    The **first** query on a cluster campaign also transfers its databases from the object
    store, so it can take noticeably longer than later ones; the returned ``fetch`` says
    what that cost, and :func:`campaign_data_status` answers it in advance.

    To **compare campaigns**, pass ``extra_campaign_ids``: each is attached under a
    schema alias ``c1``, ``c2``, … (its ``campaign.db`` as ``c1_campaign``, …), so a
    single query can span several campaigns. The returned ``attached`` maps each
    alias back to its campaign id.

    Args:
        campaign_id: Campaign identifier or absolute campaign path (schema ``main``).
        sql: A single ``SELECT`` statement.
        max_rows: Maximum rows to return (clamped to ``1..5000``); ``truncated``
            marks when more rows matched.
        extra_campaign_ids: Additional campaigns to attach as ``c1``, ``c2``, ….

    Returns:
        ``{campaign_id, columns, rows, row_count, truncated[, attached]}`` or
        ``{error}``; plus ``fetch`` when a service resolved the campaign.

    Example — mean of a metric per parameter value::

        SELECT r.param_wind_strength, AVG(m.error) AS mean_error
        FROM runs r JOIN landing_error m
          ON r.config_name = m.config_name AND r.run_id = m.run_id
        GROUP BY r.param_wind_strength ORDER BY r.param_wind_strength

    Example — compare two campaigns (``extra_campaign_ids=["campaign-B"]``)::

        SELECT 'A' AS campaign, AVG(objective) FROM runs
        UNION ALL SELECT 'B', AVG(objective) FROM c1.runs
    """
    return await _announced(
        ctx, campaign_id,
        lambda pf: data_access.query(campaign_id, sql, max_rows, extra_campaign_ids,
                                     preflight=pf))


def list_campaign_plots(campaign_id: str) -> dict:
    """List the plots a campaign's author declared in its ``.vast`` ``evaluation.plots``.

    Each plot pairs a **runnable** ``query`` (feed it to
    :func:`query_campaign_data_sql`) with a **Vega-Lite** spec describing how to
    chart the result — so this is the fastest way to learn what the campaign author
    considered worth looking at, and to reproduce those views. Returns declared
    plots only; you can always write your own SQL beyond them.

    Args:
        campaign_id: Campaign identifier or an absolute campaign path.

    Returns:
        ``{campaign_id, plots: [{title, query, vega_lite}]}`` or ``{error}``.
    """
    # Both transports implement this, so a reachable service answers for a cluster
    # campaign and LocalTransport answers from disk otherwise. Resolved explicitly
    # rather than through ``RobovastClient(detected_service_url())``: an empty URL there
    # silently yields the local transport, so "no service answered" would read as a local
    # answer instead of being reported.
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
    campaign_data_status,
    list_campaign_plots,
]


class ResultsPlugin:
    """MCP plugin: reading what a campaign did — read-only SQL."""

    name = "results"

    def register(self, mcp: FastMCP) -> None:
        """Register all tool functions with the MCP server."""
        for fn in _TOOLS:
            mcp.tool()(fn)
