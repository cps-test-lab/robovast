# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""A map that does not load raises where it is loaded, with the loader's own error.

A generator or visualizer left without a map fails later with an error about something
else, or quietly draws less.
"""

import pytest

from tests.common.test_path_variation_robot_diameter import _project, _write_map


def _broken_map(tmp_path):
    map_file = _write_map(tmp_path)
    (tmp_path / "room.pgm").unlink()
    return map_file


def test_the_waypoint_generator_raises_a_map_it_cannot_load(tmp_path):
    from robovast_nav.waypoint_generator import WaypointGenerator

    with pytest.raises(FileNotFoundError, match="Map image file not found"):
        WaypointGenerator(str(_broken_map(tmp_path)))


def test_the_map_visualizer_raises_a_map_it_cannot_load(tmp_path):
    pytest.importorskip("matplotlib")
    from robovast_nav.map_visualizer import MapVisualizer

    with pytest.raises(FileNotFoundError, match="Map image file not found"):
        MapVisualizer().load_map(str(_broken_map(tmp_path)))


def test_the_map2d_panel_names_the_map_and_the_loader_error(tmp_path):
    pytest.importorskip("matplotlib")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from robovast_nav.panels import Map2DPanelType

    files = {"maps/room.yaml": b"image: room.pgm\nresolution: 0.1\n",
             "maps/room.pgm": b"not an image"}
    fig, ax = plt.subplots()
    with pytest.raises(ValueError, match=r"could not load the map maps/room.yaml: .*room\.pgm"):
        Map2DPanelType.plot(ax, "maps/room.yaml", "xy", files.__getitem__)
    fig.clf()


_RASTERIZED = """\
  - PathVariationRasterized:
      scenario: {start: start_pose, goal: goal_pose}
      map_file: room.yaml
      raster_size: 0.5
      path_length: 0.8
      robot_diameter: 0.2
"""


@pytest.mark.parametrize("rasterized", [False, True], ids=["random", "rasterized"])
def test_a_path_variation_on_a_map_it_cannot_load_raises_the_load_error(tmp_path, rasterized):
    from robovast.common.config_generation import generate_scenario_variations
    from robovast.common.variation.base_variation import VariationFailed

    vast = _project(tmp_path)
    if rasterized:
        text = vast.read_text()
        start = text.index("  - PathVariationRandom:")
        end = text.index("execution:")
        vast.write_text(text[:start] + _RASTERIZED + text[end:])
    (tmp_path / "room.pgm").unlink()
    with pytest.raises(VariationFailed, match="Map image file not found"):
        generate_scenario_variations(str(vast), use_cache=False)
