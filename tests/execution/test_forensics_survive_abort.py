# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The evidence must outlive the campaign it explains.

A campaign that dies mid-batch records no ``run`` rows and never postprocesses, so nothing
it produced is ever ingested -- the only thing SQL can still reach is what the campaign
store itself recorded. Anything that lived only in postprocessing's output, or only in a
JSON file, would therefore be unreachable by SQL for exactly the campaign that needs
explaining. That is not hypothetical: it is what happened to
``rr-roqsim-full-2026-08-23-03124069``, whose whole post-mortem had to be reconstructed
from one formatted log sentence after its pod was collected.
"""

import json
import os

import pytest

from robovast.common.campaign_data import (read_container_failures,
                                           record_container_failures)
from robovast.common.store import CampaignStore
from robovast.results_processing.data_query import query_data_db

DSN = os.environ.get("ROBOVAST_TEST_PG_DSN")
pg = pytest.mark.skipif(not DSN, reason="ROBOVAST_TEST_PG_DSN is not set")

#: The campaign's own schema, so a leftover from another test cannot answer for it -- these
#: tests are about what a *dead* campaign can still be asked, and a stray row from a healthy
#: one would answer that question wrongly and reassuringly.
_SCHEMA = "forensics_abort_test"

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


@pytest.fixture
def ingest():
    """An empty index of this campaign's own, and the means to load one into it.

    Yields a callable so each test can build the campaign it needs -- they differ, and the
    differences are the point: one has runs the runner could not attribute, another records
    the same incident three times.

    The schema is this file's own so that a leftover from another test cannot answer for a
    dead campaign. These tests ask what a campaign that produced nothing can still be asked,
    and a stray row from a healthy one would answer it wrongly and reassuringly.
    """
    psycopg = pytest.importorskip("psycopg")
    from robovast.common import index_db
    from robovast.results_processing import campaign_ingest

    previous = os.environ.get("ROBOVAST_INDEX_DSN")
    os.environ["ROBOVAST_INDEX_DSN"] = f"{DSN} options=-csearch_path={_SCHEMA}"
    with psycopg.connect(DSN, autocommit=True) as setup:
        for statement in (f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE",
                          "DROP SCHEMA IF EXISTS campaign CASCADE",
                          f"CREATE SCHEMA {_SCHEMA}"):
            setup.execute(statement)

    def _load(campaign_dir):
        with index_db.connect() as conn:
            campaign_ingest.ingest_campaign(conn, str(campaign_dir), campaign_dir.name)
        return campaign_dir

    yield _load

    with psycopg.connect(DSN, autocommit=True) as teardown:
        teardown.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        teardown.execute("DROP SCHEMA IF EXISTS campaign CASCADE")
    if previous is None:
        os.environ.pop("ROBOVAST_INDEX_DSN", None)
    else:
        os.environ["ROBOVAST_INDEX_DSN"] = previous


def test_the_campaign_really_has_no_postprocessed_data(tmp_path):
    """Guards the premise. If this ever starts failing, the tests below stop proving
    anything, because they would be reading data the happy path produced.

    The old form of this check was "no data.db exists", which the move to a central index
    made trivially true everywhere and therefore worthless. What actually has to hold is
    that the campaign produced nothing to ingest: no run directories, no metric files, no
    verdicts -- only the store."""
    _aborted_campaign(tmp_path)
    assert list(tmp_path.glob("*/0/test.xml")) == []
    assert list(tmp_path.glob("*/*/*.csv")) == []
    assert (tmp_path / "campaign.db").exists(), "the store is the only thing it left"


@pg
def test_forensics_are_queryable_when_the_campaign_never_postprocesses(tmp_path, ingest):
    """The documented query, run verbatim against a campaign that died.

    That this ingests at all is half the assertion: a campaign with no run directories and
    no metric files must still be mirrored, or its post-mortem would be reachable only by
    opening the store by hand -- which is the situation this file exists to prevent.
    """
    _aborted_campaign(tmp_path)

    result = query_data_db(
        ingest(tmp_path),
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


@pg
def test_the_dead_containers_own_last_words_survive(tmp_path, ingest):
    """The single most useful artifact, and the one with a deadline: a restarted
    container's previous log lives only as long as the kubelet keeps its pod."""
    _aborted_campaign(tmp_path)

    result = query_data_db(
        ingest(tmp_path),
        "SELECT log_status, log_lines, log_tail FROM container_failure_view")
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


@pg
def test_a_failure_whose_runs_are_unresolved_still_appears(tmp_path, ingest):
    """json_each('[]') yields no rows, so without the UNION ALL guard a failure the runner
    could not attribute to any run would vanish from the view entirely -- silently, and in
    the case where something had already gone wrong enough to lose the mapping."""
    record_container_failures(tmp_path, [{**_SUT_SIGBUS, "runs": [], "job_dir": ""}])
    store = CampaignStore(tmp_path / "campaign.db")
    campaign_id = store.create_campaign("c", {}, mode="search")
    store.record_container_failures(campaign_id, read_container_failures(tmp_path))
    store.close()

    result = query_data_db(
        ingest(tmp_path), "SELECT run_key, signal_name FROM container_failure_view")
    row, = result["rows"]
    assert row["run_key"] is None
    assert row["signal_name"] == "SIGBUS"


@pg
def test_re_ingesting_a_campaign_does_not_multiply_its_incidents(tmp_path, ingest):
    """campaign_index rebuilds a store from disk whenever one is missing; a post-mortem
    that doubled every time would misreport how often the fault actually happened."""
    record_container_failures(tmp_path, [_SUT_SIGBUS])
    store = CampaignStore(tmp_path / "campaign.db")
    campaign_id = store.create_campaign("c", {}, mode="search")
    for _ in range(3):
        store.record_container_failures(campaign_id, read_container_failures(tmp_path))
    store.close()

    # `campaign.container_failure`, qualified: the campaign store's tables are mirrored into
    # a schema of that name rather than being an ATTACHed database, and the unqualified name
    # no longer resolves.
    result = query_data_db(ingest(tmp_path),
                           "SELECT COUNT(*) AS n FROM campaign.container_failure")
    assert result["rows"][0]["n"] == 1
