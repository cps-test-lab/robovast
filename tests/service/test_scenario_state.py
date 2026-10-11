# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The scenario's tree, folded from a run's tables, is what scenario-execution's own reader says.

``get_job_state`` shows an agent where a running scenario is. That reply is built here from
the run's ``behaviors`` and ``behaviors_meta`` tables rather than by running a reader inside
the job, and the one thing that has to hold is that both routes give the same answer for the
same log -- so the pin is scenario-execution's ``tree_state`` on the file, beside the fold on
the rows the engine read out of it.
"""

import json

import pytest
from scenario_execution.tree_state import tree_state
from scenario_execution.utils import bt_logger

from robovast.service.scenario_state import NoMetadataRecord, scenario_state
from tests.service.behaviour_log import (DRIVE, DRIVE_TO, INSERTED, ROOT, metadata, record,
                                         records, write_log, write_store)

CID = "campaign-2026-09-01-100000"


@pytest.fixture(name="campaign")
def _campaign(tmp_path):
    root = tmp_path / CID
    write_store(root, {"cfgA": [1]})
    return root


def test_the_fold_equals_the_readers_reply_on_the_same_log(campaign):
    """Every key the exec read returned -- found, log, scenario, started_at, clock, last_change,
    now, running (with its path), counts, tree -- and every value. A sim-clock log, so ``now`` is
    the caller's and the two are not racing a wall clock."""
    path = write_log(campaign / "cfgA" / "1")

    folded = scenario_state(str(campaign), CID, "cfgA/1", now=40.0)

    assert folded == tree_state(str(path), now=40.0)
    assert folded["running"]["name"] == "drive_to"
    assert folded["running"]["path"] == "scenario > drive > drive_to"
    assert folded["running"]["for_s"] == 36.0
    assert folded["counts"] == {"RUNNING": 4, "SUCCESS": 1, "INVALID": 1}


def test_the_fold_respects_log_order_not_timestamp_order(campaign):
    """Two records of one node at one stamp: the later is the node's state. The fixture ends
    ``drive_to`` SUCCESS then RUNNING at the same stamp, so a fold ordered by anything but the
    log's own sequence would report the scenario as finished."""
    write_log(campaign / "cfgA" / "1")

    folded = scenario_state(str(campaign), CID, "cfgA/1", now=40.0)

    assert folded["running"]["status"] == "RUNNING"
    assert folded["running"]["feedback"] == "again"


def test_a_monotonic_log_derives_now_from_its_start(campaign):
    """A ``monotonic`` log's stamps count from ``started_at``, so the fold derives ``now`` from
    wall time as the reader does, and a caller's *now* is ignored. Compared with a tolerance:
    the two reads are moments apart on a real clock."""
    path = write_log(campaign / "cfgA" / "1", records(clock="monotonic"))

    folded = scenario_state(str(campaign), CID, "cfgA/1", now=1.0)
    read = tree_state(str(path), now=1.0)

    for reply in (folded, read):
        assert reply["clock"] == "monotonic"
        assert reply["now"] > 1000, "derived from started_at, not the caller's 1.0"
    assert abs(folded["now"] - read["now"]) < 0.5
    assert abs(folded["running"]["for_s"] - read["running"]["for_s"]) < 0.5
    for reply in (folded, read):
        reply.pop("now")
        reply["running"].pop("for_s")
        reply.pop("tree")
    assert folded == read


def test_without_the_tree_only_the_running_action_and_the_counts_come(campaign):
    path = write_log(campaign / "cfgA" / "1")

    folded = scenario_state(str(campaign), CID, "cfgA/1", include_tree=False, now=40.0)

    assert folded == tree_state(str(path), include_tree=False, now=40.0)
    assert "tree" not in folded


def test_a_run_with_no_log_says_so_in_the_readers_own_words(campaign):
    """Not an empty tree: "the scenario has no nodes" and "nobody could read the log" are
    different answers and must not render alike."""
    (campaign / "cfgA" / "1").mkdir(parents=True)

    reply = scenario_state(str(campaign), CID, "cfgA/1")

    assert reply["found"] is False
    assert "behaviors.jsonl" in reply["error"] and "--bt-log" in reply["error"]


def test_a_log_that_has_not_ticked_says_so(campaign):
    """The writer opens the file and writes its metadata before the first tick, so a log holding
    only that line is a scenario launched and not yet ticked -- the reader's own reason."""
    path = write_log(campaign / "cfgA" / "1", [metadata()])

    reply = scenario_state(str(campaign), CID, "cfgA/1")

    assert reply == tree_state(str(path))
    assert reply["found"] is False and "has not ticked" in reply["error"]


