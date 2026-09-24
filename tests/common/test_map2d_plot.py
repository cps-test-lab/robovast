# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The map2d config panel draws its occupancy map for a caller without a browser.

The map lives in the campaign, possibly on a cluster, so the panel reads it through the
reader it is given: the YAML first, then the image the YAML names beside it.
"""

import io

import pytest

pytest.importorskip("matplotlib")
PIL = pytest.importorskip("PIL.Image")

from robovast_nav.panels import Map2DPanelType  # noqa: E402  # pylint: disable=wrong-import-position


def _files(image="room.pgm"):
    raster = PIL.new("L", (20, 10), 254)
    buf = io.BytesIO()
    raster.save(buf, format="PPM" if image.endswith(".pgm") else "PNG")
    return {
        "_config/maps/room.yaml": (f"image: {image}\nresolution: 0.1\n"
                                   "origin: [-1.0, -0.5, 0.0]\nnegate: 0\n"
                                   "occupied_thresh: 0.65\nfree_thresh: 0.196\n").encode(),
        f"_config/maps/{image}": buf.getvalue(),
    }


def _axes():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt.subplots()


def test_the_panel_declares_the_role_its_web_panel_reads():
    assert Map2DPanelType.FILE_ROLE == "map"


def test_the_map_is_drawn_top_down_at_its_world_extent():
    fig, ax = _axes()
    files = _files()
    assert Map2DPanelType.plot(ax, "_config/maps/room.yaml", "xy", files.__getitem__) is True
    (image,) = ax.get_images()
    assert image.get_extent() == pytest.approx([-1.0, 1.0, -0.5, 0.5])
    fig.clf()


def test_an_image_in_a_subdirectory_keeps_its_relative_path():
    fig, ax = _axes()
    files = _files("raster/room.png")
    assert Map2DPanelType.plot(ax, "_config/maps/room.yaml", "xy", files.__getitem__) is True
    fig.clf()


def test_a_side_projection_is_declined_not_drawn_wrong():
    fig, ax = _axes()
    assert Map2DPanelType.plot(ax, "_config/maps/room.yaml", "xz", _files().__getitem__) is False
    assert ax.get_images() == []
    fig.clf()


def test_an_image_named_by_a_host_path_is_refused():
    fig, ax = _axes()
    files = _files()
    files["_config/maps/room.yaml"] = b"image: /home/user/room.pgm\nresolution: 0.1\n"
    with pytest.raises(ValueError, match="absolute"):
        Map2DPanelType.plot(ax, "_config/maps/room.yaml", "xy", files.__getitem__)
    fig.clf()


def test_an_image_outside_the_maps_directory_is_refused():
    """The image is read from the campaign beside the map and written beside a temp copy of
    it, so a path that climbs out reads and writes outside both."""
    fig, ax = _axes()
    files = _files()
    files["_config/maps/room.yaml"] = b"image: ../../../etc/passwd\nresolution: 0.1\n"
    with pytest.raises(ValueError, match="outside its own directory"):
        Map2DPanelType.plot(ax, "_config/maps/room.yaml", "xy", files.__getitem__)
    fig.clf()
