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

from robovast.common.progress import fmt_size
from robovast.mcp_server import results_resolver, service_access
from robovast.results_processing.data_query import DataQueryError, describe_data_db, query_data_db

logger = logging.getLogger(__name__)

__all__ = ["announce_pending_fetch", "data_status", "describe", "query", "rows",
           "service_client"]


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


def data_status(campaign_id: str, client=None) -> dict | None:
    """``campaign_data_status`` for a campaign, or ``None`` when it cannot be known.

    ``None`` covers the two cases a caller must treat identically to "no warning needed":
    no service answers (the local in-process lane transfers nothing), and a service too
    old to serve the route. **Never raises** — this only decides whether to say "this may
    take a moment", so a failure here must not fail the query it precedes.
    """
    client = client or service_access.service_client()
    if client is None:
        return None
    try:
        return client.campaign_data_status(campaign_id).model_dump()
    except Exception as e:  # noqa: BLE001 - an advisory probe never breaks the real call
        logger.debug("data-status probe failed for %s: %s", campaign_id, e)
        return None


def announce_pending_fetch(campaign_id: str, client=None) -> tuple[dict | None, str | None]:
    """Probe, and log why the next query may be slow. ``(status, notice_or_None)``.

    *notice* is non-None only when a transfer is actually pending — a cluster campaign
    whose databases are not cached yet. It is logged as a **warning** so the MCP server's
    warning-forwarding middleware attaches it to whichever tool result comes back, which
    is what makes the reason reach a caller that ignores log notifications. An async caller
    can additionally deliver it up front (see ``results.py``); the return value is that
    caller's copy.
    """
    status = data_status(campaign_id, client)
    if status is None or not status.get("fetch_required") or status.get("cached"):
        return status, None
    size = fmt_size(status["db_bytes"]) if status.get("db_bytes") else "unknown size"
    queued = " Another request is already fetching it, so this one waits for that." \
        if status.get("fetch_in_progress") else ""
    notice = (
        f"First query on {campaign_id}: the service is fetching this campaign's query "
        f"databases ({size}) from the object store"
        + (" over a kubectl port-forward" if status.get("transfer") == "port-forward"
           else " over the cluster network")
        + f", so this call is slower than the ones after it.{queued}")
    logger.warning(notice)
    return status, notice


def _fetch_info(status: dict | None, cold: bool) -> dict | None:
    """The ``fetch`` block attached to a result: what the transfer cost, or None."""
    if status is None:
        return None
    info = {"source": status.get("source"), "transfer": status.get("transfer"),
            "cold": cold}
    if cold:
        # Recorded by the service during the call we just made, so this is that call's
        # own cost rather than a stale figure from an earlier one.
        info["seconds"] = status.get("last_fetch_seconds")
        info["bytes"] = status.get("last_fetch_bytes")
    return info


def describe(campaign_id: str, preflight=None) -> dict:
    """``{campaign_id, tables, note}`` for a campaign, or ``{error}``.

    *preflight* is an ``announce_pending_fetch`` result an async caller already obtained so
    it could announce before blocking; passing it keeps the announcement to **one** probe
    and one warning, instead of this function repeating both.
    """
    client = service_access.service_client()
    status, notice = (preflight if preflight is not None
                      else announce_pending_fetch(campaign_id, client))
    try:
        if client is not None:
            result = client.describe_campaign_data(campaign_id).model_dump()
        else:
            campaign_dir = results_resolver.resolve_campaign_path(campaign_id)
            result = {"campaign_id": campaign_id, **describe_data_db(campaign_dir)}
    except _REPORTED as e:
        return {"error": _message(e, client)}
    return _with_fetch(result, campaign_id, client, status, notice)


def _with_fetch(result: dict, campaign_id: str, client, status, notice) -> dict:
    """Attach what the transfer cost, when there was one.

    Re-probes after a cold call: the pre-call status could not know the duration of a
    transfer that had not happened yet, and reporting the measured cost is what lets a
    caller say "3.5 min because 8 GB moved" instead of guessing.
    """
    if notice is None:
        info = _fetch_info(status, cold=False)
    else:
        info = _fetch_info(data_status(campaign_id, client) or status, cold=True)
    if info is not None:
        result["fetch"] = info
    return result


def query(campaign_id: str, sql: str, max_rows: int = 500, preflight=None) -> dict:
    """Run a read-only ``SELECT``; ``{campaign_id, columns, rows, ...}`` or ``{error}``.

    See :func:`describe` for *preflight* — it keeps the pending-fetch announcement to one
    probe when an async caller has already made it.
    """
    client = service_access.service_client()
    status, notice = (preflight if preflight is not None
                      else announce_pending_fetch(campaign_id, client))
    try:
        if client is not None:
            result = client.query_campaign_data_sql(
                campaign_id, sql, max_rows).model_dump()
        else:
            campaign_dir = results_resolver.resolve_campaign_path(campaign_id)
            result = {"campaign_id": campaign_id,
                      **query_data_db(campaign_dir, sql, max_rows)}
    except _REPORTED as e:
        return {"error": _message(e, client)}
    return _with_fetch(result, campaign_id, client, status, notice)


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
