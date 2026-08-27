# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Per-campaign job counts for the CLI monitor, over the shared phase classifier.

This function had no test at all, and it shows: it unpacked
``list_jobs_with_phase``'s three-tuples as two, so *every* call raised
``ValueError``. The classifier is the one place that decides a job's phase, and
reading its vocabulary as this function's schema is the same mistake twice --
each phase added there (``blocked``, then ``waiting``) also became a KeyError on
the increment.

Neither showed up as a crash. The monitor wraps the call in ``except Exception``
and prints "(unreachable)", which is what a healthy cluster had been reporting.
So the tests here are about a count that must be *right*, not merely returned.
"""

import types
from unittest import mock

from robovast.execution.cluster_execution.cluster_execution import (
    JOB_PHASE_COUNTERS, get_cluster_job_counts_per_campaign)


def _job(name, campaign, *, succeeded=0, active=0, failed=0, suspend=False, total=None):
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(
            name=name, labels={"campaign-id": campaign} if campaign else {},
            annotations={"total-job-num": str(total)} if total else {}),
        spec=types.SimpleNamespace(suspend=suspend),
        status=types.SimpleNamespace(succeeded=succeeded, active=active, failed=failed))


def _counts(classified):
    """Run the function with *classified* standing in for the phase classifier."""
    with mock.patch("robovast.execution.cluster_execution.cluster_execution"
                    ".list_jobs_with_phase", return_value=classified), \
         mock.patch("robovast.execution.cluster_execution.kube_client.load_kube_config"), \
         mock.patch("kubernetes.client.BatchV1Api"), \
         mock.patch("kubernetes.client.CoreV1Api"):
        return get_cluster_job_counts_per_campaign("ns")


def test_the_classifier_s_three_tuples_are_unpacked():
    """The reported bug: two names for three values, on every call."""
    counts = _counts([(_job("j1", "camp-a", succeeded=1), "completed", None)])
    assert counts["camp-a"]["completed"] == 1


def test_a_blocked_job_is_counted_not_a_key_error():
    """``blocked`` carries a detail, so it is also the phase most likely to be seen here
    -- and it was the first phase the classifier grew after this function was written."""
    counts = _counts([(_job("j1", "camp-a", active=1), "blocked",
                       "ImagePullBackOff: no basic auth credentials")])
    assert counts["camp-a"]["blocked"] == 1
    assert counts["camp-a"]["pending"] == 0


def test_a_kueue_suspended_job_is_counted_too():
    counts = _counts([(_job("j1", "camp-a", suspend=True), "waiting",
                       "waiting for Kueue admission")])
    assert counts["camp-a"]["waiting"] == 1


def test_every_phase_the_classifier_can_report_has_a_counter():
    """The guard against a third round of this: a phase with no counter is a crash, and
    the classifier is free to grow one without looking here."""
    jobs = [(_job(f"j{i}", "camp-a"), phase, None)
            for i, phase in enumerate(JOB_PHASE_COUNTERS)]
    counts = _counts(jobs)
    for phase in JOB_PHASE_COUNTERS:
        assert counts["camp-a"][phase] == 1, phase


def test_an_unknown_phase_is_counted_unfinished_and_said_out_loud(caplog):
    """If it happens anyway, the safe direction is "not finished" -- counting it as
    nothing would let the monitor announce a batch complete while jobs remain."""
    with caplog.at_level("WARNING"):
        counts = _counts([(_job("j1", "camp-a", active=1), "quiesced", None)])
    assert counts["camp-a"]["pending"] == 1
    assert "quiesced" in caplog.text


def test_counts_are_split_per_campaign_and_unlabelled_jobs_are_legacy():
    counts = _counts([
        (_job("j1", "camp-a", succeeded=1), "completed", None),
        (_job("j2", "camp-b", failed=1), "failed", None),
        (_job("j3", None, active=1), "running", None),
    ])
    assert counts["camp-a"]["completed"] == 1
    assert counts["camp-b"]["failed"] == 1
    assert counts["<legacy>"]["running"] == 1


def test_the_total_job_num_annotation_is_picked_up_once():
    counts = _counts([
        (_job("j1", "camp-a", succeeded=1, total=20), "completed", None),
        (_job("j2", "camp-a", active=1), "running", None),
    ])
    assert counts["camp-a"]["total_job_num"] == 20


# ---------------------------------------------------------------------------
# what the monitor does with those counts
# ---------------------------------------------------------------------------

def _monitor(per_run):
    """Run ``vast cluster monitor --once`` over one context and one count set."""
    from click.testing import CliRunner

    from robovast.execution.cluster_execution import cli as cluster_cli
    with mock.patch("robovast.execution.cluster_execution.cluster_execution"
                    ".get_cluster_job_counts_per_campaign", return_value=per_run), \
         mock.patch.object(cluster_cli, "_monitor_via_service", return_value=False):
        return CliRunner().invoke(cluster_cli.monitor, ["--once", "--context", "ctx1"])


def _full(**over):
    counts = dict.fromkeys(JOB_PHASE_COUNTERS, 0)
    counts["total_job_num"] = None
    counts.update(over)
    return {"camp-a": counts}


def test_the_monitor_does_not_call_a_blocked_batch_finished():
    """The count this exists for. A blocked job is submitted and unfinished, so it has to
    reach both the batch size and the still-in-cluster tally: leaving it out of either
    reports 3/3 at 100%% and (in the polling loop, which --once does not reach) announces
    "All jobs finished." for a campaign with a job that never started.
    """
    result = _monitor(_full(completed=3, blocked=1))
    assert result.exit_code == 0
    assert "3/4" in result.output          # 4 submitted, 3 finished -- not 3/3
    assert "100.0%" not in result.output
    assert "Blocked: 1" in result.output


def test_a_queued_batch_is_not_finished_either():
    """Kueue-suspended jobs have no pod at all, so they are invisible to the pod probe
    and only this count keeps them on the books."""
    result = _monitor(_full(waiting=4))
    assert result.exit_code == 0
    assert "0/4" in result.output          # not 0/0, which reads as nothing to do
    assert "Waiting: 4" in result.output


def test_a_genuinely_finished_batch_still_reads_as_complete():
    """The other direction: counting blocked/waiting as unfinished must not keep the
    monitor watching a batch that is actually over."""
    result = _monitor(_full(completed=4))
    assert result.exit_code == 0
    assert "4/4" in result.output
    assert "100.0%" in result.output


def test_zero_counts_are_not_printed():
    """Waiting is every cluster batch's normal first state; a permanent "Waiting: 0" is
    how a reader learns to stop reading the line."""
    result = _monitor(_full(completed=2, running=1))
    assert "Waiting:" not in result.output
    assert "Blocked:" not in result.output
