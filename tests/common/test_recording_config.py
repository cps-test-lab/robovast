# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The ``recording:`` block: what it accepts, and what the run is told because of it.

The block is the only place a campaign says what its runs' bags hold, and the
``RECORD_*`` environment is the only way that reaches the entrypoint's recorder. So the
two are tested together: a shape the model refuses can never reach a run, and a value it
accepts must come out the other end spelled the way ``ros2 bag record`` reads it.
"""

import pytest
from pydantic import ValidationError

from robovast.common.config import (RESERVED_ENV_NAMES, ConfigV1, RecordingConfig,
                                    Ros2RecordingConfig, recording_config)
from robovast.common.execution import scenario_env

_EXECUTION = {"containers": {"scenario": {"image": "x:1"}}, "runs": 1,
              "scenario_file": "s.osc"}


def _env(recording=None):
    return scenario_env({"execution": dict(_EXECUTION), "scenario_file": "s.osc",
                         "recording": recording})


# -- the model -------------------------------------------------------------------------

def test_an_empty_block_records_everything():
    cfg = RecordingConfig.model_validate({"ros2": {}, "roqsim": {}})
    assert cfg.ros2.topics == "all" and cfg.ros2.exclude == [] and cfg.ros2.exclude_types == []
    assert cfg.ros2.use_sim_time is False
    assert cfg.roqsim.tracks == "all" and cfg.roqsim.exclude == [] and cfg.roqsim.rate_hz is None


def test_the_block_is_optional_on_the_config_and_absent_by_default():
    config = ConfigV1(execution=_EXECUTION)
    assert config.recording is None
    config = ConfigV1(execution=_EXECUTION, recording={"ros2": {"use_sim_time": True}})
    assert config.recording.ros2.use_sim_time is True


@pytest.mark.parametrize("block", [
    {"ros3": {}},                                   # a recorder nobody has
    {"ros2": {"topic": "/odom"}},                   # the singular is not the key
    {"roqsim": {"fps": 25}},                        # nor is a synonym
])
def test_an_unknown_key_is_refused(block):
    with pytest.raises(ValidationError):
        RecordingConfig.model_validate(block)


@pytest.mark.parametrize("ros2", [
    {"topics": []},                                 # nothing to record is not a choice
    {"topics": [""]},
    {"topics": ["/odom", "/a b"]},                  # would split into two on the way
    {"topics": "some"},                             # the only word is 'all'
    {"exclude": [""]},
    {"exclude_types": ["sensor_msgs/msg/Image "]},
])
def test_a_bad_ros2_shape_is_refused(ros2):
    with pytest.raises(ValidationError):
        RecordingConfig.model_validate({"ros2": ros2})


@pytest.mark.parametrize("roqsim", [
    {"rate_hz": 0},
    {"rate_hz": -5},
    {"tracks": []},
    {"tracks": ["robot"]},                          # names no track: no '/'
    {"tracks": ["robot/**,box/**"]},                # the separator it travels on
    {"exclude": ["wheel"]},
])
def test_a_bad_roqsim_shape_is_refused(roqsim):
    with pytest.raises(ValidationError):
        RecordingConfig.model_validate({"roqsim": roqsim})


def test_recording_config_reads_the_raw_block_once():
    assert recording_config(None) is None
    cfg = recording_config({"roqsim": {"rate_hz": 25, "tracks": ["robot/**"]}})
    assert cfg.roqsim.rate_hz == 25 and cfg.roqsim.tracks == ["robot/**"]
    assert recording_config(cfg) is cfg
    with pytest.raises(ValueError, match="mapping"):
        recording_config(["ros2"])


# -- what the run is told ----------------------------------------------------------------

def test_an_absent_block_is_stated_as_recording_everything():
    """Stated, not left to the entrypoint's default: the compose file / pod spec says
    outright what the run recorded."""
    env = _env(None)
    assert env["RECORD_TOPICS"] == "all"
    assert env["RECORD_EXCLUDE"] == ""
    assert env["RECORD_EXCLUDE_TYPES"] == ""
    assert env["RECORD_USE_SIM_TIME"] == "false"
    # The infrastructure bag is untouched by any of it.
    assert env["LOG_TOPICS"] == "/rosout /clock"


def test_a_block_is_spelled_the_way_the_recorder_reads_it():
    env = _env({"ros2": {"topics": ["/odom", "^/tf", "/scan"],
                         "exclude": ["^/camera/", "_raw$"],
                         "exclude_types": ["sensor_msgs/msg/Image", "sensor_msgs/msg/PointCloud2"],
                         "use_sim_time": True}})
    assert env["RECORD_TOPICS"] == "/odom ^/tf /scan"       # names and regexes, as written
    assert env["RECORD_EXCLUDE"] == "^/camera/|_raw$"       # one regex for --exclude-regex
    assert env["RECORD_EXCLUDE_TYPES"] == "sensor_msgs/msg/Image sensor_msgs/msg/PointCloud2"
    assert env["RECORD_USE_SIM_TIME"] == "true"


def test_a_block_without_a_ros2_section_records_everything():
    env = _env({"roqsim": {"rate_hz": 25}})
    assert env["RECORD_TOPICS"] == "all" and env["RECORD_USE_SIM_TIME"] == "false"


def test_a_model_instance_is_accepted_as_the_block():
    env = _env(RecordingConfig(ros2=Ros2RecordingConfig(use_sim_time=True)))
    assert env["RECORD_USE_SIM_TIME"] == "true"


def test_the_record_variables_are_reserved():
    """A campaign cannot repoint them through execution.env: they are the run's record."""
    for name in ("RECORD_TOPICS", "RECORD_EXCLUDE", "RECORD_EXCLUDE_TYPES",
                 "RECORD_USE_SIM_TIME"):
        assert name in RESERVED_ENV_NAMES
