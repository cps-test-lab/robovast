# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""The sampler and the planner must be asking about the same robot.

``PathVariationRandom`` samples waypoints against ``robot_diameter`` clearance and then
plans between them on a grid the planner inflates by ITS robot. Where the two differ the
planner refuses a pose the sampler just produced, and it says so with a plain error --
which is not a class composition may skip, so one unlucky pose ends the whole campaign,
every other config in the batch with it.

Two ways they can differ, and this covers both: the planner not being told the diameter
at all, and the two clearance tests disagreeing near a wall. The first is closed by
passing it; the second cannot be closed by construction -- a disc of cells and a distance
transform are not the same test -- so a rejected waypoint has to be an ordinary failed
attempt, which the generator's loop already knows how to redraw from.
"""

import struct
import textwrap

from robovast.common.config_generation import generate_scenario_variations

#: A 1.5 x 1.5 m room, walled in. Small enough that a sampled pose is routinely within a
#: robot's radius of a wall, which is where the two clearance tests can disagree.
_CELLS = 30
_RES = 0.05

#: Half the planner's own default. With the diameter not passed through, the sampler
#: accepts poses inside the planner's inflated walls and the composition dies on one.
_ROBOT_DIAMETER = 0.2

_SCENARIO = """\
import osc.robotics

scenario nav:
    start_pose: pose_3d = pose_3d()
    goal_pose: pose_3d = pose_3d()
    do serial:
        wait elapsed(1s)
"""


def _write_map(tmp_path):
    """The room, as a PGM plus the YAML that describes it."""
    side = _CELLS + 2
    wall = [0 if (x in (0, side - 1) or y in (0, side - 1)) else 254
            for y in range(side) for x in range(side)]
    (tmp_path / "room.pgm").write_bytes(
        b"P5\n%d %d\n255\n" % (side, side) + struct.pack(f"{len(wall)}B", *wall))
    (tmp_path / "room.yaml").write_text(
        f"image: room.pgm\nresolution: {_RES}\norigin: [0.0, 0.0, 0.0]\n"
        "negate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n")
    return tmp_path / "room.yaml"


def _project(tmp_path):
    _write_map(tmp_path)
    (tmp_path / "scenario.osc").write_text(_SCENARIO)
    vast = tmp_path / "campaign.vast"
    vast.write_text(textwrap.dedent(f"""\
        version: 4
        metadata: {{name: robot-diameter-test}}
        configuration:
        - name: cell
          variations:
          - PathVariationRandom:
              scenario: {{start: start_pose, goal: goal_pose}}
              map_file: room.yaml
              path_length: 0.8
              num_paths: 1
              num_goal_poses: 1
              min_distance: 0.2
              seed: 1
              robot_diameter: {_ROBOT_DIAMETER}
        execution:
          containers:
            scenario: {{image: scen:latest}}
          runs: 1
          scenario_file: scenario.osc
        """))
    return vast


def test_a_robot_smaller_than_the_planner_default_still_composes(tmp_path):
    """A robot half the planner's default diameter is an ordinary campaign, not a draw
    the framework may die on."""
    vast = _project(tmp_path)
    data = generate_scenario_variations(str(vast), use_cache=False)
    assert len(data["configs"]) == 1


def test_the_planner_is_given_the_declared_robot(tmp_path):
    """Passing the diameter is what removes the disagreement in the common case; a planner
    left on its default inflates by a robot the campaign never mentioned."""
    from robovast_nav.variation import path_variation

    seen = []
    original = path_variation.PathGenerator

    def _record(map_file_path, robot_diameter=0.4):  # PathGenerator's own default
        seen.append(robot_diameter)
        return original(map_file_path, robot_diameter)

    path_variation.PathGenerator = _record
    try:
        generate_scenario_variations(str(_project(tmp_path)), use_cache=False)
    finally:
        path_variation.PathGenerator = original
    assert seen == [_ROBOT_DIAMETER]


def test_a_waypoint_the_planner_refuses_is_a_redraw(tmp_path):
    """The residual case: the two clearance tests disagree on a pose near a wall. The
    generator redraws, as it does for a path that is too long or not found -- raising
    would end the campaign over one pose nobody asked for."""
    from robovast_nav.variation import path_variation

    original = path_variation.PathGenerator
    calls = {"n": 0}

    class _RefusesFirst:
        def __init__(self, map_file_path, robot_diameter=0.4):
            self._inner = original(map_file_path, robot_diameter)

        def generate_path(self, waypoints, obstacles):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("Invalid waypoint grid position: (19, 4)")
            return self._inner.generate_path(waypoints, obstacles)

    path_variation.PathGenerator = _RefusesFirst
    try:
        data = generate_scenario_variations(str(_project(tmp_path)), use_cache=False)
    finally:
        path_variation.PathGenerator = original
    assert calls["n"] > 1, "the rejected waypoint was not redrawn"
    assert len(data["configs"]) == 1


def test_the_clearance_check_covers_the_whole_robot(tmp_path):
    """A robot wider than the check's old ten-cell ceiling was asked about at ten cells:
    the middle of a 1.5 m room passed for a robot 2 m across, whose footprint covers both
    walls. The planner inflates by the true radius, so it then refuses to start there --
    the same disagreement from the other side, and a pose the robot does not fit in.

    How many cells a radius is depends on the map's resolution, so the ceiling limited
    maps, not robots: at 0.025 m the same ten cells describe a robot half this size.
    """
    from robovast_nav.waypoint_generator import WaypointGenerator

    generator = WaypointGenerator(str(_write_map(tmp_path)))
    middle = _CELLS * _RES / 2

    # 0.1 m radius: two cells, clear of both walls.
    assert generator.is_valid_position(middle, middle, 0.1)
    # 1.0 m radius: twenty cells, and the room is fifteen from the middle to each wall.
    assert not generator.is_valid_position(middle, middle, 1.0)
