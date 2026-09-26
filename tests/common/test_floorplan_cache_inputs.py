# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""The floorplan cache is keyed on every file the build reads, not on the entry file alone.

A ``.variation`` imports sibling ``.fpm`` files, and the whole directory is staged for the
container because of that. Keyed on the ``.variation`` alone, an edited ``rooms.fpm`` left
the cache valid and the campaign composed on the previous map.
"""

import os
import time

from robovast.common.file_cache import FileCache
from robovast_nav.floorplan_generation import cache_inputs


def _project(tmp_path):
    (tmp_path / "hexagon.variation").write_text('import "rooms.fpm"\n')
    (tmp_path / "rooms.fpm").write_text("room a\n")
    (tmp_path / "walls.fpm").write_text("wall w\n")
    (tmp_path / "notes.txt").write_text("not a model\n")
    return tmp_path / "hexagon.variation"


def test_the_inputs_are_the_model_and_every_model_file_beside_it(tmp_path):
    entry = _project(tmp_path)
    assert cache_inputs(str(entry)) == [str(entry), str(tmp_path / "rooms.fpm"),
                                        str(tmp_path / "walls.fpm")]


def test_editing_an_imported_file_changes_the_key(tmp_path):
    entry = _project(tmp_path)
    cache = FileCache(str(tmp_path), "floorplan_variation", [entry.name])
    before = cache.create_input_files_hash(cache_inputs(str(entry)), strings_for_hash=[])

    (tmp_path / "rooms.fpm").write_text("room a\nroom b\n")
    os.utime(tmp_path / "rooms.fpm", (time.time() + 5, time.time() + 5))

    after = cache.create_input_files_hash(cache_inputs(str(entry)), strings_for_hash=[])
    assert after != before
