# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``run_rtf_view``: the realtime factor per simulated second of each episode, from a run's
clock map."""

import duckdb
import pytest

from robovast.results_processing.data_query import describe_data_db
from robovast_data import Engine, Scope
from robovast_data.views import run_rtf_sql
from tests.robovast_decode.conftest import make_roqsim_campaign, write_roqsim_mcap

from .conftest import write_store


def _windows(samples):
    """The view's rows over a ``clock_map`` without ``episode`` holding *samples*
    ``(wall_ts, sim_ts)`` of one run, as a map read from ``/clock`` is."""
    con = duckdb.connect()
    con.execute("CREATE TABLE clock_map (campaign_id TEXT, config_name TEXT, run_id BIGINT, "
                "wall_ts DOUBLE, sim_ts DOUBLE)")
    for wall, sim in samples:
        con.execute("INSERT INTO clock_map VALUES ('c', 'cfg', 0, ?, ?)", [wall, sim])
    columns = {"campaign_id", "config_name", "run_id", "wall_ts", "sim_ts"}
    return con.execute(f"SELECT * EXCLUDE (episode) FROM ({run_rtf_sql(columns)}) "
                       "ORDER BY sim_start").fetchall()


def test_each_window_carries_the_factor_measured_inside_it():
    # 0..1 s of sim time in 1 s of wall time, then 1..2 s in 2 s of wall time.
    rows = _windows([(0.0, 0.0), (0.5, 0.5), (0.99, 0.99), (1.0, 1.0), (2.0, 1.5),
                     (2.98, 1.99)])
    assert [(r[3], r[4]) for r in rows] == [(0.0, 1.0), (1.0, 2.0)]
    assert rows[0][7] == pytest.approx(1.0)
    assert rows[1][7] == pytest.approx(0.99 / 1.98)
    assert rows[1][5] == pytest.approx(1.98) and rows[1][6] == pytest.approx(0.99)


def test_a_window_with_one_sample_has_no_row():
    rows = _windows([(0.0, 0.0), (0.5, 0.5), (3.0, 1.2)])
    assert [r[3:] for r in rows] == [(0.0, 1.0, 0.5, 0.5, 1.0)]


def test_no_clock_map_rows_give_no_rows():
    assert _windows([]) == []


def test_a_map_without_episode_is_one_series_with_a_null_episode():
    con = duckdb.connect()
    con.execute("CREATE TABLE clock_map (campaign_id TEXT, config_name TEXT, run_id BIGINT, "
                "wall_ts DOUBLE, sim_ts DOUBLE)")
    con.execute("INSERT INTO clock_map VALUES ('c', 'cfg', 0, 0.0, 0.0), ('c', 'cfg', 0, 1.0, 0.5)")
    sql = run_rtf_sql({"campaign_id", "config_name", "run_id", "wall_ts", "sim_ts"})
    assert con.execute(f"SELECT episode, realtime_factor FROM ({sql})").fetchall() == [
        (None, 0.5)]


def test_no_window_spans_a_reset(tmp_path):
    # A stepped run of two episodes, sim time starting over at the reset: both cover the
    # first simulated second, and each is its own window at its own factor.
    root = tmp_path / "episodes"
    make_roqsim_campaign(root, runs=(("cfg", 0),))
    write_store(root, {"cfg": {"params": {}, "runs": {0: "passed"}}})
    write_roqsim_mcap(root / "cfg" / "0" / "roqsim_bag" / "roqsim.mcap", trials=2,
                      meta={"episode": 1})
    engine = Engine([Scope(str(root))], workers=1)
    rows = engine.arrow("SELECT episode, sim_start, sim_span_s, realtime_factor "
                        "FROM run_rtf_view ORDER BY episode, sim_start").to_pylist()
    assert [(r["episode"], r["sim_start"]) for r in rows] == [(1, 0.0), (2, 0.0)]
    # The first episode runs at 1.25x wall time throughout; the second holds the fixture's
    # half-second pause. One window across the reset would measure from the first episode's
    # start to the second's end.
    assert [r["sim_span_s"] for r in rows] == pytest.approx([0.98, 0.98])
    assert rows[0]["realtime_factor"] == pytest.approx(1.25)
    assert rows[1]["realtime_factor"] == pytest.approx(0.98 / (0.98 + 0.5), rel=1e-3)


def test_a_campaign_reads_the_view_and_a_pause_is_a_factor_of_zero(campaign):
    # Each fixture run's clock holds sim time at 1.875 s for 0.2 s of wall time: the one
    # second with two samples is a pause.
    rows = Engine([Scope(str(campaign))], workers=1).arrow(
        "SELECT config_name, run_id, sim_start, sim_end, wall_span_s, realtime_factor "
        "FROM run_rtf_view ORDER BY 1, 2").to_pylist()
    assert [(r["config_name"], r["run_id"], r["sim_start"], r["sim_end"]) for r in rows] == [
        ("cfg-a", 0, 1.0, 2.0), ("cfg-a", 1, 1.0, 2.0), ("cfg-b", 0, 1.0, 2.0)]
    assert all(r["wall_span_s"] == pytest.approx(0.2) and r["realtime_factor"] == 0.0
               for r in rows)


def test_describe_lists_the_view_with_its_column_notes(campaign):
    Engine([Scope(str(campaign))], workers=1).ensure(["clock_map"])
    (entry,) = [t for t in describe_data_db(str(campaign))["tables"]
                if t["table"] == "run_rtf_view"]
    assert entry["kind"] == "view" and entry["description"]
    assert {"realtime_factor", "sim_start", "episode"} <= set(entry["column_notes"])
