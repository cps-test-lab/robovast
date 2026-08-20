# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The intervention ledger: what a human did to a campaign while it ran.

``_execution/interventions.json`` records both kinds -- a job an operator stopped by hand (see
``RobovastInterface.stop_job``) and a run somebody read into while it was going. One file because
"what was done to this run?" is one question, and :func:`intervened_runs` answers it in one call;
:func:`killed_runs` and :func:`probed_runs` are the kind-filtered views their callers want,
because the *consequence* differs even though the resolution does not. :func:`read_run_outcome`
turns a kill into ``status == "killed"``; a probe leaves the verdict alone.

The three properties that define the feature, one test each:

* a run of a killed job that delivered nothing is ``killed`` rather than ``unknown``;
* a run of a killed job that wrote a valid ``test.xml`` **keeps its real verdict** — it
  finished before the kill landed, and that verdict is measurement, not a casualty;
* with no ledger, outcomes are byte-identical to what they were before it existed.
"""

from pathlib import Path

import pytest
import yaml

from robovast.common.campaign_data import (KIND_KILLED, killed_runs, read_interventions,
                                           read_run_outcome, read_run_outcomes,
                                           record_intervention)
from robovast.common.execution import JOB_LINKS_MANIFEST, job_artifact_rel

_PASS_XML = ('<testsuite errors="0" failures="0" tests="1">'
             '<testcase time="1.5"/></testsuite>')
_FAIL_XML = ('<testsuite errors="0" failures="1" tests="1">'
             '<testcase time="2.0"><failure message="nav aborted"/></testcase></testsuite>')


def _run(campaign: Path, config: str, run: str, *, xml=None,
         job_index=0, job_prefix="batch-0") -> Path:
    """One run dir, linked to its job the way a real campaign links it.

    The link goes in the manifest and NOT as a ``job`` symlink: the symlink is only
    created once a job finishes, which is exactly what a killed job never does. Resolution
    has to work from the manifest alone or it cannot work at all here.
    """
    run_dir = campaign / config / run
    run_dir.mkdir(parents=True, exist_ok=True)
    if xml is not None:
        (run_dir / "test.xml").write_text(xml)
    manifest = campaign / "_transient" / JOB_LINKS_MANIFEST
    manifest.parent.mkdir(parents=True, exist_ok=True)
    links = yaml.safe_load(manifest.read_text()) if manifest.is_file() else {}
    links[f"{config}/{run}/job"] = f"../../_jobs/{job_artifact_rel(job_index, job_prefix)}"
    manifest.write_text(yaml.safe_dump(links))
    return run_dir


@pytest.fixture
def campaign(tmp_path):
    return tmp_path / "campaign-2026-08-13-120000"


def test_a_resultless_run_of_a_killed_job_is_killed_not_unknown(campaign):
    _run(campaign, "cfgA", "0")  # no test.xml: the kill cut it short
    record_intervention(campaign, kind=KIND_KILLED, job_dir="_jobs/batch-0/job-0", job_name="cfgA/0",
                      source="webui", detail="stuck in nav recovery")

    outcome = read_run_outcome(campaign / "cfgA" / "0", campaign)
    assert outcome["status"] == "killed"
    assert outcome["passed"] == 0
    # The reason travels with the run, so the kill is still explained months later.
    assert outcome["failure_message"] == "manually stopped via webui: stuck in nav recovery"


def test_a_finished_run_of_a_killed_job_keeps_its_real_verdict(campaign):
    """The packed-job case: a kill must never overwrite measurement that exists.

    With ``runs_per_job > 1`` a job's earlier runs routinely complete before anyone stops
    it. Marking those ``killed`` would delete real results — which is why ``killed``
    replaces ``unknown`` and only ``unknown``.
    """
    _run(campaign, "cfgA", "0", xml=_PASS_XML, job_index=0)
    _run(campaign, "cfgA", "1", xml=_FAIL_XML, job_index=0)
    _run(campaign, "cfgA", "2", job_index=0)  # the one actually in flight
    record_intervention(campaign, kind=KIND_KILLED, job_dir="_jobs/batch-0/job-0",
                      job_name="batch-0-job-0", source="mcp", detail="wedged")

    statuses = {o["run_id"]: o["status"] for o in read_run_outcomes(campaign / "cfgA",
                                                                   campaign)}
    assert statuses == {0: "passed", 1: "failed", 2: "killed"}


def test_no_ledger_leaves_outcomes_exactly_as_they_were(campaign):
    """The default path: a campaign nobody intervened in must be unaffected."""
    _run(campaign, "cfgA", "0", xml=_PASS_XML)
    _run(campaign, "cfgA", "1", xml=_FAIL_XML)
    _run(campaign, "cfgA", "2")  # genuinely lost its result, nobody killed it

    assert read_interventions(campaign) == []
    assert killed_runs(campaign) == {}
    outcomes = read_run_outcomes(campaign / "cfgA", campaign)
    assert [o["status"] for o in outcomes] == ["passed", "failed", "unknown"]
    # `unknown` keeps carrying no message: only a kill has something to say.
    assert outcomes[2]["failure_message"] is None


def test_only_the_killed_jobs_runs_are_affected(campaign):
    """A sibling job's runs are untouched — the kill is scoped to one job."""
    _run(campaign, "cfgA", "0", job_index=0)
    _run(campaign, "cfgB", "0", job_index=1)
    record_intervention(campaign, kind=KIND_KILLED, job_dir="_jobs/batch-0/job-0", job_name="cfgA/0",
                      source="cli", detail=None)

    assert sorted(killed_runs(campaign)) == ["cfgA/0"]
    assert read_run_outcome(campaign / "cfgA" / "0", campaign)["status"] == "killed"
    assert read_run_outcome(campaign / "cfgB" / "0", campaign)["status"] == "unknown"


