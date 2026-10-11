# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""GET /campaigns/{id}/archive -- the service streams a campaign as a tar.gz.

The archive is streamed rather than refused, has one top-level entry named for the campaign,
and answers a missing campaign with 404.

It carries the campaign's built tables -- the top-level ``.cache/`` manifest and table files,
never the cache's exports or locks, nor a run's own ``.cache`` -- so it opens without building
anything; ``?raw=true`` is the records alone, without the tables and what postprocessing
recorded producing.
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
    (root / ".cache" / "tables" / "poses").mkdir(parents=True)
    (root / ".cache" / "tables" / "poses" / "_compacted-0.parquet").write_bytes(b"PAR1")
    (root / ".cache" / "MANIFEST.json").write_text("{}", encoding="utf-8")
    (root / ".cache" / ".lock").write_text("", encoding="utf-8")
    (root / ".cache" / "exports" / "e1").mkdir(parents=True)
    (root / ".cache" / "exports" / "e1" / "export.tar.gz").write_bytes(b"x")
    (root / "cfg" / "0" / ".cache").mkdir(parents=True)
    (root / "cfg" / "0" / ".cache" / "render.html").write_text("x", encoding="utf-8")
    (root / "cfg" / "0" / "test.xml").write_text("<t/>", encoding="utf-8")
    # What postprocessing produced: its record naming one output, and the metadata.
    (root / "_transient").mkdir()
    (root / "_transient" / "postprocessing.yaml").write_text(
        "entries:\n- output: ../cfg/0/derived.csv\n  plugin: command\n", encoding="utf-8")
    (root / "cfg" / "0" / "derived.csv").write_text("a\n1\n", encoding="utf-8")
    (root / "metadata.yaml").write_text("x: 1\n", encoding="utf-8")
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


def test_the_archive_carries_the_built_tables_and_nothing_else_of_the_cache(env):
    names = _members(env.get(Routes.campaign_archive(_CAMPAIGN)).content)
    cache = sorted(n for n in names if "/.cache" in n)
    assert cache == [f"{_CAMPAIGN}/.cache", f"{_CAMPAIGN}/.cache/MANIFEST.json",
                     f"{_CAMPAIGN}/.cache/tables", f"{_CAMPAIGN}/.cache/tables/poses",
                     f"{_CAMPAIGN}/.cache/tables/poses/_compacted-0.parquet"], cache
    assert f"{_CAMPAIGN}/cfg/0/derived.csv" in names
    assert f"{_CAMPAIGN}/_transient/postprocessing.yaml" in names


def test_raw_is_the_records_without_the_tables_or_what_postprocessing_produced(env):
    resp = env.get(Routes.campaign_archive(_CAMPAIGN), params={"raw": "true"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-disposition"] == \
        f'attachment; filename="{_CAMPAIGN}.raw.tar.gz"'
    names = _members(resp.content)
    assert not [n for n in names if "/.cache" in n], sorted(names)
    for derived in ("cfg/0/derived.csv", "_transient/postprocessing.yaml", "metadata.yaml"):
        assert f"{_CAMPAIGN}/{derived}" not in names, derived
    assert f"{_CAMPAIGN}/cfg/0/test.xml" in names
    assert f"{_CAMPAIGN}/_config/campaign.vast" in names


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
