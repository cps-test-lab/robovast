# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``run_validity_view`` — was a run a clean observation of the system under test?

The counters it reads are monotonic and the threshold that reads them is calibrated, so
every consumer that re-derived this got a chance to get it wrong. These pin the three ways
that happened.

The view now lives in the central index rather than in a per-campaign ``data.db``, so the
rows get there the way every other measurement does: a ``system_usage.csv`` per run
directory, ingested by :func:`~robovast.results_processing.campaign_ingest.ingest_campaign`.
What is asserted is unchanged -- and deliberately so, because these are the numbers a
4-byte ``real`` or a mistranslated cast would move without raising. Set
``ROBOVAST_TEST_PG_DSN`` to run the reading tests; without it they skip, and the two that
only read prose still run.
"""

import csv
import os
from pathlib import Path

import pytest

from robovast.results_processing.advice import THROTTLE_WARN_RATIO

DSN = os.environ.get("ROBOVAST_TEST_PG_DSN")
pg = pytest.mark.skipif(not DSN, reason="ROBOVAST_TEST_PG_DSN is not set")

SCHEMA = "run_validity_view_test"
CAMPAIGN = "camp-validity-2026-08-10-07150919"

#: The tick columns a sampler without the PSI probe wrote.
_COLUMNS = ["timestamp", "wall_ts", "in_window", "container", "nr_periods",
            "nr_throttled", "throttled_usec"]
#: ... and with it.
_PSI_COLUMNS = _COLUMNS + ["cpu_stall_some_usec", "cpu_stall_full_usec"]


def _write(root: Path, columns, per_run) -> None:
    """One ``system_usage.csv`` per ``(config, run)`` directory, plus the marker that makes
    the tree a campaign root."""
    (root / "_execution").mkdir(parents=True, exist_ok=True)
    for (config, run), rows in per_run.items():
        run_dir = root / config / str(run)
        run_dir.mkdir(parents=True, exist_ok=True)
        with (run_dir / "system_usage.csv").open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            writer.writerows(rows)


@pytest.fixture(name="ingest")
def _ingest(monkeypatch, tmp_path):
    """``ingest(ticks, psi=False) -> campaign root``, in a schema of this module's own.

    A schema per test rather than per module: half of these turn on which columns
    ``system_usage`` has, and one of them on the table not existing at all -- which a view
    left over from the previous test would answer for.
    """
    psycopg = pytest.importorskip("psycopg")
    from robovast.common import index_db
    from robovast.results_processing import campaign_ingest, index_views

    with psycopg.connect(DSN, autocommit=True) as setup:
        for statement in (f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE",
                          "DROP SCHEMA IF EXISTS campaign CASCADE",
                          f"CREATE SCHEMA {SCHEMA}"):
            setup.execute(statement)
    monkeypatch.setenv(index_db.DSN_ENV, f"{DSN} options=-csearch_path={SCHEMA}")

    def build(ticks=None, *, psi=False, empty=False) -> Path:
        from robovast.results_processing import index_query

        root = tmp_path / CAMPAIGN
        if empty:
            # A campaign recorded before the probe: rows, but not these rows.
            run_dir = root / "cfg-a" / "0"
            run_dir.mkdir(parents=True)
            (run_dir / "poses.csv").write_text("timestamp,x\n0.5,1.0\n", encoding="utf-8")
            (root / "_execution").mkdir(parents=True, exist_ok=True)
        else:
            per_run = {}
            for tick in ticks:
                if psi:
                    config, run, container, in_window, wall_ts, periods, throttled, stall \
                        = tick
                    row = [0, wall_ts, in_window, container, periods, throttled, 0,
                           stall * 2, stall]
                else:
                    config, run, container, in_window, periods, throttled, usec = tick
                    row = [0, 0, in_window, container, periods, throttled, usec]
                per_run.setdefault((config, run), []).append(row)
            _write(root, _PSI_COLUMNS if psi else _COLUMNS, per_run)

        with index_query.open_index(readonly=False) as conn:
            campaign_ingest.ingest_campaign(conn, str(root), CAMPAIGN)
            index_views.create_views(conn)
        return root

    yield build

    with psycopg.connect(DSN, autocommit=True) as teardown:
        teardown.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        teardown.execute("DROP SCHEMA IF EXISTS campaign CASCADE")


def _rows(sql):
    from robovast.results_processing import index_query

    return index_query.query_index(sql, campaign_id=CAMPAIGN)["rows"]


def _view_rows(where=""):
    return _rows("SELECT * FROM run_validity_view "
                 f"WHERE campaign_id = '{CAMPAIGN}'{(' AND ' + where) if where else ''}")


@pg
def test_the_ratio_is_an_in_window_delta_not_a_sum_or_a_bare_max(ingest):
    """The three ways of reading a monotonic counter wrongly, in one campaign.

    The container enters the trial window having already been throttled 100 times in 1000
    periods (bring-up, which nobody is measuring), then takes 1 more in 100 in-window
    periods. SUM over the tick rows and MAX without the delta both answer about the
    lifetime of the container; only the in-window delta answers about the trial.
    """
    ingest([
        # (cfg, run, container, in_window, nr_periods, nr_throttled, usec)
        ("cfg-a", 0, "sut", 0, 1000, 100, 5_000),   # before the window: not measured
        ("cfg-a", 0, "sut", 1, 1000, 100, 5_000),   # window opens at the carried-in value
        ("cfg-a", 0, "sut", 1, 1100, 101, 5_100),   # +100 periods, +1 throttled
    ])
    row, = _view_rows()
    assert (row["periods"], row["throttled"]) == (100, 1)
    assert row["throttle_ratio"] == pytest.approx(0.01)
    assert row["throttled_usec"] == 100
    # A SUM would have said 3100/201 and a bare MAX 1100/101; both are the container's life.
    assert row["throttle_ratio"] != pytest.approx(201 / 3100)
    assert row["throttle_ratio"] != pytest.approx(101 / 1100)


@pg
def test_no_quota_enforced_is_not_a_clean_run(ingest):
    """``nr_periods = 0`` means no CPU quota was enforced at all. That is a different fact
    from a quota that was never hit, so the ratio is NULL rather than 0 -- a reader
    averaging it gets NULL-skipped instead of a fabricated zero pulling the mean down."""
    ingest([
        ("cfg-a", 0, "sut", 1, 0, 0, 0),
        ("cfg-a", 0, "sut", 1, 0, 0, 0),
    ])
    row, = _view_rows()
    assert row["periods"] == 0
    assert row["throttle_ratio"] is None
    assert row["quota_bound"] == 0


@pg
def test_quota_bound_uses_the_calibrated_threshold_not_a_round_number(ingest):
    """Just under and just over :data:`THROTTLE_WARN_RATIO`.

    The number is measured, not obvious: a campaign throttled 0.79% of periods lost six
    runs of fifty, so a reader guessing at a round 1% would have called it clean. Pinned
    against the constant rather than a literal so the two cannot drift apart.
    """
    under = int(round((THROTTLE_WARN_RATIO / 2) * 10_000))
    over = int(round((THROTTLE_WARN_RATIO * 2) * 10_000))
    ingest([
        ("cfg-a", 0, "sut", 1, 0, 0, 0),
        ("cfg-a", 0, "sut", 1, 10_000, under, 0),
        ("cfg-a", 1, "sut", 1, 0, 0, 0),
        ("cfg-a", 1, "sut", 1, 10_000, over, 0),
    ])
    got = {r["run_id"]: r["quota_bound"] for r in _view_rows()}
    assert got == {0: 0, 1: 1}


def test_the_flag_is_about_the_containers_own_limit_not_about_neighbours():
    """``quota_bound``, deliberately not ``starved``.

    CFS bandwidth control throttles a cgroup when it exhausts the quota its own
    ``limits.cpu`` buys inside one enforcement period. A busy neighbour does not cause that
    -- it causes scheduling latency -- and the two point OPPOSITE ways: a container that
    cannot get CPU never reaches its quota, so a contended node throttles LESS while running
    worse. Naming it for competition would send a reader to look at what else was on the
    node, when the remedy is a larger limit.

    Pinned as documentation rather than behaviour, because the wrong name is the kind of
    mistake that survives review and then misroutes every diagnosis made from the column.
    Ungated: prose needs no database.
    """
    from robovast.results_processing.data_query import _TABLE_DESCRIPTIONS

    desc = _TABLE_DESCRIPTIONS[("temp", "run_validity_view")]
    assert "quota_bound" in desc
    assert "does NOT mean other campaigns crowded it out" in desc
    assert "scheduling latency" in desc


@pg
def test_every_container_is_reported_so_the_sut_can_be_compared_against_them(ingest):
    """Not filtered to the SUT here, though the SUT is what decides validity.

    A squeezed simulator throttling harder than the SUT while the runs stay good is the
    observation that teaches a reader which container's throttling matters -- and it is
    only available if both are in the same shape. Filtering to 'sut' in the view would hide
    the comparison; filtering in the query is one WHERE.
    """
    ingest([
        ("cfg-a", 0, "sut", 1, 0, 0, 0),
        ("cfg-a", 0, "sut", 1, 10_000, 1, 0),
        ("cfg-a", 0, "simulation", 1, 0, 0, 0),
        ("cfg-a", 0, "simulation", 1, 10_000, 1_000, 0),
    ])
    by = {r["container"]: r for r in _view_rows()}
    assert set(by) == {"sut", "simulation"}
    assert by["simulation"]["quota_bound"] == 1 and by["sut"]["quota_bound"] == 0
    clean = _view_rows("container='sut' AND quota_bound=0")
    assert [r["run_id"] for r in clean] == [0]


@pg
def test_a_store_without_the_probe_has_no_view_rather_than_an_empty_one(ingest):
    """Silence is not a pass, so the view must not exist at all on a campaign recorded
    before the probe (or on a host with no cgroup v2). An empty view would read as "nothing
    was capped"; a missing one says the question cannot be answered here."""
    from robovast.results_processing import index_query

    ingest(empty=True)

    names = {t["table"] for t in index_query.describe_index(CAMPAIGN)["tables"]}
    assert "run_validity_view" not in names
    with pytest.raises(Exception):
        _view_rows()


