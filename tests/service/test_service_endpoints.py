# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Package-provided service data endpoints (``robovast.service_endpoints``).

Covers the generic mechanism (loader, reserved-name/duplicate skipping, the RunDataContext
facade) and the reference endpoint: ``robovast_nav``'s ``costmap`` served at
``GET /campaigns/{id}/costmap``.

A handler reads its campaign's tables through ``RunDataContext.open_db()``. The frames here
come either from the decoder's fixture recording or, where a test needs stamps of its own,
from a run's own ``costmaps.csv`` -- the same table, with the same columns, for a run that
has no recording to build it from.
"""

import csv
import threading

import pytest
from fastapi.testclient import TestClient

from robovast.results_processing.data_query import DataQueryError
from robovast.service.app import build_app
from robovast.service.endpoint_plugin import (RESERVED_CAMPAIGN_ENDPOINTS, RunDataContext,
                                              load_service_endpoints)
from tests.service.null_service import NullService
from tests.robovast_data.conftest import nav_campaign, write_store

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
        _ = RunDataContext("c", {"config_name": "nav"}, "/tmp").run_id      # missing
    with pytest.raises(ValueError):
        _ = RunDataContext("c", {"config_name": "nav", "run_id": "x"}, "/tmp").run_id  # non-int


def test_context_run_dir_escape_rejected(tmp_path):
    ctx = RunDataContext("c", {}, str(tmp_path))
    assert ctx.run_dir("nav", 3) == (tmp_path / "nav" / "3").resolve()
    with pytest.raises(ValueError):
        ctx.run_dir("..", "..")




# -- the tables behind the facade ------------------------------------------

THIS = "camp-2026-01-01-00000001"
OTHER = "camp-2026-01-01-00000002"

_COSTMAP_COLUMNS = ("topic", "timestamp", "frame_id", "resolution", "width", "height",
                    "origin_x", "origin_y", "origin_yaw", "data")


def _campaign(results, campaign_id, stamps, data="ZLIB_B64", others=None):
    """A campaign whose run ``nav/3`` recorded one ``/map`` frame per stamp.

    With no stamps the run recorded no costmaps at all. *others* maps further run ids of
    ``nav`` to their own stamps.
    """
    root = results / campaign_id
    runs = {3: stamps, **(others or {})}
    for run_id, run_stamps in runs.items():
        run = root / "nav" / str(run_id)
        run.mkdir(parents=True)
        if not run_stamps:
            continue
        with open(run / "costmaps.csv", "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(_COSTMAP_COLUMNS)
            for stamp in run_stamps:
                writer.writerow(("/map", stamp, "map", 0.05, 10, 10, 0.0, 0.0, 0.0, data))
    write_store(root, {"nav": {"runs": {run_id: "passed" for run_id in runs}}})
    return root


def test_context_open_db_hands_a_handler_a_read_only_connection(tmp_path):
    """A handler is served the same tables every notebook and panel reads, so a write from
    one would land in everyone's data. The connection answers a single SELECT and nothing
    else."""
    _campaign(tmp_path, THIS, stamps=(1.0,))
    ctx = RunDataContext(THIS, {}, str(tmp_path / THIS))
    with ctx.open_db() as db:
        row = db.execute("SELECT topic FROM costmaps WHERE campaign_id = ?",
                         (THIS,)).fetchone()
        assert row["topic"] == "/map" and row[0] == "/map", "rows read by name and position"
        with pytest.raises(DataQueryError):
            db.execute("DELETE FROM costmaps")


# -- e2e over the FastAPI app ----------------------------------------------

def _null_service(results_root) -> NullService:
    lt = object.__new__(NullService)
    lt._campaigns = {}
    lt._lock = threading.Lock()
    lt.store = None
    lt._campaigns_root = lambda: results_root
    return lt


def _client(tmp_path):
    (tmp_path / THIS).mkdir(parents=True, exist_ok=True)
    return TestClient(build_app(_null_service(tmp_path)))


def _get(client, t, topic="/map", campaign=THIS, run_id=3):
    return client.get(f"/campaigns/{campaign}/costmap",
                      params={"config_name": "nav", "run_id": run_id, "topic": topic, "t": t})


def _get_frame(client, t, topic="/map", run_id=3):
    resp = _get(client, t, topic, run_id=run_id)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_costmap_endpoint_serves_a_recorded_frame(tmp_path):
    """A frame the decoder built from a recording, end to end."""
    nav_campaign(tmp_path / THIS, runs=(("nav", 3),))
    frame = _get_frame(_client(tmp_path), 3.0, topic="/global_costmap/costmap")
    assert frame["t"] == 3.0 and frame["t_prev"] is None and frame["t_next"] is None
    assert frame["width"] > 0 and frame["height"] > 0 and frame["data"]


def test_costmap_endpoint_serves_frame(tmp_path):
    _campaign(tmp_path, THIS, stamps=(1.0,))
    frame = _get_frame(_client(tmp_path), 1.0)
    assert frame["frame_id"] == "map"
    assert frame["width"] == 10 and frame["data"] == "ZLIB_B64"


def test_costmap_endpoint_serves_this_campaigns_frame_and_not_another_campaigns(tmp_path):
    """Both campaigns recorded /map for config 'nav' run 3, because campaigns do; the frame
    served is the one of the campaign the request names."""
    _campaign(tmp_path, THIS, stamps=(1.0,), data="MINE")
    _campaign(tmp_path, OTHER, stamps=(1.0,), data="THEIRS")
    assert _get_frame(_client(tmp_path), 1.0)["data"] == "MINE"


def test_costmap_endpoint_no_frame_for_this_topic_is_null(tmp_path):
    """The run recorded costmaps, just not this topic: a null frame, not an error."""
    _campaign(tmp_path, THIS, stamps=(1.0,))
    resp = _get(_client(tmp_path), 1.0, topic="/nope")
    assert resp.status_code == 200
    assert resp.json() is None


def test_costmap_endpoint_says_so_when_this_campaign_recorded_none(tmp_path):
    """A robot that never navigates must not get a bare null, which the panel would draw as
    "no frame at this time", implying a costmap exists elsewhere in the run -- but the
    message naming the configuration or the absent stack. Another campaign recording
    costmaps changes nothing."""
    _campaign(tmp_path, OTHER, stamps=(1.0,))
    _campaign(tmp_path, THIS, stamps=())
    resp = _get(_client(tmp_path), 1.0)
    assert resp.status_code == 400
    assert "costmap" in resp.json()["detail"]


def test_costmap_endpoint_latched_topic_has_no_neighbours(tmp_path):
    """A single recorded frame reports no neighbours either side.

    This is what keeps the static map on screen: with no neighbours there is no publish
    period, so the panel has nothing to call it stale against, and its validity interval is
    unbounded -- it is fetched once for the session.
    """
    _campaign(tmp_path, THIS, stamps=(1.0,))
    frame = _get_frame(_client(tmp_path), 500.0)  # far past the only row: still clamped
    assert frame["t"] == 1.0
    assert frame["t_prev"] is None
    assert frame["t_next"] is None


def test_costmap_endpoint_reports_neighbours_around_the_frame(tmp_path):
    """Numerically, not lexicographically: as text '10.2' < '9.5' < '100.1', which would
    report the frame nearest t=10.3 with no predecessor and a successor 90 s away."""
    _campaign(tmp_path, THIS, stamps=(9.5, 10.2, 11.0, 100.1))
    frame = _get_frame(_client(tmp_path), 10.3)
    assert frame["t"] == 10.2
    assert frame["t_prev"] == 9.5
    assert frame["t_next"] == 11.0


def test_costmap_endpoint_neighbours_ignore_another_runs_stamps(tmp_path):
    """The neighbour lookup is a second query, and scoping it to the run is not optional.

    A frame served from this run with a t_prev/t_next taken from another one gives the panel
    a publish period it invents a staleness threshold from, on a run that never published at
    that rate.
    """
    _campaign(tmp_path, THIS, stamps=(9.5, 10.2, 11.0), others={4: (10.19, 10.21)})
    frame = _get_frame(_client(tmp_path), 10.3)
    assert frame["t"] == 10.2
    assert frame["t_prev"] == 9.5
    assert frame["t_next"] == 11.0


def test_costmap_endpoint_past_the_last_frame_has_no_next(tmp_path):
    """Off the end of the span the nearest row is the last one, and it says so.

    The endpoint still clamps -- that is what ``t_next: None`` is for. On a finished
    recording the panel reads it as "no later frame exists", and the distance from the
    cursor is what then decides whether the frame is still an honest answer.
    """
    _campaign(tmp_path, THIS, stamps=(9.5, 10.2, 11.0))
    frame = _get_frame(_client(tmp_path), 900.0)
    assert frame["t"] == 11.0
    assert frame["t_prev"] == 10.2
    assert frame["t_next"] is None
