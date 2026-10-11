# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""print_bag_topics refuses metadata that carries no topic list, naming the bag."""

import pytest
import yaml

from robovast.common.analysis import print_bag_topics


def _bag(tmp_path, metadata):
    (tmp_path / "rosbag2").mkdir()
    (tmp_path / "rosbag2" / "metadata.yaml").write_text(yaml.safe_dump(metadata))
    return str(tmp_path)


@pytest.mark.parametrize("metadata", [
    {"something_else": {}},
    {"rosbag2_bagfile_information": {"version": 5}},
])
def test_metadata_without_a_topic_list_is_refused(tmp_path, metadata):
    with pytest.raises(ValueError, match="Invalid bag info format"):
        print_bag_topics(_bag(tmp_path, metadata))


def test_topics_are_listed(tmp_path, capsys):
    metadata = {"rosbag2_bagfile_information": {"topics_with_message_count": [
        {"topic_metadata": {"name": "/odom", "type": "nav_msgs/msg/Odometry"},
         "message_count": 3}]}}

    print_bag_topics(_bag(tmp_path, metadata))

    assert "/odom" in capsys.readouterr().out