@pg
def test_the_view_is_described_so_it_is_discoverable(ingest):
    from robovast.results_processing import index_query

    ingest([("cfg-a", 0, "sut", 1, 10, 0, 0)])
    tables = {t["table"]: t for t in index_query.describe_index(CAMPAIGN)["tables"]}
    assert "run_validity_view" in tables
    desc = tables["run_validity_view"].get("description", "")
    # The two things a reader must not get wrong: which container decides, and that this
    # flags rather than filters.
    assert "sut" in desc
    assert "Never drop a run" in desc


# -- the other half: crowded out rather than capped --------------------------------------


@pg
def test_contention_is_the_stall_the_containers_own_ceiling_does_not_explain(ingest):
    """The case no throttle counter can report: never capped, and yet runnable with nothing
    running for a fifth of the window, because other work took the cores it had not reserved.

    100 s of window, 20 s of it with EVERY task in the cgroup waiting -- and 0 throttled
    periods, which is what makes the existing screen read clean.
    """
    ingest([
        # (cfg, run, container, in_window, wall_ts, periods, throttled, stall_full_usec)
        ("cfg-a", 0, "sut", 1, 1000.0, 0, 0, 0),
        ("cfg-a", 0, "sut", 1, 1100.0, 1000, 0, 20_000_000),
    ], psi=True)
    row, = _view_rows()
    assert row["quota_bound"] == 0, "it never reached its own quota -- that is the point"
    assert row["stalled_full_usec"] == 20_000_000
    assert row["stall_ratio"] == pytest.approx(0.2)
    assert row["contended"] == 1


