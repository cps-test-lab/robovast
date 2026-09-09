# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""A path length the map cannot hold is an infeasible draw, not a broken campaign.

``PathVariationRandom`` exhausting its attempts means exactly one thing: no path of that
length exists on that map. For a stated sweep level that is an error worth stopping for; for
a searched dimension it is an ordinary outcome, because an optimizer proposing a length the
map cannot hold is the optimizer doing its job. The two are told apart by the exception's
TYPE -- ``tolerate_infeasible`` drops a ``VariationInfeasibleError`` and nothing else -- so
raising a plain error here takes every other config in the batch down with it and ends the
search.

The map is built here rather than fixtured: a room too small for the requested path is the
whole setup, and stating its size in the test is what makes the expectation readable.
"""

import struct
import textwrap

import pytest

from robovast.common.config_generation import generate_scenario_variations
from robovast.common.variation.base_variation import VariationInfeasibleError

#: A 1.5 x 1.5 m open room. Small on purpose: the failing path costs `max_attempts`
#: generate-and-plan rounds, and every one of them is over a grid this size.
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


def _map(tmp_path):
    """An empty room, free everywhere, as a PGM plus the YAML that describes it."""
    (tmp_path / "room.pgm").write_bytes(
        b"P5\n%d %d\n255\n" % (_CELLS, _CELLS) + struct.pack("B", 255) * (_CELLS * _CELLS)
    )
    (tmp_path / "room.yaml").write_text(textwrap.dedent(f"""\
        image: room.pgm
        resolution: {_RES}
        origin: [0.0, 0.0, 0.0]
        negate: 0
        occupied_thresh: 0.65
        free_thresh: 0.196
        """))


def _project(tmp_path, path_length):
    _map(tmp_path)
    (tmp_path / "scenario.osc").write_text(_SCENARIO)
    vast = tmp_path / "campaign.vast"
    vast.write_text(textwrap.dedent(f"""\
        version: 4
        metadata: {{name: path-infeasible-test}}
        configuration:
        - name: ok
        - name: bad
          variations:
          - PathVariationRandom:
              scenario: {{start: start_pose, goal: goal_pose}}
              map_file: room.yaml
              path_length: {path_length}
              path_length_tolerance: 0.5
              num_paths: 1
              num_goal_poses: 1
              min_distance: 0.2
              seed: 42
              robot_diameter: 0.2
        execution:
          containers:
            scenario: {{image: scen:latest}}
          runs: 1
          scenario_file: scenario.osc
        """))
    return vast


#: Far longer than the room's diagonal (~2.1 m), so no draw can satisfy it.
_IMPOSSIBLE = 20.0


def test_an_impossible_path_length_is_infeasible_not_a_bug(tmp_path):
    """The type is the contract: only this one is droppable, so only this one lets a search
    survive a draw its map cannot realize."""
    vast = _project(tmp_path, _IMPOSSIBLE)
    with pytest.raises(VariationInfeasibleError, match="Failed to generate valid path"):
        generate_scenario_variations(str(vast), use_cache=False)


def test_a_search_drops_the_draw_and_keeps_the_rest(tmp_path):
    """What the fix buys: the unrealizable config is dropped and its siblings still compose,
    instead of one bad proposal ending the campaign."""
    vast = _project(tmp_path, _IMPOSSIBLE)
    data = generate_scenario_variations(str(vast), use_cache=False, tolerate_infeasible=True)
    assert [c["name"] for c in data["configs"]] == ["ok"]


def test_a_reachable_path_length_still_composes(tmp_path):
    """The guard above must not be passing because every draw fails."""
    vast = _project(tmp_path, 1.0)
    data = generate_scenario_variations(str(vast), use_cache=False)
    assert sorted(c["_config_name"] for c in data["configs"]) == ["bad", "ok"]