def test_a_kill_with_no_reason_still_names_the_surface(campaign):
    _run(campaign, "cfgA", "0")
    record_intervention(campaign, kind=KIND_KILLED, job_dir="_jobs/batch-0/job-0", job_name="cfgA/0",
                      source="cli", detail=None)

    outcome = read_run_outcome(campaign / "cfgA" / "0", campaign)
    assert outcome["failure_message"] == "manually stopped via cli"


def test_the_local_lanes_run_hint_resolves_without_a_manifest(campaign):
    """The local lane records the run key itself, so resolution survives a missing manifest.

    The manifest is written before the first job starts, but a kill in that startup window
    would otherwise have nothing to resolve through — and the local lane's ``job_name``
    already *is* the run key, so it passes it as a hint.
    """
    (campaign / "cfgA" / "0").mkdir(parents=True)
    record_intervention(campaign, kind=KIND_KILLED, job_dir="", job_name="cfgA/0", source="webui",
                      detail="never started properly", runs=("cfgA/0",))

    assert sorted(killed_runs(campaign)) == ["cfgA/0"]
    assert read_run_outcome(campaign / "cfgA" / "0", campaign)["status"] == "killed"


def test_several_kills_accumulate(campaign):
    """Each stop is its own event with its own reason; the ledger appends."""
    _run(campaign, "cfgA", "0", job_index=0)
    _run(campaign, "cfgB", "0", job_index=1)
    record_intervention(campaign, kind=KIND_KILLED, job_dir="_jobs/batch-0/job-0", job_name="cfgA/0",
                      source="webui", detail="first")
    record_intervention(campaign, kind=KIND_KILLED, job_dir="_jobs/batch-0/job-1", job_name="cfgB/0",
                      source="mcp", detail="second")

    assert len(read_interventions(campaign)) == 2
    resolved = killed_runs(campaign)
    assert resolved["cfgA/0"]["detail"] == "first"
    assert resolved["cfgB/0"]["detail"] == "second"


def test_a_corrupt_ledger_does_not_take_the_results_down(campaign):
    """A truncated ledger costs the annotation, not the run data."""
    _run(campaign, "cfgA", "0", xml=_PASS_XML)
    exec_dir = campaign / "_execution"
    exec_dir.mkdir(parents=True, exist_ok=True)
    (exec_dir / "killed_jobs.json").write_text('[{"job_dir": "_jobs/bat')

    assert read_interventions(campaign) == []
    assert read_run_outcome(campaign / "cfgA" / "0", campaign)["status"] == "passed"


def test_killed_runs_needs_no_manifest_read_when_nothing_was_killed(campaign, monkeypatch):
    """The default path must not pay for a feature nobody used.

    Guards the short-circuit: with no ledger, ``killed_runs`` returns before it would read
    the job-link manifest, so the common case costs one missing-file check.
    """
    _run(campaign, "cfgA", "0", xml=_PASS_XML)

    def _boom(*_a, **_k):
        raise AssertionError("read_job_links must not be called without a ledger")

    monkeypatch.setattr("robovast.common.execution.read_job_links", _boom)
    assert killed_runs(campaign) == {}


# -- the killed status, downstream: the store, the counts, and run_view -------------------


def test_a_killed_run_is_counted_apart_from_the_failures(campaign):
    """``num_killed`` exists so a human intervention never lands in ``num_failed``.

    A kill says nothing about the system under test; folding it into the failures would put
    the operator's decision into the campaign's measured outcome.
    """
    from robovast.common.store import STORE_FILENAME, CampaignStore, read_run_counts

    _run(campaign, "cfgA", "0", xml=_PASS_XML, job_index=0)
    _run(campaign, "cfgA", "1", xml=_FAIL_XML, job_index=1)
    _run(campaign, "cfgA", "2", job_index=2)  # killed
    _run(campaign, "cfgA", "3", job_index=3)  # genuinely lost its result
    record_intervention(campaign, kind=KIND_KILLED, job_dir="_jobs/batch-0/job-2", job_name="cfgA/2",
                      source="webui", detail="wedged")

    with CampaignStore(campaign / STORE_FILENAME) as store:
        cid = store.create_campaign("c", {}, mode="batch")
        bid = store.open_batch(cid, 0, ".")
        unit = store.record_unit(batch_id=bid, paramset_id="cfgA", config_name="cfgA",
                                 params={}, objectives={}, measures={},
                                 status="evaluated", result_dir="cfgA")
        store.record_runs(unit, read_run_outcomes(campaign / "cfgA", campaign))
        counts = store.run_counts(cid)

    assert counts["num_runs"] == 4
    assert counts["num_passed"] == 1
    assert counts["num_failed"] == 1, "the kill must not be counted as a failure"
    assert counts["num_killed"] == 1
    # Read back from disk by the same contract (the path status recovery uses).
    assert read_run_counts(campaign)["num_killed"] == 1


