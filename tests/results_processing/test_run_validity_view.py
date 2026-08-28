# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``run_validity_view`` — was a run a clean observation of the system under test?

The counters it reads are monotonic and the threshold that reads them is calibrated, so
every consumer that re-derived this got a chance to get it wrong. These pin the three ways
that happened.
"""

import sqlite3
from pathlib import Path

import pytest

from robovast.results_processing.advice import THROTTLE_WARN_RATIO
from robovast.results_processing.data_query import describe_data_db, query_data_db


def _campaign(tmp_path: Path, ticks) -> Path:
    """A campaign whose data.db holds only system_usage, from ``(cfg, run, container,
    in_window, periods, throttled, usec)`` tick rows."""
    exec_dir = tmp_path / "_execution"
    exec_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(exec_dir / "data.db")
    conn.execute("CREATE TABLE system_usage (config_name TEXT, run_id INTEGER, "
                 "timestamp REAL, wall_ts REAL, in_window INTEGER, container TEXT, "
                 "nr_periods INTEGER, nr_throttled INTEGER, throttled_usec INTEGER)")
    conn.executemany(
        "INSERT INTO system_usage VALUES (?,?,0,0,?,?,?,?,?)",
        [(c, r, w, k, p, t, u) for c, r, k, w, p, t, u in ticks])
    conn.commit()
    conn.close()
    return tmp_path


def _rows(campaign_dir, sql):
    return query_data_db(campaign_dir, sql)["rows"]


def test_the_ratio_is_an_in_window_delta_not_a_sum_or_a_bare_max(tmp_path):
    """The three ways of reading a monotonic counter wrongly, in one campaign.

    The container enters the trial window having already been throttled 100 times in 1000
    periods (bring-up, which nobody is measuring), then takes 1 more in 100 in-window
    periods. SUM over the tick rows and MAX without the delta both answer about the
    lifetime of the container; only the in-window delta answers about the trial.
    """
    d = _campaign(tmp_path, [
        # (cfg, run, container, in_window, nr_periods, nr_throttled, usec)
        ("cfg-a", 0, "sut", 0, 1000, 100, 5_000),   # before the window: not measured
        ("cfg-a", 0, "sut", 1, 1000, 100, 5_000),   # window opens at the carried-in value
        ("cfg-a", 0, "sut", 1, 1100, 101, 5_100),   # +100 periods, +1 throttled
    ])
    row, = _rows(d, "SELECT * FROM run_validity_view")
    assert (row["periods"], row["throttled"]) == (100, 1)
    assert row["throttle_ratio"] == pytest.approx(0.01)
    assert row["throttled_usec"] == 100
    # A SUM would have said 3100/201 and a bare MAX 1100/101; both are the container's life.
    assert row["throttle_ratio"] != pytest.approx(201 / 3100)
    assert row["throttle_ratio"] != pytest.approx(101 / 1100)


def test_no_quota_enforced_is_not_a_clean_run(tmp_path):
    """``nr_periods = 0`` means no CPU quota was enforced at all. That is a different fact
    from a quota that was never hit, so the ratio is NULL rather than 0 -- a reader
    averaging it gets NULL-skipped instead of a fabricated zero pulling the mean down."""
    d = _campaign(tmp_path, [
        ("cfg-a", 0, "sut", 1, 0, 0, 0),
        ("cfg-a", 0, "sut", 1, 0, 0, 0),
    ])
    row, = _rows(d, "SELECT * FROM run_validity_view")
    assert row["periods"] == 0
    assert row["throttle_ratio"] is None
    assert row["quota_bound"] == 0


def test_quota_bound_uses_the_calibrated_threshold_not_a_round_number(tmp_path):
    """Just under and just over :data:`THROTTLE_WARN_RATIO`.

    The number is measured, not obvious: a campaign throttled 0.79% of periods lost six
    runs of fifty, so a reader guessing at a round 1% would have called it clean. Pinned
    against the constant rather than a literal so the two cannot drift apart.
    """
    under = int(round((THROTTLE_WARN_RATIO / 2) * 10_000))
    over = int(round((THROTTLE_WARN_RATIO * 2) * 10_000))
    d = _campaign(tmp_path, [
        ("cfg-a", 0, "sut", 1, 0, 0, 0),
        ("cfg-a", 0, "sut", 1, 10_000, under, 0),
        ("cfg-a", 1, "sut", 1, 0, 0, 0),
        ("cfg-a", 1, "sut", 1, 10_000, over, 0),
    ])
    got = {r["run_id"]: r["quota_bound"] for r in _rows(d, "SELECT * FROM run_validity_view")}
    assert got == {0: 0, 1: 1}


def test_the_flag_is_about_the_containers_own_limit_not_about_neighbours(tmp_path):
    """``quota_bound``, deliberately not ``starved``.

    CFS bandwidth control throttles a cgroup when it exhausts the quota its own
    ``limits.cpu`` buys inside one enforcement period. A busy neighbour does not cause that
    -- it causes scheduling latency -- and the two point OPPOSITE ways: a container that
    cannot get CPU never reaches its quota, so a contended node throttles LESS while running
    worse. Naming it for competition would send a reader to look at what else was on the
    node, when the remedy is a larger limit.

    Pinned as documentation rather than behaviour, because the wrong name is the kind of
    mistake that survives review and then misroutes every diagnosis made from the column.
    """
    from robovast.results_processing.data_query import _TABLE_DESCRIPTIONS

    desc = _TABLE_DESCRIPTIONS[("temp", "run_validity_view")]
    assert "quota_bound" in desc
    assert "does NOT mean other campaigns crowded it out" in desc
    assert "scheduling latency" in desc


def test_every_container_is_reported_so_the_sut_can_be_compared_against_them(tmp_path):
    """Not filtered to the SUT here, though the SUT is what decides validity.

    A squeezed simulator throttling harder than the SUT while the runs stay good is the
    observation that teaches a reader which container's throttling matters -- and it is
    only available if both are in the same shape. Filtering to 'sut' in the view would hide
    the comparison; filtering in the query is one WHERE.
    """
    d = _campaign(tmp_path, [
        ("cfg-a", 0, "sut", 1, 0, 0, 0),
        ("cfg-a", 0, "sut", 1, 10_000, 1, 0),
        ("cfg-a", 0, "simulation", 1, 0, 0, 0),
        ("cfg-a", 0, "simulation", 1, 10_000, 1_000, 0),
    ])
    by = {r["container"]: r for r in _rows(d, "SELECT * FROM run_validity_view")}
    assert set(by) == {"sut", "simulation"}
    assert by["simulation"]["quota_bound"] == 1 and by["sut"]["quota_bound"] == 0
    clean = _rows(d, "SELECT run_id FROM run_validity_view "
                     "WHERE container='sut' AND quota_bound=0")
    assert [r["run_id"] for r in clean] == [0]


def test_a_store_without_the_probe_has_no_view_rather_than_an_empty_one(tmp_path):
    """Silence is not a pass, so the view must not exist at all on a campaign recorded
    before the probe (or on a host with no cgroup v2). An empty view would read as "nothing
    was capped"; a missing one says the question cannot be answered here."""
    exec_dir = tmp_path / "_execution"
    exec_dir.mkdir(parents=True)
    conn = sqlite3.connect(exec_dir / "data.db")
    conn.execute("CREATE TABLE runs (config_name TEXT, run_id INTEGER)")
    conn.commit()
    conn.close()

    names = {t["table"] for t in describe_data_db(tmp_path)["tables"]}
    assert "run_validity_view" not in names
    with pytest.raises(Exception):
        query_data_db(tmp_path, "SELECT * FROM run_validity_view")


