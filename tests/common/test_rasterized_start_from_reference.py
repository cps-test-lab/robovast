# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""``PathVariationRasterized`` with ``start_from: "@<parameter>"`` starts where that says.

The reference names a pose the configuration already carries -- stated in its
``parameters:`` block or written by an earlier variation -- and every path of that
configuration starts there. A configuration that carries no such pose is refused by name.
"""

import struct
import textwrap

import pytest

from robovast.common.config_generation import generate_scenario_variations
from robovast_nav.data_model import Pose

_CELLS = 30
_RES = 0.05

_SCENARIO = """\
import osc.robotics

scenario nav:
    start_pose: pose_3d = pose_3d()
    goal_pose: pose_3d = pose_3d()
    do serial:
        wait elapsed(1s)
"""


def _project(tmp_path, parameters):
    (tmp_path / "room.pgm").write_bytes(
        b"P5\n%d %d\n255\n" % (_CELLS, _CELLS) + struct.pack("B", 255) * (_CELLS * _CELLS))
    (tmp_path / "room.yaml").write_text(textwrap.dedent(f"""\
        image: room.pgm
        resolution: {_RES}
        origin: [0.0, 0.0, 0.0]
        negate: 0
        occupied_thresh: 0.65
        free_thresh: 0.196
        """))
    (tmp_path / "scenario.osc").write_text(_SCENARIO)
    vast = tmp_path / "campaign.vast"
    vast.write_text(textwrap.dedent(f"""\
        version: 7
        metadata: {{name: rasterized-start-from-test}}
        configuration:
        - name: grid
          {parameters}
          variations:
          - PathVariationRasterized:
              scenario: {{start: start_pose, goal: goal_pose}}
              start_from: "@start_pose"
              map_file: room.yaml
              raster_size: 0.5
              path_length: 0.5
              path_length_tolerance: 0.3
              robot_diameter: 0.2
        execution:
          containers:
            scenario: {{image: scen:latest}}
          runs: 1
          scenario_file: scenario.osc
        """))
    return vast


def test_every_path_starts_at_the_referenced_pose(tmp_path):
    vast = _project(tmp_path, "parameters: {scenario: {start_pose: "
                              "{position: {x: 0.25, y: 0.25}, orientation: {yaw: 0.5}}}}")
    configs = generate_scenario_variations(str(vast), use_cache=False)["configs"]

    assert configs
    for config in configs:
        start = Pose.from_any(config["config"]["start_pose"])
        assert (start.position.x, start.position.y, start.orientation.yaw) == (0.25, 0.25, 0.5)


def test_a_configuration_without_the_referenced_pose_is_refused_by_name(tmp_path):
    vast = _project(tmp_path, "")
    with pytest.raises(Exception, match="start_pose"):
        generate_scenario_variations(str(vast), use_cache=False)
