# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The job-artifact plugins driven over a whole campaign tree, as postprocessing runs them.

The unit tests cover the parsing and the slicing. What only shows up here is the wiring:
that a run finds its job through the **manifest** and not the ``job`` symlink (the property
that makes this work on the cluster, where a symlink cannot exist), that a packed job is
read once, that a degraded campaign still succeeds and says what was wrong, and that the
files land where the index ingest's glob will pick them up.

``RunLog`` is exercised alongside ``ResourceUsage`` because both now share
:mod:`robovast.results_processing.run_slices`, and nothing else pins that ``RunLog`` still
walks a campaign the way it always did.
"""

import pytest
import yaml

from robovast.common.execution import JOB_LINKS_MANIFEST, job_artifact_rel
from robovast.results_processing.campaign_ingest import _scenario_verdict
from robovast.results_processing.csv_types import INTEGER, REAL, infer_column_types
from robovast.results_processing.postprocessing_plugins import ResourceUsage, RunLog

_HEADER = "timestamp,pid,name,cpu_percent,memory_rss_bytes\n"

_VAST = {
    "execution": {
        "containers": {"scenario": {"image": "x"}, "sut": {"image": "y"}},
    },
}


def _xml(start_epoch: float, duration: float = 10.0, failures: int = 0) -> str:
    """A real test.xml carries the run's wall start as a testcase property, not as an
    attribute -- and that property is the only thing that can place a run on the clock."""
    return (f'<testsuite errors="0" failures="{failures}" tests="1">'
            f'<testcase time="{duration}">'
            f'<properties><property name="start_time" value="{start_epoch}"/></properties>'
            f'</testcase></testsuite>')


# _VAST is a read-only constant
def _campaign(tmp_path, *, vast=_VAST):  # pylint: disable=dangerous-default-value
    campaign = tmp_path / "results" / "camp-1"
    (campaign / "_config").mkdir(parents=True)
    (campaign / "_config" / "c.vast").write_text(yaml.safe_dump(vast))
    return campaign


def _job(campaign, index, containers, *, prefix="batch-0", ticks=(100.0, 105.0),
         log_lines=None):
    """One job's artifacts: a resource CSV per container, plus a container log each.

    *log_lines* replaces the ``robovast`` container's log body, for the tests that care what
    a packed job's shared log said rather than merely that it was read. It is the real
    on-disk grammar (``[LEVEL] [epoch] [node]: message``), so the parser is exercised too.
    """
    job_dir = campaign / "_jobs" / job_artifact_rel(index, prefix)
    (job_dir / "logs").mkdir(parents=True, exist_ok=True)
    for container, filename in containers.items():
        rows = "".join(f"{t},1,python3,10.0,1000\n{t},2,ros2,5.0,500\n" for t in ticks)
        (job_dir / filename).write_text(_HEADER + rows)
        log = "system.log" if container == "robovast" else f"system_{container}.log"
        body = (f"[INFO] [{ticks[0]}] [node]: hello from {container}\n"
                if log_lines is None or container != "robovast" else log_lines)
        (job_dir / "logs" / log).write_text(body)
    return job_dir


#: A packed job's shared log: two configurations run one after the other in one container,
#: the first tipping over and the second landing. `_SCEN` is scenario_execution's own logger,
#: which is what `scenario_markers` requires before it will read a line as a verdict.
_SCEN = "scenario_execution_ros"
#
# Both timings around a run's boundary are the MEASURED ones, and both have the awkward sign:
#
#  * the ``Executing scenario`` line comes ~35 us BEFORE the run's ``test.xml`` start (it is
#    logged, then the start is recorded), so a boundary on the start leaves each run's own
#    marker in its predecessor's share.
#  * a failing run's verdict lands ~1 ms AFTER its window closes, so ``in_window`` cannot be
#    the trial boundary.
#
# An earlier version of this fixture put the marker *after* the start and so proved nothing.
_PACKED_LOG = (
    f"[INFO] [99.99996] [{_SCEN}]: Executing scenario 'test_scenario-0'\n"
    f"[INFO] [104.900] [{_SCEN}]: Outcome: tip_over | max_tilt=0.70\n"
    f"[ERROR] [104.950] [{_SCEN}]: FAILURE: tip_over\n"
    f"[ERROR] [105.0011] [{_SCEN}]: test_scenario-0: execution failed. -^- [x]\n"
    f"[INFO] [105.060] [{_SCEN}]: Shutting down finished.\n"
    f"[INFO] [109.99997] [{_SCEN}]: Executing scenario 'test_scenario-1'\n"
    f"[INFO] [114.900] [{_SCEN}]: Outcome: landed | max_tilt=0.03\n"
    f"[INFO] [114.950] [{_SCEN}]: Scenario 'test_scenario-1' succeeded.\n"
)


def _link(campaign, config, run, index, prefix="batch-0"):
    manifest = campaign / "_transient" / JOB_LINKS_MANIFEST
    manifest.parent.mkdir(parents=True, exist_ok=True)
    links = yaml.safe_load(manifest.read_text()) if manifest.is_file() else {}
    links[f"{config}/{run}/job"] = f"../../_jobs/{job_artifact_rel(index, prefix)}"
    manifest.write_text(yaml.safe_dump(links))


def _run(campaign, config, run, *, start_epoch=None, duration=10.0, failures=0):
    run_dir = campaign / config / str(run)
    run_dir.mkdir(parents=True, exist_ok=True)
    if start_epoch is not None:
        (run_dir / "test.xml").write_text(_xml(start_epoch, duration, failures))
    return run_dir


def _junit_passed(path) -> bool:
    """The run's own verdict, as the runner recorded it in JUnit."""
    import xml.etree.ElementTree as ET
    suite = ET.parse(path).getroot()
    return int(suite.get("failures", 0)) == 0 and int(suite.get("errors", 0)) == 0


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
    """Into the RUN dir, because that is the only place the ingest's glob looks."""
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
    ok, _message, _ = ResourceUsage()(str(campaign))
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


