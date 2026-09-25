# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Each bag gets the definitions rosbag2 leaves out, in the form rosbag2 embeds them.

rosbag2 embeds nothing for an action-derived type, so the run writes them beside the bag
while the types are installed. Tested against a fake ROS install: the script finds
interfaces through ``AMENT_PREFIX_PATH`` and nothing else.
"""

import json

import pytest

from robovast.execution.data import dump_message_definitions as dump

SEP = "=" * 80


@pytest.fixture
def ros_install(tmp_path, monkeypatch):
    share = tmp_path / "prefix" / "share"
    (share / "nav_pkg" / "action").mkdir(parents=True)
    (share / "nav_pkg" / "msg").mkdir(parents=True)
    (share / "unique_identifier_msgs" / "msg").mkdir(parents=True)
    (share / "std_msgs" / "msg").mkdir(parents=True)
    (share / "builtin_interfaces" / "msg").mkdir(parents=True)
    (share / "nav_pkg" / "action" / "Go.action").write_text(
        "Target target\n---\nbool ok\n---\n# progress\nGo_Extra extra\nfloat32 remaining\n"
        "int8 CONST=3\n")
    (share / "nav_pkg" / "msg" / "Target.msg").write_text("Header header\nfloat64 x\n")
    (share / "nav_pkg" / "msg" / "Go_Extra.msg").write_text("string note\n")
    (share / "unique_identifier_msgs" / "msg" / "UUID.msg").write_text("uint8[16] uuid\n")
    (share / "std_msgs" / "msg" / "Header.msg").write_text(
        "builtin_interfaces/Time stamp\nstring frame_id\n")
    (share / "builtin_interfaces" / "msg" / "Time.msg").write_text("int32 sec\nuint32 nanosec\n")
    monkeypatch.setenv("AMENT_PREFIX_PATH", str(tmp_path / "prefix"))
    return tmp_path


def test_a_feedback_message_is_synthesised_from_its_action_file(ros_install):
    text = dump.full_definition("nav_pkg/action/Go_FeedbackMessage")
    assert text.startswith("unique_identifier_msgs/UUID goal_id\nGo_Feedback feedback")
    assert f"{SEP}\nMSG: nav_pkg/action/Go_Feedback\n" in text
    assert f"{SEP}\nMSG: unique_identifier_msgs/UUID\nuint8[16] uuid" in text
    # a relative type inside an .action that is not one it generates resolves under msg/
    assert f"{SEP}\nMSG: nav_pkg/Go_Extra\nstring note" in text
    assert "MSG: nav_pkg/action/Go_Extra" not in text


def test_a_message_carries_every_type_it_uses(ros_install):
    text = dump.full_definition("nav_pkg/msg/Target")
    assert f"{SEP}\nMSG: std_msgs/Header\n" in text
    assert f"{SEP}\nMSG: builtin_interfaces/Time\n" in text


def test_the_sidecar_is_written_beside_each_bag_and_names_what_it_lacks(ros_install, capsys):
    bag = ros_install / "out" / "cfg" / "0" / "rosbag2"
    bag.mkdir(parents=True)
    (bag / "rosbag2_0.mcap").write_bytes(b"")
    (bag / "metadata.yaml").write_text(
        "rosbag2_bagfile_information:\n  topics_with_message_count:\n"
        "    - topic_metadata:\n        name: /go/_action/feedback\n"
        "        type: nav_pkg/action/Go_FeedbackMessage\n"
        "    - topic_metadata:\n        name: /secret\n        type: other_pkg/msg/Secret\n")
    assert dump.main([str(ros_install / "out")]) == 0
    written = json.loads((bag / dump.SIDECAR_NAME).read_text())
    assert list(written) == ["nav_pkg/action/Go_FeedbackMessage"]
    assert "other_pkg/msg/Secret" in capsys.readouterr().err
