# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""The nav_search example seeds AMCL where the robot spawns.

``empty_room``'s map and world share one frame, so the pose AMCL starts from, the spawn pose of
the world file and the ``start_pose`` every campaign hands ``init_nav2`` are one pose.
"""

from pathlib import Path

import pytest
import yaml

EXAMPLE = Path(__file__).resolve().parents[2] / 'configs' / 'examples' / 'nav_search'


def _spawn():
    world = yaml.safe_load((EXAMPLE / 'world' / 'empty_room.yaml').read_text())
    for entry in world['components']:
        if 'spawn_robot' in entry:
            pose = entry['spawn_robot']['pose']
            return pose['position']['x'], pose['position']['y'], pose['orientation']['yaw']
    raise AssertionError('world/empty_room.yaml spawns no robot')


def _start_poses(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == 'start_pose':
                yield value
            else:
                yield from _start_poses(value)
    elif isinstance(node, list):
        for value in node:
            yield from _start_poses(value)


def test_amcl_starts_from_the_spawn_pose():
    params = yaml.safe_load((EXAMPLE / 'files' / 'nav2_params.yaml').read_text())
    seed = params['amcl']['ros__parameters']['initial_pose']
    assert (seed['x'], seed['y'], seed['yaw']) == pytest.approx(_spawn())


@pytest.mark.parametrize('vast', sorted(EXAMPLE.glob('*.vast')), ids=lambda p: p.name)
def test_each_campaign_starts_the_robot_where_it_spawns(vast):
    poses = list(_start_poses(yaml.safe_load(vast.read_text())))
    x, y, _ = _spawn()
    assert poses, f'{vast.name} sets no start_pose'
    for pose in poses:
        assert (pose['position']['x'], pose['position']['y']) == pytest.approx((x, y))
