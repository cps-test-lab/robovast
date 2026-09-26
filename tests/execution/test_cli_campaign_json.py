# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast campaign status --json`` and ``vast campaign list --json`` answer as the MCP tools do.

An agent may reach a campaign through either surface, and the two must not disagree about
what it is doing: the same fake service is asked through both, and the documents compared.
Stdout carries the document alone -- the target line goes to stderr -- so it parses.
"""

import contextlib
import json

from click.testing import CliRunner

from robovast.client import campaign_cli
from robovast.client.status import Phase, Status
from robovast.mcp_server import service_access
from robovast.mcp_server.plugins import execution, results
from robovast.service.interface import CampaignSummary, ListCampaignsResponse


class _Client:
    def get_status(self, campaign_id):
        return Status(phase=Phase.FINISHED, campaign_id=campaign_id,
                      runs={"completed": 8, "total": 8}, postprocessed=False,
                      postprocessing_error="conversion died")

    def list_campaigns(self, request=None):
        return ListCampaignsResponse(total=2, campaigns=[
            CampaignSummary(campaign_id="c-live", phase="running", priority=2),
            CampaignSummary(campaign_id="c-done", phase="finished", description="pilot",
                            results_bytes=1024)])


def _cli(monkeypatch, *args):
    @contextlib.contextmanager
    def _service(*_a, **_k):
        yield _Client(), "fake service"

    monkeypatch.setattr(campaign_cli, "service_client", _service)
    result = CliRunner().invoke(campaign_cli.campaign, list(args))
    assert result.exit_code == 0, result.output + result.stderr
    assert "Target: fake service" in result.stderr
    return json.loads(result.stdout)


def _drop_clock(report: dict) -> dict:
    """The ages are measured at the moment of each read, so two reads differ in them."""
    return {k: v for k, v in report.items() if not k.endswith("_age_s")}


def test_status_json_is_what_get_campaign_status_returns(monkeypatch):
    printed = _cli(monkeypatch, "status", "c1", "--json")
    monkeypatch.setattr(service_access, "service_client", _Client)
    assert _drop_clock(printed) == _drop_clock(execution.get_campaign_status("c1"))
    assert "conversion died" in printed["next_step"]


def test_list_json_carries_the_campaigns_list_campaigns_returns(monkeypatch):
    printed = _cli(monkeypatch, "list", "--json")
    monkeypatch.setattr(service_access, "service_client", _Client)
    tool = results.list_campaigns()
    assert printed["campaigns"] == tool["campaigns"]
    assert printed["total"] == tool["total"] == 2
    assert printed["campaigns"][0]["priority"] == 2
