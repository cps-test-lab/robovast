# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""The nav_search example's map holds the room AMCL localizes in.

The built-in ``empty_room`` is a 10 m square whose perimeter walls have their inner faces at
+-5 m. A map without them gives AMCL nothing to match a scan against: its estimate then drifts
along the route by a fraction of the distance driven while Nav2 reports the goal reached, and an
arrival measured in ground truth misses it by as much.
"""

from pathlib import Path

import yaml

FILES = Path(__file__).resolve().parents[2] / 'configs' / 'examples' / 'nav_search' / 'files'


def _map():
    meta = yaml.safe_load((FILES / 'empty_room.yaml').read_text())
    data = (FILES / meta['image']).read_bytes()
    magic, width, height, _maxval, pixels = data.split(maxsplit=4)
    assert magic == b'P5'
    width, height = int(width), int(height)
    return meta, width, height, pixels[:width * height]


def _cell(meta, height, x, y):
    res = float(meta['resolution'])
    ox, oy = float(meta['origin'][0]), float(meta['origin'][1])
    col = int((x - ox) / res)
    row = height - 1 - int((y - oy) / res)
    return row, col


def test_the_map_covers_the_room_and_its_walls():
    meta, width, height, _ = _map()
    res = float(meta['resolution'])
    ox, oy = float(meta['origin'][0]), float(meta['origin'][1])
    assert ox < -5.1 and oy < -5.1
    assert ox + width * res > 5.1 and oy + height * res > 5.1


def test_each_perimeter_wall_is_occupied_and_the_room_is_free():
    meta, width, height, pixels = _map()

    def value(x, y):
        row, col = _cell(meta, height, x, y)
        return pixels[row * width + col]

    res = float(meta['resolution'])
    across = [5.0 - res + k * res for k in range(5)]     # from just inside the face outward
    for along in (-3.0, 0.0, 3.0):
        for name, cells in (('+x', [(d, along) for d in across]), ('-x', [(-d, along) for d in across]),
                            ('+y', [(along, d) for d in across]), ('-y', [(along, -d) for d in across])):
            assert any(value(x, y) == 0 for x, y in cells), f'no {name} wall at {along}'
    for x, y in ((0.0, 0.0), (-3.0, 0.0), (4.5, 4.5), (-4.8, 2.0)):
        assert value(x, y) == 254, f'({x}, {y}) is not free'