def test_the_view_is_described_so_it_is_discoverable(tmp_path):
    d = _campaign(tmp_path, [("cfg-a", 0, "sut", 1, 10, 0, 0)])
    tables = {t["table"]: t for t in describe_data_db(d)["tables"]}
    assert "run_validity_view" in tables
    desc = tables["run_validity_view"].get("description", "")
    # The two things a reader must not get wrong: which container decides, and that this
    # flags rather than filters.
    assert "sut" in desc
    assert "Never drop a run" in desc


# -- the other half: crowded out rather than capped --------------------------------------

def _campaign_psi(tmp_path: Path, ticks) -> Path:
    """As :func:`_campaign`, for a store recorded by a sampler that had the PSI probe:
    ``(cfg, run, container, in_window, wall_ts, periods, throttled, stall_full)``."""
    exec_dir = tmp_path / "_execution"
    exec_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(exec_dir / "data.db")
    conn.execute("CREATE TABLE system_usage (config_name TEXT, run_id INTEGER, "
                 "timestamp REAL, wall_ts REAL, in_window INTEGER, container TEXT, "
                 "nr_periods INTEGER, nr_throttled INTEGER, throttled_usec INTEGER, "
                 "cpu_stall_some_usec INTEGER, cpu_stall_full_usec INTEGER)")
    conn.executemany(
        "INSERT INTO system_usage VALUES (?,?,0,?,?,?,?,?,0,?,?)",
        [(c, r, w, k, n, p, t, s * 2, s) for c, r, n, k, w, p, t, s in ticks])
    conn.commit()
    conn.close()
    return tmp_path


