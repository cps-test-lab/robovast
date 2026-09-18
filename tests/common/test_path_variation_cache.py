# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""A path read back from the cache is the same path as the one just generated.

``PathVariationRandom`` caches each generated path under its parameters and seed, so a
repeated draw -- the same ``path_length`` across two cells, or a second composition into
the same output directory -- is read back rather than searched for again. The cache-hit
branch and the fresh branch of ``generate_path_for_config`` are two return statements for
one caller; a shape that differs between them only shows on the second draw, which is
where a one-pass test never looks.

The composition-level ``use_cache`` is a different cache: the path cache lives in the
output directory regardless, so the second pass here goes through it.
"""

import struct
import textwrap

from robovast.common.config_generation import generate_scenario_variations

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


def _write_map(tmp_path):
    """A walled 1.5 x 1.5 m room, as a PGM plus the YAML that describes it."""
    side = _CELLS + 2
    wall = [0 if (x in (0, side - 1) or y in (0, side - 1)) else 254
            for y in range(side) for x in range(side)]
    (tmp_path / "room.pgm").write_bytes(
        b"P5\n%d %d\n255\n" % (side, side) + struct.pack(f"{len(wall)}B", *wall))
    (tmp_path / "room.yaml").write_text(
        f"image: room.pgm\nresolution: {_RES}\norigin: [0.0, 0.0, 0.0]\n"
        "negate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n")


def _project(tmp_path):
    _write_map(tmp_path)
    (tmp_path / "scenario.osc").write_text(_SCENARIO)
    vast = tmp_path / "campaign.vast"
    vast.write_text(textwrap.dedent("""\
        version: 4
        metadata: {name: path-cache-test}
        configuration:
        - name: cell
          variations:
          - PathVariationRandom:
              scenario: {start: start_pose, goal: goal_pose}
              map_file: room.yaml
              path_length: 0.8
              num_paths: 1
              num_goal_poses: 1
              min_distance: 0.2
              seed: 1
              robot_diameter: 0.2
        execution:
          containers:
            scenario: {image: scen:latest}
          runs: 1
          scenario_file: scenario.osc
        """))
    return vast


def _path_draw(data):
    (config,) = data["configs"]
    return config["config"]["start_pose"], config["config"]["goal_pose"], config["_path"]


def test_a_cached_path_composes_like_a_fresh_one(tmp_path):
    """Composing twice into one output directory: the first pass generates and caches the
    path, the second reads it back. Both must produce the one config, with the one draw."""
    vast = _project(tmp_path)
    out = tmp_path / "out"

    fresh = generate_scenario_variations(str(vast), output_dir=str(out), use_cache=False)
    cached = generate_scenario_variations(str(vast), output_dir=str(out), use_cache=False)

    assert _path_draw(cached) == _path_draw(fresh)
