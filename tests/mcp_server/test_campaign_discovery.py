# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Campaign discovery: which campaigns exist, and who was asked.

``list_campaigns`` is the only read tool with no absolute-path escape, so it is the
one place where "no project initialized" used to be a dead end. There is no
service-side project any more — a campaign runs a workspace's ``.vast`` — so the
local results root is resolved by precedence (a CWD project's ``results_dir``, else
the dir beside the workspaces store) and an absent root is simply empty.

The tool asks the **service** when one answers, because a cluster campaign's durable
home is the object store and it is on no local filesystem at all — a disk-only listing
reported those campaigns as absent, which is the failure that looks like an answer.
"""

import pytest

from robovast.mcp_server import results_resolver, service_access


@pytest.fixture
def no_project(monkeypatch, tmp_path):
    """No ``.robovast_project`` anywhere; workspaces store rooted under tmp_path."""
    monkeypatch.setenv("ROBOVAST_WORKSPACES_ROOT", str(tmp_path / "workspaces"))
    return tmp_path / "results"


def test_list_campaigns_without_project_file(no_project):
    """A campaign in the fallback root is found with no project file at all."""
    (no_project / "camp-2026-01-01-000000").mkdir(parents=True)
    (no_project / "not-a-campaign").mkdir()

    found = [p.name for p in results_resolver.list_campaigns()]
    assert found == ["camp-2026-01-01-000000"]


def test_list_campaigns_empty_when_root_absent(no_project):
    """A fresh service has no results dir yet: that is empty, not an error."""
    assert not no_project.exists()
    assert results_resolver.list_campaigns() == []


def test_list_campaigns_tool_is_newest_first(no_project):
    """Among campaigns at rest, the tool's first page is the *newest*, whatever the names
    sort like.

    It used to slice an ascending disk scan, so ``limit`` returned the oldest campaigns
    and "what did I just run?" landed on the last page. Recency is the second key now —
    live campaigns lead — but neither campaign here is running, so it is the one in
    effect.
    """
    from robovast.common.store import STORE_FILENAME, CampaignStore
    from robovast.mcp_server.plugins.results import list_campaigns

    for cid, created_at in (("zzz-2026-07-01-120000", 1_000.0),
                            ("aaa-2026-07-26-120000", 2_000.0)):
        cdir = no_project / cid
        cdir.mkdir(parents=True)
        with CampaignStore(cdir / STORE_FILENAME) as store:
            store.create_campaign(cid, {}, mode="batch", created_at=created_at)

    # Neither campaign is postprocessed, and both are still listed as ordinary entries:
    # the listing no longer splits on metadata.yaml (which only postprocessing writes),
    # it reports `postprocessed` per campaign, because a campaign without it is still
    # queryable — its runs are in campaign.db.
    result = list_campaigns(limit=1)
    assert result["total"] == 2
    assert [c["campaign_id"] for c in result["campaigns"]] == ["aaa-2026-07-26-120000"]
    assert result["campaigns"][0]["started_at"] == "1970-01-01T00:33:20+00:00"
    assert result["campaigns"][0]["postprocessed"] is False

    # ... and the page after it is the older one, i.e. the ordering is over the whole set.
    assert [c["campaign_id"] for c in list_campaigns(limit=1, offset=1)["campaigns"]] \
        == ["zzz-2026-07-01-120000"]


# -- who answered: the service, or this host's disk ---------------------------


class _FakeListingClient:
    """A service stand-in that knows campaigns this host's disk does not."""

    def list_campaigns(self, request=None):
        from robovast.service.interface import CampaignSummary, ListCampaignsResponse
        return ListCampaignsResponse(total=2, campaigns=[
            CampaignSummary(campaign_id="svc-running", phase="running",
                            description="the pilot"),
            CampaignSummary(campaign_id="svc-done", phase="finished")])


def test_listing_prefers_the_service(monkeypatch, no_project):
    """A cluster campaign lives in the object store, so only the service can list it.

    The disk root here is empty; a disk-only listing therefore answered "no campaigns"
    about a service that has two.
    """
    from robovast.mcp_server.plugins.results import list_campaigns
    monkeypatch.setattr(service_access, "service_client", _FakeListingClient)

    result = list_campaigns()
    assert result["source"] == "service"
    assert [c["campaign_id"] for c in result["campaigns"]] == ["svc-running", "svc-done"]
    assert result["campaigns"][0]["description"] == "the pilot"
    # Omitted, not reported empty: a campaign started without one has no description.
    assert "description" not in result["campaigns"][1]


def test_listing_says_when_it_fell_back_to_disk(no_project):
    """No service is not an error — but "no campaigns here" must name where it looked."""
    from robovast.mcp_server.plugins.results import list_campaigns
    assert list_campaigns()["source"] == "local results root"


def test_running_only_walks_the_whole_list(monkeypatch, no_project):
    """Filtering a single page would hide a long-running campaign started days ago."""
    from robovast.mcp_server.plugins.results import list_campaigns
    monkeypatch.setattr(service_access, "service_client", _FakeListingClient)

    listing = list_campaigns(running_only=True)
    assert [c["campaign_id"] for c in listing["campaigns"]] == ["svc-running"]
    assert listing["total"] == 1  # counts the live ones, not the whole corpus


def test_resolve_results_dir_still_raises_for_a_named_campaign(no_project):
    """Naming a campaign under a non-existent root is an error, not silence.

    The asymmetry is deliberate: listing tolerates an absent root, but a caller that
    named something it expects to find must be told the root does not exist -- and
    the message must not say "run 'vast init'", which no longer binds anything.
    """
    with pytest.raises(ValueError, match="No local results directory"):
        results_resolver.resolve_results_dir()


def test_absolute_campaign_path_needs_no_root(no_project, tmp_path):
    """The absolute-path escape keeps working for an archived tree."""
    archived = tmp_path / "elsewhere" / "camp-2026-01-01-000000"
    archived.mkdir(parents=True)
    assert results_resolver.resolve_campaign_path(str(archived)) == archived