def test_contention_is_the_stall_the_containers_own_ceiling_does_not_explain(tmp_path):
    """The case no throttle counter can report: never capped, and yet runnable with nothing
    running for a fifth of the window, because other work took the cores it had not reserved.

    100 s of window, 20 s of it with EVERY task in the cgroup waiting -- and 0 throttled
    periods, which is what makes the existing screen read clean.
    """
    d = _campaign_psi(tmp_path, [
        # (cfg, run, container, in_window, wall_ts, periods, throttled, stall_full_usec)
        ("cfg-a", 0, "sut", 1, 1000.0, 0, 0, 0),
        ("cfg-a", 0, "sut", 1, 1100.0, 1000, 0, 20_000_000),
    ])
    row, = _rows(d, "SELECT * FROM run_validity_view")
    assert row["quota_bound"] == 0, "it never reached its own quota -- that is the point"
    assert row["stalled_full_usec"] == 20_000_000
    assert row["stall_ratio"] == pytest.approx(0.2)
    assert row["contended"] == 1


def test_the_ceiling_is_attributed_first_when_a_container_is_both(tmp_path):
    """Throttling raises the stall counter too, so the two cannot be separated by
    subtraction. ``contended`` is the residue, and a container held at its own limit is
    reported as that -- the remedy is a line in the campaign's own file."""
    d = _campaign_psi(tmp_path, [
        ("cfg-a", 0, "sut", 1, 1000.0, 0, 0, 0),
        ("cfg-a", 0, "sut", 1, 1100.0, 1000, 500, 20_000_000),
    ])
    row, = _rows(d, "SELECT * FROM run_validity_view")
    assert row["quota_bound"] == 1
    assert row["stall_ratio"] == pytest.approx(0.2), "still recorded, just not attributed"
    assert row["contended"] == 0


def test_a_store_recorded_before_the_psi_probe_answers_null_rather_than_clean(tmp_path):
    """The whole reason the columns are selected as NULL instead of dropped: the view keeps
    ONE column set across store versions, so a reader writes one query -- and an older
    campaign says "not measured" where a 0 would have said "no contention"."""
    d = _campaign(tmp_path, [
        ("cfg-a", 0, "sut", 1, 1000, 0, 0),
        ("cfg-a", 0, "sut", 1, 1100, 0, 0),
    ])
    row, = _rows(d, "SELECT * FROM run_validity_view")
    assert row["quota_bound"] == 0, "the half it can answer is unaffected"
    assert row["stalled_full_usec"] is None
    assert row["stall_ratio"] is None
    assert row["contended"] is None


def test_the_two_flags_are_documented_as_opposite_diagnoses(tmp_path):
    """They point different ways and have different remedies: a bigger limit for one, a
    bigger request or a quieter node for the other. A reader who conflates them tunes the
    wrong number, which is the mistake the naming exists to prevent."""
    from robovast.results_processing.data_query import _TABLE_DESCRIPTIONS

    desc = _TABLE_DESCRIPTIONS[("temp", "run_validity_view")]
    assert "does NOT mean other campaigns crowded it out" in desc  # quota_bound
    assert "contended=1 is the OPPOSITE diagnosis" in desc
    assert "not a bigger limit" in desc
