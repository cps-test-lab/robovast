# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``draw_config`` end to end: a campaign's configuration, its map and a run's track.

The track is thinned for drawing, so what is pinned is that the thinning is an even stride
over the whole run -- first pose to last -- and says so, never the start of the run shown
as all of it.
"""

import io

import pytest
import yaml

from robovast.mcp_server import service_access
from robovast.mcp_server.plugins import results
from tests.robovast_data.conftest import write_store

CAMPAIGN = "camp-draw-2026-09-21-120000"
_POSES = 5001


def _campaign(results_root):
    pil_image = pytest.importorskip("PIL.Image")
    root = results_root / CAMPAIGN
    run = root / "cfg-a" / "0"
    run.mkdir(parents=True)
    (root / "_execution").mkdir()
    maps = root / "_config" / "maps"
    maps.mkdir(parents=True)
    (root / "_transient").mkdir()
    buf = io.BytesIO()
    pil_image.new("L", (100, 20), 254).save(buf, format="PPM")
    (maps / "room.pgm").write_bytes(buf.getvalue())
    (maps / "room.yaml").write_text("image: room.pgm\nresolution: 0.1\norigin: [0, -1, 0]\n"
                                    "negate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n")
    (root / "_config" / "campaign.vast").write_text(yaml.safe_dump(
        {"configuration": [{"name": "cfg", "variations": [{"PathVariationRandom": {}}]}]}))
    (root / "_transient" / "configurations.yaml").write_text(yaml.safe_dump({"configs": [{
        "name": "cfg-a", "_config_name": "cfg", "config": {"map_file": "maps/room.yaml"},
        "_path": [{"x": 0.0, "y": 0.0}, {"x": 10.0, "y": 0.0}]}]}))
    lines = ["timestamp,stamp,frame,position.x,position.y"]
    lines += [f"{i * 0.01},{i * 0.01},base_link,{i * 10.0 / (_POSES - 1)},0.2"
              for i in range(_POSES)]
    (run / "poses.csv").write_text("\n".join(lines) + "\n")
    write_store(root, {"cfg-a": {"runs": {0: "passed"}}})
    return root


@pytest.fixture(name="campaign")
def _campaign_fixture(monkeypatch, tmp_path):
    pytest.importorskip("robovast_nav")
    pytest.importorskip("matplotlib")
    monkeypatch.setenv("ROBOVAST_WORKSPACES_ROOT", str(tmp_path / "workspaces"))
    from tests.service.null_service import serving
    service = serving(tmp_path / "results", tmp_path / "workspaces")
    monkeypatch.setattr(service_access, "service_client", lambda: service)
    return _campaign(tmp_path / "results")


def test_the_drawn_track_spans_the_whole_run_and_says_it_was_thinned(campaign):
    track, label = results._drawn_track(CAMPAIGN, "cfg-a", 0, "poses", "base_link")
    assert len(track) <= results._DRAWN_TRACK_POINTS + 1
    assert track[0][0] == pytest.approx(0.0)
    assert track[-1][0] == pytest.approx(10.0)
    assert f"of {_POSES} poses" in label


def test_an_unrecorded_frame_is_refused_with_the_recorded_ones(campaign):
    with pytest.raises(ValueError, match="base_link"):
        results._drawn_track(CAMPAIGN, "cfg-a", 0, "poses", "odom")


def test_draw_config_returns_the_configuration_over_its_map_with_the_track(campaign):
    image = results.draw_config(CAMPAIGN, "cfg-a", run_id=0)
    assert image.data.startswith(b"\x89PNG")
