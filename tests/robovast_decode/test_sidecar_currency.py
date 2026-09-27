# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A definitions sidecar that comes or changes after a build makes the recording's tables stale.

The sidecar decides what decodes, so it is part of what a table is current against: a table
built while a type had no definition is built again once the sidecar defines it.
"""

import json
import shutil

import pyarrow.parquet as pq

from robovast_decode.build import build
from robovast_decode.definitions import SIDECAR_NAME
from robovast_decode.live import Watcher

from .conftest import make_campaign, string_cdr, write_bag

TABLE = "rosbag2_opaque"
#: A type no distribution defines, laid out as ``std_msgs/msg/String`` so its payloads decode
#: once the sidecar says so.
TOPICS = {"/opaque": ("example_msgs/msg/Opaque", [string_cdr(t) for t in "abc"])}
DEFINITIONS = {"example_msgs/msg/Opaque": "string data\n"}


def _campaign(root, verdict=True):
    campaign = make_campaign(root, verdict=verdict)
    shutil.rmtree(campaign / "cfg" / "0" / "rosbag2")
    return campaign, write_bag(campaign / "cfg" / "0" / "rosbag2", TOPICS)


def _entry(campaign):
    manifest = json.loads((campaign / ".cache" / "MANIFEST.json").read_text())
    return manifest["tables"][TABLE]["runs"]["cfg/0"]


def _recorded_reason(campaign):
    """The ``_recording`` table's reason for ``/opaque``."""
    rows = pq.read_table(campaign / ".cache" / "tables" / "_recording" / "cfg" / "0.parquet")
    (row,) = [r for r in rows.to_pylist() if r["topic"] == "/opaque"]
    return row["reason"]


def test_a_sidecar_written_after_a_build_makes_the_table_complete(tmp_path):
    campaign, bag_dir = _campaign(tmp_path / "c")
    build(str(campaign), tables=[TABLE])
    assert _entry(campaign)["rows"] == 0 and "/opaque" in _entry(campaign)["reason"]
    assert _recorded_reason(campaign)

    (bag_dir / SIDECAR_NAME).write_text(json.dumps(DEFINITIONS))
    report = build(str(campaign), tables=[TABLE])
    assert report.built[TABLE] == ["cfg/0"] and report.built["_recording"] == ["cfg/0"]
    entry = _entry(campaign)
    assert entry["rows"] == 3 and not entry.get("reason")
    assert _recorded_reason(campaign) is None, "the recording's report is built again too"
    assert build(str(campaign), tables=[TABLE]).skipped[TABLE] == ["cfg/0"]


def test_a_live_session_that_gave_a_type_up_leaves_its_table_to_a_later_build(tmp_path):
    campaign, bag_dir = _campaign(tmp_path / "c", verdict=False)
    watcher = Watcher(str(campaign), None, part_s=0.0)
    watcher.demand("cfg/0", [TABLE])
    watcher.changed([str(bag_dir / "rosbag2_0.mcap")])
    # The sidecar lands after the session gave the type up, before the run's verdict.
    (bag_dir / SIDECAR_NAME).write_text(json.dumps(DEFINITIONS))
    (campaign / "cfg" / "0" / "test.xml").write_text("<testsuite/>")
    watcher.changed([str(campaign / "cfg" / "0" / "test.xml")])
    assert watcher.following("cfg/0") == set()
    assert "/opaque" in _entry(campaign)["reason"]

    assert build(str(campaign), tables=[TABLE]).built[TABLE] == ["cfg/0"]
    assert _entry(campaign)["rows"] == 3 and not _entry(campaign).get("reason")


def test_a_live_session_with_the_sidecar_leaves_nothing_for_a_build(tmp_path):
    campaign, bag_dir = _campaign(tmp_path / "c", verdict=False)
    (bag_dir / SIDECAR_NAME).write_text(json.dumps(DEFINITIONS))
    watcher = Watcher(str(campaign), None, part_s=0.0)
    watcher.demand("cfg/0", [TABLE])
    watcher.changed([str(bag_dir / "rosbag2_0.mcap")])
    (campaign / "cfg" / "0" / "test.xml").write_text("<testsuite/>")
    watcher.changed([str(campaign / "cfg" / "0" / "test.xml")])
    assert _entry(campaign)["rows"] == 3 and not _entry(campaign).get("reason")
    assert build(str(campaign), tables=[TABLE]).skipped[TABLE] == ["cfg/0"]
