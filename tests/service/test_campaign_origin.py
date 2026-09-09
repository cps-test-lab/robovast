# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Where a campaign came from, recorded as a record and never as a link.

The distinction is the whole feature. RoboVAST deliberately keeps campaigns
workspace-independent — a campaign's fate must not hang on a workspace that may be edited,
renamed or deleted — so what is kept here is a *historical fact*, and these tests pin the
properties that keep it one:

* nothing reads it back to run anything: deleting the workspace it names changes neither
  the record nor the campaign's ability to be re-run;
* a re-run's lineage is **denormalised at launch**, so it survives the parent's deletion
  and a chain of re-runs keeps naming the workspace at its root;
* a campaign that predates the record has **no** origin, rather than one reconstructed
  from its frozen ``_config/`` — which holds a ``.vast`` basename and says nothing about
  which workspace;
* a re-run says which config version it read, because a re-run whose staged config was
  migrated is not repeating the experiment its parent ran.
"""

import sqlite3

import pytest

from robovast.common.store import (SCHEMA_VERSION, STORE_FILENAME, CampaignStore,
                                   read_campaign_origin)
from robovast.service.interface import CampaignOrigin, OriginKind


@pytest.fixture(name="store_path")
def _store_path(tmp_path):
    return tmp_path / STORE_FILENAME


#: What ``retrigger.prepare`` reports for a config that needed nothing done to it, and for
#: one the ladder carried forward. The transport copies the record; it does not compute it.
_NATIVE = {"from": 4, "to": 4, "steps": []}
_MIGRATED = {"from": 1, "to": 4, "steps": ["1_to_2", "2_to_3", "3_to_4"]}


def _origin(**kw):
    base = {"kind": OriginKind.WORKSPACE, "workspace_id": "ws-abc123",
            "workspace_name": "ros2demo", "config_path": "nav/basic_nav.vast"}
    base.update(kw)
    return CampaignOrigin(**base)


def test_a_workspace_launch_records_id_name_and_the_relative_path(store_path):
    CampaignStore(store_path).create_campaign("camp", {}, origin=_origin())

    got = read_campaign_origin(store_path.parent)
    assert got.kind == "workspace"
    assert got.workspace_id == "ws-abc123"
    assert got.workspace_name == "ros2demo"
    # The workspace-RELATIVE path, not the basename: _config/ keeps only the latter, which
    # is exactly why recording this is worth anything.
    assert got.config_path == "nav/basic_nav.vast"
    assert got.from_campaign == ""


def test_a_campaign_launched_without_an_origin_has_none_not_an_empty_one(store_path):
    """"Not recorded" and "recorded as empty" are different facts; only one is true here."""
    CampaignStore(store_path).create_campaign("camp", {})

    assert read_campaign_origin(store_path.parent) is None
    row = sqlite3.connect(store_path).execute(
        "SELECT origin_kind, origin_workspace_id FROM campaign").fetchone()
    assert row == (None, None)


def test_a_rerun_records_the_parent_and_the_parents_workspace(store_path):
    """A re-run names both what it came from and where that configuration originated."""
    rerun = _origin(kind=OriginKind.RETRIGGER, from_campaign="basic-nav-20260814-101233")
    CampaignStore(store_path).create_campaign("camp", {}, origin=rerun)

    got = read_campaign_origin(store_path.parent)
    assert got.kind == "retrigger"
    assert got.from_campaign == "basic-nav-20260814-101233"
    # Denormalised from the parent, so the workspace is still named without holding it.
    assert (got.workspace_name, got.config_path) == ("ros2demo", "nav/basic_nav.vast")


def test_kind_is_the_authority_not_whether_from_campaign_is_set(store_path):
    """A reader must switch on ``kind``; the day a third kind exists, deriving it is wrong."""
    CampaignStore(store_path).create_campaign("camp", {}, origin=_origin(kind="scheduled"))

    got = read_campaign_origin(store_path.parent)
    assert got.kind == "scheduled"          # unknown vocabulary survives the round trip
    assert got.from_campaign == ""


def test_a_rerun_records_the_config_version_it_read_and_the_steps_that_got_it_there(
        store_path):
    """A migrated re-run reads a different config than its parent ran, so it must say so."""
    rerun = _origin(kind=OriginKind.RETRIGGER, from_campaign="basic-nav-20260814-101233",
                    config_version_from=1,
                    config_migration_steps=["1_to_2", "2_to_3", "3_to_4"])
    CampaignStore(store_path).create_campaign("camp", {}, origin=rerun)

    got = read_campaign_origin(store_path.parent)
    assert got.config_version_from == 1
    assert got.config_migration_steps == ["1_to_2", "2_to_3", "3_to_4"]


def test_a_rerun_that_migrated_nothing_says_so_rather_than_saying_nothing(store_path):
    """"Read exactly as written" is an answer; only an unrecorded version is a silence."""
    rerun = _origin(kind=OriginKind.RETRIGGER, from_campaign="basic-nav-20260814-101233",
                    config_version_from=4)
    CampaignStore(store_path).create_campaign("camp", {}, origin=rerun)

    got = read_campaign_origin(store_path.parent)
    assert got.config_version_from == 4
    assert got.config_migration_steps == []
    # And a campaign nobody recorded a version for is the other answer, not the same one.
    assert _origin().config_version_from is None


def test_a_store_from_before_the_config_version_columns_keeps_the_origin_it_has(tmp_path):
    """A column added later must not cost an older campaign the origin it did record.

    Listing reads these stores without migrating them, so every origin column a store has
    predates one it does not -- and selecting all of them would answer "no origin" for a
    campaign that knows perfectly well which workspace it came from.
    """
    from robovast.common.store import _MIGRATIONS
    path = tmp_path / STORE_FILENAME
    conn = sqlite3.connect(path)
    for migration in _MIGRATIONS[:12]:
        conn.executescript(migration)
    conn.execute("INSERT INTO campaign (name, origin_kind, origin_workspace_name) "
                 "VALUES ('camp', 'retrigger', 'ros2demo')")
    conn.execute("PRAGMA user_version = 12")
    conn.commit()
    conn.close()

    got = read_campaign_origin(tmp_path)
    assert got.workspace_name == "ros2demo"
    # The one thing it genuinely cannot know, and it says so rather than reporting "native".
    assert got.config_version_from is None


def test_a_store_from_before_the_columns_migrates_forward(tmp_path):
    """An older store gains the columns on open, and reads as no-origin until then."""
    path = tmp_path / STORE_FILENAME
    # A REAL store at v6: the ladder replayed to that point, not a hand-picked table.
    # A store that genuinely reached v6 has every table the earlier steps created, and a
    # thinner stub would let a later migration that touches one of them pass here and fail
    # on a real database.
    from robovast.common.store import _MIGRATIONS
    conn = sqlite3.connect(path)
    for migration in _MIGRATIONS[:6]:
        conn.executescript(migration)
    conn.execute("PRAGMA user_version = 6")
    conn.commit()
    conn.close()

    # Read-only, before any migration: the unknown columns are "no origin", not a crash.
    # That path is the common one -- listing never migrates a store it only reads.
    assert read_campaign_origin(tmp_path) is None

    store = CampaignStore(path)
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    columns = [r[1] for r in store._conn.execute("PRAGMA table_info(campaign)")]
    assert "origin_kind" in columns and "origin_from_campaign" in columns
    # Still no origin: migrating adds the columns, it does not invent values for a
    # campaign that ran before anyone recorded where it came from.
    assert read_campaign_origin(tmp_path) is None


# -- the launch path -------------------------------------------------------------------


def _transport(root):
    """A store-only ``LocalTransport``, as in ``test_project_push``."""
    import threading

    from robovast.service.client import LocalTransport
    from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore

    lt = LocalTransport.__new__(LocalTransport)
    lt.store = WorkspaceStore(registry=WorkspaceRegistry(root=root))
    lt._campaigns = {}
    lt._lock = threading.Lock()
    lt._origin_cache = {}
    return lt


@pytest.fixture(name="client")
def _client(tmp_path):
    return _transport(tmp_path / "workspaces")


def _workspace_with(client, name, rel_vast):
    from robovast.service.interface import CreateWorkspaceRequest
    ws = client.create_workspace(CreateWorkspaceRequest(name=name))
    path = client.store.registry.project_dir(ws.workspace_id) / rel_vast
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("configuration:\n  variations: []\n")
    return ws


def test_resolving_a_project_records_where_it_came_from(client):
    ws = _workspace_with(client, "ros2demo", "nav/basic_nav.vast")

    origin = client._resolve_project(ws.workspace_id, "nav/basic_nav.vast").origin
    assert origin.kind == "workspace"
    assert origin.workspace_id == ws.workspace_id
    assert origin.workspace_name == "ros2demo"
    assert origin.config_path == "nav/basic_nav.vast"


def test_the_name_comes_from_the_registry_not_from_what_the_caller_typed(client):
    """A workspace is addressable by name, so the record must not echo the caller's alias."""
    ws = _workspace_with(client, "ros2demo", "demo.vast")

    origin = client._resolve_project("ros2demo", "").origin
    assert origin.workspace_id == ws.workspace_id
    assert origin.workspace_name == "ros2demo"
    # Resolved even though the caller named no .vast at all (the sole-file case).
    assert origin.config_path == "demo.vast"


