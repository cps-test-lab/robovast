# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The evidence must outlive the campaign it explains.

A campaign that dies mid-batch records no ``run`` rows and never postprocesses, so
``data.db`` is never built -- and the query interface fetches only ``campaign.db`` and
``_execution/data.db`` (``ClusterService._QUERY_DBS``). Anything that lived only in
``data.db``, or only in a JSON file, would therefore be unreachable by SQL for exactly the
campaign that needs explaining. That is not hypothetical: it is what happened to
``rr-roqsim-full-2026-08-23-03124069``, whose whole post-mortem had to be reconstructed
from one formatted log sentence after its pod was collected.
"""

import json

from robovast.common.campaign_data import (read_container_failures,
                                           record_container_failures)
from robovast.common.store import CampaignStore
from robovast.results_processing.data_query import query_data_db

#: The record the runner writes at the moment of the restart, shaped as
#: ``BatchJobRunner._capture_container_failures`` writes it.
_SUT_SIGBUS = {
    "detected_at": "2026-08-23T03:33:05+00:00",
    "batch": "batch-2",
    "job_name": "rrroqs-x-37",
    "job_dir": "_jobs/batch-2/job-37",
    "runs": ["cfg-1/0"],
    "pod_name": "rrroqs-x-37-pod",
    "node_label": "node-abc123def456",
    "pod_phase": "Running",
    "container": "sut",
    "role": "sut",
    "image": "an-image",
    "image_id": "an-image@sha256:abc",
    "restart_count": 1,
    "reason": "Error",
    "exit_code": 135,
    "signal": 7,
    "signal_name": "SIGBUS",
    "message": None,
    "started_at": None,
    "finished_at": None,
    "cpu_limit": "3.25",
    "memory_limit": None,
    "log_status": "captured",
    "log_lines": 2,
    "log_tail": "[sut] terminate called\n[sut] Bus error",
}


def _aborted_campaign(tmp_path):
    """A campaign root in the shape the reported one had: a record, a store, nothing else.

    No ``data.db``, no run directories, no ``test.xml`` -- the controller never got that
    far, because the exception left the batch before any of it was written.
    """
    record_container_failures(tmp_path, [_SUT_SIGBUS])
    store = CampaignStore(tmp_path / "campaign.db")
    campaign_id = store.create_campaign("rr-roqsim-full", {}, mode="search")
    store.record_container_failures(campaign_id, read_container_failures(tmp_path))
    store.close()


def test_the_campaign_really_has_no_postprocessed_data(tmp_path):
    """Guards the premise. If this ever starts failing, the test below stops proving
    anything, because it would be reading a store the happy path built."""
    _aborted_campaign(tmp_path)
    assert not (tmp_path / "_execution" / "data.db").exists()
    assert not (tmp_path / "data.db").exists()
    assert list(tmp_path.glob("*/0/test.xml")) == []


def test_forensics_are_queryable_when_the_campaign_never_postprocesses(tmp_path):
    """The documented query, run verbatim against a campaign that died."""
    _aborted_campaign(tmp_path)

    result = query_data_db(
        tmp_path,
        "SELECT run_key, node_label, container, role, exit_code, signal_name, reason, "
        "memory_limit FROM container_failure_view ORDER BY run_key")

    assert "error" not in result
    row, = result["rows"]
    assert row["run_key"] == "cfg-1/0"
    assert (row["exit_code"], row["signal_name"]) == (135, "SIGBUS")
    assert row["container"] == "sut" and row["role"] == "sut"
    assert row["node_label"] == "node-abc123def456"
    # None means NO memory limit was declared. The absence is a finding about the
    # campaign, and reconstructing it later means finding the .vast that ran.
    assert row["memory_limit"] is None


def test_the_dead_containers_own_last_words_survive(tmp_path):
    """The single most useful artifact, and the one with a deadline: a restarted
    container's previous log lives only as long as the kubelet keeps its pod."""
    _aborted_campaign(tmp_path)

    result = query_data_db(
        tmp_path, "SELECT log_status, log_lines, log_tail FROM container_failure_view")
    row, = result["rows"]
    assert row["log_status"] == "captured"
    assert "Bus error" in row["log_tail"]


def test_the_json_record_is_readable_without_any_store(tmp_path):
    """The table is an index; the file is the record. A campaign whose store never opened
    still has the evidence on disk."""
    record_container_failures(tmp_path, [_SUT_SIGBUS])
    record, = read_container_failures(tmp_path)
    assert record["signal_name"] == "SIGBUS"
    assert json.loads(
        (tmp_path / "_execution" / "container_failures.json").read_text())[0][
            "job_name"] == "rrroqs-x-37"


def test_a_failure_whose_runs_are_unresolved_still_appears(tmp_path):
    """json_each('[]') yields no rows, so without the UNION ALL guard a failure the runner
    could not attribute to any run would vanish from the view entirely -- silently, and in
    the case where something had already gone wrong enough to lose the mapping."""
    record_container_failures(tmp_path, [{**_SUT_SIGBUS, "runs": [], "job_dir": ""}])
    store = CampaignStore(tmp_path / "campaign.db")
    campaign_id = store.create_campaign("c", {}, mode="search")
    store.record_container_failures(campaign_id, read_container_failures(tmp_path))
    store.close()

    result = query_data_db(
        tmp_path, "SELECT run_key, signal_name FROM container_failure_view")
    row, = result["rows"]
    assert row["run_key"] is None
    assert row["signal_name"] == "SIGBUS"


def test_re_ingesting_a_campaign_does_not_multiply_its_incidents(tmp_path):
    """campaign_index rebuilds a store from disk whenever one is missing; a post-mortem
    that doubled every time would misreport how often the fault actually happened."""
    record_container_failures(tmp_path, [_SUT_SIGBUS])
    store = CampaignStore(tmp_path / "campaign.db")
    campaign_id = store.create_campaign("c", {}, mode="search")
    for _ in range(3):
        store.record_container_failures(campaign_id, read_container_failures(tmp_path))
    store.close()

    result = query_data_db(tmp_path, "SELECT COUNT(*) AS n FROM container_failure")
    assert result["rows"][0]["n"] == 1
