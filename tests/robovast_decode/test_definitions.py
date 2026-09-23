# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Definitions come from the recording, then its sidecar, then the distro -- in that order."""

import json

from robovast_decode.definitions import SIDECAR_NAME, TypeCatalog


def test_a_standard_type_is_filled_in_from_the_distro_when_first_needed():
    catalog = TypeCatalog()
    assert not catalog.knows("nav_msgs/msg/Odometry")
    assert catalog.ensure("nav_msgs/msg/Odometry")
    assert catalog.knows("geometry_msgs/msg/PoseWithCovariance")


def test_the_recordings_own_definition_wins_over_the_distros():
    catalog = TypeCatalog()
    assert catalog.add_definition("std_msgs/msg/Bool", "ros2msg", "bool data\nint32 revision")
    assert catalog.ensure("std_msgs/msg/Bool")
    assert [name for name, _ in catalog.fields("std_msgs/msg/Bool")] == ["data", "revision"]


def test_an_empty_recorded_definition_leaves_the_type_to_the_next_source(tmp_path):
    catalog = TypeCatalog()
    assert not catalog.add_definition("my_pkg/action/Go_FeedbackMessage", "ros2msg", "")
    (tmp_path / SIDECAR_NAME).write_text(json.dumps({
        "my_pkg/action/Go_FeedbackMessage":
            "unique_identifier_msgs/UUID goal_id\nGo_Feedback feedback\n" + "=" * 80 +
            "\nMSG: unique_identifier_msgs/UUID\nuint8[16] uuid\n" + "=" * 80 +
            "\nMSG: my_pkg/action/Go_Feedback\nfloat32 remaining\n"}))
    assert catalog.add_sidecar(str(tmp_path)) == 1
    assert catalog.ensure("my_pkg/action/Go_FeedbackMessage")


def test_a_type_nobody_defines_is_named_with_the_reason():
    missing = TypeCatalog().missing(["my_pkg/msg/Secret"])
    assert "my_pkg/msg/Secret" in missing
    assert "sidecar" in missing["my_pkg/msg/Secret"]