def test_the_table_the_ingest_will_build_has_numbers_as_numbers(campaign):
    """The whole point: what the ingest reads from these files is queryable, scoped to
    (config_name, run_id), and its numeric columns compare numerically rather than
    lexicographically."""
    RunLog()(str(campaign))
    ResourceUsage()(str(campaign))

    usage = _rows(campaign / "cfg-a" / "0" / "resource_usage.csv")
    types = infer_column_types(usage, usage[0].keys())
    assert types["cpu_percent"] == REAL
    assert types["memory_rss_bytes"] == INTEGER

    # The two derived tables must agree on what to call a container: they are joined on it.
    used = {r["container"] for r in usage}
    logged = {r["container"] for r in _rows(campaign / "cfg-a" / "0" / "run_log.csv")
              if r["container"]}
    assert logged <= used


def test_run_log_still_walks_a_campaign_the_way_it_did(campaign):
    """Pins the run_slices refactor: RunLog's own output is unchanged."""
    ok, message = RunLog()(str(campaign))
    assert ok, message
    rows = _rows(campaign / "cfg-a" / "0" / "run_log.csv")
    assert rows and {r["container"] for r in rows} == {"robovast", "sut"}


def _packed_log_campaign(tmp_path, **run1):
    """Two configurations packed into one job, sharing one log: run 0 tips over, run 1 lands.

    The windows straddle the two scenarios -- run 0 is [100, 105], run 1 is [110, 115] -- so
    each run's claim covers exactly one of them.
    """
    camp = _campaign(tmp_path)
    _job(camp, 0, {"robovast": "resource_usage_main.csv"}, log_lines=_PACKED_LOG)
    _run(camp, "cfg-a", 0, start_epoch=100.0, duration=5.0, failures=1)
    _run(camp, "cfg-a", 1, start_epoch=110.0, duration=5.0, **run1)
    _link(camp, "cfg-a", 0, 0)
    _link(camp, "cfg-a", 1, 0)
    return camp


def test_a_packed_jobs_log_is_split_so_no_run_inherits_a_siblings_verdict(tmp_path):
    """The bug this partition exists for.

    Two DIFFERENT configurations share one container, so the job's log holds both trials.
    Giving every run all of those lines gave every run the FIRST scenario's verdict: run 1
    passed its own trial and still reported ``failed``, quoting run 0's ``FAILURE: tip_over``
    -- while ``runs.passed`` said it passed. Each run must see only its own scenario.
    """
    camp = _packed_log_campaign(tmp_path)
    ok, message = RunLog()(str(camp))
    assert ok, message

    first = [r["message"] for r in _rows(camp / "cfg-a" / "0" / "run_log.csv")]
    second = [r["message"] for r in _rows(camp / "cfg-a" / "1" / "run_log.csv")]
    assert any("test_scenario-0" in m for m in first)
    assert not any("test_scenario-1" in m for m in first), "run 0 sees a later run's trial"
    assert any("test_scenario-1" in m for m in second)
    assert not any("test_scenario-0" in m for m in second), "run 1 sees run 0's trial"
    assert not any("tip_over" in m for m in second), "the reported symptom, exactly"

    # What the ingest derives from each run's own slice of that log -- the same function it
    # calls, so this pins the verdict a query would come back with, not a re-match here.
    verdicts = {run: _scenario_verdict(_rows(camp / "cfg-a" / str(run) / "run_log.csv"))
                for run in (0, 1)}
    assert {run: v.get("status") for run, v in verdicts.items()} == {0: "failed",
                                                                    1: "succeeded"}
    # The user-visible invariant: the two sources of "did this run work" must not disagree.
    # The other source is the run's own test.xml, which the campaign record is written from
    # and which the index mirrors -- read here rather than restated, so the two really are
    # independent.
    passed = {run: _junit_passed(camp / "cfg-a" / str(run) / "test.xml") for run in (0, 1)}
    assert passed == {0: False, 1: True}
    assert [verdicts[i]["status"] == "succeeded" for i in (0, 1)] == \
        [passed[i] for i in (0, 1)]


