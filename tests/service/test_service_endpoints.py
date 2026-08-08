# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Package-provided service data endpoints (``robovast.service_endpoints``).

Covers the generic mechanism (loader, reserved-name/duplicate skipping, the RunDataContext
facade) and the relocated reference endpoint: ``robovast_nav``'s ``costmap`` served at
``GET /campaigns/{id}/costmap`` — with the frame reader now living in the nav package, not core.
"""

import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from robovast.service.app import build_app
from robovast.service.client import LocalTransport
from robovast.service.endpoint_plugin import (RESERVED_CAMPAIGN_ENDPOINTS,
                                              RunDataContext,
                                              load_service_endpoints)


# -- loader ----------------------------------------------------------------

def test_loader_includes_relocated_costmap():
    eps = load_service_endpoints()
    assert "costmap" in eps
    assert type(eps["costmap"]).__name__ == "CostmapEndpoint"


def test_loader_skips_reserved_and_duplicate(monkeypatch):
    class _EP:
        def __init__(self, name, obj):
            self.name, self.value, self._obj = name, f"mod:{name}", obj
        def load(self):
            return self._obj

    class _Good:
        name = "pkg/foo"
        def handle(self, ctx):
            return {}

    class _Reserved:
        name = "panels"          # shadows a core route → skipped
        def handle(self, ctx):
            return {}

    class _Dup:
        name = "pkg/foo"         # duplicate of _Good → skipped
        def handle(self, ctx):
            return {}

    monkeypatch.setattr(
        "robovast.service.endpoint_plugin.entry_points",
        lambda group: [_EP("good", _Good), _EP("reserved", _Reserved), _EP("dup", _Dup)])
    eps = load_service_endpoints()
    assert set(eps) == {"pkg/foo"}
    assert "panels" in RESERVED_CAMPAIGN_ENDPOINTS


# -- RunDataContext facade -------------------------------------------------

def test_context_param_coercion():
    ctx = RunDataContext("c", {"config_name": "nav", "run_id": "3"}, "/tmp")
    assert (ctx.config_name, ctx.run_id) == ("nav", 3)
    with pytest.raises(ValueError):
        RunDataContext("c", {"config_name": "nav"}, "/tmp").run_id          # missing
    with pytest.raises(ValueError):
        RunDataContext("c", {"config_name": "nav", "run_id": "x"}, "/tmp").run_id  # non-int


def test_context_run_dir_escape_rejected(tmp_path):
    ctx = RunDataContext("c", {}, str(tmp_path))
    assert ctx.run_dir("nav", 3) == (tmp_path / "nav" / "3").resolve()
    with pytest.raises(ValueError):
        ctx.run_dir("..", "..")


def test_context_open_db_reads_readonly(tmp_path):
    exe = tmp_path / "_execution"
    exe.mkdir()
    conn = sqlite3.connect(exe / "data.db")
    conn.execute("CREATE TABLE t (a)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()
    ctx = RunDataContext("c", {}, str(tmp_path))
    with ctx.open_db() as db:
        assert db.execute("SELECT a FROM t").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):  # read-only
            db.execute("INSERT INTO t VALUES (2)")


# -- e2e over the FastAPI app ----------------------------------------------

def _local_transport(results_root) -> LocalTransport:
    lt = LocalTransport.__new__(LocalTransport)
    lt._campaigns = {}
    lt._lock = threading.Lock()
    lt.store = None
    lt._campaigns_root = lambda: results_root
    return lt


def _make_campaign(tmp_path, *, with_costmaps=True, stamps=(1.0,), timestamp_type="REAL"):
    """Build a campaign whose data.db holds one ``/map`` costmap row per entry in `stamps`.

    ``timestamp_type`` is a parameter because the column really is either type in the field: REAL in a
    data.db built by typed ingest, TEXT in an older one (see data_query's note). The neighbour lookup
    has to agree with both, and TEXT is where it can silently disagree.
    """
    exe = tmp_path / "camp-1" / "_execution"
    exe.mkdir(parents=True)
    conn = sqlite3.connect(exe / "data.db")
    if with_costmaps:
        conn.execute(
            "CREATE TABLE costmaps (config_name TEXT, run_id INTEGER, topic TEXT, "
            f"timestamp {timestamp_type}, frame_id TEXT, resolution REAL, width INTEGER, "
            "height INTEGER, origin_x REAL, origin_y REAL, origin_yaw REAL, data TEXT)")
        for stamp in stamps:
            conn.execute(
                "INSERT INTO costmaps VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("nav", 3, "/map", str(stamp) if timestamp_type == "TEXT" else stamp,
                 "map", 0.05, 10, 10, 0.0, 0.0, 0.0, "ZLIB_B64"))
    else:
        conn.execute("CREATE TABLE other (x)")  # a db exists, but no costmaps table
    conn.commit()
    conn.close()
    return TestClient(build_app(_local_transport(tmp_path)))


def _get_frame(client, t, topic="/map"):
    resp = client.get("/campaigns/camp-1/costmap",
                      params={"config_name": "nav", "run_id": 3, "topic": topic, "t": t})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_costmap_endpoint_serves_frame(tmp_path):
    with _make_campaign(tmp_path) as client:
        resp = client.get("/campaigns/camp-1/costmap",
                          params={"config_name": "nav", "run_id": 3, "topic": "/map", "t": 1.0})
        assert resp.status_code == 200
        frame = resp.json()
        assert frame["frame_id"] == "map"
        assert frame["width"] == 10 and frame["data"] == "ZLIB_B64"


def test_costmap_endpoint_no_frame_is_null(tmp_path):
    with _make_campaign(tmp_path) as client:
        resp = client.get("/campaigns/camp-1/costmap",
                          params={"config_name": "nav", "run_id": 3, "topic": "/nope", "t": 1.0})
        assert resp.status_code == 200
        assert resp.json() is None


def test_costmap_endpoint_missing_table_is_400(tmp_path):
    with _make_campaign(tmp_path, with_costmaps=False) as client:
        resp = client.get("/campaigns/camp-1/costmap",
                          params={"config_name": "nav", "run_id": 3, "topic": "/map", "t": 1.0})
        assert resp.status_code == 400
        assert "costmaps" in resp.json()["detail"]


def test_costmap_endpoint_latched_topic_has_no_neighbours(tmp_path):
    """A single recorded frame reports no neighbours either side.

    This is what keeps the static map on screen: with no neighbours there is no publish period, so the
    panel has nothing to call it stale against, and its validity interval is unbounded — it is fetched
    once for the session. Note the rule never mentions the layer's name.
    """
    with _make_campaign(tmp_path, stamps=(1.0,)) as client:
        frame = _get_frame(client, 500.0)  # far past the only row: still clamped to it
        assert frame["t"] == 1.0
        assert frame["t_prev"] is None
        assert frame["t_next"] is None


def test_costmap_endpoint_reports_neighbours_around_the_frame(tmp_path):
    with _make_campaign(tmp_path, stamps=(9.5, 10.2, 11.0, 100.1)) as client:
        frame = _get_frame(client, 10.3)
        assert frame["t"] == 10.2
        assert frame["t_prev"] == 9.5
        assert frame["t_next"] == 11.0


def test_costmap_endpoint_past_the_last_frame_has_no_next(tmp_path):
    """Off the end of the span the nearest row is the last one, and it says so.

    The endpoint still clamps — that is what `t_next: None` is for. On a finished recording the panel
    reads it as "no later frame exists", and the distance from the cursor is what then decides whether
    the frame is still an honest answer.
    """
    with _make_campaign(tmp_path, stamps=(9.5, 10.2, 11.0)) as client:
        frame = _get_frame(client, 900.0)
        assert frame["t"] == 11.0
        assert frame["t_prev"] == 10.2
        assert frame["t_next"] is None


def test_costmap_endpoint_neighbours_are_numeric_on_a_text_timestamp_column(tmp_path):
    """The neighbour lookup must not compare timestamps as strings.

    Lexicographically '10.2' < '9.5' < '100.1', so without the CAST the frame nearest t=10.3 would be
    reported with t_prev=None (nothing sorts below '10.2' as text) and t_next='100.1' — a wrong answer
    that looks entirely plausible, on precisely the older databases this still has to serve.
    """
    with _make_campaign(tmp_path, stamps=(9.5, 10.2, 100.1), timestamp_type="TEXT") as client:
        frame = _get_frame(client, 10.3)
        assert frame["t"] == 10.2
        assert frame["t_prev"] == 9.5
        assert frame["t_next"] == 100.1
