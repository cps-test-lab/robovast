# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A slow first query says so, and says why.

On the cluster the first query of a campaign transfers its databases from the object
store inside the request. Without a word from the tool that is indistinguishable from a
hang, so the reason is delivered twice over: as an MCP log notification *before* the wait
(the only channel that can precede it) and as a ``fetch`` block on the result, which is
what reaches a client that ignores notifications.

The probe is advisory, so its failure modes matter more than its success: no service, or a
service too old to serve the route, must both read as "nothing to announce" and never
break the query they precede.
"""

import asyncio

import pytest

from robovast.mcp_server import data_access
from robovast.mcp_server.plugins import results


class _Client:
    """A service whose data-status answer the test dictates."""

    def __init__(self, status=None, raises=None):
        self._status = status
        self._raises = raises
        self.queries = 0

    def campaign_data_status(self, campaign_id):
        if self._raises is not None:
            raise self._raises
        from robovast.service.interface import CampaignDataStatus
        return CampaignDataStatus(campaign_id=campaign_id, **self._status)

    def query_campaign_data_sql(self, campaign_id, sql, max_rows, extras):
        from robovast.service.interface import DataQueryResult
        self.queries += 1
        return DataQueryResult(campaign_id=campaign_id, columns=["n"],
                              rows=[{"n": 1}], row_count=1)

    def describe_campaign_data(self, campaign_id):
        from robovast.service.interface import DataDescribe
        return DataDescribe(campaign_id=campaign_id, tables=[])


_COLD = {"source": "object-store", "fetch_required": True, "cached": False,
         "transfer": "port-forward", "db_bytes": 41_884_672,
         "note": "the query databases are not in the service cache yet"}
_WARM = {"source": "object-store", "fetch_required": True, "cached": True,
         "transfer": "port-forward", "db_bytes": 41_884_672,
         "last_fetch_seconds": 2.4, "last_fetch_bytes": 41_884_672,
         "note": "already cached"}
_LOCAL = {"source": "local-disk", "fetch_required": False, "cached": True,
          "transfer": "none", "note": "local disk"}


class _Ctx:
    """Captures what would be sent to the client as a log notification."""

    def __init__(self):
        self.messages = []

    async def info(self, message):
        self.messages.append(message)


def _use(monkeypatch, client):
    monkeypatch.setattr(data_access.service_access, "service_client", lambda: client)
    return client


def test_cold_campaign_is_announced_before_the_wait(monkeypatch):
    _use(monkeypatch, _Client(_COLD))
    ctx = _Ctx()

    asyncio.run(results.query_campaign_data_sql("camp-1", "SELECT 1", ctx=ctx))

    assert len(ctx.messages) == 1
    notice = ctx.messages[0]
    # Names the size and the slow transport, so the caller can repeat *why*, not just that.
    assert "39.9 MiB" in notice
    assert "port-forward" in notice
    assert "camp-1" in notice


def test_cold_campaign_also_warns_for_clients_that_drop_notifications(monkeypatch, caplog):
    _use(monkeypatch, _Client(_COLD))

    with caplog.at_level("WARNING", logger="robovast.mcp_server.data_access"):
        asyncio.run(results.query_campaign_data_sql("camp-1", "SELECT 1"))

    # A warning is what the MCP server's middleware attaches to the tool result.
    assert any("fetching this campaign's query databases" in r.message.lower()
               for r in caplog.records)


def test_warm_campaign_says_nothing(monkeypatch):
    _use(monkeypatch, _Client(_WARM))
    ctx = _Ctx()

    result = asyncio.run(results.query_campaign_data_sql("camp-1", "SELECT 1", ctx=ctx))

    assert ctx.messages == []
    assert result["fetch"]["cold"] is False


def test_local_service_never_announces(monkeypatch):
    """The local backend transfers nothing, so there is nothing to warn about — ever."""
    _use(monkeypatch, _Client(_LOCAL))
    ctx = _Ctx()

    result = asyncio.run(results.query_campaign_data_sql("camp-1", "SELECT 1", ctx=ctx))

    assert ctx.messages == []
    assert result["fetch"] == {"source": "local-disk", "transfer": "none", "cold": False}


def test_result_carries_the_measured_cost(monkeypatch):
    """After a cold call the status is re-read, so the cost reported is that call's own."""
    client = _use(monkeypatch, _Client(_COLD))

    # The service records the cost during the call; model that by warming up afterwards.
    original = client.campaign_data_status
    calls = {"n": 0}

    def status(campaign_id):
        calls["n"] += 1
        client._status = _COLD if calls["n"] == 1 else _WARM
        return original(campaign_id)

    client.campaign_data_status = status
    result = asyncio.run(results.query_campaign_data_sql("camp-1", "SELECT 1"))

    assert result["fetch"]["cold"] is True
    assert result["fetch"]["seconds"] == 2.4
    assert result["fetch"]["bytes"] == 41_884_672


def test_probe_failure_does_not_fail_the_query(monkeypatch):
    """An older service has no such route; the query must still run."""
    client = _use(monkeypatch, _Client(raises=RuntimeError("404 Not Found")))
    ctx = _Ctx()

    result = asyncio.run(results.query_campaign_data_sql("camp-1", "SELECT 1", ctx=ctx))

    assert result["rows"] == [{"n": 1}]
    assert client.queries == 1
    assert ctx.messages == []
    # Nothing is claimed about a transfer that could not be checked.
    assert "fetch" not in result


def test_no_service_is_not_an_unknown(monkeypatch):
    """Without a service the campaign is read from local disk: nothing to fetch."""
    _use(monkeypatch, None)

    assert data_access.data_status("camp-1") is None
    assert data_access.announce_pending_fetch("camp-1") == (None, None)


def test_preflight_tool_reports_the_status(monkeypatch):
    _use(monkeypatch, _Client(_COLD))

    status = results.campaign_data_status("camp-1")

    assert status["cached"] is False
    assert status["transfer"] == "port-forward"
    assert status["db_bytes"] == 41_884_672


def test_preflight_tool_explains_a_missing_service(monkeypatch):
    _use(monkeypatch, None)

    assert "error" in results.campaign_data_status("camp-1")


def test_announced_once_not_once_per_layer(monkeypatch, caplog):
    """The tool announces and the chokepoint reports; only one of them may probe.

    Both used to, which logged the warning twice and made the post-call status read look
    like the pre-call one — so the measured cost was reported as no cost at all.
    """
    client = _use(monkeypatch, _Client(_COLD))
    probes = {"n": 0}
    original = client.campaign_data_status

    def counting(campaign_id):
        probes["n"] += 1
        return original(campaign_id)

    client.campaign_data_status = counting
    with caplog.at_level("WARNING", logger="robovast.mcp_server.data_access"):
        asyncio.run(results.query_campaign_data_sql("camp-1", "SELECT 1", ctx=_Ctx()))

    # One probe before the call, one after to read the cost it incurred.
    assert probes["n"] == 2
    assert sum("fetching this campaign" in r.message for r in caplog.records) == 1


def test_queued_behind_another_fetch_is_said_so(monkeypatch):
    _use(monkeypatch, _Client({**_COLD, "fetch_in_progress": True}))
    ctx = _Ctx()

    asyncio.run(results.query_campaign_data_sql("camp-1", "SELECT 1", ctx=ctx))

    assert "already fetching" in ctx.messages[0]