def test_a_rerun_copies_the_parents_workspace_forward(client, tmp_path):
    """The lineage is denormalised at launch, so a re-run names the root workspace."""
    parent_dir = tmp_path / "results" / "basic-nav-20260814-101233"
    parent_dir.mkdir(parents=True)
    CampaignStore(parent_dir / STORE_FILENAME).create_campaign("p", {}, origin=_origin())
    client._record_dir = lambda cid: parent_dir

    got = client._retrigger_origin("basic-nav-20260814-101233", _NATIVE)
    assert got.kind == "retrigger"
    assert got.from_campaign == "basic-nav-20260814-101233"
    assert (got.workspace_name, got.config_path) == ("ros2demo", "nav/basic_nav.vast")


def test_a_rerun_of_a_rerun_still_names_the_root_workspace(client, tmp_path):
    """Chains cost no walking: the parent's row already carries the root."""
    parent_dir = tmp_path / "results" / "rerun-1"
    parent_dir.mkdir(parents=True)
    first_rerun = _origin(kind=OriginKind.RETRIGGER, from_campaign="original")
    CampaignStore(parent_dir / STORE_FILENAME).create_campaign("p", {}, origin=first_rerun)
    client._record_dir = lambda cid: parent_dir

    got = client._retrigger_origin("rerun-1", _NATIVE)
    assert got.from_campaign == "rerun-1"          # the IMMEDIATE parent
    assert got.workspace_name == "ros2demo"        # ...and still the root workspace


