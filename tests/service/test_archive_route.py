# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""GET /campaigns/{id}/archive -- the service streams a campaign as a tar.gz.

The archive is streamed rather than refused, has one top-level entry named for the campaign,
and answers a missing campaign with 404.

``.cache/`` is excluded: it holds the campaign's built tables, which are rebuilt from the
records wherever a table is next named, and shipping it would make an archive's size depend on
what somebody happened to query.
"""

import tarfile
import threading
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

from robovast.service.app import build_app
from robovast.service.interface import Routes
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore
from tests.service.null_service import NullService

_CAMPAIGN = "camp-2026-01-01-000000"


def _null_service(tmp_path) -> NullService:
    """A real NullService with its results root under *tmp_path*.

    Constructed rather than ``__new__``-ed: streaming an archive goes through the campaign-dir
    resolution the constructor's state backs, so a hand-stubbed object fails on bookkeeping
    instead of on the thing under test.
    """
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    lt = NullService(store=store)
    lt._campaigns_root = lambda: tmp_path / "results"
    return lt


@pytest.fixture(name="env")
def _env(monkeypatch, tmp_path):
    transport = _null_service(tmp_path)
    root = tmp_path / "results" / _CAMPAIGN
    (root / "_config").mkdir(parents=True)
    (root / "_config" / "campaign.vast").write_text("configuration:\n  name: x\n",
                                                    encoding="utf-8")
    (root / "_execution").mkdir()
    (root / "_execution" / "execution.yaml").write_text("runs: 1\n", encoding="utf-8")
    # Excluded: the built tables are rebuilt from the records.
    (root / ".cache" / "tables").mkdir(parents=True)
    (root / ".cache" / "MANIFEST.json").write_text("{}", encoding="utf-8")
    with TestClient(build_app(transport)) as client:
        yield client


def _members(payload: bytes) -> set:
    with tarfile.open(fileobj=BytesIO(payload), mode="r:gz") as tar:
        return set(tar.getnames())


def test_the_service_streams_a_campaign_from_its_results_root(env):
    """The service serves the archive of a campaign in its results root; it does not refuse it."""
    resp = env.get(Routes.campaign_archive(_CAMPAIGN))
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-disposition"] == f'attachment; filename="{_CAMPAIGN}.tar.gz"'

    names = _members(resp.content)
    # One top-level entry, named for the campaign — the shape `import_archive` requires, so a
    # download from here is directly importable elsewhere.
    assert {name.split("/")[0] for name in names} == {_CAMPAIGN}
    assert f"{_CAMPAIGN}/_config/campaign.vast" in names


def test_the_table_cache_is_left_out(env):
    """``.cache`` is rebuilt from the records, not campaign content."""
    names = _members(env.get(Routes.campaign_archive(_CAMPAIGN)).content)
    assert not [n for n in names if "/.cache" in n], \
        f"the table cache was shipped in the archive: {sorted(names)}"


def test_an_unknown_campaign_is_a_404(env):
    """A missing campaign is absent, not a conflict."""
    resp = env.get(Routes.campaign_archive("nope-2026-01-01-000000"))
    assert resp.status_code == 404


def test_the_route_needs_no_workspace_store(tmp_path, monkeypatch):
    """Archives are campaign output, so the route must not depend on workspaces.

    Kept because the local implementation resolves a campaign directory rather than a
    workspace: a service with no workspaces configured answers 501 for project routes, and an
    archive download must not be dragged into that.
    """
    lt = object.__new__(NullService)
    lt._campaigns = {}
    lt._lock = threading.Lock()
    lt.store = None
    lt._campaigns_root = lambda: tmp_path / "results"
    root = tmp_path / "results" / _CAMPAIGN / "_config"
    root.mkdir(parents=True)
    (root / "campaign.vast").write_text("configuration:\n  name: x\n", encoding="utf-8")

    with TestClient(build_app(lt)) as client:
        resp = client.get(Routes.campaign_archive(_CAMPAIGN))
    assert resp.status_code == 200, resp.text
    assert _CAMPAIGN in resp.headers["content-disposition"]


def test_a_running_campaign_downloads_as_an_incomplete_snapshot(tmp_path, monkeypatch):
    """A campaign still being written to is served, and says so in its own name.

    Two things have to hold at once for that to be safe, and they are what this covers: the
    name a browser saves the file under carries ``incomplete``, and the archive carries the
    marker ``ingest`` reads -- because a snapshot has the shape of a finished campaign in
    every other respect, and neither a directory listing nor an import can see what is
    simply absent from it.
    """
    from robovast.execution.campaign_archive import SNAPSHOT_MEMBER

    transport = _null_service(tmp_path)
    root = tmp_path / "results" / _CAMPAIGN / "_config"
    root.mkdir(parents=True)
    (root / "campaign.vast").write_text("configuration:\n  name: x\n", encoding="utf-8")
    monkeypatch.setattr(type(transport), "campaign_is_live", lambda self, cid: True)

    with TestClient(build_app(transport)) as client:
        resp = client.get(Routes.campaign_archive(_CAMPAIGN))

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-disposition"] == \
        f'attachment; filename="{_CAMPAIGN}.incomplete.tar.gz"'
    assert f"{_CAMPAIGN}/{SNAPSHOT_MEMBER}" in _members(resp.content)


def test_a_finished_campaign_is_not_marked_incomplete(env):
    """The marker is a claim about this campaign, so it must not ride along on every one.

    A snapshot marker on a finished campaign would degrade every import of it forever, and
    the archive carries no way to take it back.
    """
    from robovast.execution.campaign_archive import SNAPSHOT_MEMBER

    resp = env.get(Routes.campaign_archive(_CAMPAIGN))
    assert resp.headers["content-disposition"] == f'attachment; filename="{_CAMPAIGN}.tar.gz"'
    assert f"{_CAMPAIGN}/{SNAPSHOT_MEMBER}" not in _members(resp.content)
