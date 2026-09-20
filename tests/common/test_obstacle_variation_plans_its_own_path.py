# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""Obstacles can be placed without a path variation ahead of them.

A campaign that fixes its start and goal outright -- because the trial under test is that one
route, not a sampled family of them -- states them in `parameters.scenario` and runs the obstacle
variation alone. That was always the design: the placement plans its own path when no `_path` is
in the config. Two things stopped it. The poses arrived as the mappings YAML parsed rather than
as `Pose`, which only the trigger subclass converted; and `amount_per_m` demanded a `_path_length`
that only a path variation published -- a length that is just the arc length of the path being
planned three lines above.
"""

import pytest

from robovast_nav.data_model import Position
from robovast_nav.variation.obstacle_variation import ObstacleVariation, ObstacleVariationConfig

BOX = [0.5, 0.5, 1.0]

#: The stub planner's corridor: 60 points, 0.25 m apart, so 14.75 m of path.
PATH_LENGTH_M = 59 * 0.25


class _Path:
    def __init__(self, *_args, **_kwargs):
        pass

    def generate_path(self, *_args, **_kwargs):
        return [Position(x=float(i) * 0.25, y=0.0) for i in range(60)]


def _corridor_map(tmp_path, half_width_m=1.6):
    import numpy as np
    from PIL import Image

    resolution, origin = 0.05, [-1.0, -2.0, 0.0]
    width, height = 340, 80
    world_y = (height - np.arange(height)) * resolution + origin[1]
    free = np.abs(world_y) <= half_width_m
    Image.fromarray(np.where(free[:, None], 255, 0).astype(np.uint8).repeat(width, axis=1),
                    mode='L').save(tmp_path / 'map.pgm')
    map_file = tmp_path / 'map.yaml'
    map_file.write_text(f"image: map.pgm\nresolution: {resolution}\norigin: {origin}\n")
    return map_file


@pytest.fixture
def variation(monkeypatch, tmp_path):
    import robovast_nav.variation.obstacle_variation as mod

    monkeypatch.setattr(mod, 'PathGenerator', _Path)
    map_file = _corridor_map(tmp_path)

    def _make(**oc):
        # pylint: disable-next=no-value-for-parameter
        v = ObstacleVariation.__new__(ObstacleVariation)
        v.parameters = ObstacleVariationConfig(
            scenario={'objects': 'static_objects'},
            reads={'start': 'start_pose', 'goal': 'goal_poses'},
            obstacle_configs=[{'max_distance': 0.7, 'model': 'file:///box.sdf.xacro',
                               'size': BOX, **oc}],
            seed=42, robot_diameter=0.35)
        v._config_child_indices = {}
        v.progress_update = lambda *_a, **_k: None
        v.get_map_file = lambda *_a, **_k: str(map_file)
        return v
    return _make


def _stated_config():
    """What a campaign writes in `parameters.scenario`: mappings, and no orientation."""
    return {'name': 'cfg', 'config': {
        'start_pose': {'position': {'x': 0.0, 'y': 0.0}},
        'goal_poses': [{'position': {'x': 14.0, 'y': 0.0}}],
    }}


def _placed(result):
    return result['config']['static_objects']


def test_poses_the_campaign_stated_are_enough_to_place(variation):
    """No `_path`, no path variation, poses as YAML mappings -- the placement plans its own."""
    v = variation(amount=2)
    result = v._generate_obstacles_for_config([], _stated_config(), v.parameters.obstacle_configs)
    assert len(_placed(result[0])) == 2


def test_the_planned_path_is_published_for_the_next_variation(variation):
    v = variation(amount=1)
    result = v._generate_obstacles_for_config([], _stated_config(), v.parameters.obstacle_configs)
    assert result[0]['_path']


@pytest.mark.parametrize('per_m,expected', [(0.0, 0), (0.1, 1), (0.2, 2)])
def test_a_density_resolves_against_the_path_it_plans(variation, per_m, expected):
    """`amount_per_m` used to require a length published by a path variation. The length is the
    arc length of the path in hand, so the count is the same number either way."""
    import math
    assert expected == math.floor(per_m * PATH_LENGTH_M)

    v = variation(amount_per_m=per_m)
    result = v._generate_obstacles_for_config([], _stated_config(), v.parameters.obstacle_configs)
    assert len(_placed(result[0])) == expected


def test_a_density_measures_the_inherited_path_when_one_was_handed_over(variation):
    """A path variation ahead of this one hands over `_path`; the density is measured on THAT
    path, not on one re-planned from the same waypoints."""
    v = variation(amount_per_m=0.5)
    short = [Position(x=0.0, y=0.0), Position(x=2.0, y=0.0)]
    config = _stated_config() | {'_path': short}
    result = v._generate_obstacles_for_config([], config, v.parameters.obstacle_configs)
    assert len(_placed(result[0])) == 1  # floor(0.5 * 2.0), not floor(0.5 * 14.75)


def test_an_absent_start_is_refused_rather_than_placed_from_the_origin(variation):
    v = variation(amount=1)
    config = {'name': 'cfg', 'config': {'goal_poses': [{'position': {'x': 14.0, 'y': 0.0}}]}}
    with pytest.raises(ValueError, match='start_pose'):
        v._generate_obstacles_for_config([], config, v.parameters.obstacle_configs)
