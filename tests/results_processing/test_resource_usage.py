# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""What a run cost: reading the monitor's samples, and cutting them to one run.

The two things worth breaking a build over are here. A **damaged** file must not yield a
tick reporting a fraction of a container's CPU, because that is not a parse error, it is a
believable wrong number. And a packed job's ticks must be **partitioned**, because a tick
copied into every run of a job makes every aggregate a multiple of the truth.
"""

import math

import pytest

from robovast.results_processing import resource_usage, run_log, run_slices
from robovast.results_processing.clock_map import NO_CLOCK_MAP, ClockMap

_HEADER = "timestamp,pid,name,cpu_percent,memory_rss_bytes\n"


def _csv(path, rows, header=_HEADER, tail=""):
    path.write_text(header + "".join(
        f"{ts},{pid},{name},{cpu},{mem}\n" for ts, pid, name, cpu, mem in rows) + tail)
    return str(path)


def _tick(rows, container="robovast", wall=100.0):
    return resource_usage.Tick(wall_ts=wall, container=container, processes=rows)


def _slice(tmp_path, *, start=None, end=None, claim_start=-math.inf,
           claim_end=math.inf, clock=NO_CLOCK_MAP):
    run_dir = tmp_path / "cfg" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_slices.RunSlice(
        config_name="cfg", run_dir=run_dir, job_dir=str(tmp_path / "_jobs" / "job-0"),
        clock=clock, start_epoch=start, end_epoch=end,
        claim_start=claim_start, claim_end=claim_end)


# -- the container name, which two tables are joined on -----------------------


def test_the_container_comes_from_the_filename():
    assert run_slices.container_of("resource_usage_main.csv") == "robovast"
    assert run_slices.container_of("resource_usage_sut.csv") == "sut"
    assert run_slices.container_of("resource_usage_simulation.csv") == "simulation"
    assert run_slices.container_of("poses.csv") is None


def test_the_two_producers_agree_on_the_main_containers_name():
    """The join is on this string, so a disagreement returns nothing instead of failing.

    The monitor calls the main container's file ``main``, the log calls it ``system.log``,
    the config calls the role ``scenario`` and the compose service is ``robovast``.
    """
    assert (run_slices.container_of("resource_usage_main.csv")
            == run_log.container_of("system.log"))
    assert (run_slices.container_of("resource_usage_sut.csv")
            == run_log.container_of("system_sut.log"))


# -- reading one container's CSV ----------------------------------------------


def test_samples_of_one_name_are_summed_and_counted(tmp_path):
    _csv(tmp_path / "resource_usage_main.csv", [
        (100.0, 1, "python3", 10.0, 1000),
        (100.0, 2, "python3", 5.0, 500),
        (100.0, 3, "gzserver", 90.0, 4000),
    ])
    (tick,) = resource_usage.collect_job_ticks(
        str(tmp_path), None, resource_usage.ScanStats())
    assert tick.processes["python3"] == (15.0, 1500, 2)
    assert tick.processes["gzserver"] == (90.0, 4000, 1)


def test_a_drifted_header_is_reported_not_misread(tmp_path):
    """A writer that renamed its columns must not be read as if it had not."""
    stats = resource_usage.ScanStats()
    path = tmp_path / "resource_usage_main.csv"
    path.write_text("timestamp,pid,name,cpu_usage,mem_usage\n100.0,1,python3,10.0,1000\n")
    assert resource_usage.read_container_csv(str(path), "robovast", stats) == []
    assert stats.unreadable and "header" in stats.unreadable[0]


def test_a_zero_timestamp_is_rejected(tmp_path):
    """Not an instant in 1970: admitting one puts the run behind it in every ORDER BY."""
    stats = resource_usage.ScanStats()
    path = _csv(tmp_path / "resource_usage_main.csv", [
        (0, 1, "python3", 10.0, 1000), (100.0, 1, "python3", 10.0, 1000),
        (101.0, 1, "python3", 10.0, 1000),
    ])
    samples = resource_usage.read_container_csv(path, "robovast", stats)
    assert [s[0] for s in samples] == [100.0]  # 101.0 dropped as the damaged file's tail
    assert stats.bad_rows == 1


def test_a_damaged_files_last_tick_is_dropped_whole(tmp_path):
    """The dangerous case: a tick cut mid-way reports a fraction of the container's CPU.

    That is not a parse error, it is a believable number. Two of the three processes of the
    last tick survived the cut, so keeping it would report 2/3 of the load as a real dip.
    """
    stats = resource_usage.ScanStats()
    path = _csv(tmp_path / "resource_usage_main.csv", [
        (100.0, 1, "a", 10.0, 1), (100.0, 2, "b", 10.0, 1), (100.0, 3, "c", 10.0, 1),
        (101.0, 1, "a", 10.0, 1), (101.0, 2, "b", 10.0, 1),
    ], tail="101.0,3,c,10.")  # cut mid-row
    samples = resource_usage.read_container_csv(path, "robovast", stats)
    assert {s[0] for s in samples} == {100.0}
    assert stats.dropped_tail_ticks == 1
    assert stats.truncated and "last tick dropped" in stats.truncated[0]


def test_a_clean_files_last_tick_is_kept(tmp_path):
    """Process count genuinely falls during teardown -- a "looks short" heuristic would
    delete real shutdown data from every healthy run."""
    stats = resource_usage.ScanStats()
    path = _csv(tmp_path / "resource_usage_main.csv", [
        (100.0, 1, "a", 10.0, 1), (100.0, 2, "b", 10.0, 1),
        (101.0, 1, "a", 10.0, 1),
    ])
    samples = resource_usage.read_container_csv(path, "robovast", stats)
    assert {s[0] for s in samples} == {100.0, 101.0}
    assert stats.dropped_tail_ticks == 0
    assert not stats.truncated


def test_an_empty_file_is_reported_as_empty_not_missing(tmp_path):
    stats = resource_usage.ScanStats()
    path = tmp_path / "resource_usage_main.csv"
    path.write_text(_HEADER)
    assert resource_usage.read_container_csv(str(path), "robovast", stats) == []
    assert stats.empty == ["robovast"]


# -- the expected container set ------------------------------------------------


class _Container:
    def __init__(self, name):
        self.name = name


class _Plan:
    def __init__(self, *names):
        self.containers = tuple(_Container(n) for n in names)

    @property
    def sidecars(self):
        return list(self.containers[1:])


def test_the_expected_set_comes_from_the_plan(tmp_path):
    expected = resource_usage.expected_container_files(_Plan("robovast", "sut"))
    assert expected == {"robovast": "resource_usage_main.csv",
                        "sut": "resource_usage_sut.csv"}


def test_a_container_that_recorded_nothing_is_reported_not_inferred_away(tmp_path):
    """A vanilla sidecar without psutil writes no file. Taking "the files that are there"
    would turn that into a campaign that simply had fewer containers."""
    job = tmp_path / "job-0"
    job.mkdir()
    _csv(job / "resource_usage_main.csv", [(100.0, 1, "python3", 1.0, 1)])
    stats = resource_usage.ScanStats()
    resource_usage.collect_job_ticks(
        str(job), resource_usage.expected_container_files(_Plan("robovast", "sut")),
        stats, "job-0")
    assert stats.missing == ["job-0:sut"]


def test_an_unexpected_csv_is_ingested_and_flagged(tmp_path):
    job = tmp_path / "job-0"
    job.mkdir()
    _csv(job / "resource_usage_main.csv", [(100.0, 1, "a", 1.0, 1)])
    _csv(job / "resource_usage_ghost.csv", [(100.0, 1, "b", 1.0, 1)])
    stats = resource_usage.ScanStats()
    ticks = resource_usage.collect_job_ticks(
        str(job), resource_usage.expected_container_files(_Plan("robovast")), stats, "job-0")
    assert stats.unexpected == ["job-0:ghost"]
    assert {t.container for t in ticks} == {"robovast", "ghost"}


# -- cutting a job's ticks to one run -----------------------------------------


def test_only_the_ticks_this_run_claims_become_rows(tmp_path):
    ticks = [_tick({"a": (1.0, 1, 1)}, wall=w) for w in (50.0, 150.0, 250.0)]
    rows = resource_usage.rows_for_slice(
        ticks, _slice(tmp_path, claim_start=100.0, claim_end=200.0))
    assert [r["wall_ts"] for r in rows] == ["150.000000000"]


def test_a_tick_belongs_to_exactly_one_run_of_a_packed_job(tmp_path):
    """The invariant the whole partition exists for: SUM over a job's runs is what the job
    consumed. No tick counted twice, none dropped."""
    claims = run_slices._claims_for_job(
        [("cfg/0", 100.0), ("cfg/1", 200.0), ("cfg/2", 300.0)])
    ticks = [_tick({"a": (1.0, 1, 1)}, wall=w)
             for w in (10.0, 90.0, 110.0, 190.0, 210.0, 400.0)]
    claimed = []
    for name, (start, end) in claims.items():
        got = resource_usage.rows_for_slice(
            ticks, _slice(tmp_path, claim_start=start, claim_end=end))
        claimed.append({float(r["wall_ts"]) for r in got})
    union = set().union(*claimed)
    assert union == {t.wall_ts for t in ticks}          # nothing dropped
    assert sum(len(c) for c in claimed) == len(union)   # nothing counted twice


def test_the_gap_between_two_runs_belongs_to_the_one_starting_up(tmp_path):
    """The simulator being reset is the NEXT run's bring-up, not the finished one's tail."""
    claims = run_slices._claims_for_job([("cfg/0", 100.0), ("cfg/1", 200.0)])
    assert claims["cfg/1"][0] == 100.0


def test_a_windowless_run_of_a_packed_job_claims_nothing(tmp_path):
    """It cannot be placed on the wall clock. A table saying "no data" is honest; one
    stating another run's numbers is not."""
    claims = run_slices._claims_for_job([("cfg/0", 100.0), ("cfg/1", None)])
    start, end = claims["cfg/1"]
    assert math.isnan(start) and math.isnan(end)
    rows = resource_usage.rows_for_slice(
        [_tick({"a": (1.0, 1, 1)}, wall=50.0)],
        _slice(tmp_path, claim_start=start, claim_end=end))
    assert rows == []


