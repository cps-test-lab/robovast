"""The costmap video overlay, on a synthetic run: no service, no ROS, no roqsim.

The run carries its own ``costmaps.csv`` and ``poses.csv`` and no recording, so those files
are its ``costmaps`` and ``poses`` tables.
"""

from __future__ import annotations

import base64
import csv
import math
import zlib

import numpy as np
import pytest
import yaml

from robovast_nav import video_overlay as vo
from tests.robovast_data.conftest import write_store


class Placement:
    """What roqsim hands an overlay: a corner, a width as a fraction of the frame, a margin."""

    def __init__(self, anchor="top-right", width=0.3, margin=12):
        self.anchor, self.width, self.margin = anchor, width, margin

    def pixels(self, frame_width):
        return int(round(self.width * frame_width))

    def box(self, frame_w, frame_h, inset_w, inset_h):
        return frame_w - inset_w - self.margin, self.margin


def _grid_payload(cells: np.ndarray) -> str:
    return base64.b64encode(zlib.compress(np.asarray(cells, dtype=np.int8).tobytes(), 9)).decode()


ODOM = (1.0, 0.5, math.pi / 2)  # odom sits translated and turned a quarter in the map frame


@pytest.fixture
def run_dir(tmp_path):
    """<campaign>/<config>/<run>/ with a map, a local costmap in odom, poses and a configuration."""
    run = tmp_path / "campaign" / "cfg" / "0"
    run.mkdir(parents=True)
    write_store(tmp_path / "campaign", {"cfg": {"runs": {0: "passed"}}})

    # /map: 10x8 cells at 0.5 m, origin (-1, -1), an occupied border.
    world = np.zeros((8, 10), dtype=np.int8)
    world[0, :] = world[-1, :] = world[:, 0] = world[:, -1] = 100
    # /local_costmap/costmap: 4x4 at 0.5 m at the odom origin; one lethal cell at col 1, row 2.
    local1 = np.zeros((4, 4), dtype=np.int8)
    local1[2, 1] = 100
    local2 = np.zeros((4, 4), dtype=np.int8)
    with (run / "costmaps.csv").open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["topic", "timestamp", "frame_id", "resolution", "width", "height",
                         "origin_x", "origin_y", "origin_yaw", "data"])
        writer.writerow(["/map", 0.0, "map", 0.5, 10, 8, -1.0, -1.0, 0.0, _grid_payload(world)])
        writer.writerow(["/local_costmap/costmap", 1.0, "odom", 0.5, 4, 4, 0.0, 0.0, 0.0,
                         _grid_payload(local1)])
        writer.writerow(["/local_costmap/costmap", 2.0, "odom", 0.5, 4, 4, 0.0, 0.0, 0.0,
                         _grid_payload(local2)])

    def quat(yaw):
        return 0.0, 0.0, math.sin(yaw / 2), math.cos(yaw / 2)

    with (run / "poses.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=[
            "frame", "timestamp", "stamp", "position.x", "position.y", "position.z",
            "orientation.x", "orientation.y", "orientation.z", "orientation.w"])
        writer.writeheader()
        for k in range(31):
            t = k * 0.1
            qx, qy, qz, qw = quat(0.0)
            writer.writerow({"frame": "base_link", "timestamp": t, "stamp": t,
                             "position.x": t, "position.y": 0.0, "position.z": 0.0,
                             "orientation.x": qx, "orientation.y": qy, "orientation.z": qz,
                             "orientation.w": qw})
            qx, qy, qz, qw = quat(ODOM[2])
            writer.writerow({"frame": "odom", "timestamp": t, "stamp": t,
                             "position.x": ODOM[0], "position.y": ODOM[1], "position.z": 0.0,
                             "orientation.x": qx, "orientation.y": qy, "orientation.z": qz,
                             "orientation.w": qw})

    (tmp_path / "campaign" / "_transient").mkdir()
    (tmp_path / "campaign" / "_config").mkdir()
    # The frozen .vast: what the config view's map2d panel declares, in the map frame.
    (tmp_path / "campaign" / "_config" / "nav.vast").write_text(yaml.safe_dump({
        "visualization": {"config": {"panels": [
            {"parameters": {}},
            {"map2d": {"map": "files/room.yaml", "markers": [
                {"kind": "pose", "pos": [-1.0, -1.0], "yaw": 0.0, "label": "start"},
                {"kind": "pose", "param": "goal", "label": "goal", "color": "#4ade80"},
            ]}},
        ]}},
    }))
    (tmp_path / "campaign" / "_transient" / "configurations.yaml").write_text(yaml.safe_dump({
        "vast": "/somewhere/on/the/service/nav.vast",
        "configs": [{
            "name": "cfg",
            "config": {
                "goal": {"position": {"x": 3.0, "y": 2.0}, "orientation": {"yaw": 0.0}},
                "objects": [{"entity_name": "crate", "model": "models/box.sdf",
                             "spawn_pose": {"position": {"x": 2.0, "y": 1.0},
                                            "orientation": {"yaw": 0.3}}}],
            },
            "_goal_parameter_name": "goal",
            "_objects_parameter_name": "objects",
            "_path": [{"x": 0.0, "y": 0.0}, {"x": 3.0, "y": 2.0}],
            "sim": {"instances": [{"name": "crate", "pose": {"x": 2.0, "y": 1.0},
                                   "size": [0.5, 0.5, 1.0]}]},
        }]
    }))
    return run


