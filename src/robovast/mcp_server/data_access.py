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

"""One way for MCP tools to read a campaign's data: read-only SQL.

Every tool that answers a question about what a campaign *did* goes through here, so
there is a single place that knows how to reach a campaign — delegating to a reachable
``robovast-service`` (so a cluster campaign works, not just local disk) and otherwise
resolving the directory locally and querying it in process.

This exists because the alternative was nine tools each parsing ``metadata.yaml`` with
its own response schema, which meant every one of them answered "run postprocessing
first" for a campaign that had perfectly good results — ``metadata.yaml`` is written only
by postprocessing, while ``campaign.db`` is written as the campaign runs.
"""

import logging

from robovast.mcp_server import results_resolver, service_access
from robovast.results_processing.data_query import DataQueryError, describe_data_db, query_data_db

logger = logging.getLogger(__name__)

__all__ = ["describe", "query", "rows", "service_client"]


def service_client():
    """Re-exported so a caller has one import for reading a campaign.

    Delegates rather than aliases, so patching
    ``service_access.service_client`` is a single seam that reaches every tool.
    """
    return service_access.service_client()


#: Errors a caller must see as ``{"error": ...}`` rather than as a traceback. The
#: transport ones matter as much as the query ones: a service running an older robovast
#: rejects a query naming a column it does not have, and that arrives as an HTTP 400. An
#: MCP tool has to report it — an escaping exception tells the caller nothing about which
#: of the two ends is behind.
_REPORTED = (DataQueryError, ValueError, OSError)


def describe(campaign_id: str) -> dict:
    """``{campaign_id, tables, note}`` for a campaign, or ``{error}``."""
    client = service_access.service_client()
    try:
        if client is not None:
            result = client.describe_campaign_data(campaign_id).model_dump()
        else:
            campaign_dir = results_resolver.resolve_campaign_path(campaign_id)
            result = {"campaign_id": campaign_id, **describe_data_db(campaign_dir)}
    except _REPORTED as e:
        return {"error": _message(e, client)}
    return result


def query(campaign_id: str, sql: str, max_rows: int = 500,
          max_bytes: int | None = None) -> dict:
    """Run a read-only ``SELECT``; ``{campaign_id, columns, rows, ...}`` or ``{error}``.

    *max_bytes* raises the reply's size ceiling for a caller that consumes the rows rather
    than reading them into a context window -- a plot, say. Omitted, the reply keeps the
    ceiling sized for an agent.
    """
    client = service_access.service_client()
    try:
        if client is not None:
            result = client.query_campaign_data_sql(
                campaign_id, sql, max_rows, max_bytes=max_bytes).model_dump()
        else:
            campaign_dir = results_resolver.resolve_campaign_path(campaign_id)
            result = {"campaign_id": campaign_id,
                      **query_data_db(campaign_dir, sql, max_rows, max_bytes=max_bytes)}
    except _REPORTED as e:
        return {"error": _message(e, client)}
    return result


def _message(exc: Exception, client) -> str:
    """The error text, saying *where* it came from when a service answered.

    Without this a schema error from a service running a different robovast version is
    indistinguishable from one in the local database, and the fix (restart the service)
    is not discoverable from the message.
    """
    if client is None:
        return str(exc)
    return (f"{exc} (reported by the robovast-service this tool is talking to; if the "
            "query names a table or column that exists in this robovast, that service "
            "may be running an older version — restart it)")


def rows(campaign_id: str, sql: str, max_rows: int = 5000) -> list[dict]:
    """Just the rows of a query, or ``[]``.

    For a tool that computes over the result rather than returning it. Errors are logged
    and yield ``[]``: a convenience tool built on SQL must not turn a missing table into a
    traceback, and its caller reports the empty case in its own terms.
    """
    result = query(campaign_id, sql, max_rows)
    if "error" in result:
        logger.debug("query failed for %s: %s", campaign_id, result["error"])
        return []
    return result.get("rows") or []
