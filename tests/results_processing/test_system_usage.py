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
    assert mon.cpu_stat_probe() == {}
    assert mon.start_probes([mon.cpu_stat_probe]) == []


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