def _overlay(run_dir, **options):
    overlay = vo.CostmapOverlay.from_spec(options, Placement())
    overlay.prepare(640, 360, state=run_dir / "run.npz")
    return overlay


# -- the palette is the web panel's -------------------------------------------------------------------


@pytest.mark.parametrize("value, expected", [
    (-1, (0, 0, 0, 0)), (0, (255, 255, 255, 255)), (50, (128, 128, 128, 255)),
    (99, (3, 3, 3, 255)), (100, (0, 0, 0, 255)),
])
def test_map_colours_match_the_panel(value, expected):
    assert vo.map_color(value) == expected
    assert tuple(vo.palette(vo.map_color)[value & 0xFF]) == expected


@pytest.mark.parametrize("value, expected", [
    (-1, (0, 0, 0, 0)), (0, (0, 0, 0, 0)), (50, (128, 0, 127, 150)),
    (99, (0, 255, 255, 150)), (100, (255, 0, 255, 150)),
])
def test_costmap_colours_match_the_panel(value, expected):
    assert vo.costmap_color(value) == expected
    assert tuple(vo.palette(vo.costmap_color)[value & 0xFF]) == expected


# -- loading ------------------------------------------------------------------------------------------


@pytest.fixture
def layers():
    """The layers this fixture records: the default set minus the global costmap."""
    return {"map": {"topic": "/map"}, "local": {"topic": "/local_costmap/costmap"},
            "poses": {"table": "poses"}}


def test_the_run_directory_is_read_beside_the_recording(run_dir, layers):
    overlay = _overlay(run_dir, layers=layers)
    assert overlay.run == run_dir
    assert set(overlay._grids) == {"map", "local"}


def test_a_stated_layer_whose_topic_was_not_recorded_is_named(run_dir, layers):
    layers["global"] = {"topic": "/global_costmap/costmap"}
    with pytest.raises(vo.NavVideoError, match="no frames of /global_costmap/costmap"):
        _overlay(run_dir, layers=layers)


def test_no_binding_takes_the_default_layers_the_run_recorded(run_dir, caplog):
    """`--overlay costmap` alone: this run has /map and the local costmap, not the global one."""
    with caplog.at_level("INFO", logger="robovast_nav.video_overlay"):
        overlay = _overlay(run_dir)
    assert set(overlay._grids) == {"map", "local"}
    assert "layers map, local" in caplog.text


