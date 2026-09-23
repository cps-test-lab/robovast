# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The campaign listing's ``sort``/``order``, on every surface that carries it.

The order is the service's to apply -- it sorts before ``limit``/``offset`` cut the page, so
no client can re-apply it -- which is why each surface is only asked to hand the two values
through unchanged, and the transport is asked the ordering questions.
"""

import contextlib

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from tests.service.null_lane import NullLane
from robovast.service.interface import (CampaignSummary, ListCampaignsRequest,
                                        ListCampaignsResponse, Routes)
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore


@pytest.fixture
def transport(tmp_path):
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    return NullLane(store=store)


def _campaign(transport, cid: str, created_at: float, *, size=None, ended=True) -> None:
    """A campaign dir with a recorded start and, when *ended*, a terminal outcome.

    The size goes where the controller records it -- ``outcome.json`` -- so the listing
    reads it by the path the service uses.
    """
    from robovast.client.status import Status
    from robovast.common.campaign_data import write_execution_outcome
    from robovast.common.store import STORE_FILENAME, CampaignStore
    from robovast.execution.control_server import Phase

    cdir = transport._campaigns_root() / cid
    cdir.mkdir(parents=True)
    with CampaignStore(cdir / STORE_FILENAME) as store:
        store.create_campaign(cid, {}, mode="batch", config_dir="_config",
                              created_at=created_at)
    if ended:
        write_execution_outcome(cdir, Status(phase=Phase.FINISHED,
                                             phase_since=created_at + 10,
                                             results_bytes=size))


def _mark_live(transport, cid: str) -> None:
    from robovast.common.store import read_campaign_created_at
    from robovast.execution.control_server import ControllerState
    from robovast.service.service_base import _TrackedCampaign

    state = ControllerState()
    state.set_phase("running")
    entry = _TrackedCampaign(cid, str(transport._campaigns_root()), state)
    entry.created_at = read_campaign_created_at(transport.campaign_dir(cid)) or entry.created_at
    with transport._lock:
        transport._campaigns[cid] = entry


def _ids(transport, **kw) -> list[str]:
    return [c.campaign_id for c in
            transport.list_campaigns(ListCampaignsRequest(**kw)).campaigns]


@pytest.fixture
def sized(transport):
    """Three measured campaigns, one unmeasured, one live -- each with a distinct start."""
    _campaign(transport, "small-2026-07-01-120000", 1_000.0, size=10)
    _campaign(transport, "large-2026-07-02-120000", 2_000.0, size=1_000)
    _campaign(transport, "medium-2026-07-03-120000", 3_000.0, size=100)
    _campaign(transport, "unmeasured-2026-07-04-120000", 4_000.0, size=None)
    _campaign(transport, "live-2026-06-01-120000", 500.0, ended=False)
    _mark_live(transport, "live-2026-06-01-120000")
    return transport


def test_the_default_is_newest_first_with_the_live_campaign_leading(sized):
    assert _ids(sized) == [
        "live-2026-06-01-120000", "unmeasured-2026-07-04-120000",
        "medium-2026-07-03-120000", "large-2026-07-02-120000", "small-2026-07-01-120000"]


def test_recent_ascending_reverses_the_order_but_not_the_live_group(sized):
    assert _ids(sized, sort="recent", order="asc") == [
        "live-2026-06-01-120000", "small-2026-07-01-120000", "large-2026-07-02-120000",
        "medium-2026-07-03-120000", "unmeasured-2026-07-04-120000"]


def test_size_descending_puts_the_largest_first(sized):
    assert _ids(sized, sort="size") == [
        "live-2026-06-01-120000", "large-2026-07-02-120000", "medium-2026-07-03-120000",
        "small-2026-07-01-120000", "unmeasured-2026-07-04-120000"]


def test_an_unknown_size_comes_last_in_both_directions(sized):
    """An unmeasured campaign is not the smallest: ascending must not lead with it."""
    assert _ids(sized, sort="size", order="asc") == [
        "live-2026-06-01-120000", "small-2026-07-01-120000", "medium-2026-07-03-120000",
        "large-2026-07-02-120000", "unmeasured-2026-07-04-120000"]


def test_the_page_is_cut_from_the_sorted_list(sized):
    assert _ids(sized, sort="size", limit=2, offset=1) == [
        "large-2026-07-02-120000", "medium-2026-07-03-120000"]


def test_the_row_shows_the_size_it_was_sorted_by(sized):
    rows = sized.list_campaigns(ListCampaignsRequest(sort="size")).campaigns
    assert [r.results_bytes for r in rows] == [None, 1_000, 100, 10, None]


def test_campaigns_without_a_size_keep_recency_among_themselves(transport):
    _campaign(transport, "older-2026-07-01-120000", 1_000.0)
    _campaign(transport, "newer-2026-07-02-120000", 2_000.0)
    for order in ("desc", "asc"):
        assert _ids(transport, sort="size", order=order) == [
            "newer-2026-07-02-120000", "older-2026-07-01-120000"]


@pytest.mark.parametrize("field,value", [("sort", "name"), ("order", "up")])
def test_an_unknown_value_is_refused_not_defaulted(field, value):
    with pytest.raises(ValidationError):
        ListCampaignsRequest(**{field: value})


def test_sorting_by_size_builds_a_summary_only_for_the_page(sized, monkeypatch):
    built = []
    real = type(sized)._summary_for
    monkeypatch.setattr(type(sized), "_summary_for",
                        lambda self, cid: built.append(cid) or real(self, cid))
    sized.list_campaigns(ListCampaignsRequest(sort="size", limit=2))
    assert len(built) == 2


def test_a_settled_size_is_read_once_including_an_unmeasured_one(sized, monkeypatch):
    """The SSE stream repeats the listing once a second; the size must not cost a read each time."""
    from robovast.service import service_base

    reads = []
    real = service_base.read_campaign_results_bytes
    monkeypatch.setattr(service_base, "read_campaign_results_bytes",
                        lambda d: reads.append(d.name) or real(d))
    sized.list_campaigns(ListCampaignsRequest(sort="size"))
    first = sorted(reads)
    sized.list_campaigns(ListCampaignsRequest(sort="size"))
    assert sorted(reads) == first, "a second listing re-read a settled record"
    assert "unmeasured-2026-07-04-120000" in first
    # The live campaign answers from its entry, never from disk.
    assert "live-2026-06-01-120000" not in first


def test_a_size_is_read_again_until_the_record_settles(transport):
    """The other half of that memo: it holds a terminal record's answer only. A campaign whose
    driver has recorded no ending may still record one, and a remembered "unmeasured" would
    outlive the record that replaced it."""
    from robovast.client.status import Status
    from robovast.common.campaign_data import write_execution_outcome
    from robovast.execution.control_server import Phase

    cid = "late-2026-07-01-120000"
    _campaign(transport, cid, 1_000.0, ended=False)
    assert transport._results_bytes_for(cid) is None                # noqa: SLF001

    write_execution_outcome(transport.campaign_dir(cid),
                            Status(phase=Phase.FINISHED, phase_since=1_010.0,
                                   results_bytes=512))

    assert transport._results_bytes_for(cid) == 512                 # noqa: SLF001


def test_the_default_order_reads_no_size(sized, monkeypatch):
    from robovast.service import service_base

    monkeypatch.setattr(service_base, "read_campaign_results_bytes",
                        lambda d: pytest.fail("the default listing read a size"))
    sized.list_campaigns()


def test_a_reactivated_campaign_keeps_its_size(transport):
    """A re-triggered step answers for the campaign from a fresh entry; that entry must carry
    the size measured when the campaign ended, or it lists and sorts as unmeasured."""
    import threading

    _campaign(transport, "done-2026-07-01-120000", 1_000.0, size=4_096)
    release = threading.Event()
    transport._dispatch_background("done-2026-07-01-120000", phase="sharing",
                                   work=lambda state: release.wait(5))
    try:
        row = transport.list_campaigns(ListCampaignsRequest(sort="size")).campaigns[0]
        assert row.results_bytes == 4_096
    finally:
        release.set()


# -- HTTP ----------------------------------------------------------------------------


def _app(transport):
    from robovast.service.app import build_app
    return build_app(transport)


def test_the_route_hands_the_order_through(sized):
    from fastapi.testclient import TestClient

    with TestClient(_app(sized)) as client:
        body = client.get(Routes.CAMPAIGNS, params={"sort": "size", "order": "asc"}).json()
    assert [c["campaign_id"] for c in body["campaigns"]][1] == "small-2026-07-01-120000"


@pytest.mark.parametrize("route", [Routes.CAMPAIGNS, Routes.CAMPAIGNS_STREAM])
@pytest.mark.parametrize("params", [{"sort": "name"}, {"order": "up"}])
def test_the_routes_refuse_an_unknown_value(transport, route, params):
    from fastapi.testclient import TestClient

    with TestClient(_app(transport)) as client:
        assert client.get(route, params=params).status_code == 422


def test_the_stream_is_ordered_like_the_pull(sized):
    import json
    import threading

    from fastapi.testclient import TestClient

    app = _app(sized)
    timer = threading.Timer(2, lambda: setattr(app.state, "should_exit", lambda: True))
    timer.start()
    try:
        with TestClient(app) as client:
            with client.stream("GET", Routes.CAMPAIGNS_STREAM,
                               params={"sort": "size", "order": "desc"}) as response:
                body = "".join(response.iter_text())
    finally:
        timer.cancel()
    frame = next(line[len("data: "):] for line in body.splitlines()
                 if line.startswith("data: "))
    listed = [c["campaign_id"] for c in json.loads(frame)["campaigns"]]
    assert listed == _ids(sized, sort="size", order="desc")


def test_the_http_client_sends_the_order(monkeypatch):
    from robovast.service.http_client import RobovastClient

    sent = {}
    client = RobovastClient("http://service.example")
    monkeypatch.setattr(client, "_get", lambda route, **params: sent.update(params)
                        or {"campaigns": [], "total": 0})
    client.list_campaigns(ListCampaignsRequest(sort="size", order="asc"))
    assert sent["sort"] == "size" and sent["order"] == "asc"


# -- CLI -----------------------------------------------------------------------------


@pytest.fixture
def cli_service(monkeypatch):
    from robovast.client import campaign_cli

    asked = []

    class _Client:
        def list_campaigns(self, request):
            asked.append(request)
            return ListCampaignsResponse(total=2, campaigns=[
                CampaignSummary(campaign_id="big-2026-07-01-120000", phase="finished",
                                results_bytes=3 * 1024 * 1024),
                CampaignSummary(campaign_id="run-2026-07-02-120000", phase="running")])

    @contextlib.contextmanager
    def _client(*_a, **_k):
        yield _Client(), "fake service"

    monkeypatch.setattr(campaign_cli, "service_client", _client)
    return asked


def test_the_cli_asks_for_the_order_and_shows_the_size(cli_service):
    from robovast.client import campaign_cli

    result = CliRunner().invoke(campaign_cli.campaign, ["list", "--sort", "size", "--asc"])
    assert result.exit_code == 0, result.output
    assert (cli_service[0].sort, cli_service[0].order) == ("size", "asc")
    lines = result.output.splitlines()
    assert any("big-2026-07-01-120000" in l and "3.0 MiB" in l for l in lines)
    # No size recorded reads as a dash, not as 0.
    assert any("run-2026-07-02-120000" in l and " - " in l for l in lines)


def test_the_cli_defaults_to_newest_first(cli_service):
    from robovast.client import campaign_cli

    assert CliRunner().invoke(campaign_cli.campaign, ["list"]).exit_code == 0
    assert (cli_service[0].sort, cli_service[0].order) == ("recent", "desc")


def test_the_cli_refuses_an_unknown_sort(cli_service):
    from robovast.client import campaign_cli

    result = CliRunner().invoke(campaign_cli.campaign, ["list", "--sort", "name"])
    assert result.exit_code != 0
    assert not cli_service


# -- MCP -----------------------------------------------------------------------------


def test_the_mcp_tool_hands_the_order_through_and_reports_the_size(monkeypatch):
    from robovast.mcp_server import service_access
    from robovast.mcp_server.plugins.results import list_campaigns

    asked = []

    class _Client:
        def list_campaigns(self, request):
            asked.append(request)
            return ListCampaignsResponse(total=2, campaigns=[
                CampaignSummary(campaign_id="big", phase="finished", results_bytes=2048),
                CampaignSummary(campaign_id="running", phase="running")])

    monkeypatch.setattr(service_access, "service_client", _Client)
    result = list_campaigns(sort="size", order="asc")
    assert (asked[0].sort, asked[0].order) == ("size", "asc")
    assert result["campaigns"][0]["results_bytes"] == 2048
    # Omitted, not null: a campaign with no recorded size has none.
    assert "results_bytes" not in result["campaigns"][1]

    list_campaigns(running_only=True, sort="size")
    assert asked[-1].sort == "size", "running_only must walk the list in the order asked for"


def test_the_mcp_tool_reports_an_unknown_value_as_an_error(monkeypatch):
    from robovast.mcp_server import service_access
    from robovast.mcp_server.plugins.results import list_campaigns

    monkeypatch.setattr(service_access, "service_client", lambda: pytest.fail(
        "an unknown value must be refused before anything is asked"))
    assert "error" in list_campaigns(sort="name")