@pg
def test_the_ceiling_is_attributed_first_when_a_container_is_both(ingest):
    """Throttling raises the stall counter too, so the two cannot be separated by
    subtraction. ``contended`` is the residue, and a container held at its own limit is
    reported as that -- the remedy is a line in the campaign's own file."""
    ingest([
        ("cfg-a", 0, "sut", 1, 1000.0, 0, 0, 0),
        ("cfg-a", 0, "sut", 1, 1100.0, 1000, 500, 20_000_000),
    ], psi=True)
    row, = _view_rows()
    assert row["quota_bound"] == 1
    assert row["stall_ratio"] == pytest.approx(0.2), "still recorded, just not attributed"
    assert row["contended"] == 0


@pg
def test_a_store_recorded_before_the_psi_probe_answers_null_rather_than_clean(ingest):
    """The whole reason the columns are selected as NULL instead of dropped: the view keeps
    ONE column set across store versions, so a reader writes one query -- and an older
    campaign says "not measured" where a 0 would have said "no contention"."""
    ingest([
        ("cfg-a", 0, "sut", 1, 1000, 0, 0),
        ("cfg-a", 0, "sut", 1, 1100, 0, 0),
    ])
    row, = _view_rows()
    assert row["quota_bound"] == 0, "the half it can answer is unaffected"
    assert row["stalled_full_usec"] is None
    assert row["stall_ratio"] is None
    assert row["contended"] is None


def test_the_two_flags_are_documented_as_opposite_diagnoses():
    """They point different ways and have different remedies: a bigger limit for one, a
    bigger request or a quieter node for the other. A reader who conflates them tunes the
    wrong number, which is the mistake the naming exists to prevent. Ungated."""
    from robovast.results_processing.data_query import _TABLE_DESCRIPTIONS

    desc = _TABLE_DESCRIPTIONS[("temp", "run_validity_view")]
    assert "does NOT mean other campaigns crowded it out" in desc  # quota_bound
    assert "contended=1 is the OPPOSITE diagnosis" in desc
    assert "not a bigger limit" in desc