def test_a_rerun_of_a_campaign_with_no_origin_still_records_its_lineage(client, tmp_path):
    """An old parent leaves the workspace unknown -- but not where this one came from."""
    parent_dir = tmp_path / "results" / "old-campaign"
    parent_dir.mkdir(parents=True)
    CampaignStore(parent_dir / STORE_FILENAME).create_campaign("p", {})
    client._record_dir = lambda cid: parent_dir

    got = client._retrigger_origin("old-campaign", _NATIVE)
    assert got.from_campaign == "old-campaign"
    assert got.workspace_id == "" and got.workspace_name == ""


def test_a_rerun_of_an_old_campaign_records_where_its_config_was_migrated_from(
        client, tmp_path):
    """The re-run is the only run that can say it read a version its parent never ran."""
    parent_dir = tmp_path / "results" / "v1-campaign"
    parent_dir.mkdir(parents=True)
    CampaignStore(parent_dir / STORE_FILENAME).create_campaign("p", {}, origin=_origin())
    client._record_dir = lambda cid: parent_dir

    got = client._retrigger_origin("v1-campaign", _MIGRATED)
    assert got.config_version_from == 1
    assert got.config_migration_steps == ["1_to_2", "2_to_3", "3_to_4"]


def test_a_rerun_of_a_current_campaign_claims_no_migration(client, tmp_path):
    """It read the config as written, which must not read as "migrated from somewhere"."""
    parent_dir = tmp_path / "results" / "current-campaign"
    parent_dir.mkdir(parents=True)
    CampaignStore(parent_dir / STORE_FILENAME).create_campaign("p", {}, origin=_origin())
    client._record_dir = lambda cid: parent_dir

    got = client._retrigger_origin("current-campaign", _NATIVE)
    assert got.config_version_from == 4
    assert got.config_migration_steps == []


def test_deleting_the_workspace_leaves_the_record_intact(client, tmp_path):
    """The record is not a link: it outlives what it names, and nothing reads it back."""
    ws = _workspace_with(client, "ros2demo", "demo.vast")
    origin = client._resolve_project(ws.workspace_id, "").origin

    campaign_dir = tmp_path / "results" / "camp"
    campaign_dir.mkdir(parents=True)
    CampaignStore(campaign_dir / STORE_FILENAME).create_campaign("camp", {}, origin=origin)

    client.delete_workspace(ws.workspace_id)

    got = read_campaign_origin(campaign_dir)
    assert got.workspace_name == "ros2demo"
    assert got.workspace_id == ws.workspace_id
