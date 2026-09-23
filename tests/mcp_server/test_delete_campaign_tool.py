# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``delete_campaign`` takes one id or a list, and answers one outcome per id either way.

One tool rather than a second one for several: every tool's description is paid for on every
request, and deleting one campaign is deleting a list of one. Both shapes go through the same
interface operation, so a single id is reported exactly as it would be among many.
"""

import pytest

from robovast.mcp_server import service_access
from robovast.mcp_server.plugins import results_lifecycle
from robovast.service.interface import CampaignDeletion, DeleteCampaignsResponse


class _Service:
    def __init__(self):
        self.asked = []

    def delete_campaigns(self, request):
        self.asked.append(request.campaign_ids)
        return DeleteCampaignsResponse(results=[
            CampaignDeletion(campaign_id=cid, outcome="deleted", ok=True, message="Deleted.")
            for cid in request.campaign_ids])


@pytest.fixture(name="service")
def _service(monkeypatch):
    svc = _Service()
    monkeypatch.setattr(service_access, "service_client", lambda: svc)
    return svc


def test_one_id_is_a_list_of_one(service):
    out = results_lifecycle.delete_campaign("a-2026-09-01-101500")
    assert service.asked == [["a-2026-09-01-101500"]]
    assert [r["outcome"] for r in out["results"]] == ["deleted"]


def test_a_list_is_sent_as_given(service):
    ids = ["a-2026-09-01-101500", "b-2026-09-01-101500"]
    out = results_lifecycle.delete_campaign(ids)
    assert service.asked == [ids]
    assert [r["campaign_id"] for r in out["results"]] == ids


@pytest.mark.parametrize("empty", ["", [], ["a-2026-09-01-101500", ""]])
def test_an_empty_id_is_refused_before_the_service_is_asked(service, empty):
    assert "error" in results_lifecycle.delete_campaign(empty)
    assert service.asked == []


def test_no_service_is_said_rather_than_worked_around(monkeypatch):
    monkeypatch.setattr(service_access, "service_client", lambda: None)
    assert "error" in results_lifecycle.delete_campaign("a-2026-09-01-101500")
