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

"""MCP plugin: query a campaign's results with read-only **SQL**.

A campaign's per-run metrics are consolidated into
``<campaign>/_execution/data.db`` during postprocessing — one table per CSV
stem (``poses``, ``behaviors``, …), a ``rosout`` log table, ``scenario_timestamps``,
and a ``runs`` **dimension table** (per-run ``status``/``duration_s`` + scenario
parameters as ``param_*`` columns). That ``runs`` table is the postprocessed
*view* over ``campaign.db``'s ``run`` table — the operational source of truth for
per-run outcomes, written live from each ``test.xml`` and attached as
``campaign.run``. For raw pass/fail (``SELECT status, COUNT(*) FROM campaign.run
GROUP BY status``) ``campaign.run`` is queryable even before postprocessing exists.
SQL is the generic query interface an LLM wants, so this plugin exposes exactly two
tools:

* :func:`describe_campaign_data` — the schema (tables, columns, row counts);
* :func:`query_campaign_data_sql` — a **read-only** ``SELECT`` (authorizer + ``mode=ro``),
  with ``campaign.db`` attached as schema ``campaign``.

The query logic lives in :mod:`robovast.results_processing.data_query` (shared with
the ``robovast-service`` interface ops). When a service is reachable (auto-detected
on the conventional local port — a ``vast serve`` or a tunnel you brought up) these
tools **delegate to it** (so they work against a remote/cluster campaign, not just
local disk); otherwise they resolve the campaign directory locally via
``results_resolver`` and query it in-process.
"""

import logging

from fastmcp import FastMCP

from robovast.mcp_server import results_resolver
from robovast.results_processing.data_query import (DataQueryError,
                                                    describe_data_db, query_data_db)

logger = logging.getLogger(__name__)


def _service_client():
    """A ``RobovastClient`` bound to a reachable service, or None (query locally)."""
    from robovast.common.cli.service_target import detected_service_url
    url = detected_service_url()
    if not url:
        return None
    from robovast.service.client import RobovastClient
    return RobovastClient(url)


def describe_campaign_data(campaign_id: str) -> dict:
    """Describe a campaign's queryable data — the schema to write SQL against.

    Lists every table in ``data.db`` (metric tables, ``rosout``, the ``runs``
    dimension table with per-run status/duration + ``param_*`` columns) plus the
    attached ``campaign.db`` (schema ``campaign``: ``campaign``/``batch``/``unit``/
    ``run`` — ``campaign.run`` is the per-run source of truth). Use before
    :func:`query_campaign_data_sql`.

    Args:
        campaign_id: Campaign identifier or an absolute campaign path.

    Returns:
        ``{campaign_id, tables: [{schema, table, columns, rows}], note}`` or
        ``{error}`` if no ``data.db`` exists (run postprocessing first).
    """
    client = _service_client()
    if client is not None:
        return client.describe_campaign_data(campaign_id).model_dump()
    try:
        campaign_dir = results_resolver.resolve_campaign_path(campaign_id)
        return {"campaign_id": campaign_id, **describe_data_db(campaign_dir)}
    except (DataQueryError, ValueError) as e:
        return {"error": str(e)}


def query_campaign_data_sql(campaign_id: str, sql: str, max_rows: int = 500,
                            extra_campaign_ids: list | None = None) -> dict:
    """Run a **read-only** SQL query over a campaign's data.

    Runs against ``data.db`` (metric tables + the ``runs`` dimension table) with
    ``campaign.db`` attached as schema ``campaign``. Only ``SELECT`` is permitted;
    a ``REGEXP(pat, col)`` function is available. Discover the schema first with
    :func:`describe_campaign_data`.

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
        ``{error}``.

    Example — mean of a metric per parameter value::

        query_campaign_data_sql(
            campaign_id="campaign-...",
            sql='''SELECT r.param_wind_strength, AVG(CAST(m.error AS REAL)) AS mean_error
                   FROM runs r JOIN landing_error m
                     ON r.config_name = m.config_name AND r.run_id = m.run_id
                   GROUP BY r.param_wind_strength ORDER BY r.param_wind_strength''')

    Example — compare a metric across two campaigns::

        query_campaign_data_sql(
            campaign_id="campaign-A", extra_campaign_ids=["campaign-B"],
            sql='''SELECT 'A' AS campaign, AVG(objective) FROM runs
                   UNION ALL
                   SELECT 'B', AVG(objective) FROM c1.runs''')
    """
    extra_campaign_ids = extra_campaign_ids or []
    aliases = {f"c{i + 1}": cid for i, cid in enumerate(extra_campaign_ids)}

    client = _service_client()
    if client is not None:
        result = client.query_campaign_data_sql(
            campaign_id, sql, max_rows, extra_campaign_ids).model_dump()
        if aliases:
            result["attached"] = aliases
        return result
    try:
        campaign_dir = results_resolver.resolve_campaign_path(campaign_id)
        extra_dirs = {alias: results_resolver.resolve_campaign_path(cid)
                      for alias, cid in aliases.items()}
        result = {"campaign_id": campaign_id,
                  **query_data_db(campaign_dir, sql, max_rows, extra_dirs=extra_dirs)}
        if aliases:
            result["attached"] = aliases
        return result
    except (DataQueryError, ValueError) as e:
        return {"error": str(e)}


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
    from robovast.common.cli.service_target import detected_service_url  # noqa: PLC0415
    from robovast.service.client import RobovastClient  # noqa: PLC0415
    try:
        # Both transports implement this, so the factory (LocalTransport when no
        # service is reachable) resolves it with no local special-casing.
        client = RobovastClient(detected_service_url())
        return client.list_campaign_plots(campaign_id).model_dump()
    except Exception as e:  # noqa: BLE001 - surface resolution/parse errors to the client
        return {"error": str(e)}


_TOOLS = [
    describe_campaign_data,
    query_campaign_data_sql,
    list_campaign_plots,
]


class RunDataPlugin:
    """Expose read-only SQL querying of a campaign's results as MCP tools."""

    name = "run_data"

    def register(self, mcp: FastMCP) -> None:
        for fn in _TOOLS:
            mcp.tool()(fn)
