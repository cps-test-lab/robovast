# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A table fed by a topic the decoder cannot read says so in its manifest entry.

A topic whose type nothing defines gives its table no rows; one with a message that does not
decode gives the rows before it. Either way the entry carries the reason, so a reader does not
take an empty or cut-off table for the whole recording. A build and a live session agree.
"""

import json
import os
import shutil

from robovast_decode.build import available_tables, build
from robovast_decode.live import Watcher

from .conftest import UNDECODABLE_TOPICS, make_campaign, write_bag

TABLES = ["rosbag2_opaque", "rosbag2_torn", "rosbag2_fine"]


def _campaign(root, verdict=True):
    campaign = make_campaign(root, verdict=verdict)
    shutil.rmtree(campaign / "cfg" / "0" / "rosbag2")
    return campaign


def _entries(campaign, key="cfg/0"):
    manifest = json.loads((campaign / ".cache" / "MANIFEST.json").read_text())
    return {t: manifest["tables"][t]["runs"][key] for t in TABLES}


def _assert_reasons(entries):
    opaque, torn, fine = (entries[t] for t in TABLES)
    assert opaque["rows"] == 0 and opaque["files"] == []
    assert "/opaque" in opaque["reason"] and "example_msgs/msg/Opaque" in opaque["reason"]
    assert torn["rows"] == 1 and torn["files"], "the rows before the message are kept"
    assert "/torn" in torn["reason"] and "does not decode" in torn["reason"]
    assert fine["rows"] == 3 and not fine.get("reason"), "a sibling topic is not marked"


def test_a_build_names_the_undecodable_topic_in_its_tables_entry(tmp_path):
    campaign = _campaign(tmp_path / "c")
    write_bag(campaign / "cfg" / "0" / "rosbag2", UNDECODABLE_TOPICS)
    report = build(str(campaign), tables=TABLES)
    _assert_reasons(_entries(campaign))
    assert "/opaque" in report.failed["rosbag2_opaque"]["cfg/0"]
    assert "/torn" in report.incomplete["rosbag2_torn"]["cfg/0"]
    assert "rosbag2_fine" not in report.failed and "rosbag2_fine" not in report.incomplete
    counts = available_tables(str(campaign))
    assert counts["rosbag2_torn"]["built"] == 1 and "cfg/0" in counts["rosbag2_torn"]["failed"]
    assert counts["rosbag2_opaque"]["built"] == 0
    assert counts["rosbag2_fine"] == {"runs": 1, "built": 1, "failed": {}}


def test_a_live_session_records_the_same_reasons_as_a_build(tmp_path):
    campaign = _campaign(tmp_path / "c", verdict=False)
    bag_dir = write_bag(campaign / "cfg" / "0" / "rosbag2", UNDECODABLE_TOPICS)
    watcher = Watcher(str(campaign), None, part_s=0.0)
    watcher.demand("cfg/0", TABLES)
    watcher.changed([str(bag_dir / "rosbag2_0.mcap")])
    manifest = json.loads((campaign / ".cache" / "MANIFEST.json").read_text())
    live = manifest["tables"]["rosbag2_torn"]["runs"]["cfg/0"]
    assert "live" in live and "/torn" in live["reason"], "a query during the run sees it"
    (campaign / "cfg" / "0" / "test.xml").write_text("<testsuite/>")
    watcher.changed([str(campaign / "cfg" / "0" / "test.xml")])
    assert watcher.following("cfg/0") == set()
    followed = _entries(campaign)
    _assert_reasons(followed)

    built = _campaign(tmp_path / "b")
    write_bag(built / "cfg" / "0" / "rosbag2", UNDECODABLE_TOPICS)
    build(str(built), tables=TABLES)
    for table, entry in _entries(built).items():
        assert followed[table].get("reason") == entry.get("reason"), table
        assert followed[table]["rows"] == entry["rows"], table


def test_an_absent_table_of_a_run_with_two_recordings_is_current_on_the_next_build(tmp_path):
    # The run has its scenario recording and its job's rosout recording; the table comes from
    # the first alone, so its entry is current against that one's bytes.
    campaign = _campaign(tmp_path / "c")
    bag_dir = write_bag(campaign / "cfg" / "0" / "rosbag2", UNDECODABLE_TOPICS)
    assert build(str(campaign), tables=["rosbag2_opaque"]).failed["rosbag2_opaque"]
    report = build(str(campaign), tables=["rosbag2_opaque"])
    assert report.skipped.get("rosbag2_opaque") == ["cfg/0"]
    assert "rosbag2_opaque" not in report.built and "rosbag2_opaque" not in report.failed
    manifest = json.loads((campaign / ".cache" / "MANIFEST.json").read_text())
    absent = manifest["tables"]["rosbag2_opaque"]["runs"]["cfg/0"]
    assert list(absent["sources"]) == [os.path.relpath(bag_dir, campaign)]
    assert "/opaque" in absent["reason"], "the reason stays with the entry"
