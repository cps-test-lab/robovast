# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A described table names its schema under ``schema`` on the service path too.

The local path builds plain dicts with ``schema``; the service path returns
:class:`~robovast.service.interface.DataTable`, whose field is ``schema_`` because the bare
name collides with a pydantic attribute. Dumped without its alias, every consumer looking up
``schema`` -- the MCP describe, the nav plugin's filter -- finds nothing, and silently.
"""

from robovast.mcp_server import data_access, service_access
from robovast.service.interface import DataDescribe, DataTable


class _Client:
    def describe_campaign_data(self, campaign_id):
        return DataDescribe(campaign_id=campaign_id, tables=[
            DataTable(schema="campaign", table="run"),
            DataTable(schema="main", table="poses", kind="table"),
        ])


def test_the_service_path_reports_schema_under_the_key_the_local_path_uses(monkeypatch):
    monkeypatch.setattr(service_access, "service_client", _Client)
    described = data_access.describe("camp-2026-09-21-120000")
    assert [(t["schema"], t["table"]) for t in described["tables"]] == [
        ("campaign", "run"), ("main", "poses")]
    assert all("schema_" not in t for t in described["tables"])
