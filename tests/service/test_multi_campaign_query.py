# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Phase 1.6: LocalTransport.query_campaign_data_sql attaches extra campaigns.

Locks the service-side plumbing: ``extra_campaign_ids`` must resolve to schema
aliases ``c1``, ``c2``, … so an A/B query spans several campaigns through the
interface (not just the direct MCP local path)."""

import sqlite3

import pytest

from robovast.service.client import LocalTransport
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore


@pytest.fixture
def transport(monkeypatch, tmp_path):
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    return LocalTransport(store=store)


def _make_campaign(root, name, objectives):
    cdir = root / name
    (cdir / "_execution").mkdir(parents=True)
    db = sqlite3.connect(cdir / "_execution" / "data.db")
    db.execute("CREATE TABLE runs (run_id INTEGER, objective REAL)")
    db.executemany("INSERT INTO runs VALUES (?,?)", list(enumerate(objectives)))
    db.commit()
    db.close()
    return cdir


def test_extra_campaign_attached_as_c1(transport):
    root = transport._campaigns_root()
    root.mkdir(parents=True, exist_ok=True)
    _make_campaign(root, "camp-A", [0.1, 0.2])
    _make_campaign(root, "camp-B", [0.3, 0.4, 0.5])

    res = transport.query_campaign_data_sql(
        "camp-A",
        "SELECT (SELECT COUNT(*) FROM runs) AS a, (SELECT COUNT(*) FROM c1.runs) AS b",
        extra_campaign_ids=["camp-B"])
    assert res.rows[0]["a"] == 2
    assert res.rows[0]["b"] == 3


def test_no_extra_is_backward_compatible(transport):
    root = transport._campaigns_root()
    root.mkdir(parents=True, exist_ok=True)
    _make_campaign(root, "camp-A", [0.1, 0.2])
    res = transport.query_campaign_data_sql("camp-A", "SELECT COUNT(*) n FROM runs")
    assert res.rows[0]["n"] == 2
