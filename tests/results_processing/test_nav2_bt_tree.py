# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``nav2_bt_tree``: nav2's transition log joined with its BT XML into a ``behaviors`` table.

The transitions are the decoder fixture recording's ``/behavior_tree_log``, read as the
``nav2_behavior_tree`` table; what the step writes is each run's ``nav2_behaviors.csv``,
read back as the ``nav2_behaviors`` table.
"""

import pytest

from robovast_data import Campaign
from robovast_nav.postprocessing import Nav2BtTree
from tests.robovast_data.conftest import nav_campaign, write_store

#: Two of the three node names the fixture log carries, so one is name drift.
_XML = """<root main_tree_to_execute="Main">
  <BehaviorTree ID="Main">
    <Sequence name="node_0">
      <Action name="node_1"/>
    </Sequence>
  </BehaviorTree>
</root>
"""


@pytest.fixture(name="campaign")
def _campaign(tmp_path):
    root = nav_campaign(tmp_path / "nav-2026-01-01-00000000", runs=(("cfg", 0), ("cfg", 1)))
    config = tmp_path / "config"
    (config / "files").mkdir(parents=True)
    (config / "files" / "bt.xml").write_text(_XML)
    return root, config


def test_each_run_gets_its_tree_as_the_nav2_behaviors_table(campaign):
    root, config = campaign
    ok, message = Nav2BtTree()(str(root), str(config), bt_xml="files/bt.xml")
    assert ok, message
    assert "for 2 run(s)" in message and "name drift" in message

    behaviors = Campaign(str(root)).table("nav2_behaviors", config="cfg", run=0)
    raw = Campaign(str(root)).table("nav2_behavior_tree", config="cfg", run=0)
    assert set(behaviors["behavior_name"]) == {"node_0", "node_1"}
    assert set(behaviors.loc[behaviors["behavior_name"] == "node_1", "parent_id"]) == {0}
    baseline = behaviors[behaviors["status_name"] == "INVALID"]
    assert len(baseline) >= 2 and baseline["timestamp"].min() == raw["timestamp"].min()
    ticked = raw["node_name"].isin(["node_0", "node_1"]) & raw["current_status"].str.upper(
    ).isin(["RUNNING", "SUCCESS", "FAILURE"])
    assert (behaviors["status_name"] != "INVALID").sum() == ticked.sum() > 0


def test_a_run_already_written_is_left_unless_forced(campaign):
    root, config = campaign
    tree = Nav2BtTree()
    tree(str(root), str(config), bt_xml="files/bt.xml")
    ok, message = tree(str(root), str(config), bt_xml="files/bt.xml")
    assert ok and "for 0 run(s) (2 up-to-date)" in message
    ok, message = tree(str(root), str(config), bt_xml="files/bt.xml", force=True)
    assert ok and "for 2 run(s)" in message


def test_a_campaign_without_the_log_is_refused_by_name(tmp_path):
    root = tmp_path / "nav-2026-01-01-00000000"
    (root / "cfg" / "0").mkdir(parents=True)
    write_store(root, {"cfg": {"runs": {0: "passed"}}})
    (tmp_path / "bt.xml").write_text(_XML)
    ok, message = Nav2BtTree()(str(root), str(tmp_path), bt_xml="bt.xml")
    assert not ok and "nav2_behavior_tree" in message


def test_bt_xml_is_required():
    ok, message = Nav2BtTree()("unused", "unused")
    assert not ok and "bt_xml" in message