def test_a_single_run_job_without_test_xml_still_gets_its_whole_trace(tmp_path):
    """The run killed mid-flight is the one whose resource trace matters most."""
    claims = run_slices._claims_for_job([("cfg/0", None)])
    start, end = claims["cfg/0"]
    rows = resource_usage.rows_for_slice(
        [_tick({"a": (1.0, 1, 1)}, wall=50.0)],
        _slice(tmp_path, claim_start=start, claim_end=end))
    assert len(rows) == 1
    assert rows[0]["in_window"] == 1


# -- the two clocks ------------------------------------------------------------


def test_a_tick_outside_the_clock_maps_range_keeps_its_wall_time(tmp_path):
    """Bring-up happens before /clock exists. That is real measurement with no sim time,
    not measurement at sim time zero, and nothing is extrapolated."""
    clock = ClockMap([(100.0, 0.0), (200.0, 100.0)])
    rows = resource_usage.rows_for_slice(
        [_tick({"a": (1.0, 1, 1)}, wall=50.0), _tick({"a": (1.0, 1, 1)}, wall=150.0)],
        _slice(tmp_path, clock=clock))
    assert rows[0]["timestamp"] == "" and rows[0]["wall_ts"] == "50.000000000"
    assert rows[1]["timestamp"] == "50.000000"


