# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The job-artifact plugins driven over a whole campaign tree, as postprocessing runs them.

The unit tests cover the parsing and the slicing. What only shows up here is the wiring:
that a run finds its job through the **manifest** and not the ``job`` symlink (the property
that makes this work on the cluster, where a symlink cannot exist), that a packed job is
read once, that a degraded campaign still succeeds and says what was wrong, and that the
files land where ``generate_data_db``'s glob will pick them up.

``RunLog`` is exercised alongside ``ResourceUsage`` because both now share
:mod:`robovast.results_processing.run_slices`, and nothing else pins that ``RunLog`` still
walks a campaign the way it always did.
"""

import sqlite3

import pytest
import yaml

from robovast.common.execution import JOB_LINKS_MANIFEST, job_artifact_rel
from robovast.results_processing.postprocessing_plugins import (ResourceUsage, RunLog,
                                                                generate_data_db)

_HEADER = "timestamp,pid,name,cpu_percent,memory_rss_bytes\n"

_VAST = {
    "execution": {
        "containers": {"scenario": {"image": "x"}, "sut": {"image": "y"}},
    },
}


def _xml(start_epoch: float, duration: float = 10.0) -> str:
    """A real test.xml carries the run's wall start as a testcase property, not as an
    attribute -- and that property is the only thing that can place a run on the clock."""
    return (f'<testsuite errors="0" failures="0" tests="1">'
            f'<testcase time="{duration}">'
            f'<properties><property name="start_time" value="{start_epoch}"/></properties>'
            f'</testcase></testsuite>')


def _campaign(tmp_path, *, vast=_VAST):
    campaign = tmp_path / "results" / "camp-1"
    (campaign / "_config").mkdir(parents=True)
    (campaign / "_config" / "c.vast").write_text(yaml.safe_dump(vast))
    return campaign


def _job(campaign, index, containers, *, prefix="batch-0", ticks=(100.0, 105.0)):
    """One job's artifacts: a resource CSV per container, plus a container log each."""
    job_dir = campaign / "_jobs" / job_artifact_rel(index, prefix)
    (job_dir / "logs").mkdir(parents=True, exist_ok=True)
    for container, filename in containers.items():
        rows = "".join(f"{t},1,python3,10.0,1000\n{t},2,ros2,5.0,500\n" for t in ticks)
        (job_dir / filename).write_text(_HEADER + rows)
        log = "system.log" if container == "robovast" else f"system_{container}.log"
        (job_dir / "logs" / log).write_text(
            f"[INFO] [{ticks[0]}] [node]: hello from {container}\n")
    return job_dir


def _link(campaign, config, run, index, prefix="batch-0"):
    manifest = campaign / "_transient" / JOB_LINKS_MANIFEST
    manifest.parent.mkdir(parents=True, exist_ok=True)
    links = yaml.safe_load(manifest.read_text()) if manifest.is_file() else {}
    links[f"{config}/{run}/job"] = f"../../_jobs/{job_artifact_rel(index, prefix)}"
    manifest.write_text(yaml.safe_dump(links))


def _run(campaign, config, run, *, start_epoch=None, duration=10.0):
    run_dir = campaign / config / str(run)
    run_dir.mkdir(parents=True, exist_ok=True)
    if start_epoch is not None:
        (run_dir / "test.xml").write_text(_xml(start_epoch, duration))
    return run_dir


def _rows(path):
    import csv
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


@pytest.fixture
def campaign(tmp_path):
    """One config, one run, two containers -- the ordinary shape."""
    camp = _campaign(tmp_path)
    _job(camp, 0, {"robovast": "resource_usage_main.csv", "sut": "resource_usage_sut.csv"})
    _run(camp, "cfg-a", 0, start_epoch=100.0)
    _link(camp, "cfg-a", 0, 0)
    return camp


def test_the_plugin_writes_one_csv_per_run_into_the_run_dir(campaign):
    """Into the RUN dir, because that is the only place generate_data_db's glob looks."""
    ok, message, entries = ResourceUsage()(str(campaign))
    assert ok, message
    rows = _rows(campaign / "cfg-a" / "0" / "resource_usage.csv")
    assert {r["container"] for r in rows} == {"robovast", "sut"}
    assert {r["name"] for r in rows} == {"python3", "ros2"}
    assert entries[0]["plugin"] == "resource_usage"
    assert any("resource_usage_main.csv" in s for s in entries[0]["sources"])


