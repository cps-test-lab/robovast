# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""The path cache is keyed on the map image as well as the map YAML that names it."""

import os
import struct
import time

import pytest

from robovast.common.config_generation import generate_scenario_variations
from robovast_nav.map_loader import map_files

from .test_path_variation_cache import _CELLS, _project

_HIT = "Using cached start/goal poses"


def _compose(vast, out):
    messages = []
    generate_scenario_variations(str(vast), progress_update_callback=messages.append,
                                 output_dir=str(out), use_cache=False)
    return messages


def _redraw_image(tmp_path):
    """The same room with an inner wall, same size, later mtime; the YAML is untouched."""
    side = _CELLS + 2
    wall = [0 if (x in (0, side - 1) or y in (0, side - 1) or x == side // 2) else 254
            for y in range(side) for x in range(side)]
    image = tmp_path / "room.pgm"
    image.write_bytes(b"P5\n%d %d\n255\n" % (side, side) + struct.pack(f"{len(wall)}B", *wall))
    later = time.time() + 5
    os.utime(image, (later, later))


def test_an_unchanged_map_is_served_from_the_cache(tmp_path):
    vast = _project(tmp_path)
    _compose(vast, tmp_path / "out")
    assert any(_HIT in m for m in _compose(vast, tmp_path / "out"))


def test_a_changed_map_image_is_not_served_from_the_cache(tmp_path):
    vast = _project(tmp_path)
    _compose(vast, tmp_path / "out")
    _redraw_image(tmp_path)
    assert not any(_HIT in m for m in _compose(vast, tmp_path / "out"))


def test_a_map_whose_image_is_missing_is_refused(tmp_path):
    (tmp_path / "room.yaml").write_text("image: room.pgm\nresolution: 0.05\n")
    with pytest.raises(FileNotFoundError, match="room.pgm"):
        map_files(str(tmp_path / "room.yaml"))