def test_ticks_outside_the_trial_window_are_flagged_not_dropped(tmp_path):
    """Bring-up and teardown are this run's, and are worth having -- but they are not the
    trial, and a reader comparing runs must be able to exclude them."""
    ticks = [_tick({"a": (1.0, 1, 1)}, wall=w) for w in (50.0, 150.0, 250.0)]
    rows = resource_usage.rows_for_slice(ticks, _slice(tmp_path, start=100.0, end=200.0))
    assert [r["in_window"] for r in rows] == [0, 1, 0]


# -- the campaign totals -------------------------------------------------------


def test_campaign_totals_carry_every_counter_a_job_reported():
    """Driven by the dataclass fields, so a counter added later cannot be silently dropped
    from the summary -- which is the one thing these counters exist to prevent."""
    import dataclasses
    totals, job = resource_usage.ScanStats(), resource_usage.ScanStats()
    for spec in dataclasses.fields(job):
        setattr(job, spec.name, ["x"] if spec.type.startswith("List") else 3)
    totals.add_job(job)
    for spec in dataclasses.fields(totals):
        if spec.name in resource_usage.ScanStats._PER_RUN_FIELDS:
            continue
        value = getattr(totals, spec.name)
        assert value in (3, ["x"]), f"{spec.name} was not folded"