def test_each_run_owns_the_scenario_start_line_stamped_just_before_its_own_start(tmp_path):
    """``Executing scenario`` is logged ~35 us BEFORE ``test.xml``'s start, so a boundary on
    the start files every run's own marker with its predecessor. Found on a real campaign:
    the first run of each job held two scenario-start lines and the last held none."""
    camp = _packed_log_campaign(tmp_path)
    ok, message = RunLog()(str(camp))
    assert ok, message

    def starts(run):
        return [r["message"] for r in _rows(camp / "cfg-a" / str(run) / "run_log.csv")
                if r["message"].startswith("Executing scenario")]

    assert starts(0) == ["Executing scenario 'test_scenario-0'"]
    assert starts(1) == ["Executing scenario 'test_scenario-1'"]


def test_a_runs_teardown_stays_with_it_and_does_not_slide_into_its_successor(tmp_path):
    """The gap between two runs holds the earlier one's verdict and shutdown, then the later
    one's scenario start. Splitting it anywhere but at that marker -- a midpoint, say -- hands
    ``Shutting down finished.`` to the wrong run; measured, it sits far closer to the next
    run's start than to its own run's window."""
    camp = _packed_log_campaign(tmp_path)
    ok, message = RunLog()(str(camp))
    assert ok, message
    first = [r["message"] for r in _rows(camp / "cfg-a" / "0" / "run_log.csv")]
    second = [r["message"] for r in _rows(camp / "cfg-a" / "1" / "run_log.csv")]
    assert "Shutting down finished." in first
    assert "Shutting down finished." not in second


def test_a_failing_runs_verdict_lands_past_its_window_and_is_still_its_own(tmp_path):
    """``in_window`` cannot be the verdict boundary, which is why the CLAIM is the filter.

    Run 0's window closes at 105.0 and its ``execution failed.`` line is stamped 105.0011 --
    1.1 ms late, as measured on the real campaign. It is outside the window but inside the
    claim, so it stays with run 0 and is flagged ``in_window=0``.
    """
    camp = _packed_log_campaign(tmp_path)
    ok, message = RunLog()(str(camp))
    assert ok, message
    verdict = [r for r in _rows(camp / "cfg-a" / "0" / "run_log.csv")
               if "execution failed." in r["message"]]
    assert len(verdict) == 1, "the verdict left run 0 when its window closed"
    assert verdict[0]["in_window"] == "0"


def test_an_unstamped_line_in_a_packed_job_lands_in_exactly_one_run(tmp_path):
    """A record with no ``[epoch]`` prefix cannot be placed, and ``claims()`` answers
    "unknown is inside" -- so without an explicit rule it is copied into EVERY run of the job.
    It goes to the job's first run, which is where the merge's ordering already puts it.

    The line goes at the HEAD of the log on purpose: that is the only place a standalone
    unstamped record is produced (the entrypoint's bash output before the first ROS stamp).
    One that follows a stamped line is folded into it as a continuation and travels with its
    parent's ``wall_ts``, so it was never at risk.
    """
    camp = _campaign(tmp_path)
    _job(camp, 0, {"robovast": "resource_usage_main.csv"},
         log_lines="a warning from a third party with no stamp\n" + _PACKED_LOG)
    _run(camp, "cfg-a", 0, start_epoch=100.0, duration=5.0)
    _run(camp, "cfg-a", 1, start_epoch=110.0, duration=5.0)
    _link(camp, "cfg-a", 0, 0)
    _link(camp, "cfg-a", 1, 0)
    ok, message = RunLog()(str(camp))
    assert ok, message

    def unstamped(run):
        return [r for r in _rows(camp / "cfg-a" / str(run) / "run_log.csv")
                if "third party" in r["message"]]

    assert len(unstamped(0)) == 1, "the job's first run must keep it"
    assert not unstamped(1), "it was duplicated into a second run"


def test_an_unplaceable_run_of_a_packed_job_gets_no_log_rather_than_a_siblings(tmp_path):
    """A run killed before writing ``test.xml`` claims nothing, so its log is empty.

    The alternative -- hand it the whole job's log -- is what gave it another run's verdict.
    ``get_job_log`` still shows the container's whole log, and the run is named in the
    plugin's message rather than failing silently.
    """
    camp = _packed_log_campaign(tmp_path)
    _run(camp, "cfg-a", 2)  # no test.xml, so no window and no claim
    _link(camp, "cfg-a", 2, 0)
    ok, message = RunLog()(str(camp))
    assert ok, message
    assert _rows(camp / "cfg-a" / "2" / "run_log.csv") == []
    assert "cfg-a/2" in message
