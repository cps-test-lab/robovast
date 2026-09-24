# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``export_campaign`` and ``get_export_status`` — an export as an MCP handle.

The tool starts the export and hands back what a caller needs to finish the job away from
this host: the id to poll, the status as it stands, the route, a URL when the service
declares an origin, and the ``vast campaign export`` command with every option filled in.
Nothing is downloaded here.
"""

from types import SimpleNamespace

import pytest

from robovast.mcp_server import service_access
from robovast.mcp_server.plugins import results_lifecycle
from robovast.service.interface import ExportRef, ExportRequest, ExportStatus, VersionInfo

_CAMPAIGN = "camp-2026-01-01-000000"


@pytest.fixture(name="calls")
def _calls(monkeypatch):
    seen = []

    class _Client:
        base_url = "http://127.0.0.1:8800"

        @staticmethod
        def version():
            return VersionInfo(robovast_version="test", backend="docker")

        @staticmethod
        def create_export(campaign_id, request):
            seen.append(("create", campaign_id, request))
            return ExportRef(export_id="0123456789ab", url=f"/data/campaigns/{campaign_id}"
                                                            "/exports/0123456789ab")

        @staticmethod
        def get_export_status(campaign_id, export_id):
            seen.append(("status", campaign_id, export_id))
            return ExportStatus(export_id=export_id, tables={"runs": 2})

    monkeypatch.setattr(service_access, "service_client", _Client)
    return seen


def test_the_export_is_started_as_asked_and_answered_as_a_handle(calls):
    res = results_lifecycle.export_campaign(_CAMPAIGN, tables=["poses", "run_log"],
                                            format="csv", bags="sqlite3", records=False)
    assert "error" not in res
    assert calls[0] == ("create", _CAMPAIGN, ExportRequest(
        tables=["poses", "run_log"], format="csv", bags="sqlite3", records=False))
    assert res["export_id"] == "0123456789ab"
    assert res["status"] == {"export_id": "0123456789ab", "done": False, "error": "",
                             "bytes": 0, "tables": {"runs": 2}, "started_at": None,
                             "finished_at": None}
    assert res["path"] == f"/data/campaigns/{_CAMPAIGN}/exports/0123456789ab"
    assert res["url"] == f"http://127.0.0.1:8800/data/campaigns/{_CAMPAIGN}/exports/0123456789ab"
    assert res["next_step"] == (f"vast campaign export {_CAMPAIGN} --tables poses,run_log "
                                "--format csv --bags sqlite3 --no-records")


def test_the_defaults_are_every_table_as_parquet_with_the_records(calls):
    res = results_lifecycle.export_campaign(_CAMPAIGN)
    assert calls[0][2] == ExportRequest()
    assert res["next_step"] == f"vast campaign export {_CAMPAIGN} --format parquet --bags none"


def test_without_an_origin_the_url_is_omitted(monkeypatch):
    impl = SimpleNamespace(
        version=lambda: VersionInfo(robovast_version="test", backend="kubernetes"),
        create_export=lambda cid, req: ExportRef(export_id="0123456789ab", url="/data/x"),
        get_export_status=lambda cid, eid: ExportStatus(export_id=eid))
    monkeypatch.setattr(service_access, "service_client", lambda: impl)
    res = results_lifecycle.export_campaign(_CAMPAIGN)
    assert "url" not in res
    assert res["path"] == "/data/x"


def test_a_refusal_is_the_services_sentence(monkeypatch):
    def _refuse(cid, req):
        raise ValueError("no table 'nope' in this campaign")
    impl = SimpleNamespace(
        version=lambda: VersionInfo(robovast_version="test", backend="docker"),
        create_export=_refuse)
    monkeypatch.setattr(service_access, "service_client", lambda: impl)
    assert results_lifecycle.export_campaign(_CAMPAIGN, tables=["nope"]) == {
        "error": "no table 'nope' in this campaign"}


def test_the_status_is_the_services_answer(calls):
    res = results_lifecycle.get_export_status(_CAMPAIGN, "0123456789ab")
    assert calls == [("status", _CAMPAIGN, "0123456789ab")]
    assert res["tables"] == {"runs": 2} and res["done"] is False


def test_without_a_service_the_export_fails_loudly(monkeypatch):
    monkeypatch.setattr(service_access, "service_client", lambda: None)
    assert "no robovast-service" in results_lifecycle.export_campaign(_CAMPAIGN)["error"]
