# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Local campaigns are workspace-independent: they share one results root.

Campaigns created from a workspace must NOT land under ``<workspace>/results``
(where the service's readers never look and ``delete_workspace`` would take them
along). They belong in the shared :meth:`LocalTransport._campaigns_root`, which
every read path — list / status / data query — resolves.
"""

from pathlib import Path

import pytest

from robovast.service.client import LocalTransport
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore


@pytest.fixture
def transport(monkeypatch, tmp_path):
    # No CWD project → _campaigns_root falls back to the dir beside the workspaces
    # (here <tmp_path>/results, kept unique per test by rooting under tmp_path).
    monkeypatch.setattr(
        "robovast.common.cli.project_config.ProjectConfig.load",
        staticmethod(lambda *a, **k: None))
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    return LocalTransport(store=store)


def _make_workspace(transport) -> str:
    ws = transport.store.registry.create(name="w")
    wid = ws["workspace_id"]
    transport.store.write_file(wid, "sim.vast", "configuration:\n  name: x\n")
    return wid


def test_workspace_project_results_go_to_shared_root(transport):
    """_project_for_workspace points results at the shared root, not the workspace."""
    wid = _make_workspace(transport)
    project = transport._project_for_workspace(wid)

    shared = transport._campaigns_root()
    ws_results = transport.store.registry.root / wid / "results"

    assert Path(project.results_dir) == shared
    assert Path(project.results_dir) != ws_results
    # config still resolves from inside the workspace
    assert Path(project.config_path).name == "sim.vast"


def test_shared_root_is_stable_across_workspaces(transport):
    """Two different workspaces resolve to the same campaigns root."""
    w1 = _make_workspace(transport)
    w2 = _make_workspace(transport)
    assert transport._project_for_workspace(w1).results_dir == \
        transport._project_for_workspace(w2).results_dir


def test_list_and_status_read_the_shared_root(transport):
    """A campaign dir placed in the shared root is listed and reconstructable."""
    root = transport._campaigns_root()
    cid = "campaign-2026-07-16-101010"
    (root / cid).mkdir(parents=True)

    listed = {c.campaign_id for c in transport.list_campaigns().campaigns}
    assert cid in listed
    # get_status for an untracked campaign reconstructs from that same root
    assert transport.get_status(cid).phase != "unknown"
    assert transport._campaign_dir(cid) == root / cid


def test_started_at_comes_from_the_store(transport):
    """list_campaigns reports the real recorded start time (campaign.created_at)."""
    from datetime import datetime, timezone

    from robovast.common.store import CampaignStore, STORE_FILENAME

    root = transport._campaigns_root()
    cid = "campaign-2026-07-16-131415"
    cdir = root / cid
    cdir.mkdir(parents=True)
    with CampaignStore(cdir / STORE_FILENAME) as store:
        store.create_campaign(cid, {}, mode="batch", config_dir="_config")
        created_at = store.list_campaigns()[0]["created_at"]

    summary = next(c for c in transport.list_campaigns().campaigns if c.campaign_id == cid)
    assert summary.started_at == datetime.fromtimestamp(
        created_at, tz=timezone.utc).isoformat()


def test_started_at_none_without_store(transport):
    """A campaign dir with no campaign.db yields started_at=None (not an error)."""
    root = transport._campaigns_root()
    cid = "campaign-2026-07-16-161718"
    (root / cid).mkdir(parents=True)
    summary = next(c for c in transport.list_campaigns().campaigns if c.campaign_id == cid)
    assert summary.started_at is None


def test_deleting_workspace_keeps_campaigns(transport):
    """Campaigns survive deletion of the workspace that authored them."""
    wid = _make_workspace(transport)
    root = transport._project_for_workspace(wid).results_dir
    cid = "campaign-2026-07-16-121212"
    (Path(root) / cid).mkdir(parents=True)

    transport.store.registry.delete(wid)

    assert (Path(root) / cid).is_dir()
    assert cid in {c.campaign_id for c in transport.list_campaigns().campaigns}
