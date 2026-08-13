# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Who says they started a campaign, recorded once and shown as a claim.

With one shared secret nobody can prove who they are, so the whole feature is a label.
Two properties keep it honest:

* the name comes from the **authenticated caller**, not the request body — otherwise a
  client could send one name in the header and a different one here, and the record
  would answer a question nobody asked;
* **no name stays no name.** "Nobody said" and "someone called themselves X" are
  different facts, and a placeholder would erase the difference.
"""

import sqlite3

import pytest

from robovast.common.store import (SCHEMA_VERSION, CampaignStore,
                                   read_campaign_created_by)


@pytest.fixture(name="store_path")
def _store_path(tmp_path):
    return tmp_path / "campaign.db"


def test_a_name_is_recorded_with_the_campaign(store_path):
    store = CampaignStore(store_path)
    store.create_campaign("camp", {}, created_by="Fred")
    assert read_campaign_created_by(store_path.parent) == "Fred"


def test_no_name_is_stored_as_null_not_as_a_placeholder(store_path):
    store = CampaignStore(store_path)
    store.create_campaign("camp", {}, created_by="")
    assert read_campaign_created_by(store_path.parent) is None

    row = sqlite3.connect(store_path).execute(
        "SELECT created_by FROM campaign").fetchone()
    assert row[0] is None


def test_a_store_from_before_the_column_migrates_forward(tmp_path):
    """An older store gains the column on open, and reads as unattributed until then."""
    path = tmp_path / "campaign.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE campaign (id INTEGER PRIMARY KEY, name TEXT, description TEXT);")
    conn.execute("PRAGMA user_version = 4")
    conn.commit()
    conn.close()

    # Read-only, before any migration: the unknown column is "no name", not a crash.
    assert read_campaign_created_by(tmp_path) is None

    store = CampaignStore(path)
    version = store._conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == SCHEMA_VERSION
    columns = [r[1] for r in store._conn.execute("PRAGMA table_info(campaign)")]
    assert "created_by" in columns


def test_the_service_takes_the_name_from_the_caller_not_the_body(monkeypatch):
    """A body-supplied name must not become a second, competing answer."""
    from starlette.testclient import TestClient

    from robovast.service.app import build_app
    from robovast.service.client import LocalTransport
    from robovast.service.interface import CampaignRef, Routes

    seen = {}

    class _Impl(LocalTransport):
        def create_campaign(self, request):
            seen["created_by"] = request.created_by
            return CampaignRef(campaign_id="camp-1")

    with TestClient(build_app(_Impl(), mount_mcp=False, auth_token="tok")) as client:
        client.post(Routes.CAMPAIGNS,
                    json={"workspace_id": "w", "created_by": "Somebody Else"},
                    headers={"Authorization": "Bearer tok",
                             "X-Robovast-User": "Fred"})

    assert seen["created_by"] == "Fred"


def test_an_unnamed_caller_creates_an_unattributed_campaign():
    from starlette.testclient import TestClient

    from robovast.service.app import build_app
    from robovast.service.client import LocalTransport
    from robovast.service.interface import CampaignRef, Routes

    seen = {}

    class _Impl(LocalTransport):
        def create_campaign(self, request):
            seen["created_by"] = request.created_by
            return CampaignRef(campaign_id="camp-1")

    with TestClient(build_app(_Impl(), mount_mcp=False, auth_token="tok")) as client:
        client.post(Routes.CAMPAIGNS, json={"workspace_id": "w"},
                    headers={"Authorization": "Bearer tok"})

    assert seen["created_by"] == ""