def test_an_empty_file_is_a_log_that_has_not_ticked(campaign):
    path = write_log(campaign / "cfgA" / "1", [], terminated=False)

    reply = scenario_state(str(campaign), CID, "cfgA/1")

    assert reply["found"] is False and "has not ticked" in reply["error"]
    assert path.stat().st_size == 0


def test_a_log_without_its_metadata_record_is_refused_naming_the_file(campaign):
    """The record says which scenario ran and which clock every stamp is in. A tree folded
    without it is a tree of an unknown run, and reporting one as merely "unavailable" beside a
    healthy simulator would read as a run with nothing to show."""
    path = write_log(campaign / "cfgA" / "1", records()[1:])

    with pytest.raises(NoMetadataRecord, match="metadata record") as err:
        scenario_state(str(campaign), CID, "cfgA/1")

    assert str(path) in str(err.value)


def test_the_fold_follows_a_log_as_it_grows(campaign):
    """The whole point of folding from the tables: a running run's log is appended to, and the
    engine rebuilds the run's table when the file has grown, so every read is of the log as it
    is. A half-written last line is held back, not refused."""
    run = campaign / "cfgA" / "1"
    entries = [e for e in records() if e.get("behavior_id") != INSERTED]
    write_log(run, entries[:7])            # metadata, the snapshot, root RUNNING
    first = scenario_state(str(campaign), CID, "cfgA/1", now=10.0)
    assert first["running"]["name"] == "scenario"

    write_log(run, entries)
    with open(run / "behaviors.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record(DRIVE_TO, 9.0, "FAILURE"))[:-3])   # mid-write
    second = scenario_state(str(campaign), CID, "cfgA/1", now=10.0)
    assert second["running"]["name"] == "drive_to"
    assert second["last_change"] == 4.0

    with open(run / "behaviors.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record(DRIVE_TO, 9.0, "FAILURE"))[-3:] + "\n")
        for node in (DRIVE, ROOT):
            fh.write(json.dumps(record(node, 9.0, "FAILURE")) + "\n")
    third = scenario_state(str(campaign), CID, "cfgA/1", now=10.0)
    assert third["running"] is None
    assert third["counts"]["FAILURE"] == 3
    assert third == tree_state(str(run / "behaviors.jsonl"), now=10.0)


def test_a_pruned_node_is_stated_gone_and_keeps_its_last_state(campaign):
    """The writer states a pruned subtree with a three-key record. Folded from a row, those three
    are what it contributes: the row's other columns are the table's nulls, and giving them back
    would blank the node the writer meant to mark."""
    path = write_log(campaign / "cfgA" / "1")

    folded = scenario_state(str(campaign), CID, "cfgA/1", now=40.0)

    drive_to = folded["tree"]["children"][1]["children"][0]
    inserted = drive_to["children"][0]
    assert inserted["name"] == "inserted" and inserted["since"] == 3.2
    assert inserted == tree_state(str(path), now=40.0)["tree"]["children"][1]["children"][0][
        "children"][0]
    assert INSERTED not in json.dumps(folded)


def test_a_malformed_run_key_is_refused(campaign):
    with pytest.raises(ValueError, match="<config>/<run>"):
        scenario_state(str(campaign), CID, "cfgA")


# -- the fixture is what the writer writes ---------------------------------------------------------


def test_the_fixture_records_carry_exactly_the_writers_keys(tmp_path):
    """The fold names the metadata record's keys and the behaviour record's. Pinned against the
    writer itself rather than against a copy of its output, so a key the writer gains or loses
    fails here and not on a live run."""
    import py_trees

    root = py_trees.composites.Sequence("scenario", memory=True)
    root.add_child(py_trees.behaviours.Success("setup"))
    tree = py_trees.trees.BehaviourTree(root)
    meta = bt_logger.build_meta("demo", str(tmp_path / "none.osc"), 0.1, None)
    logger = bt_logger.BehaviourTreeJsonlLogger(str(tmp_path / "behaviors.jsonl"), meta, None)
    tree.add_visitor(logger.snapshot_visitor)
    logger.write_initial_snapshot(tree)
    tree.add_post_tick_handler(logger)
    tree.tick()
    logger.close()
    written = [json.loads(line)
               for line in (tmp_path / "behaviors.jsonl").read_text().splitlines()]

    assert set(written[0]) == set(metadata())
    assert written[0]["format"] == metadata()["format"]
    assert set(written[1]) == set(record(DRIVE_TO, 0.0, "INVALID"))