def test_no_binding_and_none_of_the_default_topics_is_refused(run_dir):
    rows = list(csv.DictReader((run_dir / "costmaps.csv").open()))
    with (run_dir / "costmaps.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        for r in rows:
            writer.writerow({**r, "topic": "/other"})
    with pytest.raises(vo.NavVideoError, match="holds none of .*it has: /other"):
        _overlay(run_dir)


def test_a_run_without_costmaps_names_the_decoder_entry(run_dir, layers):
    (run_dir / "costmaps.csv").unlink()
    with pytest.raises(vo.NavVideoError, match="no costmaps table: .*rosbags_costmap_to_csv"):
        _overlay(run_dir, layers=layers)


def test_a_grid_in_a_frame_the_poses_lack_is_refused(run_dir, layers):
    rows = list(csv.DictReader((run_dir / "poses.csv").open()))
    with (run_dir / "poses.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(r for r in rows if r["frame"] != "odom")
    with pytest.raises(vo.NavVideoError, match=r"in odom, which .* \(it has: base_link\)"):
        _overlay(run_dir, layers=layers)


def test_a_run_outside_a_campaign_is_refused(tmp_path, layers):
    run = tmp_path / "loose" / "cfg" / "0"
    run.mkdir(parents=True)
    with pytest.raises(vo.NavVideoError, match="not inside a campaign directory"):
        _overlay(run, layers=layers)


def test_the_poses_come_from_the_recording_when_the_run_has_one(tmp_path):
    """The table the decoder builds from a recording, read the same way as a run's own file."""
    from tests.robovast_data.conftest import nav_campaign

    run = nav_campaign(tmp_path / "nav-2026-01-01-00000000") / "cfg" / "0"
    poses = vo.read_poses(vo.run_table(run, "poses", "test"))
    assert "base_link" in poses and len(poses["base_link"].times) > 1
    assert list(poses["base_link"].times) == sorted(poses["base_link"].times)


def test_an_unknown_robot_frame_lists_the_frames_present(run_dir, layers):
    with pytest.raises(vo.NavVideoError, match="no frame 'base_footprint' .*base_link, odom"):
        _overlay(run_dir, layers=layers, robot_frame="base_footprint")


def test_an_unknown_option_is_refused():
    with pytest.raises(vo.NavVideoError, match="unknown option"):
        vo.CostmapOverlay.from_spec({"follow": 3.0}, Placement())


def test_markers_come_from_the_configuration(run_dir, layers):
    overlay = _overlay(run_dir, layers=layers, markers=[])
    kinds = sorted(m.kind for m in overlay._markers)
    assert kinds == ["box", "path", "pose"]
    without = _overlay(run_dir, layers=layers, planned_path=False, goal=False, obstacles=False)
    assert without._markers == []


def test_the_map2d_panels_declared_markers_are_drawn_unless_stated_otherwise(run_dir, layers):
    """The same declaration the web config view resolves -- a literal start, a goal read from
    the configuration's parameter -- lands beside the contributed markers."""
    overlay = _overlay(run_dir, layers=layers)
    declared = [m for m in overlay._markers if m.group == "declared"]
    assert [(m.label, m.pos) for m in declared] == [("start", [-1.0, -1.0]), ("goal", [3.0, 2.0])]
    panel = overlay.render(1.0)
    x, y = overlay._to_pixels(3.0, 2.0)
    r, g, b, _a = _pixel(panel, x, y)
    assert g > 180 and r < 120, f"expected the goal's green, got {(r, g, b)}"

    stated = _overlay(run_dir, layers=layers, markers=[{"kind": "pose", "pos": [0.5, 0.5], "label": "x"}])
    assert [m.label for m in stated._markers if m.group == "declared"] == ["x"]
    assert [m for m in _overlay(run_dir, layers=layers, markers=[])._markers if m.group == "declared"] == []


def test_a_missing_frozen_vast_is_named_unless_markers_are_stated(run_dir, layers):
    (run_dir.parent.parent / "_config" / "nav.vast").unlink()
    with pytest.raises(vo.NavVideoError, match="nav.vast is missing"):
        _overlay(run_dir, layers=layers)
    _overlay(run_dir, layers=layers, markers=[])


def test_a_missing_configurations_file_is_named_unless_markers_are_off(run_dir, layers):
    (run_dir.parent.parent / "_transient" / "configurations.yaml").unlink()
    with pytest.raises(vo.NavVideoError, match="configurations.yaml is missing"):
        _overlay(run_dir, layers=layers)
    _overlay(run_dir, layers=layers, planned_path=False, goal=False, obstacles=False)


# -- geometry -----------------------------------------------------------------------------------------


def _pixel(image, x, y):
    return image.getpixel((int(round(x)), int(round(y))))


def test_the_lethal_cell_lands_where_the_odom_pose_puts_it(run_dir, layers):
    """Grid (col 1, row 2) at 0.5 m is (0.75, 1.25) in odom; odom is at (1, 0.5) turned a quarter,
    so in the map it is (-0.25, 1.25). The panel must paint purple there and grey one cell over."""
    overlay = _overlay(run_dir, layers=layers, planned_path=False, goal=False, obstacles=False,
                       trail=False)
    panel = overlay.render(1.0)
    x, y = overlay._to_pixels(-0.25, 1.25)
    r, g, b, _a = _pixel(panel, x, y)
    assert r > 180 and g < 60 and b > 180, f"expected lethal purple, got {(r, g, b)}"
    # One local cell further along odom's x (map -y): free, so the map shows through.
    x2, y2 = overlay._to_pixels(-0.25, 0.75)
    r2, g2, b2, _a = _pixel(panel, x2, y2)
    assert abs(r2 - g2) < 5 and abs(g2 - b2) < 5, f"expected grey, got {(r2, g2, b2)}"


def test_the_map_extent_sets_the_view(run_dir, layers):
    overlay = _overlay(run_dir, layers=layers)
    cx, cy, ppm = overlay._view
    assert (cx, cy) == pytest.approx((1.5, 1.0))  # -1..4 by -1..3
    w, h = overlay._panel
    assert w == 192 and h == pytest.approx(192 * 4 / 5, abs=1)
    assert ppm == pytest.approx(min(w / (5 * 1.05), h / (4 * 1.05)))


def test_a_stale_frame_is_withheld_and_said(run_dir, layers):
    """Two frames a second apart give a 2 s window; at t=10 the nearest is 8 s away."""
    overlay = _overlay(run_dir, layers=layers, planned_path=False, goal=False, obstacles=False,
                       trail=False)
    panel = overlay.render(10.0)
    x, y = overlay._to_pixels(-0.25, 1.25)
    r, g, b, _a = _pixel(panel, x, y)
    assert not (r > 180 and g < 60 and b > 180), "a stale local costmap was drawn as current"
    # The note is painted in the panel: some warm pixels along its bottom rows.
    bottom = np.asarray(panel)[-18:, :, :3]
    assert (bottom[..., 0] > 200).any()


def test_stale_after_can_be_set(run_dir, layers):
    """0.4 s from the t=1 frame is within the default window (two 1 s periods) and outside 0.1 s."""
    x, y = None, None
    for limit, expect_drawn in ((None, True), (0.1, False)):
        overlay = _overlay(run_dir, layers=layers, planned_path=False, goal=False, obstacles=False,
                           trail=False, stale_after=limit)
        panel = overlay.render(1.4)
        x, y = overlay._to_pixels(-0.25, 1.25)
        r, g, b, _a = _pixel(panel, x, y)
        assert (r > 180 and g < 60 and b > 180) is expect_drawn, (limit, (r, g, b))


def test_a_grid_is_decoded_once_per_row(run_dir, layers, monkeypatch):
    calls = []
    real = vo.GridRow.cells

    def counting(self):
        calls.append(self.t)
        return real(self)

    monkeypatch.setattr(vo.GridRow, "cells", counting)
    overlay = _overlay(run_dir, layers=layers)
    for t in (1.0, 1.1, 1.2, 2.0, 2.1):
        overlay.render(t)
    assert sorted(calls) == [0.0, 1.0, 2.0]  # the map once, each local row once


# -- the frame contract ----------------------------------------------------------------------------------


def test_draw_paints_the_inset_at_its_placement_and_keeps_the_frame(run_dir, layers):
    overlay = _overlay(run_dir, layers=layers)
    frame = np.zeros((360, 640, 3), dtype=np.uint8)
    out = overlay.draw(frame, 1.5)
    assert out is frame and out.shape == (360, 640, 3) and out.dtype == np.uint8
    w, _h = overlay._panel
    assert tuple(out[12, 640 - 12 - 1]) == (0x12, 0x17, 0x1F)  # the panel's background, top-right
    assert not out[:, : 640 - w - 12].any()  # nothing painted left of the inset
