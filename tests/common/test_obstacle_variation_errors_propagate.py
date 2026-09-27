# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""A fault in the placer or the planner ends the composition; it is not an infeasible draw.

``VariationInfeasibleError`` is the one exception a search drops a draw for, so a bug
raised as that type reads as "this draw cannot be realized" and a search spends its budget
around it instead of stopping on it.
"""

import pytest

from robovast_nav.variation.obstacle_variation import ObstacleVariation, ObstacleVariationConfig
from tests.common.test_obstacle_variation_plans_its_own_path import (_Path, _corridor_map,
                                                                      _stated_config)


@pytest.fixture
def variation(monkeypatch, tmp_path):
    import robovast_nav.variation.obstacle_variation as mod

    monkeypatch.setattr(mod, 'PathGenerator', _Path)
    map_file = _corridor_map(tmp_path)
    # pylint: disable-next=no-value-for-parameter
    v = ObstacleVariation.__new__(ObstacleVariation)
    v.parameters = ObstacleVariationConfig(
        scenario={'objects': 'static_objects'},
        reads={'start': 'start_pose', 'goal': 'goal_poses'},
        obstacle_configs=[{'max_distance': 0.7, 'model': 'file:///box.sdf.xacro',
                           'size': [0.5, 0.5, 1.0], 'amount': 1}],
        seed=42, robot_diameter=0.35)
    v._config_child_indices = {}
    v.progress_update = lambda *_a, **_k: None
    v.get_map_file = lambda *_a, **_k: str(map_file)
    return v


def test_a_placer_that_raises_ends_the_composition_with_its_own_error(variation, monkeypatch):
    import robovast_nav.variation.obstacle_variation as mod

    def _bug(self, *_a, **_k):
        raise TypeError("place_obstacles() got an unexpected keyword argument")

    monkeypatch.setattr(mod.ObstaclePlacer, 'place_obstacles', _bug)
    with pytest.raises(TypeError, match="unexpected keyword"):
        variation._generate_obstacles_for_config([], _stated_config(),
                                                 variation.parameters.obstacle_configs)



def test_a_planner_error_that_is_not_a_refused_waypoint_ends_the_composition(variation,
                                                                             monkeypatch):
    import robovast_nav.variation.obstacle_variation as mod

    class _NoGrid(_Path):
        def generate_path(self, *_args, **_kwargs):
            raise ValueError("No waypoints provided.")

    monkeypatch.setattr(mod, 'PathGenerator', _NoGrid)
    with pytest.raises(ValueError, match="No waypoints provided") as exc:
        variation._generate_obstacles_for_config([], _stated_config(),
                                                 variation.parameters.obstacle_configs)
    assert not isinstance(exc.value, mod.VariationInfeasibleError)


def test_a_map_that_does_not_load_raises_from_the_planner(tmp_path):
    from robovast_nav.path_generator import PathGenerator

    broken = tmp_path / "map.yaml"
    broken.write_text("image: missing.pgm\nresolution: 0.05\norigin: [0, 0, 0]\n")
    with pytest.raises(FileNotFoundError, match="Map image file not found"):
        PathGenerator(str(broken))