def test_the_manifest_is_read_not_the_job_symlink(campaign):
    """No ``job`` symlink is created anywhere in this fixture. It must still resolve --
    the symlink is only made once a job finishes and cannot exist in an object store."""
    assert not (campaign / "cfg-a" / "0" / "job").exists()
    ok, message, _ = ResourceUsage()(str(campaign))
    assert ok and _rows(campaign / "cfg-a" / "0" / "resource_usage.csv")


def test_a_run_without_a_manifest_entry_is_reported_not_silently_empty(campaign):
    _run(campaign, "cfg-a", 1, start_epoch=100.0)  # no _link() for it
    ok, message, _ = ResourceUsage()(str(campaign))
    assert ok
    assert "no job artifacts for 1 run(s)" in message and "cfg-a/1" in message


def test_a_container_that_recorded_nothing_is_named_and_the_campaign_succeeds(tmp_path):
    """One vanilla sidecar without psutil must not fail a campaign -- but must be visible."""
    camp = _campaign(tmp_path)
    _job(camp, 0, {"robovast": "resource_usage_main.csv"})  # sut wrote nothing
    _run(camp, "cfg-a", 0, start_epoch=100.0)
    _link(camp, "cfg-a", 0, 0)
    ok, message, _ = ResourceUsage()(str(camp))
    assert ok
    assert "no CSV for 1 container(s)" in message and "sut" in message


def test_a_campaign_with_no_resource_data_at_all_leads_with_that(tmp_path):
    camp = _campaign(tmp_path)
    (camp / "_jobs" / job_artifact_rel(0, "batch-0")).mkdir(parents=True)
    _run(camp, "cfg-a", 0, start_epoch=100.0)
    _link(camp, "cfg-a", 0, 0)
    ok, message, _ = ResourceUsage()(str(camp))
    assert ok
    assert message.startswith("resource_usage: NO resource data")


def test_a_packed_job_is_read_once_and_split_between_its_runs(tmp_path, monkeypatch):
    """Two runs share one job. Its CSVs are parsed once, and each tick lands in exactly
    one run -- so SUM over the two runs is what the job actually consumed."""
    camp = _campaign(tmp_path)
    _job(camp, 0, {"robovast": "resource_usage_main.csv", "sut": "resource_usage_sut.csv"},
         ticks=(100.0, 105.0, 115.0, 125.0))
    _run(camp, "cfg-a", 0, start_epoch=100.0, duration=10.0)   # ends at 110
    _run(camp, "cfg-a", 1, start_epoch=115.0, duration=10.0)   # ends at 125
    _link(camp, "cfg-a", 0, 0)
    _link(camp, "cfg-a", 1, 0)

    from robovast.results_processing import resource_usage as module
    reads = []
    real = module.read_container_csv
    monkeypatch.setattr(module, "read_container_csv",
                        lambda *a, **k: (reads.append(a[0]), real(*a, **k))[1])

    ok, message, _ = ResourceUsage()(str(camp))
    assert ok, message
    assert len(reads) == 2, "the job's two CSVs must be parsed once, not once per run"

    first = {r["wall_ts"] for r in _rows(camp / "cfg-a" / "0" / "resource_usage.csv")}
    second = {r["wall_ts"] for r in _rows(camp / "cfg-a" / "1" / "resource_usage.csv")}
    assert not (first & second), "a tick was counted in both runs"
    assert len(first | second) == 4, "a tick was dropped"


def test_the_table_lands_in_data_db_with_numbers_as_numbers(campaign, tmp_path):
    """The whole point: it is queryable, joined on (config_name, run_id), and its numeric
    columns compare numerically rather than lexicographically."""
    RunLog()(str(campaign))
    ResourceUsage()(str(campaign))
    ok, message = generate_data_db(str(campaign))[:2]
    assert ok, message

    db = sqlite3.connect(campaign / "_execution" / "data.db")
    columns = {r[1]: r[2] for r in db.execute("PRAGMA table_info(resource_usage)")}
    assert columns["cpu_percent"] == "REAL"
    assert columns["memory_rss_bytes"] == "INTEGER"
    assert {"config_name", "run_id"} <= set(columns)

    # The two derived tables must agree on what to call a container: they are joined on it.
    used = {r[0] for r in db.execute("SELECT DISTINCT container FROM resource_usage")}
    logged = {r[0] for r in db.execute(
        "SELECT DISTINCT container FROM run_log WHERE container != ''")}
    assert logged <= used


def test_run_log_still_walks_a_campaign_the_way_it_did(campaign):
    """Pins the run_slices refactor: RunLog's own output is unchanged."""
    ok, message = RunLog()(str(campaign))
    assert ok, message
    rows = _rows(campaign / "cfg-a" / "0" / "run_log.csv")
    assert rows and {r["container"] for r in rows} == {"robovast", "sut"}