def test_run_view_exposes_the_kill_and_its_reason(campaign):
    """SQL is how results are read, so the kill has to be filterable and explained there."""
    from robovast.common.store import STORE_FILENAME, CampaignStore
    from robovast.results_processing.data_query import query_data_db

    _run(campaign, "cfgA", "0", xml=_PASS_XML, job_index=0)
    _run(campaign, "cfgA", "1", job_index=1)
    record_intervention(campaign, kind=KIND_KILLED, job_dir="_jobs/batch-0/job-1", job_name="cfgA/1",
                      source="mcp", detail="never converged")

    with CampaignStore(campaign / STORE_FILENAME) as store:
        cid = store.create_campaign("c", {}, mode="batch")
        bid = store.open_batch(cid, 0, ".")
        unit = store.record_unit(batch_id=bid, paramset_id="cfgA", config_name="cfgA",
                                 params={}, objectives={}, measures={},
                                 status="evaluated", result_dir="cfgA")
        store.record_runs(unit, read_run_outcomes(campaign / "cfgA", campaign))

    rows = query_data_db(campaign,
                         "SELECT run_id, status, failure_message FROM run_view "
                         "WHERE status = 'killed'")["rows"]
    assert len(rows) == 1
    assert rows[0]["run_id"] == 1
    assert rows[0]["failure_message"] == "manually stopped via mcp: never converged"


# -- probes: the same ledger, a different consequence ---------------------------------------------


def test_a_probe_and_a_kill_share_one_ledger_but_not_one_meaning(campaign):
    """One file answers "what was done to this run?" in one read. What follows differs by kind and
    the callers act on that: a kill becomes a status, a probe leaves the verdict alone."""
    from robovast.common.campaign_data import (KIND_PROBED, intervened_runs, probed_runs)
    _run(campaign, "cfgA", "0", xml=_PASS_XML, job_index=0)
    _run(campaign, "cfgB", "0", job_index=1)
    record_intervention(campaign, kind=KIND_KILLED, job_dir="_jobs/batch-0/job-1",
                        job_name="cfgB/0", source="webui", detail="wedged",
                        runs=("cfgB/0",))
    record_intervention(campaign, kind=KIND_PROBED, job_dir="_jobs/batch-0/job-0",
                        job_name="cfgA/0", source="mcp", detail="ros2 node list",
                        runs=("cfgA/0",))

    assert sorted(intervened_runs(campaign)) == ["cfgA/0", "cfgB/0"]
    assert sorted(killed_runs(campaign)) == ["cfgB/0"]
    assert sorted(probed_runs(campaign)) == ["cfgA/0"]
    # The probed run keeps the verdict it reached: an intervention is not an outcome.
    assert read_run_outcome(campaign / "cfgA" / "0", campaign)["status"] == "passed"


def test_a_probed_run_is_flagged_in_data_db_without_changing_its_status(campaign, tmp_path):
    """`probed` is a separate column for the reason `killed` is kept out of num_failed: putting a
    human's action into the measured outcome makes the result unreadable."""
    import sqlite3

    from robovast.common.campaign_data import KIND_PROBED
    from robovast.results_processing.postprocessing_plugins import _build_runs_table
    _run(campaign, "cfgA", "0", xml=_PASS_XML, job_index=0)
    _run(campaign, "cfgA", "1", xml=_PASS_XML, job_index=1)
    record_intervention(campaign, kind=KIND_PROBED, job_dir="_jobs/batch-0/job-0",
                        job_name="cfgA/0", source="mcp", detail="uptime",
                        runs=("cfgA/0",))

    conn = sqlite3.connect(tmp_path / "data.db")
    _build_runs_table(conn, campaign, [campaign / "cfgA"])
    rows = dict(conn.execute("SELECT run_id, probed FROM runs").fetchall())
    statuses = dict(conn.execute("SELECT run_id, status FROM runs").fetchall())
    conn.close()

    assert rows == {0: 1, 1: 0}
    assert statuses == {0: "passed", 1: "passed"}, "a probe must not touch the verdict"


def test_a_campaign_nobody_touched_has_no_ledger_at_all(campaign):
    """The default path, and the one worth pinning: no file, no manifest read, and every run
    reported exactly as before this record existed."""
    _run(campaign, "cfgA", "0", xml=_PASS_XML)
    assert not (campaign / "_execution" / "interventions.json").exists()
    assert read_interventions(campaign) == []
    assert killed_runs(campaign) == {}
