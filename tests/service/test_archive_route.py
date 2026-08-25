# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""GET /campaigns/{id}/archive — every lane serves it, and a caller cannot tell which did.

A cluster service streams the postprocessed tar.gz out of the object store with no scratch; a
local one tars its own results directory. That symmetry is the point, and it is what the route
used to lack: a local service answered 409 ("the results are already on this host"), which is
true of the *service* and useless to a browser, a colleague, or anyone importing the campaign
somewhere else. So the interesting property to defend is that the two lanes agree — same
exclusions, same streaming, same status codes — because a client that has to ask which backend
it is talking to before it can offer a download is a client that will get it wrong.

``_postproc/`` is excluded alongside ``.cache``: it is postprocessing's staging area, not part
of the campaign, and shipping it would make an archive's size depend on when it was taken.
"""

import tarfile
import threading
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

from robovast.service.app import build_app
from robovast.service.client import LocalTransport
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore

_CAMPAIGN = "camp-2026-01-01-000000"


def _local_transport(tmp_path) -> LocalTransport:
    """A real LocalTransport with its results root under *tmp_path*.

    Constructed rather than ``__new__``-ed: streaming an archive goes through the campaign-dir
    resolution the constructor's state backs, so a hand-stubbed object fails on bookkeeping
    instead of on the thing under test.
    """
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    lt = LocalTransport(store=store)
    lt._campaigns_root = lambda: tmp_path / "results"
    return lt


@pytest.fixture(name="env")
def _env(monkeypatch, tmp_path):
    monkeypatch.setattr("robovast.client.project_config.ProjectConfig.load",
                        staticmethod(lambda *a, **k: None))
    transport = _local_transport(tmp_path)
    root = tmp_path / "results" / _CAMPAIGN
    (root / "_config").mkdir(parents=True)
    (root / "_config" / "campaign.vast").write_text("configuration:\n  name: x\n",
                                                    encoding="utf-8")
    (root / "_execution").mkdir()
    (root / "_execution" / "execution.yaml").write_text("runs: 1\n", encoding="utf-8")
    # Both excluded, and for different reasons: `.cache` is postprocessing's hash cache and
    # `_postproc` its staging tree.
    (root / ".cache").mkdir()
    (root / ".cache" / "hashes.json").write_text("{}", encoding="utf-8")
    (root / "_postproc").mkdir()
    (root / "_postproc" / "scratch.csv").write_text("a,b\n", encoding="utf-8")
    with TestClient(build_app(transport)) as client:
        yield client


def _members(payload: bytes) -> set:
    with tarfile.open(fileobj=BytesIO(payload), mode="r:gz") as tar:
        return set(tar.getnames())


def test_a_local_service_streams_its_own_campaign(env):
    """The local lane serves the archive instead of refusing it.

    It used to answer 409. That made the campaign view hide its own download button behind a
    backend check, and left an operator on a local service with no way to hand a campaign to
    anybody without shell access to the host.
    """
    resp = env.get(f"/campaigns/{_CAMPAIGN}/archive")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-disposition"] == f'attachment; filename="{_CAMPAIGN}.tar.gz"'

    names = _members(resp.content)
    # One top-level entry, named for the campaign — the shape `import_archive` requires, so a
    # download from here is directly importable elsewhere.
    assert {name.split("/")[0] for name in names} == {_CAMPAIGN}
    assert f"{_CAMPAIGN}/_config/campaign.vast" in names


def test_postprocessing_scratch_is_left_out(env):
    """``.cache`` and ``_postproc`` are staging, not campaign content."""
    names = _members(env.get(f"/campaigns/{_CAMPAIGN}/archive").content)
    assert not [n for n in names if "/.cache" in n or "/_postproc" in n], \
        f"staging directories were shipped in the archive: {sorted(names)}"


def test_an_unknown_campaign_is_a_404(env):
    """A missing campaign is absent, not a conflict — the same answer either lane gives."""
    resp = env.get("/campaigns/nope-2026-01-01-000000/archive")
    assert resp.status_code == 404


def test_the_route_needs_no_workspace_store(tmp_path, monkeypatch):
    """Archives are campaign output, so the route must not depend on workspaces.

    Kept because the local implementation resolves a campaign directory rather than a
    workspace: a service with no workspaces configured answers 501 for project routes, and an
    archive download must not be dragged into that.
    """
    monkeypatch.setattr("robovast.client.project_config.ProjectConfig.load",
                        staticmethod(lambda *a, **k: None))
    lt = LocalTransport.__new__(LocalTransport)
    lt._campaigns = {}
    lt._lock = threading.Lock()
    lt.store = None
    lt._campaigns_root = lambda: tmp_path / "results"
    root = tmp_path / "results" / _CAMPAIGN / "_config"
    root.mkdir(parents=True)
    (root / "campaign.vast").write_text("configuration:\n  name: x\n", encoding="utf-8")

    with TestClient(build_app(lt)) as client:
        resp = client.get(f"/campaigns/{_CAMPAIGN}/archive")
    assert resp.status_code == 200, resp.text
    assert _CAMPAIGN in resp.headers["content-disposition"]
