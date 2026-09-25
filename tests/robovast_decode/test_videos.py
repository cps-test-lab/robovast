# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A camera topic becomes a WebM beside the recording and a row saying what it holds."""

import io
import shutil
from types import SimpleNamespace

import pytest

from robovast_decode.handlers import HandlerError, Videos
from robovast_decode.registry import plan_for

TOPIC = "/camera/image_raw/compressed"


def _jpeg(shade: int) -> bytes:
    image = pytest.importorskip("PIL.Image")
    out = io.BytesIO()
    image.new("RGB", (32, 24), (shade, shade, shade)).save(out, format="JPEG")
    return out.getvalue()


def _feed(handler, frames):
    for i, shade in enumerate(frames):
        handler.message(TOPIC, SimpleNamespace(data=_jpeg(shade)),
                        "sensor_msgs/msg/CompressedImage", 1_000_000_000 * (10 + i))


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_a_camera_topic_is_encoded_and_described(tmp_path):
    handler = Videos([(TOPIC, 30.0)])
    handler.output_dir, handler.bag_name = str(tmp_path), "rosbag2"
    _feed(handler, [0, 80, 160, 240])
    handler.end({TOPIC: "sensor_msgs/msg/CompressedImage"})
    (row,) = handler.buffers["videos"].to_arrow(handler.orders["videos"]).to_pylist()
    assert row["file"] == "rosbag2_camera_image_raw_compressed.webm"
    assert (tmp_path / row["file"]).stat().st_size > 0
    assert row["t_start"] == 10.0 and row["t_end"] == 13.0 and row["frames"] == 4
    assert row["fps"] == 1.0, "(n - 1) frames over the recorded span"
    assert not list(tmp_path.glob(".webm-frames-*")), "the spool is removed"


def test_without_ffmpeg_the_table_fails_with_that_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    handler = Videos([(TOPIC, 30.0)])
    handler.output_dir = str(tmp_path)
    _feed(handler, [0])
    with pytest.raises(HandlerError, match="ffmpeg is not installed"):
        handler.end({TOPIC: "sensor_msgs/msg/CompressedImage"})
    assert not list(tmp_path.glob(".webm-frames-*"))


def test_every_configured_camera_shares_one_videos_table():
    plan = plan_for("rosbag2", {TOPIC: "sensor_msgs/msg/CompressedImage",
                                "/rear/compressed": "sensor_msgs/msg/CompressedImage"},
                    [{"type": "to_webm", "topic": TOPIC},
                     {"type": "to_webm", "topic": "/rear/compressed", "fps": 10}])
    assert isinstance(plan.tables["videos"], Videos)
    assert set(plan.tables["videos"].topics()) == {TOPIC, "/rear/compressed"}


def test_a_named_topic_that_gave_no_frame_fails_the_table(tmp_path):
    """A camera panel would otherwise say "no video" for a topic that was never recorded."""
    handler = Videos([(TOPIC, 30.0), ("/rear/compressed", 30.0)])
    handler.output_dir = str(tmp_path)
    _feed(handler, [0])
    with pytest.raises(HandlerError, match="/rear/compressed"):
        handler.end({TOPIC: "sensor_msgs/msg/CompressedImage"})
    assert not list(tmp_path.glob(".webm-frames-*")), "the spool is removed"
