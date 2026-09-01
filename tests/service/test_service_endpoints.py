# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Package-provided service data endpoints (``robovast.service_endpoints``).

Covers the generic mechanism (loader, reserved-name/duplicate skipping, the RunDataContext
facade) and the relocated reference endpoint: ``robovast_nav``'s ``costmap`` served at
``GET /campaigns/{id}/costmap`` — with the frame reader now living in the nav package, not core.

The facade now hands a handler a **Postgres** connection to the central index, so anything
that reads rows needs one: those tests skip unless ``ROBOVAST_TEST_PG_DSN`` is set, the same
gate as ``tests/results_processing/test_index_matches_data_db.py``. The loader and the
parameter facade need no database and stay ungated.

Two campaigns are ingested rather than one, and both record the same ``/map`` topic for the
same config and run. That is not padding: every campaign shares one ``costmaps`` table now,
so a handler that forgets ``campaign_id`` returns the other campaign's frame — a picture,
correctly drawn, of a different experiment.
"""

import contextlib
import os
import threading

import pytest
from fastapi.testclient import TestClient

from robovast.service.app import build_app
from robovast.service.client import LocalTransport
from robovast.service.endpoint_plugin import (RESERVED_CAMPAIGN_ENDPOINTS, RunDataContext,
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
        _ = RunDataContext("c", {"config_name": "nav"}, "/tmp").run_id      # missing
    with pytest.raises(ValueError):
        _ = RunDataContext("c", {"config_name": "nav", "run_id": "x"}, "/tmp").run_id  # non-int


def test_context_run_dir_escape_rejected(tmp_path):
    ctx = RunDataContext("c", {}, str(tmp_path))
    assert ctx.run_dir("nav", 3) == (tmp_path / "nav" / "3").resolve()
    with pytest.raises(ValueError):
        ctx.run_dir("..", "..")




# -- the index behind the facade -------------------------------------------

DSN = os.environ.get("ROBOVAST_TEST_PG_DSN")
pg = pytest.mark.skipif(not DSN, reason="ROBOVAST_TEST_PG_DSN is not set")

THIS = "camp-1"
OTHER = "camp-2"

#: One ``/map`` frame per stamp, for whichever campaign is being written.
_COSTMAP_COLUMNS = ("topic", "timestamp", "frame_id", "resolution", "width", "height",
                    "origin_x", "origin_y", "origin_yaw", "data")


@contextlib.contextmanager
def _index(schema, timestamp_type="REAL"):
    """An empty index in *schema*, yielding ``write(campaign_id, stamps, data)``.

    Rows go in through the ingest's own writer rather than raw SQL, so the tests read what
    a postprocessed campaign would actually leave behind -- including the column type,
    which is the parameter that matters: ``timestamp`` is REAL in a typed ingest and TEXT
    in one whose values could not all be read as numbers, and the endpoint has to answer
    the same for both.
    """
    psycopg = pytest.importorskip("psycopg")
    from robovast.results_processing import index_query, index_schema
    from robovast.results_processing.csv_types import INTEGER, REAL, TEXT
    from robovast.results_processing.row_sink import PostgresRowSink

    types = {"topic": TEXT, "timestamp": REAL if timestamp_type == "REAL" else TEXT,
             "frame_id": TEXT, "resolution": REAL, "width": INTEGER, "height": INTEGER,
             "origin_x": REAL, "origin_y": REAL, "origin_yaw": REAL, "data": TEXT}

    previous = os.environ.get("ROBOVAST_INDEX_DSN")
    os.environ["ROBOVAST_INDEX_DSN"] = f"{DSN} options=-csearch_path={schema}"
    with psycopg.connect(DSN, autocommit=True) as setup:
        setup.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        setup.execute(f"CREATE SCHEMA {schema}")
    try:
        with index_query.open_index(readonly=False) as conn:

            def write(campaign_id, stamps, data="ZLIB_B64"):
                sink = PostgresRowSink(conn, campaign_id=campaign_id)
                rows = [dict(zip(_COSTMAP_COLUMNS,
                                 ("/map", str(s) if timestamp_type == "TEXT" else s,
                                  "map", 0.05, 10, 10, 0.0, 0.0, 0.0, data)))
                        for s in stamps]
                if rows:
                    sink.write("costmaps", rows,
                               context={"config_name": "nav", "run_id": 3}, types=types,
                               source=campaign_id)
                index_schema.record_campaign(conn, campaign_id)

            yield write
    finally:
        with psycopg.connect(DSN, autocommit=True) as teardown:
            teardown.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        if previous is None:
            os.environ.pop("ROBOVAST_INDEX_DSN", None)
        else:
            os.environ["ROBOVAST_INDEX_DSN"] = previous


@pg
def test_context_open_db_hands_a_handler_a_read_only_index_connection(tmp_path):
    """Read-only is the session's property, not the file's, now that there is no file.

    A handler is served the same index every notebook and panel reads, so a write from one
    would land in everyone's data. The connection refuses it at the server, whatever the
    statement is spelled like.
    """
    psycopg = pytest.importorskip("psycopg")
    with _index("endpoint_open_db_test") as write:
        write(THIS, stamps=(1.0,))
        ctx = RunDataContext(THIS, {}, str(tmp_path))
        with ctx.open_db() as db:
            row = db.execute("SELECT topic FROM costmaps WHERE campaign_id = %s",
                             (THIS,)).fetchone()
            assert row["topic"] == "/map", "rows are dicts, keyed by column name"
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                db.execute("DELETE FROM costmaps")


# -- e2e over the FastAPI app ----------------------------------------------

def _local_transport(results_root) -> LocalTransport:
    lt = LocalTransport.__new__(LocalTransport)
    lt._campaigns = {}
    lt._lock = threading.Lock()
    lt.store = None
    lt._campaigns_root = lambda: results_root
    return lt


def _client(tmp_path):
    (tmp_path / THIS).mkdir(parents=True, exist_ok=True)
    return TestClient(build_app(_local_transport(tmp_path)))


def _get(client, t, topic="/map", campaign=THIS):
    return client.get(f"/campaigns/{campaign}/costmap",
                      params={"config_name": "nav", "run_id": 3, "topic": topic, "t": t})


def _get_frame(client, t, topic="/map"):
    resp = _get(client, t, topic)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pg
def test_costmap_endpoint_serves_frame(tmp_path):
    with _index("endpoint_costmap_test") as write:
        write(THIS, stamps=(1.0,))
        frame = _get_frame(_client(tmp_path), 1.0)
        assert frame["frame_id"] == "map"
        assert frame["width"] == 10 and frame["data"] == "ZLIB_B64"


@pg
def test_costmap_endpoint_serves_this_campaigns_frame_and_not_another_campaigns(tmp_path):
    """Both campaigns recorded /map for config 'nav' run 3, because campaigns do.

    Without the campaign_id predicate this query matches twice and returns whichever row
    the plan reaches first -- a frame that decodes, draws, and belongs to another
    experiment. Nothing about the response says so.
    """
    with _index("endpoint_scope_test") as write:
        write(THIS, stamps=(1.0,), data="MINE")
        write(OTHER, stamps=(1.0,), data="THEIRS")
        assert _get_frame(_client(tmp_path), 1.0)["data"] == "MINE"


@pg
def test_costmap_endpoint_no_frame_for_this_topic_is_null(tmp_path):
    """The run recorded costmaps, just not this topic: a null frame, not an error."""
    with _index("endpoint_null_test") as write:
        write(THIS, stamps=(1.0,))
        resp = _get(_client(tmp_path), 1.0, topic="/nope")
        assert resp.status_code == 200
        assert resp.json() is None


@pg
def test_costmap_endpoint_says_so_when_this_campaign_recorded_none(tmp_path):
    """A shared table makes "no costmaps here" a question about the campaign, not the schema.

    Another campaign's nav2 rows are enough for the table to exist, so existence no longer
    answers it. Left there, a robot that never navigates would get a bare null and the
    panel would draw "no frame at this time", implying a costmap exists elsewhere in the
    run -- instead of the message naming the postprocessing step or the absent stack.
    """
    with _index("endpoint_absent_test") as write:
        write(OTHER, stamps=(1.0,))
        write(THIS, stamps=())
        resp = _get(_client(tmp_path), 1.0)
        assert resp.status_code == 400
        assert "costmap" in resp.json()["detail"]


@pg
def test_costmap_endpoint_latched_topic_has_no_neighbours(tmp_path):
    """A single recorded frame reports no neighbours either side.

    This is what keeps the static map on screen: with no neighbours there is no publish
    period, so the panel has nothing to call it stale against, and its validity interval is
    unbounded -- it is fetched once for the session.
    """
    with _index("endpoint_latched_test") as write:
        write(THIS, stamps=(1.0,))
        frame = _get_frame(_client(tmp_path), 500.0)  # far past the only row: still clamped
        assert frame["t"] == 1.0
        assert frame["t_prev"] is None
        assert frame["t_next"] is None


@pg
def test_costmap_endpoint_reports_neighbours_around_the_frame(tmp_path):
    with _index("endpoint_neighbours_test") as write:
        write(THIS, stamps=(9.5, 10.2, 11.0, 100.1))
        frame = _get_frame(_client(tmp_path), 10.3)
        assert frame["t"] == 10.2
        assert frame["t_prev"] == 9.5
        assert frame["t_next"] == 11.0


@pg
def test_costmap_endpoint_neighbours_ignore_another_campaigns_stamps(tmp_path):
    """The neighbour lookup is a second query, and scoping it is not optional either.

    A frame served from this campaign with a t_prev/t_next taken from another one gives
    the panel a publish period it invents a staleness threshold from, on a run that never
    published at that rate.
    """
    with _index("endpoint_neighbour_scope_test") as write:
        write(THIS, stamps=(9.5, 10.2, 11.0))
        write(OTHER, stamps=(10.19, 10.21))
        frame = _get_frame(_client(tmp_path), 10.3)
        assert frame["t"] == 10.2
        assert frame["t_prev"] == 9.5
        assert frame["t_next"] == 11.0


@pg
def test_costmap_endpoint_past_the_last_frame_has_no_next(tmp_path):
    """Off the end of the span the nearest row is the last one, and it says so.

    The endpoint still clamps -- that is what ``t_next: None`` is for. On a finished
    recording the panel reads it as "no later frame exists", and the distance from the
    cursor is what then decides whether the frame is still an honest answer.
    """
    with _index("endpoint_past_end_test") as write:
        write(THIS, stamps=(9.5, 10.2, 11.0))
        frame = _get_frame(_client(tmp_path), 900.0)
        assert frame["t"] == 11.0
        assert frame["t_prev"] == 10.2
        assert frame["t_next"] is None


@pg
def test_costmap_endpoint_neighbours_are_numeric_on_a_text_timestamp_column(tmp_path):
    """The neighbour lookup must not compare timestamps as strings.

    Lexicographically '10.2' < '9.5' < '100.1', so without the cast the frame nearest
    t=10.3 would be reported with t_prev=None (nothing sorts below '10.2' as text) and
    t_next='100.1' -- a wrong answer that looks entirely plausible. And the cast must be
    ``double precision``: Postgres' ``REAL`` is four bytes, which rounds an epoch stamp to
    the nearest ~30 s and would pick a neighbour tens of seconds away, just as plausibly.
    """
    with _index("endpoint_text_stamp_test", timestamp_type="TEXT") as write:
        write(THIS, stamps=(9.5, 10.2, 100.1))
        frame = _get_frame(_client(tmp_path), 10.3)
        assert frame["t"] == 10.2
        assert frame["t_prev"] == 9.5
        assert frame["t_next"] == 100.1


# -- the index is secured when the service starts ----------------------------

def test_startup_secures_the_index_so_an_upgrade_leaves_nothing_unreadable(
        tmp_path, monkeypatch):
    """An upgrade must not leave the campaigns it already holds unqueryable.

    Campaigns ingested before the scoping existed carry no policy, and a scoped query
    refuses an unsecured relation rather than answering with the whole corpus. Without
    this the repair arrives only with the next postprocessing run -- so every existing
    campaign reads as broken while the campaigns themselves are perfectly intact, which
    is a repair nobody would think to look for.
    """
    from robovast.results_processing import index_scope

    secured = []
    monkeypatch.setattr(index_scope, "apply_to_index",
                        lambda conn: secured.append(True) or ["public.poses"])
    monkeypatch.setattr("robovast.common.index_db.connect",
                        lambda *a, **k: _NullConn())

    with TestClient(build_app(_local_transport(tmp_path), mount_mcp=False)):
        pass

    assert secured, "startup must repair the index's scoping"


def test_an_unreachable_index_does_not_stop_the_service_booting(tmp_path, monkeypatch):
    """Campaign control, logs and file access do not touch the index.

    Refusing to boot would turn "results are unreadable" into "nothing works". The
    scoped query path fails loudly on its own and names this repair, so nothing becomes
    silently unscoped by starting anyway.
    """
    from robovast.common.errors import IndexUnreachableError

    def _refuse(*_a, **_k):
        raise IndexUnreachableError("ROBOVAST_INDEX_DSN is not set")

    monkeypatch.setattr("robovast.common.index_db.connect", _refuse)

    with TestClient(build_app(_local_transport(tmp_path), mount_mcp=False)) as client:
        assert client.get("/campaigns").status_code == 200, (
            "a service whose index is down must still serve everything else")


class _NullConn:
    """A connection that is only entered and exited; the repair itself is stubbed."""

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False
