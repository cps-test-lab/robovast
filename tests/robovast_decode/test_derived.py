# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The tables derived from a job's records, built over a whole campaign as a query builds them.

The unit tests cover the parsing and the trial window. What only shows up here is the wiring:
that a run finds its job through the job-link **manifest** and not the ``job`` symlink, that a
degraded campaign still builds and says what was wrong, and that the tables come out typed.
"""

import pyarrow.parquet as pq
import yaml

from robovast_decode.build import available_tables, build

from .conftest import make_campaign

_HEADER = "timestamp,pid,name,cpu_percent,memory_rss_bytes\n"
_SCEN = "scenario_execution_ros"

#: A run's log, tipping over. Both timings around the trial are the measured ones: the
#: ``Executing scenario`` line comes ~35 us BEFORE the run's test.xml start, and a failing
#: run's verdict lands ~1 ms AFTER its window closes.
_FAILING_LOG = (
    f"[INFO] [99.99996] [{_SCEN}]: Executing scenario 'test_scenario-0'\n"
    f"[INFO] [104.900] [{_SCEN}]: Outcome: tip_over | max_tilt=0.70\n"
    f"[ERROR] [104.950] [{_SCEN}]: FAILURE: tip_over\n"
    f"[ERROR] [105.0011] [{_SCEN}]: test_scenario-0: execution failed. -^- [x]\n"
    f"[INFO] [105.060] [{_SCEN}]: Shutting down finished.\n"
)

CONFIG = {"containers": ["robovast", "sut"]}


def _xml(start_epoch, duration=10.0, failures=0):
    return (f'<testsuite errors="0" failures="{failures}" tests="1">'
            f'<testcase time="{duration}">'
            f'<properties><property name="start_time" value="{start_epoch}"/></properties>'
            f'</testcase></testsuite>')


def _campaign(tmp_path):
    root = tmp_path / "camp-1"
    (root / "_transient").mkdir(parents=True)
    (root / "_transient" / "job_links.yaml").write_text("{}\n")
    return root


def _job(root, index, containers, ticks=(100.0, 105.0), log_lines=None):
    job = root / "_jobs" / "batch-0" / f"job-{index}"
    (job / "logs").mkdir(parents=True, exist_ok=True)
    for container, filename in containers.items():
        rows = "".join(f"{t},1,python3,10.0,1000\n{t},2,ros2,5.0,500\n" for t in ticks)
        (job / filename).write_text(_HEADER + rows)
        log = "system.log" if container == "robovast" else f"system_{container}.log"
        body = (f"[INFO] [{ticks[0]}] [node]: hello from {container}\n"
                if log_lines is None or container != "robovast" else log_lines)
        (job / "logs" / log).write_text(body)
    return job


def _run(root, config, run, index=None, start_epoch=None, duration=10.0, failures=0):
    run_dir = root / config / str(run)
    run_dir.mkdir(parents=True, exist_ok=True)
    if start_epoch is not None:
        (run_dir / "test.xml").write_text(_xml(start_epoch, duration, failures))
    if index is not None:
        links_path = root / "_transient" / "job_links.yaml"
        links = yaml.safe_load(links_path.read_text()) or {}
        links[f"{config}/{run}/job"] = f"../../_jobs/batch-0/job-{index}"
        links_path.write_text(yaml.safe_dump(links))
    return run_dir


def _rows(root, table, config="cfg-a", run=0):
    return pq.read_table(root / ".cache" / "tables" / table / config / f"{run}.parquet").to_pylist()


def _ordinary(tmp_path):
    root = _campaign(tmp_path)
    _job(root, 0, {"robovast": "resource_usage_main.csv", "sut": "resource_usage_sut.csv"})
    _run(root, "cfg-a", 0, index=0, start_epoch=100.0)
    return root


def test_a_runs_derived_tables_come_from_its_job_through_the_manifest(tmp_path):
    root = _ordinary(tmp_path)
    assert not (root / "cfg-a" / "0" / "job").exists(), "no symlink: the manifest is read"
    report = build(str(root), tables=["run_log", "resource_usage"], config=CONFIG)
    assert report.built["run_log"] == ["cfg-a/0"]
    usage = _rows(root, "resource_usage")
    assert {r["container"] for r in usage} == {"robovast", "sut"}
    assert {r["name"] for r in usage} == {"python3", "ros2"}
    logged = {r["container"] for r in _rows(root, "run_log") if r["container"]}
    assert logged <= {r["container"] for r in usage}, "the two tables name containers alike"


def test_the_derived_tables_are_typed(tmp_path):
    root = _ordinary(tmp_path)
    build(str(root), tables=["resource_usage", "run_log", "run_clock"], config=CONFIG)
    schema = pq.read_schema(root / ".cache/tables/resource_usage/cfg-a/0.parquet")
    assert str(schema.field("cpu_percent").type) == "double"
    assert str(schema.field("memory_rss_bytes").type) == "int64"
    assert str(schema.field("shm_used_bytes").type) == "int64"
    log = pq.read_schema(root / ".cache/tables/run_log/cfg-a/0.parquet")
    assert str(log.field("seq").type) == "int64" and str(log.field("wall_ts").type) == "double"
    (clock,) = _rows(root, "run_clock")
    assert clock["clock_map_source"] == "none" and clock["clock_map_samples"] == 0


def test_a_run_without_a_manifest_entry_is_named_not_silently_empty(tmp_path):
    root = _ordinary(tmp_path)
    _run(root, "cfg-a", 1, start_epoch=100.0)
    report = build(str(root), tables=["resource_usage"], config=CONFIG)
    assert any("cfg-a/1" in n and "no job-link entry" in n for n in report.notes)
    assert _rows(root, "resource_usage", run=1) == []


def test_a_container_that_recorded_nothing_is_named(tmp_path):
    root = _campaign(tmp_path)
    _job(root, 0, {"robovast": "resource_usage_main.csv"})
    _run(root, "cfg-a", 0, index=0, start_epoch=100.0)
    report = build(str(root), tables=["resource_usage"], config=CONFIG)
    assert any("no resource CSV for" in n and "sut" in n for n in report.notes)


def _failing_log_campaign(tmp_path):
    root = _campaign(tmp_path)
    _job(root, 0, {"robovast": "resource_usage_main.csv"}, log_lines=_FAILING_LOG)
    _run(root, "cfg-a", 0, index=0, start_epoch=100.0, duration=5.0, failures=1)
    build(str(root), tables=["run_log", "scenario_timestamps"], config={"containers":
                                                                        ["robovast"]})
    return root


def test_a_run_keeps_its_bring_up_verdict_and_teardown_marked_outside_its_trial(tmp_path):
    rows = _rows(_failing_log_campaign(tmp_path), "run_log")
    window = {r["message"]: r["in_window"] for r in rows}
    assert window["Executing scenario 'test_scenario-0'"] == 0
    assert window["Outcome: tip_over | max_tilt=0.70"] == 1
    assert window["test_scenario-0: execution failed. -^- [x]"] == 0
    assert window["Shutting down finished."] == 0
    assert [r["seq"] for r in rows] == list(range(len(rows)))


def test_a_failing_runs_verdict_past_its_window_is_still_read(tmp_path):
    root = _failing_log_campaign(tmp_path)
    assert _rows(root, "scenario_timestamps")[0]["status"] == "failed"


def test_a_second_build_of_an_unchanged_job_does_nothing(tmp_path):
    root = _ordinary(tmp_path)
    build(str(root), tables=["run_log"], config=CONFIG)
    again = build(str(root), tables=["run_log"], config=CONFIG)
    assert again.built.get("run_log") is None and again.skipped["run_log"] == ["cfg-a/0"]
    with open(root / "_jobs/batch-0/job-0/logs/system.log", "a", encoding="utf-8") as fh:
        fh.write("[INFO] [106.0] [node]: later\n")
    assert build(str(root), tables=["run_log"], config=CONFIG).built["run_log"] == ["cfg-a/0"]


def test_rosout_is_joined_from_the_jobs_recording(tmp_path):
    campaign = make_campaign(tmp_path / "nav")
    build(str(campaign), tables=["run_log", "run_clock"])
    rows = pq.read_table(campaign / ".cache/tables/run_log/cfg/0.parquet").to_pylist()
    assert any(r["source"] == "rosout" for r in rows)
    (clock,) = pq.read_table(campaign / ".cache/tables/run_clock/cfg/0.parquet").to_pylist()
    assert clock["clock_map_source"] == "ros_clock_bag" and clock["clock_map_samples"] > 1


def test_every_run_can_have_its_derived_tables(tmp_path):
    tables = available_tables(str(_ordinary(tmp_path)))
    for name in ("run_log", "scenario_timestamps", "resource_usage", "system_usage",
                 "run_clock"):
        assert tables[name]["runs"] == 1, name
