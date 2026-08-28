# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The container-level counter lane, end to end: sampler -> per-job CSV -> per-run table.

Two properties carry this feature and are pinned here rather than left to inspection. The
first is that it is **column-generic**: nothing between the sampler and ``data.db`` names a
metric, so adding a probe is a one-line change. The second is that it uses the **same
partition** as ``resource_usage`` -- a job serves several runs, and a counter copied into all
of them reports a multiple of the truth in every aggregate, plausibly and with no error.
"""

from robovast.execution.data import monitor_resources as mon
from robovast.results_processing import clock_map, run_slices, system_usage


def _slice_(tmp_path, job_dir, start=100.0, end=200.0, claim=None):
    """*start*/*end* are the trial window; *claim* the run's share of the job timeline.

    They differ on purpose: bring-up before the trial still belongs to this run.
    """
    claim_start, claim_end = claim if claim else (start, end)
    run_dir = tmp_path / "cfg" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_slices.RunSlice(
        config_name="cfg", run_dir=run_dir, job_dir=str(job_dir),
        clock=clock_map.ClockMap([], []), start_epoch=start, end_epoch=end,
        claim_start=claim_start, claim_end=claim_end,
        log_claim_start=claim_start, log_claim_end=claim_end)


# -- the sampler ------------------------------------------------------------------------

def test_the_sibling_is_derived_from_the_process_file_so_no_entrypoint_changes():
    """The launch contract passes ONE path. Deriving the second keeps both scripts, and the
    test that pins them, untouched."""
    assert mon.system_usage_path("/out/_jobs/job-3/resource_usage_sut.csv") == \
        "/out/_jobs/job-3/system_usage_sut.csv"
    assert mon.system_usage_path("/out/resource_usage_main.csv") == "/out/system_usage_main.csv"


def test_a_probe_that_cannot_answer_contributes_no_columns_rather_than_zeros(monkeypatch):
    """A runtime that cannot report and a container that was never throttled are different
    facts; zeros would make the first indistinguishable from the second in every aggregate."""
    monkeypatch.setattr(mon, "CPU_STAT_PATH", "/nonexistent/cpu.stat")
    monkeypatch.setattr(mon, "CPU_STAT_PATH_V1", "/nonexistent/cpu/cpu.stat")
    assert mon.cpu_stat_probe() == {}
    assert mon.start_probes([mon.cpu_stat_probe]) == []


def _cpu_stat(tmp_path, monkeypatch, *, v2=None, v1=None):
    """Point the probe at written-out cpu.stat files; a version omitted is absent."""
    for name, text, attr in (("v2.stat", v2, "CPU_STAT_PATH"),
                             ("v1.stat", v1, "CPU_STAT_PATH_V1")):
        if text is None:
            monkeypatch.setattr(mon, attr, str(tmp_path / f"missing-{name}"))
        else:
            f = tmp_path / name
            f.write_text(text, encoding="utf-8")
            monkeypatch.setattr(mon, attr, str(f))


def test_cgroup_v2_is_read_as_it_comes(tmp_path, monkeypatch):
    _cpu_stat(tmp_path, monkeypatch,
              v2="usage_usec 900\nnr_periods 1000\nnr_throttled 7\nthrottled_usec 4200\n")
    assert mon.cpu_stat_probe() == {"nr_periods": 1000, "nr_throttled": 7,
                                    "throttled_usec": 4200}


def test_cgroup_v1_is_read_too_and_converted_to_the_same_unit(tmp_path, monkeypatch):
    """v1 spells it ``throttled_time`` in NANOseconds; v2 uses ``throttled_usec``.

    Converted on read so one column cannot mean nanoseconds on one node and microseconds on
    another. That would be a silent 1000x error in exactly the comparison this data exists
    for -- per-node -- where a missing column would at least have been visible.
    """
    _cpu_stat(tmp_path, monkeypatch,
              v1="nr_periods 1000\nnr_throttled 7\nthrottled_time 4200000\n")
    assert mon.cpu_stat_probe() == {"nr_periods": 1000, "nr_throttled": 7,
                                    "throttled_usec": 4200}


def test_v2_wins_when_both_are_present(tmp_path, monkeypatch):
    """A host running v2 may still carry a v1 tree; the one the kernel enforces against is v2,
    and reading both would double-report."""
    _cpu_stat(tmp_path, monkeypatch,
              v2="nr_periods 10\nnr_throttled 1\nthrottled_usec 5\n",
              v1="nr_periods 999\nnr_throttled 99\nthrottled_time 999000\n")
    assert mon.cpu_stat_probe()["nr_periods"] == 10


def test_an_unrecognisable_v2_file_falls_through_to_v1(tmp_path, monkeypatch):
    """The file existing is not the same as it answering. A cgroup v2 tree mounted without
    CPU accounting has a cpu.stat with no nr_periods in it, and reporting a half-filled row
    for that would hide a node that v1 could have measured."""
    _cpu_stat(tmp_path, monkeypatch,
              v2="usage_usec 900\n",
              v1="nr_periods 1000\nnr_throttled 7\nthrottled_time 4200000\n")
    assert mon.cpu_stat_probe() == {"nr_periods": 1000, "nr_throttled": 7,
                                    "throttled_usec": 4200}


def test_availability_is_decided_once_so_the_header_cannot_go_ragged():
    """A probe that came and went mid-run would produce a CSV the generic ingest cannot type."""
    calls = []

    def flaky():
        calls.append(1)
        return {"metric": len(calls)} if len(calls) == 1 else {}

    probes = mon.start_probes([flaky])
    assert probes and probes[0][1] == ("metric",)
    # Still one column later, even though the probe has stopped answering -- blank, not gone.
    assert mon._system_row(probes) == [""]


def test_a_probe_that_raises_does_not_take_the_sampler_down():
    """The run's own measurement is worth more than this diagnostic."""
    def broken():
        raise RuntimeError("no")

    assert mon.start_probes([broken]) == []
    assert mon._system_row([(broken, ("x",))]) == [""]


# -- the slicer -------------------------------------------------------------------------

def test_columns_are_carried_through_without_being_named(tmp_path):
    """The point of the lane: a metric the sampler invents appears downstream untouched."""
    job = tmp_path / "_jobs" / "job-0"
    job.mkdir(parents=True)
    (job / "system_usage_sut.csv").write_text(
        "timestamp,nr_throttled,some_future_metric\n150.0,7,42\n")
    columns, samples = system_usage.collect_job_rows(str(job))
    assert columns == ["nr_throttled", "some_future_metric"]
    assert samples == [("sut", 150.0, {"nr_throttled": "7", "some_future_metric": "42"})]
    assert system_usage.fieldnames(columns)[-2:] == ["nr_throttled", "some_future_metric"]


def test_containers_reporting_different_sets_are_unioned_with_blanks(tmp_path):
    """Probe availability is a property of the image, so a CPU-only sidecar and a simulator
    can legitimately report different columns in the same job."""
    job = tmp_path / "_jobs" / "job-0"
    job.mkdir(parents=True)
    (job / "system_usage_main.csv").write_text("timestamp,a\n150.0,1\n")
    (job / "system_usage_sut.csv").write_text("timestamp,b\n151.0,2\n")
    columns, samples = system_usage.collect_job_rows(str(job))
    assert columns == ["a", "b"]
    rows = system_usage.rows_for_slice(columns, samples, _slice_(tmp_path, job))
    by_container = {r["container"]: r for r in rows}
    assert by_container["robovast"]["a"] == "1" and by_container["robovast"]["b"] == ""
    assert by_container["sut"]["b"] == "2" and by_container["sut"]["a"] == ""


def test_a_tick_belongs_to_exactly_one_run(tmp_path):
    """The partition ``resource_usage`` uses, for the same reason: a counter copied into every
    run of a packed job multiplies the truth in every aggregate."""
    job = tmp_path / "_jobs" / "job-0"
    job.mkdir(parents=True)
    (job / "system_usage_main.csv").write_text(
        "timestamp,nr_throttled\n50.0,1\n150.0,2\n250.0,3\n")
    columns, samples = system_usage.collect_job_rows(str(job))
    mine = system_usage.rows_for_slice(columns, samples, _slice_(tmp_path, job, 100.0, 200.0))
    assert [r["wall_ts"] for r in mine] == ["150.000000000"]


def test_an_unparseable_stamp_is_dropped_not_guessed(tmp_path):
    """Without a stamp the row cannot be attributed, and attributing it to the wrong run is
    worse than losing it."""
    job = tmp_path / "_jobs" / "job-0"
    job.mkdir(parents=True)
    (job / "system_usage_main.csv").write_text("timestamp,x\n,1\nnonsense,2\n150.0,3\n")
    _, samples = system_usage.collect_job_rows(str(job))
    assert [s[1] for s in samples] == [150.0]


def test_no_files_is_an_empty_table_not_a_crash(tmp_path):
    """A runtime that reports nothing still gets a header, so readers can say 'no data'."""
    job = tmp_path / "_jobs" / "job-0"
    job.mkdir(parents=True)
    columns, samples = system_usage.collect_job_rows(str(job))
    assert (columns, samples) == ([], [])
    assert system_usage.fieldnames([]) == list(system_usage.KEY_FIELDS)


def test_out_of_window_ticks_are_kept_and_marked(tmp_path):
    """Bring-up and teardown are the run's cost too; ``in_window`` separates them rather than
    the slicer dropping them."""
    job = tmp_path / "_jobs" / "job-0"
    job.mkdir(parents=True)
    (job / "system_usage_main.csv").write_text("timestamp,x\n105.0,1\n150.0,2\n")
    columns, samples = system_usage.collect_job_rows(str(job))
    slice_ = _slice_(tmp_path, job, start=120.0, end=200.0, claim=(100.0, 200.0))
    marks = {r["wall_ts"]: r["in_window"] for r in
             system_usage.rows_for_slice(columns, samples, slice_)}
    assert marks == {"105.000000000": 0, "150.000000000": 1}


# -- the sizing authority reads the kernel, not the sum of processes ---------------------

def test_the_cgroup_peak_replaces_summed_rss_and_the_advice_says_which(monkeypatch):
    """The bug this exists to prevent: summing per-process RSS counts a shared page once per
    process, so a stack of many nodes sharing libraries and a Fast DDS segment reports several
    times what the container holds -- and the limit is enforced against the container.

    Measured on one basic_nav campaign: summed RSS peaked at 5147 MiB inside a 2944 MiB limit
    that never OOM-killed anything.
    """
    from robovast.results_processing import advice

    usage = [{"container": "simulation", "ticks": 100, "cpu_p95": 0.5, "cpu_peak": 1.0,
              "core_seconds": 50.0, "mem_peak": 5147 * 1024 ** 2}]
    declared = [{"fullkey": "$.execution.containers.simulation", "value": "{}"}]
    cgroup = [{"container": "simulation", "mem_peak": 1000 * 1024 ** 2}]

    with_cgroup = advice.resource_advice(usage, declared, cgroup)
    mem = [a for a in with_cgroup if a["kind"].startswith("memory")]
    assert mem, "expected memory advice"
    # 1000Mi x 1.25 = 1250Mi, not 5147Mi x 1.25 -- the kernel's number won.
    suggested = mem[0]["evidence"]["suggested_per_container"]["simulation"]
    assert suggested.endswith("Mi") and 1200 <= int(suggested[:-2]) <= 1300, suggested
    assert "kernel" in mem[0]["detail"]

    # Without it the older figure is still used -- but the advice states that it is.
    without = advice.resource_advice(usage, declared, [])
    mem_without = [a for a in without if a["kind"].startswith("memory")][0]
    assert "over-reports" in mem_without["detail"]
    assert int(mem_without["evidence"]["suggested_per_container"]["simulation"][:-2]) > 5000


def test_absent_system_usage_table_does_not_break_advice():
    """A campaign recorded before the probe existed has no such table; the query raises and
    the advice must fall back rather than propagate."""
    from robovast.results_processing import advice

    def query_rows(sql):
        if "system_usage" in sql:
            raise RuntimeError("no such table: system_usage")
        return []

    assert advice.campaign_advice(query_rows) == {"advice": []}


# -- the SUT validity screen ------------------------------------------------------------

def _throttle_row(container, periods, throttled, runs=10, runs_throttled=3):
    return {"container": container, "periods": periods, "throttled": throttled,
            "runs": runs, "runs_throttled": runs_throttled}


def test_silence_is_the_result_worth_having():
    """No report means no run was starved, so a failure is attributable to the stack. That is
    the normal case and it must not be noisy."""
    from robovast.results_processing import advice
    assert advice.throttle_advice([_throttle_row("sut", 2054, 0)], []) == []
    # 2 periods of 2054 (0.10%) is bring-up noise, measured on a healthy campaign.
    assert advice.throttle_advice([_throttle_row("sut", 2054, 2)], []) == []
    # 0.385% cost 0 control-loop misses in the sweep -- still silence.
    assert advice.throttle_advice([_throttle_row("sut", 100000, 385)], []) == []


def test_the_threshold_catches_the_configuration_that_lost_runs():
    """The regression this exists for: an earlier 1% threshold was silent at 0.79%, where the
    campaign missed 58 control loops and lost 6 runs of 50."""
    from robovast.results_processing import advice
    assert advice.throttle_advice([_throttle_row("sut", 100000, 790)], []) != []
    # And the marginal configurations either side of it, since the screen is deliberately
    # sensitive -- a false positive costs a glance, a false negative cost 11 runs.
    assert advice.throttle_advice([_throttle_row("sut", 100000, 580)], []) != []


def test_only_the_system_under_test_is_reported():
    """The simulator is expected to burst and be clipped, and the realtime factor already
    answers whether that hurt. Reporting it would bury the one line that matters."""
    from robovast.results_processing import advice
    assert advice.throttle_advice([_throttle_row("simulation", 2052, 174)], []) == []
    assert advice.throttle_advice([_throttle_row("robovast", 2057, 56)], []) == []


def test_a_throttled_sut_is_flagged_as_inconclusive_not_as_a_fault():
    """It marks where a resource explanation is available; the stack's own health decides."""
    from robovast.results_processing import advice
    out = advice.throttle_advice([_throttle_row("sut", 2000, 880, runs=50, runs_throttled=44)],
                                 [])
    assert len(out) == 1 and out[0]["kind"] == "sut_throttled"
    assert out[0]["severity"] == "warning"
    assert out[0]["evidence"]["throttled_fraction"] == 0.44
    assert out[0]["evidence"]["runs_affected"] == 44
    detail = out[0]["detail"]
    assert "does not by itself mean the stack misbehaved" in detail
    assert "controller frequency" in detail


def test_no_recorded_counters_is_silence_not_a_pass():
    """A campaign predating the probe, or a host without cgroup v2, knows nothing -- and must
    not be mistaken for one that was checked."""
    from robovast.results_processing import advice
    assert advice.throttle_advice([], []) == []
