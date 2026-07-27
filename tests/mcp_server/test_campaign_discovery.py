# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Campaign discovery works without a ``.robovast_project``.

``list_campaigns`` is the only read tool with no absolute-path escape, so it is the
one place where "no project initialized" used to be a dead end. There is no
service-side project any more — a campaign runs a workspace's ``.vast`` — so the
local results root is resolved by precedence (a CWD project's ``results_dir``, else
the dir beside the workspaces store) and an absent root is simply empty.
"""

import pytest

from robovast.mcp_server import results_resolver


@pytest.fixture
def no_project(monkeypatch, tmp_path):
    """No ``.robovast_project`` anywhere; workspaces store rooted under tmp_path."""
    monkeypatch.setattr(
        "robovast.common.cli.project_config.ProjectConfig.load",
        staticmethod(lambda *a, **k: None))
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
    """The tool's first page is the *newest* campaigns, whatever the names sort like.

    It used to slice an ascending disk scan, so ``limit`` returned the oldest campaigns
    and "what did I just run?" landed on the last page.
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
