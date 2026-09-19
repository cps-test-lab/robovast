# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Several kinds of bag are converted in one pass: one command, one group per bag directory.

A run carries its own bag and the infrastructure bag, and converting them in two passes put a
second scan and a second pool after the first pass's long tail. One command carrying both
groups lets the second kind's bags fill the workers the first kind would leave idle.
"""

import json

import pytest

from robovast.results_processing.data.rosbags_common import (ConversionGroup, bag_bytes,
                                                             parse_conversion_groups)
from robovast.results_processing.postprocessing import _batch_rosbags_commands
from robovast.results_processing.postprocessing_plugins import DEFAULT_BAG_DIR, conversion_groups


def _groups_of(commands):
    converts = [c for c in commands if isinstance(c, dict) and "rosbags_process" in c]
    assert len(converts) == 1, f"expected one conversion, got {converts}"
    return converts[0]["rosbags_process"]["groups"]


# -- the batching -------------------------------------------------------------

def test_every_kind_of_bag_lands_in_one_command_at_the_first_rosbag_slot():
    commands = _batch_rosbags_commands([
        "before",
        {"rosbags_tf_to_csv": {"frames": "all"}},
        "between",
        {"rosbags_to_csv": {"topics": ["/collision"]}},
    ])
    assert commands[0] == "before"
    assert "rosbags_process" in commands[1]
    assert commands[2:] == ["between"]
    groups = _groups_of(commands)
    assert [g["bag_dir"] for g in groups] == ["rosbag2", "logs/rosout_bag"]
    assert [p["type"] for p in groups[0]["plugins"]] == ["tf_to_csv", "to_csv"]
    assert [p["type"] for p in groups[1]["plugins"]] == ["rosout_to_csv", "clock_to_csv"]


def test_a_per_command_bag_dir_is_its_own_group():
    groups = _groups_of(_batch_rosbags_commands([
        {"rosbags_to_csv": {"topics": ["/a"]}},
        {"rosbags_to_csv": {"topics": ["/b"], "bag_dir": "other_bag"}},
    ], skip={"rosbags_rosout_to_csv", "rosbags_clock_to_csv"}))
    assert {g["bag_dir"]: g["plugins"] for g in groups} == {
        "rosbag2": [{"type": "to_csv", "topics": ["/a"]}],
        "other_bag": [{"type": "to_csv", "topics": ["/b"]}],
    }


def test_the_infrastructure_bag_alone_is_still_one_command():
    groups = _groups_of(_batch_rosbags_commands(["run_log"]))
    assert [g["bag_dir"] for g in groups] == ["logs/rosout_bag"]


def test_with_everything_skipped_there_is_no_conversion():
    commands = _batch_rosbags_commands(
        ["run_log"], skip={"rosbags_rosout_to_csv", "rosbags_clock_to_csv"})
    assert commands == ["run_log"]


# -- the plugin's two spellings -------------------------------------------------

def test_plugins_is_one_group_in_the_default_bag_dir():
    assert conversion_groups([{"type": "to_csv"}]) == [
        {"bag_dir": DEFAULT_BAG_DIR, "plugins": [{"type": "to_csv"}]}]


def test_plugins_with_a_bag_dir_is_one_group_there():
    assert conversion_groups([{"type": "rosout_to_csv"}], "logs/rosout_bag") == [
        {"bag_dir": "logs/rosout_bag", "plugins": [{"type": "rosout_to_csv"}]}]


def test_groups_pass_through():
    groups = [{"bag_dir": "a", "plugins": [{"type": "to_csv"}]}]
    assert conversion_groups(groups=groups) == groups


@pytest.mark.parametrize("kwargs", [
    {},
    {"plugins": []},
    {"groups": []},
    {"plugins": [{"type": "to_csv"}], "groups": [{"bag_dir": "a", "plugins": []}]},
    {"bag_dir": "a", "groups": [{"bag_dir": "a", "plugins": [{"type": "to_csv"}]}]},
])
def test_an_argument_that_would_be_dropped_is_refused(kwargs):
    with pytest.raises(ValueError):
        conversion_groups(**kwargs)


# -- the script's side ----------------------------------------------------------

def test_the_script_reads_the_groups_it_is_given():
    text = json.dumps({"groups": [
        {"bag_dir": "rosbag2", "plugins": [{"type": "tf_to_csv"}]},
        {"bag_dir": "logs/rosout_bag", "plugins": [{"type": "rosout_to_csv"}]},
    ]})
    assert parse_conversion_groups(text) == [
        ConversionGroup("rosbag2", [{"type": "tf_to_csv"}]),
        ConversionGroup("logs/rosout_bag", [{"type": "rosout_to_csv"}]),
    ]


@pytest.mark.parametrize("text", [
    "not json",
    json.dumps({"plugins": [{"type": "to_csv"}]}),
    json.dumps({"groups": []}),
    json.dumps({"groups": [{"plugins": [{"type": "to_csv"}]}]}),
    json.dumps({"groups": [{"bag_dir": "a", "plugins": []}]}),
    json.dumps({"groups": [{"bag_dir": "a", "plugins": [{"topics": []}]}]}),
    json.dumps({"groups": [{"bag_dir": "a", "plugins": [{"type": "to_csv"}]},
                           {"bag_dir": "a", "plugins": [{"type": "tf_to_csv"}]}]}),
])
def test_the_script_refuses_a_config_it_cannot_follow(text):
    with pytest.raises(ValueError):
        parse_conversion_groups(text)


def test_bag_bytes_sums_every_file_of_the_bag(tmp_path):
    bag = tmp_path / "rosbag2"
    (bag / "sub").mkdir(parents=True)
    (bag / "a.mcap").write_bytes(b"x" * 10)
    (bag / "sub" / "b").write_bytes(b"x" * 5)
    assert bag_bytes(str(bag)) == 15


def test_the_command_passes_the_groups_and_no_bag_dir_flag():
    from robovast.results_processing.postprocessing_plugins import ImageContext, RosbagsProcess

    groups = [{"bag_dir": "rosbag2", "plugins": [{"type": "tf_to_csv"}]},
              {"bag_dir": "logs/rosout_bag", "plugins": [{"type": "rosout_to_csv"}]}]
    argv = RosbagsProcess().image_command(ImageContext(campaign_dir="/campaign/c1"),
                                          groups=groups)
    assert json.loads(argv[argv.index("--config") + 1]) == {"groups": groups}
    assert "--bag-dir" not in argv
