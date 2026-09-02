# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Postprocessing loads the campaign into the index, or fails saying so.

Written because swapping the ``data.db`` writer for the index ingest inside
``run_postprocessing`` broke no test at all: every existing test drove that writer
directly, so the call site itself was uncovered and a regression there would have been
silent.

The failure direction matters more than the success one. "Finished" has to keep meaning
"queryable"; a postprocess that completes with the index down would make those two things
differ, and the difference is invisible until somebody asks a question and gets nothing.
"""

import os

import pytest

from robovast.common.errors import IndexUnreachableError
from robovast.results_processing import postprocessing

DSN = os.environ.get("ROBOVAST_TEST_PG_DSN")


def _campaign_tree(tmp_path):
    """The smallest results tree ``run_postprocessing`` will walk."""
    root = tmp_path / "camp-2026-01-01-000000"
    run_dir = root / "goal-1" / "0"
    run_dir.mkdir(parents=True)
    (run_dir / "nav_metrics.csv").write_text("duration_s,collided\n12.5,0\n")
    (root / "_config").mkdir()
    (root / "_config" / "campaign.vast").write_text(
        "execution:\n  containers: {}\nresults_processing:\n  postprocessing: []\n")
    return root


def test_an_unreachable_index_fails_postprocessing_rather_than_passing_quietly(
        tmp_path, monkeypatch):
    """The load-bearing direction: down must not read as done.

    Port 1 is reserved and never listening, so this exercises the real driver path rather
    than a stubbed exception.
    """
    from robovast.common import index_db

    monkeypatch.setenv(index_db.DSN_ENV,
                       "host=127.0.0.1 port=1 dbname=robovast connect_timeout=2")
    root = _campaign_tree(tmp_path)

    with pytest.raises(IndexUnreachableError) as excinfo:
        postprocessing.run_postprocessing(str(tmp_path), campaign=root.name,
                                          skip_metadata=True)

    assert "did not answer" in str(excinfo.value)
    assert excinfo.value.include_traceback is False


def test_an_unconfigured_index_names_the_variable(tmp_path, monkeypatch):
    """A deployment that forgot the DSN should say which one, not fail obscurely."""
    from robovast.common import index_db

    monkeypatch.delenv(index_db.DSN_ENV, raising=False)
    root = _campaign_tree(tmp_path)

    with pytest.raises(IndexUnreachableError) as excinfo:
        postprocessing.run_postprocessing(str(tmp_path), campaign=root.name,
                                          skip_metadata=True)

    assert index_db.DSN_ENV in str(excinfo.value)


def test_skip_db_skips_the_ingest_without_needing_an_index(tmp_path, monkeypatch):
    """``skip_db`` has to keep working: it is how a re-run avoids redoing the load."""
    from robovast.common import index_db

    monkeypatch.delenv(index_db.DSN_ENV, raising=False)
    root = _campaign_tree(tmp_path)

    # No index configured at all, and no exception: the ingest was not attempted.
    postprocessing.run_postprocessing(str(tmp_path), campaign=root.name,
                                      skip_db=True, skip_metadata=True)


@pytest.mark.skipif(not DSN, reason="ROBOVAST_TEST_PG_DSN is not set")
def test_a_successful_postprocess_leaves_the_campaign_queryable(tmp_path, monkeypatch):
    """The success direction, end to end through the real call site."""
    psycopg = pytest.importorskip("psycopg")
    from robovast.common import index_db

    with psycopg.connect(DSN, autocommit=True) as setup:
        setup.execute("DROP SCHEMA IF EXISTS pp_test CASCADE")
        setup.execute("CREATE SCHEMA pp_test")
    # ``options`` rather than a SET, so the search_path survives the connection the
    # ingest opens for itself.
    monkeypatch.setenv(index_db.DSN_ENV, DSN + " options=-csearch_path=pp_test")

    root = _campaign_tree(tmp_path)
    postprocessing.run_postprocessing(str(tmp_path), campaign=root.name,
                                      skip_metadata=True)

    with psycopg.connect(DSN, autocommit=True) as check:
        check.execute("SET search_path TO pp_test")
        got = check.execute(
            "SELECT campaign_id, config_name, run_id, duration_s FROM nav_metrics"
        ).fetchall()
    assert got == [(root.name, "goal-1", 0, 12.5)]

    with psycopg.connect(DSN, autocommit=True) as teardown:
        teardown.execute("DROP SCHEMA IF EXISTS pp_test CASCADE")


def test_a_campaign_with_no_provenance_record_is_a_failure(tmp_path, monkeypatch):
    """What goes unwritten is the campaign's FAIR provenance record -- what an archive's
    recipient reads to know what produced the data.

    Reported as a warning, the campaign read as unqualified success while missing it, so it
    would export and be shared as complete. The message names what is missing, and says the
    derived data is intact so a reader knows a re-run is cheap rather than a re-derivation.
    """
    root = _campaign_tree(tmp_path)
    monkeypatch.setattr(postprocessing, "generate_campaign_metadata",
                        lambda *a, **kw: (False, "execution.yaml not found in " + str(root)))

    ok, message = postprocessing.run_postprocessing(str(tmp_path), campaign=root.name,
                                                    skip_db=True)

    assert ok is False
    assert "no FAIR provenance record" in message
    assert "execution.yaml not found" in message, "the underlying reason has to survive"
    assert "re-running postprocessing" in message, "and what it costs to fix"


def test_the_derived_data_is_still_recorded_when_the_record_is_not(tmp_path, monkeypatch):
    """The provenance MARKER says what was derived, and that stays true when the metadata
    step fails. Withholding it to express the failure would tell every reader the CSVs and
    index rows are absent when they are there -- one wrong signal traded for another. The
    failure travels in the return value instead.
    """
    from robovast.common.campaign_data import POSTPROCESSING_RECORD

    root = _campaign_tree(tmp_path)
    monkeypatch.setattr(postprocessing, "generate_campaign_metadata",
                        lambda *a, **kw: (False, "no execution.yaml"))

    ok, _message = postprocessing.run_postprocessing(str(tmp_path), campaign=root.name,
                                                     skip_db=True)

    assert ok is False
    assert (root / POSTPROCESSING_RECORD).is_file()
