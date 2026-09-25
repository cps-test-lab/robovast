# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Postprocessing leaves what the campaign declares built, or fails saying so.

"Finished" has to keep meaning "queryable": a postprocess that completes while a declared
table could not be built would make the two differ, and the difference is invisible until
somebody asks a question and gets less than the campaign recorded.
"""

import json

from robovast.common.campaign_data import POSTPROCESSING_RECORD
from robovast.results_processing import postprocessing
from robovast.results_processing.data_query import query_data_db
from robovast_data import Problem

from .conftest import write_campaign_db


def _campaign_tree(tmp_path):
    """The smallest campaign ``run_postprocessing`` will finish: a record and a data file."""
    root = tmp_path / "camp-2026-01-01-000000"
    write_campaign_db(root, root.name)
    for config, run in (("cfg-a", 0), ("cfg-a", 1), ("cfg-b", 0), ("cfg-b", 1)):
        run_dir = root / config / str(run)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "nav_metrics.csv").write_text(f"duration_s,collided\n{10 + run},0\n")
    (root / "_config").mkdir()
    (root / "_config" / "campaign.vast").write_text(
        "version: 6\nexecution:\n  containers: {}\n"
        "results_processing:\n  postprocessing: []\n")
    return root


def _manifest(root):
    return json.loads((root / ".cache" / "MANIFEST.json").read_text())


def test_a_successful_postprocess_leaves_the_campaign_queryable(tmp_path):
    root = _campaign_tree(tmp_path)
    ok, message = postprocessing.run_postprocessing(
        str(tmp_path), campaign=root.name, skip_metadata=True)
    assert ok, message

    got = query_data_db(root, "SELECT config_name, run_id, duration_s FROM nav_metrics "
                              "ORDER BY 1, 2")["rows"]
    assert [(r["config_name"], r["run_id"], r["duration_s"]) for r in got] == [
        ("cfg-a", 0, 10), ("cfg-a", 1, 11), ("cfg-b", 0, 10), ("cfg-b", 1, 11)]
    assert {"run_health", "postprocessing_steps"} <= set(_manifest(root)["tables"])
    assert (root / POSTPROCESSING_RECORD).is_file()


def test_the_decoder_configuration_is_recorded_for_later_builds(tmp_path):
    """A table named after postprocessing is built by the configuration it declared."""
    root = _campaign_tree(tmp_path)
    postprocessing.run_postprocessing(str(tmp_path), campaign=root.name, skip_metadata=True)
    assert (root / "_execution" / "tables.yaml").is_file()


def test_a_declared_table_that_cannot_be_built_fails_postprocessing(tmp_path, monkeypatch):
    """The load-bearing direction: a table that is not there must not read as done."""
    root = _campaign_tree(tmp_path)
    monkeypatch.setattr(postprocessing, "build_tables", lambda *a, **kw: [
        Problem(campaign_id=root.name, table="poses", run="cfg-a/0", reason="bag is truncated")])

    ok, message = postprocessing.run_postprocessing(
        str(tmp_path), campaign=root.name, skip_metadata=True)

    assert ok is False
    assert "poses" in message and "bag is truncated" in message


def test_force_builds_again_from_the_records(tmp_path):
    root = _campaign_tree(tmp_path)
    postprocessing.run_postprocessing(str(tmp_path), campaign=root.name, skip_metadata=True)
    query_data_db(root, "SELECT count(*) FROM nav_metrics")
    assert "nav_metrics" in _manifest(root)["tables"]

    lines = []
    ok, message = postprocessing.run_postprocessing(
        str(tmp_path), campaign=root.name, skip_metadata=True, force=True,
        output_callback=lines.append)

    assert ok, message
    assert any("cleared the campaign's tables" in line for line in lines)
    assert "nav_metrics" not in _manifest(root)["tables"], "a cleared table is built on use"
    assert query_data_db(root, "SELECT count(*) AS n FROM nav_metrics")["rows"] == [{"n": 4}]


def test_a_campaign_with_no_provenance_record_is_a_failure(tmp_path, monkeypatch):
    """What goes unwritten is the campaign's FAIR provenance record -- what an archive's
    recipient reads to know what produced the data.

    Reported as a warning, the campaign read as unqualified success while missing it, so it
    would export and be shared as complete. The message names what is missing, and says the
    tables are intact so a reader knows a re-run is cheap.
    """
    root = _campaign_tree(tmp_path)
    monkeypatch.setattr(postprocessing, "generate_campaign_metadata",
                        lambda *a, **kw: (False, "execution.yaml not found in " + str(root)))

    ok, message = postprocessing.run_postprocessing(str(tmp_path), campaign=root.name)

    assert ok is False
    assert "no FAIR provenance record" in message
    assert "execution.yaml not found" in message, "the underlying reason has to survive"
    assert "re-running postprocessing" in message, "and what it costs to fix"


def test_the_derived_data_is_still_recorded_when_the_record_is_not(tmp_path, monkeypatch):
    """The postprocessing record says what was derived, and that stays true when the
    metadata step fails; the failure travels in the return value instead."""
    root = _campaign_tree(tmp_path)
    monkeypatch.setattr(postprocessing, "generate_campaign_metadata",
                        lambda *a, **kw: (False, "no execution.yaml"))

    ok, _message = postprocessing.run_postprocessing(str(tmp_path), campaign=root.name)

    assert ok is False
    assert (root / POSTPROCESSING_RECORD).is_file()


def test_replay_builds_every_table_the_records_can_give(tmp_path):
    """``force`` builds the declared tables again; ``replay`` builds every table the records
    can give, for every run, so a table nobody has asked for yet is there afterwards."""
    root = _campaign_tree(tmp_path)
    postprocessing.run_postprocessing(str(tmp_path), campaign=root.name, skip_metadata=True)
    assert "nav_metrics" not in _manifest(root)["tables"], "declared tables only"

    lines = []
    ok, message = postprocessing.run_postprocessing(
        str(tmp_path), campaign=root.name, skip_metadata=True, replay=True,
        output_callback=lines.append)

    assert ok, message
    assert any("Replay: cleared the campaign's tables" in line for line in lines)
    entries = _manifest(root)["tables"]["nav_metrics"]["runs"]
    assert set(entries) == {"cfg-a/0", "cfg-a/1", "cfg-b/0", "cfg-b/1"}
    assert all(e["files"] for e in entries.values()), "built, not merely looked for"
    assert {"run_health", "postprocessing_steps"} <= set(_manifest(root)["tables"])
    assert (root / POSTPROCESSING_RECORD).is_file()
